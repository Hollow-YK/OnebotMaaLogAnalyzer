#!/usr/bin/env python3
"""
模板编辑器 — 单文件独立工具：`python tools/templates/editor.py`

功能
  - 直接读写工程根目录 `templates/` 下的模板文件（page.html / header.html /
    footer.html / style.css 以及目录内其它 .css / .html）
  - 左侧模板与文件管理，中间代码编辑（行号 / Tab 缩进 / Ctrl+S 保存），
    右侧实时渲染预览
  - 保存后自动重渲染；外部改动会被探测并提示
  - 新建 / 复制 / 删除模板；上次保存内容自动备份，可一键撤销
  - 预览走的是线上同一套 `features.maa.render` 管线，不是另写一套排版

用法
  python tools/templates/editor.py
  python tools/templates/editor.py --port 8899 --no-open
  python tools/templates/editor.py --browser "C:/.../msedge.exe"
  python tools/templates/editor.py --templates D:/somewhere/templates

依赖
  mistune / pydantic / websockets（项目 .venv 里已装）。

说明
  工具只改写 `templates/` 内的文件；备份与浏览器 profile 放在系统临时目录，
  不往仓库里写任何额外内容。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from core.models import ReportConfig
    from features.maa.render import RenderAttachment
    from features.maa.render import _attachment_html as production_attachment_html
    from features.maa.render.browser import (
        BrowserRenderer,
        BrowserUnavailable,
        find_browser,
    )
    from features.maa.render.template import load_template
except ImportError as exc:  # pragma: no cover - 启动期环境问题
    venv_python = (ROOT / ".venv" / "Scripts" / "python.exe"
                   if sys.platform.startswith("win")
                   else ROOT / ".venv" / "bin" / "python")
    print(f"导入渲染模块失败：{exc}", file=sys.stderr)
    print(
        "请在项目虚拟环境中运行，例如：\n"
        f"  {venv_python} tools/templates/editor.py",
        file=sys.stderr,
    )
    raise SystemExit(1)

TEMPLATES_ROOT = ROOT / "templates"
DATA_DIR = Path(tempfile.gettempdir()) / "maa-template-editor"
BROWSER_PATH = ""

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024

# 模板目录内可编辑的文件（其余扩展名一律拒绝）
KNOWN_FILES = ("page.html", "header.html", "footer.html", "style.css")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.(html|css|txt|json|svg|md)$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

# 浏览器只有一个 CDP 连接，渲染必须串行
_LOCK = threading.Lock()
_RENDERER: Optional[BrowserRenderer] = None
_RENDERER_KEY: Optional[tuple[str, str]] = None

META_LABELS = (
    ("title", "标题"),
    ("project", "项目"),
    ("file", "日志文件名"),
    ("generated_at", "生成时间"),
    ("tokens_input", "输入 token"),
    ("tokens_output", "输出 token"),
    ("tokens_total", "总 token"),
    ("usage_calls", "调用次数"),
    ("provider", "provider"),
    ("model", "model"),
    ("usage", "自定义用量行"),
)

PLACEHOLDERS = (
    ("title", "报告标题"),
    ("project", "项目名"),
    ("file", "日志文件名"),
    ("generated_at", "生成时间"),
    ("tokens_input", "输入 token"),
    ("tokens_output", "输出 token"),
    ("tokens_total", "总 token"),
    ("usage_calls", "模型调用次数"),
    ("provider", "provider 名称"),
    ("model", "模型名"),
    ("usage", "整行自定义用量文本"),
    ("page", "当前页码"),
    ("pages", "总页数"),
    ("header", "页眉内容（仅 page.html）"),
    ("footer", "页脚内容（仅 page.html）"),
    ("content", "正文内容（仅 page.html）"),
)

SAMPLE_MARKDOWN = """<!-- section: 结论 -->
本次日志显示 **模板匹配失败**：`PVP_Click:StartBattle` 的匹配分数为 0.62，低于配置阈值 0.70，
任务在「开始战斗」界面反复重试后因超过重试次数而中止。

<!-- section: 关键证据 -->
- `12:00:02` `Recognition.Failed` 节点 `PVP_Click:StartBattle`，分数 `0.62 < 0.70`
- `12:00:03` 已保存截图 `on_error/on_error_20260101_120002.png`
- `12:00:18` `Task.StartUp` 重试 3 次后进入 `Task.Failed`
- 运行环境：MFAAvalonia v1.2.3 / MaaFramework v4.6.0 / 资源 `narutomobile`

| 节点 | 实际分数 | 阈值 | 结果 |
| --- | --- | --- | --- |
| PVP_Click:StartBattle | 0.62 | 0.70 | 失败 |
| PVP_Click:Home | 0.94 | 0.70 | 通过 |

<!-- section: 可能原因 -->
1. **模板图过期**：游戏更新后开始按钮的图标/配色发生了变化。
2. **分辨率不匹配**：截图分辨率为 `1280x720`，而模板图取自 `1920x1080`。
3. **ROI 区域偏移**：界面语言切换后按钮位置发生位移。

```json
"PVP_Click:StartBattle": {
  "recognition": "TemplateMatch",
  "template": "PVP/StartBattle.png",
  "threshold": 0.70,
  "roi": [820, 640, 240, 90]
}
```

<!-- section: 处理建议 -->
1. 用本次截图中的按钮区域重新裁剪 `assets/resource/image/PVP/StartBattle.png`。
2. 若确认界面布局未变，可临时把 `threshold` 从 `0.70` 调整到 `0.60`。
3. 在 `1920x1080` 与 `1280x720` 两种分辨率下各跑一次，确认 ROI 是否仍然命中。

> 提示：调整阈值只能掩盖问题，优先更新模板图。

<!-- page-break -->

<!-- section: 附件与图文混排 -->
填了「附件目录」后，下面的指令会内联渲染成图片或文件块；没填时显示「未找到附件」占位。

[附件@日志: on_error/on_error_20260101_120002.png]

