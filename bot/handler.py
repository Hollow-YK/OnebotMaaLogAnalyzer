"""
事件处理器：将 OneBot v11 事件桥接到 MaaService。

  消息事件 → handle_message（文件段 / 文本消息）
  通知事件 → handle_notice（群文件上传）
  请求事件 → handle_request（暂不处理）
"""
from __future__ import annotations

import logging

from bot.api import OneBotAPI
from core.service import MaaService

logger = logging.getLogger("Maa.Handler")


class EventHandler:
    """OneBot v11 事件 → 业务逻辑的桥梁。"""

    def __init__(self, api: OneBotAPI, service: MaaService):
        self.api = api
        self.service = service

    async def on_message(self, event: dict):
        try:
            await self.service.handle_message(event)
        except Exception:
            logger.exception("消息处理异常")

    async def on_notice(self, event: dict):
        try:
            await self.service.handle_notice(event)
        except Exception:
            logger.exception("通知处理异常")

    async def on_request(self, event: dict):
        try:
            await self.service.handle_request(event)
        except Exception:
            logger.exception("请求处理异常")
