"""AstrBot AIOCQHTTP plugin shell for the shared Maa log analyzer."""
from __future__ import annotations

import logging
import importlib
import os
import sys
import weakref
from pathlib import Path
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

logger = logging.getLogger("Maa.AstrBot")
PLUGIN_NAME = "OnebotMaaLogAnalyzer"


class _AstrBotLogHandler(logging.Handler):
    """Forward shared kernel logs to AstrBot's logger."""

    _maa_astrbot_bridge = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = ("error" if record.levelno >= logging.ERROR else
                     "warning" if record.levelno >= logging.WARNING else
                     "info" if record.levelno >= logging.INFO else "debug")
            getattr(astrbot_logger, level)("[%s] %s" % (record.name, self.format(record)))
        except Exception:
            self.handleError(record)


def _locate_kernel_root() -> Path | None:
    """Find a source checkout or packaged plugin root with ``maa_analyzer``."""
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if (candidate / "maa_analyzer" / "__init__.py").is_file():
            return candidate
    return None


_KERNEL_ROOT = _locate_kernel_root()
_KERNEL_IMPORT_ERROR: BaseException | None = None
if _KERNEL_ROOT is not None and _KERNEL_ROOT != Path(__file__).resolve().parent:
    kernel_path = str(_KERNEL_ROOT)
    if kernel_path not in sys.path:
        sys.path.insert(0, kernel_path)

try:
    if _KERNEL_ROOT == Path(__file__).resolve().parent and __package__:
        runtime_module = importlib.import_module(
            ".maa_analyzer.astrbot.plugin_runtime", package=__package__
        )
        config_module = importlib.import_module(".maa_analyzer.config", package=__package__)
    else:
        runtime_module = importlib.import_module("maa_analyzer.astrbot.plugin_runtime")
        config_module = importlib.import_module("maa_analyzer.config")
    AstrBotPluginRuntime = runtime_module.AstrBotPluginRuntime
except ImportError as exc:  # Reported from initialize() without breaking discovery.
    AstrBotPluginRuntime = None  # type: ignore[assignment,misc]
    _KERNEL_IMPORT_ERROR = exc


_ACTIVE_RUNTIMES: weakref.WeakSet[Any] = weakref.WeakSet()


class MaaEventFilter(filter.CustomFilter):
    """Wake AstrBot only for events owned by an active analyzer runtime."""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        for runtime in tuple(_ACTIVE_RUNTIMES):
            checker = getattr(runtime, "is_relevant_event", None)
            if not callable(checker):
                continue
            try:
                if checker(event):
                    return True
            except Exception:
                logger.exception("判断 OnebotMaaLogAnalyzer 事件相关性失败")
        return False


class Main(Star):
    """Register the shared analyzer as an AstrBot AIOCQHTTP plugin."""

    def __init__(
        self,
        context: Context,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self._panel_config = config
        # Migrate legacy flat fields into grouped objects and keep the shared
        # runtime on its established flat key model.
        if config is not None and _KERNEL_IMPORT_ERROR is None:
            runtime_config, changed = config_module.synchronize_panel_config(config)
            save = getattr(config, "save_config", None)
            if changed and callable(save):
                save()
            self.config = runtime_config
        elif config is not None:
            self.config = dict(config)
        else:
            self.config = {}
        self.plugin_data_dir: Path | None = None
        self.plugin_runtime: Any | None = None
        self._log_handler: logging.Handler | None = None

    async def initialize(self) -> None:
        """Start the shared analyzer once for this plugin instance."""
        if self._log_handler is None:
            self._log_handler = _AstrBotLogHandler()
            logging.getLogger("Maa").addHandler(self._log_handler)
        if self.plugin_runtime is not None:
            await self._start_runtime()
            return
        if _KERNEL_IMPORT_ERROR is not None or AstrBotPluginRuntime is None:
            raise RuntimeError(
                "无法导入 maa_analyzer，请检查插件包及依赖。"
                f"原始错误：{_KERNEL_IMPORT_ERROR}"
            ) from _KERNEL_IMPORT_ERROR
        if _KERNEL_ROOT is None:
            raise RuntimeError(
                "无法定位 maa_analyzer。请重新安装 AstrBot 插件包。"
            )

        self.plugin_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        template_root = Path(
            os.environ.get("MAA_INSTALL_ROOT") or _KERNEL_ROOT
        ) / "templates"
        self.plugin_runtime = AstrBotPluginRuntime(
            self.config,
            plugin_data_dir=self.plugin_data_dir,
            template_root=template_root,
        )
        await self._start_runtime()

    async def _start_runtime(self) -> None:
        runtime = self.plugin_runtime
        if runtime is None:
            raise RuntimeError("插件运行时尚未创建")
        try:
            await runtime.initialize()
        except (ValueError, OSError) as exc:
            logger.error("插件配置无效，分析功能未启动；请在插件面板修正配置并重新加载：%s", exc)
            return
        if self.config.get("_persist_llm_migration") or self.config.get("_persist_endpoint_migration"):
            section = self._panel_config.get("llm") if self._panel_config is not None else None
            if isinstance(section, dict):
                old_flags = {key: section.get(key) for key in
                             ("llm_migration_pending", "llm_endpoints_migration_pending")}
                if self.config.get("_persist_llm_migration"):
                    section["llm_migration_pending"] = False
                if self.config.get("_persist_endpoint_migration"):
                    section["llm_endpoints_migration_pending"] = False
                save = getattr(self._panel_config, "save_config", None)
                if callable(save):
                    try:
                        save()
                    except Exception:
                        section.update(old_flags)
                        raise
            self.config.pop("_persist_llm_migration", None)
            self.config.pop("_persist_endpoint_migration", None)
            runtime.panel.pop("_persist_llm_migration", None)
            runtime.panel.pop("_persist_endpoint_migration", None)
        _ACTIVE_RUNTIMES.add(runtime)
        try:
            from maa_analyzer.update.boot import confirm_boot

            confirm_boot()
        except Exception:
            logger.debug("启动确认失败", exc_info=True)

    async def terminate(self) -> None:
        """Stop background work and release plugin-owned resources."""
        runtime, self.plugin_runtime = self.plugin_runtime, None
        try:
            if runtime is not None:
                _ACTIVE_RUNTIMES.discard(runtime)
                await runtime.terminate()
        finally:
            if runtime is not None:
                _ACTIVE_RUNTIMES.discard(runtime)
            if self._log_handler is not None:
                logging.getLogger("Maa").removeHandler(self._log_handler)
                self._log_handler.close()
                self._log_handler = None

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.custom_filter(MaaEventFilter)
    async def on_aiocqhttp_event(self, event: AstrMessageEvent) -> None:
        """Dispatch one AIOCQHTTP event through the shared analyzer."""
        runtime = self.plugin_runtime
        if runtime is None:
            return
        try:
            consumed = await runtime.handle_event(event)
        except Exception:
            logger.exception("处理 AstrBot AIOCQHTTP 事件失败")
            return
        if consumed:
            event.stop_event()
