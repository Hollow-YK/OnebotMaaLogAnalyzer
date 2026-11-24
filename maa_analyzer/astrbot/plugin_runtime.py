"""AstrBot-independent lifecycle and event bridge for the plugin shell."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..config import (
    load_effective_config,
    setup_logging,
    teardown_logging,
)
from ..core.archive_names import ui_key_from_file_name
from ..features.maa.commands import CommandHandler
from ..platforms.astrbot.adapter import AstrBotPlatformAdapter
from ..runtime import MaaWorkflowRuntime

logger = logging.getLogger("Maa.AstrBot")


class PluginConfigError(ValueError):
    """The plugin configuration could not be loaded or validated."""


ConfigLoader = Callable[
    [Mapping[str, Any], Path], tuple[dict, Path, Path]
]
AdapterFactory = Callable[[Path], AstrBotPlatformAdapter]
RuntimeFactory = Callable[
    [AstrBotPlatformAdapter, dict, Path, Path], MaaWorkflowRuntime
]


def _effective_config_loader(
    panel: Mapping[str, Any], plugin_data_dir: Path
) -> tuple[dict, Path, Path]:
    return load_effective_config(
        panel, plugin_data_dir=plugin_data_dir
    )


def _adapter_factory(outbox_dir: Path) -> AstrBotPlatformAdapter:
    return AstrBotPlatformAdapter(outbox_dir=outbox_dir)


def _runtime_factory(
    adapter: AstrBotPlatformAdapter,
    cfg: dict,
    data_dir: Path,
    template_root: Path,
) -> MaaWorkflowRuntime:
    return MaaWorkflowRuntime(
        port=adapter,
        cfg=cfg,
        data_dir=data_dir,
        template_root=template_root,
        owns_port=False,
    )


class AstrBotPluginRuntime:
    """Own the analyzer runtime shared by one AstrBot plugin instance."""

    def __init__(
        self,
        panel: Mapping[str, Any] | None,
        *,
        plugin_data_dir: str | Path,
        template_root: str | Path,
        config_loader: ConfigLoader = _effective_config_loader,
        adapter_factory: AdapterFactory = _adapter_factory,
        runtime_factory: RuntimeFactory = _runtime_factory,
    ):
        self.panel = dict(panel or {})
        self.plugin_data_dir = Path(plugin_data_dir).expanduser().resolve()
        self.template_root = Path(template_root).expanduser().resolve()
        self.config_loader = config_loader
        self.adapter_factory = adapter_factory
        self.runtime_factory = runtime_factory

        self.adapter: AstrBotPlatformAdapter | None = None
        self.runtime: MaaWorkflowRuntime | None = None
        self.cfg: dict | None = None
        self.config_path: Path | None = None
        self.data_dir: Path | None = None
        self._state = "new"
        self._lifecycle_lock = asyncio.Lock()
        self._reload_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._state == "running"

    @property
    def service(self):
        return self.runtime.service if self.runtime is not None else None

    async def initialize(self) -> None:
        """Build and start the plugin runtime. Repeated calls are no-ops."""
        async with self._lifecycle_lock:
            if self.running:
                return
            if self._state == "starting":
                return

            self._state = "starting"
            try:
                try:
                    cfg, config_path, data_dir = self._load_config()
                except (ValueError, OSError) as exc:
                    raise PluginConfigError(str(exc)) from exc
                setup_logging(
                    cfg.get("log") or {},
                    isolated=True,
                    base_dir=self.plugin_data_dir,
                )
                adapter = self.adapter_factory(data_dir / "_outbox")
                runtime = self.runtime_factory(
                    adapter, cfg, data_dir, self.template_root
                )
                try:
                    runtime.config_path = config_path
                except Exception:
                    pass
                self.adapter = adapter
                self.runtime = runtime
                await runtime.start()
                self._bind_reload_callbacks(runtime)
                self.cfg = cfg
                self.config_path = config_path
                self.data_dir = data_dir
                self._state = "running"
                logger.info(
                    "AstrBot 插件已启动：配置 %s，数据 %s",
                    config_path,
                    data_dir,
                )
            except BaseException:
                await self._cleanup_resources()
                self._state = "failed"
                raise

    async def terminate(self) -> None:
        """Stop the runtime and close plugin-owned resources idempotently."""
        async with self._lifecycle_lock:
            if self._state in {"new", "stopped"}:
                return
            self._state = "stopping"
            await self._cleanup_resources()
            self._state = "stopped"

    async def handle_event(self, event: Any) -> bool:
        """Map one AstrBot event and report whether this plugin consumed it."""
        adapter = self.adapter
        runtime = self.runtime
        if adapter is None or runtime is None or not runtime.running:
            return False

        event_context = getattr(adapter, "event_context", None)
        with event_context(event) if callable(event_context) else nullcontext():
            bind_bot = getattr(adapter, "bind_bot", None)
            if callable(bind_bot):
                bind_bot(getattr(event, "bot", None))
            mapped = await adapter.map_event(event)
            if mapped is None or runtime.service is None:
                return False
            return bool(await runtime.service.handle_platform_event(mapped))

    def is_relevant_event(self, event: Any) -> bool:
        """
        Synchronously decide whether the plugin should wake for an event.

        AstrBot treats every passing listener filter as a wake signal before the
        handler runs, so this method must mirror dispatch relevance exactly.
        """
        adapter = self.adapter
        runtime = self.runtime
        if (
            adapter is None
            or runtime is None
            or not runtime.running
            or runtime.service is None
        ):
            return False

        try:
            # File notices have no text to classify. They are routed into the
            # service, which applies the listening-group and archive checks.
            if adapter.is_group_upload(event):
                return True
            if not adapter.is_group_message(event):
                return False

            hints = adapter.event_hints(event)
            group_id = str(hints.get("group_id") or "")
            if not group_id:
                logger.debug("忽略群消息：缺少群号")
                return False
            cfg = runtime.service.resolve_config(group_id)
            if cfg is None:
                logger.debug("忽略群消息：群 %s 不在任何监听配置中", group_id)
                return False

            text = str(hints.get("text") or "").strip()
            if (
                text
                and cfg.commands.enabled
                and CommandHandler.match(text, cfg.commands) is not None
            ):
                logger.debug("接收管理指令：群 %s，配置 %s", group_id, cfg.name)
                return True

            if cfg.commands.prefix and text.startswith(cfg.commands.prefix):
                logger.debug("忽略管理指令：群 %s，配置 %s，指令开关 %s，或指令不匹配", group_id, cfg.name, cfg.commands.enabled)

            if cfg.settings.enabled:
                for file_name in hints.get("file_names") or []:
                    if not cfg.project.matches(str(file_name)):
                        continue
                    ui_key = ui_key_from_file_name(str(file_name))
                    if not ui_key or cfg.project.ui_enabled(ui_key):
                        return True

            reply_id = str(hints.get("reply_id") or "").strip()
            if (
                not text
                or not reply_id
                or not cfg.settings.followup_enabled
                or runtime.followup is None
            ):
                return False
            session = runtime.followup.by_message_id(reply_id, cfg.name)
            return (
                session is not None
                and not session.closed
                and session.group_id == group_id
            )
        except Exception as exc:
            logger.debug("判断 AstrBot 事件相关性失败：%s", exc)
            return False

    def _load_config(self) -> tuple[dict, Path, Path]:
        return self.config_loader(self.panel, self.plugin_data_dir)

    def _bind_reload_callbacks(self, runtime: MaaWorkflowRuntime) -> None:
        service = runtime.service
        if service is None:
            raise RuntimeError("分析运行时未创建服务。")
        service.register_reload_loader(self._reload_config)
        service.register_runtime_reload(self._reload_runtime)

    def _reload_config(self, _path: str) -> dict:
        cfg, _, _ = self._load_config()
        return cfg

    async def _reload_runtime(
        self, new_cfg: dict, notify_group: str = ""
    ) -> tuple[bool, str]:
        async with self._reload_lock:
            old_runtime = self.runtime
            if old_runtime is None or self.adapter is None:
                return False, "插件尚未初始化，无法重载。"

            old_cfg = self.cfg
            old_config_path = self.config_path
            old_data_dir = self.data_dir
            try:
                _, new_config_path, new_data_dir = self._load_config()
            except Exception as exc:
                return False, f"配置加载失败：{exc}"

            setup_logging(
                new_cfg.get("log") or {},
                isolated=True,
                base_dir=self.plugin_data_dir,
            )
            await old_runtime.stop()

            replacement: MaaWorkflowRuntime | None = None
            try:
                outbox_dir = new_data_dir / "_outbox"
                self.adapter.outbox_dir = outbox_dir
                clear_role_cache = getattr(self.adapter, "clear_role_cache", None)
                if callable(clear_role_cache):
                    clear_role_cache()
                replacement = self.runtime_factory(
                    self.adapter,
                    new_cfg,
                    new_data_dir,
                    self.template_root,
                )
                try:
                    replacement.config_path = new_config_path
                except Exception:
                    pass
                await replacement.start()
                self._bind_reload_callbacks(replacement)
                self.runtime = replacement
                self.cfg = new_cfg
                self.config_path = new_config_path
                self.data_dir = new_data_dir
            except Exception as exc:
                logger.exception("[重载] 新运行时启动失败，正在回退")
                if replacement is not None:
                    await replacement.stop(force=True)
                self.runtime = old_runtime
                self.cfg = old_cfg
                self.config_path = old_config_path
                self.data_dir = old_data_dir
                if old_data_dir is not None:
                    self.adapter.outbox_dir = old_data_dir / "_outbox"
                try:
                    await old_runtime.start()
                    self._bind_reload_callbacks(old_runtime)
                    self._state = "running"
                except Exception as rollback_exc:
                    logger.exception("[重载] 旧运行时回退失败")
                    self._state = "failed"
                    return (
                        False,
                        f"重载失败：{exc}；回退也失败：{rollback_exc}",
                    )
                setup_logging(
                    (old_cfg or {}).get("log") or {},
                    isolated=True,
                    base_dir=self.plugin_data_dir,
                )
                return False, f"重载失败，已回退到旧配置：{exc}"

            self._state = "running"
            await self._notify(
                notify_group,
                "重载完成：配置、日志文件、LLM 与平台会话均已按新设置生效。",
            )
            return True, "已重新加载并重建运行时。"

    async def _notify(self, group_id: str, text: str) -> None:
        service = self.service
        if not group_id or service is None:
            return
        try:
            await service.send_text(int(group_id), text)
        except Exception as exc:
            logger.warning("[重载] 发送结果消息失败：%s", exc)

    async def _cleanup_resources(self) -> None:
        runtime, self.runtime = self.runtime, None
        adapter, self.adapter = self.adapter, None
        if runtime is not None:
            try:
                await runtime.stop(force=True)
            except Exception:
                logger.exception("停止分析运行时失败")
        if adapter is not None:
            try:
                await adapter.close()
            except Exception:
                logger.exception("关闭 AstrBot 平台适配器失败")
        teardown_logging(isolated=True)
        self.cfg = None
        self.config_path = None
        self.data_dir = None
