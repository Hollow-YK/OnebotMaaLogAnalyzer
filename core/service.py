"""
核心服务 — Bot 能力 API + 配置管理 + 事件分发。

功能模块的唯一依赖入口 —— 不包含任何业务逻辑。

职责:
  - Bot 能力 API: send_message() / send_image() / 群文件查询 / 下载
  - 配置管理: 多配置加载与持久化、监听群解析、设置合并
  - 任务记录: add_job() / trim_jobs()
  - 事件分发: handle_message() / handle_notice() / handle_request()
"""
from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from .data_manager import DataManager
from .models import (
    AnalysisSettings,
    CommandConfig,
    ConfigState,
    JobRecord,
)

if TYPE_CHECKING:
    from bot.api import OneBotAPI

logger = logging.getLogger("Maa.Service")

# 事件监听器签名
EventListener = Callable[[dict], Awaitable[None]]


def _normalize_keys(model_cls, data: dict) -> dict:
    """
    把 dict 的键统一为模型的 snake_case 字段名。

    同时接受 snake_case 与 camelCase（含 pydantic alias），
    无法识别的键会被丢弃（视为无效配置项）。
    """
    if not isinstance(data, dict):
        return {}
    fields = model_cls.model_fields
    lookup: dict[str, str] = {}
    for name, info in fields.items():
        lookup[name.lower()] = name
        alias = getattr(info, "alias", None)
        if alias:
            lookup[str(alias).lower()] = name
    result: dict = {}
    for key, value in data.items():
        field_name = lookup.get(str(key).lower())
        if field_name is None:
            continue
        # 嵌套模型递归规范化，避免 camelCase 子字段被丢弃
        sub = fields[field_name].annotation
        if isinstance(value, dict) and isinstance(sub, type) and hasattr(sub, "model_fields"):
            value = _normalize_keys(sub, value)
        result[field_name] = value
    return result


def _deep_merge(base: dict, patch: dict) -> dict:
    """递归合并 dict，patch 中的嵌套 dict 与 base 合并而非整体替换。"""
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


