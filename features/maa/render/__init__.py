"""
图片化报告 — 对外入口。

    result = await render_report(markdown, report_cfg, meta, attachments, ...)
    # result.images 非空 → 报告已渲染为图片

渲染失败 / 浏览器缺失时返回 used_browser=False，由上层决定回退文本。
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import ReportConfig
from features.maa.render.browser import BrowserRenderer, BrowserUnavailable, find_browser
from features.maa.render.markdown import clean_markdown
from features.maa.render.template import TemplateSet, load_template

logger = logging.getLogger("Maa.Render")

# (data_dir, template_root) → 常驻浏览器
_RENDERERS: dict[tuple[str, str, str], BrowserRenderer] = {}


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


def _renderer(data_dir: str | Path, template_root: str | Path,
              browser_path: str = "") -> BrowserRenderer:
    key = (
        str(Path(data_dir).resolve()),
        str(Path(template_root).resolve()),
        browser_path,
    )
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

    renderer = _renderer(data_dir, template_root, report.browser_path)
    embed = bool(report.embed_attachments)
    attachments = attachments or {}
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
    """异步包装：渲染在线程中执行。"""
    if report.render_mode() == "text":
        return RenderOutcome(reason="render=text",
                             clean_markdown=clean_markdown(markdown))
    return await asyncio.to_thread(
        render_sync, markdown, report,
        meta=meta, attachments=attachments, data_dir=data_dir,
        template_root=template_root,
    )


def close_all() -> None:
    """关闭全部常驻浏览器（运行时停止 / 重载）。"""
    renderers = list(_RENDERERS.values())
    _RENDERERS.clear()
    for renderer in renderers:
        try:
            renderer.close()
        except Exception as exc:
            logger.debug(f"[渲染] 关闭浏览器失败：{exc}")
