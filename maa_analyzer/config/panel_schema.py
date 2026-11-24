"""Typed AstrBot panel schema and its flat runtime representation."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .panel_overrides import (
    PANEL_NONNEGATIVE_INT_FIELDS,
    PANEL_TRISTATE_MODE_FIELDS,
)


class PanelSection(BaseModel):
    """Base model for one AstrBot panel section."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    panel_title: ClassVar[str] = ""


class BasicPanel(PanelSection):
    """Basic connection and group settings."""

    panel_title: ClassVar[str] = "基础与群组"

    enabled_mode: str = "enabled"
    config_file: str = ""
    data_dir: str = "data"
    default_config: str = ""
    listen_groups: list[str] = Field(default_factory=list)
    notify_group: str = ""


class CommandsPanel(PanelSection):
    """Management commands and permissions."""

    panel_title: ClassVar[str] = "管理指令与权限"

    command_prefix: str = "/maa"
    owners: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    commands_mode: str = "enabled"
    commands_read_level: int = 0
    commands_write_level: int = 2
    commands_followup_level: int = 0
    commands_reply_chunk_chars: int = 3500
    commands_max_list_items: int = 40


class ProjectPanel(PanelSection):
    """Project identification and UI matching."""

    panel_title: ClassVar[str] = "项目识别与 UI 匹配"

    project_name: str = ""
    archive_match_mode: str = "auto"
    file_prefix: str = ""
    require_log_layout_mode: str = "enabled"
    ui_auto_mode: str = "enabled"


class LLMPanel(PanelSection):
    """LLM endpoints, models, and circuit breakers."""

    panel_title: ClassVar[str] = "LLM 端点、模型与熔断"

    llm_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    llm_models: list[dict[str, Any]] = Field(default_factory=list)
    llm_profile_max_tokens: int = -1
    llm_endpoint_selector: str = ""
    llm_endpoint_name: str = ""
    llm_endpoint_kind: str = "openai"
    llm_endpoint_base_url: str = "https://api.openai.com/v1"
    llm_endpoint_api_key: str = ""
    llm_endpoint_max_concurrency: int = 2
    llm_endpoint_timeout_seconds: int = 180
    llm_endpoint_connect_timeout_seconds: int = 10
    llm_endpoint_max_retries: int = 2
    llm_endpoint_model: str = ""
    llm_model_support_vision_mode: str = "disabled"
    llm_model_support_json_mode_mode: str = "disabled"
    llm_model_max_tokens: int = 0
    llm_model_max_context_tokens: int = 0
    llm_defaults_max_tokens: int = 8192
    llm_defaults_max_context_tokens: int = 48000
    llm_circuit_failure_threshold: int = 3
    llm_circuit_cooldown_seconds: int = 300
    llm_circuit_rate_cooldown_seconds: int = 60
    llm_circuit_auth_cooldown_seconds: int = 1800


class VisionPanel(PanelSection):
    """Image understanding settings."""

    panel_title: ClassVar[str] = "图像理解"

    vision_mode: str = "enabled"
    vision_max_images: int = 4
    vision_max_image_kb: int = 800
    vision_detail: str = "high"
    vision_attach_error_screenshots_mode: str = "enabled"
    vision_attach_repo_templates_mode: str = "enabled"
    vision_allow_model_request_mode: str = "enabled"


class ReportPanel(PanelSection):
    """Report rendering settings."""

    panel_title: ClassVar[str] = "报告渲染"

    report_mode: str = "auto"
    report_template: str = "default"
    report_max_width_px: int = 1080
    report_max_height_px: int = 4096
    report_max_pages: int = 6
    report_device_scale_factor: str = "1.0"
    report_font_family: str = ""
    report_browser_path: str = ""
    report_reuse_browser_mode: str = "enabled"
    report_render_timeout_ms: int = 8000
    report_embed_attachments_mode: str = "enabled"
    report_attach_markdown_mode: str = "disabled"
    report_fallback_to_text_mode: str = "enabled"
    report_render_followup_mode: str = "enabled"


