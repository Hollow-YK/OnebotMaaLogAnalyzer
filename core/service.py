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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .data_manager import DataManager
from .models import (
    AnalysisSettings,
    CommandConfig,
    ConfigLLM,
    ConfigState,
    JobRecord,
    ModelRef,
    ProjectConfig,
    ProjectUISettings,
    ReportConfig,
    RepoConfig,
    VisionConfig,
    normalize_ui_key,
)

if TYPE_CHECKING:
    from bot.api import OneBotAPI

logger = logging.getLogger("Maa.Service")

# 事件监听器签名
EventListener = Callable[[dict], Awaitable[None]]

# 运行时重建回调：async (new_cfg, notify_group) -> (ok, message)
ReloadHandler = Callable[[dict, str], Awaitable[tuple[bool, str]]]

# 与 main.MODES 保持一致（避免 core 反向依赖 main）
ONEBOT_MODES = ("ws", "http_ws", "ws_reverse", "http_ws_reverse")


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


def _normalize_project(data: dict) -> dict:
    """
    归一化 `project` 段：顶层键、ui.<名称> 与 repo 子字段都兼容两种命名。

    `_normalize_keys` 不会递归 dict 值与 Optional[BaseModel]，这里补上。
    """
    if not isinstance(data, dict):
        return {}
    result = _normalize_keys(ProjectConfig, data)

    ui_raw = result.get("ui")
    if isinstance(ui_raw, dict):
        ui: dict[str, dict] = {}
        for key, value in ui_raw.items():
            if not isinstance(value, dict):
                continue
            ui[normalize_ui_key(key)] = _normalize_keys(ProjectUISettings, value)
        result["ui"] = ui

    repo_raw = result.get("repo")
    if isinstance(repo_raw, dict):
        result["repo"] = _normalize_keys(RepoConfig, repo_raw)
    return result


def _normalize_config_llm(data: dict) -> dict:
    """归一化配置级 `llm` 段（models[] 是列表，需要逐项处理）。"""
    if not isinstance(data, dict):
        return {}
    result = _normalize_keys(ConfigLLM, data)
    models = result.get("models")
    if isinstance(models, list):
        result["models"] = [
            _normalize_keys(ModelRef, item)
            for item in models if isinstance(item, dict)
        ]
    return result


def _normalize_simple(model_cls, data: dict) -> dict:
    """归一化一个直接由 BaseModel 表达的配置段。"""
    if not isinstance(data, dict):
        return {}
    return _normalize_keys(model_cls, data)


def _build_model(model_cls, data: dict, label: str = ""):
    """校验并构造配置模型；失败时退回默认值。"""
    normalized = _normalize_simple(model_cls, data)
    if not normalized:
        return model_cls()
    try:
        return model_cls.model_validate(normalized)
    except Exception as exc:
        where = f"[{label}] " if label else ""
        logger.warning(f"{where}{model_cls.__name__} 配置无效，使用默认值：{exc}")
        return model_cls()


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


def _flatten(data: dict, prefix: tuple = ()) -> dict:
    """把嵌套 dict 展平为 {路径元组: 值}，用于比较两个配置的差异。"""
    flat: dict = {}
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _unflatten(flat: dict) -> dict:
    """_flatten 的逆操作。"""
    result: dict = {}
    for path, value in flat.items():
        node = result
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return result


