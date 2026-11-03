"""
指令权限：默认等级与子指令提权。

Run with: python -m unittest discover -s tests -v
"""
import unittest

from core.models import CommandConfig
from features.maa.commands import required_level


class DefaultLevelTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        commands = CommandConfig()
        self.assertEqual(commands.read_level, 1, "查看类默认只对管理员开放")
        self.assertEqual(commands.write_level, 2)
        self.assertEqual(commands.followup_level, 0, "追问答疑仍对成员开放")


class RequiredLevelTests(unittest.TestCase):
    def setUp(self):
        self.commands = CommandConfig()

    # 与 handle() 的映射表一致：这些指令的基线取 write_level
    _WRITE_ACTIONS = {"set", "mode", "enable", "disable", "reload"}

    def _level(self, action, rest=(), base=None):
        if base is None:
            if action == "followup":
                base = self.commands.followup_level
            elif action in self._WRITE_ACTIONS:
                base = self.commands.write_level
            else:
                base = self.commands.read_level
        return required_level(self.commands, action, list(rest), base=base)

    def test_help_always_open(self):
        self.assertEqual(self._level("help", base=2), 0)

    def test_read_commands_need_admin(self):
        for action in ("status", "get", "project", "repo", "jobs", "history", "ai"):
            self.assertEqual(self._level(action), 1, action)

    def test_write_commands_need_owner(self):
        for action in ("set", "mode", "enable", "disable", "reload"):
            self.assertEqual(self._level(action), 2, action)

    def test_read_commands_can_be_opened_explicitly(self):
        commands = CommandConfig(read_level=0)
        self.assertEqual(
            required_level(commands, "status", [], base=commands.read_level), 0
        )

    def test_write_subcommands_are_promoted(self):
        self.assertEqual(self._level("repo", ["mode"]), 2)
        self.assertEqual(self._level("repo", ["mirror", "add"]), 2)
        self.assertEqual(self._level("repo", ["preload", "on"]), 2)
        self.assertEqual(self._level("history", ["clean"]), 2)
        self.assertEqual(self._level("history", ["purge"]), 2)
        self.assertEqual(self._level("project", ["set", "name", "MAK"]), 2)

    def test_read_subcommands_stay_read(self):
        self.assertEqual(self._level("repo"), 1)
        self.assertEqual(self._level("history"), 1)
        self.assertEqual(self._level("history", ["status"]), 1)
        self.assertEqual(self._level("project", ["get"]), 1)
        # 旧行为保留：`mirror` 只要出现就按写权限处理（`/maa repo mirror add|remove`）
        self.assertEqual(self._level("repo", ["mirror"]), 2)

    def test_followup_status_open_but_close_needs_admin(self):
        self.assertEqual(self._level("followup"), 0)
        self.assertEqual(self._level("followup", ["status"]), 0)
        self.assertEqual(self._level("followup", ["close"]), 1, "结束会话是写操作")
        self.assertEqual(self._level("followup", ["end"]), 1)
        self.assertEqual(self._level("followup", ["结束"]), 1)

    def test_followup_close_follows_higher_followup_level(self):
        commands = CommandConfig(followup_level=2)
        self.assertEqual(
            required_level(commands, "followup", ["close"],
                           base=commands.followup_level),
            2,
            "配置把追问抬到 2 时，close 不能反而更低",
        )

    def test_case_insensitive_subcommand(self):
        self.assertEqual(self._level("followup", ["CLOSE"]), 1)
        self.assertEqual(self._level("repo", ["MODE"]), 2)


if __name__ == "__main__":
    unittest.main()