class FeedbackPanel(PanelSection):
    """Message feedback and attachments."""

    panel_title: ClassVar[str] = "消息反馈与附件"

    send_images_mode: str = "enabled"
    send_repo_images_mode: str = "enabled"
    send_files_mode: str = "enabled"
    settings_send_progress_message_mode: str = "enabled"
    settings_send_summary_mode: str = "enabled"
    settings_reply_chunk_chars: int = 3500
    settings_forward_analysis_results_mode: str = "disabled"
    settings_inline_attachments_mode: str = "enabled"
    settings_max_report_images: int = 4
    settings_max_report_files: int = 2
    settings_max_file_attachment_mb: int = 20


class FollowupPanel(PanelSection):
    """Follow-up question settings."""

    panel_title: ClassVar[str] = "追问答疑"

    followup_mode: str = "enabled"
    followup_window_minutes: int = 0
    settings_followup_max_turns: int = 10
    settings_followup_max_sessions: int = 50


class HistoryPanel(PanelSection):
    """Analysis history settings."""

    panel_title: ClassVar[str] = "分析历史"

    history_mode: str = "enabled"
    history_period_hours: int = 24
    history_keep_periods: int = 2
    settings_history_max_records: int = 200
    settings_history_max_total_mb: int = 4096


class AnalysisPanel(PanelSection):
    """Analysis workflow and context settings."""

    panel_title: ClassVar[str] = "分析流程与上下文"

    settings_download_concurrency: int = 2
    settings_download_timeout_seconds: int = 120
    settings_ai_stage_timeout_seconds: int = 300
    settings_max_context_tokens: int = 0
    settings_context_prune_tool_result_tokens: int = 512
    settings_context_summary_tokens: int = 1500
    settings_analysis_cache_hours: int = 0
    settings_duplicate_window_seconds: int = 120
    settings_context_compact_enabled_mode: str = "enabled"
    settings_circuit_breaker_enabled_mode: str = "enabled"
    settings_cleanup_after_done_mode: str = "enabled"
    settings_file_list_delay_seconds: str = "0.0"
    settings_context_compact_ratio: str = "0.8"
    settings_context_retain_ratio: str = "0.25"
    settings_system_prompt: str = ""


class ProcessingPanel(PanelSection):
    """Archive and log processing settings."""

    panel_title: ClassVar[str] = "压缩包与日志处理"

    settings_max_zip_size_mb: int = 100
    settings_max_total_uncompressed_mb: int = 1024
    settings_merge_archive_parts_mode: str = "enabled"
    settings_archive_part_settle_seconds: str = "30.0"
    settings_max_log_file_read_kb: int = 512


class RepoPanel(PanelSection):
    """Project repository and retrieval settings."""

    panel_title: ClassVar[str] = "项目仓库与检索"

    repo_path: str = ""
    repo_url: str = "https://github.com/MAAXYZ/MaaXXX"
    repo_mirrors: list[str] = Field(default_factory=list)
    repo_branch: str = ""
    repo_default_branch: str = ""
    repo_preload_enabled_mode: str = "enabled"
    repo_preload_retries: int = 3
    repo_update_on_analyze_mode: str = "disabled"
    repo_allow_ai_update_mode: str = "enabled"
    repo_auto_checkout_version_mode: str = "enabled"
    repo_allow_ai_switch_tag_mode: str = "enabled"
    repo_max_tag_switch_rounds: int = 2
    repo_restore_latest_after_analyze_mode: str = "enabled"
    repo_refuse_dirty_worktree_mode: str = "enabled"
    repo_max_tool_rounds: int = 8
    repo_max_tool_calls_per_round: int = 6
    repo_max_tool_result_chars: int = 30000
    repo_max_tool_result_tokens: int = 0
    repo_max_tool_context_tokens: int = 0
    repo_agent_deadline_seconds: int = 600
    repo_extensions: list[str] = Field(
        default_factory=lambda: [
            ".json",
            ".jsonc",
            ".yaml",
            ".yml",
            ".toml",
            ".md",
            ".txt",
        ]
    )
    repo_max_file_kb: int = 256
    repo_max_scan_files: int = 20000