<!-- section: 需要补充 -->
- 若仍失败，请上传 `debug/on_error/` 目录下的完整截图。
- 附带 `config/mxu-mfaa.json`，便于核对实际生效的 ROI 与阈值。
"""


# ── 通用工具 ──

def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def default_meta() -> dict[str, str]:
    now = datetime.now()
    return {
        "title": "日志分析报告",
        "project": "MAN",
        "file": f"narutomobile-logs-{now:%Y%m%d-%H%M%S}.zip",
        "generated_at": f"{now:%Y-%m-%d %H:%M:%S}",
        "tokens_input": "18.4k",
        "tokens_output": "5.2k",
        "tokens_total": "23.6k",
        "usage_calls": "6",
        "provider": "main",
        "model": "deepseek-v4.1-flash",
        "usage": "输入 18.4k · 输出 5.2k · 合计 23.6k tokens · main/deepseek-v4.1-flash ×4",
    }


class _EditorError(ValueError):
    """参数非法（返回给前端展示，不打印堆栈）。"""


# ── 模板文件读写 ──

def _template_dir(name: Any) -> Path:
    text = str(name or "").strip()
    if not _SAFE_NAME.match(text):
        raise _EditorError(f"模板名非法：{text!r}（只允许字母、数字、下划线、连字符）")
    directory = (TEMPLATES_ROOT / text).resolve()
    if directory.parent != TEMPLATES_ROOT.resolve():
        raise _EditorError(f"模板路径越界：{text!r}")
    return directory


def _file_path(name: Any, filename: Any) -> Path:
    directory = _template_dir(name)
    text = str(filename or "").strip()
    if not _SAFE_FILE.match(text):
        raise _EditorError(f"文件名非法：{text!r}（只允许字母、数字、点、下划线、连字符）")
    path = (directory / text).resolve()
    if path.parent != directory:
        raise _EditorError(f"文件路径越界：{text!r}")
    return path


def list_templates() -> list[dict]:
    items: list[dict] = []
    if not TEMPLATES_ROOT.is_dir():
        return items
    for entry in sorted(TEMPLATES_ROOT.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if not _SAFE_NAME.match(entry.name):
            continue
        files = []
        for path in sorted(entry.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if not _SAFE_FILE.match(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "known": path.name in KNOWN_FILES,
            })
        items.append({
            "name": entry.name,
            "files": files,
            "ok": (entry / "page.html").is_file(),
        })
    return items


def templates_revision() -> str:
    """模板目录内容指纹：任意文件 mtime / size 变化都会改变返回值。"""
    digest = hashlib.sha1()
    if TEMPLATES_ROOT.is_dir():
        for path in sorted(TEMPLATES_ROOT.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(TEMPLATES_ROOT).as_posix()
            digest.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def read_file(name: Any, filename: Any) -> dict:
    path = _file_path(name, filename)
    if not path.is_file():
        return {"ok": True, "exists": False, "content": "", "mtime": 0}
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError) as exc:
        raise _EditorError(f"读取失败：{exc}") from exc
    return {"ok": True, "exists": True, "content": text, "mtime": mtime}


def _backup_path(name: str, filename: str) -> Path:
    return DATA_DIR / "backups" / name / (filename + ".bak")


def write_file(name: Any, filename: Any, content: Any) -> dict:
    path = _file_path(name, filename)
    text = "" if content is None else str(content)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise _EditorError(f"内容过大（{len(encoded)} 字节，上限 {MAX_WRITE_BYTES}）")
    directory = path.parent
    if not directory.is_dir():
        raise _EditorError(f"模板目录不存在：{directory.name}")

    # 保存前把旧内容存入临时目录，便于「撤销上次保存」
    if path.is_file():
        try:
            backup = _backup_path(directory.name, path.name)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(path.read_bytes())
        except OSError:
            pass

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        mtime = path.stat().st_mtime
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise _EditorError(f"写入失败：{exc}") from exc
    return {"ok": True, "bytes": len(encoded), "mtime": mtime}


def restore_file(name: Any, filename: Any) -> dict:
    path = _file_path(name, filename)
    backup = _backup_path(path.parent.name, path.name)
    if not backup.is_file():
        raise _EditorError("没有可撤销的备份（每个文件只保留最近一次保存前的内容）")
    try:
        text = backup.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _EditorError(f"读取备份失败：{exc}") from exc
    write_file(name, filename, text)
    return {"ok": True, "content": text}


def create_template(name: Any, base: Any) -> dict:
    target = _template_dir(name)
    if target.exists():
        raise _EditorError(f"模板已存在：{target.name}")
    source_name = str(base or "").strip()
    if source_name:
        source = _template_dir(source_name)
        if not source.is_dir():
            raise _EditorError(f"来源模板不存在：{source_name}")
        shutil.copytree(source, target)
    else:
        target.mkdir(parents=True)
        (target / "page.html").write_text(
            "<div class=\"page\">\n  {{header}}\n  <main class=\"content\">{{content}}</main>\n"
            "  {{footer}}\n</div>\n",
            encoding="utf-8",
        )
    return {"ok": True, "name": target.name}


def delete_template(name: Any) -> dict:
    target = _template_dir(name)
    if target.name == "default":
        raise _EditorError("不能删除 default 模板")
    if not target.is_dir():
        raise _EditorError(f"模板不存在：{target.name}")
    shutil.rmtree(target)
    return {"ok": True, "name": target.name}


# ── 附件索引与渲染 ──

def index_attachments(raw_dir: Any) -> dict[str, RenderAttachment]:
    """
    把「附件目录」里的文件索引成与线上一致的 key。

    两种摆放都支持（相对路径按工程根目录解析）：
        <目录>/debug/a.png       → zip:debug/a.png / repo:debug/a.png
        <目录>/zip/debug/a.png   → 同上（首段只用来区分来源，不进 key）
    """
    indexed: dict[str, RenderAttachment] = {}
    text = str(raw_dir or "").strip()
    if not text:
        return indexed
    base = Path(text).expanduser()
    if not base.is_absolute():
        base = ROOT / base
    if not base.is_dir():
        return indexed
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(base).as_posix()
        head, _, tail = rel.partition("/")
        if head in ("zip", "repo") and tail:
            source, inner = head, tail
        else:
            source, inner = "zip", rel
        kind = "image" if path.suffix.lower() in IMAGE_SUFFIXES else "file"
        item = RenderAttachment(
            path=inner, kind=kind, data=data, origin=f"{source}:{rel}",
        )
        for alias in ("zip", "repo"):
            indexed[f"{alias}:{inner.lower()}"] = item
    return indexed


class _Collector(logging.Handler):
    """收集渲染期警告（含未识别占位符的 debug 日志）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        if record.levelno < logging.WARNING and "未识别占位符" not in message:
            return
        if message not in self.messages:
            self.messages.append(message)


def _get_renderer() -> BrowserRenderer:
    global _RENDERER, _RENDERER_KEY
    key = (str(DATA_DIR), BROWSER_PATH)
    if _RENDERER is None or _RENDERER_KEY != key:
        if _RENDERER is not None:
            _RENDERER.close()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _RENDERER = BrowserRenderer(DATA_DIR, TEMPLATES_ROOT, BROWSER_PATH)
        _RENDERER_KEY = key
    return _RENDERER


