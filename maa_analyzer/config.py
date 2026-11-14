"""Shared configuration loading, defaults, validation, and panel overlays."""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel

from .core.llm_pool import detect_legacy_llm, validate_llm_config
from .core.log_context import ConfigNameFormatter
from .core.models import (
    AnalysisSettings, CommandConfig, ConfigLLM, LLMGlobalConfig,
    ProjectConfig, ReportConfig, VisionConfig,
)

logger = logging.getLogger("Maa")

LOG_FORMAT = "%(asctime)s [%(levelname)s]%(config_prefix)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BOOTSTRAP_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
ONEBOT_MODES = ("ws", "http_ws", "ws_reverse", "http_ws_reverse")
# Panel keys which use inherit / enabled / disabled instead of a raw boolean.
PANEL_BASE_TRISTATE_MODE_FIELDS = {
    "enabled_mode",
    "require_log_layout_mode",
    "ui_auto_mode",
    "vision_mode",
    "send_images_mode",
    "send_repo_images_mode",
    "send_files_mode",
    "followup_mode",
    "history_mode",
    "commands_mode",
    "log_to_file_mode",
    "llm_model_support_vision_mode",
    "llm_model_support_json_mode_mode",
}
PANEL_NONNEGATIVE_INT_FIELDS = {
    "followup_window_minutes",
    "history_period_hours",
    "history_keep_periods",
    "commands_read_level",
    "commands_write_level",
    "commands_followup_level",
}

