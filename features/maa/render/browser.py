"""
无头浏览器渲染（Edge / Chrome / Chromium + CDP）。

要点：
  - 只用 Chromium 系的 DevTools 协议；Firefox 不支持 CDP，不做适配
  - 独立 `user-data-dir`，不碰用户配置
  - `--host-resolver-rules="MAP * 0.0.0.0"` 阻断一切外联
  - 逐页构建 DOM 后截图（页脚与页码天然正确）
  - `device_scale_factor` 默认 1；超体积时同页改拍 JPEG
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from core.models import ReportConfig
from features.maa.render.paginate import (
    MEASURE_BLOCKS_JS,
    MEASURE_CHROME_JS,
    MEASURE_PAGE_BOX_JS,
    BlockMetrics,
    pack_blocks,
    truncate_pages,
)
from features.maa.render.template import TemplateSet, load_template

logger = logging.getLogger("Maa.Render.Browser")

_START_TIMEOUT = 15.0
# 单页 PNG 超过该体积时改拍 JPEG（内部阈值：仅为控制消息体积，不对外配置）
_MAX_PNG_BYTES = 4 * 1024 * 1024
# 非 root 运行时也会出现的正常噪音，不能当作失败原因上报
_STDERR_NOISE = (
    "Failed to connect to the bus",
    "chrome_crashpad_handler",
    "recvmsg:",
    "ERROR:dbus",
    "dconf-CRITICAL",
    "Fontconfig",
)


class BrowserUnavailable(RuntimeError):
    """浏览器不可用（未安装 / 启动失败 / 渲染失败）。"""


def find_browser(browser_path: str = "") -> Optional[str]:
    """按配置或平台默认顺序探测浏览器可执行文件。"""
    configured = str(browser_path or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        logger.warning(
            f"[渲染] report.browser_path 无效（不是文件）：{configured}，回退自动探测"
        )

    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            candidates.extend([
                str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                str(Path(base) / "Google/Chrome/Application/chrome.exe"),
            ])
        for name in ("msedge", "chrome", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])
    else:
        for name in (
            "microsoft-edge", "microsoft-edge-stable", "google-chrome",
            "google-chrome-stable", "chromium", "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        for base in ("/usr/bin", "/usr/local/bin", "/snap/bin"):
            for name in ("microsoft-edge", "google-chrome", "chromium"):
                candidates.append(str(Path(base) / name))

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _sandbox_args() -> list[str]:
    """
    root 下 Chromium 无法建立有效沙箱（没有更低的身份可降权），fail-closed 直接退出，
    必须显式关闭沙箱才能启动。非 root 不追加，保持原有安全边界。
    """
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and getuid() == 0:
        return ["--no-sandbox", "--disable-setuid-sandbox"]
    return []


def _read_tail(path: Path, limit: int = 8, max_chars: int = 240) -> str:
    """
    读取浏览器 stderr 末尾若干行，用于把失败原因带进日志/通知。

    过滤 bus / crashpad 等噪音行（非 root 运行时正常也会刷），去重后截断，
    避免把整屏无关输出塞进群消息。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(noise in line for noise in _STDERR_NOISE):
            continue
        if line not in lines:
            lines.append(line)
    detail = " | ".join(lines[-limit:])
    return detail if len(detail) <= max_chars else detail[:max_chars] + "…"


