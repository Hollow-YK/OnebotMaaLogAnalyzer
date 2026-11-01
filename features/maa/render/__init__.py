"""
图片化报告 — 对外入口。

    result = await render_report(markdown, report_cfg, meta, attachments, ...)
    # result.images 非空 → 报告已渲染为图片

渲染失败 / 浏览器缺失时返回 used_browser=False，由上层决定回退文本。
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import html
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.concurrency import KeyedLocks, KeyedThreadLocks
from core.models import ReportConfig
from features.maa.render.browser import BrowserRenderer, BrowserUnavailable, find_browser
from features.maa.render.markdown import clean_markdown
from features.maa.render.template import TemplateSet, load_template

logger = logging.getLogger("Maa.Render")

# (data_dir, template_root, browser_path) → 常驻浏览器
_RENDERERS: dict[tuple[str, str, str], BrowserRenderer] = {}
# 渲染器字典本身的取用互斥（线程之间）
_RENDERERS_GUARD = threading.Lock()
# 同一渲染器的浏览器进程与 CDP 会话是独占资源：渲染 / 重启 / 关闭必须串行。
# 用渲染器键（data_dir 等）而不是配置名——不同 data_dir 的渲染器互不影响。
_RENDER_SYNC_LOCKS = KeyedThreadLocks()
# 进线程之前的异步队列：让等待发生在事件循环里，而不是占满线程池
_RENDER_ASYNC_QUEUE = KeyedLocks()


@dataclass
class RenderAttachment:
    """渲染期可内联的一个附件。"""

    path: str
    kind: str          # image / file
    data: bytes = b""
    origin: str = ""   # 人类可读来源说明


@dataclass
class RenderOutcome:
    """渲染结果。"""

    images: list[bytes] = field(default_factory=list)
    used_browser: bool = False
    reason: str = ""
    clean_markdown: str = ""
    embedded_images: set[str] = field(default_factory=set)


def _key(source: str, path: str) -> str:
    return f"{source}:{str(path or '').replace(chr(92), '/').lower()}"


def _format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _attachment_html(attachment, attachments: dict[str, RenderAttachment],
                     embed_images: bool) -> str:
    """把一条附件指令转成模板可定制的 HTML 外壳。"""
    item = attachments.get(_key(attachment.source, attachment.path))
    path = html.escape(attachment.path)
    if item is None or not item.data:
        return (
            '<div class="attach-file attach-missing">'
            f'<span class="attach-name">未找到附件：{path}</span></div>'
        )
    origin = html.escape(item.origin or attachment.path)
    if item.kind == "image":
        if not embed_images:
            return (
                '<div class="attach-file attach-image-skipped">'
                f'<span class="attach-name">图片未内联：{path}</span></div>'
            )
        from core.vision import mime_for

        encoded = base64.b64encode(item.data).decode("ascii")
        return (
            '<figure class="attach-image">'
            f'<img src="data:{mime_for(item.path)};base64,{encoded}" alt="{path}">'
            f'<figcaption>{origin}</figcaption></figure>'
        )
    return (
        '<div class="attach-file">'
        f'<span class="attach-name">{html.escape(Path(item.path).name)}</span>'
        f'<span class="attach-meta">{origin} · {_format_size(len(item.data))}</span>'
        "</div>"
    )


def _render_queue_key(data_dir: str | Path) -> str:
    """渲染队列键 = 数据目录。

    浏览器进程与它使用的 `user-data-dir`（`<data_dir>/_browser_profile`）都按
    data_dir 共享，所以**排队按 data_dir**，而不是按渲染器或配置：
    同一 data_dir 下即使 `browser_path` / `template` 不同，也共用同一个
    浏览器配置文件，必须排队。
    """
    return str(Path(data_dir).resolve())


def _renderer_key(data_dir: str | Path, template_root: str | Path,
                  browser_path: str = "") -> tuple[str, str, str]:
    """渲染器身份：数据目录 + 模板目录 + 浏览器路径。"""
    return (
        str(Path(data_dir).resolve()),
        str(Path(template_root).resolve()),
        browser_path,
    )


def _renderer(data_dir: str | Path, template_root: str | Path,
              browser_path: str = "") -> BrowserRenderer:
    key = _renderer_key(data_dir, template_root, browser_path)
    with _RENDERERS_GUARD:
        renderer = _RENDERERS.get(key)
        if renderer is None:
            renderer = BrowserRenderer(data_dir, template_root, browser_path)
            _RENDERERS[key] = renderer
        return renderer


def render_sync(
    markdown: str,
    report: ReportConfig,
    *,
    meta: dict,
    attachments: Optional[dict[str, RenderAttachment]] = None,
    data_dir: str | Path = "data",
    template_root: str | Path = "templates",
) -> RenderOutcome:
    """同步渲染（在线程中调用，避免阻塞事件循环）。"""
    mode = report.render_mode()
    outcome = RenderOutcome(clean_markdown=clean_markdown(markdown))
    if mode == "text":
        outcome.reason = "render=text"
        return outcome

    if find_browser(report.browser_path) is None:
        outcome.reason = (
            "未找到可用的 Chromium 系浏览器，报告已回退为纯文本。"
            "可安装 Edge/Chrome/Chromium，或设置 report.browser_path 指向可执行文件。"
        )
        return outcome

    try:
        template: TemplateSet = load_template(template_root, report.template)
    except Exception as exc:
        outcome.reason = f"模板加载失败：{exc}"
        logger.warning(f"[渲染] {outcome.reason}")
        return outcome

    queue_key = _render_queue_key(data_dir)
    embed = bool(report.embed_attachments)
    attachments = attachments or {}
    # 同一 data_dir 下所有配置共用一个浏览器配置文件：渲染、失败重启、
    # 关闭都在同一把队列锁内完成，避免并发分析互相切页面 / 串图 / 关掉对方的连接。
    with _RENDER_SYNC_LOCKS.get(queue_key):
        renderer = _renderer(data_dir, template_root, report.browser_path)
        try:
            images = renderer.render(
                markdown,
                report,
                template,
                meta,
                attachment_html=lambda item: _attachment_html(item, attachments, embed),
            )
        except BrowserUnavailable as exc:
            outcome.reason = str(exc)
            logger.warning(f"[渲染] 图片渲染失败：{exc}")
            return outcome
        except Exception as exc:
            outcome.reason = f"渲染异常：{exc}"
            logger.warning(f"[渲染] 图片渲染异常：{exc}", exc_info=True)
            return outcome
        finally:
            if not report.reuse_browser:
                renderer.close()

    outcome.images = images
    outcome.used_browser = bool(images)
    if embed:
        outcome.embedded_images = {
            key for key, item in attachments.items() if item.kind == "image"
        }
    return outcome


async def render_report(
    markdown: str,
    report: ReportConfig,
    *,
    meta: dict,
    attachments: Optional[dict[str, RenderAttachment]] = None,
    data_dir: str | Path = "data",
    template_root: str | Path = "templates",
) -> RenderOutcome:
    """异步包装：渲染在线程中执行。

    先在渲染器队列里排队再进线程——否则并发配置会各自占一个线程一起
    抢同一个浏览器进程。
    """
    if report.render_mode() == "text":
        return RenderOutcome(reason="render=text",
                             clean_markdown=clean_markdown(markdown))
    async with _RENDER_ASYNC_QUEUE.get(_render_queue_key(data_dir)):
        return await asyncio.to_thread(
            render_sync, markdown, report,
            meta=meta, attachments=attachments, data_dir=data_dir,
            template_root=template_root,
        )


def close_all() -> None:
    """关闭全部常驻浏览器（运行时停止 / 重载）。"""
    with _RENDERERS_GUARD:
        items = list(_RENDERERS.items())
        _RENDERERS.clear()
    for _key, renderer in items:
        # 与正在跑的渲染互斥（按 data_dir 的同一把队列锁）：等它跑完再关，
        # 避免关掉别人正在用的连接
        try:
            with _RENDER_SYNC_LOCKS.get(_render_queue_key(renderer.data_dir)):
                renderer.close()
        except Exception as exc:
            logger.debug(f"[渲染] 关闭浏览器失败：{exc}")


# 兜底：进程退出时确保没有渲染浏览器残留。
# 调试脚本、临时探针、异常路径都可能忘了调用 close_all()，而一个残留的
# 无头浏览器会一直占着 <data_dir>/_browser_profile，导致数据目录删不掉。
atexit.register(close_all)