# Declarative profile overrides. Paths are relative to
# bot.configs.<name>. Flat keys are required by AstrBot's plugin schema.
PANEL_PROFILE_STRING_OVERRIDES: dict[str, tuple[str, ...]] = {
    "report_font_family": ("report", "font_family"),
    "report_browser_path": ("report", "browser_path"),
    "repo_path": ("project", "repo", "path"),
    "repo_url": ("project", "repo", "url"),
    "repo_branch": ("project", "repo", "branch"),
    "repo_default_branch": ("project", "repo", "default_branch"),
    "settings_system_prompt": ("settings", "system_prompt"),
}
PANEL_PROFILE_FLOAT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "report_device_scale_factor": ("report", "device_scale_factor"),
    "settings_file_list_delay_seconds": ("settings", "file_list_delay_seconds"),
    "settings_archive_part_settle_seconds": (
        "settings", "archive_part_settle_seconds"
    ),
    "settings_context_compact_ratio": (
        "settings", "context_compact_ratio"
    ),
    "settings_context_retain_ratio": ("settings", "context_retain_ratio"),
}
PANEL_PROFILE_INT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "vision_max_images": ("vision", "max_images"),
    "vision_max_image_kb": ("vision", "max_image_kb"),
    "report_max_width_px": ("report", "max_width_px"),
    "report_max_height_px": ("report", "max_height_px"),
    "report_max_pages": ("report", "max_pages"),
    "report_render_timeout_ms": ("report", "render_timeout_ms"),
    "commands_reply_chunk_chars": ("commands", "reply_chunk_chars"),
    "commands_max_list_items": ("commands", "max_list_items"),
    "repo_preload_retries": ("project", "repo", "preload_retries"),
    "repo_max_tag_switch_rounds": ("project", "repo", "max_tag_switch_rounds"),
    "repo_max_tool_rounds": ("project", "repo", "max_tool_rounds"),
    "repo_max_tool_calls_per_round": (
        "project", "repo", "max_tool_calls_per_round"
    ),
    "repo_max_tool_result_chars": (
        "project", "repo", "max_tool_result_chars"
    ),
    "repo_max_tool_result_tokens": (
        "project", "repo", "max_tool_result_tokens"
    ),
    "repo_max_tool_context_tokens": (
        "project", "repo", "max_tool_context_tokens"
    ),
    "repo_agent_deadline_seconds": ("project", "repo", "agent_deadline_seconds"),
    "repo_max_file_kb": ("project", "repo", "max_file_kb"),
    "repo_max_scan_files": ("project", "repo", "max_scan_files"),
    "repo_max_files": ("project", "repo", "max_files"),
    "repo_max_identifiers": ("project", "repo", "max_identifiers"),
    "repo_context_lines": ("project", "repo", "context_lines"),
    "repo_max_block_lines": ("project", "repo", "max_block_lines"),
    "repo_max_chars": ("project", "repo", "max_chars"),
    "settings_reply_chunk_chars": ("settings", "reply_chunk_chars"),
    "settings_max_report_images": ("settings", "max_report_images"),
    "settings_max_report_files": ("settings", "max_report_files"),
    "settings_max_file_attachment_mb": ("settings", "max_file_attachment_mb"),
    "settings_followup_max_turns": ("settings", "followup_max_turns"),
    "settings_followup_max_sessions": ("settings", "followup_max_sessions"),
    "settings_history_max_records": ("settings", "history_max_records"),
    "settings_history_max_total_mb": ("settings", "history_max_total_mb"),
    "settings_download_concurrency": ("settings", "download_concurrency"),
    "settings_download_timeout_seconds": ("settings", "download_timeout_seconds"),
    "settings_digest_timeout_seconds": ("settings", "digest_timeout_seconds"),
    "settings_ai_stage_timeout_seconds": ("settings", "ai_stage_timeout_seconds"),
    "settings_max_context_tokens": ("settings", "max_context_tokens"),
    "settings_context_prune_tool_result_tokens": ("settings", "context_prune_tool_result_tokens"),
    "settings_context_summary_tokens": ("settings", "context_summary_tokens"),
    "settings_analysis_cache_hours": ("settings", "analysis_cache_hours"),
    "settings_max_zip_size_mb": ("settings", "max_zip_size_mb"),
    "settings_max_total_uncompressed_mb": ("settings", "max_total_uncompressed_mb"),
    "settings_max_prompt_chars": ("settings", "max_prompt_chars"),
    "settings_max_zip_members": ("settings", "max_zip_members"),
    "settings_max_log_files": ("settings", "max_log_files"),
    "settings_max_maafw_bak_files": ("settings", "max_maafw_bak_files"),
    "settings_max_maafw_bak_mb": ("settings", "max_maafw_bak_mb"),
    "settings_max_log_file_read_kb": ("settings", "max_log_file_read_kb"),
    "settings_small_log_full_read_mb": ("settings", "small_log_full_read_mb"),
    "settings_large_log_threshold_mb": ("settings", "large_log_threshold_mb"),
    "settings_max_large_log_read_kb": ("settings", "max_large_log_read_kb"),
    "settings_max_stream_log_mb": ("settings", "max_stream_log_mb"),
    "settings_max_sections_per_log": ("settings", "max_sections_per_log"),
    "settings_max_log_chars": ("settings", "max_log_chars"),
    "settings_max_recognition_evidence_lines": ("settings", "max_recognition_evidence_lines"),
    "settings_max_recognition_evidence_chars": ("settings", "max_recognition_evidence_chars"),
    "settings_log_head_lines": ("settings", "log_head_lines"),
    "settings_duplicate_window_seconds": ("settings", "duplicate_window_seconds"),
    "llm_profile_max_tokens": ("llm", "max_tokens"),
}
PANEL_PROFILE_LIST_OVERRIDES: dict[str, tuple[str, ...]] = {
    "repo_mirrors": ("project", "repo", "mirrors"),
    "repo_extensions": ("project", "repo", "extensions"),
}
PANEL_PROFILE_MODE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "vision_attach_error_screenshots_mode": (
        "vision", "attach_error_screenshots"
    ),
    "vision_attach_repo_templates_mode": (
        "vision", "attach_repo_templates"
    ),
    "vision_allow_model_request_mode": (
        "vision", "allow_model_request"
    ),
    "report_reuse_browser_mode": ("report", "reuse_browser"),
    "report_embed_attachments_mode": ("report", "embed_attachments"),
    "report_attach_markdown_mode": ("report", "attach_markdown"),
    "report_fallback_to_text_mode": ("report", "fallback_to_text"),
    "report_render_followup_mode": ("report", "render_followup"),
    "repo_preload_enabled_mode": ("project", "repo", "preload_enabled"),
    "repo_update_on_analyze_mode": ("project", "repo", "update_on_analyze"),
    "repo_allow_ai_update_mode": ("project", "repo", "allow_ai_update"),
    "repo_auto_checkout_version_mode": (
        "project", "repo", "auto_checkout_version"
    ),
    "repo_allow_ai_switch_tag_mode": (
        "project", "repo", "allow_ai_switch_tag"
    ),
    "repo_restore_latest_after_analyze_mode": (
        "project", "repo", "restore_latest_after_analyze"
    ),
    "repo_refuse_dirty_worktree_mode": (
        "project", "repo", "refuse_dirty_worktree"
    ),
    "settings_send_progress_message_mode": ("settings", "send_progress_message"),
    "settings_send_summary_mode": ("settings", "send_summary"),
    "settings_forward_analysis_results_mode": ("settings", "forward_analysis_results"),
    "settings_inline_attachments_mode": ("settings", "inline_attachments"),
    "settings_context_compact_enabled_mode": ("settings", "context_compact_enabled"),
    "settings_circuit_breaker_enabled_mode": ("settings", "circuit_breaker_enabled"),
    "settings_log_tools_enabled_mode": ("settings", "log_tools_enabled"),
    "settings_cleanup_after_done_mode": ("settings", "cleanup_after_done"),
    "settings_save_digest_debug_mode": ("settings", "save_digest_debug"),
    "settings_merge_archive_parts_mode": ("settings", "merge_archive_parts"),
    "settings_stream_medium_logs_mode": ("settings", "stream_medium_logs"),
    "settings_stream_large_logs_mode": ("settings", "stream_large_logs"),
    "settings_compress_context_noise_mode": ("settings", "compress_context_noise"),
}
PANEL_PROFILE_OPTION_OVERRIDES: dict[str, tuple[str, ...]] = {
    "vision_detail": ("vision", "detail"),
    "repo_search_mode": ("project", "repo", "mode"),
}
PANEL_PROFILE_OPTION_VALUES: dict[str, set[str]] = {
    "vision_detail": {"", "low", "high", "auto"},
    "repo_search_mode": {"", "inject", "agent"},
}
PANEL_LLM_ENDPOINT_FIELDS = {
    "llm_endpoint_selector",
    "llm_endpoint_name",
    "llm_endpoint_kind",
    "llm_endpoint_base_url",
    "llm_endpoint_api_key",
    "llm_endpoint_max_concurrency",
    "llm_endpoint_timeout_seconds",
    "llm_endpoint_connect_timeout_seconds",
    "llm_endpoint_max_retries",
}
PANEL_LLM_ENDPOINT_INT_FIELDS = {
    "llm_endpoint_max_concurrency",
    "llm_endpoint_timeout_seconds",
    "llm_endpoint_connect_timeout_seconds",
    "llm_endpoint_max_retries",
}
PANEL_LLM_MODEL_FIELDS = {
    "llm_endpoint_model",
    "llm_model_support_vision_mode",
    "llm_model_support_json_mode_mode",
    "llm_model_max_tokens",
    "llm_model_max_context_tokens",
}
PANEL_LLM_GLOBAL_FIELDS = {
    "llm_defaults_max_tokens",
    "llm_defaults_max_context_tokens",
    "llm_circuit_failure_threshold",
    "llm_circuit_cooldown_seconds",
    "llm_circuit_rate_cooldown_seconds",
    "llm_circuit_auth_cooldown_seconds",
}
PANEL_TRISTATE_MODE_FIELDS = (
    PANEL_BASE_TRISTATE_MODE_FIELDS | set(PANEL_PROFILE_MODE_OVERRIDES)
)
PANEL_NONNEGATIVE_INT_FIELDS |= set(PANEL_PROFILE_INT_OVERRIDES)
PANEL_NONNEGATIVE_INT_FIELDS |= (
    PANEL_LLM_ENDPOINT_INT_FIELDS
    | {
        "llm_model_max_tokens",
        "llm_model_max_context_tokens",
        "llm_defaults_max_tokens",
        "llm_defaults_max_context_tokens",
        "llm_circuit_failure_threshold",
        "llm_circuit_cooldown_seconds",
        "llm_circuit_rate_cooldown_seconds",
        "llm_circuit_auth_cooldown_seconds",
    }
)

