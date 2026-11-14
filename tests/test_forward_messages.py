"""QQ merged-forward transport, account routing, and failure handling."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter
from maa_analyzer.platforms.onebot.api import OneBotAPI


BODIES = [[{"type": "text", "data": {"text": "report"}}],
          [{"type": "image", "data": {"file": "base64://aW1hZ2U="}}]]


class ForwardMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_sends_custom_nodes_and_returns_outer_message_id(self):
        api = OneBotAPI()
        api._call = AsyncMock(side_effect=[
            {"user_id": 123, "nickname": "Analyzer"},
            {"message_id": 456, "forward_id": "resource-id"},
        ])
        adapter = OneBotPlatformAdapter(api)
        self.assertEqual(await adapter.send_forward_messages(789, BODIES), "456")
        self.assertEqual(api._call.await_args_list[0].args, ("get_login_info", {}))
        action, params = api._call.await_args_list[1].args
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(params["group_id"], 789)
        nodes = params["messages"]
        self.assertEqual([n["data"]["content"] for n in nodes], BODIES)
        self.assertTrue(all(n["type"] == "node" and n["data"]["uin"] == "123"
                            and n["data"]["name"] == "Analyzer" for n in nodes))

    async def test_onebot_missing_identity_never_sends_invalid_nodes(self):
        api = SimpleNamespace(get_login_info=AsyncMock(return_value=None),
                              send_group_forward_msg=AsyncMock())
        self.assertIsNone(await OneBotPlatformAdapter(api).send_forward_messages(789, BODIES))
        api.send_group_forward_msg.assert_not_awaited()

    async def test_astrbot_forward_uses_group_account_after_event_context_exits(self):
        default_bot = SimpleNamespace(call_action=AsyncMock())
        routed_bot = SimpleNamespace(call_action=AsyncMock(return_value={
            "status": "ok", "data": {"message_id": 456, "forward_id": "resource-id"},
        }))
        adapter = AstrBotPlatformAdapter(default_bot)
        event = SimpleNamespace(bot=routed_bot, message_obj=SimpleNamespace(
            raw_message={"group_id": 789, "self_id": 123},
        ), get_group_id=lambda: "789", get_messages=lambda: [])
        with adapter.event_context(event):
            pass
        self.assertEqual(await adapter.send_forward_messages(789, BODIES), "456")
        default_bot.call_action.assert_not_awaited()
        args, kwargs = routed_bot.call_action.await_args
        self.assertEqual(args, ("send_group_forward_msg",))
        self.assertEqual(kwargs["self_id"], 123)
        self.assertEqual(kwargs["group_id"], 789)
        self.assertEqual([n["data"]["content"] for n in kwargs["messages"]], BODIES)
        self.assertTrue(all(n["data"]["uin"] == "123" for n in kwargs["messages"]))

    async def test_astrbot_without_event_looks_up_bot_identity(self):
        bot = SimpleNamespace(call_action=AsyncMock(side_effect=[
            {"data": {"user_id": 123, "nickname": "Analyzer"}}, {"message_id": 456},
        ]))
        adapter = AstrBotPlatformAdapter(bot)
        self.assertEqual(await adapter.send_forward_messages(789, BODIES), "456")
        self.assertEqual(bot.call_action.await_args_list[0].args, ("get_login_info",))
        self.assertEqual(bot.call_action.await_args_list[0].kwargs, {})
        nodes = bot.call_action.await_args_list[1].kwargs["messages"]
        self.assertEqual(nodes[0]["data"]["name"], "Analyzer")

    async def test_astrbot_rejected_send_does_not_retry_or_return_resource_id(self):
        for result in ({"status": "failed", "retcode": 100, "message_id": 456},
                       {"forward_id": "resource-id"}, RuntimeError("timeout")):
            with self.subTest(result=result):
                bot = SimpleNamespace(call_action=AsyncMock(side_effect=[
                    {"user_id": 123}, result,
                ]))
                self.assertIsNone(await AstrBotPlatformAdapter(bot).send_forward_messages(789, BODIES))
                self.assertEqual(bot.call_action.await_count, 2)


if __name__ == "__main__":
    unittest.main()