def _data_url(blob: bytes) -> str:
    mime = "image/jpeg" if blob[:2] == b"\xff\xd8" else "image/png"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def run_render(payload: dict) -> dict:
    started = time.monotonic()
    collector = _Collector()
    render_logger = logging.getLogger("Maa.Render")
    previous_level = render_logger.level
    render_logger.addHandler(collector)
    render_logger.setLevel(logging.DEBUG)

    def failure(message: str, *, hint: str = "", trace: str = "") -> dict:
        return {
            "ok": False,
            "error": message,
            "hint": hint,
            "trace": trace,
            "warnings": collector.messages,
            "ms": int((time.monotonic() - started) * 1000),
        }

    try:
        markdown = str(payload.get("markdown") or "")
        if not markdown.strip():
            return failure("Markdown 为空，先写点内容或点「重置示例」")

        raw_meta = payload.get("meta")
        meta = {str(key): "" if value is None else str(value)
                for key, value in (raw_meta.items() if isinstance(raw_meta, dict) else ())}

        template = load_template(TEMPLATES_ROOT, str(payload.get("template") or "default"))
        report = ReportConfig(
            render="image",
            template=template.name,
            max_width_px=_int(payload.get("width"), 1080, 320, 2160),
            max_height_px=_int(payload.get("maxHeight"), 2000, 320, 8192),
            max_pages=_int(payload.get("maxPages"), 8, 1, 40),
            device_scale_factor=_float(payload.get("scale"), 1.0, 0.5, 4.0),
            browser_path=BROWSER_PATH,
            reuse_browser=True,
            embed_attachments=True,
            render_timeout_ms=_int(payload.get("timeoutMs"), 20000, 2000, 120000),
        )
        attachments = index_attachments(payload.get("attachDir"))

        with _LOCK:
            renderer = _get_renderer()
            images = renderer.render(
                markdown, report, template, meta,
                attachment_html=lambda item: production_attachment_html(
                    item, attachments, True,
                ),
            )

        return {
            "ok": True,
            "images": [_data_url(blob) for blob in images],
            "pages": len(images),
            "bytes": sum(len(blob) for blob in images),
            "ms": int((time.monotonic() - started) * 1000),
            "template": template.name,
            "warnings": collector.messages,
        }
    except BrowserUnavailable as exc:
        return failure(
            str(exc),
            hint="未找到 Chromium 系浏览器；可安装 Edge/Chrome，或用 --browser 指定可执行文件。",
        )
    except Exception as exc:  # noqa: BLE001 - 工具需要把任意异常回显到页面
        traceback.print_exc()
        return failure(f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
    finally:
        render_logger.removeHandler(collector)
        render_logger.setLevel(previous_level)


# ── HTTP ──

def collect_state() -> dict:
    templates = list_templates()
    names = [item["name"] for item in templates if item["ok"]]
    return {
        "root": str(TEMPLATES_ROOT),
        "templates": templates,
        "defaultTemplate": "default" if "default" in names else (names[0] if names else ""),
        "sample": SAMPLE_MARKDOWN,
        "meta": default_meta(),
        "knownFiles": list(KNOWN_FILES),
        "metaLabels": [{"key": key, "label": label} for key, label in META_LABELS],
        "placeholders": [{"key": key, "label": label} for key, label in PLACEHOLDERS],
        "defaults": {"width": 1080, "maxHeight": 2000, "maxPages": 8, "scale": 1.0},
        "browser": find_browser(BROWSER_PATH) or "",
        "revision": templates_revision(),
    }


def dispatch(path: str, payload: dict) -> dict:
    """把请求转成具体的编辑器动作。"""
    if path == "/api/state":
        return collect_state()
    if path == "/api/revision":
        return {"ok": True, "revision": templates_revision()}
    if path == "/api/render":
        return run_render(payload)
    if path == "/api/file/read":
        return read_file(payload.get("name"), payload.get("file"))
    if path == "/api/file/write":
        return write_file(payload.get("name"), payload.get("file"), payload.get("content"))
    if path == "/api/file/restore":
        return restore_file(payload.get("name"), payload.get("file"))
    if path == "/api/template/create":
        return create_template(payload.get("name"), payload.get("base"))
    if path == "/api/template/delete":
        return delete_template(payload.get("name"))
    raise _EditorError(f"未知接口：{path}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "MaaTemplateEditor/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 静默 access log
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send(index_html().encode("utf-8"), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/"):
            query = {
                key: values[-1]
                for key, values in parse_qs(parsed.query).items() if values
            }
            return self._dispatch(path, query)
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return self._send_json(
                {"ok": False, "error": f"请求体过大（{length} 字节）"}, 413,
            )
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8-sig") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._send_json({"ok": False, "error": f"请求体不是合法 JSON：{exc}"}, 400)
        if not isinstance(payload, dict):
            return self._send_json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
        return self._dispatch(path, payload)

    def _dispatch(self, path: str, payload: dict) -> None:
        try:
            result = dispatch(path, payload)
        except _EditorError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001 - 回显到页面
            traceback.print_exc()
            return self._send_json(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                 "trace": traceback.format_exc()},
                500,
            )
        if "ok" not in result:
            result["ok"] = True
        return self._send_json(result)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)


def index_html() -> str:
    return (
        _PAGE
        .replace("__META_LABELS__", json.dumps(
            [{"key": key, "label": label} for key, label in META_LABELS],
            ensure_ascii=False))
        .replace("__PLACEHOLDERS__", json.dumps(
            [{"key": key, "label": label} for key, label in PLACEHOLDERS],
            ensure_ascii=False))
        .replace("__KNOWN_FILES__", json.dumps(list(KNOWN_FILES)))
    )


def serve(host: str, port: int, open_browser: bool) -> int:
    httpd: Optional[ThreadingHTTPServer] = None
    last_error: Optional[Exception] = None
    requested = port
    for candidate in range(port, port + 30):
        try:
            httpd = ThreadingHTTPServer((host, candidate), _Handler)
            httpd.daemon_threads = True
            port = candidate
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        print(f"无法绑定端口 {requested}~{requested + 29}：{last_error}", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/"
    print(f"模板目录：{TEMPLATES_ROOT}")
    print(f"备份 / profile：{DATA_DIR}")
    print(f"渲染浏览器：{find_browser(BROWSER_PATH) or '未找到（可用 --browser 指定）'}")
    print(f"编辑器地址：{url}  （Ctrl+C 退出）", flush=True)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
        with _LOCK:
            if _RENDERER is not None:
                try:
                    _RENDERER.close()
                except Exception:
                    pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    global TEMPLATES_ROOT, DATA_DIR, BROWSER_PATH

    parser = argparse.ArgumentParser(
        description="模板编辑器：编辑 templates/ 下的模板并实时渲染预览",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8756, help="监听端口（默认 8756，被占用则自动顺延）")
    parser.add_argument("--browser", default="", help="浏览器可执行文件路径（默认自动探测）")
    parser.add_argument("--templates", default="", help="模板根目录（默认工程根目录下的 templates/）")
    parser.add_argument("--data-dir", default="", help="浏览器 profile / 备份目录（默认系统临时目录）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开系统浏览器")
    args = parser.parse_args(argv)

    if args.templates:
        candidate = Path(args.templates).expanduser()
        TEMPLATES_ROOT = candidate if candidate.is_absolute() else ROOT / candidate
    TEMPLATES_ROOT = TEMPLATES_ROOT.resolve()
    if not TEMPLATES_ROOT.is_dir():
        print(f"模板目录不存在：{TEMPLATES_ROOT}", file=sys.stderr)
        return 1

    if args.data_dir:
        candidate = Path(args.data_dir).expanduser()
        DATA_DIR = candidate if candidate.is_absolute() else ROOT / candidate
    BROWSER_PATH = args.browser or ""

    return serve(args.host, args.port, not args.no_open)


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模板编辑器 · OnebotMaaLogAnalyzer</title>
<style>
  * { box-sizing: border-box; }
  :root {
    color-scheme: dark;
    --bg: #14161a; --panel: #1b1e23; --panel-2: #21242a; --line: #2f333b;
    --text: #d8dce3; --dim: #8b929c; --faint: #6a707a;
    --accent: #4c8dff; --accent-dim: #2f6fde;
    --ok: #6cc48a; --warn: #e0b341; --bad: #e06c6c;
    --mono: "Cascadia Mono", "JetBrains Mono", Consolas, "SFMono-Regular", Menlo, monospace;
  }
  html, body { height: 100%; }
  body {
    margin: 0; display: flex; flex-direction: column; background: var(--bg); color: var(--text);
    font: 13px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei",
      "PingFang SC", "Noto Sans CJK SC", sans-serif; overflow: hidden;
  }
  button, input, select, textarea { font: inherit; color: inherit; }
  button {
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 5px;
    padding: 5px 10px; cursor: pointer; white-space: nowrap;
  }
  button:hover { background: #2b2f37; }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { background: var(--accent-dim); border-color: #3f7fd6; color: #fff; font-weight: 600; }
  button.primary:hover { background: #3a7ceb; }
  button.ghost { background: transparent; }
  button.ghost:hover { background: var(--panel-2); }
  button.tiny { padding: 2px 7px; font-size: 11px; border-radius: 4px; }
  button.danger:hover { background: #432620; border-color: #6b3733; color: #f0b4ae; }
  input[type=text], input[type=number], select, textarea {
    background: #16181d; border: 1px solid var(--line); border-radius: 5px;
    padding: 5px 7px; outline: none;
  }
  input[type=number] { width: 78px; }
  input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  kbd {
    background: #101216; border: 1px solid #3a3e46; border-radius: 3px;
    padding: 0 3px; font-size: 10px; font-family: var(--mono);
  }

  /* 顶栏 */
  header {
    display: flex; align-items: center; gap: 12px; padding: 0 12px; height: 46px;
    background: var(--panel); border-bottom: 1px solid var(--line); flex: none;
  }
  .brand { font-weight: 700; letter-spacing: .2px; white-space: nowrap; display: flex; align-items: center; gap: 8px; }
  .brand .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
  .brand small { font-weight: 400; color: var(--faint); font-size: 11px; }
  .spacer { flex: 1; }
  .toolbar { display: flex; align-items: center; gap: 8px; }
  .switch { display: flex; align-items: center; gap: 6px; color: var(--dim); font-size: 12px; cursor: pointer; user-select: none; }
  .badge {
    font-size: 11px; padding: 2px 7px; border-radius: 20px; border: 1px solid var(--line);
    color: var(--dim); background: #171a1f; white-space: nowrap;
  }
  .badge.dirty { color: var(--warn); border-color: #5a4a1e; background: #241f13; }
  .badge.ok { color: var(--ok); border-color: #2c4a37; background: #16211b; }

  /* 主区域 */
  main {
    flex: 1; display: grid; min-height: 0;
    grid-template-columns: 250px minmax(0, 1fr) minmax(0, 0.92fr);
  }
  .pane { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
  .pane + .pane { border-left: 1px solid var(--line); }
  .pane-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 6px 10px; border-bottom: 1px solid var(--line); font-size: 11.5px; color: var(--dim);
    flex: none; min-height: 34px; background: #191c21;
  }
  .pane-head .title { color: #c7ccd4; font-weight: 600; letter-spacing: .2px; }
  .pane-body { flex: 1; min-height: 0; overflow: auto; }

  /* 侧栏 */
  .rail-tabs { display: flex; gap: 2px; padding: 6px 8px 0; border-bottom: 1px solid var(--line); flex: none; }
  .rail-tab {
    padding: 5px 10px; border-radius: 6px 6px 0 0; font-size: 12px; color: var(--dim);
    cursor: pointer; border: 1px solid transparent; border-bottom: none; margin-bottom: -1px;
  }
  .rail-tab:hover { color: var(--text); }
  .rail-tab.active { color: var(--text); background: var(--panel); border-color: var(--line); }
  .rail-view { display: none; padding: 10px; }
  .rail-view.active { display: block; }
  .section { margin-bottom: 16px; }
  .section h3 {
    margin: 0 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
    color: var(--faint); font-weight: 600;
  }
  .tpl-item {
    display: flex; align-items: center; gap: 7px; padding: 6px 8px; border-radius: 6px;
    cursor: pointer; border: 1px solid transparent; color: #c2c8d1;
  }
  .tpl-item:hover { background: var(--panel-2); }
  .tpl-item.active { background: #1f2a3d; border-color: #2f4a72; color: #dbe6f7; }
  .tpl-item .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tpl-item .warn { color: var(--warn); font-size: 11px; }
  .file-item {
    display: flex; align-items: center; gap: 7px; padding: 5px 8px; border-radius: 5px;
    cursor: pointer; color: #bcc2cb; font-family: var(--mono); font-size: 12px;
  }
  .file-item:hover { background: var(--panel-2); }
  .file-item.active { background: #1f2a3d; color: #dbe6f7; }
  .file-item .mark { width: 6px; height: 6px; border-radius: 50%; background: transparent; flex: none; }
  .file-item .mark.dirty { background: var(--warn); }
  .file-item .mark.new { background: var(--ok); }
  .file-item .meta { margin-left: auto; font-size: 10.5px; color: var(--faint); }
  .ph-item {
    display: flex; align-items: baseline; gap: 7px; padding: 3px 6px; border-radius: 4px;
    cursor: pointer; font-size: 11.5px;
  }
  .ph-item:hover { background: var(--panel-2); }
  .ph-item code { font-family: var(--mono); color: #8fb6f5; font-size: 11px; }
  .ph-item span { color: var(--faint); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hint { color: var(--faint); font-size: 11px; line-height: 1.7; }

  /* 编辑区 */
  .tabs { display: flex; align-items: stretch; gap: 1px; padding: 0 6px; background: #191c21; border-bottom: 1px solid var(--line); flex: none; overflow-x: auto; }
  .tab {
    display: flex; align-items: center; gap: 6px; padding: 7px 11px; font-size: 12px;
    font-family: var(--mono); color: var(--dim); cursor: pointer; border-bottom: 2px solid transparent;
    white-space: nowrap; user-select: none;
  }
  .tab:hover { color: var(--text); background: var(--panel-2); }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); background: var(--panel); }
  .tab .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--warn); }
  .tab .dot.hidden { display: none; }
  .tab .new { color: var(--ok); font-size: 10px; }
  .editor-wrap { flex: 1; display: flex; min-height: 0; position: relative; }
  .gutter {
    flex: none; width: 52px; padding: 10px 8px 10px 0; text-align: right; overflow: hidden;
    font-family: var(--mono); font-size: 12.5px; line-height: 1.6; color: #454b55;
    background: #16181d; border-right: 1px solid var(--line); user-select: none; white-space: pre;
  }
  #code {
    flex: 1; border: none; border-radius: 0; background: var(--panel); resize: none;
    padding: 10px 14px; font-family: var(--mono); font-size: 12.5px; line-height: 1.6;
    tab-size: 2; white-space: pre; overflow: auto; outline: none;
  }
  .statusbar {
    flex: none; display: flex; align-items: center; gap: 14px; padding: 4px 12px;
    border-top: 1px solid var(--line); background: #191c21; font-size: 11px; color: var(--faint);
    font-family: var(--mono);
  }
  .statusbar .grow { flex: 1; }

  /* 预览区 */
  .preview-controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .preview-controls label { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--faint); }
  .preview-controls input[type=number] { width: 66px; padding: 3px 5px; font-size: 11.5px; }
  .preview-body { flex: 1; overflow: auto; padding: 14px; background: #101216; }
  .pages { display: flex; flex-direction: column; align-items: center; gap: 16px; }
  .pages.actual { align-items: flex-start; }
  .page-card {
    margin: 0; width: 100%; max-width: 720px; background: #fff; border-radius: 5px;
    overflow: hidden; box-shadow: 0 3px 16px rgba(0, 0, 0, .5);
  }
  .pages.actual .page-card { width: auto; max-width: none; }
  .page-card img { display: block; width: 100%; height: auto; cursor: zoom-in; }
  .pages.actual .page-card img { width: auto; max-width: none; cursor: zoom-out; }
  .page-card figcaption {
    padding: 4px 9px; font-size: 10.5px; color: #667085; background: #f3f4f6;
    border-top: 1px solid #e5e7eb; font-family: var(--mono);
  }
  .warnings {
    margin: 0 0 12px; padding: 8px 11px; border-radius: 5px; font-size: 11.5px; line-height: 1.7;
    background: #2a2416; border: 1px solid #55461c; color: #e6cd8a; white-space: pre-wrap;
    font-family: var(--mono);
  }
  .warnings.hidden { display: none; }
  .error {
    padding: 11px 13px; border-radius: 5px; background: #32201e; border: 1px solid #68352f;
    color: #f0b4ae; font-size: 12px; word-break: break-word;
  }
  .error .sub { color: #d0a29c; display: block; margin-top: 6px; font-size: 11px; }
  .error details { margin-top: 8px; }
  .error summary { cursor: pointer; font-size: 11px; color: #d0a29c; }
  .error pre { margin: 6px 0 0; font-size: 10.5px; white-space: pre-wrap; max-height: 240px; overflow: auto; font-family: var(--mono); }
  .empty { color: var(--faint); font-size: 12px; text-align: center; padding: 46px 0; }

  /* 数据视图 */
  .data-field { margin-bottom: 9px; }
  .data-field label { display: block; font-size: 11px; color: var(--faint); margin-bottom: 3px; }
  .data-field input { width: 100%; }
  #md {
    width: 100%; min-height: 230px; resize: vertical; font-family: var(--mono);
    font-size: 12px; line-height: 1.6;
  }
  .row { display: flex; gap: 6px; align-items: center; }
  .row > * { min-width: 0; }
  .row input { flex: 1; }

  /* 弹窗与提示 */
  .modal-back {
    position: fixed; inset: 0; background: rgba(0, 0, 0, .55); display: none;
    align-items: center; justify-content: center; z-index: 40;
  }
  .modal-back.show { display: flex; }
  .modal {
    width: 380px; background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
    padding: 16px 18px; box-shadow: 0 18px 50px rgba(0, 0, 0, .6);
  }
  .modal h2 { margin: 0 0 10px; font-size: 14px; }
  .modal p { margin: 0 0 12px; color: var(--dim); font-size: 12px; line-height: 1.7; }
  .modal .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  .toast {
    position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%) translateY(14px);
    background: #262b33; border: 1px solid #3d434d; color: var(--text); padding: 7px 14px;
    border-radius: 20px; font-size: 12px; opacity: 0; pointer-events: none;
    transition: opacity .16s ease, transform .16s ease; z-index: 60;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  .toast.bad { border-color: #6b3733; background: #35221f; color: #f0b4ae; }
  .toast.ok { border-color: #2c4a37; background: #17211b; color: #9fe0b6; }
  .conflict {
    display: none; align-items: center; gap: 10px; padding: 6px 12px; font-size: 11.5px;
    background: #2a2416; border-bottom: 1px solid #55461c; color: #e6cd8a; flex: none;
  }
  .conflict.show { display: flex; }

  @media (max-width: 1180px) {
    main { grid-template-columns: 210px minmax(0, 1fr) minmax(0, 0.85fr); }
  }
  @media (max-width: 960px) {
    main { grid-template-columns: 170px minmax(0, 1fr) minmax(0, 0.8fr); }
    .brand small { display: none; }
    .rail-view { padding: 8px 6px; }
  }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>模板编辑器<small id="rootPath"></small></div>
  <div class="toolbar">
    <button id="newTplBtn" class="ghost tiny">新建模板</button>
    <button id="dupTplBtn" class="ghost tiny" disabled>复制</button>
    <button id="delTplBtn" class="ghost tiny danger" disabled>删除</button>
  </div>
  <div class="spacer"></div>
  <div class="toolbar">
    <span id="dirtyBadge" class="badge">已保存</span>
    <label class="switch"><input id="autoSave" type="checkbox"> 自动保存</label>
    <label class="switch"><input id="autoRender" type="checkbox" checked> 自动渲染</label>
    <button id="saveBtn" class="primary" disabled>保存 <kbd>Ctrl</kbd><kbd>S</kbd></button>
  </div>
</header>

<div id="conflict" class="conflict">
  <span>磁盘上的模板文件已被外部修改，当前编辑器有未保存内容。</span>
  <span class="spacer"></span>
  <button id="conflictReload" class="tiny">放弃本地改动并重新载入</button>
  <button id="conflictDismiss" class="tiny ghost">知道了</button>
</div>

<main>
  <section class="pane rail">
    <div class="rail-tabs">
      <div class="rail-tab active" data-view="templates">模板</div>
      <div class="rail-tab" data-view="data">数据</div>
      <div class="rail-tab" data-view="placeholders">占位符</div>
    </div>
    <div class="pane-body">
      <div class="rail-view active" id="view-templates">
        <div class="section">
          <h3>模板</h3>
          <div id="tplList"></div>
        </div>
        <div class="section">
          <h3>文件</h3>
          <div id="fileList"></div>
        </div>
        <div class="section">
          <h3>操作</h3>
          <div class="hint">
            <kbd>Ctrl</kbd>+<kbd>S</kbd> 保存 · <kbd>Ctrl</kbd>+<kbd>Enter</kbd> 渲染<br>
            保存前后内容会备份到临时目录，可「撤销上次保存」。
          </div>
          <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
            <button id="restoreBtn" class="tiny">撤销上次保存</button>
            <button id="reloadBtn" class="tiny">重新载入</button>
          </div>
        </div>
      </div>

      <div class="rail-view" id="view-data">
        <div class="section">
          <h3>占位符取值</h3>
          <div id="metaGrid"></div>
        </div>
        <div class="section">
          <h3>附件目录（可选）</h3>
          <input id="attachDir" type="text" placeholder="留空 = 不内联附件" style="width:100%">
          <div class="hint" style="margin-top:4px">用于验证 <code>[附件@日志: ...]</code> 的图文混排</div>
        </div>
        <div class="section">
          <h3>Markdown 内容</h3>
          <textarea id="md" spellcheck="false"></textarea>
          <div class="row" style="margin-top:6px">
            <button id="insertSection" class="tiny">插入 section</button>
            <button id="insertBreak" class="tiny">插入分页</button>
            <button id="resetMd" class="tiny">重置示例</button>
          </div>
        </div>
      </div>

      <div class="rail-view" id="view-placeholders">
        <div class="section">
          <h3>可用的占位符</h3>
          <div class="hint" style="margin-bottom:6px">点击插入到光标处（代码或 Markdown）</div>
          <div id="phList"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="pane">
    <div class="tabs" id="tabs"></div>
    <div class="editor-wrap">
      <div class="gutter" id="gutter">1</div>
      <textarea id="code" spellcheck="false" wrap="off" placeholder="选择一个文件开始编辑"></textarea>
    </div>
    <div class="statusbar">
      <span id="cursorPos">行 1，列 1</span>
      <span id="fileInfo"></span>
      <span class="grow"></span>
      <span id="saveState">已保存</span>
    </div>
  </section>

  <section class="pane">
    <div class="pane-head">
      <span class="title">渲染预览</span>
      <span id="renderStatus" class="badge">尚未渲染</span>
    </div>
    <div class="pane-head preview-controls">
      <label>宽度 <input id="width" type="number" min="320" max="2160" step="10" value="1080"></label>
      <label>页高 <input id="maxHeight" type="number" min="320" max="8192" step="100" value="2000"></label>
      <label>页数 <input id="maxPages" type="number" min="1" max="40" step="1" value="8"></label>
      <label>缩放 <input id="scale" type="number" min="0.5" max="4" step="0.25" value="1"></label>
      <span class="spacer"></span>
      <button id="zoomBtn" class="tiny">1:1</button>
      <button id="renderBtn" class="tiny primary">渲染</button>
    </div>
    <div class="preview-body">
      <div id="warnings" class="warnings hidden"></div>
      <div id="pages" class="pages"></div>
    </div>
  </section>
</main>

<div class="modal-back" id="modalBack">
  <div class="modal">
    <h2 id="modalTitle">标题</h2>
    <p id="modalText"></p>
    <div id="modalField" class="data-field" style="display:none">
      <label id="modalLabel">名称</label>
      <input id="modalInput" type="text">
    </div>
    <div class="actions">
      <button id="modalCancel">取消</button>
      <button id="modalOk" class="primary">确定</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const META_LABELS = __META_LABELS__;
const PLACEHOLDERS = __PLACEHOLDERS__;
const KNOWN_FILES = __KNOWN_FILES__;

const $ = (id) => document.getElementById(id);

let STATE = null;
let TEMPLATE = '';
let OPEN = {};            // file -> {content, saved, exists, mtime}
let ACTIVE = '';
let REVISION = '';
let RENDERING = false;
let RENDER_QUEUED = false;
let SAVE_TIMER = null;
let RENDER_TIMER = null;
let LAST_FOCUS = 'code';  // code | md

// ── 基础请求 ──

async function api(path, payload) {
  const options = payload === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) };
  const response = await fetch(path, options);
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    throw new Error('响应不是 JSON：' + text.slice(0, 200));
  }
  if (!response.ok && !data.error) {
    throw new Error('HTTP ' + response.status);
  }
  return data;
}

let TOAST_TIMER = null;
function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => { node.className = 'toast'; }, 2200);
}

function formatBytes(value) {
  let number = Number(value) || 0;
  const units = ['B', 'KB', 'MB'];
  let index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
  return number.toFixed(index === 0 ? 0 : 1) + ' ' + units[index];
}

function currentTemplate() {
  return (STATE && STATE.templates || []).find((item) => item.name === TEMPLATE) || null;
}

function anyDirty() {
  return Object.values(OPEN).some((file) => computeDirty(file));
}

function computeDirty(state) {
  if (!state) { return false; }
  if (state.content !== state.saved) { return true; }
  return !state.exists && state.content.length > 0;
}

function updateListMarks() {
  document.querySelectorAll('#fileList .file-item').forEach((row) => {
    const state = OPEN[row.dataset.file];
    const mark = row.querySelector('.mark');
    if (!mark) { return; }
    mark.className = 'mark';
    if (computeDirty(state)) { mark.classList.add('dirty'); }
    else if (state && !state.exists) { mark.classList.add('new'); }
  });
}

function syncTemplateControls() {
  $('delTplBtn').disabled = (!TEMPLATE || TEMPLATE === 'default');
  $('dupTplBtn').disabled = !TEMPLATE;
  $('newTplBtn').disabled = !STATE;
}

// ── 模板 / 文件列表 ──

function renderTemplateList() {
  const host = $('tplList');
  host.innerHTML = '';
  if (!STATE) { return; }
  for (const item of STATE.templates) {
    const row = document.createElement('div');
    row.className = 'tpl-item' + (item.name === TEMPLATE ? ' active' : '');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.name;
    row.appendChild(name);
    if (!item.ok) {
      const warn = document.createElement('span');
      warn.className = 'warn';
      warn.textContent = '缺 page.html';
      row.appendChild(warn);
    }
    row.addEventListener('click', () => selectTemplate(item.name));
    host.appendChild(row);
  }
  if (!STATE.templates.length) {
    host.innerHTML = '<div class="hint">模板目录为空</div>';
  }
}

function renderFileList() {
  const host = $('fileList');
  host.innerHTML = '';
  const files = fileNames();
  if (!files.length) {
    host.innerHTML = '<div class="hint">没有可编辑的文件</div>';
    return;
  }
  for (const name of files) {
    const row = document.createElement('div');
    row.className = 'file-item' + (name === ACTIVE ? ' active' : '');
    row.dataset.file = name;
    const mark = document.createElement('span');
    mark.className = 'mark';
    const state = OPEN[name];
    if (computeDirty(state)) { mark.classList.add('dirty'); }
    else if (state && !state.exists) { mark.classList.add('new'); }
    row.appendChild(mark);
    const label = document.createElement('span');
    label.textContent = name;
    row.appendChild(label);
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = (state && state.exists) ? formatBytes((state.content || '').length) : '新建';
    row.appendChild(meta);
    row.addEventListener('click', () => setActiveFile(name));
    host.appendChild(row);
  }
}

function fileNames() {
  const names = [];
  const template = currentTemplate();
  const onDisk = (template && template.files || []).map((file) => file.name);
  for (const name of KNOWN_FILES) { names.push(name); }
  for (const name of onDisk) { if (!names.includes(name)) { names.push(name); } }
  return names;
}

function renderTabs() {
  const host = $('tabs');
  host.innerHTML = '';
  for (const name of fileNames()) {
    const state = OPEN[name];
    const tab = document.createElement('div');
    tab.className = 'tab' + (name === ACTIVE ? ' active' : '');
    tab.dataset.file = name;
    tab.title = name;
    const label = document.createElement('span');
    label.textContent = name;
    tab.appendChild(label);
    if (state && !state.exists) {
      const badge = document.createElement('span');
      badge.className = 'new';
      badge.textContent = '+';
      tab.appendChild(badge);
    }
    const dot = document.createElement('span');
    dot.className = 'dot' + (state && state.content !== state.saved ? '' : ' hidden');
    tab.appendChild(dot);
    tab.addEventListener('click', () => setActiveFile(name));
    host.appendChild(tab);
  }
}

// ── 编辑器 ──

function updateGutter() {
  const text = $('code').value;
  const lines = text.split('\n').length;
  const parts = [];
  for (let i = 1; i <= lines; i += 1) { parts.push(i); }
  $('gutter').textContent = parts.join('\n');
  $('gutter').scrollTop = $('code').scrollTop;
}

function currentFileState() {
  if (!ACTIVE) { return null; }
  if (!OPEN[ACTIVE]) {
    OPEN[ACTIVE] = { content: '', saved: '', exists: false, mtime: 0 };
  }
  return OPEN[ACTIVE];
}

function loadActiveIntoEditor() {
  const state = currentFileState();
  const code = $('code');
  if (!state) {
    code.value = '';
    code.disabled = true;
    updateGutter();
    $('fileInfo').textContent = '';
    return;
  }
  code.disabled = false;
  code.value = state.content;
  updateGutter();
  updateStatus();
  resetHistoryScroll();
}

let LAST_SCROLL = {};
function resetHistoryScroll() {
  $('code').scrollTop = LAST_SCROLL[ACTIVE] || 0;
}

function updateStatus() {
  const state = currentFileState();
  const dirty = computeDirty(state);
  $('fileInfo').textContent = state
    ? (ACTIVE + ' · ' + state.content.length + ' 字符' + (state.exists ? '' : ' · 保存后创建'))
    : '';
  $('saveState').textContent = dirty ? '未保存' : '已保存';
  $('saveState').style.color = dirty ? 'var(--warn)' : 'var(--faint)';
  const badge = $('dirtyBadge');
  if (dirty) {
    badge.className = 'badge dirty';
    badge.textContent = '未保存';
  } else {
    badge.className = 'badge ok';
    badge.textContent = '已保存';
  }
  $('saveBtn').disabled = !dirty;
  document.title = (dirty ? '● ' : '') + '模板编辑器 · OnebotMaaLogAnalyzer';
  updateCursor();
  updateTabDots();
  updateListMarks();
}

function updateTabDots() {
  document.querySelectorAll('#tabs .tab').forEach((tab) => {
    const state = OPEN[tab.dataset.file];
    const dot = tab.querySelector('.dot');
    if (!dot) { return; }
    dot.classList.toggle('hidden', !computeDirty(state));
  });
}

function updateCursor() {
  const code = $('code');
  const upto = code.value.slice(0, code.selectionStart);
  const line = upto.split('\n').length;
  const column = upto.length - upto.lastIndexOf('\n');
  $('cursorPos').textContent = '行 ' + line + '，列 ' + column;
}

function onCodeInput() {
  const state = currentFileState();
  if (!state) { return; }
  state.content = $('code').value;
  updateGutter();
  updateStatus();
  if ($('autoSave').checked) {
    clearTimeout(SAVE_TIMER);
    SAVE_TIMER = setTimeout(() => saveActive(true), 1100);
  }
}

function onCodeKeydown(event) {
  const code = $('code');

  if (event.key === 'Tab') {
    event.preventDefault();
    const value = code.value;
    const start = code.selectionStart;
    const end = code.selectionEnd;
    if (!event.shiftKey && start === end) {
      code.value = value.slice(0, start) + '  ' + value.slice(end);
      code.selectionStart = code.selectionEnd = start + 2;
    } else {
      // 处理所有被覆盖的行；Shift+Tab 时即使没有选区也作用于当前行
      const from = value.lastIndexOf('\n', start - 1) + 1;
      let to = value.indexOf('\n', Math.max(end, start));
      if (to === -1) { to = value.length; }
      const lines = value.slice(from, to).split('\n');
      let firstDelta = 0;
      const shifted = lines.map((line, index) => {
        const next = event.shiftKey ? line.replace(/^ {1,2}/, '') : '  ' + line;
        if (index === 0) { firstDelta = next.length - line.length; }
        return next;
      }).join('\n');
      code.value = value.slice(0, from) + shifted + value.slice(to);
      if (start === end) {
        const caret = Math.max(from, start + firstDelta);
        code.selectionStart = code.selectionEnd = caret;
      } else {
        code.selectionStart = from;
        code.selectionEnd = from + shifted.length;
      }
    }
    onCodeInput();
    return;
  }

  if (event.key === 'Enter' && !event.ctrlKey && !event.metaKey) {
    // 保持上一行缩进
    const start = code.selectionStart;
    const value = code.value;
    const lineStart = value.lastIndexOf('\n', start - 1) + 1;
    const current = value.slice(lineStart, start);
    const indent = (current.match(/^[ \t]*/) || [''])[0];
    if (indent) {
      event.preventDefault();
      const insert = '\n' + indent;
      code.value = value.slice(0, start) + insert + value.slice(code.selectionEnd);
      code.selectionStart = code.selectionEnd = start + insert.length;
      onCodeInput();
    }
  }
}

// ── 保存 / 载入 ──

async function saveActive(quiet) {
  const state = currentFileState();
  if (!state) { return false; }
  if (!computeDirty(state)) {
    if (!quiet) { toast('没有需要保存的改动'); }
    return true;
  }
  try {
    const result = await api('/api/file/write', {
      name: TEMPLATE, file: ACTIVE, content: state.content,
    });
    if (!result.ok) { throw new Error(result.error || '保存失败'); }
    state.saved = state.content;
    state.exists = true;
    state.mtime = result.mtime;
    REVISION = (await api('/api/revision')).revision || REVISION;
    await refreshState(true, true);
    updateStatus();
    if (!quiet) { toast('已保存 ' + ACTIVE, 'ok'); }
    if ($('autoRender').checked) { scheduleRender(120); }
    return true;
  } catch (err) {
    toast('保存失败：' + ((err && err.message) || err), 'bad');
    return false;
  }
}

async function loadFile(name) {
  const payload = await api('/api/file/read?name=' + encodeURIComponent(TEMPLATE)
    + '&file=' + encodeURIComponent(name));
  if (payload.ok === false) { throw new Error(payload.error || '读取失败'); }
  OPEN[name] = {
    content: payload.content || '',
    saved: payload.content || '',
    exists: !!payload.exists,
    mtime: payload.mtime || 0,
  };
}

async function loadAllFiles() {
  OPEN = {};
  const files = fileNames();
  await Promise.all(files.map((name) => loadFile(name).catch(() => {
    OPEN[name] = { content: '', saved: '', exists: false, mtime: 0 };
  })));
  if (!files.includes(ACTIVE)) { ACTIVE = files[0] || ''; }
  loadActiveIntoEditor();
  renderTabs();
  renderFileList();
}

async function setActiveFile(name) {
  if (ACTIVE === name) { return; }
  LAST_SCROLL[ACTIVE] = $('code').scrollTop;
  ACTIVE = name;
  if (!OPEN[name]) { await loadFile(name).catch(() => {}); }
  loadActiveIntoEditor();
  renderTabs();
  renderFileList();
  updateStatus();
}

async function reloadActive() {
  if (!ACTIVE) { return; }
  if (computeDirty(currentFileState())) {
    const yes = await confirmDialog('放弃改动', '当前文件有未保存的改动，确定从磁盘重新载入？', '重新载入');
    if (!yes) { return; }
  }
  await loadFile(ACTIVE).catch(() => {});
  loadActiveIntoEditor();
  updateStatus();
  renderFileList();
  toast('已重新载入 ' + ACTIVE);
}

async function restoreActive() {
  if (!ACTIVE) { return; }
  const yes = await confirmDialog(
    '撤销上次保存',
    '将 ' + ACTIVE + ' 恢复为最近一次保存前的内容（当前内容会被覆盖，不可再撤销）。',
    '撤销',
  );
  if (!yes) { return; }
  const result = await api('/api/file/restore', { name: TEMPLATE, file: ACTIVE });
  if (!result.ok) { toast(result.error || '撤销失败', 'bad'); return; }
  OPEN[ACTIVE] = {
    content: result.content, saved: result.content, exists: true, mtime: Date.now() / 1000,
  };
  loadActiveIntoEditor();
  updateStatus();
  await refreshState(true, true);
  if ($('autoRender').checked) { scheduleRender(120); }
  toast('已撤销上次保存', 'ok');
}

// ── 模板切换 / 新建 / 删除 ──

async function selectTemplate(name) {
  if (name === TEMPLATE) { return; }
  if (anyDirty()) {
    const yes = await confirmDialog('切换模板', '当前模板有未保存的改动，切换后将丢失。继续？', '继续切换');
    if (!yes) { return; }
  }
  TEMPLATE = name;
  ACTIVE = 'page.html';
  try {
    await loadAllFiles();
  } catch (err) {
    toast('载入模板失败：' + ((err && err.message) || err), 'bad');
  }
  renderTemplateList();
  updateStatus();
  syncTemplateControls();
  scheduleRender(60);
}

async function refreshState(keepEditor, silent) {
  const previous = TEMPLATE;
  const data = await api('/api/state');
  STATE = data;
  REVISION = data.revision || REVISION;
  $('rootPath').textContent = '· ' + data.root;
  const exists = STATE.templates.some((item) => item.name === TEMPLATE);
  if (!exists) {
    TEMPLATE = STATE.defaultTemplate || (STATE.templates[0] || {}).name || '';
  }
  if (!TEMPLATE) {
    $('pages').innerHTML = '<div class="empty">templates/ 下没有任何模板</div>';
  }
  renderTemplateList();
  syncTemplateControls();
  if (!keepEditor || previous !== TEMPLATE) {
    await loadAllFiles();
  } else {
    renderFileList();
    renderTabs();
  }
  if (!data.browser && !silent) {
    $('renderStatus').className = 'badge';
    $('renderStatus').textContent = '未找到浏览器';
    toast('未找到 Chromium 系浏览器，无法渲染预览', 'bad');
  }
}

async function createTemplate() {
  const name = await promptDialog('新建模板', '输入新模板目录名（字母、数字、下划线、连字符）。', '名称', 'my-theme');
  if (!name) { return; }
  const base = TEMPLATE || 'default';
  const result = await api('/api/template/create', { name: name.trim(), base: base });
  if (!result.ok) { toast(result.error || '创建失败', 'bad'); return; }
  await refreshState(true);
  await selectTemplate(result.name);
  toast('已创建模板 ' + result.name, 'ok');
}

async function duplicateTemplate() {
  const suggestion = (TEMPLATE || 'default') + '-copy';
  const name = await promptDialog('复制模板', '以 ' + TEMPLATE + ' 为蓝本复制一份。', '名称', suggestion);
  if (!name) { return; }
  const result = await api('/api/template/create', { name: name.trim(), base: TEMPLATE });
  if (!result.ok) { toast(result.error || '复制失败', 'bad'); return; }
  await refreshState(true);
  await selectTemplate(result.name);
  toast('已复制为 ' + result.name, 'ok');
}

async function deleteTemplate() {
  if (!TEMPLATE || TEMPLATE === 'default') { return; }
  const yes = await confirmDialog(
    '删除模板', '将删除整个目录 templates/' + TEMPLATE + '，不可恢复。确定删除？', '删除',
  );
  if (!yes) { return; }
  const result = await api('/api/template/delete', { name: TEMPLATE });
  if (!result.ok) { toast(result.error || '删除失败', 'bad'); return; }
  TEMPLATE = '';
  await refreshState(false);
  scheduleRender(60);
  toast('已删除模板', 'ok');
}

// ── 弹窗 ──

let MODAL_RESOLVE = null;
function openModal(options) {
  $('modalTitle').textContent = options.title || '';
  $('modalText').textContent = options.text || '';
  const field = $('modalField');
  if (options.field) {
    field.style.display = '';
    $('modalLabel').textContent = options.field;
    $('modalInput').value = options.value || '';
  } else {
    field.style.display = 'none';
  }
  $('modalOk').textContent = options.ok || '确定';
  $('modalBack').classList.add('show');
  if (options.field) {
    setTimeout(() => { $('modalInput').focus(); $('modalInput').select(); }, 30);
  }
  return new Promise((resolve) => { MODAL_RESOLVE = resolve; });
}

function closeModal(value) {
  $('modalBack').classList.remove('show');
  const resolver = MODAL_RESOLVE;
  MODAL_RESOLVE = null;
  if (resolver) { resolver(value); }
}

function confirmDialog(title, text, ok) {
  return openModal({ title: title, text: text, ok: ok || '确定' });
}

function promptDialog(title, text, field, value) {
  return openModal({ title: title, text: text, field: field, value: value, ok: '创建' })
    .then((result) => (result ? result.value : ''));
}

$('modalCancel').addEventListener('click', () => closeModal(null));
$('modalOk').addEventListener('click', () => {
  const field = $('modalField');
  if (field.style.display === 'none') {
    closeModal(true);
  } else {
    const value = $('modalInput').value.trim();
    if (!value) { toast('名称不能为空', 'bad'); return; }
    closeModal({ value: value });
  }
});
$('modalInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); $('modalOk').click(); }
  if (event.key === 'Escape') { closeModal(null); }
});
$('modalBack').addEventListener('mousedown', (event) => {
  if (event.target === $('modalBack')) { closeModal(null); }
});

// ── 渲染 ──

function metaValues() {
  const meta = {};
  document.querySelectorAll('#metaGrid input').forEach((input) => {
    meta[input.dataset.key] = input.value;
  });
  return meta;
}

function buildMetaForm() {
  const grid = $('metaGrid');
  grid.innerHTML = '';
  for (const field of META_LABELS) {
    const wrap = document.createElement('div');
    wrap.className = 'data-field';
    const label = document.createElement('label');
    label.textContent = field.label;
    const input = document.createElement('input');
    input.type = 'text';
    input.dataset.key = field.key;
    input.value = STATE.meta[field.key] === undefined ? '' : STATE.meta[field.key];
    input.addEventListener('input', () => {
      if ($('autoRender').checked) { scheduleRender(600); }
    });
    wrap.appendChild(label);
    wrap.appendChild(input);
    grid.appendChild(wrap);
  }
  $('attachDir').addEventListener('change', () => {
    if ($('autoRender').checked) { scheduleRender(100); }
  });
}

function buildPlaceholderList() {
  const host = $('phList');
  host.innerHTML = '';
  for (const item of PLACEHOLDERS) {
    const row = document.createElement('div');
    row.className = 'ph-item';
    const code = document.createElement('code');
    code.textContent = '{{' + item.key + '}}';
    const label = document.createElement('span');
    label.textContent = item.label;
    row.appendChild(code);
    row.appendChild(label);
    row.addEventListener('click', () => insertAtCursor('{{' + item.key + '}}'));
    host.appendChild(row);
  }
}

function insertAtCursor(text) {
  if (LAST_FOCUS === 'md') {
    const area = $('md');
    const start = area.selectionStart;
    area.value = area.value.slice(0, start) + text + area.value.slice(area.selectionEnd);
    area.selectionStart = area.selectionEnd = start + text.length;
    area.focus();
    if ($('autoRender').checked) { scheduleRender(600); }
    return;
  }
  const code = $('code');
  if (code.disabled) { return; }
  const start = code.selectionStart;
  code.value = code.value.slice(0, start) + text + code.value.slice(code.selectionEnd);
  code.selectionStart = code.selectionEnd = start + text.length;
  code.focus();
  onCodeInput();
}

function scheduleRender(delay) {
  clearTimeout(RENDER_TIMER);
  RENDER_TIMER = setTimeout(doRender, delay === undefined ? 200 : delay);
}

function showWarnings(list) {
  const box = $('warnings');
  const items = (list || []).filter(Boolean);
  box.innerHTML = '';
  if (!items.length) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  for (const text of items) {
    const line = document.createElement('div');
    line.textContent = text;
    box.appendChild(line);
  }
}

function showFailure(data) {
  const host = $('pages');
  host.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'error';
  box.textContent = data.error || '渲染失败';
  if (data.hint) {
    const hint = document.createElement('span');
    hint.className = 'sub';
    hint.textContent = data.hint;
    box.appendChild(hint);
  }
  if (data.trace) {
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = '异常堆栈';
    const pre = document.createElement('pre');
    pre.textContent = data.trace;
    details.appendChild(summary);
    details.appendChild(pre);
    box.appendChild(details);
  }
  host.appendChild(box);
}

function showImages(data) {
  const host = $('pages');
  host.innerHTML = '';
  data.images.forEach((src, index) => {
    const card = document.createElement('figure');
    card.className = 'page-card';
    const image = document.createElement('img');
    image.src = src;
    image.alt = '第 ' + (index + 1) + ' 页';
    image.addEventListener('click', toggleZoom);
    const caption = document.createElement('figcaption');
    caption.textContent = '第 ' + (index + 1) + '/' + data.pages + ' 页';
    image.addEventListener('load', () => {
      caption.textContent = '第 ' + (index + 1) + '/' + data.pages + ' 页 · '
        + image.naturalWidth + '×' + image.naturalHeight;
    });
    card.appendChild(image);
    card.appendChild(caption);
    host.appendChild(card);
  });
}

function toggleZoom() {
  const pages = $('pages');
  pages.classList.toggle('actual');
  $('zoomBtn').textContent = pages.classList.contains('actual') ? '适应宽度' : '1:1';
}

async function doRender() {
  if (!TEMPLATE) { return; }
  if (RENDERING) { RENDER_QUEUED = true; return; }
  RENDERING = true;
  RENDER_QUEUED = false;
  $('renderStatus').className = 'badge';
  $('renderStatus').textContent = '渲染中…';
  $('renderBtn').disabled = true;
  try {
    const data = await api('/api/render', {
      template: TEMPLATE,
      markdown: $('md').value,
      meta: metaValues(),
      width: Number($('width').value) || 1080,
      maxHeight: Number($('maxHeight').value) || 2000,
      maxPages: Number($('maxPages').value) || 8,
      scale: Number($('scale').value) || 1,
      attachDir: $('attachDir').value,
    });
    showWarnings(data.warnings);
    if (data.ok) {
      showImages(data);
      $('renderStatus').className = 'badge ok';
      $('renderStatus').textContent = data.pages + ' 页 · ' + formatBytes(data.bytes)
        + ' · ' + data.ms + ' ms';
    } else {
      showFailure(data);
      $('renderStatus').className = 'badge';
      $('renderStatus').textContent = '渲染失败';
    }
  } catch (err) {
    showFailure({ error: (err && err.message) ? err.message : String(err) });
    showWarnings([]);
    $('renderStatus').className = 'badge';
    $('renderStatus').textContent = '渲染失败';
  } finally {
    RENDERING = false;
    $('renderBtn').disabled = false;
    if (RENDER_QUEUED) { RENDER_QUEUED = false; doRender(); }
  }
}

// ── 外部改动探测 ──

async function watch() {
  try {
    const data = await api('/api/revision');
    if (!data.revision || data.revision === REVISION) { return; }
    const dirty = anyDirty();
    REVISION = data.revision;
    await refreshState(true, true);
    if (dirty) {
      $('conflict').classList.add('show');
      return;
    }
    // 未保存改动时才自动重新载入磁盘内容
    for (const name of Object.keys(OPEN)) {
      if (!fileNames().includes(name)) { delete OPEN[name]; }
    }
    for (const name of fileNames()) {
      await loadFile(name).catch(() => {});
    }
    const before = $('code').value;
    loadActiveIntoEditor();
    if (before !== $('code').value && ACTIVE) { toast('已重新载入磁盘上的 ' + ACTIVE); }
    renderFileList();
    updateStatus();
    if ($('autoRender').checked) { scheduleRender(150); }
  } catch (err) {
    /* 服务重启期间静默 */
  }
}

// ── 事件绑定 ──

$('saveBtn').addEventListener('click', () => saveActive(false));
$('renderBtn').addEventListener('click', () => doRender());
$('zoomBtn').addEventListener('click', toggleZoom);
$('reloadBtn').addEventListener('click', reloadActive);
$('restoreBtn').addEventListener('click', restoreActive);
$('newTplBtn').addEventListener('click', createTemplate);
$('dupTplBtn').addEventListener('click', duplicateTemplate);
$('delTplBtn').addEventListener('click', deleteTemplate);
$('conflictReload').addEventListener('click', async () => {
  for (const name of fileNames()) { await loadFile(name).catch(() => {}); }
  loadActiveIntoEditor();
  renderFileList();
  updateStatus();
  $('conflict').classList.remove('show');
  if ($('autoRender').checked) { scheduleRender(100); }
});
$('conflictDismiss').addEventListener('click', () => $('conflict').classList.remove('show'));

$('resetMd').addEventListener('click', () => {
  $('md').value = STATE.sample || '';
  scheduleRender(60);
});
$('insertSection').addEventListener('click', () => {
  const md = $('md');
  const start = md.selectionStart;
  const insertion = '\n<!-- section: 新小节 -->\n';
  md.value = md.value.slice(0, start) + insertion + md.value.slice(md.selectionEnd);
  md.selectionStart = md.selectionEnd = start + insertion.length;
  md.focus();
  scheduleRender(60);
});
$('insertBreak').addEventListener('click', () => {
  const md = $('md');
  const start = md.selectionStart;
  md.value = md.value.slice(0, start) + '\n<!-- page-break -->\n' + md.value.slice(md.selectionEnd);
  md.focus();
  scheduleRender(60);
});

$('code').addEventListener('input', onCodeInput);
$('code').addEventListener('keydown', onCodeKeydown);
$('code').addEventListener('scroll', () => { $('gutter').scrollTop = $('code').scrollTop; });
['click', 'keyup', 'select', 'input'].forEach((type) => {
  $('code').addEventListener(type, () => { LAST_FOCUS = 'code'; updateCursor(); });
});
['click', 'keyup', 'select', 'input'].forEach((type) => {
  $('md').addEventListener(type, () => { LAST_FOCUS = 'md'; });
});
$('md').addEventListener('input', () => {
  if ($('autoRender').checked) { scheduleRender(600); }
});
['width', 'maxHeight', 'maxPages', 'scale'].forEach((id) => {
  $(id).addEventListener('change', () => { scheduleRender(60); });
});
$('autoSave').addEventListener('change', () => {
  if ($('autoSave').checked && anyDirty()) { saveActive(true); }
});

document.querySelectorAll('.rail-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.rail-tab').forEach((item) => item.classList.remove('active'));
    document.querySelectorAll('.rail-view').forEach((view) => view.classList.remove('active'));
    tab.classList.add('active');
    $('view-' + tab.dataset.view).classList.add('active');
  });
});

document.addEventListener('keydown', (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveActive(false);
    return;
  }
  if (mod && event.key === 'Enter') {
    event.preventDefault();
    doRender();
    return;
  }
  if (event.key === 'Escape') {
    closeModal(null);
  }
});

window.addEventListener('beforeunload', (event) => {
  if (anyDirty()) {
    event.preventDefault();
    event.returnValue = '';
  }
});

// ── 启动 ──

async function boot() {
  await refreshState(false);
  buildMetaForm();
  buildPlaceholderList();
  $('md').value = STATE.sample || '';
  $('width').value = STATE.defaults.width;
  $('maxHeight').value = STATE.defaults.maxHeight;
  $('maxPages').value = STATE.defaults.maxPages;
  $('scale').value = STATE.defaults.scale;
  renderTemplateList();
  renderFileList();
  updateStatus();
  if (!STATE.templates.length) {
    $('pages').innerHTML = '<div class="empty">templates/ 下没有任何模板，先新建一个</div>';
    return;
  }
  if (!TEMPLATE) { TEMPLATE = STATE.defaultTemplate; await loadAllFiles(); }
  await doRender();
  setInterval(watch, 1500);
}

boot().catch((err) => {
  showFailure({ error: '初始化失败：' + ((err && err.message) || err) });
  $('renderStatus').textContent = '初始化失败';
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