PANEL_DEFAULTS: dict[str, Any] = {
    "enabled_mode": 'enabled',
    "config_file": "",
    "data_dir": "data",
    "default_config": "",
    "listen_groups": [],
    "notify_group": "",
    "command_prefix": '/maa',
    "owners": [],
    "admins": [],
    "report_mode": 'auto',
    "report_template": 'default',
    "project_name": "",
    "archive_match_mode": 'auto',
    "file_prefix": "",
    "require_log_layout_mode": 'enabled',
    "ui_auto_mode": 'enabled',
    "llm_provider": "",
    "llm_model": "",
    "llm_fallback_models": [],
    "llm_profile_max_tokens": -1,
    "llm_endpoint_selector": "",
    "llm_endpoint_name": "",
    "llm_endpoint_kind": 'openai',
    "llm_endpoint_base_url": "https://api.openai.com/v1",
    "llm_endpoint_api_key": "",
    "llm_endpoint_max_concurrency": 2,
    "llm_endpoint_timeout_seconds": 180,
    "llm_endpoint_connect_timeout_seconds": 10,
    "llm_endpoint_max_retries": 2,
    "llm_endpoint_model": "",
    "llm_model_support_vision_mode": 'disabled',
    "llm_model_support_json_mode_mode": 'disabled',
    "llm_model_max_tokens": 0,
    "llm_model_max_context_tokens": 0,
    "llm_defaults_max_tokens": 8192,
    "llm_defaults_max_context_tokens": 48000,
    "llm_circuit_failure_threshold": 3,
    "llm_circuit_cooldown_seconds": 300,
    "llm_circuit_rate_cooldown_seconds": 60,
    "llm_circuit_auth_cooldown_seconds": 1800,
    "vision_mode": 'enabled',
    "send_images_mode": 'enabled',
    "send_repo_images_mode": 'enabled',
    "send_files_mode": 'enabled',
    "followup_mode": 'enabled',
    "followup_window_minutes": 0,
    "history_mode": 'enabled',
    "history_period_hours": 24,
    "history_keep_periods": 2,
    "commands_mode": 'enabled',
    "commands_read_level": 0,
    "commands_write_level": 2,
    "commands_followup_level": 0,
    "log_to_file_mode": 'enabled',
    "log_dir": 'logs',
    "log_level": 'INFO',
    "vision_max_images": 4,
    "vision_max_image_kb": 800,
    "vision_detail": 'high',
    "vision_attach_error_screenshots_mode": 'enabled',
    "vision_attach_repo_templates_mode": 'enabled',
    "vision_allow_model_request_mode": 'enabled',
    "report_max_width_px": 1080,
    "report_max_height_px": 4096,
    "report_max_pages": 6,
    "report_device_scale_factor": '1.0',
    "report_font_family": '',
    "report_browser_path": '',
    "report_reuse_browser_mode": 'enabled',
    "report_render_timeout_ms": 8000,
    "report_embed_attachments_mode": 'enabled',
    "report_attach_markdown_mode": 'disabled',
    "report_fallback_to_text_mode": 'enabled',
    "report_render_followup_mode": 'enabled',
    "commands_reply_chunk_chars": 3500,
    "commands_max_list_items": 40,
    "repo_search_mode": 'inject',
    "repo_path": '',
    "repo_url": 'https://github.com/MAAXYZ/MaaXXX',
    "repo_mirrors": [],
    "repo_branch": '',
    "repo_default_branch": '',
    "repo_preload_enabled_mode": 'enabled',
    "repo_preload_retries": 3,
    "repo_update_on_analyze_mode": 'disabled',
    "repo_allow_ai_update_mode": 'enabled',
    "repo_auto_checkout_version_mode": 'enabled',
    "repo_allow_ai_switch_tag_mode": 'enabled',
    "repo_max_tag_switch_rounds": 2,
    "repo_restore_latest_after_analyze_mode": 'enabled',
    "repo_refuse_dirty_worktree_mode": 'enabled',
    "repo_max_tool_rounds": 8,
    "repo_max_tool_calls_per_round": 6,
    "repo_max_tool_result_chars": 30000,
    "repo_max_tool_result_tokens": 0,
    "repo_max_tool_context_tokens": 0,
    "repo_agent_deadline_seconds": 600,
    "repo_extensions": ['.json', '.jsonc', '.yaml', '.yml', '.toml', '.md', '.txt'],
    "repo_max_file_kb": 256,
    "repo_max_scan_files": 20000,
    "repo_max_files": 12,
    "repo_max_identifiers": 24,
    "repo_context_lines": 2,
    "repo_max_block_lines": 150,
    "repo_max_chars": 60000,
    "settings_reply_chunk_chars": 3500,
    "settings_max_report_images": 4,
    "settings_max_report_files": 2,
    "settings_max_file_attachment_mb": 20,
    "settings_followup_max_turns": 10,
    "settings_followup_max_sessions": 50,
    "settings_history_max_records": 200,
    "settings_history_max_total_mb": 4096,
    "settings_download_concurrency": 2,
    "settings_download_timeout_seconds": 120,
    "settings_digest_timeout_seconds": 0,
    "settings_ai_stage_timeout_seconds": 300,
    "settings_max_context_tokens": 0,
    "settings_context_prune_tool_result_tokens": 512,
    "settings_context_summary_tokens": 1500,
    "settings_analysis_cache_hours": 0,
    "settings_max_zip_size_mb": 100,
    "settings_max_total_uncompressed_mb": 1024,
    "settings_max_prompt_chars": 200000,
    "settings_max_zip_members": 300,
    "settings_max_log_files": 6,
    "settings_max_maafw_bak_files": 1,
    "settings_max_maafw_bak_mb": 64,
    "settings_max_log_file_read_kb": 512,
    "settings_small_log_full_read_mb": 5,
    "settings_large_log_threshold_mb": 50,
    "settings_max_large_log_read_kb": 256,
    "settings_max_stream_log_mb": 0,
    "settings_max_sections_per_log": 60,
    "settings_max_log_chars": 120000,
    "settings_max_recognition_evidence_lines": 60,
    "settings_max_recognition_evidence_chars": 35000,
    "settings_log_head_lines": 16,
    "settings_duplicate_window_seconds": 120,
    "settings_send_progress_message_mode": 'enabled',
    "settings_send_summary_mode": 'enabled',
    "settings_forward_analysis_results_mode": 'disabled',
    "settings_inline_attachments_mode": 'enabled',
    "settings_context_compact_enabled_mode": 'enabled',
    "settings_circuit_breaker_enabled_mode": 'enabled',
    "settings_log_tools_enabled_mode": 'enabled',
    "settings_cleanup_after_done_mode": 'enabled',
    "settings_save_digest_debug_mode": 'disabled',
    "settings_merge_archive_parts_mode": 'enabled',
    "settings_stream_medium_logs_mode": 'enabled',
    "settings_stream_large_logs_mode": 'enabled',
    "settings_compress_context_noise_mode": 'enabled',
    "settings_file_list_delay_seconds": '0.0',
    "settings_archive_part_settle_seconds": '30.0',
    "settings_context_compact_ratio": '0.8',
    "settings_context_retain_ratio": '0.25',
    "settings_system_prompt": '',
}