class _CDP:
    """极简 CDP 客户端（浏览器级 WebSocket + sessionId）。"""

    def __init__(self, url: str, timeout: float):
        from websockets.sync.client import connect

        self._ws = connect(url, max_size=None, open_timeout=timeout)
        self._next_id = 1
        self.timeout = timeout

    def send(self, method: str, params: Optional[dict] = None,
             session_id: str = "", deadline: Optional[float] = None) -> dict:
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        message_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps(message))

        while True:
            remaining = self.timeout
            if deadline is not None:
                remaining = max(0.05, deadline - time.monotonic())
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise BrowserUnavailable(f"等待 CDP 响应超时：{method}") from exc
            data = json.loads(raw)
            if data.get("id") != message_id:
                continue
            if "error" in data:
                raise BrowserUnavailable(
                    f"CDP {method} 失败：{data.get('error')}"
                )
            return data.get("result") or {}

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class BrowserRenderer:
    """常驻的无头浏览器实例（渲染串行化）。"""

    def __init__(self, data_dir: str | Path, template_root: str | Path,
                 browser_path: str = ""):
        # 浏览器子进程的工作目录不可控：配置目录必须是绝对路径
        self.data_dir = Path(data_dir).resolve()
        self.template_root = Path(template_root).resolve()
        self.browser_path = browser_path
        self.exe: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cdp: Optional[_CDP] = None
        self._session_id = ""
        self._frame_id = ""
        self._profile = self.data_dir / "_browser_profile"

    # ── 生命周期 ──

    @property
    def available(self) -> bool:
        return self.exe is not None or find_browser(self.browser_path) is not None

    def start(self) -> None:
        if self._cdp is not None:
            return
        exe = find_browser(self.browser_path)
        if not exe:
            raise BrowserUnavailable(
                "未找到可用的 Chromium 系浏览器（Edge / Chrome / Chromium）"
            )
        self.exe = exe
        # 独立配置目录：优先复用固定目录；被占用时换唯一目录，
        # 避免读到上次遗留的 DevToolsActivePort 而连到已失效的端口。
        try:
            shutil.rmtree(self._profile)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"[渲染] 清理浏览器配置目录失败，改用临时目录：{exc}")
            self._profile = self.data_dir / (
                f"_browser_profile_{os.getpid()}_{int(time.time() * 1000)}"
            )
        # 清理历史崩溃遗留的临时配置目录
        for stale in self.data_dir.glob("_browser_profile_*"):
            shutil.rmtree(stale, ignore_errors=True)
        try:
            self._profile.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BrowserUnavailable(f"无法创建浏览器配置目录：{exc}") from exc

        args = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-dev-shm-usage",
            *_sandbox_args(),
            f"--user-data-dir={self._profile}",
            "--remote-debugging-port=0",
            '--host-resolver-rules=MAP * 0.0.0.0',
            "about:blank",
        ]
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        logger.info(f"[渲染] 启动浏览器：{exe}（profile={self._profile}）")
        # 保留 stderr：浏览器拒绝启动时（沙箱 / 参数 / 依赖缺失）原因只在这里
        stderr_log: Optional[Path] = self.data_dir / "_browser_stderr.log"
        stderr_target: Any = subprocess.DEVNULL
        try:
            stderr_target = open(stderr_log, "wb")
        except OSError:
            stderr_log = None
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=stderr_target,
                creationflags=creationflags,
            )
        finally:
            if stderr_target is not subprocess.DEVNULL:
                stderr_target.close()

        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + _START_TIMEOUT
        lines: list[str] = []
        exited_at = 0.0
        while time.monotonic() < deadline:
            if port_file.is_file():
                try:
                    lines = port_file.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                if len(lines) >= 2:
                    break
            # Chromium 系可能在启动后把工作交给子进程，父进程先退出；
            # 因此进程退出本身不代表失败，继续等调试端口出现。
            if self._proc.poll() is not None and exited_at <= 0:
                exited_at = time.monotonic()
            if exited_at > 0 and time.monotonic() - exited_at > 5:
                break
            time.sleep(0.1)
        if len(lines) < 2:
            logger.warning(
                f"[渲染] 调试端口未就绪：port_file={port_file.exists()}"
                f" lines={len(lines)} exit={self._proc.poll()}"
            )
            detail = _read_tail(stderr_log) if stderr_log else ""
            if detail:
                logger.warning(f"[渲染] 浏览器输出：{detail}")
            raise BrowserUnavailable(
                "浏览器未在超时内暴露调试端口（可能是启动参数被拒绝或配置目录被占用）"
                + (f"；浏览器输出：{detail}" if detail else "")
            )

        port, path = lines[0].strip(), lines[1].strip()
        url = f"ws://127.0.0.1:{port}{path}"
        self._cdp = _CDP(url, timeout=max(5.0, 20.0))

        target = self._cdp.send("Target.createTarget", {"url": "about:blank"})
        target_id = target.get("targetId")
        if not target_id:
            raise BrowserUnavailable("无法创建浏览器页面")
        attached = self._cdp.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._session_id = str(attached.get("sessionId") or "")
        if not self._session_id:
            raise BrowserUnavailable("无法附加到浏览器页面")

        self._cdp.send("Page.enable", session_id=self._session_id)
        self._cdp.send("Runtime.enable", session_id=self._session_id)
        frame_tree = self._cdp.send("Page.getFrameTree", session_id=self._session_id)
        frame = (frame_tree.get("frameTree") or {}).get("frame") or {}
        self._frame_id = str(frame.get("id") or "")
        if not self._frame_id:
            raise BrowserUnavailable("无法获取页面 frameId")
        logger.info(f"[渲染] 浏览器已启动：{exe}")

    def close(self) -> None:
        if self._cdp is not None:
            try:
                self._cdp.send("Browser.close", deadline=time.monotonic() + 2)
            except Exception:
                pass
            self._cdp.close()
            self._cdp = None
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._session_id = ""
        self._frame_id = ""

    # ── 页面操作 ──

    def _eval(self, expression: str, deadline: float) -> Any:
        assert self._cdp is not None
        result = self._cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id=self._session_id,
            deadline=deadline,
        )
        if result.get("exceptionDetails"):
            raise BrowserUnavailable(
                f"页面脚本异常：{result.get('exceptionDetails')}"
            )
        return (result.get("result") or {}).get("value")

    def _set_html(self, document: str, deadline: float) -> None:
        assert self._cdp is not None
        self._cdp.send(
            "Page.setDocumentContent",
            {"frameId": self._frame_id, "html": document},
            session_id=self._session_id,
            deadline=deadline,
        )
        # 等字体加载完成，避免中文回退字体导致测量偏差
        try:
            self._eval("document.fonts.ready.then(() => 1)", deadline)
        except BrowserUnavailable:
            pass

    def _measure(self, script: str, deadline: float) -> dict:
        value = self._eval(script, deadline)
        return value if isinstance(value, dict) else {}

    def _capture(self, box: dict, max_height: int,
                 deadline: float) -> bytes:
        assert self._cdp is not None
        width = max(1.0, float(box.get("width") or 0))
        height = min(float(box.get("height") or 0), float(max_height))
        if height <= 1:
            raise BrowserUnavailable("页面高度为 0，无法截图")
        clip = {
            "x": float(box.get("x") or 0),
            "y": float(box.get("y") or 0),
            "width": width,
            "height": height,
            "scale": 1,
        }
        result = self._cdp.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": clip,
                "captureBeyondViewport": True,
                "fromSurface": True,
            },
            session_id=self._session_id,
            deadline=deadline,
        )
        data = str(result.get("data") or "")
        if not data:
            raise BrowserUnavailable("截图返回为空")
        return base64.b64decode(data)

    # ── 渲染 ──

    def render(
        self,
        markdown: str,
        report: ReportConfig,
        template: TemplateSet,
        meta: dict,
        *,
        attachment_html,
    ) -> list[bytes]:
        """把报告渲染为若干 PNG/JPEG 字节（串行）。"""
        from features.maa.render.markdown import parse_report

        timeout = max(1.0, float(report.render_timeout_ms or 8000) / 1000.0)
        parsed = parse_report(markdown, attachment_html)
        if not parsed.blocks:
            raise BrowserUnavailable("报告没有可渲染的内容")

        width = max(320, int(report.max_width_px or 1080))
        max_height = max(320, int(report.max_height_px or 4096))

        try:
            return self._render_once(parsed, report, template, meta, width,
                                     max_height, timeout)
        except Exception as exc:
            # 进程 / 连接异常或超时：重启一次后重试
            logger.warning(f"[渲染] 首次渲染失败，尝试重启浏览器：{exc}")
            self.close()
            try:
                self.start()
            except Exception as restart_exc:
                raise BrowserUnavailable(str(restart_exc)) from restart_exc
            return self._render_once(parsed, report, template, meta, width,
                                     max_height, timeout)

    def _render_once(self, parsed, report: ReportConfig, template: TemplateSet,
                     meta: dict, width: int, max_height: int,
                     timeout: float) -> list[bytes]:
        self.start()
        assert self._cdp is not None
        deadline = time.monotonic() + timeout

        self._cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": max_height,
                "deviceScaleFactor": max(0.5, float(report.device_scale_factor or 1.0)),
                "mobile": False,
            },
            session_id=self._session_id,
            deadline=deadline,
        )
        self._eval(f"window.__MAA_MAX_PAGE_HEIGHT__ = {max_height};", deadline)

        # ① 空正文测模板自身占用
        empty_doc = template.document(
            template.render_page(content="", meta=meta, page=1, pages=1),
            width=width, max_height=max_height, font_family=report.font_family,
        )
        self._set_html(empty_doc, deadline)
        chrome = self._measure(MEASURE_CHROME_JS, deadline)
        available = float(chrome.get("available") or (max_height * 0.8))

        # ② 全部内容块测高度
        all_content = "".join(block.html for block in parsed.blocks)
        measure_doc = template.document(
            template.render_page(content=all_content, meta=meta, page=1, pages=1),
            width=width, max_height=max_height, font_family=report.font_family,
        )
        self._set_html(measure_doc, deadline)
        measured = self._measure(MEASURE_BLOCKS_JS, deadline)
        heights = measured.get("heights") or []
        if len(heights) != len(parsed.blocks):
            raise BrowserUnavailable("内容块测量数量不一致")

        metrics = [
            BlockMetrics(index=index, height=float(heights[index] or 0),
                         force_break_after=block.force_break_after)
            for index, block in enumerate(parsed.blocks)
        ]
        pages, pack_warnings = pack_blocks(metrics, available)
        pages, truncated = truncate_pages(pages, int(report.max_pages or 6))
        for warning in pack_warnings:
            logger.warning(f"[渲染] {warning}")

        total = len(pages)
        images: list[bytes] = []
        for page_index, block_ids in enumerate(pages, 1):
            content = "".join(parsed.blocks[index].html for index in block_ids)
            if truncated and page_index == total:
                content += (
                    '<section class="block"><h2 class="block-title">提示</h2>'
                    "<p>报告过长，已按 max_pages 截断。</p></section>"
                )
            document = template.document(
                template.render_page(
                    content=content, meta=meta, page=page_index, pages=total
                ),
                width=width, max_height=max_height,
                font_family=report.font_family,
            )
            self._set_html(document, deadline)
            box = self._measure(MEASURE_PAGE_BOX_JS, deadline)
            if box.get("error"):
                raise BrowserUnavailable(f"无法测量页面：{box.get('error')}")
            image = self._capture(box, max_height, deadline)
            # 单页 PNG 过大时改拍 JPEG（内部阈值，不作为配置项）
            if len(image) > _MAX_PNG_BYTES:
                image = self._capture_jpeg(box, max_height, deadline)
            images.append(image)

        if truncated:
            logger.warning("[渲染] 报告页数超过 max_pages，已截断")
        return images

    def _capture_jpeg(self, box: dict, max_height: int, deadline: float) -> bytes:
        assert self._cdp is not None
        height = min(float(box.get("height") or 0), float(max_height))
        clip = {
            "x": float(box.get("x") or 0),
            "y": float(box.get("y") or 0),
            "width": max(1.0, float(box.get("width") or 0)),
            "height": max(1.0, height),
            "scale": 1,
        }
        result = self._cdp.send(
            "Page.captureScreenshot",
            {
                "format": "jpeg",
                "quality": 85,
                "clip": clip,
                "captureBeyondViewport": True,
                "fromSurface": True,
            },
            session_id=self._session_id,
            deadline=deadline,
        )
        return base64.b64decode(str(result.get("data") or ""))
