#!/usr/bin/env python3
"""
config.json 可视化编辑器 — 单文件独立工具：`python tools/config/editor.py`

做什么
  - 浏览器里用表单生成 config.json：OneBot 连接、Bot 配置（群组 / 项目 / 仓库 /
    候选模型 / 视觉 / 报告 / 分析设置 / 指令）、LLM 端点与模型、调试、日志
  - 端点与候选模型用下拉联动，避免手写 provider/model 名字出错
  - 实时 JSON 预览、实时校验（复用项目的 `validate_llm_config` 与 pydantic 模型）
  - 支持导入（粘贴 JSON 或选择文件）与导出（下载 config.json / 复制到剪贴板）
  - 只负责生成，不读也不写仓库里的 config.json

用法
  python tools/config/editor.py
  python tools/config/editor.py --port 8899 --no-open
  python tools/config/editor.py --templates D:/我的模板目录

依赖
  pydantic（项目已装）。校验会顺带导入 core.llm_pool，需要 aiohttp。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from core.models import (
        AnalysisSettings,
        CircuitBreakerConfig,
        CommandConfig,
        ConfigLLM,
        LLMDefaults,
        ProjectConfig,
        ReportConfig,
        VisionConfig,
    )
except ImportError as exc:  # pragma: no cover - 启动期环境问题
    venv_python = (ROOT / ".venv" / "Scripts" / "python.exe"
                   if sys.platform.startswith("win")
                   else ROOT / ".venv" / "bin" / "python")
    print(f"导入核心模型失败：{exc}", file=sys.stderr)
    print(f"请在项目虚拟环境中运行，例如：\n  {venv_python} tools/config/editor.py",
          file=sys.stderr)
    raise SystemExit(1)

try:
    from core.llm_pool import validate_llm_config
except Exception:  # pragma: no cover - 缺少 aiohttp 等运行依赖时降级
    validate_llm_config = None  # type: ignore[assignment]

try:
    from features.maa.render.browser import find_browser
except Exception:  # pragma: no cover
    find_browser = None  # type: ignore[assignment]

TEMPLATES_ROOT = ROOT / "templates"
MAX_BODY_BYTES = 4 * 1024 * 1024

ONEBOT_MODES = (
    ("http_ws", "http_ws — HTTP 调用 + 正向 WS 事件"),
    ("ws", "ws — 仅正向 WS"),
    ("ws_reverse", "ws_reverse — 反向 WS"),
    ("http_ws_reverse", "http_ws_reverse — HTTP 调用 + 反向 WS"),
)
ARCHIVE_MODES = (
    ("auto", "auto — 文件名初筛 + 内容识别（推荐）"),
    ("known", "known — 仅已知 UI 文件名 + 内容识别"),
    ("prefix", "prefix — 仅前缀识别"),
    ("any", "any — 任意 zip 都下载检查"),
)
REPO_MODES = (
    ("inject", "inject — 预检索后注入提示词（省 token）"),
    ("agent", "agent — 交给 AI 自主多轮检索（深、费 token）"),
)
RENDER_MODES = (
    ("auto", "auto — 能用浏览器就出图"),
    ("image", "image — 强制出图"),
    ("text", "text — 纯文本"),
)
VISION_DETAILS = (("high", "high"), ("low", "low"), ("auto", "auto"))
PROVIDER_KINDS = (("openai", "openai — OpenAI 兼容 /chat/completions"),)
LOG_LEVELS = (("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR"))
UI_KEYS = ("mxu", "mfaa", "maafwapp", "auto")


# ── 默认文档（来自 pydantic 模型，保证与代码同步） ──

def _dump(model: Any) -> dict:
    return json.loads(model.model_dump_json(exclude_none=False))


def _config_defaults() -> dict:
    project = _dump(ProjectConfig())
    project["name"] = "MaaXXX"
    return {
        "listen_groups": ["123456789"],
        "notify_group": "123456789",
        "project": project,
        "llm": {"models": [{"name": "default", "model": "gpt-4o-mini"}]},
        "vision": _dump(VisionConfig()),
        "report": _dump(ReportConfig()),
        "settings": _dump(AnalysisSettings()),
        "commands": _dump(CommandConfig()),
    }


def default_document() -> dict:
    return {
        "onebot": {
            "mode": "http_ws",
            "http_url": "http://127.0.0.1:3000",
            "ws_url": "ws://127.0.0.1:3001",
            "ws_reverse_port": 8080,
            "access_token": "",
            "api_timeout_seconds": 30,
        },
        "bot": {
            "data_dir": "data",
            "configs": {"默认": _config_defaults()},
        },
        "llm": {
            "providers": [{
                "name": "default",
                "kind": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "max_concurrency": 2,
                "timeout_seconds": 180,
                "connect_timeout_seconds": 10,
                "max_retries": 2,
                "models": [{
                    "model": "gpt-4o-mini",
                    "supports": {"tools": True, "vision": True, "json_mode": True},
                    "max_tokens": 8192,
                    "max_context_tokens": 48000,
                }],
            }],
            "defaults": _dump(LLMDefaults()),
            "circuit_breaker": _dump(CircuitBreakerConfig()),
        },
        "debug": {"enabled": False, "http_port": 8765, "data_dir": "data/test"},
        "log": {"log_to_file": True, "log_level": "INFO", "log_dir": "logs"},
    }


# ── 校验 ──

def _errors_text(exc: Any, limit: int = 4) -> str:
    parts: list[str] = []
    try:
        for item in exc.errors()[:limit]:
            loc = ".".join(str(piece) for piece in item.get("loc", ()))
            parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
    except Exception:
        return str(exc)
    if not parts:
        return str(exc)
    if len(exc.errors()) > limit:
        parts.append("…")
    return "；".join(parts)


def _check_number(value: Any, label: str, low: float, high: float,
                  errors: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} 必须是数字")
        return
    if value < low or value > high:
        errors.append(f"{label} 应在 {low}~{high} 之间（当前 {value}）")


def validate_document(cfg: Any) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(cfg, dict):
        return {"errors": ["配置必须是 JSON 对象"], "warnings": []}

    # ── onebot ──
    onebot = cfg.get("onebot")
    if not isinstance(onebot, dict):
        errors.append("缺少 onebot 段")
        onebot = {}
    mode = str(onebot.get("mode") or "").strip()
    if mode not in {key for key, _ in ONEBOT_MODES}:
        errors.append(f"onebot.mode 取值无效：{mode or '（空）'}")
    if mode in ("http_ws", "ws_reverse", "http_ws_reverse"):
        if not str(onebot.get("http_url") or "").strip() and mode != "ws_reverse":
            warnings.append("onebot.http_url 为空：需要调用 OneBot HTTP API 才能回消息")
    if not str(onebot.get("http_url") or "").strip() and mode in ("http_ws", "http_ws_reverse"):
        errors.append("onebot.http_url 不能为空")
    if not str(onebot.get("ws_url") or "").strip() and mode in ("ws", "http_ws"):
        errors.append("onebot.ws_url 不能为空")
    for key, low, high in (("ws_reverse_port", 1, 65535), ("api_timeout_seconds", 1, 3600)):
        if key in onebot:
            _check_number(onebot.get(key), f"onebot.{key}", low, high, errors)

    # ── bot ──
    bot = cfg.get("bot")
    if not isinstance(bot, dict):
        errors.append("缺少 bot 段")
        bot = {}
    if not str(bot.get("data_dir") or "").strip():
        errors.append("bot.data_dir 不能为空")
    configs = bot.get("configs")
    if not isinstance(configs, dict) or not configs:
        errors.append("bot.configs 至少需要一个配置")
        configs = {}

    for name, spec in configs.items():
        display = str(name or "").strip() or "（空名称）"
        if not str(name or "").strip():
            errors.append("存在空名称的配置")
        if str(name or "").startswith("/"):
            errors.append(f"配置名称不能以斜杠开头：{display}")
        if not isinstance(spec, dict):
            errors.append(f"配置「{display}」必须是对象")
            continue

        groups = spec.get("listen_groups") or []
        if not isinstance(groups, list):
            errors.append(f"配置「{display}」的 listen_groups 必须是数组")
            groups = []
        clean_groups = [str(item).strip() for item in groups if str(item).strip()]
        if not clean_groups:
            warnings.append(f"配置「{display}」没有监听群，不会响应任何消息")
        for item in clean_groups:
            if not item.isdigit():
                errors.append(f"配置「{display}」的监听群必须是数字：{item}")

        notify = str(spec.get("notify_group") or "").strip()
        if notify and not notify.isdigit():
            errors.append(f"配置「{display}」的通知群必须是数字：{notify}")
        if notify and clean_groups and notify not in clean_groups:
            warnings.append(f"配置「{display}」的通知群不在监听群列表中（仍可发送）")

        for label, model in (
            ("project", ProjectConfig),
            ("settings", AnalysisSettings),
            ("vision", VisionConfig),
            ("report", ReportConfig),
            ("commands", CommandConfig),
            ("llm", ConfigLLM),
        ):
            raw = spec.get(label)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                errors.append(f"配置「{display}」的 {label} 段必须是对象")
                continue
            try:
                model.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
                errors.append(f"配置「{display}」的 {label} 段无效：{_errors_text(exc)}")

        project = spec.get("project") if isinstance(spec.get("project"), dict) else {}
        ui = project.get("ui")
        if isinstance(ui, dict):
            for key, value in ui.items():
                if not isinstance(value, dict):
                    errors.append(f"配置「{display}」的 project.ui.{key} 必须是对象")

        repo = project.get("repo") if isinstance(project.get("repo"), dict) else {}
        if repo.get("enabled"):
            if not str(repo.get("path") or "").strip() and not str(repo.get("url") or "").strip():
                errors.append(f"配置「{display}」启用了仓库但没有填 path 或 url")
            if str(repo.get("mode") or "inject") not in {"inject", "agent"}:
                errors.append(f"配置「{display}」的 repo.mode 只能是 inject 或 agent")

        report = spec.get("report") if isinstance(spec.get("report"), dict) else {}
        template_name = str(report.get("template") or "").strip()
        if template_name and template_name not in list_templates():
            warnings.append(
                f"配置「{display}」使用的模板 {template_name} 不在 templates/ 下，运行时会回退 default"
            )
        browser_path = str(report.get("browser_path") or "").strip()
        if browser_path and not Path(browser_path).expanduser().is_file():
            warnings.append(f"配置「{display}」的 report.browser_path 不是有效文件：{browser_path}")
        if str(report.get("render") or "auto") not in {"auto", "image", "text"}:
            errors.append(f"配置「{display}」的 report.render 只能是 auto / image / text")

        commands = spec.get("commands") if isinstance(spec.get("commands"), dict) else {}
        for key in ("read_level", "write_level", "followup_level"):
            if key in commands:
                _check_number(commands.get(key), f"配置「{display}」的 commands.{key}", 0, 2, errors)

    # ── llm ──
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        errors.append("缺少 llm 段")
        llm = {}
    providers = llm.get("providers") or []
    if not isinstance(providers, list):
        errors.append("llm.providers 必须是数组")
        providers = []

    if validate_llm_config is not None:
        for message in validate_llm_config(llm, configs):
            errors.append(message)
    else:
        warnings.append("未加载 llm_pool.validate_llm_config，端点校验被跳过")

    seen_names: dict[str, int] = {}
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            errors.append(f"llm.providers[{index}] 必须是对象")
            continue
        name = str(provider.get("name") or "").strip()
        if name:
            seen_names[name] = seen_names.get(name, 0) + 1
        api_key = str(provider.get("api_key") or "").strip()
        if not api_key:
            warnings.append(f"端点 {name or index} 的 api_key 为空，真实调用会失败")
        base_url = str(provider.get("base_url") or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append(f"端点 {name or index} 的 base_url 必须以 http:// 或 https:// 开头")
        kind = str(provider.get("kind") or "openai")
        if kind != "openai":
            warnings.append(f"端点 {name or index} 的 kind={kind}，当前只实现了 openai 兼容协议")
        models = provider.get("models") or []
        if not isinstance(models, list):
            errors.append(f"端点 {name or index} 的 models 必须是数组")
            continue
        for m_index, spec in enumerate(models):
            if not isinstance(spec, dict):
                errors.append(f"端点 {name or index} 的 models[{m_index}] 必须是对象")
                continue
            if not str(spec.get("model") or "").strip():
                errors.append(f"端点 {name or index} 的 models[{m_index}] 缺少 model 名称")
    for name, count in seen_names.items():
        if count > 1:
            errors.append(f"llm.providers 中端点名重复：{name}")

    defaults = llm.get("defaults") if isinstance(llm.get("defaults"), dict) else {}
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1)):
        if key in defaults:
            _check_number(defaults.get(key), f"llm.defaults.{key}", low, high, errors)

    # 候选模型与能力
    capability: dict[tuple[str, str], dict] = {}
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        pname = str(provider.get("name") or "")
        for spec in provider.get("models") or []:
            if isinstance(spec, dict):
                capability[(pname, str(spec.get("model") or ""))] = spec.get("supports") or {}

    for name, spec in configs.items():
        if not isinstance(spec, dict):
            continue
        llm_spec = spec.get("llm") if isinstance(spec.get("llm"), dict) else {}
        refs = [ref for ref in (llm_spec.get("models") or []) if isinstance(ref, dict)]
        if not refs:
            continue
        seen: set[tuple[str, str]] = set()
        any_tools = False
        any_vision = False
        for ref in refs:
            key = (str(ref.get("name") or ""), str(ref.get("model") or ""))
            if key in seen:
                warnings.append(f"配置「{name}」的候选列表存在重复：{key[0]}/{key[1]}")
            seen.add(key)
            supports = capability.get(key) or {}
            if supports.get("tools"):
                any_tools = True
            if supports.get("vision"):
                any_vision = True
        if refs and not any_tools:
            warnings.append(
                f"配置「{name}」的候选模型都没有声明 supports.tools，工具模式无法使用"
            )
        vision_cfg = spec.get("vision") if isinstance(spec.get("vision"), dict) else {}
        if vision_cfg.get("enabled", True) and refs and not any_vision:
            warnings.append(
                f"配置「{name}」开启了看图，但候选模型都没有声明 supports.vision"
            )

    # ── debug / log ──
    debug = cfg.get("debug") if isinstance(cfg.get("debug"), dict) else {}
    if debug.get("enabled"):
        warnings.append("debug.enabled=true：调试面板会暴露本地 HTTP 端口，正式部署请关闭")
    if "http_port" in debug:
        _check_number(debug.get("http_port"), "debug.http_port", 1, 65535, errors)
    log = cfg.get("log") if isinstance(cfg.get("log"), dict) else {}
    level = str(log.get("log_level") or "INFO")
    if level not in {key for key, _ in LOG_LEVELS}:
        errors.append(f"log.log_level 取值无效：{level}")

    if any(str((p or {}).get("api_key") or "").strip()
           for p in providers if isinstance(p, dict)):
        warnings.append("导出文件包含 api_key，请勿提交到公开仓库")

    return {"errors": _dedupe(errors), "warnings": _dedupe(warnings)}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def list_templates() -> list[str]:
    if not TEMPLATES_ROOT.is_dir():
        return []
    return sorted(
        entry.name for entry in TEMPLATES_ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    )


def collect_meta() -> dict:
    browser = ""
    if find_browser is not None:
        try:
            browser = find_browser("") or ""
        except Exception:
            browser = ""
    return {
        "defaults": default_document(),
        "templates": list_templates(),
        "browser": browser,
        "enums": {
            "onebot.mode": [{"value": key, "label": label} for key, label in ONEBOT_MODES],
            "project.archive_match_mode": [{"value": key, "label": label}
                                           for key, label in ARCHIVE_MODES],
            "repo.mode": [{"value": key, "label": label} for key, label in REPO_MODES],
            "report.render": [{"value": key, "label": label} for key, label in RENDER_MODES],
            "vision.detail": [{"value": key, "label": label} for key, label in VISION_DETAILS],
            "provider.kind": [{"value": key, "label": label} for key, label in PROVIDER_KINDS],
            "log.log_level": [{"value": key, "label": label} for key, label in LOG_LEVELS],
        },
        "uiKeys": list(UI_KEYS),
        "hasValidator": validate_llm_config is not None,
    }


# ── HTTP ──

class _Handler(BaseHTTPRequestHandler):
    server_version = "MaaConfigEditor/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 静默 access log
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(index_html().encode("utf-8"), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/meta":
            return self._send_json(collect_meta())
        if path == "/api/templates":
            return self._send_json({"ok": True, "templates": list_templates()})
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/validate":
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return self._send_json({"errors": ["请求体过大"], "warnings": []}, 413)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8-sig") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._send_json({"errors": [f"请求体不是合法 JSON：{exc}"], "warnings": []}, 400)
        cfg = payload.get("config") if isinstance(payload, dict) else None
        try:
            result = validate_document(cfg)
        except Exception as exc:  # noqa: BLE001 - 校验自身异常也要回显
            traceback.print_exc()
            result = {"errors": [f"校验异常：{type(exc).__name__}: {exc}"], "warnings": []}
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
        .replace("__UI_KEYS__", json.dumps(list(UI_KEYS)))
        .replace("__DEFAULT_TEMPLATE__", json.dumps("默认"))
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
    print(f"编辑器地址：{url}  （Ctrl+C 退出）", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    global TEMPLATES_ROOT

    parser = argparse.ArgumentParser(description="config.json 可视化编辑器（只生成，不读写配置）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8757, help="监听端口（默认 8757，被占用则自动顺延）")
    parser.add_argument("--templates", default="", help="模板根目录（用于报告模板下拉，默认 templates/）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开系统浏览器")
    args = parser.parse_args(argv)

    if args.templates:
        candidate = Path(args.templates).expanduser()
        TEMPLATES_ROOT = candidate if candidate.is_absolute() else ROOT / candidate
    TEMPLATES_ROOT = TEMPLATES_ROOT.resolve()

    return serve(args.host, args.port, not args.no_open)


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>config.json 编辑器 · OnebotMaaLogAnalyzer</title>
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
  input[type=text], input[type=number], input[type=password], select, textarea {
    background: #16181d; border: 1px solid var(--line); border-radius: 5px;
    padding: 5px 7px; outline: none; width: 100%;
  }
  input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  input[type=number] { max-width: 140px; }
  input.mono { font-family: var(--mono); font-size: 12px; }
  kbd { background: #101216; border: 1px solid #3a3e46; border-radius: 3px; padding: 0 3px; font-size: 10px; }

  header {
    display: flex; align-items: center; gap: 12px; padding: 0 12px; height: 46px;
    background: var(--panel); border-bottom: 1px solid var(--line); flex: none;
  }
  .brand { font-weight: 700; display: flex; align-items: center; gap: 8px; white-space: nowrap; }
  .brand .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
  .brand small { font-weight: 400; color: var(--faint); font-size: 11px; }
  .spacer { flex: 1; }
  .toolbar { display: flex; align-items: center; gap: 8px; }
  .badge {
    font-size: 11px; padding: 2px 7px; border-radius: 20px; border: 1px solid var(--line);
    color: var(--dim); background: #171a1f; white-space: nowrap;
  }
  .badge.ok { color: var(--ok); border-color: #2c4a37; background: #16211b; }
  .badge.warn { color: var(--warn); border-color: #5a4a1e; background: #241f13; }
  .badge.bad { color: var(--bad); border-color: #6b3733; background: #241614; }

  main { flex: 1; display: grid; grid-template-columns: 232px minmax(0, 1fr) minmax(0, 0.78fr); min-height: 0; }
  .pane { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
  .pane + .pane { border-left: 1px solid var(--line); }
  .pane-head {
    display: flex; align-items: center; gap: 8px; padding: 6px 12px; min-height: 34px;
    border-bottom: 1px solid var(--line); background: #191c21; font-size: 11.5px; color: var(--dim);
    flex: none;
  }
  .pane-head .title { color: #c7ccd4; font-weight: 600; }
  .pane-body { flex: 1; min-height: 0; overflow: auto; }

  /* 左侧导航 */
  .nav { padding: 8px; }
  .nav-group { margin-bottom: 4px; }
  .nav-item {
    display: flex; align-items: center; gap: 8px; padding: 6px 9px; border-radius: 6px;
    cursor: pointer; color: #c2c8d1; font-size: 12.5px;
  }
  .nav-item:hover { background: var(--panel-2); }
  .nav-item.active { background: #1f2a3d; color: #dbe6f7; }
  .nav-item .count {
    margin-left: auto; font-size: 10.5px; color: var(--faint); background: #22262d;
    border-radius: 10px; padding: 0 6px;
  }
  .nav-sub { margin: 2px 0 6px 18px; display: flex; flex-direction: column; gap: 2px; }
  .nav-sub .nav-item { padding: 4px 8px; font-size: 12px; }
  .nav-actions { padding: 8px 10px 4px; display: flex; gap: 6px; flex-wrap: wrap; }
  .nav-foot { padding: 10px 12px; color: var(--faint); font-size: 11px; line-height: 1.7; }

  /* 表单 */
  .form { padding: 14px 18px 40px; max-width: 820px; }
  .form-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .form-head h2 { margin: 0; font-size: 15px; }
  .form-head .sub { color: var(--faint); font-size: 11.5px; }
  .group {
    background: #191c21; border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 14px 14px; margin-bottom: 14px;
  }
  .group > h3 {
    margin: 0 0 10px; font-size: 11.5px; color: #a9b1bc; font-weight: 600;
    letter-spacing: .4px; text-transform: uppercase;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(268px, 1fr)); gap: 10px 16px; }
  .field { min-width: 0; }
  .field.wide { grid-column: 1 / -1; }
  .field > label {
    display: flex; align-items: baseline; gap: 6px; font-size: 11.5px; color: #98a0ab;
    margin-bottom: 3px;
  }
  .field > label .key { font-family: var(--mono); font-size: 10px; color: #4f5661; }
  .field .help { font-size: 10.5px; color: var(--faint); margin-top: 3px; line-height: 1.5; word-break: break-word; }
  .field.inline { display: flex; align-items: center; gap: 8px; }
  .field.inline > label { margin: 0; }
  .switch { display: flex; align-items: center; gap: 7px; padding: 4px 0; cursor: pointer; }
  .switch input { width: auto; }
  .switch .state { font-size: 11px; color: var(--faint); }

  .chips { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
  .chip {
    display: inline-flex; align-items: center; gap: 5px; background: #22262d;
    border: 1px solid #333841; border-radius: 12px; padding: 1px 4px 1px 9px;
    font-family: var(--mono); font-size: 11.5px;
  }
  .chip button {
    background: transparent; border: none; color: var(--faint); padding: 0 3px;
    font-size: 12px; line-height: 1; cursor: pointer;
  }
  .chip button:hover { color: var(--bad); }
  .chip-add { display: flex; gap: 5px; width: 100%; margin-top: 5px; }
  .chip-add input { max-width: 200px; }

  .card {
    border: 1px solid var(--line); border-radius: 8px; background: #191c21;
    padding: 10px 14px 14px; margin-bottom: 12px;
  }
  .card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .card-head .idx {
    font-family: var(--mono); font-size: 11px; color: var(--faint); background: #22262d;
    border-radius: 4px; padding: 1px 6px;
  }
  .card-head input { max-width: 240px; }
  .card-head .spacer { flex: 1; }
  .sub-title {
    font-size: 11px; color: #a9b1bc; font-weight: 600; margin: 12px 0 8px;
    letter-spacing: .3px;
  }
  .empty-note {
    border: 1px dashed #383d46; border-radius: 8px; padding: 14px; text-align: center;
    color: var(--faint); font-size: 12px; margin-bottom: 12px;
  }
  .tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin-bottom: 14px; flex-wrap: wrap; }
  .tab {
    padding: 6px 11px; font-size: 12px; color: var(--dim); cursor: pointer;
    border-bottom: 2px solid transparent;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); }

  /* 右侧 */
  .right-tabs { display: flex; gap: 2px; }
  .right-tab {
    padding: 4px 10px; font-size: 11.5px; border-radius: 5px; cursor: pointer; color: var(--dim);
  }
  .right-tab.active { background: var(--panel-2); color: var(--text); }
  .json { margin: 0; padding: 12px 14px; font-family: var(--mono); font-size: 11.5px;
          line-height: 1.6; white-space: pre; color: #b9c4d1; }
  .json .k { color: #8fb6f5; }
  .json .s { color: #a5d6a7; }
  .json .n { color: #e0b341; }
  .json .b { color: #d98fd8; }
  .json .p { color: #5c6470; }
  .report { padding: 12px 14px; }
  .issue { display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid #23262c; font-size: 12px; line-height: 1.6; }
  .issue:last-child { border-bottom: none; }
  .issue .mark { flex: none; font-size: 11px; }
  .issue.error .mark { color: var(--bad); }
  .issue.warn .mark { color: var(--warn); }
  .issue.error { color: #f0b4ae; }
  .issue.warn { color: #e6cd8a; }
  .ok-note { color: var(--ok); font-size: 12px; padding: 10px 0; }
  .hint-list { margin: 0; padding: 0 0 0 18px; color: var(--faint); font-size: 11.5px; line-height: 1.9; }
  .hint-list code { font-family: var(--mono); color: #8fb6f5; }

  /* 弹窗与提示 */
  .modal-back {
    position: fixed; inset: 0; background: rgba(0, 0, 0, .6); display: none;
    align-items: center; justify-content: center; z-index: 40;
  }
  .modal-back.show { display: flex; }
  .modal {
    width: min(680px, 92vw); background: var(--panel); border: 1px solid var(--line);
    border-radius: 9px; padding: 16px 18px; box-shadow: 0 18px 50px rgba(0, 0, 0, .6);
  }
  .modal h2 { margin: 0 0 8px; font-size: 14px; }
  .modal p { margin: 0 0 10px; color: var(--dim); font-size: 12px; line-height: 1.7; }
  .modal textarea {
    width: 100%; height: 300px; resize: vertical; font-family: var(--mono); font-size: 12px;
    line-height: 1.55;
  }
  .modal .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
  .modal .actions .left { margin-right: auto; }
  .modal.narrow { width: min(430px, 92vw); }
  .modal input[type=text] { width: 100%; }
  button.danger { border-color: #6b3733; color: #f0b4ae; }
  .toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%) translateY(12px);
    background: #262b33; border: 1px solid #3d434d; padding: 7px 14px; border-radius: 20px;
    font-size: 12px; opacity: 0; pointer-events: none; transition: opacity .16s, transform .16s;
    z-index: 60;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  .toast.ok { border-color: #2c4a37; background: #17211b; color: #9fe0b6; }
  .toast.bad { border-color: #6b3733; background: #35221f; color: #f0b4ae; }

  @media (max-width: 1200px) {
    main { grid-template-columns: 200px minmax(0, 1fr) minmax(0, 0.7fr); }
    .grid { grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
  }
  @media (max-width: 980px) {
    main { grid-template-columns: 168px minmax(0, 1fr) minmax(0, 0.6fr); }
    .form { padding: 12px 12px 32px; }
  }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>config.json 编辑器<small id="rootPath"></small></div>
  <div class="spacer"></div>
  <div class="toolbar">
    <span id="validBadge" class="badge">未校验</span>
    <button id="importBtn" class="ghost">导入</button>
    <button id="copyBtn" class="ghost">复制 JSON</button>
    <button id="exportBtn" class="primary">下载 config.json</button>
  </div>
</header>

<main>
  <section class="pane">
    <div class="pane-head"><span class="title">配置分区</span></div>
    <div class="pane-body">
      <div class="nav" id="nav"></div>
      <div class="nav-actions">
        <button id="resetBtn" class="tiny">恢复默认</button>
        <button id="clearBtn" class="tiny danger">清空</button>
      </div>
      <div class="nav-foot">
        <kbd>Ctrl</kbd>+<kbd>S</kbd> 下载 config.json<br>
        工具只生成文件，不会读写仓库里的 config.json。
      </div>
    </div>
  </section>

  <section class="pane">
    <div class="pane-head">
      <span class="title" id="formTitle">表单</span>
      <span class="spacer"></span>
      <label class="switch" title="把草稿（含密钥）保存在本浏览器，刷新不丢">
        <input id="draftToggle" type="checkbox"> <span class="state">记住草稿</span>
      </label>
    </div>
    <div class="pane-body"><div class="form" id="form"></div></div>
  </section>

  <section class="pane">
    <div class="pane-head">
      <span class="title">JSON 预览</span>
      <span class="spacer"></span>
      <div class="right-tabs">
        <span class="right-tab active" data-view="json">JSON</span>
        <span class="right-tab" data-view="report">校验 <span id="issueCount"></span></span>
      </div>
    </div>
    <div class="pane-body" id="jsonPane"><pre class="json" id="json"></pre></div>
    <div class="pane-body" id="reportPane" style="display:none"><div class="report" id="report"></div></div>
  </section>
</main>

<div class="modal-back" id="modalBack">
  <div class="modal">
    <h2 id="modalTitle">导入配置</h2>
    <p id="modalHint">粘贴 config.json 内容，或选择一个文件。导入会替换当前编辑内容，缺失字段用默认值补全。</p>
    <textarea id="modalText" spellcheck="false" placeholder="{ ... }"></textarea>
    <div class="actions">
      <button id="modalFile" class="left">选择文件…</button>
      <button id="modalCancel">取消</button>
      <button id="modalOk" class="primary">导入</button>
    </div>
  </div>
</div>
<div class="modal-back" id="askBack">
  <div class="modal narrow">
    <h2 id="askTitle"></h2>
    <p id="askHint"></p>
    <input id="askInput" type="text" spellcheck="false">
    <div class="actions">
      <button id="askCancel" class="left">取消</button>
      <button id="askOk" class="primary">确定</button>
    </div>
  </div>
</div>
<input type="file" id="filePicker" accept="application/json,.json" style="display:none">

<div class="toast" id="toast"></div>

<script>
const UI_KEYS = __UI_KEYS__;
const DEFAULT_TEMPLATE_NAME = __DEFAULT_TEMPLATE__;

const $ = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));

let META = null;
let CFG = null;
let SECTION = 'onebot';
let CONFIG_TAB = 'groups';
let ACTIVE_CONFIG = '';
let VALIDATION = { errors: [], warnings: [] };
let VALIDATING = false;
let VALIDATE_TIMER = null;
let JSON_TIMER = null;
const DRAFT_KEY = 'maa-config-editor-draft-v1';

// ── 标签与说明 ──

const LABELS = {
  'onebot.mode': '连接模式',
  'onebot.http_url': 'HTTP 地址',
  'onebot.ws_url': 'WebSocket 地址',
  'onebot.ws_reverse_port': '反向 WS 端口',
  'onebot.access_token': 'Access Token',
  'onebot.api_timeout_seconds': 'API 超时（秒）',
  'bot.data_dir': '数据目录',
  'config.listen_groups': '监听群号',
  'config.notify_group': '通知群号',
  'project.name': '项目名',
  'project.archive_match_mode': '日志包识别方式',
  'project.file_prefix': '文件名前缀',
  'project.require_log_layout': '要求日志布局特征',
  'repo.enabled': '启用仓库对照',
  'repo.mode': '检索模式',
  'repo.path': '本地仓库路径',
  'repo.url': '仓库地址',
  'repo.mirrors': '镜像地址',
  'repo.branch': '默认分支',
  'repo.default_branch': '分析后切回的分支',
  'repo.preload_enabled': '启动时预下载',
  'repo.preload_retries': '预下载重试次数',
  'repo.update_on_analyze': '每份日志前更新',
  'repo.allow_ai_update': '允许 AI 请求更新',
  'repo.auto_checkout_version': '按日志版本切 tag',
  'repo.allow_ai_switch_tag': '允许 AI 切换 tag',
  'repo.max_tag_switch_rounds': 'AI 切 tag 轮次上限',
  'repo.restore_latest_after_analyze': '分析后还原位置',
  'repo.refuse_dirty_worktree': '工作区脏时拒绝切换',
  'repo.max_tool_rounds': '工具调用轮次上限',
  'repo.max_tool_calls_per_round': '单轮工具数上限',
  'repo.max_tool_result_chars': '单次结果字符上限',
  'repo.max_tool_result_tokens': '单次结果 token 上限',
  'repo.max_tool_context_tokens': '工具上下文 token 上限',
  'repo.agent_deadline_seconds': 'agent 总时长上限（秒）',
  'repo.extensions': '扫描扩展名',
  'repo.max_file_kb': '单文件大小上限（KB）',
  'repo.max_scan_files': '扫描文件数上限',
  'repo.max_files': '注入文件数上限',
  'repo.max_identifiers': '日志标识符上限',
  'repo.context_lines': '命中上下文行数',
  'repo.max_block_lines': '展开块最大行数',
  'repo.max_chars': '代码片段字符上限',
  'vision.enabled': '启用看图',
  'vision.max_images': '每张图数量上限',
  'vision.max_image_kb': '单图大小上限（KB）',
  'vision.detail': '视觉细节',
  'vision.attach_error_screenshots': '附带错误截图',
  'vision.attach_repo_templates': '附带仓库模板图',
  'vision.allow_model_request': '允许 AI 主动索图',
  'report.render': '渲染方式',
  'report.template': '报告模板',
  'report.max_width_px': '画布宽度（px）',
  'report.max_height_px': '单页高度（px）',
  'report.max_pages': '页数上限',
  'report.device_scale_factor': '缩放倍率',
  'report.font_family': '字体',
  'report.browser_path': '浏览器路径',
  'report.reuse_browser': '复用浏览器进程',
  'report.render_timeout_ms': '渲染超时（ms）',
  'report.embed_attachments': '内联图片附件',
  'report.attach_markdown': '附带 .md 原稿',
  'report.fallback_to_text': '失败回退文本',
  'report.render_followup': '追问也出图',
  'settings.enabled': '启用分析',
  'settings.file_list_delay_seconds': '文件列表延迟（秒）',
  'settings.duplicate_window_seconds': '同文件去重窗口（秒）',
  'settings.send_progress_message': '发送进度消息',
  'settings.send_summary': '发送摘要',
  'settings.reply_chunk_chars': '单条消息字符上限',
  'settings.send_images': '发送图片',
  'settings.max_report_images': '图片数量上限',
  'settings.send_repo_images': '发送仓库模板图',
  'settings.send_files': '发送文件附件',
  'settings.max_report_files': '文件数量上限',
  'settings.max_file_attachment_mb': '单文件大小上限（MB）',
  'settings.inline_attachments': '附件内联在正文中',
  'settings.followup_enabled': '启用追问',
  'settings.followup_window_minutes': '追问窗口（分钟）',
  'settings.followup_max_turns': '单会话轮数上限',
  'settings.followup_max_sessions': '会话数上限',
  'settings.history_enabled': '保留分析历史',
  'settings.history_period_hours': '保留周期（小时）',
  'settings.history_keep_periods': '保留周期数',
  'settings.history_max_records': '记录数上限',
  'settings.history_max_total_mb': '历史体积上限（MB）',
  'settings.analysis_concurrency': '分析并发',
  'settings.download_concurrency': '下载并发',
  'settings.download_timeout_seconds': '下载超时（秒）',
  'settings.digest_timeout_seconds': '摘要超时（秒）',
  'settings.ai_stage_timeout_seconds': 'AI 阶段总时长（秒）',
  'settings.max_context_tokens': '上下文 token 上限',
  'settings.analysis_cache_hours': '结果缓存（小时）',
  'settings.circuit_breaker_enabled': '启用熔断',
  'settings.system_prompt': '追加系统提示词',
  'settings.log_tools_enabled': '启用日志包工具',
  'settings.cleanup_after_done': '完成后清理临时文件',
  'settings.save_digest_debug': '保存摘要调试文件',
  'settings.max_zip_size_mb': '压缩包上限（MB）',
  'settings.max_total_uncompressed_mb': '解压总量上限（MB）',
  'settings.max_prompt_chars': '提示词字符上限',
  'settings.max_zip_members': '成员数上限',
  'settings.max_log_files': '日志文件数上限',
  'settings.max_maafw_bak_files': 'maafw.bak 数量上限',
  'settings.max_maafw_bak_mb': 'maafw.bak 大小上限（MB）',
  'settings.merge_archive_parts': '合并分卷日志包',
  'settings.archive_part_settle_seconds': '分卷静默期（秒）',
  'settings.max_log_file_read_kb': '单次读取上限（KB）',
  'settings.small_log_full_read_mb': '小日志全读阈值（MB）',
  'settings.stream_medium_logs': '流式读取中等日志',
  'settings.large_log_threshold_mb': '大日志阈值（MB）',
  'settings.max_large_log_read_kb': '大日志单次读取（KB）',
  'settings.stream_large_logs': '流式读取大日志',
  'settings.max_stream_log_mb': '流式日志上限（MB）',
  'settings.max_sections_per_log': '每日志分段上限',
  'settings.max_log_chars': '日志字符上限',
  'settings.compress_context_noise': '压缩上下文噪声',
  'settings.max_recognition_evidence_lines': '识别证据行数上限',
  'settings.max_recognition_evidence_chars': '识别证据字符上限',
  'settings.log_head_lines': '日志头部行数',
  'settings.context_before_lines': '命中前文行数',
  'settings.context_after_lines': '命中后文行数',
  'commands.enabled': '启用指令',
  'commands.prefix': '指令前缀',
  'commands.owners': '超级管理员 QQ',
  'commands.admins': '管理员 QQ',
  'commands.read_level': '读指令所需等级',
  'commands.write_level': '写指令所需等级',
  'commands.followup_level': '追问所需等级',
  'commands.reply_chunk_chars': '回复分段字符数',
  'commands.max_list_items': '列表项上限',
  'llm.models': '候选模型（按优先级）',
  'llm.name': '端点',
  'llm.model': '模型',
  'llm.temperature': '温度覆盖',
  'llm.max_tokens': '输出 token 覆盖',
  'llm.top_p': 'top_p 覆盖',
  'defaults.temperature': '默认温度',
  'defaults.top_p': '默认 top_p',
  'defaults.max_tokens': '默认输出 token',
  'defaults.max_context_tokens': '默认上下文 token',
  'circuit_breaker.failure_threshold': '失败阈值',
  'circuit_breaker.cooldown_seconds': '冷却时间（秒）',
  'circuit_breaker.rate_cooldown_seconds': '限流/配额冷却（秒）',
  'circuit_breaker.auth_cooldown_seconds': '鉴权失败冷却（秒）',
  'provider.name': '端点名称',
  'provider.kind': '协议类型',
  'provider.base_url': 'Base URL',
  'provider.api_key': 'API Key',
  'provider.max_concurrency': '并发上限',
  'provider.timeout_seconds': '请求超时（秒）',
  'provider.connect_timeout_seconds': '连接超时（秒）',
  'provider.max_retries': '重试次数',
  'model.model': '模型名',
  'model.supports.tools': '支持工具调用',
  'model.supports.vision': '支持看图',
  'model.supports.json_mode': '支持 JSON 模式',
  'model.max_tokens': '输出 token 上限',
  'model.max_context_tokens': '上下文 token 上限',
  'model.temperature': '温度',
  'model.top_p': 'top_p',
  'debug.enabled': '启用调试面板',
  'debug.http_port': '调试端口',
  'debug.data_dir': '调试数据目录',
  'log.log_to_file': '写入日志文件',
  'log.log_level': '日志级别',
  'log.log_dir': '日志目录',
};

const HELP = {
  'onebot.mode': 'http_ws = HTTP 调用 + 正向 WS 事件（最常见）；ws_reverse 由协议端主动连入。',
  'onebot.access_token': '与 OneBot 实现里配置的一致，留空表示不校验。',
  'config.listen_groups': '这些群里收到日志包才会分析；一个配置可以监听多个群。',
  'config.notify_group': '分析结果发送到哪个群，通常填主群。',
  'project.archive_match_mode': 'auto 适合大多数情况：先按文件名初筛，再按内容确认。',
  'project.require_log_layout': '开启后会检查压缩包里是否有 MaaFramework 的日志目录结构。',
  'repo.path': '指向本地仓库目录时无需网络与 git，优先级高于 url。',
  'repo.mode': 'inject 快且省 token；agent 让 AI 自己多轮检索，理解更深但更贵。',
  'repo.refuse_dirty_worktree': '本地仓库有未提交改动时拒绝切换 tag，避免打断开发。',
  'repo.extensions': '注入提示词时允许读取的文件类型。',
  'vision.max_images': '一次请求最多附带的图片数量，注意模型上下文与费用。',
  'report.render': 'auto：浏览器可用就出图；image：强制出图（失败按 fallback_to_text 处理）。',
  'report.template': 'templates/ 下的目录名，可用 tools/templates/editor.py 调试模板。',
  'report.browser_path': '留空则自动探测 Edge / Chrome / Chromium。',
  'report.max_height_px': '单页最大高度，超出的内容会分页。',
  'settings.max_context_tokens': '0 = 使用 llm.defaults.max_context_tokens。',
  'settings.followup_window_minutes': '0 = 跟随历史保留期。',
  'settings.max_log_chars': '进入提示词的日志字符上限，越大越贵。',
  'commands.read_level': '0 任何人 / 1 管理员 / 2 超管。',
  'llm.models': '按顺序尝试：前面的失败（401 除外）会顺延到下一个候选。',
  'provider.base_url': 'OpenAI 兼容端点，例如 https://api.deepseek.com/v1。',
  'provider.api_key': '明文保存在 config.json 中，注意不要提交到公开仓库。',
  'model.supports.tools': '分析默认使用工具循环，关闭后该模型无法承担带工具的调用。',
  'model.max_tokens': '0 = 使用 llm.defaults.max_tokens。',
  'debug.enabled': '开启后可在浏览器访问调试面板，正式部署请关闭。',
};

const SETTINGS_GROUPS = [
  ['触发', ['enabled', 'file_list_delay_seconds', 'duplicate_window_seconds']],
  ['消息反馈', ['send_progress_message', 'send_summary', 'reply_chunk_chars']],
  ['结果图片与附件', ['send_images', 'max_report_images', 'send_repo_images', 'send_files',
    'max_report_files', 'max_file_attachment_mb', 'inline_attachments']],
  ['追问答疑', ['followup_enabled', 'followup_window_minutes', 'followup_max_turns',
    'followup_max_sessions']],
  ['分析历史', ['history_enabled', 'history_period_hours', 'history_keep_periods',
    'history_max_records', 'history_max_total_mb']],
  ['并发与超时', ['analysis_concurrency', 'download_concurrency', 'download_timeout_seconds',
    'digest_timeout_seconds', 'ai_stage_timeout_seconds', 'max_context_tokens',
    'analysis_cache_hours', 'circuit_breaker_enabled']],
  ['模型', ['system_prompt', 'log_tools_enabled']],
  ['临时文件', ['cleanup_after_done', 'save_digest_debug']],
  ['压缩包与摘要', ['max_zip_size_mb', 'max_total_uncompressed_mb', 'max_prompt_chars',
    'max_zip_members', 'max_log_files', 'max_maafw_bak_files', 'max_maafw_bak_mb',
    'merge_archive_parts', 'archive_part_settle_seconds', 'max_log_file_read_kb',
    'small_log_full_read_mb', 'stream_medium_logs', 'large_log_threshold_mb',
    'max_large_log_read_kb', 'stream_large_logs', 'max_stream_log_mb',
    'max_sections_per_log', 'max_log_chars', 'compress_context_noise',
    'max_recognition_evidence_lines', 'max_recognition_evidence_chars', 'log_head_lines',
    'context_before_lines', 'context_after_lines']],
];

const REPO_GROUPS = [
  ['基本', ['enabled', 'mode']],
  ['来源', ['path', 'url', 'mirrors', 'branch', 'default_branch']],
  ['预下载与更新', ['preload_enabled', 'preload_retries', 'update_on_analyze', 'allow_ai_update']],
  ['版本切换', ['auto_checkout_version', 'allow_ai_switch_tag', 'max_tag_switch_rounds',
    'restore_latest_after_analyze', 'refuse_dirty_worktree']],
  ['agent 模式', ['max_tool_rounds', 'max_tool_calls_per_round', 'max_tool_result_chars',
    'max_tool_result_tokens', 'max_tool_context_tokens', 'agent_deadline_seconds']],
  ['扫描范围', ['extensions', 'max_file_kb', 'max_scan_files', 'max_files']],
  ['注入提示词', ['max_identifiers', 'context_lines', 'max_block_lines', 'max_chars']],
];

const REPORT_GROUPS = [
  ['基本', ['render', 'template', 'reuse_browser']],
  ['尺寸与分页', ['max_width_px', 'max_height_px', 'max_pages', 'device_scale_factor', 'font_family']],
  ['浏览器', ['browser_path', 'render_timeout_ms']],
  ['附件与回退', ['embed_attachments', 'attach_markdown', 'fallback_to_text', 'render_followup']],
];

const VISION_GROUPS = [
  ['基本', ['enabled', 'detail']],
  ['限制', ['max_images', 'max_image_kb']],
  ['图片来源', ['attach_error_screenshots', 'attach_repo_templates', 'allow_model_request']],
];

const COMMAND_GROUPS = [
  ['基本', ['enabled', 'prefix', 'reply_chunk_chars', 'max_list_items']],
  ['权限', ['owners', 'admins', 'read_level', 'write_level', 'followup_level']],
];

const PROJECT_GROUPS = [
  ['基本', ['name', 'archive_match_mode', 'file_prefix', 'require_log_layout']],
];

// ── 工具函数 ──

let TOAST_TIMER = null;
function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => { node.className = 'toast'; }, 2200);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

function humanize(key) {
  return String(key).replace(/[_-]/g, ' ');
}

function deepMerge(base, patch) {
  if (Array.isArray(patch)) { return clone(patch); }
  if (patch === null || typeof patch !== 'object') {
    return patch === undefined ? base : patch;
  }
  const target = (base && typeof base === 'object' && !Array.isArray(base)) ? base : {};
  for (const [key, value] of Object.entries(patch)) {
    target[key] = deepMerge(target[key], value);
  }
  return target;
}

function number(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// ── 字段渲染 ──

function fieldShell(key, path, wide) {
  const box = el('div', 'field' + (wide ? ' wide' : ''));
  const label = el('label');
  label.appendChild(el('span', '', LABELS[path] || humanize(key)));
  label.appendChild(el('span', 'key', key));
  box.appendChild(label);
  return { box, label };
}

function addHelp(box, path) {
  if (HELP[path]) { box.appendChild(el('div', 'help', HELP[path])); }
}

function onBump() {
  scheduleJson();
  scheduleValidate();
  saveDraft();
}

function renderTextField(box, obj, key, path, opts) {
  const input = el('input');
  input.type = opts && opts.secret ? 'password' : 'text';
  input.className = opts && opts.mono ? 'mono' : '';
  input.value = obj[key] === undefined || obj[key] === null ? '' : String(obj[key]);
  if (opts && opts.placeholder) { input.placeholder = opts.placeholder; }
  input.addEventListener('input', () => {
    const value = input.value;
    if (opts && opts.nullable && value.trim() === '') {
      delete obj[key];
    } else {
      obj[key] = value;
    }
    onBump();
  });
  if (opts && opts.secret) {
    const row = el('div', 'row');
    row.style.display = 'flex';
    row.style.gap = '6px';
    row.appendChild(input);
    const toggle = el('button', 'tiny ghost', '显示');
    toggle.addEventListener('click', () => {
      const hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      toggle.textContent = hidden ? '隐藏' : '显示';
    });
    row.appendChild(toggle);
    box.appendChild(row);
    return;
  }
  box.appendChild(input);
}

function renderNumberField(box, obj, key, path, opts) {
  const isFloat = opts && opts.float;
  const input = el('input');
  input.type = 'number';
  if (isFloat) { input.step = '0.05'; }
  if (opts && opts.min !== undefined) { input.min = String(opts.min); }
  if (opts && opts.max !== undefined) { input.max = String(opts.max); }
  const raw = obj[key];
  input.value = raw === undefined || raw === null ? '' : String(raw);
  if (opts && opts.nullable) {
    input.placeholder = '留空 = 跟随全局';
  }
  input.addEventListener('input', () => {
    const text = input.value.trim();
    if (text === '') {
      if (opts && opts.nullable) { delete obj[key]; } else { obj[key] = 0; }
      onBump();
      return;
    }
    const value = Number(text);
    if (!Number.isFinite(value)) { return; }
    obj[key] = isFloat ? value : Math.round(value);
    onBump();
  });
  box.appendChild(input);
}

function renderBoolField(box, obj, key) {
  const wrapper = el('label', 'switch');
  const input = el('input');
  input.type = 'checkbox';
  input.checked = !!obj[key];
  const state = el('span', 'state', obj[key] ? '开启' : '关闭');
  input.addEventListener('change', () => {
    obj[key] = input.checked;
    state.textContent = input.checked ? '开启' : '关闭';
    onBump();
  });
  wrapper.appendChild(input);
  wrapper.appendChild(state);
  box.appendChild(wrapper);
}

function renderSelectField(box, obj, key, options, opts) {
  const select = el('select');
  const current = obj[key] === undefined || obj[key] === null ? '' : String(obj[key]);
  let matched = false;
  for (const option of options) {
    const item = el('option', '', option.label || option.value);
    item.value = option.value;
    if (option.value === current) { matched = true; }
    select.appendChild(item);
  }
  if (!matched && current) {
    const item = el('option', '', current + '（自定义）');
    item.value = current;
    select.appendChild(item);
  }
  select.value = current;
  select.addEventListener('change', () => {
    obj[key] = select.value;
    onBump();
  });
  box.appendChild(select);
  return { select, matched, current };
}

function renderListField(box, obj, key, path) {
  const values = Array.isArray(obj[key]) ? obj[key] : (obj[key] = []);
  const holder = el('div', 'chips');
  values.forEach((value, index) => {
    const chip = el('span', 'chip', String(value));
    const remove = el('button', '', '×');
    remove.title = '移除';
    remove.addEventListener('click', () => {
      values.splice(index, 1);
      onBump();
      renderSection();
    });
    chip.appendChild(remove);
    holder.appendChild(chip);
  });
  if (!values.length) { holder.appendChild(el('span', 'help', '（空）')); }
  box.appendChild(holder);

  const addRow = el('div', 'chip-add');
  const input = el('input');
  input.type = 'text';
  input.className = 'mono';
  input.placeholder = path === 'config.listen_groups' ? '群号' : '新增一项';
  const add = el('button', 'tiny', '添加');
  const commit = () => {
    const text = input.value.trim();
    if (!text) { return; }
    values.push(text);
    input.value = '';
    onBump();
    renderSection();
  };
  add.addEventListener('click', commit);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
  });
  addRow.appendChild(input);
  addRow.appendChild(add);
  box.appendChild(addRow);
}

function renderField(container, obj, key, path, opts) {
  const spec = opts || {};
  const value = obj[key];
  const wide = spec.wide || key === 'system_prompt' || Array.isArray(value)
    || path === 'report.browser_path' || path === 'repo.path' || path === 'repo.url'
    || path === 'settings.system_prompt';
  const { box } = fieldShell(key, path, wide);

  if (spec.type === 'bool' || typeof value === 'boolean') {
    renderBoolField(box, obj, key);
  } else if (spec.enums) {
    renderSelectField(box, obj, key, spec.enums, spec);
  } else if (spec.type === 'template') {
    const options = (META.templates || []).map((name) => ({ value: name, label: name }));
    if (!options.length) { options.push({ value: 'default', label: 'default' }); }
    renderSelectField(box, obj, key, options, spec);
  } else if (Array.isArray(value)) {
    renderListField(box, obj, key, path);
  } else if (typeof value === 'number' || spec.type === 'number') {
    renderNumberField(box, obj, key, path, spec);
  } else if (spec.type === 'textarea') {
    const area = el('textarea');
    area.className = 'mono';
    area.rows = 4;
    area.value = value === undefined || value === null ? '' : String(value);
    area.addEventListener('input', () => {
      if (area.value.trim() === '' && spec.nullable) { delete obj[key]; } else { obj[key] = area.value; }
      onBump();
    });
    box.appendChild(area);
  } else if (path === 'report.browser_path') {
    renderTextField(box, obj, key, path, { mono: true, placeholder: '留空自动探测' });
    const row = el('div', 'row');
    row.style.marginTop = '5px';
    const detect = el('button', 'tiny', '用检测到的浏览器');
    detect.disabled = !META.browser;
    detect.addEventListener('click', () => {
      obj[key] = META.browser || '';
      onBump();
      renderSection();
    });
    row.appendChild(detect);
    if (META.browser) {
      row.appendChild(el('span', 'help', META.browser));
    }
    box.appendChild(row);
  } else if (spec.secret) {
    renderTextField(box, obj, key, path, { secret: true, mono: true, nullable: spec.nullable });
  } else {
    renderTextField(box, obj, key, path, { mono: spec.mono, nullable: spec.nullable });
  }
  addHelp(box, path);
  container.appendChild(box);
}

function groupFields(defaults, scope, groups) {
  // 嵌套对象（project.ui / project.repo 等）由专门的区块渲染，不在这里铺成字段
  const keys = Object.keys(defaults || {}).filter((key) => !isPlainObject(defaults[key]));
  const used = new Set();
  const result = [];
  for (const [title, list] of groups) {
    const present = list.filter((key) => keys.includes(key));
    present.forEach((key) => used.add(key));
    if (present.length) { result.push({ title, keys: present }); }
  }
  const rest = keys.filter((key) => !used.has(key));
  if (rest.length) { result.push({ title: '其他', keys: rest }); }
  return result;
}

function specFor(key, value, path) {
  const spec = {};
  if (path === 'settings.system_prompt') { spec.type = 'textarea'; }
  if (path === 'report.template') { spec.type = 'template'; }
  if (path === 'report.browser_path') { spec.type = 'browser'; }
  if ((path === 'provider.api_key') || path === 'model.api_key') { spec.secret = true; }
  if (path.startsWith('llm.') && (key === 'temperature' || key === 'top_p' || key === 'max_tokens')) {
    spec.nullable = true;
  }
  if (META.enums[path]) { spec.enums = META.enums[path]; }
  if (key.startsWith('max_') || key.endsWith('_seconds') || key.endsWith('_ms')
    || key.endsWith('_kb') || key.endsWith('_mb') || key.endsWith('_px')
    || key === 'history_period_hours') {
    spec.type = 'number';
  }
  if (path.endsWith('temperature') || path.endsWith('top_p')
    || path.endsWith('device_scale_factor') || path.endsWith('settle_seconds')
    || path === 'settings.file_list_delay_seconds') {
    spec.float = true;
  }
  return spec;
}

function renderGroups(container, obj, defaults, scope, groups) {
  for (const group of groupFields(defaults, scope, groups)) {
    const box = el('section', 'group');
    box.appendChild(el('h3', '', group.title));
    const grid = el('div', 'grid');
    for (const key of group.keys) {
      renderField(grid, obj, key, scope + '.' + key, specFor(key, obj[key], scope + '.' + key));
    }
    box.appendChild(grid);
    container.appendChild(box);
  }
}

function formHead(container, title, subtitle) {
  const head = el('div', 'form-head');
  head.appendChild(el('h2', '', title));
  if (subtitle) { head.appendChild(el('span', 'sub', subtitle)); }
  container.appendChild(head);
}

// ── 各分区 ──

function renderOnebot(host) {
  formHead(host, 'OneBot 连接', '与协议端（LL4 / SnowLuma / NapCat 等）的通信方式');
  renderGroups(host, CFG.onebot, META.defaults.onebot, 'onebot', [['连接', Object.keys(META.defaults.onebot)]]);
}

function renderBot(host) {
  formHead(host, 'Bot 基础', 'data_dir 下的每个配置各有一套群组、模型与设置');
  const box = el('section', 'group');
  box.appendChild(el('h3', '', '运行'));
  const grid = el('div', 'grid');
  renderField(grid, CFG.bot, 'data_dir', 'bot.data_dir');
  box.appendChild(grid);
  host.appendChild(box);

  const list = el('section', 'group');
  list.appendChild(el('h3', '', '配置列表（bot.configs）'));
  const names = Object.keys(CFG.bot.configs || {});
  if (!names.length) {
    list.appendChild(el('div', 'empty-note', '还没有配置，点下面的「新增配置」开始。'));
  }
  for (const name of names) {
    const row = el('div', 'card-head');
    row.style.margin = '0 0 8px';
    const label = el('button', 'ghost', name);
    label.addEventListener('click', () => selectSection('config:' + name));
    row.appendChild(label);
    row.appendChild(el('span', 'help',
      (CFG.bot.configs[name].listen_groups || []).length + ' 个监听群'));
    const spacer = el('span', 'spacer');
    row.appendChild(spacer);
    const rename = el('button', 'tiny', '重命名');
    rename.addEventListener('click', () => renameConfig(name));
    const remove = el('button', 'tiny danger', '删除');
    remove.addEventListener('click', () => removeConfig(name));
    row.appendChild(rename);
    row.appendChild(remove);
    list.appendChild(row);
  }
  const add = el('button', 'tiny primary', '新增配置');
  add.addEventListener('click', addConfig);
  list.appendChild(add);
  host.appendChild(list);

  const help = el('section', 'group');
  help.appendChild(el('h3', '', '这份配置怎么用'));
  const ol = el('ol', 'hint-list');
  for (const text of [
    '先在「LLM 端点」里填好端点和模型（含 API Key）。',
    '再到「Bot 配置 → 每个配置 → 候选模型」按优先级挑选模型。',
    '填好监听群与项目名，点右上角下载 config.json，放到项目根目录。',
  ]) {
    const li = el('li');
    li.textContent = text;
    ol.appendChild(li);
  }
  help.appendChild(ol);
  host.appendChild(help);
}

function renderConfig(host, name) {
  const spec = CFG.bot.configs[name];
  if (!spec) {
    host.appendChild(el('div', 'empty-note', '配置不存在'));
    return;
  }
  formHead(host, name, '群组 / 项目 / 仓库 / 候选模型 / 视觉 / 报告 / 分析 / 指令');

  const tabs = el('div', 'tabs');
  const tabDefs = [
    ['groups', '群组与项目'],
    ['repo', '仓库'],
    ['models', '候选模型'],
    ['vision', '看图'],
    ['report', '报告'],
    ['settings', '分析设置'],
    ['commands', '指令'],
  ];
  for (const [id, label] of tabDefs) {
    const tab = el('div', 'tab' + (CONFIG_TAB === id ? ' active' : ''), label);
    tab.addEventListener('click', () => {
      CONFIG_TAB = id;
      renderSection();
    });
    tabs.appendChild(tab);
  }
  host.appendChild(tabs);

  if (CONFIG_TAB === 'groups') { renderConfigGroups(host, spec, name); }
  else if (CONFIG_TAB === 'repo') { renderConfigRepo(host, spec, name); }
  else if (CONFIG_TAB === 'models') { renderConfigModels(host, spec, name); }
  else if (CONFIG_TAB === 'vision') {
    renderGroups(host, spec.vision, META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].vision,
      'vision', VISION_GROUPS);
  } else if (CONFIG_TAB === 'report') {
    renderGroups(host, spec.report, META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].report,
      'report', REPORT_GROUPS);
  } else if (CONFIG_TAB === 'settings') {
    renderGroups(host, spec.settings, META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].settings,
      'settings', SETTINGS_GROUPS);
  } else if (CONFIG_TAB === 'commands') {
    renderGroups(host, spec.commands, META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].commands,
      'commands', COMMAND_GROUPS);
  }
}

function renderConfigGroups(host, spec, name) {
  const box = el('section', 'group');
  box.appendChild(el('h3', '', '群组'));
  const grid = el('div', 'grid');
  renderField(grid, spec, 'listen_groups', 'config.listen_groups');
  renderField(grid, spec, 'notify_group', 'config.notify_group');
  box.appendChild(grid);
  host.appendChild(box);

  const projectDefaults = META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].project;
  renderGroups(host, spec.project, projectDefaults, 'project', PROJECT_GROUPS);
  renderProjectUI(host, spec.project);
}

function renderProjectUI(host, project) {
  const box = el('section', 'group');
  box.appendChild(el('h3', '', '按 UI 开关（project.ui）'));
  const help = el('div', 'help', '只控制是否分析该 UI 的日志；未配置时回退 auto。');
  box.appendChild(help);
  const grid = el('div', 'grid');
  const ui = isPlainObject(project.ui) ? project.ui : (project.ui = {});
  const keys = Array.from(new Set([...UI_KEYS, ...Object.keys(ui)]));
  for (const key of keys) {
    const current = isPlainObject(ui[key]) ? !!ui[key].enabled : true;
    const wrap = el('div', 'field');
    const line = el('div', 'switch');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = current;
    const label = el('span', '', key);
    input.addEventListener('change', () => {
      ui[key] = { enabled: input.checked };
      onBump();
    });
    line.appendChild(input);
    line.appendChild(label);
    wrap.appendChild(line);
    grid.appendChild(wrap);
  }
  box.appendChild(grid);
  host.appendChild(box);
}

function renderConfigRepo(host, spec, name) {
  const project = spec.project || (spec.project = {});
  if (!isPlainObject(project.repo)) { project.repo = clone(META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].project.repo); }
  renderGroups(host, project.repo, META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].project.repo,
    'repo', REPO_GROUPS);
}

function providerOptions() {
  return (CFG.llm.providers || []).map((provider) => String(provider.name || ''));
}

function modelOptions(providerName) {
  const provider = (CFG.llm.providers || []).find((item) => String(item.name || '') === providerName);
  if (!provider) { return []; }
  return (provider.models || []).map((item) => String(item.model || ''));
}

function renderConfigModels(host, spec, name) {
  if (!isPlainObject(spec.llm)) { spec.llm = { models: [] }; }
  const llm = spec.llm;
  if (!Array.isArray(llm.models)) { llm.models = []; }

  const box = el('section', 'group');
  box.appendChild(el('h3', '', '候选模型（按优先级从上到下）'));
  box.appendChild(el('div', 'help',
    '每个候选是「端点名 + 模型名」。前一个失败（鉴权错误除外）会自动尝试下一个。'));

  if (!llm.models.length) {
    box.appendChild(el('div', 'empty-note', '还没有候选模型：先在「LLM 端点」里加端点和模型。'));
  }

  llm.models.forEach((ref, index) => {
    const card = el('div', 'card');
    const head = el('div', 'card-head');
    head.appendChild(el('span', 'idx', '#' + (index + 1)));
    const up = el('button', 'tiny ghost', '↑');
    up.disabled = index === 0;
    up.addEventListener('click', () => {
      const item = llm.models.splice(index, 1)[0];
      llm.models.splice(index - 1, 0, item);
      onBump();
      renderSection();
    });
    const down = el('button', 'tiny ghost', '↓');
    down.disabled = index === llm.models.length - 1;
    down.addEventListener('click', () => {
      const item = llm.models.splice(index, 1)[0];
      llm.models.splice(index + 1, 0, item);
      onBump();
      renderSection();
    });
    const remove = el('button', 'tiny danger', '移除');
    remove.addEventListener('click', () => {
      llm.models.splice(index, 1);
      onBump();
      renderSection();
    });
    head.appendChild(el('span', 'spacer'));
    head.appendChild(up);
    head.appendChild(down);
    head.appendChild(remove);
    card.appendChild(head);

    const grid = el('div', 'grid');

    // 端点选择
    const providerBox = fieldShell('name', 'llm.name').box;
    const providerSelect = el('select');
    const names = providerOptions();
    if (!names.length) {
      const option = el('option', '', '（还没有端点）');
      option.value = '';
      providerSelect.appendChild(option);
    }
    for (const providerName of names) {
      const option = el('option', '', providerName || '（未命名端点）');
      option.value = providerName;
      providerSelect.appendChild(option);
    }
    const currentProvider = String(ref.name || '');
    if (currentProvider && !names.includes(currentProvider)) {
      const option = el('option', '', currentProvider + '（不存在）');
      option.value = currentProvider;
      providerSelect.appendChild(option);
    }
    providerSelect.value = currentProvider;
    providerSelect.addEventListener('change', () => {
      ref.name = providerSelect.value;
      const models = modelOptions(ref.name);
      ref.model = models[0] || '';
      onBump();
      renderSection();
    });
    providerBox.appendChild(providerSelect);
    addHelp(providerBox, 'llm.models');
    grid.appendChild(providerBox);

    // 模型选择
    const modelBox = fieldShell('model', 'llm.model').box;
    const modelSelect = el('select');
    const models = modelOptions(String(ref.name || ''));
    if (!models.length) {
      const option = el('option', '', '（该端点没有模型）');
      option.value = '';
      modelSelect.appendChild(option);
    }
    for (const modelName of models) {
      const option = el('option', '', modelName || '（未命名模型）');
      option.value = modelName;
      modelSelect.appendChild(option);
    }
    const currentModel = String(ref.model || '');
    if (currentModel && !models.includes(currentModel)) {
      const option = el('option', '', currentModel + '（不存在）');
      option.value = currentModel;
      modelSelect.appendChild(option);
    }
    modelSelect.value = currentModel;
    modelSelect.addEventListener('change', () => {
      ref.model = modelSelect.value;
      onBump();
      renderSection();
    });
    modelBox.appendChild(modelSelect);
    grid.appendChild(modelBox);

    card.appendChild(grid);
    box.appendChild(card);
  });

  const add = el('button', 'tiny primary', '添加候选');
  add.disabled = !providerOptions().length;
  add.addEventListener('click', () => {
    const first = providerOptions()[0] || '';
    const models = modelOptions(first);
    llm.models.push({ name: first, model: models[0] || '' });
    onBump();
    renderSection();
  });
  box.appendChild(add);
  host.appendChild(box);

  const overrides = el('section', 'group');
  overrides.appendChild(el('h3', '', '本配置的参数覆盖（留空 = 跟随全局）'));
  const grid = el('div', 'grid');
  for (const key of ['temperature', 'top_p', 'max_tokens']) {
    renderField(grid, llm, key, 'llm.' + key, {
      nullable: true,
      float: key !== 'max_tokens',
      type: 'number',
    });
  }
  overrides.appendChild(grid);
  host.appendChild(overrides);
}

function renderLLM(host) {
  formHead(host, 'LLM 端点与模型', '全局声明可用的端点与模型；每个 Bot 配置只做引用');
  const llm = CFG.llm;
  if (!Array.isArray(llm.providers)) { llm.providers = []; }

  const listBox = el('section', 'group');
  listBox.appendChild(el('h3', '', '端点（llm.providers）'));
  if (!llm.providers.length) {
    listBox.appendChild(el('div', 'empty-note', '还没有端点。'));
  }

  llm.providers.forEach((provider, index) => {
    const card = el('div', 'card');
    const head = el('div', 'card-head');
    head.appendChild(el('span', 'idx', '#' + (index + 1)));
    const nameInput = el('input');
    nameInput.type = 'text';
    nameInput.className = 'mono';
    nameInput.value = String(provider.name || '');
    nameInput.placeholder = '端点名称，如 deepseek';
    nameInput.addEventListener('input', () => {
      provider.name = nameInput.value;
      onBump();
    });
    head.appendChild(nameInput);
    head.appendChild(el('span', 'spacer'));
    const remove = el('button', 'tiny danger', '删除端点');
    remove.addEventListener('click', () => {
      llm.providers.splice(index, 1);
      onBump();
      renderSection();
    });
    head.appendChild(remove);
    card.appendChild(head);

    const grid = el('div', 'grid');
    for (const key of ['kind', 'base_url', 'api_key', 'max_concurrency', 'timeout_seconds',
      'connect_timeout_seconds', 'max_retries']) {
      renderField(grid, provider, key, 'provider.' + key, specFor(key, provider[key], 'provider.' + key));
    }
    card.appendChild(grid);

    // 模型
    card.appendChild(el('div', 'sub-title', '该端点上的模型（llm.providers[].models）'));
    if (!Array.isArray(provider.models)) { provider.models = []; }
    provider.models.forEach((spec, mIndex) => {
      const box = el('div', 'card');
      box.style.background = '#1b1e23';
      const mHead = el('div', 'card-head');
      mHead.appendChild(el('span', 'idx', 'model #' + (mIndex + 1)));
      mHead.appendChild(el('span', 'spacer'));
      const mRemove = el('button', 'tiny danger', '移除');
      mRemove.addEventListener('click', () => {
        provider.models.splice(mIndex, 1);
        onBump();
        renderSection();
      });
      mHead.appendChild(mRemove);
      box.appendChild(mHead);

      const mGrid = el('div', 'grid');
      renderField(mGrid, spec, 'model', 'model.model', { mono: true });
      for (const key of ['max_tokens', 'max_context_tokens']) {
        renderField(mGrid, spec, key, 'model.' + key, specFor(key, spec[key], 'model.' + key));
      }
      for (const key of ['temperature', 'top_p']) {
        renderField(mGrid, spec, key, 'model.' + key, {
          type: 'number', float: true, nullable: true,
        });
      }
      box.appendChild(mGrid);

      if (!isPlainObject(spec.supports)) { spec.supports = { tools: true, vision: true, json_mode: true }; }
      const caps = el('div', 'grid');
      for (const key of ['tools', 'vision', 'json_mode']) {
        renderField(caps, spec.supports, key, 'model.supports.' + key);
      }
      box.appendChild(el('div', 'sub-title', '能力'));
      box.appendChild(caps);
      card.appendChild(box);
    });

    const addModel = el('button', 'tiny', '添加模型');
    addModel.addEventListener('click', () => {
      provider.models.push({
        model: '',
        supports: { tools: true, vision: false, json_mode: true },
        max_tokens: 0,
        max_context_tokens: 0,
      });
      onBump();
      renderSection();
    });
    card.appendChild(addModel);
    listBox.appendChild(card);
  });

  const addProvider = el('button', 'tiny primary', '添加端点');
  addProvider.addEventListener('click', () => {
    llm.providers.push({
      name: 'provider' + (llm.providers.length + 1),
      kind: 'openai',
      base_url: '',
      api_key: '',
      max_concurrency: 2,
      timeout_seconds: 180,
      connect_timeout_seconds: 10,
      max_retries: 2,
      models: [{
        model: '',
        supports: { tools: true, vision: false, json_mode: true },
        max_tokens: 0,
        max_context_tokens: 0,
      }],
    });
    onBump();
    renderSection();
  });
  listBox.appendChild(addProvider);
  host.appendChild(listBox);

  const defaults = META.defaults.llm;
  renderGroups(host, llm.defaults, defaults.defaults, 'defaults', [['默认参数', Object.keys(defaults.defaults)]]);
  renderGroups(host, llm.circuit_breaker, defaults.circuit_breaker, 'circuit_breaker',
    [['熔断', Object.keys(defaults.circuit_breaker)]]);
}

function renderSimple(host, section) {
  const titles = { debug: '调试', log: '日志' };
  const subtitles = {
    debug: '本地调试面板，正式部署请关闭',
    log: '控制台与文件日志',
  };
  formHead(host, titles[section], subtitles[section]);
  renderGroups(host, CFG[section], META.defaults[section], section,
    [['设置', Object.keys(META.defaults[section])]]);
}

function renderSection() {
  const host = $('form');
  host.innerHTML = '';
  if (!CFG) { return; }
  if (SECTION === 'onebot') { renderOnebot(host); $('formTitle').textContent = 'OneBot 连接'; }
  else if (SECTION === 'bot') { renderBot(host); $('formTitle').textContent = 'Bot 基础'; }
  else if (SECTION === 'llm') { renderLLM(host); $('formTitle').textContent = 'LLM 端点'; }
  else if (SECTION === 'debug' || SECTION === 'log') {
    renderSimple(host, SECTION);
    $('formTitle').textContent = SECTION === 'debug' ? '调试' : '日志';
  } else if (SECTION.startsWith('config:')) {
    const name = SECTION.slice(7);
    renderConfig(host, name);
    $('formTitle').textContent = '配置：' + name;
  }
  renderNav();
}

// ── 导航 ──

function renderNav() {
  const nav = $('nav');
  nav.innerHTML = '';
  const items = [
    ['onebot', 'OneBot 连接', ''],
    ['bot', 'Bot 基础', ''],
    ['llm', 'LLM 端点', String((CFG.llm.providers || []).length) + ' 个'],
    ['debug', '调试', ''],
    ['log', '日志', ''],
  ];
  for (const [id, label, count] of items) {
    const row = el('div', 'nav-item' + (SECTION === id ? ' active' : ''));
    row.appendChild(el('span', '', label));
    if (count) { row.appendChild(el('span', 'count', count)); }
    row.addEventListener('click', () => selectSection(id));
    nav.appendChild(row);
  }

  const names = Object.keys(CFG.bot.configs || {});
  const groupLabel = el('div', 'nav-item');
  groupLabel.style.marginTop = '8px';
  groupLabel.style.color = 'var(--faint)';
  groupLabel.appendChild(el('span', '', 'Bot 配置'));
  groupLabel.appendChild(el('span', 'count', names.length + ' 个'));
  nav.appendChild(groupLabel);

  const sub = el('div', 'nav-sub');
  for (const name of names) {
    const active = SECTION === 'config:' + name;
    const row = el('div', 'nav-item' + (active ? ' active' : ''));
    row.appendChild(el('span', '', name || '（空名称）'));
    row.addEventListener('click', () => selectSection('config:' + name));
    sub.appendChild(row);
  }
  const add = el('div', 'nav-item');
  add.style.color = 'var(--ok)';
  add.appendChild(el('span', '', '＋ 新增配置'));
  add.addEventListener('click', addConfig);
  sub.appendChild(add);
  nav.appendChild(sub);
}

function selectSection(id) {
  SECTION = id;
  if (id.startsWith('config:')) {
    ACTIVE_CONFIG = id.slice(7);
    if (!CONFIG_TAB) { CONFIG_TAB = 'groups'; }
  }
  renderSection();
}

// ── 配置增删改 ──

function configTemplate() {
  return clone(META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME]);
}

function addConfig() {
  const names = Object.keys(CFG.bot.configs || {});
  let index = names.length + 1;
  let name = '配置' + index;
  while (CFG.bot.configs[name]) { index += 1; name = '配置' + index; }
  CFG.bot.configs[name] = configTemplate();
  if (isPlainObject(CFG.bot.configs[name].llm)) { CFG.bot.configs[name].llm.models = []; }
  CFG.bot.configs[name].listen_groups = [];
  CFG.bot.configs[name].notify_group = '';
  onBump();
  selectSection('config:' + name);
  toast('已新增配置 ' + name, 'ok');
}

function renameConfig(oldName) {
  askDialog({
    title: '重命名配置',
    hint: '只改配置名，里面的内容不变。',
    value: oldName,
    okLabel: '重命名',
  }).then((input) => {
    if (input === null) { return; }
    const name = String(input).trim();
    if (!name || name === oldName) { return; }
    if (CFG.bot.configs[name]) {
      toast('已存在同名配置', 'bad');
      return;
    }
    const rebuilt = {};
    for (const [key, value] of Object.entries(CFG.bot.configs)) {
      rebuilt[key === oldName ? name : key] = value;
    }
    CFG.bot.configs = rebuilt;
    onBump();
    selectSection('config:' + name);
  });
}

function removeConfig(name) {
  askDialog({
    title: '删除配置「' + name + '」？',
    hint: '这个配置里的群组、模型、报告设置都会一起消失。',
    input: false,
    okLabel: '删除',
    danger: true,
  }).then((yes) => {
    if (!yes) { return; }
    delete CFG.bot.configs[name];
    onBump();
    selectSection('bot');
  });
}

// ── JSON 与校验 ──

function jsonText() {
  return JSON.stringify(CFG, null, 2);
}

function highlight(text) {
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
      (match) => {
        let cls = 'n';
        if (/^"/.test(match)) {
          cls = /:$/.test(match) ? 'k' : 's';
        } else if (/true|false/.test(match)) {
          cls = 'b';
        } else if (/null/.test(match)) {
          cls = 'p';
        }
        return '<span class="' + cls + '">' + match + '</span>';
      });
}

function scheduleJson() {
  clearTimeout(JSON_TIMER);
  JSON_TIMER = setTimeout(() => {
    $('json').innerHTML = highlight(jsonText());
  }, 120);
}

function scheduleValidate() {
  clearTimeout(VALIDATE_TIMER);
  VALIDATE_TIMER = setTimeout(runValidate, 420);
}

async function runValidate() {
  if (VALIDATING || !CFG) { return; }
  VALIDATING = true;
  try {
    const response = await fetch('/api/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: CFG }),
    });
    const data = await response.json();
    VALIDATION = {
      errors: Array.isArray(data.errors) ? data.errors : [],
      warnings: Array.isArray(data.warnings) ? data.warnings : [],
    };
  } catch (err) {
    VALIDATION = { errors: ['校验请求失败：' + (err && err.message ? err.message : err)], warnings: [] };
  } finally {
    VALIDATING = false;
    renderValidation();
  }
}

function renderValidation() {
  const errors = VALIDATION.errors;
  const warnings = VALIDATION.warnings;
  const badge = $('validBadge');
  if (errors.length) {
    badge.className = 'badge bad';
    badge.textContent = errors.length + ' 个错误';
  } else if (warnings.length) {
    badge.className = 'badge warn';
    badge.textContent = warnings.length + ' 个提醒';
  } else {
    badge.className = 'badge ok';
    badge.textContent = '校验通过';
  }
  $('issueCount').textContent = errors.length + warnings.length
    ? ' ' + (errors.length + warnings.length) : '';

  const host = $('report');
  host.innerHTML = '';
  for (const text of errors) {
    const row = el('div', 'issue error');
    row.appendChild(el('span', 'mark', '✕'));
    row.appendChild(el('span', '', text));
    host.appendChild(row);
  }
  for (const text of warnings) {
    const row = el('div', 'issue warn');
    row.appendChild(el('span', 'mark', '!'));
    row.appendChild(el('span', '', text));
    host.appendChild(row);
  }
  if (!errors.length && !warnings.length) {
    host.appendChild(el('div', 'ok-note', '校验通过，可以导出使用。'));
  }
  const list = el('ul', 'hint-list');
  list.style.marginTop = '14px';
  const tips = [
    '导出后把 config.json 放到项目根目录，重启程序生效。',
    'api_key 以明文保存，注意不要提交到公开仓库。',
    '校验只做静态检查，能否真正调通取决于端点与网络。',
  ];
  for (const tip of tips) {
    const li = el('li', '', tip);
    list.appendChild(li);
  }
  host.appendChild(list);
}

function switchRightView(view) {
  document.querySelectorAll('.right-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === view);
  });
  $('jsonPane').style.display = view === 'json' ? '' : 'none';
  $('reportPane').style.display = view === 'report' ? '' : 'none';
}

// ── 草稿 / 导入 / 导出 ──

function saveDraft() {
  if (!$('draftToggle').checked) { return; }
  try {
    localStorage.setItem(DRAFT_KEY, jsonText());
  } catch (err) { /* 隐私模式等场景忽略 */ }
}

function loadDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) { return null; }
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

function normalizeCfg(doc) {
  const base = clone(META.defaults);
  const merged = deepMerge(base, isPlainObject(doc) ? doc : {});
  if (!isPlainObject(merged.bot)) { merged.bot = clone(META.defaults.bot); }
  // bot.configs 以导入内容为准，避免和编辑器里的旧配置混在一起
  const incoming = isPlainObject(doc) && isPlainObject(doc.bot) && isPlainObject(doc.bot.configs)
    ? Object.keys(doc.bot.configs).filter((name) => isPlainObject(doc.bot.configs[name]))
    : [];
  if (incoming.length) {
    merged.bot.configs = {};
    for (const name of incoming) {
      const source = doc.bot.configs[name];
      const entry = deepMerge(configTemplate(), source);
      // 导入内容没写候选模型时不要塞进模板里的示例引用（会指向不存在的端点）
      if (!isPlainObject(source.llm)) { entry.llm = { models: [] }; }
      merged.bot.configs[name] = entry;
    }
  }
  if (!isPlainObject(merged.bot.configs) || !Object.keys(merged.bot.configs).length) {
    const name = DEFAULT_TEMPLATE_NAME;
    merged.bot.configs = { [name]: configTemplate() };
  }
  if (!isPlainObject(merged.llm)) { merged.llm = clone(META.defaults.llm); }
  if (!Array.isArray(merged.llm.providers)) { merged.llm.providers = []; }
  for (const provider of merged.llm.providers) {
    if (!isPlainObject(provider)) { continue; }
    if (!Array.isArray(provider.models)) { provider.models = []; }
    for (const spec of provider.models) {
      if (isPlainObject(spec) && !isPlainObject(spec.supports)) {
        spec.supports = { tools: true, vision: false, json_mode: true };
      }
    }
  }
  for (const spec of Object.values(merged.bot.configs)) {
    if (!isPlainObject(spec)) { continue; }
    const tpl = META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME];
    specsDefault(spec, 'project', tpl.project);
    if (isPlainObject(spec.project)) {
      specsDefault(spec.project, 'repo', tpl.project.repo);
      if (!isPlainObject(spec.project.ui)) { spec.project.ui = clone(tpl.project.ui); }
    }
    specsDefault(spec, 'settings', META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].settings);
    specsDefault(spec, 'vision', META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].vision);
    specsDefault(spec, 'report', META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].report);
    specsDefault(spec, 'commands', META.defaults.bot.configs[DEFAULT_TEMPLATE_NAME].commands);
    if (!isPlainObject(spec.llm)) { spec.llm = { models: [] }; }
    if (!Array.isArray(spec.llm.models)) { spec.llm.models = []; }
    if (!Array.isArray(spec.listen_groups)) { spec.listen_groups = []; }
  }
  return merged;
}

function specsDefault(spec, key, defaults) {
  if (!isPlainObject(spec[key])) { spec[key] = clone(defaults); }
}

function openImport(prefill) {
  $('modalBack').classList.add('show');
  $('modalText').value = prefill || '';
  setTimeout(() => $('modalText').focus(), 30);
}

function closeImport() {
  $('modalBack').classList.remove('show');
}

let ASK_SETTLE = null;

// 通用确认/输入框：input === false 时返回 true/false，否则返回字符串（取消返回 null）
function askDialog(opts) {
  const o = Object.assign(
    { title: '', hint: '', value: '', okLabel: '确定', danger: false, input: true },
    opts || {},
  );
  if (ASK_SETTLE) { settleAsk(o.input ? null : false); }
  return new Promise((resolve) => {
    ASK_SETTLE = resolve;
    $('askTitle').textContent = o.title;
    $('askHint').textContent = o.hint;
    $('askHint').style.display = o.hint ? '' : 'none';
    const input = $('askInput');
    input.value = o.value;
    input.style.display = o.input ? '' : 'none';
    const ok = $('askOk');
    ok.textContent = o.okLabel;
    ok.className = o.danger ? 'danger' : 'primary';
    $('askBack').classList.add('show');
    setTimeout(() => { if (o.input) { input.focus(); input.select(); } else { ok.focus(); } }, 30);
  });
}

function settleAsk(value) {
  $('askBack').classList.remove('show');
  const resolve = ASK_SETTLE;
  ASK_SETTLE = null;
  if (resolve) { resolve(value); }
}

function cancelAsk() {
  settleAsk($('askInput').style.display === 'none' ? false : null);
}

function applyImport(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    toast('JSON 解析失败：' + err.message, 'bad');
    return;
  }
  CFG = normalizeCfg(parsed);
  closeImport();
  const names = Object.keys(CFG.bot.configs);
  ACTIVE_CONFIG = names[0] || '';
  SECTION = 'onebot';
  onBump();
  renderSection();
  scheduleJson();
  runValidate();
  toast('已导入 ' + names.length + ' 个配置', 'ok');
}

function download() {
  const blob = new Blob([jsonText() + '\n'], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'config.json';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast('已开始下载 config.json', 'ok');
}

async function copyJson() {
  const text = jsonText();
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板', 'ok');
    return;
  } catch (err) {
    // 非 https / 无权限时退回 execCommand
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', 'readonly');
  area.style.cssText = 'position:fixed;top:-1000px;left:-1000px;';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    ok = false;
  }
  document.body.removeChild(area);
  if (ok) { toast('已复制到剪贴板', 'ok'); }
  else { toast('复制失败，请手动在 JSON 面板选择', 'bad'); }
}

// ── 事件 ──

$('exportBtn').addEventListener('click', download);
$('copyBtn').addEventListener('click', copyJson);
$('importBtn').addEventListener('click', () => openImport(''));
$('modalCancel').addEventListener('click', closeImport);
$('modalOk').addEventListener('click', () => applyImport($('modalText').value));
$('modalFile').addEventListener('click', () => $('filePicker').click());
$('filePicker').addEventListener('change', () => {
  const file = $('filePicker').files && $('filePicker').files[0];
  if (!file) { return; }
  const reader = new FileReader();
  reader.onload = () => { openImport(String(reader.result || '')); };
  reader.readAsText(file, 'utf-8');
  $('filePicker').value = '';
});
$('modalBack').addEventListener('mousedown', (event) => {
  if (event.target === $('modalBack')) { closeImport(); }
});

$('askCancel').addEventListener('click', () => cancelAsk());
$('askOk').addEventListener('click', () => {
  settleAsk($('askInput').style.display === 'none' ? true : $('askInput').value);
});
$('askInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    settleAsk($('askInput').value);
  }
});
$('askBack').addEventListener('mousedown', (event) => {
  if (event.target === $('askBack')) { cancelAsk(); }
});

$('resetBtn').addEventListener('click', async () => {
  const yes = await askDialog({
    title: '恢复默认配置？', hint: '当前编辑内容会丢失，且无法撤销。', input: false, okLabel: '恢复默认',
  });
  if (!yes) { return; }
  CFG = normalizeCfg(META.defaults);
  const names = Object.keys(CFG.bot.configs);
  ACTIVE_CONFIG = names[0] || '';
  SECTION = 'onebot';
  onBump();
  renderSection();
  scheduleJson();
  runValidate();
  toast('已恢复默认', 'ok');
});
$('clearBtn').addEventListener('click', async () => {
  const yes = await askDialog({
    title: '清空所有内容？', hint: '会保留一份空的配置骨架，方便从头填。', input: false, okLabel: '清空', danger: true,
  });
  if (!yes) { return; }
  CFG = normalizeCfg({
    onebot: { mode: 'http_ws', http_url: '', ws_url: '', ws_reverse_port: 8080, access_token: '', api_timeout_seconds: 30 },
    bot: {
      data_dir: 'data',
      configs: {
        [DEFAULT_TEMPLATE_NAME]: {
          listen_groups: [],
          notify_group: '',
          project: { name: '', ui: {} },
          llm: { models: [] },
        },
      },
    },
    llm: { providers: [] },
  });
  const names = Object.keys(CFG.bot.configs);
  ACTIVE_CONFIG = names[0] || '';
  SECTION = 'onebot';
  onBump();
  renderSection();
  scheduleJson();
  runValidate();
  toast('已清空', 'ok');
});

$('draftToggle').addEventListener('change', () => {
  if ($('draftToggle').checked) {
    saveDraft();
    toast('已在本浏览器保存草稿（含密钥）', 'ok');
  } else {
    try { localStorage.removeItem(DRAFT_KEY); } catch (err) { /* ignore */ }
    toast('已停止保存草稿');
  }
});

document.querySelectorAll('.right-tab').forEach((tab) => {
  tab.addEventListener('click', () => switchRightView(tab.dataset.view));
});

document.addEventListener('keydown', (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === 's') {
    event.preventDefault();
    download();
  }
  if (event.key === 'Escape') {
    if ($('askBack').classList.contains('show')) { cancelAsk(); }
    else { closeImport(); }
  }
});

// ── 启动 ──

async function boot() {
  META = await (await fetch('/api/meta')).json();
  $('rootPath').textContent = '· ' + META.templates.length + ' 个模板';
  const draft = loadDraft();
  if (draft) {
    $('draftToggle').checked = true;
    CFG = normalizeCfg(draft);
  } else {
    CFG = normalizeCfg(META.defaults);
  }
  const names = Object.keys(CFG.bot.configs);
  ACTIVE_CONFIG = names[0] || '';
  renderSection();
  scheduleJson();
  await runValidate();
  setInterval(() => { if (!VALIDATING) { runValidate(); } }, 4000);
}

boot().catch((err) => {
  $('form').innerHTML = '';
  $('form').appendChild(el('div', 'empty-note',
    '初始化失败：' + (err && err.message ? err.message : err)));
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