PANEL_PROFILE_FIELDS = (
    {
        "enabled_mode",
        "default_config",
        "listen_groups",
        "notify_group",
        "command_prefix",
        "owners",
        "admins",
        "report_mode",
        "report_template",
        "project_name",
        "archive_match_mode",
        "file_prefix",
        "require_log_layout_mode",
        "ui_auto_mode",
        "llm_provider",
        "llm_model",
        "llm_fallback_models",
        "vision_mode",
        "send_images_mode",
        "send_repo_images_mode",
        "send_files_mode",
        "followup_mode",
        "followup_window_minutes",
        "history_mode",
        "history_period_hours",
        "history_keep_periods",
        "commands_mode",
        "commands_read_level",
        "commands_write_level",
        "commands_followup_level",
    }
    | set(PANEL_PROFILE_STRING_OVERRIDES)
    | set(PANEL_PROFILE_FLOAT_OVERRIDES)
    | set(PANEL_PROFILE_INT_OVERRIDES)
    | set(PANEL_PROFILE_LIST_OVERRIDES)
    | set(PANEL_PROFILE_MODE_OVERRIDES)
    | set(PANEL_PROFILE_OPTION_OVERRIDES)
)


def default_config() -> dict:
    """Return a fresh copy of the first-run configuration."""
    return {
        "onebot": {
            "mode": "http_ws",
            "http_url": "http://127.0.0.1:3000",
            "ws_url": "ws://127.0.0.1:3001",
            "ws_reverse_port": 8080,
            "ws_reverse_host": "127.0.0.1",
            "access_token": "",
            "api_timeout_seconds": 30,
        },
        "bot": {
            "data_dir": "data",
            "configs": {
                "默认": {
                    "listen_groups": [],
                    "notify_group": "",
                    "project": {
                        "name": "MaaXXX",
                        "archive_match_mode": "auto",
                        "require_log_layout": True,
                        "repo": {
                            "mode": "inject",
                            "path": "",
                            "url": "https://github.com/MAAXYZ/MaaXXX",
                            "mirrors": [],
                            "branch": "",
                            "preload_enabled": True,
                            "preload_retries": 3,
                            "update_on_analyze": False,
                            "allow_ai_update": True,
                            "auto_checkout_version": True,
                            "allow_ai_switch_tag": True,
                            "restore_latest_after_analyze": True,
                        },
                    },
                    "llm": {
                        "models": [
                            {"name": "default", "model": "gpt-4o-mini"},
                        ],
                    },
                    "vision": {
                        "enabled": True,
                        "max_images": 4,
                        "max_image_kb": 800,
                        "detail": "high",
                        "attach_error_screenshots": True,
                        "attach_repo_templates": True,
                        "allow_model_request": True,
                    },
                    "report": {
                        "render": "auto",
                        "template": "default",
                        "max_width_px": 1080,
                        "max_height_px": 4096,
                        "max_pages": 6,
                        "device_scale_factor": 1,
                        "font_family": "",
                        "browser_path": "",
                        "reuse_browser": True,
                        "render_timeout_ms": 8000,
                        "embed_attachments": True,
                        "attach_markdown": False,
                        "fallback_to_text": True,
                        "render_followup": True,
                    },
                    "settings": {
                        "send_images": True,
                        "max_report_images": 4,
                        "send_repo_images": True,
                        "send_files": True,
                        "max_report_files": 2,
                        "max_file_attachment_mb": 20,
                        "followup_enabled": True,
                        "followup_window_minutes": 0,
                        "history_enabled": True,
                        "history_period_hours": 24,
                        "history_keep_periods": 2,
                        "ai_stage_timeout_seconds": 300,
                        "max_context_tokens": 0,
                    },
                    "commands": {
                        "enabled": True,
                        "prefix": "/maa",
                        "owners": [],
                        "admins": [],
                        "read_level": 0,
                        "write_level": 2,
                        "followup_level": 0,
                    },
                }
            },
        },
        "llm": {
            "providers": [
                {
                    "name": "default",
                    "kind": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "max_concurrency": 2,
                    "timeout_seconds": 180,
                    "connect_timeout_seconds": 10,
                    "max_retries": 2,
                    "models": [
                        {
                            "model": "gpt-4o-mini",
                            "supports": {
                                "vision": True,
                                "json_mode": True,
                            },
                            "max_tokens": 8192,
                            "max_context_tokens": 48000,
                        }
                    ],
                }
            ],
            "defaults": {
                "max_tokens": 8192,
                "max_context_tokens": 48000,
            },
            "circuit_breaker": {
                "failure_threshold": 3,
                "cooldown_seconds": 300,
                "rate_cooldown_seconds": 60,
                "auth_cooldown_seconds": 1800,
            },
        },
        "debug": {
            "enabled": False,
            "http_port": 8765,
            "data_dir": "data/test",
        },
        "log": {
            "log_to_file": True,
            "log_level": "INFO",
            "log_dir": "logs",
        },
    }


def write_default_config(path: str | Path) -> Path:
    """Create a complete default config if it does not already exist."""
    target = Path(path)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def load_config_file(path: str | Path) -> dict:
    """Load a JSON object and reject malformed top-level values."""
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config.json 顶层必须是对象。")
    return data


def validate_common_config(cfg: Mapping[str, Any]) -> list[str]:
    """Validate transport-neutral fields shared by both entry points."""
    errors: list[str] = []
    bot_cfg = cfg.get("bot")
    if not isinstance(bot_cfg, dict):
        errors.append("bot 段必须是对象。")
        bot_cfg = {}
    if not str(bot_cfg.get("data_dir", "data") or "").strip():
        errors.append("bot.data_dir 不能为空。")
    configs = bot_cfg.get("configs")
    if configs is None:
        configs = {}
    if not isinstance(configs, dict):
        errors.append("bot.configs 必须是对象。")
        configs = {}
    legacy = detect_legacy_llm(cfg.get("llm") or {})
    if legacy:
        errors.append(legacy)
    else:
        errors.extend(validate_llm_config(cfg.get("llm") or {}, configs))
    return errors


