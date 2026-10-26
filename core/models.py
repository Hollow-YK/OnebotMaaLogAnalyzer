"""
数据模型 — pydantic BaseModel，自动序列化/校验，snake_case ↔ camelCase。

包含：
  - ConfigInfo       单个配置的群组信息（监听群 + 通知群）
  - CommandConfig    QQ 指令系统设置（权限等级 / 前缀）
  - RepoConfig       项目代码仓库设置（供 AI 对照源码）
  - LLM*Config       LLM 端点 / 模型 / 候选 / 全局默认
  - VisionConfig     多模态（看图）设置
  - ReportConfig     图片化报告设置
  - AnalysisSettings 日志分析设置（全局默认 + 各配置可覆盖）
  - LogFileItem      群文件条目
  - JobRecord        一次分析任务的运行记录
  - ConfigState      单个配置的运行时完整状态（dataclass）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from core.archive_names import (
    is_known_log_archive_name,
    looks_like_log_archive_name,
    normalize_archive_name,
)


def _to_camel(name: str) -> str:
    """snake_case → camelCase"""
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class ConfigInfo(BaseModel):
    """单个配置的群组信息。"""

    listen_groups: set[str] = Field(default_factory=set, alias="listenGroups")
    notify_group: Optional[str] = Field(default=None, alias="notifyGroup")

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class CommandConfig(BaseModel):
    """
    QQ 指令系统设置。

    权限等级（数值越大权限越高）:
      0  任意群成员
      1  普通管理员（admins 列表）
      2  超级管理员（owners 列表）

    读指令默认 0（群内可查看），写指令默认 2（仅超管可改），
    避免普通成员误改配置。
    """

    enabled: bool = True
    prefix: str = "/maa"

    owners: List[str] = Field(default_factory=list)   # 超管 QQ 号
    admins: List[str] = Field(default_factory=list)   # 管理员 QQ 号

    read_level: int = 0    # 查看类指令所需等级
    write_level: int = 2   # 修改类指令所需等级
    followup_level: int = 0  # 追问答疑所需等级

    reply_chunk_chars: int = 3500
    max_list_items: int = 40

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def level_of(self, user_id: str) -> int:
        """返回用户的权限等级。"""
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        if uid in {str(x).strip() for x in self.owners}:
            return 2
        if uid in {str(x).strip() for x in self.admins}:
            return 1
        return 0


class ModelSupports(BaseModel):
    """模型能力声明：决定候选模型能否满足某次调用。"""

    tools: bool = False
    vision: bool = False
    json_mode: bool = False

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class ModelSpec(BaseModel):
    """全局 `llm.providers[].models[]`：端点上的一个模型及其参数。"""

    model: str = ""
    supports: ModelSupports = Field(default_factory=ModelSupports)
    max_tokens: int = 0          # 0 = 用 llm.defaults.max_tokens
    max_context_tokens: int = 0  # 0 = 用 llm.defaults.max_context_tokens
    temperature: Optional[float] = None
    top_p: Optional[float] = None

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class LLMProviderConfig(BaseModel):
    """全局 `llm.providers[]`：一个 OpenAI 兼容端点。"""

    name: str = ""
    kind: str = "openai"          # 首版只实现 openai
    base_url: str = ""
    api_key: str = ""
    max_concurrency: int = 2
    timeout_seconds: int = 180
    connect_timeout_seconds: int = 10
    max_retries: int = 2
    models: List[ModelSpec] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def find_model(self, name: str) -> Optional[ModelSpec]:
        """按模型名查找；找不到返回 None。"""
        target = str(name or "").strip()
        for spec in self.models:
            if spec.model == target:
                return spec
        return None


class LLMDefaults(BaseModel):
    """全局 `llm.defaults`：参数兜底值。"""

    temperature: float = 0.2
    top_p: float = 1.0
    max_tokens: int = 8192
    max_context_tokens: int = 48000

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class CircuitBreakerConfig(BaseModel):
    """全局 `llm.circuit_breaker`：端点级熔断。"""

    failure_threshold: int = 3
    cooldown_seconds: int = 300
    # 配额/限流（429）单独冷却：TPM / RPM 通常按分钟恢复，
    # 用通用冷却会把端点在配额恢复后仍锁住。
    rate_cooldown_seconds: int = 60
    # 鉴权失败（401/403）冷却更长，但冷却后允许一次半开试探，
    # 这样在面板上改好 key 后无需重启即可自动恢复。
    auth_cooldown_seconds: int = 1800

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class LLMGlobalConfig(BaseModel):
    """config.json → `llm`（端点、密钥、模型能力与兜底参数）。"""

    providers: List[LLMProviderConfig] = Field(default_factory=list)
    defaults: LLMDefaults = Field(default_factory=LLMDefaults)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class ModelRef(BaseModel):
    """`bot.configs.<名称>.llm.models[]` 中的一个候选引用。"""

    name: str = ""
    model: str = ""

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class ConfigLLM(BaseModel):
    """`bot.configs.<名称>.llm`：本配置按优先级尝试的候选模型列表。"""

    models: List[ModelRef] = Field(default_factory=list)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class VisionConfig(BaseModel):
    """`bot.configs.<名称>.vision`：多模态（给模型看图）设置。"""

    enabled: bool = True
    max_images: int = 4
    max_image_kb: int = 800
    detail: str = "high"
    attach_error_screenshots: bool = True
    attach_repo_templates: bool = True
    allow_model_request: bool = True

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class ReportConfig(BaseModel):
    """`bot.configs.<名称>.report`：图片化报告设置。"""

    render: str = "auto"          # image / text / auto
    template: str = "default"
    max_width_px: int = 1080
    max_height_px: int = 4096
    max_pages: int = 6
    device_scale_factor: float = 1.0
    font_family: str = ""
    browser_path: str = ""
    reuse_browser: bool = True
    render_timeout_ms: int = 8000
    embed_attachments: bool = True
    attach_markdown: bool = False
    fallback_to_text: bool = True
    render_followup: bool = True

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def render_mode(self) -> str:
        """规范化 render 取值（非法值按 auto 处理）。"""
        mode = str(self.render or "auto").strip().lower()
        return mode if mode in ("image", "text", "auto") else "auto"


class RepoConfig(BaseModel):
    """
    项目代码仓库设置 — 让 AI 在分析时对照源码核对节点定义、阈值与 expected 文本。

    来源二选一（path 优先）：
      - path: 本地已存在的仓库目录，无需网络与 git
      - url:  git 仓库地址；mirrors 按顺序作为备用来源
        预下载开启 → 完整克隆并初始化子模块；关闭 → 浅克隆并按需拉取

    版本切换：
      - 从日志包文件名提取版本号（如 ...-logs-1.0.0-alpha.9-20260907-033044.zip），
        自动 checkout 对应 tag，使代码参考与日志实际运行的版本一致
      - allow_ai_switch_tag 开启时，AI 可在限定轮次内获取其他 tag / commit 复核
        （与预下载开关无关）
      - 分析结束后还原到分析前的分支、tag 或提交
    """

    enabled: bool = False

    # ── 检索模式 ──
    # inject: 预检索后把命中片段拼进提示词（快、省 token，默认）
    # agent:  把仓库读写工具交给 AI，由 AI 自主多轮检索（深、token 消耗高）
    mode: str = "inject"

    # ── 来源 ──
    path: str = ""
    url: str = "https://github.com/MAAXYZ/MaaXXX"
    mirrors: List[str] = Field(default_factory=list)
    branch: str = ""
    default_branch: str = ""       # 分析结束后切回的目标，空 = 自动探测
    # 启动时预下载：完整克隆（含全部历史 / tag）并初始化子模块；
    # 关闭时改为首次分析时浅克隆，并按需拉取 tag / commit
    preload_enabled: bool = True
    preload_retries: int = 3
    update_on_analyze: bool = False  # 每份日志分析前更新默认分支
    allow_ai_update: bool = True    # 允许 AI 请求更新当前配置的仓库

    # ── 版本切换 ──
    auto_checkout_version: bool = True      # 从日志版本号自动切 tag
    allow_ai_switch_tag: bool = True        # 允许 AI 获取并切换 tag / commit
    max_tag_switch_rounds: int = 2          # AI 切换 tag 的最大轮次
    # 分析结束后是否还原到分析前的位置。
    # 部署环境用浅克隆时，还原只是切回原来的 tag，开销可忽略；
    # 开发环境若 path 指向你自己的仓库，建议保持开启以免分支被切走。
    # 注意：关闭后仓库会停在分析用的 tag（detached HEAD），
    # 下次分析仍能正常工作，但手动查看仓库时需自行切回。
    restore_latest_after_analyze: bool = True
    refuse_dirty_worktree: bool = True      # 工作区有未提交改动时拒绝切换（保护本地开发）

    # ── agent 模式 ──
    max_tool_rounds: int = 8           # 工具调用最大轮次
    max_tool_calls_per_round: int = 6  # 单轮最多执行的工具数
    max_tool_result_chars: int = 30000  # 单次工具结果字符上限
    max_tool_result_tokens: int = 0    # 0 = 由 max_tool_result_chars 估算推导
    max_tool_context_tokens: int = 0   # 0 = 不超过本次上下文预算的 50%
    agent_deadline_seconds: int = 600  # agent 循环总时长上限

    # ── 扫描范围 ──
    extensions: List[str] = Field(
        default_factory=lambda: [".json", ".jsonc", ".yaml", ".yml", ".toml", ".md", ".txt"]
    )
    max_file_kb: int = 256            # 跳过大于该值的文件
    max_scan_files: int = 20000       # 单次扫描文件数上限
    max_files: int = 12               # 注入提示词的文件数上限

    # ── 注入提示词 ──
    max_identifiers: int = 24         # 从日志中提取的标识符上限
    context_lines: int = 2            # 命中行前后附带的上下文行数
    max_block_lines: int = 150        # 命中 JSON 对象定义时展开的最大行数
    max_chars: int = 60000            # 代码片段段落字符上限

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def usable(self) -> bool:
        """是否配置了可用来源。"""
        return bool(self.path or self.url)

    def remote_urls(self) -> list[str]:
        """返回去重后的主地址与镜像，保持配置顺序。"""
        if self.path:
            return []
        return list(dict.fromkeys(
            str(url).strip()
            for url in [self.url, *self.mirrors]
            if str(url).strip()
        ))

    def git_ops_allowed(self) -> bool:
        """是否需要（且允许）对仓库执行 git 切换操作。"""
        return self.auto_checkout_version or self.allow_ai_switch_tag

    def use_agent(self) -> bool:
        """是否使用 agent 自主检索模式。"""
        return str(self.mode or "").strip().lower() == "agent"


_UI_KEY_ALIASES = {
    "mxu": "mxu",
    "mxuapp": "mxu",
    "mfaa": "mfaa",
    "mfaavalonia": "mfaa",
    "mfaaavalonia": "mfaa",
    "maafwapp": "maafwapp",
    "maafw": "maafwapp",
    "auto": "auto",
    "default": "auto",
    "unknown": "auto",
    "other": "auto",
}


def normalize_ui_key(key: str) -> str:
    """把 UI 名称归一化为 project.ui 的键名（未知名称原样保留小写）。"""
    text = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    if not text:
        return "auto"
    return _UI_KEY_ALIASES.get(text, text)


class ProjectUISettings(BaseModel):
    """单个 UI 的设置（`project.ui.<名称>`）——只控制是否分析该 UI。"""

    enabled: bool = True

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


class ProjectConfig(BaseModel):
    """
    项目相关设置（`bot.configs.<名称>.project`）。

    - `name`：项目名（如 MAK），同时作为报告与提示词中的显示名
    - `archive_match_mode`：日志包识别方式
    - `file_prefix` / `require_log_layout`：仅 `prefix` 模式使用
    - `ui`：按 UI 开关（键名 mxu / mfaa / maafwapp / auto）
    - `repo`：项目代码仓库
    """

    name: str = ""
    # auto   文件名特征初筛 + 内容特征识别（默认）
    # known  仅已知 UI 文件名初筛 + 内容特征识别
    # prefix 仅前缀识别；require_log_layout=true 时额外做内容特征识别
    # any    仅内容特征识别（任意 zip 都会下载后检查）
    archive_match_mode: str = "auto"
    # 仅 prefix 模式使用
    file_prefix: str = ""
    require_log_layout: bool = True

    ui: Dict[str, ProjectUISettings] = Field(default_factory=dict)
    repo: RepoConfig = Field(default_factory=RepoConfig)

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def ui_key_for(self, ui_key: str) -> str:
        """把任意写法归一化为 project.ui 的键名。"""
        return normalize_ui_key(ui_key)

    def ui_enabled(self, ui_key: str) -> bool:
        """该 UI 是否允许分析；未配置时回退 `auto`，再回退 True。"""
        key = normalize_ui_key(ui_key)
        item = self.ui.get(key)
        if item is not None:
            return bool(item.enabled)
        fallback = self.ui.get("auto")
        if fallback is not None:
            return bool(fallback.enabled)
        return True

    def matches(self, file_name: str) -> bool:
        """
        文件名初筛（下载前）。

        内容特征识别始终在下载后进行，见 `require_log_layout` 与
        `DigestResult.is_log_archive`。
        """
        name = normalize_archive_name(file_name)
        if not name.lower().endswith(".zip"):
            return False

        mode = str(self.archive_match_mode or "auto").strip().lower()
        if mode == "any":
            return True
        if mode == "prefix":
            prefix = str(self.file_prefix or "").strip()
            return bool(prefix) and name.startswith(prefix)
        if is_known_log_archive_name(name):
            return True
        if mode == "known":
            return False
        return looks_like_log_archive_name(name)


class AnalysisSettings(BaseModel):
    """日志分析设置 — 全局默认 + 各配置可覆盖。"""

    # ── 触发 ──
    enabled: bool = True
    file_list_delay_seconds: float = 0.0   # 上传后等待群文件列表刷新
    # 同一文件的去重窗口（秒）。
    # 部分协议端（如 SnowLuma）对一次上传会同时发出 notice.group_upload
    # 与带 file 段的消息事件，两条入口各自触发一次分析，导致重复回复。
    # 在此窗口内视为同一次上传，只处理一次；0 = 关闭去重。
    duplicate_window_seconds: int = 120

    # ── 消息反馈 ──
    send_progress_message: bool = True
    send_summary: bool = True
    reply_chunk_chars: int = 3500

    # ── 结果附图 ──
    send_images: bool = True            # 报告后附带相关图片
    max_report_images: int = 4          # 最多附带几张图
    send_repo_images: bool = True       # 附带仓库中的模板图（仅本地仓库可用）
    # ── 结果附件（非图片文件，以群文件形式发送）──
    send_files: bool = True             # 允许模型附带任意文件（如配置、pipeline JSON）
    max_report_files: int = 2           # 最多附带几个文件
    max_file_attachment_mb: int = 20    # 单个附件文件大小上限 MB
    # 附件可插在报告文字中间（保持模型给出的位置）；关闭则统一附在末尾
    inline_attachments: bool = True

    # ── 追问答疑（分析完成后在该群继续提问）──
    followup_enabled: bool = True
    # 追问会话有效分钟数。0 = 跟随历史保留期（history_period_hours × history_keep_periods）
    followup_window_minutes: int = 0
    followup_max_turns: int = 10        # 单会话最多追问轮数
    followup_max_sessions: int = 50     # 最多保留多少个会话

    # ── 分析历史 ──
    # 保留「用于追问的消息 ID + 日志压缩包 + 报告」，使追问可跨越较长时间。
    # 清理策略：每过一个 period 检查一次，删除超过 keep_periods 个周期的记录。
    # 因此默认 1d 周期 / 保留 2 周期 → 可追问约 2~3 天内的日志。
    # 注意：git 仓库缓存（data/repo）不属于历史记录，**永不删除**。
    history_enabled: bool = True
    history_period_hours: int = 24      # 保留周期（默认 1 天）
    history_keep_periods: int = 2       # 删除 N 个周期以前的记录
    history_max_records: int = 200      # 每配置最多保留记录数（安全上限，0=不限）
    history_max_total_mb: int = 4096    # 历史目录总大小上限 MB（安全上限，0=不限）

    # ── 并发与超时 ──
    analysis_concurrency: int = 2
    download_concurrency: int = 2
    download_timeout_seconds: int = 120
    digest_timeout_seconds: int = 0
    # 整个 AI 阶段（含工具循环与收敛）的总时长上限
    ai_stage_timeout_seconds: int = 300
    # 0 = 用 llm.defaults.max_context_tokens；非 0 时按配置覆盖
    max_context_tokens: int = 0
    # 分析结果缓存小时数；0 = 关闭
    analysis_cache_hours: int = 0
    # 连续失败触发熔断（端点级冷却），失败时降级为模板报告
    circuit_breaker_enabled: bool = True

    # ── 模型调用 ──
    system_prompt: str = ""

    # ── 日志包工具（让 AI 像读仓库一样读日志包原始文件）──
    # 开启后，即使 repo.enabled=false，分析也会走工具模式：
    # 模型可调用 log_search / log_read_file / log_list_files 直接查压缩包内容，
    # 而不是只能看到摘要里已提取的部分。追问同样适用。
    log_tools_enabled: bool = True

    # ── 临时文件 ──
    cleanup_after_done: bool = True
    save_digest_debug: bool = False

    # ── 压缩包与摘要 ──
    max_zip_size_mb: int = 100
    max_total_uncompressed_mb: int = 120
    max_prompt_chars: int = 200000
    max_zip_members: int = 300
    max_log_files: int = 6
    max_maafw_bak_files: int = 1
    # 单个 maafw.bak 日志的大小上限（MB）；0 = 不限制。避免误扫数百 MB 备份
    max_maafw_bak_mb: int = 64
    # 分卷日志包（*-part01.zip / *-part02.zip）自动合并分析
    merge_archive_parts: bool = True
    # 分卷包静默期：每收到一个新分卷就重新计时，连续该秒数无新分卷后才分析
    # 0 = 收到第一个分卷立即分析（可能缺少后续分卷）
    archive_part_settle_seconds: float = 30.0
    max_log_file_read_kb: int = 512
    small_log_full_read_mb: int = 5
    stream_medium_logs: bool = True
    large_log_threshold_mb: int = 50
    max_large_log_read_kb: int = 256
    stream_large_logs: bool = True
    max_stream_log_mb: int = 0
    max_sections_per_log: int = 60
    max_log_chars: int = 120000
    compress_context_noise: bool = True
    max_recognition_evidence_lines: int = 60
    max_recognition_evidence_chars: int = 35000
    log_head_lines: int = 16
    context_before_lines: int = 80
    context_after_lines: int = 60

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    def max_zip_bytes(self) -> int:
        return max(1, int(self.max_zip_size_mb or 100)) * 1024 * 1024


class LogFileItem(BaseModel):
    """群文件条目（get_group_root_files / get_group_files_by_folder 返回项）。"""

    file_id: str = Field(default="", alias="fileId")
    file_name: str = Field(default="", alias="fileName")
    size: int = 0
    modify_time: int = Field(default=0, alias="modifyTime")
    upload_time: int = Field(default=0, alias="uploadTime")
    busid: int = 0
    relative_path: str = Field(default="", alias="relativePath")
    parent_id: str = Field(default="/", alias="parentId")

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        """兼容 OneBot 实现的不同字段名。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get("file_id"):
            data["file_id"] = data.get("id") or data.get("fileId") or ""
        if not data.get("file_name"):
            data["file_name"] = data.get("name") or data.get("fileName") or ""
        if not data.get("size"):
            data["size"] = data.get("file_size") or 0
        if not data.get("modify_time"):
            data["modify_time"] = (
                data.get("modifyTime") or data.get("upload_time") or data.get("uploadTime") or 0
            )
        if not data.get("upload_time"):
            data["upload_time"] = data.get("uploadTime") or data.get("modify_time") or 0
        for key in ("size", "busid", "modify_time", "upload_time"):
            try:
                data[key] = int(data.get(key) or 0)
            except (TypeError, ValueError):
                data[key] = 0
        return data

    def sort_timestamp(self) -> int:
        return int(self.modify_time or self.upload_time or 0)


