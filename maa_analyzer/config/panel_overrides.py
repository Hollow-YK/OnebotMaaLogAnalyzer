"""Typed adapters from flat AstrBot fields to sparse runtime patches.

Pydantic models own the boundary between the flat panel keys and the nested
runtime configuration. Each field keeps its runtime name; aliases identify the
corresponding AstrBot key. ``model_dump(exclude_unset=True)`` then produces a
sparse, nested patch without replacing advanced JSON values.
"""
from __future__ import annotations

from typing import (
    Annotated,
    Any,
    Iterator,
    Literal,
    Mapping,
    TypeVar,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo

from ..core.models import ModelRef


_ModelT = TypeVar("_ModelT", bound="PanelPatchModel")


def _validation_alias(field: FieldInfo) -> str | None:
    alias = field.validation_alias or field.alias
    if isinstance(alias, str):
        return alias
    return field.alias


def _panel_kind(field: FieldInfo) -> str | None:
    candidates = [field, *_annotation_metadata(field.annotation)]
    for candidate in candidates:
        if not isinstance(candidate, FieldInfo):
            continue
        extra = candidate.json_schema_extra
        if isinstance(extra, dict):
            value = extra.get("panel_kind")
            if isinstance(value, str):
                return value
    return None


def _panel_field_alias(name: str, field: FieldInfo) -> str:
    alias = _validation_alias(field)
    if isinstance(alias, str):
        return alias
    return name


def _mode_value(value: Any, *, allow_inherit: bool = False) -> bool:
    text = str(value or "inherit").strip().lower()
    allowed = {"inherit", "enabled", "disabled"} if allow_inherit else {
        "enabled",
        "disabled",
    }
    if text not in allowed:
        expected = "inherit、enabled 或 disabled" if allow_inherit else (
            "enabled 或 disabled"
        )
        raise ValueError(f"必须是 {expected}")
    if text == "inherit":
        return True
    return text == "enabled"


def _clean_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [
        str(item).strip()
        for item in value
        if str(item).strip()
    ]


def _unlimited_int(value: Any) -> Any:
    if str(value).strip() == "-1":
        return None
    return value


def _parse_model_refs(value: Any) -> list[ModelRef]:
    candidates: list[ModelRef] = []
    for raw in value or []:
        if isinstance(raw, Mapping):
            model = str(raw.get("model") or "").strip()
            name = str(raw.get("name") or "").strip()
        else:
            text = str(raw).strip()
            if not text:
                continue
            name, separator, model = text.partition(":")
            name = name.strip()
            model = model.strip()
            if not separator:
                model = name
                name = ""
        if not model:
            raise ValueError("每项必须包含模型名。")
        candidates.append(ModelRef(name=name, model=model))
    return candidates


def _literal_before(value: Any, allowed: set[str]) -> Any:
    text = str(value or "").strip().lower()
    if text not in allowed:
        choices = "、".join(sorted(item for item in allowed if item))
        raise ValueError(f"必须是 {choices}")
    return text


def _lower_text(value: Any, allowed: set[str]) -> Any:
    return _literal_before(value, allowed)


Mode = Annotated[
    bool,
    BeforeValidator(_mode_value),
    Field(json_schema_extra={"panel_kind": "mode"}),
]
StringList = Annotated[list[str], BeforeValidator(_clean_string_list)]
NonNegativeInt = Annotated[int, Field(ge=0)]
UnlimitedInt = Annotated[
    int | None,
    BeforeValidator(_unlimited_int),
]
ArchiveMatchMode = Annotated[
    str,
    BeforeValidator(
        lambda value: _literal_before(
            value, {"auto", "known", "prefix", "any"}
        )
    ),
]
ReportRenderMode = Annotated[
    str,
    BeforeValidator(
        lambda value: _lower_text(value, {"auto", "image", "text"})
    ),
]
VisionDetail = Annotated[
    str,
    BeforeValidator(
        lambda value: _lower_text(value, {"", "low", "high", "auto"})
    ),
]
LLMEndpointKind = Literal["openai", "anthropic"]


class PanelPatchModel(BaseModel):
    """Base configuration shared by all sparse panel adapters."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    @classmethod
    def from_panel(
        cls: type[_ModelT],
        panel: Mapping[str, Any],
    ) -> _ModelT:
        return cls.model_validate(panel)

    def sparse_dump(self) -> dict[str, Any]:
        """Dump only fields explicitly supplied by the panel."""
        return self.model_dump(exclude_unset=True)


def _settings_alias(name: str) -> str:
    return f"settings_{name}"


class AnalysisSettingsPatch(PanelPatchModel):
    """Sparse patch for ``bot.configs.<name>.settings``."""

    model_config = ConfigDict(alias_generator=_settings_alias)

    enabled: Mode | None = Field(
        default=None, validation_alias="enabled_mode"
    )
    file_list_delay_seconds: FiniteFloat | None = None
    duplicate_window_seconds: NonNegativeInt | None = None

    send_progress_message: Mode | None = Field(
        default=None,
        validation_alias="settings_send_progress_message_mode",
    )
    send_summary: Mode | None = Field(
        default=None,
        validation_alias="settings_send_summary_mode",
    )
    forward_analysis_results: Mode | None = Field(
        default=None,
        validation_alias="settings_forward_analysis_results_mode",
    )
    reply_chunk_chars: NonNegativeInt | None = None

    send_images: Mode | None = Field(
        default=None, validation_alias="send_images_mode"
    )
    max_report_images: NonNegativeInt | None = None
    send_repo_images: Mode | None = Field(
        default=None, validation_alias="send_repo_images_mode"
    )
    send_files: Mode | None = Field(
        default=None, validation_alias="send_files_mode"
    )
    max_report_files: NonNegativeInt | None = None
    max_file_attachment_mb: NonNegativeInt | None = None
    inline_attachments: Mode | None = Field(
        default=None, validation_alias="settings_inline_attachments_mode"
    )

    followup_enabled: Mode | None = Field(
        default=None, validation_alias="followup_mode"
    )
    followup_window_minutes: NonNegativeInt | None = Field(
        default=None, validation_alias="followup_window_minutes"
    )
    followup_max_turns: NonNegativeInt | None = None
    followup_max_sessions: NonNegativeInt | None = None

    history_enabled: Mode | None = Field(
        default=None, validation_alias="history_mode"
    )
    history_period_hours: NonNegativeInt | None = Field(
        default=None, validation_alias="history_period_hours"
    )
    history_keep_periods: NonNegativeInt | None = Field(
        default=None, validation_alias="history_keep_periods"
    )
    history_max_records: NonNegativeInt | None = None
    history_max_total_mb: NonNegativeInt | None = None

    download_concurrency: NonNegativeInt | None = None
    download_timeout_seconds: NonNegativeInt | None = None
    ai_stage_timeout_seconds: NonNegativeInt | None = None
    max_context_tokens: NonNegativeInt | None = None
    context_compact_enabled: Mode | None = Field(
        default=None,
        validation_alias="settings_context_compact_enabled_mode",
    )
    context_compact_ratio: FiniteFloat | None = None
    context_retain_ratio: FiniteFloat | None = None
    context_prune_tool_result_tokens: NonNegativeInt | None = None
    context_summary_tokens: NonNegativeInt | None = None
    analysis_cache_hours: NonNegativeInt | None = None
    circuit_breaker_enabled: Mode | None = Field(
        default=None,
        validation_alias="settings_circuit_breaker_enabled_mode",
    )

    system_prompt: str | None = None
    cleanup_after_done: Mode | None = Field(
        default=None, validation_alias="settings_cleanup_after_done_mode"
    )

    max_zip_size_mb: NonNegativeInt | None = None
    max_total_uncompressed_mb: NonNegativeInt | None = None
    merge_archive_parts: Mode | None = Field(
        default=None,
        validation_alias="settings_merge_archive_parts_mode",
    )
    archive_part_settle_seconds: FiniteFloat | None = None
    max_log_file_read_kb: NonNegativeInt | None = None


class CommandConfigPatch(PanelPatchModel):
    """Sparse patch for ``bot.configs.<name>.commands``."""

    enabled: Mode | None = Field(
        default=None, validation_alias="commands_mode"
    )
    prefix: str | None = Field(
        default=None, validation_alias="command_prefix"
    )
    owners: StringList | None = Field(
        default=None, validation_alias="owners"
    )
    admins: StringList | None = Field(
        default=None, validation_alias="admins"
    )
    read_level: NonNegativeInt | None = Field(
        default=None, validation_alias="commands_read_level"
    )
    write_level: NonNegativeInt | None = Field(
        default=None, validation_alias="commands_write_level"
    )
    followup_level: NonNegativeInt | None = Field(
        default=None, validation_alias="commands_followup_level"
    )
    reply_chunk_chars: NonNegativeInt | None = Field(
        default=None, validation_alias="commands_reply_chunk_chars"
    )
    max_list_items: NonNegativeInt | None = Field(
        default=None, validation_alias="commands_max_list_items"
    )


def _repo_alias(name: str) -> str:
    return f"repo_{name}"


class RepoConfigPatch(PanelPatchModel):
    """Sparse patch for ``project.repo``."""

    model_config = ConfigDict(alias_generator=_repo_alias)

    path: str | None = None
    url: str | None = None
    mirrors: StringList | None = None
    branch: str | None = None
    default_branch: str | None = None
    preload_enabled: Mode | None = Field(
        default=None, validation_alias="repo_preload_enabled_mode"
    )
    preload_retries: NonNegativeInt | None = None
    update_on_analyze: Mode | None = Field(
        default=None, validation_alias="repo_update_on_analyze_mode"
    )
    allow_ai_update: Mode | None = Field(
        default=None, validation_alias="repo_allow_ai_update_mode"
    )
    auto_checkout_version: Mode | None = Field(
        default=None, validation_alias="repo_auto_checkout_version_mode"
    )
    allow_ai_switch_tag: Mode | None = Field(
        default=None, validation_alias="repo_allow_ai_switch_tag_mode"
    )
    max_tag_switch_rounds: NonNegativeInt | None = None
    restore_latest_after_analyze: Mode | None = Field(
        default=None,
        validation_alias="repo_restore_latest_after_analyze_mode",
    )
    refuse_dirty_worktree: Mode | None = Field(
        default=None,
        validation_alias="repo_refuse_dirty_worktree_mode",
    )
    max_tool_rounds: NonNegativeInt | None = None
    max_tool_calls_per_round: NonNegativeInt | None = None
    max_tool_result_chars: NonNegativeInt | None = None
    max_tool_result_tokens: NonNegativeInt | None = None
    max_tool_context_tokens: NonNegativeInt | None = None
    agent_deadline_seconds: NonNegativeInt | None = None
    extensions: StringList | None = None
    max_file_kb: NonNegativeInt | None = None
    max_scan_files: NonNegativeInt | None = None


class ProjectUIPatch(PanelPatchModel):
    """Nested project UI patch produced from the flat ``ui_auto_mode`` key."""

    auto: dict[str, bool] | None = None


class ProjectConfigPatch(PanelPatchModel):
    """Sparse patch for ``project`` including its nested repository patch."""

    name: str | None = Field(
        default=None, validation_alias="project_name"
    )
    archive_match_mode: ArchiveMatchMode | None = Field(
        default=None, validation_alias="archive_match_mode"
    )
    file_prefix: str | None = Field(
        default=None, validation_alias="file_prefix"
    )
    require_log_layout: Mode | None = Field(
        default=None,
        validation_alias="require_log_layout_mode",
    )
    ui: ProjectUIPatch | None = Field(
        default=None,
        validation_alias="ui_auto_mode",
        json_schema_extra={"panel_kind": "mode"},
    )
    repo: RepoConfigPatch | None = None

    @field_validator("ui", mode="before")
    @classmethod
    def _build_ui_patch(cls, value: Any) -> Any:
        if value is None:
            return None
        return {"auto": {"enabled": _mode_value(value)}}

    @model_validator(mode="before")
    @classmethod
    def _group_repo_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            **value,
            "repo": RepoConfigPatch.from_panel(value),
        }


class ConfigLLMPatch(PanelPatchModel):
    """Sparse patch for the profile-level model candidates."""

    models: list[ModelRef] | None = Field(
        default=None,
        validation_alias="llm_models",
        exclude=True,
        json_schema_extra={"panel_kind": "template_list"},
    )
    max_tokens: UnlimitedInt | None = Field(
        default=None, validation_alias="llm_profile_max_tokens"
    )

    @field_validator("models", mode="before")
    @classmethod
    def _parse_models(cls, value: Any) -> Any:
        return _parse_model_refs(value)


def _vision_alias(name: str) -> str:
    return f"vision_{name}"


class VisionConfigPatch(PanelPatchModel):
    """Sparse patch for ``vision``."""

    model_config = ConfigDict(alias_generator=_vision_alias)

    enabled: Mode | None = Field(
        default=None, validation_alias="vision_mode"
    )
    max_images: NonNegativeInt | None = None
    max_image_kb: NonNegativeInt | None = None
    detail: VisionDetail | None = None
    attach_error_screenshots: Mode | None = Field(
        default=None,
        validation_alias="vision_attach_error_screenshots_mode",
    )
    attach_repo_templates: Mode | None = Field(
        default=None,
        validation_alias="vision_attach_repo_templates_mode",
    )
    allow_model_request: Mode | None = Field(
        default=None,
        validation_alias="vision_allow_model_request_mode",
    )


def _report_alias(name: str) -> str:
    return f"report_{name}"


class ReportConfigPatch(PanelPatchModel):
    """Sparse patch for ``report``."""

    model_config = ConfigDict(alias_generator=_report_alias)

    render: ReportRenderMode | None = Field(
        default=None, validation_alias="report_mode"
    )
    template: str | None = Field(
        default=None, validation_alias="report_template"
    )
    max_width_px: NonNegativeInt | None = None
    max_height_px: NonNegativeInt | None = None
    max_pages: NonNegativeInt | None = None
    device_scale_factor: FiniteFloat | None = None
    font_family: str | None = None
    browser_path: str | None = None
    reuse_browser: Mode | None = Field(
        default=None, validation_alias="report_reuse_browser_mode"
    )
    render_timeout_ms: NonNegativeInt | None = None
    embed_attachments: Mode | None = Field(
        default=None, validation_alias="report_embed_attachments_mode"
    )
    attach_markdown: Mode | None = Field(
        default=None, validation_alias="report_attach_markdown_mode"
    )
    fallback_to_text: Mode | None = Field(
        default=None, validation_alias="report_fallback_to_text_mode"
    )
    render_followup: Mode | None = Field(
        default=None, validation_alias="report_render_followup_mode"
    )


class ProfilePatch(PanelPatchModel):
    """Sparse patch for one runtime profile."""

    listen_groups: StringList | None = Field(
        default=None, validation_alias="listen_groups"
    )
    notify_group: str | None = Field(
        default=None, validation_alias="notify_group"
    )
    settings: AnalysisSettingsPatch | None = None
    project: ProjectConfigPatch | None = None
    commands: CommandConfigPatch | None = None
    llm: ConfigLLMPatch | None = None
    vision: VisionConfigPatch | None = None
    report: ReportConfigPatch | None = None

    @model_validator(mode="before")
    @classmethod
    def _group_profile_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            **value,
            "settings": AnalysisSettingsPatch.from_panel(value),
            "project": ProjectConfigPatch.from_panel(value),
            "commands": CommandConfigPatch.from_panel(value),
            "llm": ConfigLLMPatch.from_panel(value),
            "vision": VisionConfigPatch.from_panel(value),
            "report": ReportConfigPatch.from_panel(value),
        }

    def sparse_profile(self) -> dict[str, Any]:
        """Return only changed profile values, preserving empty overrides."""
        data = self.model_dump(exclude_unset=True)
        for key in (
            "settings",
            "project",
            "commands",
            "llm",
            "vision",
            "report",
        ):
            if data.get(key) == {}:
                data.pop(key)
        project = data.get("project")
        if isinstance(project, dict) and project.get("repo") == {}:
            project.pop("repo")
        return data


def _endpoint_alias(name: str) -> str:
    return f"llm_endpoint_{name}"


class LLMEndpointPatch(PanelPatchModel):
    """Sparse patch for one global LLM provider."""

    model_config = ConfigDict(alias_generator=_endpoint_alias)

    selector: str | None = None
    name: str | None = None
    kind: LLMEndpointKind | None = None
    base_url: str | None = None
    api_key: str | None = None
    max_concurrency: NonNegativeInt | None = None
    timeout_seconds: NonNegativeInt | None = None
    connect_timeout_seconds: NonNegativeInt | None = None
    max_retries: NonNegativeInt | None = None

    @property
    def payload_changed(self) -> bool:
        return bool(self.model_fields_set - {"selector", "name"})


class LLMModelPatch(PanelPatchModel):
    """Sparse patch for one model declaration on an LLM provider."""

    endpoint_model: str | None = Field(
        default=None, validation_alias="llm_endpoint_model"
    )
    supports_vision: Mode | None = Field(
        default=None,
        validation_alias="llm_model_support_vision_mode",
    )
    supports_json_mode: Mode | None = Field(
        default=None,
        validation_alias="llm_model_support_json_mode_mode",
    )
    max_tokens: NonNegativeInt | None = Field(
        default=None, validation_alias="llm_model_max_tokens"
    )
    max_context_tokens: NonNegativeInt | None = Field(
        default=None, validation_alias="llm_model_max_context_tokens"
    )

    @property
    def has_changes(self) -> bool:
        return bool(self.model_fields_set)

    @property
    def selected_model(self) -> str:
        return str(self.endpoint_model or "").strip()


class LLMDefaultsPatch(PanelPatchModel):
    """Sparse patch for ``llm.defaults``."""

    model_config = ConfigDict(alias_generator=lambda name: f"llm_defaults_{name}")

    max_tokens: NonNegativeInt | None = None
    max_context_tokens: NonNegativeInt | None = None


class LLMCircuitBreakerPatch(PanelPatchModel):
    """Sparse patch for ``llm.circuit_breaker``."""

    model_config = ConfigDict(alias_generator=lambda name: f"llm_circuit_{name}")

    failure_threshold: NonNegativeInt | None = None
    cooldown_seconds: NonNegativeInt | None = None
    rate_cooldown_seconds: NonNegativeInt | None = None
    auth_cooldown_seconds: NonNegativeInt | None = None


class GlobalLLMPatch(PanelPatchModel):
    """Sparse patch for global LLM declarations."""

    endpoint: LLMEndpointPatch | None = None
    model: LLMModelPatch | None = None
    defaults: LLMDefaultsPatch | None = None
    circuit_breaker: LLMCircuitBreakerPatch | None = None

    @model_validator(mode="before")
    @classmethod
    def _group_global_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            **value,
            "endpoint": LLMEndpointPatch.from_panel(value),
            "model": LLMModelPatch.from_panel(value),
            "defaults": LLMDefaultsPatch.from_panel(value),
            "circuit_breaker": LLMCircuitBreakerPatch.from_panel(value),
        }

    @property
    def has_changes(self) -> bool:
        return any(
            section is not None and bool(section.model_fields_set)
            for section in (
                self.endpoint,
                self.model,
                self.defaults,
                self.circuit_breaker,
            )
        )


class LoggingPatch(PanelPatchModel):
    """Sparse patch for the runtime ``log`` section."""

    log_level: str | None = None
    log_dir: str | None = None
    log_to_file: Mode | None = Field(
        default=None, validation_alias="log_to_file_mode"
    )


class UpdatePatch(PanelPatchModel):
    """Sparse patch for the runtime top-level ``update`` section."""

    check_enabled: Mode | None = Field(
        default=None, validation_alias="update_check_mode"
    )
    channel: str | None = Field(
        default=None, validation_alias="update_channel"
    )
    auto_update: Mode | None = Field(
        default=None, validation_alias="update_auto_mode"
    )
    download_mode: str | None = Field(
        default=None, validation_alias="update_download_mode"
    )
    update_templates: Mode | None = Field(
        default=None, validation_alias="update_templates_mode"
    )
    notify_qq: StringList | None = Field(
        default=None, validation_alias="update_notify_qq"
    )
    check_on_start: Mode | None = Field(
        default=None, validation_alias="update_check_on_start_mode"
    )
    check_interval_hours: NonNegativeInt | None = Field(
        default=None, validation_alias="update_check_interval_hours"
    )
    api_bases: StringList | None = Field(
        default=None, validation_alias="update_api_bases"
    )
    download_mirrors: StringList | None = Field(
        default=None, validation_alias="update_download_mirrors"
    )


class PanelPatch(PanelPatchModel):
    """Top-level typed view of the flat AstrBot panel."""

    default_config: str | None = None
    data_dir: str | None = None
    log: LoggingPatch | None = None
    llm: GlobalLLMPatch | None = None
    profile: ProfilePatch | None = None
    update: UpdatePatch | None = None

    @model_validator(mode="before")
    @classmethod
    def _group_panel_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            **value,
            "log": LoggingPatch.from_panel(value),
            "llm": GlobalLLMPatch.from_panel(value),
            "profile": ProfilePatch.from_panel(value),
            "update": UpdatePatch.from_panel(value),
        }


def _aliases_from_model(model: type[PanelPatchModel]) -> set[str]:
    return {
        _panel_field_alias(name, field)
        for name, field in model.model_fields.items()
    }


def _annotation_contains(annotation: Any, expected: type[Any]) -> bool:
    if annotation is expected:
        return True
    if get_origin(annotation) is Annotated:
        return _annotation_contains(annotation.__origin__, expected)
    return any(
        _annotation_contains(item, expected) for item in get_args(annotation)
    )


def _annotation_metadata(annotation: Any) -> Iterator[Any]:
    if get_origin(annotation) is Annotated:
        yield from annotation.__metadata__
        yield from _annotation_metadata(annotation.__origin__)
        return
    for item in get_args(annotation):
        yield from _annotation_metadata(item)


def _annotation_has_nonnegative_bound(annotation: Any) -> bool:
    for metadata in _annotation_metadata(annotation):
        if getattr(metadata, "ge", None) == 0:
            return True
        if isinstance(metadata, FieldInfo):
            if any(getattr(item, "ge", None) == 0 for item in metadata.metadata):
                return True
    return False


def _mode_fields() -> set[str]:
    fields: set[str] = set()
    for model in (
        AnalysisSettingsPatch,
        CommandConfigPatch,
        RepoConfigPatch,
        ProjectConfigPatch,
        VisionConfigPatch,
        ReportConfigPatch,
        LLMModelPatch,
        LoggingPatch,
        UpdatePatch,
    ):
        for name, field in model.model_fields.items():
            if _panel_kind(field) == "mode":
                fields.add(_panel_field_alias(name, field))
    return fields


def _nonnegative_int_fields() -> set[str]:
    fields: set[str] = set()
    for model in (
        AnalysisSettingsPatch,
        CommandConfigPatch,
        RepoConfigPatch,
        VisionConfigPatch,
        ReportConfigPatch,
        LLMEndpointPatch,
        LLMModelPatch,
        LLMDefaultsPatch,
        LLMCircuitBreakerPatch,
        UpdatePatch,
    ):
        for name, field in model.model_fields.items():
            if not _annotation_contains(field.annotation, int):
                continue
            if _annotation_has_nonnegative_bound(field.annotation):
                fields.add(_panel_field_alias(name, field))
    return fields


PANEL_TRISTATE_MODE_FIELDS = frozenset(_mode_fields())
PANEL_NONNEGATIVE_INT_FIELDS = frozenset(_nonnegative_int_fields())
PANEL_PROFILE_FIELDS = (
    {"listen_groups", "notify_group"}
    | _aliases_from_model(AnalysisSettingsPatch)
    | _aliases_from_model(CommandConfigPatch)
    | (_aliases_from_model(ProjectConfigPatch) - {"repo"})
    | _aliases_from_model(RepoConfigPatch)
    | _aliases_from_model(ConfigLLMPatch)
    | _aliases_from_model(VisionConfigPatch)
    | _aliases_from_model(ReportConfigPatch)
)
PANEL_LLM_ENDPOINT_FIELDS = frozenset(_aliases_from_model(LLMEndpointPatch))
PANEL_LLM_MODEL_FIELDS = frozenset(_aliases_from_model(LLMModelPatch))
PANEL_LLM_GLOBAL_FIELDS = frozenset(
    _aliases_from_model(LLMDefaultsPatch)
    | _aliases_from_model(LLMCircuitBreakerPatch)
)


def validate_panel_patch(panel: Mapping[str, Any]) -> PanelPatch:
    """Validate all typed sections and return the sparse top-level patch."""
    try:
        return PanelPatch.from_panel(panel)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        field = ".".join(str(part) for part in error["loc"])
        raise ValueError(f"{field} {error['msg']}") from exc


__all__ = [
    "AnalysisSettingsPatch",
    "CommandConfigPatch",
    "ConfigLLMPatch",
    "GlobalLLMPatch",
    "LLMCircuitBreakerPatch",
    "LLMDefaultsPatch",
    "LLMEndpointPatch",
    "LLMModelPatch",
    "LoggingPatch",
    "PANEL_LLM_ENDPOINT_FIELDS",
    "PANEL_LLM_GLOBAL_FIELDS",
    "PANEL_LLM_MODEL_FIELDS",
    "PANEL_NONNEGATIVE_INT_FIELDS",
    "PANEL_PROFILE_FIELDS",
    "PANEL_TRISTATE_MODE_FIELDS",
    "PanelPatch",
    "PanelPatchModel",
    "ProfilePatch",
    "ProjectConfigPatch",
    "ReportConfigPatch",
    "RepoConfigPatch",
    "UpdatePatch",
    "VisionConfigPatch",
    "validate_panel_patch",
]