def validate_onebot_config(cfg: Mapping[str, Any]) -> str:
    """Validate a bare OneBot connection block and return its mode."""
    ob = cfg.get("onebot") or {}
    if not isinstance(ob, dict):
        raise ValueError("onebot 段必须是对象。")
    mode = str(ob.get("mode", "http_ws"))
    if mode not in ONEBOT_MODES:
        raise ValueError(
            f"未知模式: {mode}，可选: {', '.join(ONEBOT_MODES)}"
        )

    def check_url(key: str, schemes: tuple[str, ...], required: bool) -> str:
        raw = str(ob.get(key, "") or "").strip()
        if not raw:
            if required:
                raise ValueError(f"onebot.mode={mode} 需要设置 onebot.{key}")
            return ""
        parts = urlsplit(raw)
        if parts.scheme not in schemes or not parts.hostname:
            raise ValueError(
                f"onebot.{key} 不是合法的 {schemes[0]} 地址: {raw!r}"
            )
        try:
            parts.port
        except ValueError as exc:
            raise ValueError(f"onebot.{key} 端口非法: {raw!r}") from exc
        return raw

    if mode in ("http_ws", "http_ws_reverse"):
        check_url("http_url", ("http", "https"), True)
    if mode in ("ws", "http_ws"):
        check_url("ws_url", ("ws", "wss"), True)
    if mode in ("ws_reverse", "http_ws_reverse"):
        try:
            port = int(ob.get("ws_reverse_port", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("onebot.ws_reverse_port 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise ValueError(
                f"onebot.mode={mode} 需要 1-65535 的 ws_reverse_port，"
                f"当前为 {port}"
            )
        if not str(ob.get("ws_reverse_host", "127.0.0.1") or "").strip():
            raise ValueError("onebot.ws_reverse_host 不能为空")
    return mode


def resolve_data_dir(
    cfg: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    override: str | Path | None = None,
) -> Path:
    """Resolve the configured data directory against an optional base."""
    bot_cfg = cfg.get("bot") or {}
    raw = override or bot_cfg.get("data_dir", "data")
    path = Path(str(raw or "data")).expanduser()
    if not path.is_absolute():
        path = Path(base_dir or ".") / path
    return path.resolve()


def _panel_value(panel: Mapping[str, Any], key: str) -> Any:
    return panel.get(key, PANEL_DEFAULTS[key])


PANEL_AUTO_SELECT_FIELDS = {
    "config_file", "default_config", "llm_endpoint_selector",
    "llm_endpoint_name", "llm_endpoint_model",
}


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


def _is_inherit_value(key: str, value: Any) -> bool:
    """Only selectors may use blank values to select an existing object."""
    return key in PANEL_AUTO_SELECT_FIELDS and not str(value or "").strip()


def _changed(panel: Mapping[str, Any], key: str) -> bool:
    return key in panel and not _is_inherit_value(key, _panel_value(panel, key))


def _target_profile(cfg: dict, panel: Mapping[str, Any]) -> str:
    bot_cfg = cfg.setdefault("bot", {})
    configs = bot_cfg.setdefault("configs", {})
    if not isinstance(configs, dict):
        configs = {}
        bot_cfg["configs"] = configs
    requested = str(_panel_value(panel, "default_config") or "").strip()
    if requested:
        if requested in configs:
            return requested
        if not configs:
            configs[requested] = {}
            return requested
        raise ValueError(
            f"指定配置不存在：{requested}。"
            f"可选配置：{', '.join(str(name) for name in configs)}"
        )
    if configs:
        return next(iter(configs))
    target = "默认"
    configs[target] = {}
    return target


def _has_panel_changes(panel: Mapping[str, Any]) -> bool:
    return any(
        _changed(panel, key)
        for key in PANEL_DEFAULTS
        if key != "config_file"
    )


def _has_profile_changes(panel: Mapping[str, Any]) -> bool:
    return any(_changed(panel, key) for key in PANEL_PROFILE_FIELDS)


def _enabled_mode(panel: Mapping[str, Any]) -> str:
    return _mode_value(panel, "enabled_mode") if "enabled_mode" in panel else "inherit"


def _mode_value(panel: Mapping[str, Any], key: str) -> str:
    mode = str(_panel_value(panel, key) or "inherit").strip().lower()
    if mode not in {"inherit", "enabled", "disabled"}:
        raise ValueError(
            f"{key} 必须是 inherit、enabled 或 disabled。"
        )
    return mode


def _apply_mode(
    panel: Mapping[str, Any],
    key: str,
    parent: dict,
    setting: str,
) -> None:
    if _changed(panel, key):
        parent[setting] = _mode_value(panel, key) == "enabled"


def _nonnegative_int(panel: Mapping[str, Any], key: str) -> int:
    raw = _panel_value(panel, key)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是不小于 0 的整数。") from exc
    if value < 0:
        raise ValueError(f"{key} 必须是不小于 0 的整数。")
    return value


def _apply_nonnegative_int(
    panel: Mapping[str, Any],
    key: str,
    parent: dict,
    setting: str,
) -> None:
    if _changed(panel, key):
        parent[setting] = _nonnegative_int(panel, key)


def _float(panel: Mapping[str, Any], key: str) -> float:
    raw = _panel_value(panel, key)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是数字。") from exc
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError(f"{key} 必须是有限数字。")
    return value


def _set_path(parent: dict, path: tuple[str, ...], value: Any) -> None:
    target = parent
    for key in path[:-1]:
        target = _ensure_dict(target, key)
    target[path[-1]] = value


def _apply_profile_field_mappings(
    panel: Mapping[str, Any], profile: dict
) -> None:
    for key, path in PANEL_PROFILE_STRING_OVERRIDES.items():
        if _changed(panel, key):
            _set_path(profile, path, str(_panel_value(panel, key)).strip())
    for key, path in PANEL_PROFILE_FLOAT_OVERRIDES.items():
        if _changed(panel, key):
            _set_path(profile, path, _float(panel, key))
    for key, path in PANEL_PROFILE_INT_OVERRIDES.items():
        if _changed(panel, key):
            _set_path(profile, path, (
                None if key == "llm_profile_max_tokens"
                and _panel_value(panel, key) == -1
                else _nonnegative_int(panel, key)
            ))
    for key, path in PANEL_PROFILE_LIST_OVERRIDES.items():
        if _changed(panel, key):
            list_value = [
                str(item).strip()
                for item in (_panel_value(panel, key) or [])
                if str(item).strip()
            ]
            _set_path(profile, path, list_value)
    for key, path in PANEL_PROFILE_MODE_OVERRIDES.items():
        if _changed(panel, key):
            mode = _mode_value(panel, key)
            _set_path(profile, path, mode == "enabled")
    for key, path in PANEL_PROFILE_OPTION_OVERRIDES.items():
        if not _changed(panel, key):
            continue
        value = str(_panel_value(panel, key)).strip().lower()
        allowed = PANEL_PROFILE_OPTION_VALUES.get(key)
        if allowed is not None and value not in allowed:
            raise ValueError(
                f"{key} 必须是 {', '.join(sorted(item for item in allowed if item))}。"
            )
        _set_path(profile, path, value)


def _apply_llm_endpoint_overrides(
    llm_cfg: dict, result: dict, panel: Mapping[str, Any]
) -> None:
    providers = llm_cfg.get("providers")
    if not isinstance(providers, list):
        providers = []
        llm_cfg["providers"] = providers

    selector = str(_panel_value(panel, "llm_endpoint_selector") or "").strip()
    provider = next(
        (
            item
            for item in providers
            if isinstance(item, dict)
            and str(item.get("name") or "").strip() == selector
        ),
        None,
    )
    if not selector:
        provider = next(
            (item for item in providers if isinstance(item, dict)), None
        )

    if provider is None:
        endpoint_payload_changed = any(
            _changed(panel, key)
            for key in PANEL_LLM_ENDPOINT_FIELDS - {
                "llm_endpoint_selector", "llm_endpoint_name"
            }
        )
        new_name = selector or (
            str(_panel_value(panel, "llm_endpoint_name") or "").strip()
            if _changed(panel, "llm_endpoint_name")
            else ""
        )
        if not new_name or not endpoint_payload_changed:
            raise ValueError("llm_endpoint_selector 对应的端点不存在。")
        provider = {"name": new_name}
        providers.append(provider)

    if _changed(panel, "llm_endpoint_name"):
        new_name = str(_panel_value(panel, "llm_endpoint_name") or "").strip()
        old_name = str(provider.get("name") or "").strip()
        if any(
            isinstance(item, dict)
            and item is not provider
            and str(item.get("name") or "").strip() == new_name
            for item in providers
        ):
            raise ValueError(f"LLM 端点名已存在：{new_name}")
        provider["name"] = new_name
        for profile in result.get("bot", {}).get("configs", {}).values():
            if not isinstance(profile, dict):
                continue
            profile_llm = profile.get("llm")
            if not isinstance(profile_llm, dict):
                continue
            candidates = profile_llm.get("models")
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("name") or "").strip() == old_name
                ):
                    candidate["name"] = new_name

    if _changed(panel, "llm_endpoint_kind"):
        kind = str(_panel_value(panel, "llm_endpoint_kind") or "").strip().lower()
        if kind not in {"openai", "anthropic"}:
            raise ValueError("llm_endpoint_kind 必须是 openai 或 anthropic。")
        provider["kind"] = kind
    if _changed(panel, "llm_endpoint_base_url"):
        provider["base_url"] = str(
            _panel_value(panel, "llm_endpoint_base_url")
        ).strip()
    if _changed(panel, "llm_endpoint_api_key"):
        provider["api_key"] = str(
            _panel_value(panel, "llm_endpoint_api_key")
        ).strip()
    for key, setting in (
        ("llm_endpoint_max_concurrency", "max_concurrency"),
        ("llm_endpoint_timeout_seconds", "timeout_seconds"),
        ("llm_endpoint_connect_timeout_seconds", "connect_timeout_seconds"),
        ("llm_endpoint_max_retries", "max_retries"),
    ):
        if _changed(panel, key):
            provider[setting] = _nonnegative_int(panel, key)

    if any(_changed(panel, key) for key in PANEL_LLM_MODEL_FIELDS):
        models = provider.get("models")
        if not isinstance(models, list):
            models = []
            provider["models"] = models
        selected_name = str(
            _panel_value(panel, "llm_endpoint_model") or ""
        ).strip()
        model = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and str(item.get("model") or "").strip() == selected_name
            ),
            None,
        )
        if not selected_name:
            model = next(
                (item for item in models if isinstance(item, dict)), None
            )
        if model is None:
            if not selected_name:
                raise ValueError(
                    "所选 LLM 端点未声明模型，无法应用模型高级配置。"
                )
            model = {"model": selected_name}
            models.append(model)

        supports = model.get("supports")
        if not isinstance(supports, dict):
            supports = {}
            model["supports"] = supports
        for key, setting in (
            ("llm_model_support_vision_mode", "vision"),
            ("llm_model_support_json_mode_mode", "json_mode"),
        ):
            if _changed(panel, key):
                supports[setting] = _mode_value(panel, key) == "enabled"
        for key, setting in (
            ("llm_model_max_tokens", "max_tokens"),
            ("llm_model_max_context_tokens", "max_context_tokens"),
        ):
            if _changed(panel, key):
                model[setting] = _nonnegative_int(panel, key)


