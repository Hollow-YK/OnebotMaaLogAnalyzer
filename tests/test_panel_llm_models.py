import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_analyzer.config import (apply_panel_overrides, default_config,
                                 load_effective_config, synchronize_panel_config)
from maa_analyzer.core.llm import ChatMessage, LLMError
from maa_analyzer.core.llm_pool import LLMPool
from maa_analyzer.core.models import ConfigLLM


class PanelLLMModelsTests(unittest.TestCase):
    def _default_config(self):
        return default_config()

    def test_candidates_configure_each_model_on_same_endpoint(self):
        panel = {"llm_models": [
            {"name": "default", "model": "visual", "support_vision_mode": "enabled",
             "model_max_context_tokens": 128000},
            {"name": "default", "model": "text", "support_vision_mode": "disabled",
             "support_json_mode_mode": "enabled", "model_max_context_tokens": 32000},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _, _ = load_effective_config(panel, plugin_data_dir=tmp)
        models = {m["model"]: m for m in cfg["llm"]["providers"][0]["models"]}
        self.assertTrue(models["visual"]["supports"]["vision"])
        self.assertFalse(models["text"]["supports"]["vision"])
        self.assertTrue(models["text"]["supports"]["json_mode"])
        self.assertEqual(models["visual"]["max_context_tokens"], 128000)
        self.assertEqual(models["text"]["max_context_tokens"], 32000)
        self.assertEqual([m["model"] for m in cfg["bot"]["configs"]["默认"]["llm"]["models"]],
                         ["visual", "text"])

    def test_endpoint_model_properties_move_to_matching_candidate(self):
        config = {"llm": {"llm_endpoints_migrated": True, "llm_endpoints": [
            {"name": "default", "model": "visual", "support_vision_mode": "enabled",
             "model_max_tokens": 4000}], "llm_models": [
            {"name": "default", "model": "visual"}, {"name": "default", "model": "text"}]}}
        panel, changed = synchronize_panel_config(config)
        self.assertTrue(changed)
        self.assertEqual(config["llm"]["llm_models"][0]["support_vision_mode"], "enabled")
        self.assertEqual(config["llm"]["llm_models"][0]["model_max_tokens"], 4000)
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _, _ = load_effective_config(panel, plugin_data_dir=tmp)
        model = next(m for m in cfg["llm"]["providers"][0]["models"] if m["model"] == "visual")
        self.assertTrue(model["supports"]["vision"])
        self.assertEqual(model["max_tokens"], 4000)
        self.assertFalse(synchronize_panel_config(config)[1])

    def test_multiple_panel_endpoints_and_ordered_candidates_load(self):
        panel = {
            "llm_endpoints": [
                {"name": "primary", "kind": "openai", "base_url": "https://one.test/v1",
                 "api_key": "key-one", "model": "first", "support_vision_mode": "enabled"},
                {"name": "backup", "kind": "anthropic", "base_url": "https://two.test",
                 "api_key": "key-two", "model": "second", "support_json_mode_mode": "enabled"},
            ],
            "llm_models": [{"name": "primary", "model": "first"},
                           {"name": "backup", "model": "second"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _, _ = load_effective_config(panel, plugin_data_dir=tmp)
        providers = {p["name"]: p for p in cfg["llm"]["providers"]}
        self.assertEqual(providers["primary"]["api_key"], "key-one")
        self.assertEqual(providers["backup"]["kind"], "anthropic")
        self.assertTrue(providers["primary"]["models"][0]["supports"]["vision"])
        self.assertTrue(providers["backup"]["models"][0]["supports"]["json_mode"])
        self.assertEqual(cfg["bot"]["configs"]["默认"]["llm"]["models"], panel["llm_models"])
        self.assertIn("default", providers)

    def test_duplicate_panel_endpoint_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "端点名重复"):
            apply_panel_overrides(default_config(), {"llm_endpoints": [
                {"name": "same", "base_url": "https://one.test"},
                {"name": "same", "base_url": "https://two.test"},
            ]})

    def test_old_endpoint_migrates_to_both_files_without_resurrection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(default_config()), encoding="utf-8")
            old = {"llm": {"llm_endpoint_base_url": "https://example.test/v1",
                           "llm_endpoint_api_key": "secret", "llm_endpoint_model": "old-model",
                           "llm_model_support_vision_mode": "enabled"}}
            runtime, changed = synchronize_panel_config(old)
            self.assertTrue(changed)
            endpoint = old["llm"]["llm_endpoints"][0]
            self.assertEqual(endpoint["base_url"], "https://example.test/v1")
            self.assertEqual(endpoint["api_key"], "secret")
            self.assertTrue(old["llm"]["llm_endpoints_migration_pending"])
            effective, _, _ = load_effective_config(runtime, plugin_data_dir=tmp)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["llm"]["providers"][0]["base_url"], "https://example.test/v1")
            self.assertEqual(persisted["llm"]["providers"][0], effective["llm"]["providers"][0])
            self.assertTrue((Path(tmp) / "config.json.panel-migration.bak").exists())
            old["llm"]["llm_endpoints_migration_pending"] = False
            old["llm"]["llm_endpoints"] = []
            after, _ = synchronize_panel_config(old)
            self.assertEqual(after["llm_endpoints"], [])
            self.assertNotIn("_persist_endpoint_migration", after)

    def test_llm_models_use_array_order_for_fallback(self):
        result = apply_panel_overrides(self._default_config(), {
            "default_config": "默认",
            "llm_models": [
                {"model": "deepseek-flash"},
                {"name": "backup", "model": "backup-model"},
            ],
        })
        profile = result["bot"]["configs"]["默认"]
        self.assertEqual(profile["llm"]["models"], [
            {"name": "default", "model": "deepseek-flash"},
            {"name": "backup", "model": "backup-model"},
        ])

    def test_empty_llm_models_preserve_existing_candidates(self):
        cfg = self._default_config()
        cfg["bot"]["configs"]["默认"]["llm"]["models"] = [
            {"name": "alpha", "model": "alpha-model"},
        ]
        result = apply_panel_overrides(cfg, {
            "default_config": "默认",
            "llm_models": [],
        })
        self.assertEqual(result["bot"]["configs"]["默认"]["llm"]["models"], [
            {"name": "alpha", "model": "alpha-model"},
        ])

    def test_upgrade_reconciliation_migration_and_effective_load(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "astrbot_plugin/_conf_schema.json").read_text(encoding="utf-8"))
        # Model AstrBot's documented recursive schema reconciliation.
        def reconcile(nodes, saved):
            return {key: (reconcile(node["items"], saved.get(key, {}))
                          if node["type"] == "object" else copy.deepcopy(saved.get(key, node.get("default"))))
                    for key, node in nodes.items()}

        for grouped in (True, False):
            with self.subTest(grouped=grouped), tempfile.TemporaryDirectory() as tmp:
                old = {"llm_provider": "default", "llm_model": "deepseek-flash",
                       "llm_fallback_models": ["default:deepseek-v4-pro"]}
                saved = reconcile(schema, {"llm": old} if grouped else old)
                runtime, changed = synchronize_panel_config(saved)
                self.assertTrue(changed)
                self.assertEqual(saved["llm"]["llm_models"], [
                    {"__template_key": "model", "name": "default", "model": "deepseek-flash"},
                    {"__template_key": "model", "name": "default", "model": "deepseek-v4-pro"},
                ])
                self.assertEqual(saved["llm"]["llm_model"], "")
                self.assertEqual(saved["llm_model"], "")
                self.assertFalse(synchronize_panel_config(saved)[1])
                cfg, _, _ = load_effective_config(runtime, plugin_data_dir=tmp)
                self.assertEqual(cfg["bot"]["configs"]["默认"]["llm"]["models"], [
                    {"name": "default", "model": "deepseek-flash"},
                    {"name": "default", "model": "deepseek-v4-pro"},
                ])
                self.assertTrue({"deepseek-flash", "deepseek-v4-pro"}.issubset(
                    {m["model"] for m in cfg["llm"]["providers"][0]["models"]}))
                persisted = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["bot"]["configs"]["默认"]["llm"]["models"],
                                 cfg["bot"]["configs"]["默认"]["llm"]["models"])

    def test_existing_json_is_backed_up_and_other_profiles_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            old = default_config()
            old["bot"]["configs"]["other"] = copy.deepcopy(old["bot"]["configs"]["默认"])
            path.write_text(json.dumps(old), encoding="utf-8")
            panel, _ = synchronize_panel_config({"llm": {"llm_model": "new-model"}})
            load_effective_config(panel, plugin_data_dir=tmp)
            saved = json.loads(path.read_text(encoding="utf-8"))
            backup = json.loads((Path(tmp) / "config.json.panel-migration.bak").read_text(encoding="utf-8"))
            self.assertEqual(backup, old)
            self.assertEqual(saved["bot"]["configs"]["other"], old["bot"]["configs"]["other"])
            self.assertEqual(saved["bot"]["configs"]["默认"]["llm"]["models"][0]["model"], "new-model")
            load_effective_config(panel, plugin_data_dir=tmp)
            self.assertEqual(json.loads((Path(tmp) / "config.json.panel-migration.bak").read_text(encoding="utf-8")), old)

    def test_failed_validation_keeps_both_migration_state_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            old = default_config()
            path.write_text(json.dumps(old), encoding="utf-8")
            panel_config = {"llm": {"llm_provider": "missing", "llm_model": "model"}}
            runtime, _ = synchronize_panel_config(panel_config)
            with self.assertRaisesRegex(ValueError, "不存在的端点"):
                load_effective_config(runtime, plugin_data_dir=tmp)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old)
            self.assertTrue(panel_config["llm"]["llm_migration_pending"])
            self.assertTrue(synchronize_panel_config(panel_config)[0]["_persist_llm_migration"])

    def test_new_array_wins_and_old_fields_cannot_resurrect(self):
        config = {"llm": {"llm_models": [{"__template_key": "model", "name": "default", "model": "new"}],
                          "llm_model": "old", "llm_fallback_models": ["default:old-backup"]}}
        runtime, _ = synchronize_panel_config(config)
        self.assertEqual(runtime["llm_models"][0]["model"], "new")
        config["llm"]["llm_models"] = []
        self.assertEqual(synchronize_panel_config(config)[0]["llm_models"], [])

    def test_legacy_auto_primary_uses_endpoint_model_or_requires_selection(self):
        config = {"llm": {"llm_model": "", "llm_endpoint_model": "selected",
                          "llm_fallback_models": ["default:backup"]}}
        runtime, _ = synchronize_panel_config(config)
        self.assertEqual(runtime["llm_models"][0]["model"], "selected")
        config = {"llm": {"llm_fallback_models": ["default:backup"]}}
        runtime, _ = synchronize_panel_config(config)
        with self.assertRaisesRegex(ValueError, "模型名"):
            apply_panel_overrides(default_config(), runtime)

    def test_invalid_legacy_fallback_is_not_erased(self):
        config = {"llm": {"llm_model": "main", "llm_fallback_models": ["broken"]}}
        before = copy.deepcopy(config)
        with self.assertRaisesRegex(ValueError, "llm_fallback_models"):
            synchronize_panel_config(config)
        self.assertEqual(config, before)


class PanelLLMFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_switches_between_panel_endpoints(self):
        panel = {
            "llm_endpoints": [
                {"name": "first", "base_url": "https://first.test", "max_retries": 0},
                {"name": "second", "base_url": "https://second.test", "max_retries": 0},
            ],
            "llm_models": [{"name": "first", "model": "model-a"},
                           {"name": "second", "model": "model-b"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _, _ = load_effective_config(panel, plugin_data_dir=tmp)
        pool = LLMPool(cfg["llm"])
        profile = ConfigLLM.model_validate(cfg["bot"]["configs"]["默认"]["llm"])
        attempted = []

        async def first(*args, **kwargs):
            attempted.append(("first", kwargs["model"]))
            raise LLMError("failed", kind="other")

        async def second(*args, **kwargs):
            attempted.append(("second", kwargs["model"]))
            return ChatMessage(content="ok")

        with patch.object(pool.clients["first"], "chat_messages", side_effect=first), \
             patch.object(pool.clients["second"], "chat_messages", side_effect=second):
            reply = await pool.chat(profile, [], use_breaker=False)
        self.assertEqual(reply.content, "ok")
        self.assertEqual(attempted, [("first", "model-a"), ("second", "model-b")])

    async def test_migrated_candidates_are_attempted_in_order(self):
        panel, _ = synchronize_panel_config({"llm": {
            "llm_model": "first", "llm_fallback_models": ["default:second"],
            "llm_endpoint_max_retries": 0,
        }})
        cfg = apply_panel_overrides(default_config(), panel)
        pool = LLMPool(cfg["llm"])
        profile = ConfigLLM.model_validate(cfg["bot"]["configs"]["默认"]["llm"])
        attempted = []

        async def chat(_messages, **kwargs):
            attempted.append(kwargs["model"])
            if kwargs["model"] == "first":
                raise LLMError("failed", kind="other")
            return ChatMessage(content="ok")

        with patch.object(pool.clients["default"], "chat_messages", side_effect=chat):
            result = await pool.chat(profile, [], use_breaker=False)
        self.assertEqual(result.content, "ok")
        self.assertEqual(attempted, ["first", "second"])