class LoggingPanel(PanelSection):
    """File logging settings."""

    panel_title: ClassVar[str] = "文件日志"

    log_to_file_mode: str = "enabled"
    log_dir: str = "logs"
    log_level: str = "INFO"


class UpdatePanel(PanelSection):
    """GitHub release checking and self-update settings."""

    panel_title: ClassVar[str] = "检查更新"

    update_check_mode: str = "enabled"
    update_channel: str = "stable"
    update_auto_mode: str = "disabled"
    update_download_mode: str = "auto"
    update_notify_qq: list[str] = Field(default_factory=list)
    update_check_on_start_mode: str = "enabled"
    update_check_interval_hours: int = 24
    update_api_bases: list[str] = Field(
        default_factory=lambda: ["https://api.github.com"]
    )
    update_download_mirrors: list[str] = Field(default_factory=list)


class PanelConfig(BaseModel):
    """Typed AstrBot panel configuration grouped by semantic section."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    basic: BasicPanel = Field(default_factory=BasicPanel)
    commands: CommandsPanel = Field(default_factory=CommandsPanel)
    project: ProjectPanel = Field(default_factory=ProjectPanel)
    llm: LLMPanel = Field(default_factory=LLMPanel)
    vision: VisionPanel = Field(default_factory=VisionPanel)
    report: ReportPanel = Field(default_factory=ReportPanel)
    feedback: FeedbackPanel = Field(default_factory=FeedbackPanel)
    followup: FollowupPanel = Field(default_factory=FollowupPanel)
    history: HistoryPanel = Field(default_factory=HistoryPanel)
    analysis: AnalysisPanel = Field(default_factory=AnalysisPanel)
    processing: ProcessingPanel = Field(default_factory=ProcessingPanel)
    repo: RepoPanel = Field(default_factory=RepoPanel)
    logging: LoggingPanel = Field(default_factory=LoggingPanel)
    update: UpdatePanel = Field(default_factory=UpdatePanel)


@dataclass(frozen=True, slots=True)
class PanelGroupSpec:
    """AstrBot presentation metadata bound to a typed panel section."""

    key: str
    title: str
    section_model: type[PanelSection]

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self.section_model.model_fields)


def _build_group_specs() -> tuple[PanelGroupSpec, ...]:
    specs: list[PanelGroupSpec] = []
    for key, field in PanelConfig.model_fields.items():
        section_model = field.annotation
        if not isinstance(section_model, type) or not issubclass(
            section_model, PanelSection
        ):
            raise TypeError(f"面板分组 {key} 必须使用 PanelSection 子模型。")
        if not section_model.panel_title:
            raise TypeError(f"面板分组 {key} 缺少 panel_title。")
        specs.append(
            PanelGroupSpec(
                key=key,
                title=section_model.panel_title,
                section_model=section_model,
            )
        )
    return tuple(specs)


PANEL_GROUP_SPECS = _build_group_specs()
PANEL_SCHEMA_GROUPS: dict[str, PanelGroupSpec] = {
    spec.key: spec for spec in PANEL_GROUP_SPECS
}


def _section_defaults(model: type[PanelSection]) -> dict[str, Any]:
    return copy.deepcopy(model().model_dump())


PANEL_DEFAULTS: dict[str, Any] = {
    key: value
    for spec in PANEL_GROUP_SPECS
    for key, value in _section_defaults(spec.section_model).items()
}
PANEL_SCHEMA_GROUP_FIELDS: dict[str, str] = {
    key: group
    for group, spec in PANEL_SCHEMA_GROUPS.items()
    for key in spec.fields
}

PANEL_AUTO_SELECT_FIELDS = {
    "config_file",
    "default_config",
    "llm_endpoint_selector",
    "llm_endpoint_name",
    "llm_endpoint_model",
    "llm_endpoints",
    "llm_models",
}

_PANEL_MISSING = object()

# Kept in the AstrBot schema until old installations have been migrated.
LEGACY_LLM_DEFAULTS: dict[str, Any] = {
    "llm_provider": "", "llm_model": "", "llm_fallback_models": [],
}

LEGACY_ENDPOINT_FIELDS = {
    "name": "llm_endpoint_name", "kind": "llm_endpoint_kind",
    "base_url": "llm_endpoint_base_url", "api_key": "llm_endpoint_api_key",
    "max_concurrency": "llm_endpoint_max_concurrency",
    "timeout_seconds": "llm_endpoint_timeout_seconds",
    "connect_timeout_seconds": "llm_endpoint_connect_timeout_seconds",
    "max_retries": "llm_endpoint_max_retries", "model": "llm_endpoint_model",
    "support_vision_mode": "llm_model_support_vision_mode",
    "support_json_mode_mode": "llm_model_support_json_mode_mode",
    "model_max_tokens": "llm_model_max_tokens",
    "model_max_context_tokens": "llm_model_max_context_tokens",
}


def _mapping_value(value: Mapping[str, Any], key: str) -> Any:
    """Find a key inside one nested AstrBot object section."""
    if key in value:
        return value[key]
    for child in value.values():
        if isinstance(child, Mapping):
            found = _mapping_value(child, key)
            if found is not _PANEL_MISSING:
                return found
    return _PANEL_MISSING


def flatten_panel_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten grouped AstrBot config, accepting legacy top-level keys."""
    flat: dict[str, Any] = {}
    for group, spec in PANEL_SCHEMA_GROUPS.items():
        section = config.get(group)
        if not isinstance(section, Mapping):
            continue
        for key in spec.fields:
            value = _mapping_value(section, key)
            if value is not _PANEL_MISSING:
                flat[key] = copy.deepcopy(value)

    # During an AstrBot schema upgrade both layouts can briefly coexist. Keep
    # the legacy value once; synchronization then moves it into the new group.
    for key in PANEL_DEFAULTS:
        if key in config:
            flat[key] = copy.deepcopy(config[key])
    return flat