def _apply_llm_panel_overrides(result: dict, panel: Mapping[str, Any]) -> None:
    """Apply flat dashboard fields to global LLM endpoint/model declarations."""
    if not any(
        _changed(panel, key)
        for key in PANEL_LLM_ENDPOINT_FIELDS | PANEL_LLM_MODEL_FIELDS | PANEL_LLM_GLOBAL_FIELDS
    ):
        return

    llm_cfg = _ensure_dict(result, "llm")
    if any(_changed(panel, key) for key in PANEL_LLM_ENDPOINT_FIELDS | PANEL_LLM_MODEL_FIELDS):
        _apply_llm_endpoint_overrides(llm_cfg, result, panel)

    if any(_changed(panel, key) for key in PANEL_LLM_GLOBAL_FIELDS):
        defaults = _ensure_dict(llm_cfg, "defaults")
        circuit = _ensure_dict(llm_cfg, "circuit_breaker")
        for key, setting in (
            ("llm_defaults_max_tokens", "max_tokens"),
            ("llm_defaults_max_context_tokens", "max_context_tokens"),
        ):
            if _changed(panel, key):
                defaults[setting] = _nonnegative_int(panel, key)
        for key, setting in (
            ("llm_circuit_failure_threshold", "failure_threshold"),
            ("llm_circuit_cooldown_seconds", "cooldown_seconds"),
            ("llm_circuit_rate_cooldown_seconds", "rate_cooldown_seconds"),
            ("llm_circuit_auth_cooldown_seconds", "auth_cooldown_seconds"),
        ):
            if _changed(panel, key):
                circuit[setting] = _nonnegative_int(panel, key)


