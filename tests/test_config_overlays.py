"""Config inheritance and AstrBot panel override tests."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from maa_analyzer.config import (
    PANEL_DEFAULTS,
    PANEL_PROFILE_FLOAT_OVERRIDES,
    PANEL_PROFILE_INT_OVERRIDES,
    PANEL_PROFILE_MODE_OVERRIDES,
    PANEL_PROFILE_STRING_OVERRIDES,
    apply_panel_overrides,
    default_config,
    load_effective_config,
    normalize_panel_values,
)
from maa_analyzer.core.models import AnalysisSettings, CommandConfig, ConfigLLM, ModelSupports, ProjectConfig, ReportConfig, VisionConfig
from maa_analyzer.core.llm_pool import LLMPool
from tools.config.editor import _config_defaults


class PanelOverrideTests(unittest.TestCase):
    def test_model_tools_are_always_enabled_with_legacy_settings(self):
        for legacy in ({}, {"tools": False}, {"tools": True}):
            with self.subTest(legacy=legacy):
                supports = ModelSupports.model_validate(legacy)
                self.assertTrue(supports.tools)
                self.assertNotIn("tools", supports.model_dump())

    def test_legacy_disabled_tools_model_remains_a_candidate(self):
        pool = LLMPool({"providers": [{"name": "main", "base_url": "https://example.com",
                                      "models": [{"model": "model", "supports": {"tools": False}}]}]})
        config = ConfigLLM.model_validate({"models": [{"name": "main", "model": "model"}]})
        self.assertTrue(pool.has_capability(config, "tools"))
        self.assertFalse(pool.has_capability(config, "vision"))

    def test_repo_is_always_enabled_with_legacy_settings(self):
        for legacy in ({}, {"enabled": False}, {"enabled": True}):
            with self.subTest(legacy=legacy):
                repo = ProjectConfig.model_validate({"repo": legacy}).repo
                self.assertTrue(repo.enabled)
                self.assertNotIn("enabled", repo.model_dump())

    def test_clearing_lists_and_text_removes_hidden_json_values(self):
        cfg = self._config()
        alpha = cfg["bot"]["configs"]["Alpha"]
        alpha["listen_groups"] = ["10001"]
        alpha["commands"].update(owners=["1"], admins=["2"])
        alpha["project"].update(name="HiddenProject", file_prefix="hidden")
        alpha["project"]["repo"].update(
            path="/hidden/repo", mirrors=["https://hidden.example"],
            branch="hidden", default_branch="hidden", extensions=[".hidden"],
        )
        alpha["report"].update(font_family="HiddenFont", browser_path="/hidden/browser")
        alpha["settings"]["system_prompt"] = "hidden prompt"
        alpha["llm"]["models"].append({"name": "beta", "model": "beta-model"})
        panel = {
            "listen_groups": [], "owners": [], "admins": [],
            "project_name": "", "file_prefix": "", "repo_path": "",
            "repo_mirrors": [], "repo_branch": "", "repo_default_branch": "",
            "repo_extensions": [], "report_font_family": "",
            "report_browser_path": "", "settings_system_prompt": "",
            "llm_fallback_models": [],
        }

        result = apply_panel_overrides(cfg, panel)
        actual = result["bot"]["configs"]["Alpha"]
        self.assertEqual(actual["listen_groups"], [])
        self.assertEqual(actual["commands"]["owners"], [])
        self.assertEqual(actual["commands"]["admins"], [])
        self.assertEqual(actual["project"]["name"], "")
        self.assertEqual(actual["project"]["file_prefix"], "")
        self.assertEqual(actual["project"]["repo"]["path"], "")
        self.assertEqual(actual["project"]["repo"]["mirrors"], [])
        self.assertEqual(actual["project"]["repo"]["branch"], "")
        self.assertEqual(actual["project"]["repo"]["default_branch"], "")
        self.assertEqual(actual["project"]["repo"]["extensions"], [])
        self.assertEqual(actual["report"]["font_family"], "")
        self.assertEqual(actual["report"]["browser_path"], "")
        self.assertEqual(actual["settings"]["system_prompt"], "")
        self.assertEqual(actual["llm"]["models"], [{"name": "default", "model": "gpt-4o-mini"}])
        self.assertEqual(result["bot"]["configs"]["Beta"], cfg["bot"]["configs"]["Beta"])
        self.assertEqual(alpha["settings"]["system_prompt"], "hidden prompt")

    def test_llm_profile_token_limit_clears_instead_of_inheriting_same_level(self):
        cfg = self._config()
        cfg["bot"]["configs"]["Alpha"]["llm"]["max_tokens"] = 1234
        cfg["llm"]["providers"][0]["api_key"] = "hidden-key"
        result = apply_panel_overrides(cfg, {
            "llm_endpoint_api_key": "", "llm_profile_max_tokens": -1,
        })
        self.assertEqual(result["llm"]["providers"][0]["api_key"], "")
        self.assertIsNone(result["bot"]["configs"]["Alpha"]["llm"]["max_tokens"])

    def test_blank_fixed_defaults_replace_hidden_json_and_are_visible(self):
        panel = {"log_dir": "", "log_level": "", "report_mode": "",
                 "report_device_scale_factor": "", "history_period_hours": -1,
                 "enabled_mode": "inherit", "data_dir": ""}
        normalized = normalize_panel_values(panel)
        for key in panel:
            self.assertEqual(normalized[key], PANEL_DEFAULTS[key], key)
        cfg = self._config()
        cfg["log"].update(log_dir="hidden", log_level="DEBUG")
        cfg["bot"]["data_dir"] = "hidden"
        result = apply_panel_overrides(cfg, panel)
        self.assertEqual(result["log"]["log_dir"], "logs")
        self.assertEqual(result["log"]["log_level"], "INFO")
        self.assertEqual(result["bot"]["data_dir"], "data")

    def test_automatic_primary_llm_uses_panel_endpoint_instead_of_hidden_candidate(self):
        cfg = self._config()
        result = apply_panel_overrides(cfg, {
            "llm_endpoint_selector": "beta", "llm_endpoint_model": "beta-model",
            "llm_provider": "", "llm_model": "", "llm_fallback_models": [],
        })
        self.assertEqual(result["bot"]["configs"]["Alpha"]["llm"]["models"],
                         [{"name": "beta", "model": "beta-model"}])

    def test_camel_case_json_cannot_override_panel_edits(self):
        cfg = self._config()
        alpha = cfg["bot"]["configs"]["Alpha"]
        alpha["report"]["browserPath"] = "/hidden/browser"
        alpha["report"]["maxWidthPx"] = 9999
        alpha["settings"]["sendSummary"] = False
        alpha["project"]["repo"]["defaultBranch"] = "hidden"
        cfg["llm"]["providers"][0]["baseUrl"] = "https://hidden.example"
        result = apply_panel_overrides(cfg, {
            "report_browser_path": "", "report_max_width_px": 1200,
            "settings_send_summary_mode": "enabled", "repo_default_branch": "",
            "llm_endpoint_base_url": "https://visible.example",
        })
        actual = result["bot"]["configs"]["Alpha"]
        report = ReportConfig.model_validate(actual["report"])
        self.assertEqual(report.browser_path, "")
        self.assertEqual(report.max_width_px, 1200)
        self.assertTrue(AnalysisSettings.model_validate(actual["settings"]).send_summary)
        self.assertEqual(ProjectConfig.model_validate(actual["project"]).repo.default_branch, "")
        self.assertNotIn("baseUrl", result["llm"]["providers"][0])
        self.assertEqual(result["llm"]["providers"][0]["base_url"], "https://visible.example")

    def test_global_llm_parameters_do_not_require_selecting_an_endpoint(self):
        cfg = self._config()
        cfg["llm"]["providers"] = []
        result = apply_panel_overrides(cfg, {"llm_defaults_max_tokens": 4096})
        self.assertEqual(result["llm"]["defaults"]["max_tokens"], 4096)
        self.assertEqual(result["llm"]["providers"], [])

    def test_clearing_panel_notification_only_disables_selected_profile(self):
        cfg = self._config()
        cfg["bot"]["configs"]["Alpha"]["notify_group"] = "10003"
        cfg["bot"]["configs"]["Beta"]["notify_group"] = "10004"

        for value in ("", "  "):
            with self.subTest(value=value):
                result = apply_panel_overrides(
                    cfg, {"default_config": "Alpha", "notify_group": value}
                )
                self.assertEqual(result["bot"]["configs"]["Alpha"]["notify_group"], "")
                self.assertEqual(result["bot"]["configs"]["Beta"]["notify_group"], "10004")
                self.assertEqual(cfg["bot"]["configs"]["Alpha"]["notify_group"], "10003")

    def test_absent_panel_notification_preserves_json_destination(self):
        cfg = self._config()
        cfg["bot"]["configs"]["Alpha"]["notify_group"] = "10003"
        result = apply_panel_overrides(cfg, {"default_config": "Alpha"})
        self.assertEqual(result, cfg)

    def test_editor_new_profiles_do_not_configure_message_destinations(self):
        profile = _config_defaults()
        self.assertEqual(profile["listen_groups"], [])
        self.assertEqual(profile["notify_group"], "")

    def test_profile_panel_defaults_match_builtin_models(self):
        defaults = {
            "settings": AnalysisSettings().model_dump(),
            "commands": CommandConfig().model_dump(),
            "project": ProjectConfig().model_dump(),
            "report": ReportConfig().model_dump(),
            "vision": VisionConfig().model_dump(),
        }
        for mapping in (PANEL_PROFILE_INT_OVERRIDES, PANEL_PROFILE_FLOAT_OVERRIDES,
                        PANEL_PROFILE_MODE_OVERRIDES, PANEL_PROFILE_STRING_OVERRIDES):
            for key, path in mapping.items():
                value = defaults
                for component in path:
                    if not isinstance(value, dict) or component not in value:
                        break
                    value = value[component]
                else:
                    if value is None:
                        continue
                    if isinstance(value, bool):
                        value = "enabled" if value else "disabled"
                    elif key in PANEL_PROFILE_FLOAT_OVERRIDES:
                        value = str(value)
                    self.assertEqual(PANEL_DEFAULTS[key], value, key)

    def _config(self) -> dict:
        cfg = default_config()
        configs = cfg["bot"]["configs"]
        configs["Alpha"] = configs.pop("默认")
        configs["Alpha"]["project"]["custom_advanced"] = {"keep": True}
        configs["Beta"] = copy.deepcopy(configs["Alpha"])
        configs["Beta"]["listen_groups"] = ["999"]
        configs["Beta"]["project"]["name"] = "BetaProject"
        configs["Beta"]["llm"]["models"] = [
            {"name": "beta", "model": "beta-model"},
            {"name": "backup", "model": "backup-model"},
        ]
        cfg["llm"]["providers"].append(
            {
                "name": "beta",
                "kind": "openai",
                "base_url": "https://beta.example/v1",
                "api_key": "old-key",
                "models": [
                    {"model": "beta-model", "supports": {"tools": False}}
                ],
            }
        )
        configs["Beta"]["settings"]["send_files"] = False
        return cfg

    def test_panel_only_overrides_selected_profile_and_preserves_advanced_json(self):
        cfg = self._config()
        panel = {
            "enabled_mode": "enabled",
            "default_config": "Beta",
            "listen_groups": ["10001", " 10002 ", ""],
            "notify_group": "10003",
            "command_prefix": "!maa",
            "owners": ["1", " 2 ", ""],
            "admins": ["3"],
            "report_mode": "image",
            "report_template": "dark",
            "project_name": " Beta Project ",
            "archive_match_mode": "any",
            "file_prefix": "MaaXXX-logs",
            "require_log_layout_mode": "disabled",
            "ui_mxu_mode": "disabled",
            "ui_mfaa_mode": "enabled",
            "ui_maafwapp_mode": "disabled",
            "ui_auto_mode": "enabled",
            "llm_provider": "default",
            "llm_model": "gpt-4o-mini",
            "vision_mode": "disabled",
            "send_images_mode": "disabled",
            "send_repo_images_mode": "enabled",
            "send_files_mode": "enabled",
            "followup_mode": "disabled",
            "followup_window_minutes": 0,
            "history_mode": "disabled",
            "history_period_hours": 12,
            "history_keep_periods": 3,
            "commands_mode": "disabled",
            "commands_read_level": 1,
            "commands_write_level": 1,
            "commands_followup_level": 2,
            "log_to_file_mode": "disabled",
            "log_dir": "runtime-logs",
        }

        result = apply_panel_overrides(cfg, panel)

        self.assertEqual(result["bot"]["configs"]["Alpha"], cfg["bot"]["configs"]["Alpha"])
        beta = result["bot"]["configs"]["Beta"]
        self.assertEqual(beta["listen_groups"], ["10001", "10002"])
        self.assertEqual(beta["notify_group"], "10003")
        self.assertIs(beta["settings"]["enabled"], True)
        self.assertIs(beta["settings"]["send_files"], True)
        self.assertEqual(beta["commands"]["prefix"], "!maa")
        self.assertEqual(beta["commands"]["owners"], ["1", "2"])
        self.assertEqual(beta["commands"]["admins"], ["3"])
        self.assertEqual(beta["report"]["render"], "image")
        self.assertEqual(beta["report"]["template"], "dark")
        self.assertEqual(beta["project"]["name"], "Beta Project")
        self.assertEqual(beta["project"]["archive_match_mode"], "any")
        self.assertEqual(beta["project"]["file_prefix"], "MaaXXX-logs")
        self.assertIs(beta["project"]["require_log_layout"], False)
        self.assertEqual(
            beta["project"]["ui"],
            {
                "auto": {"enabled": True},
            },
        )
        self.assertTrue(ProjectConfig.model_validate(beta["project"]).repo.enabled)
        self.assertEqual(
            beta["llm"]["models"],
            [
                {"name": "default", "model": "gpt-4o-mini"},
                {"name": "backup", "model": "backup-model"},
            ],
        )
        self.assertIs(beta["vision"]["enabled"], False)
        self.assertIs(beta["settings"]["send_images"], False)
        self.assertIs(beta["settings"]["send_repo_images"], True)
        self.assertIs(beta["settings"]["send_files"], True)
        self.assertIs(beta["settings"]["followup_enabled"], False)
        self.assertEqual(beta["settings"]["followup_window_minutes"], 0)
        self.assertIs(beta["settings"]["history_enabled"], False)
        self.assertEqual(beta["settings"]["history_period_hours"], 12)
        self.assertEqual(beta["settings"]["history_keep_periods"], 3)
        self.assertIs(beta["commands"]["enabled"], False)
        self.assertEqual(beta["commands"]["read_level"], 1)
        self.assertEqual(beta["commands"]["write_level"], 1)
        self.assertEqual(beta["commands"]["followup_level"], 2)
        self.assertIs(result["log"]["log_to_file"], False)
        self.assertEqual(result["log"]["log_dir"], "runtime-logs")
        self.assertEqual(
            beta["project"]["custom_advanced"],
            {"keep": True},
        )

    def test_vision_report_and_command_advanced_fields_are_panel_overridable(self):
        result = apply_panel_overrides(
            self._config(),
            {
                "default_config": "Alpha",
                "vision_max_images": 2,
                "vision_max_image_kb": 512,
                "vision_detail": "low",
                "vision_attach_error_screenshots_mode": "disabled",
                "vision_attach_repo_templates_mode": "disabled",
                "vision_allow_model_request_mode": "enabled",
                "report_max_width_px": 900,
                "report_max_height_px": 3000,
                "report_max_pages": 10,
                "report_device_scale_factor": "1.5",
                "report_font_family": " Noto Sans CJK ",
                "report_browser_path": "/usr/bin/chromium",
                "report_reuse_browser_mode": "disabled",
                "report_render_timeout_ms": 12000,
                "report_embed_attachments_mode": "disabled",
                "report_attach_markdown_mode": "enabled",
                "report_fallback_to_text_mode": "disabled",
                "report_render_followup_mode": "disabled",
                "commands_reply_chunk_chars": 2000,
                "commands_max_list_items": 20,
            },
        )
        profile = result["bot"]["configs"]["Alpha"]
        self.assertEqual(profile["vision"]["max_images"], 2)
        self.assertEqual(profile["vision"]["max_image_kb"], 512)
        self.assertEqual(profile["vision"]["detail"], "low")
        self.assertIs(profile["vision"]["attach_error_screenshots"], False)
        self.assertIs(profile["vision"]["attach_repo_templates"], False)
        self.assertIs(profile["vision"]["allow_model_request"], True)
        self.assertEqual(profile["report"]["max_width_px"], 900)
        self.assertEqual(profile["report"]["max_height_px"], 3000)
        self.assertEqual(profile["report"]["max_pages"], 10)
        self.assertEqual(profile["report"]["device_scale_factor"], 1.5)
        self.assertEqual(profile["report"]["font_family"], "Noto Sans CJK")
        self.assertEqual(profile["report"]["browser_path"], "/usr/bin/chromium")
        self.assertIs(profile["report"]["reuse_browser"], False)
        self.assertEqual(profile["report"]["render_timeout_ms"], 12000)
        self.assertIs(profile["report"]["embed_attachments"], False)
        self.assertIs(profile["report"]["attach_markdown"], True)
        self.assertIs(profile["report"]["fallback_to_text"], False)
        self.assertIs(profile["report"]["render_followup"], False)
        self.assertEqual(profile["commands"]["reply_chunk_chars"], 2000)
        self.assertEqual(profile["commands"]["max_list_items"], 20)

    def test_llm_endpoint_model_defaults_and_profile_fields_are_overridable(self):
        result = apply_panel_overrides(
            self._config(),
            {
                "default_config": "Beta",
                "llm_fallback_models": ["backup:backup-model"],
                "llm_profile_temperature": "0.3",
                "llm_profile_max_tokens": 4096,
                "llm_profile_top_p": "0.9",
                "llm_endpoint_selector": "beta",
                "llm_endpoint_kind": "anthropic",
                "llm_endpoint_base_url": "https://beta.example/anthropic",
                "llm_endpoint_api_key": "new-key",
                "llm_endpoint_max_concurrency": 4,
                "llm_endpoint_timeout_seconds": 300,
                "llm_endpoint_connect_timeout_seconds": 5,
                "llm_endpoint_max_retries": 1,
                "llm_endpoint_model": "beta-model",
                "llm_model_support_vision_mode": "enabled",
                "llm_model_support_json_mode_mode": "disabled",
                "llm_model_max_tokens": 8192,
                "llm_model_max_context_tokens": 128000,
                "llm_model_temperature": "0.1",
                "llm_model_top_p": "0.95",
                "llm_defaults_temperature": "0.25",
                "llm_defaults_top_p": "0.9",
                "llm_defaults_max_tokens": 4096,
                "llm_defaults_max_context_tokens": 96000,
                "llm_circuit_failure_threshold": 5,
                "llm_circuit_cooldown_seconds": 120,
                "llm_circuit_rate_cooldown_seconds": 30,
                "llm_circuit_auth_cooldown_seconds": 600,
            },
        )
        beta = next(
            item for item in result["llm"]["providers"] if item["name"] == "beta"
        )
        self.assertEqual(beta["kind"], "anthropic")
        self.assertEqual(beta["base_url"], "https://beta.example/anthropic")
        self.assertEqual(beta["api_key"], "new-key")
        self.assertEqual(beta["max_concurrency"], 4)
        self.assertEqual(beta["timeout_seconds"], 300)
        self.assertEqual(beta["connect_timeout_seconds"], 5)
        self.assertEqual(beta["max_retries"], 1)
        model = beta["models"][0]
        self.assertEqual(model["supports"], {"tools": False, "vision": True, "json_mode": False})
        self.assertEqual(model["max_tokens"], 8192)
        self.assertEqual(model["max_context_tokens"], 128000)
        self.assertEqual(
            result["llm"]["defaults"],
            {
                "max_tokens": 4096,
                "max_context_tokens": 96000,
            },
        )
        self.assertEqual(
            result["llm"]["circuit_breaker"],
            {
                "failure_threshold": 5,
                "cooldown_seconds": 120,
                "rate_cooldown_seconds": 30,
                "auth_cooldown_seconds": 600,
            },
        )
        profile_llm = result["bot"]["configs"]["Beta"]["llm"]
        self.assertEqual(
            profile_llm["models"],
            [
                {"name": "beta", "model": "beta-model"},
                {"name": "backup", "model": "backup-model"},
            ],
        )
        self.assertEqual(profile_llm["max_tokens"], 4096)

    def test_llm_endpoint_rename_updates_profile_references(self):
        result = apply_panel_overrides(
            self._config(),
            {
                "llm_endpoint_selector": "beta",
                "llm_endpoint_name": "renamed-beta",
            },
        )
        provider_names = [item["name"] for item in result["llm"]["providers"]]
        self.assertIn("renamed-beta", provider_names)
        self.assertEqual(
            result["bot"]["configs"]["Beta"]["llm"]["models"][0]["name"],
            "renamed-beta",
        )

    def test_invalid_llm_panel_fields_are_rejected(self):
        for panel, message in (
            ({"llm_endpoint_selector": "missing"}, "端点不存在"),
            ({"llm_endpoint_kind": "grpc"}, "llm_endpoint_kind"),
            ({"llm_fallback_models": ["bad"]}, "llm_fallback_models"),
        ):
            with self.subTest(panel=panel):
                with self.assertRaisesRegex(ValueError, message):
                    apply_panel_overrides(self._config(), panel)

    def test_all_remaining_analysis_settings_have_panel_fields(self):
        mapping_groups = (
            PANEL_PROFILE_STRING_OVERRIDES,
            PANEL_PROFILE_FLOAT_OVERRIDES,
            PANEL_PROFILE_INT_OVERRIDES,
            PANEL_PROFILE_MODE_OVERRIDES,
        )
        mapped_settings = {
            path[-1]
            for mappings in mapping_groups
            for path in mappings.values()
            if path[0] == "settings"
        }
        expected = {
            "file_list_delay_seconds", "duplicate_window_seconds",
            "send_progress_message", "send_summary", "forward_analysis_results", "reply_chunk_chars",
            "max_report_images", "max_report_files",
            "max_file_attachment_mb", "inline_attachments",
            "followup_max_turns", "followup_max_sessions",
            "history_max_records", "history_max_total_mb",
            "download_concurrency",
            "download_timeout_seconds", "digest_timeout_seconds",
            "ai_stage_timeout_seconds", "max_context_tokens",
            "context_compact_enabled", "context_compact_ratio",
            "context_retain_ratio", "context_prune_tool_result_tokens",
            "context_summary_tokens", "analysis_cache_hours",
            "circuit_breaker_enabled", "system_prompt",
            "log_tools_enabled", "cleanup_after_done", "save_digest_debug",
            "max_zip_size_mb", "max_total_uncompressed_mb",
            "max_prompt_chars", "max_zip_members", "max_log_files",
            "max_maafw_bak_files", "max_maafw_bak_mb", "merge_archive_parts",
            "archive_part_settle_seconds", "max_log_file_read_kb",
            "small_log_full_read_mb", "stream_medium_logs",
            "large_log_threshold_mb", "max_large_log_read_kb",
            "stream_large_logs", "max_stream_log_mb", "max_sections_per_log",
            "max_log_chars", "compress_context_noise",
            "max_recognition_evidence_lines",
            "max_recognition_evidence_chars", "log_head_lines",
        }
        self.assertLessEqual(expected, mapped_settings)
        for field in expected:
            direct = "settings_" + field
            tristate = direct + "_mode"
            self.assertTrue(direct in PANEL_DEFAULTS or tristate in PANEL_DEFAULTS)

    def test_analysis_advanced_fields_apply_each_panel_type(self):
        panel = {
            "default_config": "Alpha",
            "settings_file_list_delay_seconds": "1.5",
            "settings_duplicate_window_seconds": 60,
            "settings_send_progress_message_mode": "disabled",
            "settings_forward_analysis_results_mode": "enabled",
            "settings_system_prompt": "你是日志分析助手",
            "settings_context_compact_ratio": "0.75",
        }
        result = apply_panel_overrides(self._config(), panel)
        settings = result["bot"]["configs"]["Alpha"]["settings"]
        self.assertEqual(settings["file_list_delay_seconds"], 1.5)
        self.assertEqual(settings["duplicate_window_seconds"], 60)
        self.assertIs(settings["send_progress_message"], False)
        self.assertIs(settings["forward_analysis_results"], True)
        self.assertEqual(settings["system_prompt"], "你是日志分析助手")
        self.assertEqual(settings["context_compact_ratio"], 0.75)

    def test_repo_advanced_fields_are_panel_overridable(self):
        result = apply_panel_overrides(
            self._config(),
            {
                "default_config": "Alpha",
                "repo_search_mode": "agent",
                "repo_path": " /repos/maa ",
                "repo_url": "https://example.com/maa.git",
                "repo_mirrors": [" https://mirror.example/maa.git ", ""],
                "repo_branch": "dev",
                "repo_default_branch": "main",
                "repo_preload_enabled_mode": "disabled",
                "repo_preload_retries": 5,
                "repo_update_on_analyze_mode": "enabled",
                "repo_allow_ai_update_mode": "disabled",
                "repo_auto_checkout_version_mode": "disabled",
                "repo_allow_ai_switch_tag_mode": "enabled",
                "repo_max_tag_switch_rounds": 3,
                "repo_restore_latest_after_analyze_mode": "disabled",
                "repo_refuse_dirty_worktree_mode": "disabled",
                "repo_max_tool_rounds": 6,
                "repo_max_tool_calls_per_round": 4,
                "repo_max_tool_result_chars": 12000,
                "repo_max_tool_result_tokens": 4000,
                "repo_max_tool_context_tokens": 16000,
                "repo_agent_deadline_seconds": 900,
                "repo_extensions": [" .json ", ".yaml", ""],
                "repo_max_file_kb": 512,
                "repo_max_scan_files": 5000,
                "repo_max_files": 8,
                "repo_max_identifiers": 12,
                "repo_context_lines": 4,
                "repo_max_block_lines": 80,
                "repo_max_chars": 30000,
            },
        )
        repo = result["bot"]["configs"]["Alpha"]["project"]["repo"]
        self.assertEqual(repo["mode"], "agent")
        self.assertEqual(repo["path"], "/repos/maa")
        self.assertEqual(repo["url"], "https://example.com/maa.git")
        self.assertEqual(repo["mirrors"], ["https://mirror.example/maa.git"])
        self.assertEqual(repo["branch"], "dev")
        self.assertEqual(repo["default_branch"], "main")
        self.assertIs(repo["preload_enabled"], False)
        self.assertEqual(repo["preload_retries"], 5)
        self.assertIs(repo["update_on_analyze"], True)
        self.assertIs(repo["allow_ai_update"], False)
        self.assertIs(repo["auto_checkout_version"], False)
        self.assertIs(repo["allow_ai_switch_tag"], True)
        self.assertEqual(repo["max_tag_switch_rounds"], 3)
        self.assertIs(repo["restore_latest_after_analyze"], False)
        self.assertIs(repo["refuse_dirty_worktree"], False)
        self.assertEqual(repo["max_tool_rounds"], 6)
        self.assertEqual(repo["max_tool_calls_per_round"], 4)
        self.assertEqual(repo["max_tool_result_chars"], 12000)
        self.assertEqual(repo["max_tool_result_tokens"], 4000)
        self.assertEqual(repo["max_tool_context_tokens"], 16000)
        self.assertEqual(repo["agent_deadline_seconds"], 900)
        self.assertEqual(repo["extensions"], [".json", ".yaml"])
        self.assertEqual(repo["max_file_kb"], 512)
        self.assertEqual(repo["max_scan_files"], 5000)
        self.assertEqual(repo["max_files"], 8)
        self.assertEqual(repo["max_identifiers"], 12)
        self.assertEqual(repo["context_lines"], 4)
        self.assertEqual(repo["max_block_lines"], 80)
        self.assertEqual(repo["max_chars"], 30000)

    def test_invalid_repo_search_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "repo_search_mode"):
            apply_panel_overrides(
                self._config(), {"repo_search_mode": "browse"}
            )

    def test_invalid_advanced_float_and_mode_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "report_device_scale_factor"):
            apply_panel_overrides(
                self._config(),
                {"report_device_scale_factor": "not-a-number"},
            )
        with self.assertRaisesRegex(ValueError, "report_reuse_browser_mode"):
            apply_panel_overrides(
                self._config(),
                {"report_reuse_browser_mode": "sometimes"},
            )

    def test_history_panel_defaults_override_advanced_values(self):
        cfg = self._config()
        settings = cfg["bot"]["configs"]["Alpha"]["settings"]
        settings["history_enabled"] = False
        settings["history_period_hours"] = 12
        panel = dict(PANEL_DEFAULTS)

        result = apply_panel_overrides(cfg, panel)
        actual = result["bot"]["configs"]["Alpha"]["settings"]
        self.assertTrue(actual["history_enabled"])
        self.assertEqual(actual["history_period_hours"], 24)

    def test_absent_history_panel_fields_do_not_override_advanced_values(self):
        cfg = self._config()
        settings = cfg["bot"]["configs"]["Alpha"]["settings"]
        settings["history_enabled"] = False
        settings["history_period_hours"] = 12
        result = apply_panel_overrides(cfg, {"default_config": "Alpha"})
        self.assertEqual(result, cfg)

    def test_string_and_list_panel_values_are_trimmed_when_applied(self):
        cfg = self._config()
        panel = {
            "default_config": " Alpha ",
            "data_dir": " runtime ",
            "notify_group": " 10003 ",
            "log_level": "warning",
        }

        result = apply_panel_overrides(cfg, panel)

        self.assertEqual(result["bot"]["data_dir"], "runtime")
        self.assertEqual(result["log"]["log_level"], "WARNING")
        self.assertEqual(
            result["bot"]["configs"]["Alpha"]["notify_group"],
            "10003",
        )

    def test_enabled_mode_alone_overrides_selected_profile(self):
        result = apply_panel_overrides(
            self._config(), {"enabled_mode": "disabled"}
        )

        self.assertIs(
            result["bot"]["configs"]["Alpha"]["settings"]["enabled"], False
        )

    def test_invalid_panel_modes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "archive_match_mode"):
            apply_panel_overrides(
                self._config(), {"archive_match_mode": "everything"}
            )

        with self.assertRaisesRegex(
            ValueError, "followup_window_minutes"
        ):
            apply_panel_overrides(
                self._config(), {"followup_window_minutes": -2}
            )

    def test_missing_selected_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "指定配置不存在"):
            apply_panel_overrides(
                self._config(),
                {"default_config": "Missing"},
            )


class EffectiveConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin_data = Path(self.temp.name) / "plugin-data"
        self.plugin_data.mkdir()
        self.config_path = self.plugin_data / "custom-config.json"
        self.config_path.write_text(
            json.dumps(default_config(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_empty_config_file_uses_default_in_plugin_data_dir(self):
        for panel in ({}, {"config_file": ""}, {"config_file": "  "}):
            with self.subTest(panel=panel):
                effective, config_path, _ = load_effective_config(
                    panel, plugin_data_dir=self.plugin_data
                )
                self.assertEqual(config_path, (self.plugin_data / "config.json").resolve())
                self.assertTrue(config_path.is_file())
                self.assertIn("bot", effective)

    def test_panel_file_data_and_log_paths_take_explicit_priority(self):
        cfg, config_path, data_dir = load_effective_config(
            {
                "config_file": "custom-config.json",
                "data_dir": "plugin-runtime",
                "default_config": "默认",
                "listen_groups": ["777"],
                "log_level": "debug",
            },
            plugin_data_dir=self.plugin_data,
        )

        self.assertEqual(config_path, self.config_path.resolve())
        self.assertEqual(data_dir, (self.plugin_data / "plugin-runtime").resolve())
        self.assertEqual(cfg["bot"]["data_dir"], "plugin-runtime")
        self.assertEqual(
            cfg["bot"]["configs"]["默认"]["listen_groups"],
            ["777"],
        )
        self.assertEqual(cfg["log"]["log_level"], "DEBUG")

    def test_relative_configured_data_dir_is_relative_to_plugin_data(self):
        cfg = default_config()
        cfg["bot"]["data_dir"] = "state/maa"
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False),
            encoding="utf-8",
        )

        effective, _, data_dir = load_effective_config(
            {"config_file": str(self.config_path)},
            plugin_data_dir=self.plugin_data,
        )

        self.assertEqual(effective["bot"]["data_dir"], "state/maa")
        self.assertEqual(data_dir, (self.plugin_data / "state/maa").resolve())

    def test_loader_creates_default_config_when_missing(self):
        new_path = self.plugin_data / "new-config.json"

        effective, config_path, data_dir = load_effective_config(
            {"config_file": str(new_path)},
            plugin_data_dir=self.plugin_data,
        )

        self.assertTrue(config_path.is_file())
        self.assertIn("bot", effective)
        self.assertEqual(data_dir, (self.plugin_data / "data").resolve())
        profile = effective["bot"]["configs"]["默认"]
        self.assertEqual(profile["listen_groups"], [])
        self.assertEqual(profile["notify_group"], "")

    def test_first_run_panel_listeners_do_not_inherit_example_notification_group(self):
        panel = dict(PANEL_DEFAULTS)
        panel["listen_groups"] = ["10001", "10002"]

        effective, config_path, _ = load_effective_config(
            panel, plugin_data_dir=self.plugin_data
        )

        profile = effective["bot"]["configs"]["默认"]
        self.assertEqual(profile["listen_groups"], panel["listen_groups"])
        self.assertEqual(profile["notify_group"], "")
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["bot"]["configs"]["默认"]["listen_groups"], [])
        self.assertEqual(saved["bot"]["configs"]["默认"]["notify_group"], "")

    def test_blank_panel_notification_disables_legacy_json_destination(self):
        cfg = default_config()
        cfg["bot"]["configs"]["默认"]["notify_group"] = "123456789"
        original = json.dumps(cfg, ensure_ascii=False)
        self.config_path.write_text(original, encoding="utf-8")

        effective, _, _ = load_effective_config(
            {
                "config_file": str(self.config_path),
                "listen_groups": ["10001"],
                "notify_group": "",
            },
            plugin_data_dir=self.plugin_data,
        )

        self.assertEqual(
            effective["bot"]["configs"]["默认"]["notify_group"], ""
        )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)