def panel_value(panel: Mapping[str, Any], key: str) -> Any:
    """Read a panel field with its typed default."""
    return panel.get(key, PANEL_DEFAULTS[key])


def panel_changed(panel: Mapping[str, Any], key: str) -> bool:
    """Return whether a panel field carries an explicit, non-placeholder value."""
    if key not in panel:
        return False
    value = panel_value(panel, key)
    if key in PANEL_AUTO_SELECT_FIELDS and isinstance(value, (list, dict)):
        return bool(value)
    return not (
        key in PANEL_AUTO_SELECT_FIELDS and not str(value or "").strip()
    )


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def normalize_panel_values(panel: Mapping[str, Any]) -> dict:
    """Reset legacy/blank fixed defaults without retaining hidden JSON values."""
    result = copy.deepcopy(dict(panel))
    for key, value in result.items():
        if key not in PANEL_DEFAULTS or key in PANEL_AUTO_SELECT_FIELDS:
            continue
        default = PANEL_DEFAULTS[key]
        blank = _is_blank(value)
        legacy = (
            key in PANEL_TRISTATE_MODE_FIELDS
            and str(value).strip() == "inherit"
        ) or (key in PANEL_NONNEGATIVE_INT_FIELDS and str(value).strip() == "-1")
        if blank or legacy:
            result[key] = copy.deepcopy(default)
    return result