def _parse_llm_fallback_models(value: Any) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for raw in value or []:
        text = str(raw).strip()
        if not text:
            continue
        provider, separator, model = text.partition(":")
        provider = provider.strip()
        model = model.strip()
        if not separator or not provider or not model:
            raise ValueError(
                "llm_fallback_models 每项格式必须是 端点名:模型名。"
            )
        candidates.append({"name": provider, "model": model})
    return candidates


def _apply_ui_mode(
    panel: Mapping[str, Any],
    key: str,
    ui: dict,
    ui_name: str,
) -> None:
    if _changed(panel, key):
        target = _ensure_dict(ui, ui_name)
        _apply_mode(panel, key, target, "enabled")


def _ensure_dict(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _panel_primary_candidate(cfg: dict, panel: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve the panel's explicit automatic endpoint/model selection."""
    providers = (cfg.get("llm") or {}).get("providers") or []
    name = str(
        panel.get("llm_provider") or panel.get("llm_endpoint_name")
        or panel.get("llm_endpoint_selector") or ""
    ).strip()
    provider = next(
        (item for item in providers if isinstance(item, dict)
         and (not name or item.get("name") == name)), {}
    )
    models = provider.get("models") or []
    first_model = next((item for item in models if isinstance(item, dict)), {})
    selected_model = str(panel.get("llm_endpoint_model") or "").strip()
    model = (
        selected_model if any(
            item.get("model") == selected_model
            for item in models if isinstance(item, dict)
        ) else first_model.get("model", "")
    )
    return str(provider.get("name") or name), str(model)


def _normalize_model_aliases(data: dict, model: type[BaseModel]) -> None:
    """Keep camelCase JSON from taking precedence over new snake_case edits."""
    for name, field in model.model_fields.items():
        if field.alias and field.alias != name and field.alias in data:
            data[name] = data.pop(field.alias)
        value = data.get(name)
        annotation = field.annotation
        candidates = (annotation, *get_args(annotation))
        child = next(
            (item for item in candidates
             if isinstance(item, type) and issubclass(item, BaseModel)), None
        )
        if child is None:
            continue
        if isinstance(value, dict):
            _normalize_model_aliases(value, child)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _normalize_model_aliases(item, child)


def apply_panel_overrides(
    cfg: Mapping[str, Any],
    panel: Mapping[str, Any] | None,
) -> dict:
    """
    Apply changed dashboard values to one profile without replacing advanced JSON.

    Supplied fields are authoritative, including empty strings/lists. Missing
    fields leave advanced JSON untouched; blank selectors select the first item.
    """
    result = copy.deepcopy(dict(cfg))
    if not panel:
        return result
    panel = normalize_panel_values(panel)
    enabled_mode = _enabled_mode(panel)
    if not _has_panel_changes(panel):
        return result
    bot_cfg = _ensure_dict(result, "bot")

    if _changed(panel, "data_dir"):
        bot_cfg["data_dir"] = str(_panel_value(panel, "data_dir")).strip()
    if _changed(panel, "log_level"):
        log_cfg = _ensure_dict(result, "log")
        log_cfg["log_level"] = str(_panel_value(panel, "log_level")).upper()
    if _changed(panel, "log_to_file_mode"):
        log_cfg = _ensure_dict(result, "log")
        _apply_mode(panel, "log_to_file_mode", log_cfg, "log_to_file")
    if _changed(panel, "log_dir"):
        log_cfg = _ensure_dict(result, "log")
        log_cfg["log_dir"] = str(_panel_value(panel, "log_dir")).strip()

    if isinstance(result.get("llm"), dict):
        _normalize_model_aliases(result["llm"], LLMGlobalConfig)
    _apply_llm_panel_overrides(result, panel)

    if _has_profile_changes(panel):
        target = _target_profile(result, panel)
        configs = _ensure_dict(bot_cfg, "configs")
        profile = configs.get(target)
        if not isinstance(profile, dict):
            profile = {}
            configs[target] = profile
        for section, model in (
            ("project", ProjectConfig), ("settings", AnalysisSettings),
            ("commands", CommandConfig), ("llm", ConfigLLM),
            ("vision", VisionConfig), ("report", ReportConfig),
        ):
            if isinstance(profile.get(section), dict):
                _normalize_model_aliases(profile[section], model)

        if _changed(panel, "listen_groups"):
            profile["listen_groups"] = [
                str(item).strip()
                for item in (_panel_value(panel, "listen_groups") or [])
                if str(item).strip()
            ]
        if _changed(panel, "notify_group"):
            profile["notify_group"] = str(
                _panel_value(panel, "notify_group") or ""
            ).strip()
        if enabled_mode != "inherit":
            settings = _ensure_dict(profile, "settings")
            settings["enabled"] = enabled_mode == "enabled"

        if _changed(panel, "command_prefix"):
            commands = _ensure_dict(profile, "commands")
            commands["prefix"] = str(
                _panel_value(panel, "command_prefix")
            ).strip()
        if _changed(panel, "owners"):
            commands = _ensure_dict(profile, "commands")
            commands["owners"] = [
                str(item).strip()
                for item in (_panel_value(panel, "owners") or [])
                if str(item).strip()
            ]
        if _changed(panel, "admins"):
            commands = _ensure_dict(profile, "commands")
            commands["admins"] = [
                str(item).strip()
                for item in (_panel_value(panel, "admins") or [])
                if str(item).strip()
            ]
        if _changed(panel, "report_mode"):
            report = _ensure_dict(profile, "report")
            report["render"] = str(
                _panel_value(panel, "report_mode")
            ).lower()
        if _changed(panel, "report_template"):
            report = _ensure_dict(profile, "report")
            report["template"] = str(
                _panel_value(panel, "report_template")
            ).strip()

        if _changed(panel, "project_name"):
            project = _ensure_dict(profile, "project")
            project["name"] = str(
                _panel_value(panel, "project_name")
            ).strip()
        if _changed(panel, "archive_match_mode"):
            project = _ensure_dict(profile, "project")
            mode = str(
                _panel_value(panel, "archive_match_mode")
            ).strip().lower()
            if mode not in {"auto", "known", "prefix", "any"}:
                raise ValueError(
                    "archive_match_mode 必须是 auto、known、prefix 或 any。"
                )
            project["archive_match_mode"] = mode
        if _changed(panel, "file_prefix"):
            project = _ensure_dict(profile, "project")
            project["file_prefix"] = str(
                _panel_value(panel, "file_prefix")
            ).strip()
        if _changed(panel, "require_log_layout_mode"):
            project = _ensure_dict(profile, "project")
            _apply_mode(
                panel,
                "require_log_layout_mode",
                project,
                "require_log_layout",
            )
        if any(
            _changed(panel, key)
            for key in (
                "ui_auto_mode",
            )
        ):
            project = _ensure_dict(profile, "project")
            ui = _ensure_dict(project, "ui")
            _apply_ui_mode(panel, "ui_auto_mode", ui, "auto")
        if any(
            _changed(panel, key)
            for key in ("llm_provider", "llm_model", "llm_fallback_models")
        ):
            llm = _ensure_dict(profile, "llm")
            models = llm.get("models")
            if not isinstance(models, list):
                models = []
            if _changed(panel, "llm_provider") or _changed(panel, "llm_model"):
                if not models:
                    models.append({})
                first = models[0]
                if not isinstance(first, dict):
                    first = {}
                    models[0] = first
                if _changed(panel, "llm_provider"):
                    first["name"] = str(
                        _panel_value(panel, "llm_provider") or ""
                    ).strip() or _panel_primary_candidate(result, panel)[0]
                if _changed(panel, "llm_model"):
                    first["model"] = str(
                        _panel_value(panel, "llm_model") or ""
                    ).strip() or _panel_primary_candidate(result, panel)[1]
            if _changed(panel, "llm_fallback_models"):
                fallbacks = _parse_llm_fallback_models(
                    _panel_value(panel, "llm_fallback_models")
                )
                first = (
                    models[0]
                    if models and isinstance(models[0], dict)
                    else None
                )
                models = ([first] if first is not None else []) + fallbacks
            llm["models"] = models

        if _changed(panel, "vision_mode"):
            vision = _ensure_dict(profile, "vision")
            _apply_mode(panel, "vision_mode", vision, "enabled")
        if any(
            _changed(panel, key)
            for key in (
                "send_images_mode",
                "send_repo_images_mode",
                "send_files_mode",
                "followup_mode",
                "followup_window_minutes",
                "history_mode",
                "history_period_hours",
                "history_keep_periods",
            )
        ):
            settings = _ensure_dict(profile, "settings")
            _apply_mode(panel, "send_images_mode", settings, "send_images")
            _apply_mode(
                panel, "send_repo_images_mode", settings, "send_repo_images"
            )
            _apply_mode(panel, "send_files_mode", settings, "send_files")
            _apply_mode(panel, "followup_mode", settings, "followup_enabled")
            _apply_nonnegative_int(
                panel,
                "followup_window_minutes",
                settings,
                "followup_window_minutes",
            )
            _apply_mode(panel, "history_mode", settings, "history_enabled")
            _apply_nonnegative_int(
                panel, "history_period_hours", settings, "history_period_hours"
            )
            _apply_nonnegative_int(
                panel,
                "history_keep_periods",
                settings,
                "history_keep_periods",
            )
        if any(
            _changed(panel, key)
            for key in (
                "commands_mode",
                "commands_read_level",
                "commands_write_level",
                "commands_followup_level",
            )
        ):
            commands = _ensure_dict(profile, "commands")
            _apply_mode(panel, "commands_mode", commands, "enabled")
            _apply_nonnegative_int(
                panel, "commands_read_level", commands, "read_level"
            )
            _apply_nonnegative_int(
                panel, "commands_write_level", commands, "write_level"
            )
            _apply_nonnegative_int(
                panel,
                "commands_followup_level",
                commands,
                "followup_level",
            )

        _apply_profile_field_mappings(panel, profile)
    return result


def load_effective_config(
    panel: Mapping[str, Any] | None,
    *,
    plugin_data_dir: str | Path,
) -> tuple[dict, Path, Path]:
    """Load advanced JSON, apply panel overrides, and resolve runtime paths."""
    panel = panel or {}
    plugin_dir = Path(plugin_data_dir).resolve()
    configured_path = str(_panel_value(panel, "config_file") or "").strip() or "config.json"
    config_path = Path(configured_path).expanduser()
    if not config_path.is_absolute():
        config_path = plugin_dir / config_path
    config_path = config_path.resolve()
    created = not config_path.exists()
    write_default_config(config_path)
    raw = load_config_file(config_path)
    effective = apply_panel_overrides(raw, panel)
    errors = validate_common_config(effective)
    if errors:
        raise ValueError("\n".join(errors))
    data_dir = resolve_data_dir(
        effective,
        base_dir=plugin_dir,
        override=(
            str(_panel_value(panel, "data_dir")).strip()
            if _changed(panel, "data_dir")
            else None
        ),
    )
    if created:
        logger.info("已生成 AstrBot 插件默认配置：%s", config_path)
    return effective, config_path, data_dir


def _unique_log_file(log_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = log_dir / f"{stamp}.log"
    index = 2
    while path.exists():
        path = log_dir / f"{stamp}_{index}.log"
        index += 1
    return path


def setup_logging(
    log_cfg: Mapping[str, Any],
    *,
    isolated: bool = False,
    base_dir: str | Path | None = None,
) -> None:
    """Configure bare-process logging or an isolated plugin logger."""
    level = getattr(
        logging, str(log_cfg.get("log_level", "INFO")).upper(), logging.INFO
    )
    log_to_file = bool(log_cfg.get("log_to_file", True))
    log_dir = Path(str(log_cfg.get("log_dir", "logs"))).expanduser()
    if not log_dir.is_absolute():
        log_dir = Path(base_dir or ".") / log_dir
    target = logging.getLogger("Maa") if isolated else logging.getLogger()

    for handler in list(target.handlers):
        if not isolated or getattr(handler, "_maa_plugin_handler", False):
            target.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    target.setLevel(level)
    target.propagate = not isolated

    if not isolated or not any(
        getattr(handler, "_maa_astrbot_bridge", False) for handler in target.handlers
    ):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
        if isolated:
            setattr(console, "_maa_plugin_handler", True)
        target.addHandler(console)

    if log_to_file:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                _unique_log_file(log_dir), encoding="utf-8"
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(
                ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
            )
            if isolated:
                setattr(file_handler, "_maa_plugin_handler", True)
            target.addHandler(file_handler)
            logger.info(
                "日志输出到文件: %s (level=%s)",
                file_handler.baseFilename,
                logging.getLevelName(level),
            )
        except OSError as exc:
            logger.warning("无法创建日志文件：%s", exc)


def teardown_logging(*, isolated: bool = False) -> None:
    """Close handlers installed by :func:`setup_logging`."""
    target = logging.getLogger("Maa") if isolated else logging.getLogger()
    for handler in list(target.handlers):
        if not isolated or getattr(handler, "_maa_plugin_handler", False):
            target.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