class MaaService:
    """核心服务 — 提供 Bot 能力 API + 配置管理 + 事件分发。"""

    def __init__(self, api: "OneBotAPI", dm: DataManager,
                 global_settings: Optional[AnalysisSettings] = None):
        self._api = api
        self.dm = dm

        # ── 多配置运行时数据 ──
        self.configs: dict[str, ConfigState] = {}
        self.global_settings: AnalysisSettings = global_settings or AnalysisSettings()

        # ── 协议端探测 ──
        self.backend: str = "unknown"  # napcat / llonebot / unknown

        # ── 事件监听表 ──
        self._event_listeners: dict[str, list[EventListener]] = {}

    # ════════════════════════════════════════════════════════════
    # 事件注册
    # ════════════════════════════════════════════════════════════

    def register_event(self, event_type: str, handler: EventListener) -> None:
        """
        注册事件监听器。
        - event_type: "notice.group_upload", "message.file" 等
        - handler:    接收原始 event dict，无返回值
        """
        self._event_listeners.setdefault(event_type, []).append(handler)

    # ════════════════════════════════════════════════════════════
    # Bot 能力 API — 消息发送
    # ════════════════════════════════════════════════════════════

    async def send_message(self, group_id: int,
                           message: str | list[dict]) -> Optional[int]:
        """
        发送群聊消息，返回 message_id（失败或空消息返回 None）。

        str 自动转 text 段，list 直接作为消息段数组发送。
        message_id 用于后续「引用回复」定位追问会话。
        """
        if isinstance(message, str):
            if not message:
                return None
            message = [{"type": "text", "data": {"text": message}}]
        if not message:
            return None
        return await self._api.send_group_msg(group_id, message)

    async def send_text(self, group_id: int, text: str) -> Optional[int]:
        """发送纯文本群消息，返回 message_id。"""
        return await self.send_message(group_id, text)

    async def send_image(self, group_id: int, image_bytes: bytes) -> Optional[int]:
        """发送图片消息（base64 编码，OneBot v11 数组格式 image 段）。"""
        return await self.send_message(group_id, [
            {"type": "image", "data": {
                "file": f"base64://{base64.b64encode(image_bytes).decode()}"
            }}
        ])

    async def send_file(self, group_id: int, file_name: str,
                        data: bytes) -> bool:
        """
        以群文件形式发送任意文件。

        优先写临时文件后走 upload_group_file（兼容性最好）；
        失败时退回 base64 形式。返回是否成功。

        注意：**不能在 API 返回后立刻删除临时文件**。部分协议端
        （NapCat / SnowLuma 等）会在返回后才异步读取该路径，过早删除
        会导致 "ENOENT: no such file or directory" —— 表现为日志报错、
        但文件其实已经发出去。因此这里保留文件，由下次发送时统一清理。
        """
        name = Path(str(file_name or "file.bin")).name or "file.bin"
        temp_dir = Path(self.dm._dir) / "_outbox"
        target: Optional[Path] = None
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            # 文件名加时间戳，避免同名并发覆盖
            target = temp_dir / f"{int(time.time() * 1000)}_{name}"
            target.write_bytes(data)
        except OSError as exc:
            logger.warning(f"[发送] 写临时文件失败，改用 base64：{exc}")
            target = None

        if target is not None:
            try:
                result = await self._api.upload_group_file(
                    group_id, str(target.resolve()), name
                )
                if result is not None:
                    self._sweep_outbox(temp_dir)
                    return True
                logger.warning("[发送] upload_group_file 返回空，尝试 base64")
            except Exception as exc:
                logger.warning(f"[发送] upload_group_file 失败，尝试 base64：{exc}")

        try:
            result = await self._api.upload_group_file(
                group_id,
                f"base64://{base64.b64encode(data).decode()}",
                name,
            )
            return result is not None
        except Exception as exc:
            logger.warning(f"[发送] 发送文件失败（{name}）：{exc}")
            return False

    def _sweep_outbox(self, temp_dir: Path, keep_seconds: int = 600) -> None:
        """
        清理 _outbox 中的旧临时文件。

        只删除超过 keep_seconds 的文件，给协议端留出异步读取的时间窗口。
        """
        try:
            cutoff = time.time() - max(60, int(keep_seconds))
            for item in temp_dir.iterdir():
                try:
                    if item.is_file() and item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    # ════════════════════════════════════════════════════════════
    # Bot 能力 API — 群文件
    # ════════════════════════════════════════════════════════════

    async def get_group_root_files(self, group_id: int,
                                   file_count: Optional[int] = None) -> dict:
        """获取群根目录文件列表，返回 {"files": [...], "folders": [...]}。"""
        if file_count is None:
            data = await self._api.get_group_root_files(group_id)
        else:
            data = await self._api.get_group_root_files(group_id, file_count=file_count)
        return data if isinstance(data, dict) else {}

    async def get_group_files_by_folder(self, group_id: int, folder_id: str,
                                        file_count: Optional[int] = None) -> dict:
        """获取群子目录文件列表。"""
        if file_count is None:
            data = await self._api.get_group_files_by_folder(group_id, folder_id)
        else:
            data = await self._api.get_group_files_by_folder(
                group_id, folder_id, file_count=file_count
            )
        return data if isinstance(data, dict) else {}

    async def get_group_file_url(self, group_id: int, file_id: str) -> Optional[str]:
        """获取群文件下载链接。"""
        return await self._api.get_group_file_url(group_id, file_id)

    async def download_url(self, url: str, target_path, *,
                           max_bytes: int, timeout_seconds: int = 120) -> int:
        """下载 URL 到本地文件，返回字节数。"""
        return await self._api.download_url(
            url, target_path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )

    async def get_version_info(self) -> dict:
        """获取协议端版本信息。"""
        data = await self._api.get_version_info()
        return data if isinstance(data, dict) else {}

    async def detect_backend(self) -> str:
        """探测协议端实现（LLOneBot / NapCat），结果缓存在 self.backend。"""
        info = await self.get_version_info()
        app_name = str(info.get("app_name") or info.get("appName") or "")
        lowered = app_name.lower()
        if "llonebot" in lowered:
            self.backend = "llonebot"
        elif "napcat" in lowered:
            self.backend = "napcat"
        else:
            self.backend = "unknown"
        logger.info(f"协议端探测：app_name={app_name or 'unknown'}, backend={self.backend}")
        return self.backend

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    def load(self):
        """加载所有配置和运行时数据。"""
        self.dm.check_all()
        self.configs.clear()
        self.global_settings = self.dm.load_global_settings()

        for name in self.dm.list_configs():
            info = self.dm.load_config_info(name)
            settings = self._merge_settings(name)
            commands = self.dm.load_config_commands(name)
            jobs = self.dm.load_config_jobs(name)
            max_id = max((j.id for j in jobs), default=0)

            self.configs[name] = ConfigState(
                name=name,
                info=info,
                settings=settings,
                commands=commands,
                jobs=jobs,
                next_job_id=max_id + 1,
            )

        total = sum(len(c.info.listen_groups) for c in self.configs.values())
        logger.info(f"已加载: {len(self.configs)} 配置, 共监听 {total} 个群")

    def _merge_settings(self, name: str) -> AnalysisSettings:
        """配置级 settings.json 覆盖全局设置（仅覆盖显式设置的字段）。"""
        overrides = self.dm.load_config_settings(name)
        if not overrides:
            return self.global_settings.model_copy(deep=True)
        return self.apply_settings_overrides(self.global_settings, overrides, name)

    @staticmethod
    def apply_settings_overrides(base: AnalysisSettings, overrides: dict,
                                 label: str = "") -> AnalysisSettings:
        """
        在 base 之上应用部分覆盖，并做完整校验。

        - 键名同时接受 snake_case 与 camelCase（如 file_prefix / filePrefix）
        - 嵌套模型（如 repo）做深合并，未覆盖字段沿用 base 的值
        - 校验失败时回退到 base，绝不因配置错误导致启动失败
        """
        if not isinstance(overrides, dict) or not overrides:
            return base.model_copy(deep=True)
        try:
            normalized = _normalize_keys(AnalysisSettings, overrides)
            if not normalized:
                return base.model_copy(deep=True)
            merged = _deep_merge(base.model_dump(), normalized)
            return AnalysisSettings.model_validate(merged)
        except Exception as exc:
            where = f"[{label}] " if label else ""
            logger.warning(f"{where}settings 覆盖无效，使用原设置：{exc}")
            return base.model_copy(deep=True)

    def sync_from_config(self, configs_cfg: dict) -> None:
        """
        用 config.json → bot.configs 同步监听群与通知群。

        config.json 是群组关系的声明式来源（便于部署时直接编辑）；
        数据目录中的配置用于保存分析设置与任务记录。
        """
        if not isinstance(configs_cfg, dict):
            return

        for name, spec in configs_cfg.items():
            if not isinstance(spec, dict):
                continue

            listen = {
                str(g).strip()
                for g in (spec.get("listen_groups") or [])
                if str(g).strip()
            }
            notify_raw = spec.get("notify_group")
            notify = str(notify_raw).strip() if notify_raw else None

            state = self.configs.get(name)
            if state is None:
                state = ConfigState(name=name)
                self.configs[name] = state

            state.info.listen_groups = listen
            state.info.notify_group = notify or None
            self.dm.save_config_info(name, state.info)

            # 配置级分析设置覆盖（可选）
            overrides = spec.get("settings")
            if isinstance(overrides, dict) and overrides:
                merged = self.apply_settings_overrides(state.settings, overrides, name)
                if merged is not state.settings:
                    state.settings = merged
                    self.dm.save_config_settings(name, state.settings)

            # 指令配置（可选）
            commands_spec = spec.get("commands")
            if isinstance(commands_spec, dict) and commands_spec:
                state.commands = self.apply_commands_overrides(state.commands, commands_spec, name)
                self.dm.save_config_commands(name, state.commands)

    @staticmethod
    def apply_commands_overrides(base: CommandConfig, overrides: dict,
                                 label: str = "") -> CommandConfig:
        """应用指令配置覆盖，键名兼容 snake_case 与 camelCase。"""
        if not isinstance(overrides, dict) or not overrides:
            return base.model_copy(deep=True)
        try:
            normalized = _normalize_keys(CommandConfig, overrides)
            if not normalized:
                return base.model_copy(deep=True)
            merged = {**base.model_dump(), **normalized}
            return CommandConfig.model_validate(merged)
        except Exception as exc:
            where = f"[{label}] " if label else ""
            logger.warning(f"{where}commands 覆盖无效，使用原设置：{exc}")
            return base.model_copy(deep=True)

    def save(self):
        """持久化所有配置。"""
        self.dm.save_global_settings(self.global_settings)
        for name, state in self.configs.items():
            self.dm.save_config(name, state)

    def save_config(self, name: str):
        """持久化单个配置。"""
        state = self.configs.get(name)
        if state is not None:
            self.dm.save_config(name, state)

    def reload_from_file(self, path: str = "config.json") -> tuple[bool, str]:
        """
        热重载 config.json 中的群组与设置。

        保留数据目录中的任务记录与运行时状态，仅重新同步声明式配置。
        """
        import json as _json
        from pathlib import Path as _Path

        target = _Path(path)
        if not target.exists():
            return False, f"未找到配置文件：{path}"
        try:
            cfg = _json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"配置文件解析失败：{exc}"

        configs_cfg = (cfg.get("bot") or {}).get("configs")
        if not isinstance(configs_cfg, dict) or not configs_cfg:
            return False, "config.json 中 bot.configs 为空，未做任何变更。"

        self.sync_from_config(configs_cfg)
        total = sum(len(c.info.listen_groups) for c in self.configs.values())
        logger.info(f"[重载] 已重新加载 config.json：{len(self.configs)} 配置 / {total} 个群")
        return True, f"已重新加载：{len(self.configs)} 个配置，共监听 {total} 个群。"

    # ════════════════════════════════════════════════════════════
    # 配置查找
    # ════════════════════════════════════════════════════════════

    def find_configs(self, group_id: str) -> list[ConfigState]:
        """查找监听此群的所有配置。"""
        gid = str(group_id)
        return [
            cfg for cfg in self.configs.values()
            if gid in cfg.info.listen_groups or cfg.info.notify_group == gid
        ]

    def resolve_config(self, group_id: str) -> Optional[ConfigState]:
        """
        解析当前群生效的配置。
        本 Bot 要求一个群只属于一个配置（否则无法确定用哪套设置）。
        """
        configs = self.find_configs(group_id)
        if len(configs) == 1:
            return configs[0]
        return None

    def is_listened(self, group_id: str) -> bool:
        """判断群是否被监听（可能属于多个配置，用于决定是否响应）。"""
        return bool(self.find_configs(group_id))

    # ════════════════════════════════════════════════════════════
    # 事件分发
    # ════════════════════════════════════════════════════════════

    async def handle_message(self, event: dict):
        """
        处理群消息事件。

        - 含文件段 → 分发给 message.file（日志分析路径）
        - 文本消息 → 若注册了 message.text 监听器则分发（指令 / 追问答疑）
        """
        if event.get("message_type") != "group":
            return

        file_segments = self._extract_file_segments(event)
        if file_segments:
            enriched = dict(event)
            enriched["_file_segments"] = file_segments
            await self._dispatch_event("message.file", enriched)

        text = self._extract_text(event)
        if text or self._extract_reply_id(event):
            enriched = dict(event)
            enriched["_text"] = text
            enriched["_reply_id"] = self._extract_reply_id(event)
            await self._dispatch_event("message.text", enriched)

    async def handle_notice(self, event: dict):
        """处理通知事件 → 分发给注册的事件监听器。"""
        notice_type = event.get("notice_type", "")
        if not notice_type:
            return
        await self._dispatch_event(f"notice.{notice_type}", event)

    async def handle_request(self, event: dict):
        """处理请求事件 → 分发给注册的事件监听器。"""
        request_type = event.get("request_type", "")
        sub_type = event.get("sub_type", "")
        if request_type and sub_type:
            await self._dispatch_event(f"request.{request_type}_{sub_type}", event)

    async def _dispatch_event(self, event_key: str, event: dict):
        """分发事件到指定 key 的所有监听器。"""
        for listener in self._event_listeners.get(event_key, []):
            try:
                await listener(event)
            except Exception:
                logger.exception(f"事件监听器异常: {event_key}")

    # ════════════════════════════════════════════════════════════
    # 共享工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_reply_id(event: dict) -> str:
        """
        提取消息引用的目标 message_id（引用回复）。

        兼容 message 数组中的 reply 段与 raw_message 中的 CQ 码。
        无引用时返回空字符串。
        """
        message = event.get("message")
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict) or seg.get("type") != "reply":
                    continue
                data = seg.get("data")
                if isinstance(data, dict):
                    target = data.get("id") or data.get("message_id")
                    if target is not None:
                        return str(target).strip()
        raw = event.get("raw_message")
        if isinstance(raw, str):
            match = re.search(r"\[CQ:reply,[^\]]*?id=([^,\]]+)", raw)
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _extract_text(event: dict) -> str:
        """提取消息中的纯文本（兼容 message 数组与 raw_message）。"""
        parts: list[str] = []
        message = event.get("message")
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict) or seg.get("type") != "text":
                    continue
                data = seg.get("data")
                if isinstance(data, dict):
                    text = data.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        if parts:
            return "".join(parts).strip()
        raw = event.get("raw_message")
        if isinstance(raw, str):
            # 去掉 CQ 码，只留可见文本
            return re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
        return ""

    @staticmethod
    def _extract_file_segments(event: dict) -> list[dict]:
        """提取消息中的文件段（file 类型）。"""
        result: list[dict] = []
        message = event.get("message")
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "file":
                    data = seg.get("data")
                    if isinstance(data, dict):
                        result.append(dict(data))
        raw = event.get("raw_message")
        if isinstance(raw, str):
            for m in re.finditer(r"\[CQ:file,([^\]]+)\]", raw):
                kv: dict[str, str] = {}
                for pair in m.group(1).split(","):
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        kv[key.strip()] = value.strip()
                if kv:
                    result.append(kv)
        return result

    # ════════════════════════════════════════════════════════════
    # 任务记录
    # ════════════════════════════════════════════════════════════

    def add_job(self, cfg_name: str, **fields) -> JobRecord:
        """创建分析任务记录。"""
        cfg = self.configs[cfg_name]
        job = JobRecord(id=cfg.next_job_id, **fields)
        cfg.next_job_id += 1
        cfg.jobs.append(job)
        return job

    def trim_jobs(self, cfg_name: str, keep: int = 200):
        """裁剪任务记录，避免无限增长。"""
        cfg = self.configs[cfg_name]
        if len(cfg.jobs) > keep:
            cfg.jobs = cfg.jobs[-keep:]
