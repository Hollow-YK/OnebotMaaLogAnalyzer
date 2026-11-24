"""/maa update command output and permission levels."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from maa_analyzer.core.models import CommandConfig
from maa_analyzer.features.maa.commands import CommandHandler, required_level


class _FakeUpdater:
    def __init__(self):
        self.config = SimpleNamespace(channel="stable")
        self.checked: list[str | None] = []
        self.applied: list[str | None] = []

    def describe_status(self) -> str:
        return "status text"

    async def check(self, *, channel=None, auto_apply=False, notify=True):
        self.checked.append(channel)
        return SimpleNamespace(latest=None)

    def format_check_result(self, result) -> str:
        return "check text"

    async def request_apply(self, channel=None) -> str:
        self.applied.append(channel)
        return "apply text"


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_check_and_apply(self):
        updater = _FakeUpdater()
        handler = CommandHandler(SimpleNamespace(), updater=updater)
        cfg = SimpleNamespace(commands=SimpleNamespace(prefix="/maa"))
        self.assertEqual((await handler._cmd_update(cfg, [])).text, "status text")
        check = await handler._cmd_update(cfg, ["check", "pre"])
        self.assertEqual(check.text, "check text")
        self.assertEqual(updater.checked, ["pre"])
        apply = await handler._cmd_update(cfg, ["apply", "stable"])
        self.assertEqual(apply.text, "apply text")
        self.assertEqual(updater.applied, ["stable"])

    async def test_unknown_subcommand_returns_usage(self):
        handler = CommandHandler(SimpleNamespace(), updater=_FakeUpdater())
        cfg = SimpleNamespace(commands=SimpleNamespace(prefix="/maa"))
        result = await handler._cmd_update(cfg, ["wat"])
        self.assertFalse(result.ok)
        self.assertIn("用法", result.text)

    def test_apply_requires_write_level(self):
        commands = CommandConfig(write_level=2)
        self.assertEqual(required_level(commands, "update", ["apply"]), 2)
        self.assertEqual(required_level(commands, "update", ["check"]), 1)


if __name__ == "__main__":
    unittest.main()