def synchronize_panel_config(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Move legacy flat panel values into groups and return runtime values."""
    flat = normalize_panel_values(flatten_panel_config(config))
    section = config.get("llm")
    section = section if isinstance(section, Mapping) else {}
    migrate_endpoint = (
        section.get("llm_endpoints_migrated") is not True
        and not flat.get("llm_endpoints")
        and any(key in section or key in config for key in LEGACY_ENDPOINT_FIELDS.values())
    )
    if migrate_endpoint:
        endpoint = {name: copy.deepcopy(flat.get(key, PANEL_DEFAULTS[key]))
                    for name, key in LEGACY_ENDPOINT_FIELDS.items()}
        endpoint["name"] = endpoint["name"] or flat.get("llm_endpoint_selector") or ""
        endpoint["__template_key"] = "endpoint"
        flat["llm_endpoints"] = [endpoint]
    legacy = {}
    for key, default in LEGACY_LLM_DEFAULTS.items():
        # AstrBot inserts empty root defaults on upgrades from grouped config.
        root = config.get(key, default)
        legacy[key] = root if root != default else section.get(key, default)
    migrated_llm = not flat.get("llm_models") and any(legacy.values())
    if migrated_llm:
        primary = str(
            legacy["llm_model"] or flat.get("llm_endpoint_model") or ""
        ).strip()
        candidates = [{
            "__template_key": "model",
            "name": str(legacy["llm_provider"] or "").strip(),
            "model": primary,
        }]
        for item in legacy["llm_fallback_models"]:
            name, sep, model = str(item).partition(":")
            if not sep or not name.strip() or not model.strip():
                raise ValueError(
                    f"旧配置 llm_fallback_models 格式无效：{item!r}；"
                    "需要端点名:模型名。"
                )
            candidates.append({
                "__template_key": "model", "name": name.strip(),
                "model": model.strip(),
            })
        flat["llm_models"] = candidates
    migrated_model_values = False
    model_fields = ("support_vision_mode", "support_json_mode_mode",
                    "model_max_tokens", "model_max_context_tokens")
    for endpoint in flat.get("llm_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        name = str(endpoint.get("name") or "").strip()
        model = str(endpoint.get("model") or "").strip()
        if not model:
            continue  # Legacy automatic selection depends on config.json.
        candidate = next((item for item in flat.get("llm_models") or []
                          if isinstance(item, dict)
                          and str(item.get("name") or "").strip() == name
                          and str(item.get("model") or "").strip() == model), None)
        if candidate is None:
            continue
        for key in model_fields:
            if key in endpoint:
                candidate.setdefault(key, endpoint.pop(key))
                migrated_model_values = True
    for key, default in PANEL_DEFAULTS.items():
        flat.setdefault(key, copy.deepcopy(default))
    flat = normalize_panel_values(flat)

    changed = False
    if migrated_llm or migrated_model_values or section.get("llm_migration_pending") is True:
        flat["_persist_llm_migration"] = True
    if migrate_endpoint or section.get("llm_endpoints_migration_pending") is True:
        flat["_persist_endpoint_migration"] = True
    if migrate_endpoint or section.get("llm_endpoints_migrated") is True or flat.get("llm_endpoints"):
        flat["_endpoints_configured"] = True
    # Reset only after producing the new array; do not resurrect old values
    # when the user later edits or clears the array.
    for key, default in LEGACY_LLM_DEFAULTS.items():
        if key in config and config[key] != default:
            config[key] = copy.deepcopy(default)
            changed = True
        if isinstance(section, dict) and key in section and section[key] != default:
            section[key] = copy.deepcopy(default)
            changed = True
    for key in PANEL_SCHEMA_GROUP_FIELDS:
        if key in config:
            del config[key]
            changed = True

    for group, spec in PANEL_SCHEMA_GROUPS.items():
        current = config.get(group)
        if isinstance(current, dict):
            section = current
        elif isinstance(current, Mapping):
            section = dict(current)
            config[group] = section
            changed = True
        else:
            section = {}
            config[group] = section
            changed = True
        if group == "llm" and (migrated_llm or migrated_model_values) and section.get("llm_migration_pending") is not True:
            section["llm_migration_pending"] = True
            changed = True
        if group == "llm":
            if section.get("llm_endpoints_migrated") is not True:
                section["llm_endpoints_migrated"] = True
                changed = True
            if migrate_endpoint and section.get("llm_endpoints_migration_pending") is not True:
                section["llm_endpoints_migration_pending"] = True
                changed = True
        for key in spec.fields:
            value = copy.deepcopy(flat[key])
            if section.get(key) != value:
                section[key] = value
                changed = True
    return flat, changed
