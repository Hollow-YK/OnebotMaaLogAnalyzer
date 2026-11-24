"""Private-message routing used by update notifications."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.core.service import MaaService
from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter


class _FakeOneBotAPI:
    def __init__(self):
        self.calls = []

    async def send_private_msg(self, user_id, message):
        self.calls.append((user_id, message))
        return {"message_id": 42}


class PrivateMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_private_message(self):
        api = _FakeOneBotAPI()
        adapter = OneBotPlatformAdapter(api)
        message_id = await adapter.send_private_text(123, "hello")
        self.assertEqual(message_id, "42")
        self.assertEqual(api.calls[0][0], 123)

    async def test_astrbot_private_message(self):
        bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 7})
        )
        adapter = AstrBotPlatformAdapter(bot=bot)
        message_id = await adapter.send_private_text(123, "hello")
        self.assertEqual(message_id, "7")
        bot.call_action.assert_awaited_once()
        args, kwargs = bot.call_action.await_args
        self.assertEqual(args[0], "send_private_msg")
        self.assertEqual(kwargs["user_id"], 123)

    async def test_service_forwards_private_text(self):
        class _Port:
            def __init__(self):
                self.calls = []

            async def send_private_text(self, user_id, text):
                self.calls.append((user_id, text))
                return "9"

        port = _Port()
        service = MaaService(port=port, dm=SimpleNamespace())
        message_id = await service.send_private_text(456, "notice")
        self.assertEqual(message_id, "9")
        self.assertEqual(port.calls, [(456, "notice")])


if __name__ == "__main__":
    unittest.main()