class JobRecord(BaseModel):
    """一次分析任务记录。"""

    id: int = 0
    config_name: str = Field(default="", alias="configName")
    group_id: str = Field(default="", alias="groupId")
    file_name: str = Field(default="", alias="fileName")
    file_id: str = Field(default="", alias="fileId")
    file_size: int = Field(default=0, alias="fileSize")
    uploader: str = ""
    started_at: int = Field(default=0, alias="startedAt")
    finished_at: int = Field(default=0, alias="finishedAt")
    status: str = ""          # 已执行 / 执行失败 / 已跳过
    log_count: int = Field(default=0, alias="logCount")
    image_count: int = Field(default=0, alias="imageCount")
    detail: str = ""

    model_config = {"populate_by_name": True, "alias_generator": _to_camel}


@dataclass
class ConfigState:
    """单个配置的运行时完整状态。"""

    name: str
    info: ConfigInfo = field(default_factory=ConfigInfo)
    settings: AnalysisSettings = field(default_factory=AnalysisSettings)
    commands: CommandConfig = field(default_factory=CommandConfig)
    jobs: List[JobRecord] = field(default_factory=list)
    next_job_id: int = 1

    # 设置来源分层（优先级从低到高）：
    #   global_settings（data/settings.json）
    #   → json_settings（config.json 的 settings 段，仅内存）
    #   → settings_overrides（/maa set，持久化到 data/<名称>/settings.json）
    # 这样从 config.json 删掉某个键就会回退到 /maa set 的值或全局默认值。
    settings_overrides: dict = field(default_factory=dict)
    json_settings: dict = field(default_factory=dict)
    # 旧版 settings.json 是全量 dump，首次同步后会被改写成增量格式
    settings_legacy: bool = False

    # 项目配置（config.json 的 project 段 + /maa project set 覆盖）
    project: ProjectConfig = field(default_factory=ProjectConfig)
    project_base: dict = field(default_factory=dict)
    project_overrides: dict = field(default_factory=dict)

    # AI 相关配置（完全由 config.json 的 bot.configs.<名称> 决定）
    llm: ConfigLLM = field(default_factory=ConfigLLM)
    vision: VisionConfig = field(default_factory=VisionConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
