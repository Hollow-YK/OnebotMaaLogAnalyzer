"""Default and panel configuration for update settings."""
from __future__ import annotations

import unittest

from maa_analyzer.config import apply_panel_overrides, default_config
from maa_analyzer.core.models import UpdateConfig


class UpdateConfigTests(unittest.TestCase):
    def test_defaults_are_check_stable_and_no_auto_update(self):
        cfg = default_config()["update"]
        self.assertTrue(cfg["check_enabled"])
        self.assertEqual(cfg["channel"], "stable")
        self.assertFalse(cfg["auto_update"])
        self.assertTrue(cfg["update_templates"])
        self.assertEqual(cfg["notify_qq"], [])
        self.assertEqual(cfg["api_bases"], ["https://api.github.com"])

    def test_missing_section_uses_defaults(self):
        cfg = UpdateConfig.model_validate({})
        self.assertTrue(cfg.check_enabled)
        self.assertFalse(cfg.auto_update)

    def test_notify_qq_accepts_numbers_and_single_value(self):
        cfg = UpdateConfig.model_validate({"notify_qq": 123456})
        self.assertEqual(cfg.notify_qq, ["123456"])
        cfg = UpdateConfig.model_validate({"notify_qq": [123456, " 789 "]})
        self.assertEqual(cfg.notify_qq, ["123456", "789"])

    def test_panel_fields_map_to_update_section(self):
        result = apply_panel_overrides(default_config(), {
            "update_check_mode": "enabled",
            "update_channel": "prerelease",
            "update_auto_mode": "disabled",
            "update_download_mode": "incremental",
            "update_templates_mode": "disabled",
            "update_notify_qq": ["123"],
            "update_check_on_start_mode": "disabled",
            "update_check_interval_hours": 6,
            "update_api_bases": ["https://a.example", "https://b.example"],
            "update_download_mirrors": ["https://m/{url}"],
        })
        update = result["update"]
        self.assertEqual(update["channel"], "prerelease")
        self.assertFalse(update["auto_update"])
        self.assertFalse(update["check_on_start"])
        self.assertEqual(update["download_mode"], "incremental")
        self.assertFalse(update["update_templates"])
        self.assertEqual(update["notify_qq"], ["123"])
        self.assertEqual(update["check_interval_hours"], 6)
        self.assertEqual(
            update["api_bases"],
            ["https://a.example", "https://b.example"],
        )


if __name__ == "__main__":
    unittest.main()
