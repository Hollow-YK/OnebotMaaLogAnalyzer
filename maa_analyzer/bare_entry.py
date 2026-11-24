#!/usr/bin/env python3
"""Bare OneBot entry point for OnebotMaaLogAnalyzer.

The analyzer workflow lives in :mod:`maa_analyzer.runtime`. This module owns
only OneBot v11 transport, bare-mode configuration, debugging, and supervision.
The root ``main.py`` is a stable launcher; this module is the real application
entry point that a versioned install runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from maa_analyzer.config import (
    ONEBOT_MODES,
    default_config,
    load_config_file,
    resolve_data_dir,
    setup_logging,
    validate_common_config,
    validate_onebot_config,
    write_default_config,
)
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter
from maa_analyzer.platforms.onebot.api import OneBotAPI
from maa_analyzer.platforms.onebot.client import OneBotWS
from maa_analyzer.platforms.onebot.handler import EventHandler
from maa_analyzer.runtime import MaaWorkflowRuntime

if TYPE_CHECKING:
    from debug import DebugManager

logger = logging.getLogger("Maa")

RESTART_EXIT_CODE = 87


def load_config(path: str = "config.json") -> dict:
    """Load the bare config, creating the full template on first run."""
    target = Path(path)
    if not target.exists():
        write_default_config(target)
        logger.info(
            "已生成默认配置: %s，请填写监听群与 LLM 后重启。", path
        )
        raise SystemExit(0)
    return load_config_file(target)


def _load_bare_config(path: str = "config.json") -> dict:
    """Validate common and OneBot fields for startup and hot reload."""
    cfg = load_config_file(path)
    errors = validate_common_config(cfg)
    if errors:
        raise ValueError("\n".join(errors))
    validate_onebot_config(cfg)
    return cfg


def _resolve_onebot_mode(cfg: dict, *, debug_enabled: bool) -> str:
    """Keep offline debugging independent of live OneBot endpoints."""
    if debug_enabled:
        mode = str((cfg.get("onebot") or {}).get("mode", "http_ws"))
        if mode not in ONEBOT_MODES:
            raise ValueError(
                f"未知模式: {mode}，可选: {', '.join(ONEBOT_MODES)}"
            )
        return mode
    return validate_onebot_config(cfg)


def _print_config_template() -> bool:
    print(json.dumps(default_config(), ensure_ascii=False, indent=2))
    return True


def _install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _template_root() -> Path:
    """Use the install-root templates so user edits survive version switches."""
    root = Path(os.environ.get("MAA_INSTALL_ROOT") or _install_root())
    return root / "templates"


class BotRuntime:
    """Bare OneBot supervisor around the shared analyzer runtime."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api: Optional[OneBotAPI] = None
        self.adapter: Optional[OneBotPlatformAdapter] = None
        self.ws: Optional[OneBotWS] = None
        self.runtime: Optional[MaaWorkflowRuntime] = None
        self.handler: Optional[EventHandler] = None
        self.debug_manager: Optional[DebugManager] = None
        self.mode = ""
        self._tasks: list[asyncio.Task] = []
        self._restart_event: Optional[asyncio.Event] = None
        self._restart_requested = False

    @property
    def service(self):
        return self.runtime.service if self.runtime is not None else None

    @property
    def dm(self):
        return self.runtime.dm if self.runtime is not None else None

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def _request_restart(self) -> None:
        self._restart_requested = True
        if self._restart_event is not None:
            self._restart_event.set()

    async def start(self) -> None:
        if self.runtime is not None and self.runtime.running:
            return

        cfg = self.cfg
        setup_logging(cfg.get("log") or {})
        errors = validate_common_config(cfg)
        if errors:
            raise ValueError("\n".join(errors))

        ob = cfg.get("onebot") or {}
        debug_cfg = cfg.get("debug") or {}
        debug_enabled = bool(debug_cfg.get("enabled", False))
        self.mode = _resolve_onebot_mode(cfg, debug_enabled=debug_enabled)
        token = str(ob.get("access_token", "") or "")
        http_url = str(ob.get("http_url", "") or "")
        ws_url = str(ob.get("ws_url", "") or "")
        ws_reverse_port = int(ob.get("ws_reverse_port", 0) or 0)
        ws_reverse_host = str(
            ob.get("ws_reverse_host", "127.0.0.1") or "127.0.0.1"
        )
        api_timeout = int(ob.get("api_timeout_seconds", 30) or 30)

        use_http = self.mode in ("http_ws", "http_ws_reverse")
        self.api = OneBotAPI(
            http_url=http_url if use_http else "",
            access_token=token,
            timeout_seconds=api_timeout,
        )
        self.ws = OneBotWS(access_token=token)
        if self.mode in ("ws", "ws_reverse"):
            self.api.set_ws_send(self.ws.send)
            self.ws.on_api_response(self.api.handle_ws_response)

        data_dir = resolve_data_dir(
            cfg,
            base_dir=Path.cwd(),
            override=(
                str(debug_cfg.get("data_dir") or "data/test")
                if debug_enabled
                else None
            ),
        )
        self.adapter = OneBotPlatformAdapter(
            self.api, data_dir / "_outbox"
        )
        self._restart_event = asyncio.Event()
        self.runtime = MaaWorkflowRuntime(
            port=self.adapter,
            cfg=cfg,
            data_dir=data_dir,
            template_root=_template_root(),
            owns_port=False,
            background_services=not debug_enabled,
            restart_callback=self._request_restart,
            config_path=Path("config.json").resolve(),
        )
        try:
            await self.runtime.start()
        except BaseException:
            await self.stop()
            raise

        assert self.runtime.service is not None
        self.runtime.service.register_runtime_reload(self._handle_reload)
        self.runtime.service.register_reload_loader(_load_bare_config)
        self.handler = EventHandler(self.adapter, self.runtime.service)
        self.ws.on_event("message", self.handler.on_message)
        self.ws.on_event("notice", self.handler.on_notice)
        self.ws.on_event("request", self.handler.on_request)

        try:
            from maa_analyzer.update.boot import confirm_boot

            confirm_boot()
        except Exception:
            logger.debug("启动确认失败", exc_info=True)

        self._tasks = []
        if debug_enabled:
            try:
                from debug import DebugManager
                from debug.http_server import start_http_server
            except ImportError as exc:
                raise RuntimeError(
                    "配置启用了 debug.enabled，但未找到 debug 模块。"
                    "调试模式仅适用于源码仓库。"
                ) from exc
            self.debug_manager = DebugManager.from_config(
                cfg, data_dir=str(data_dir)
            )
            http_port = int(debug_cfg.get("http_port", 8765))
            self._tasks.append(
                asyncio.create_task(
                    start_http_server(http_port, self.debug_manager)
                )
            )
            logger.info(
                "[Debug] 离线调试模式：数据目录 %s，HTTP 端点 127.0.0.1:%s",
                data_dir,
                http_port,
            )
        else:
            if self.mode in ("http_ws", "ws"):
                self._tasks.append(
                    asyncio.create_task(self.ws.connect(ws_url))
                )
            if self.mode in ("ws_reverse", "http_ws_reverse"):
                self._tasks.append(
                    asyncio.create_task(
                        self.ws.serve(ws_reverse_host, ws_reverse_port)
                    )
                )

        logger.info(
            "OnebotMaaLogAnalyzer 已就绪 (mode=%s)，等待日志包上传...",
            self.mode,
        )

    async def stop(self) -> None:
        """Stop transport and the shared runtime, persisting state."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.ws is not None:
            try:
                await self.ws.stop()
            except Exception as exc:
                logger.warning("停止 WS 失败：%s", exc)
        if self.api is not None:
            try:
                await self.api.close()
            except Exception as exc:
                logger.warning("关闭 API 失败：%s", exc)
        if self.runtime is not None:
            await self.runtime.stop()

    async def _handle_reload(
        self, new_cfg: dict, notify_group: str = ""
    ) -> tuple[bool, str]:
        previous = self.cfg
        logger.info("[重载] 正在停止当前运行时 ...")
        await self.stop()
        self.cfg = new_cfg
        self.runtime = None
        self.api = None
        self.ws = None
        self.adapter = None
        self.handler = None
        try:
            await self.start()
        except Exception as exc:
            logger.exception("[重载] 按新配置启动失败，回退到旧配置")
            self.cfg = previous
            await self.stop()
            self.runtime = None
            self.api = None
            self.ws = None
            self.adapter = None
            self.handler = None
            try:
                await self.start()
            except Exception:
                logger.exception("[重载] 回退启动也失败，请重启进程")
                return False, f"重载失败，且回退失败：{exc}"
            await self._notify(
                notify_group, f"重载失败，已回退到旧配置：{exc}"
            )
            return False, f"重载失败，已回退到旧配置：{exc}"

        await self._notify(
            notify_group,
            "重载完成：配置、日志文件、LLM 与连接均已按新设置生效。",
        )
        return True, "已重新加载并重建运行时。"

    async def _notify(self, group_id: str, text: str) -> None:
        if not group_id or self.service is None:
            return
        try:
            await self.service.send_text(int(group_id), text)
        except Exception as exc:
            logger.warning("[重载] 发送结果消息失败：%s", exc)

    async def supervise(self) -> None:
        """Wait for transport tasks or an updater-requested restart."""
        while not self._restart_requested:
            tasks = list(self._tasks)
            if self._restart_event is None:
                return
            event_task = asyncio.create_task(self._restart_event.wait())
            waiters: list[asyncio.Task] = [*tasks, event_task]
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            event_task.cancel()
            try:
                await event_task
            except asyncio.CancelledError:
                pass
            if self._restart_requested:
                return
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.error("后台任务异常退出：%s", exc, exc_info=exc)
            if list(self._tasks) == tasks and all(task.done() for task in tasks):
                return


def _validate_ai_config(cfg: dict) -> bool:
    errors = validate_common_config(cfg)
    if errors:
        logger.error("[配置] 配置无效，已拒绝启动：")
        for error in errors:
            logger.error("  - %s", error)
        logger.error(
            "可运行 `python main.py --print-config-template` 查看新模板。"
        )
        return False
    return True


def _restart_after_update(runtime: BotRuntime) -> None:
    """Restart through the stable launcher, or let the launcher do it."""
    if not runtime.restart_requested:
        return
    if os.environ.get("MAA_SUPERVISED") == "1":
        raise SystemExit(RESTART_EXIT_CODE)
    launcher = _install_root() / "main.py"
    if not launcher.is_file():
        logger.error("[更新] 找不到稳定引导器 %s，请手动重启。", launcher)
        raise SystemExit(1)
    logger.info("[更新] 正在通过稳定引导器重启 ...")
    try:
        os.execv(
            sys.executable,
            [sys.executable, str(launcher), *sys.argv[1:]],
        )
    except OSError as exc:
        logger.error("[更新] 重启失败，请手动运行 main.py：%s", exc)
        raise SystemExit(1)


async def main() -> None:
    logger.info("OnebotMaaLogAnalyzer 启动中...")
    cfg = load_config()
    if not _validate_ai_config(cfg):
        raise SystemExit(2)

    runtime = BotRuntime(cfg)
    try:
        await runtime.start()
    except Exception as exc:
        logger.error("启动失败：%s", exc)
        await runtime.stop()
        raise SystemExit(1) from exc

    debug_enabled = bool((cfg.get("debug") or {}).get("enabled", False))
    if debug_enabled:
        try:
            from debug.cli import run_cli
        except ImportError:
            logger.error(
                "配置启用了 debug.enabled，但未找到 debug 模块。\n"
                "调试模式仅适用于源码仓库。"
            )
            await runtime.stop()
            raise SystemExit(1)
        try:
            assert runtime.debug_manager is not None
            await run_cli(runtime.debug_manager)
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            await runtime.stop()
            _restart_after_update(runtime)
        return

    try:
        await runtime.supervise()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        await runtime.stop()
        if runtime.restart_requested:
            _restart_after_update(runtime)
        else:
            logger.info("数据已保存，退出。")


def _self_check() -> int:
    """Import the runtime without starting network or platform connections."""
    from maa_analyzer.config import apply_panel_overrides
    from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter

    cfg = default_config()
    errors = validate_common_config(cfg)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    apply_panel_overrides(cfg, {})
    del AstrBotPlatformAdapter
    print(f"OnebotMaaLogAnalyzer self-check ok ({cfg.get('version')})")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(_self_check())
    if "--print-config-template" in sys.argv[1:]:
        _print_config_template()
        raise SystemExit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
