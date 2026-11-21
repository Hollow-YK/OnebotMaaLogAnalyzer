"""Default configuration written on first run."""
from __future__ import annotations

import json
from pathlib import Path

from .constants import CONFIG_VERSION, CONFIG_VERSION_FIELD


def default_config() -> dict:
    """Return a fresh copy of the first-run configuration."""
    return {
        CONFIG_VERSION_FIELD: CONFIG_VERSION,
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
