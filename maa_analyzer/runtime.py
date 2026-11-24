"""Shared analyzer workflow used by the bare OneBot and AstrBot entries."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .config import validate_common_config
from .core.ai_usage import UsageStore
from .core.data_manager import DataManager
from .core.llm_pool import LLMPool
from .core.service import MaaService
from .features.maa.analyzer import MaaAnalyzer
from .features.maa.followup import FollowupStore
from .features.maa.history import HistoryStore
from .features.maa.message_handler import MessageHandler
from .features.maa.watcher import LogWatcher
from .platform import PlatformPort
from .update.install import detect_install
from .update.manager import UpdateManager

logger = logging.getLogger("Maa.Runtime")


class MaaWorkflowRuntime:
    """Own the platform-neutral analyzer workflow and its background tasks."""

    def __init__(
        self,
        *,
        port: PlatformPort,
        cfg: Mapping[str, Any],
        data_dir: str | Path,
        template_root: str | Path | None = None,
        llm: Any | None = None,
        owns_port: bool = False,
        owns_llm: bool | None = None,
        background_services: bool = True,
        restart_callback: Callable[[], None] | None = None,
        config_path: str | Path | None = None,
    ):
        errors = validate_common_config(cfg)
        if errors:
            raise ValueError("\n".join(errors))
        self.port = port
        self.cfg = dict(cfg)
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.template_root = (
            Path(template_root).expanduser().resolve()
            if template_root
            else Path(__file__).resolve().parent.parent / "templates"
        )
        self._provided_llm = llm
        self.owns_port = bool(owns_port)
        self.owns_llm = bool(owns_llm)
        self.background_services = bool(background_services)
        self.restart_callback = restart_callback
        self.config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )

        self.dm: DataManager | None = None
        self.llm: Any | None = None
        self.service: MaaService | None = None
        self.history: HistoryStore | None = None
        self.analyzer: MaaAnalyzer | None = None
        self.followup: FollowupStore | None = None
        self.watcher: LogWatcher | None = None
        self.messages: MessageHandler | None = None
        self.updater: UpdateManager | None = None
        self._tasks: list[asyncio.Task] = []
        self._state = "new"

    @property
    def running(self) -> bool:
        return self._state == "running"

    async def start(self) -> "MaaWorkflowRuntime":
        """Build and start the shared workflow. Repeated calls are no-ops."""
        if self.running:
            return self
        if self._state == "running":
            return self

        self._state = "starting"
        try:
            self.dm = DataManager(str(self.data_dir))
            self.llm = self._provided_llm
            if self.llm is None:
                self.llm = LLMPool(
                    self.cfg.get("llm") or {},
                    usage=UsageStore(self.dm._dir),
                )
                self.owns_llm = True

            configured = getattr(self.llm, "configured", True)
            if not configured:
                logger.warning(
                    "未配置可用的 llm.providers[]，AI 分析将无法工作。"
                )
            describe_all = getattr(self.llm, "describe_all", None)
            if callable(describe_all):
                for line in describe_all():
                    logger.info("[LLM] %s", line)

            self.service = MaaService(port=self.port, dm=self.dm)
            self.service.load()
            bot_cfg = self.cfg.get("bot") or {}
            self.service.sync_from_config(bot_cfg.get("configs") or {})
            self._log_config_summary()

            self.updater = UpdateManager(
                data_dir=self.dm._dir,
                cfg=self.cfg.get("update") or {},
                install=detect_install(),
                config_path=self.config_path,
                service=self.service,
                restart_callback=self.restart_callback,
                background=self.background_services,
            )

            self.history = HistoryStore(self.dm._dir)
            self.analyzer = MaaAnalyzer(
                self.service,
                self.llm,
                history=self.history,
                template_root=self.template_root,
            )
            self.followup = FollowupStore(history=self.history)
            self.watcher = LogWatcher(
                self.service,
                self.analyzer,
                self.followup,
                history=self.history,
            )
            self.messages = MessageHandler(
                self.service,
                self.analyzer,
                self.followup,
                history=self.history,
                segment_sender=self.watcher.send_segments,
                updater=self.updater,
            )

            self.service.register_event(
                "notice.group_upload", self.watcher.on_group_upload
            )
            self.service.register_event(
                "message.file", self.watcher.on_message_file
            )
            self.service.register_event(
                "message.text", self.messages.on_message_text
            )

            self._tasks = []
            if self.background_services:
                self._tasks = [
                    asyncio.create_task(self._detect_backend()),
                    asyncio.create_task(
                        self._history_cleanup_loop(self.history, self.service)
                    ),
                ]
                self._tasks.extend(self.analyzer.start_repo_preloads())
                await self.updater.start()
            self._state = "running"
            return self
        except BaseException:
            await self.stop(force=True)
            raise

    async def stop(self, *, force: bool = False) -> None:
        """Stop tasks and close only resources owned by this runtime."""
        if self._state in {"new", "stopped"} and not force:
            return

        self._state = "stopping"
        if self.watcher is not None:
            self.watcher.cancel_pending()
        updater, self.updater = self.updater, None
        if updater is not None:
            try:
                await updater.stop()
            except Exception as exc:
                logger.warning("停止更新检查失败：%s", exc)

        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.service is not None:
            try:
                self.service.save()
            except Exception as exc:
                logger.warning("保存配置失败：%s", exc)
        if self.owns_llm and self.llm is not None:
            try:
                close = getattr(self.llm, "close", None)
                if callable(close):
                    await close()
            except Exception as exc:
                logger.warning("关闭 LLM 连接池失败：%s", exc)
        try:
            from .features.maa.render import close_all

            close_all()
        except Exception as exc:
            logger.warning("关闭渲染浏览器失败：%s", exc)
        if self.owns_port:
            try:
                await self.port.close()
            except Exception as exc:
                logger.warning("关闭平台适配器失败：%s", exc)
        self._state = "stopped"

    def _log_config_summary(self) -> None:
        if self.service is None:
            return
        if not self.service.configs:
            logger.warning(
                "未配置任何监听群，请设置 bot.configs.<名称>.listen_groups。"
            )
            return
        for name, state in self.service.configs.items():
            groups = ", ".join(sorted(state.info.listen_groups)) or "（无）"
            notify = state.info.notify_group or "（未设置）"
            candidates = " -> ".join(
                f"{ref.name}/{ref.model}" for ref in state.llm.models
            ) or "（未声明）"
            logger.info(
                "配置「%s」：监听群 %s，通知群 %s，模型 %s",
                name,
                groups,
                notify,
                candidates,
            )

    async def _detect_backend(self) -> None:
        await asyncio.sleep(3)
        if self.service is None:
            return
        try:
            await self.service.detect_backend()
        except Exception as exc:
            logger.warning("协议端探测失败：%s", exc)

    async def _history_cleanup_loop(
        self,
        history: HistoryStore,
        service: MaaService,
        interval_seconds: int = 3600,
    ) -> None:
        """Periodically clean history without touching repository caches."""
        while True:
            try:
                await asyncio.sleep(max(300, int(interval_seconds or 3600)))
                results = await history.cleanup_all_async(
                    {
                        name: state.settings
                        for name, state in service.configs.items()
                    }
                )
                await history.cleanup_staging_async()
                for name, result in results.items():
                    if result.removed:
                        logger.info(
                            "[历史] 配置「%s」清理 %s 条记录，释放 %s 字节（%s）",
                            name,
                            result.removed,
                            result.freed_bytes,
                            result.reason,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[历史] 周期清理失败：%s", exc)