def _settings_delta(base: AnalysisSettings, raw: dict) -> dict:
    """
    把持久化的设置转换为相对全局设置的显式覆盖。

    兼容旧版全量 dump：逐字段与全局比较，只保留真正不同的项。
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    normalized = _normalize_keys(AnalysisSettings, raw)
    if not normalized:
        return {}
    try:
        merged = AnalysisSettings.model_validate(
            _deep_merge(base.model_dump(), normalized)
        )
    except Exception as exc:
        logger.warning(f"settings.json 覆盖无效，已忽略：{exc}")
        return {}
    base_flat = _flatten(base.model_dump())
    merged_flat = _flatten(merged.model_dump())
    diff = {path: value for path, value in merged_flat.items()
            if base_flat.get(path) != value}
    return _unflatten(diff)


def _remove_keys(base: dict, patch: dict) -> dict:
    """
    从 base 中删除 patch 声明过的键（递归）。

    用于让 config.json 的「删除」生效：声明过的键不再保留历史覆盖，
    删掉后自然回退到 /maa set 的值或全局设置。
    """
    result = dict(base)
    for key, value in patch.items():
        if key not in result:
            continue
        current = result[key]
        if isinstance(current, dict) and isinstance(value, dict):
            trimmed = _remove_keys(current, value)
            if trimmed:
                result[key] = trimmed
            else:
                result.pop(key, None)
        else:
            result.pop(key, None)
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

        # ── 热重载：登记待执行的运行时重建（在指令回复发出后执行）──
        self._runtime_reload: Optional[ReloadHandler] = None
        self._pending_reload: Optional[tuple[dict, str]] = None

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
            self.configs[name] = self._load_state(name)

        total = sum(len(c.info.listen_groups) for c in self.configs.values())
        logger.info(f"已加载: {len(self.configs)} 配置, 共监听 {total} 个群")

    def _load_state(self, name: str) -> ConfigState:
        """
        从数据目录读取单个配置的持久化状态。

        设置文件只应包含 `/maa set` 的显式覆盖；旧版文件是全量 dump，
        这里会转换为相对全局设置的增量，并标记为 legacy 以便下次保存时改写。
        """
        info = self.dm.load_config_info(name)
        jobs = self.dm.load_config_jobs(name)
        raw = self.dm.load_config_settings(name)
        legacy = False
        if raw is None:
            overrides: dict = {}
        elif raw.get("version") == self.dm.SETTINGS_VERSION:
            overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        else:
            overrides = _settings_delta(self.global_settings, raw)
            legacy = True

        settings = self.apply_settings_overrides(self.global_settings, overrides, name)
        raw_project = self.dm.load_config_project(name)
        project_overrides = (
            raw_project.get("overrides")
            if isinstance(raw_project, dict) and isinstance(raw_project.get("overrides"), dict)
            else {}
        )
        state = ConfigState(
            name=name,
            info=info,
            settings=settings,
            commands=self.dm.load_config_commands(name),
            jobs=jobs,
            next_job_id=max((j.id for j in jobs), default=0) + 1,
            settings_overrides=overrides,
            settings_legacy=legacy,
            project_overrides=project_overrides,
        )
        state.project = self._build_project(
            state.project_base, state.project_overrides, name
        )
        return state

    @staticmethod
    def _build_project(base: dict, overrides: dict, label: str = "") -> ProjectConfig:
        """合并并校验 project 配置；失败时退回空配置。"""
        merged = _deep_merge(base or {}, overrides or {})
        if not merged:
            return ProjectConfig()
        try:
            return ProjectConfig.model_validate(merged)
        except Exception as exc:
            where = f"[{label}] " if label else ""
            logger.warning(f"{where}project 配置无效，使用空配置：{exc}")
            return ProjectConfig()

    @staticmethod
    def _build_config_llm(data: dict, label: str = "") -> ConfigLLM:
        """校验配置级 llm 段（候选模型列表）。"""
        normalized = _normalize_config_llm(data)
        if not normalized:
            return ConfigLLM()
        try:
            return ConfigLLM.model_validate(normalized)
        except Exception as exc:
            where = f"[{label}] " if label else ""
            logger.warning(f"{where}llm 配置无效，使用空候选列表：{exc}")
            return ConfigLLM()

    def recompute_project(self, name: str) -> None:
        """
        按「config.json → /maa project set」重新计算项目配置。

        project 是声明式的：从 config.json 删掉某个键即回退到
        `/maa project set` 的值或模型默认值。
        """
        state = self.configs.get(name)
        if state is None:
            return
        state.project = self._build_project(
            state.project_base, state.project_overrides, name
        )

    def recompute_settings(self, name: str) -> None:
        """
        按「全局 → config.json → /maa set」重新计算生效设置。

        config.json 是声明式基线；`/maa set` 是显式运行时覆盖，优先级最高。
        项目相关字段（识别方式 / UI 开关 / 仓库）不在 settings 中，见 `recompute_project`。
        """
        state = self.configs.get(name)
        if state is None:
            return
        base = self.apply_settings_overrides(
            self.global_settings, state.json_settings, name
        )
        state.settings = self.apply_settings_overrides(
            base, state.settings_overrides, name
        )

    def set_project_override(self, name: str, key: str, value: Any,
                             nested: str = "") -> None:
        """
        记录一次 `/maa project set` 覆盖并立即重算。

        `nested` 支持多级路径（如 `ui.mfaa` / `repo`），会按层级建 dict。
        """
        state = self.configs.get(name)
        if state is None:
            return
        node = state.project_overrides
        for part in [p for p in str(nested or "").split(".") if p]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[key] = value
        self.recompute_project(name)
        self.recompute_settings(name)

    def set_setting_override(self, name: str, key: str, value: Any,
                             nested: str = "") -> None:
        """记录一次 `/maa set` 覆盖并立即重算生效设置。"""
        state = self.configs.get(name)
        if state is None:
            return
        if nested:
            node = state.settings_overrides.get(nested)
            if not isinstance(node, dict):
                node = {}
                state.settings_overrides[nested] = node
            node[key] = value
        else:
            state.settings_overrides[key] = value
        self.recompute_settings(name)

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
        用 config.json → bot.configs 同步配置（与重启进程等价）。

        - 不在 config.json 中的配置会被停用（数据目录保留，重新加回即可恢复）
        - `settings` / `project` / `commands` 是声明式的：删掉某个键即回退到下一层
        - 设置分层：全局 → config.json → `/maa set` → `project`
        - 项目配置分层：config.json 的 `project` → `/maa project set`
        """
        if not isinstance(configs_cfg, dict):
            return

        removed = sorted(name for name in self.configs if name not in configs_cfg)
        for name in removed:
            self.configs.pop(name, None)
        if removed:
            logger.info(f"[配置] 已停用（不在 config.json 中）：{', '.join(removed)}")

        for name, spec in configs_cfg.items():
            if not isinstance(spec, dict):
                continue

            state = self.configs.get(name)
            if state is None:
                state = self._load_state(name)
                self.configs[name] = state

            state.info.listen_groups = {
                str(g).strip()
                for g in (spec.get("listen_groups") or [])
                if str(g).strip()
            }
            notify_raw = spec.get("notify_group")
            notify = str(notify_raw).strip() if notify_raw else None
            state.info.notify_group = notify or None
            self.dm.save_config_info(name, state.info)

            # 设置：config.json 作为声明式基线，/maa set 覆盖在其上
            state.json_settings = _normalize_keys(
                AnalysisSettings, spec.get("settings") or {}
            )
            if state.settings_legacy:
                # 旧版全量 dump 会把 config.json 的历史值也当成显式覆盖，
                # 这里剔除 config.json 声明过的键，使「删除即回退」生效。
                state.settings_overrides = _remove_keys(
                    state.settings_overrides, state.json_settings
                )
                state.settings_legacy = False

            # 项目：config.json 作为声明式基线，/maa project set 覆盖在其上
            state.project_base = _normalize_project(spec.get("project") or {})
            self.recompute_project(name)
            self.recompute_settings(name)
            self.dm.save_config_settings(name, state.settings_overrides)
            self.dm.save_config_project(name, state.project_overrides)

            # AI 段：完全由 config.json 决定（声明式，不做运行时覆盖）
            state.llm = self._build_config_llm(
                spec.get("llm") or {}, name
            )
            state.vision = _build_model(VisionConfig, spec.get("vision") or {}, name)
            state.report = _build_model(ReportConfig, spec.get("report") or {}, name)

            # 指令：完全由 config.json 决定（删除段即回退默认值）
            state.commands = self.apply_commands_overrides(
                CommandConfig(), spec.get("commands") or {}, name
            )
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

    def register_runtime_reload(self, handler: ReloadHandler) -> None:
        """注册运行时重建回调 async (new_cfg, notify_group) -> (ok, message)。"""
        self._runtime_reload = handler

    def request_reload(self, path: str = "config.json",
                       notify_group: str = "") -> tuple[bool, str]:
        """
        解析并校验 config.json，登记一次待执行的运行时重建。

        真正的重建由运行时在**指令回复发送之后**执行（apply_pending_reload），
        否则会拆掉正在发送回复的连接。
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
        if not isinstance(cfg, dict):
            return False, "config.json 顶层必须是对象。"

        ob = cfg.get("onebot") or {}
        if not isinstance(ob, dict):
            return False, "onebot 段必须是对象。"
        mode = str(ob.get("mode", "http_ws"))
        if mode not in ONEBOT_MODES:
            return False, f"onebot.mode 无效：{mode}（可选：{', '.join(ONEBOT_MODES)}）"

        bot_cfg = cfg.get("bot") or {}
        if not isinstance(bot_cfg, dict):
            return False, "bot 段必须是对象。"
        if not str(bot_cfg.get("data_dir", "data") or "").strip():
            return False, "bot.data_dir 不能为空。"

        configs_cfg = bot_cfg.get("configs")
        if configs_cfg is None:
            configs_cfg = {}
        if not isinstance(configs_cfg, dict):
            return False, "bot.configs 必须是对象。"

        # AI 配置：旧结构拒绝启动；候选引用必须有效
        from core.llm_pool import detect_legacy_llm, validate_llm_config

        legacy = detect_legacy_llm(cfg.get("llm") or {})
        if legacy:
            return False, legacy
        llm_errors = validate_llm_config(cfg.get("llm") or {}, configs_cfg)
        if llm_errors:
            return False, "LLM 配置无效：\n" + "\n".join(f"- {e}" for e in llm_errors)

        total = sum(
            len([g for g in (spec.get("listen_groups") or []) if str(g).strip()])
            for spec in configs_cfg.values() if isinstance(spec, dict)
        )
        self._pending_reload = (cfg, str(notify_group or ""))
        return True, (
            f"已读取 config.json：{len(configs_cfg)} 个配置 / {total} 个群。\n"
            "正在按新配置重建运行时（日志另开新文件，LLM / OneBot 连接 / 数据目录重新初始化），"
            "连接会短暂中断。"
        )

    def has_pending_reload(self) -> bool:
        """是否有待执行的运行时重建。"""
        return self._pending_reload is not None

    async def apply_pending_reload(self) -> tuple[bool, str]:
        """执行已登记的运行时重建（在指令回复发送之后调用）。"""
        pending = self._pending_reload
        self._pending_reload = None
        if pending is None:
            return True, ""
        cfg, notify_group = pending
        if self._runtime_reload is None:
            return False, "当前进程没有注册运行时重建回调，请重启进程。"
        return await self._runtime_reload(cfg, notify_group)

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
