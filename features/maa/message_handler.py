"""
消息处理器 — 处理群内文本消息（指令 + 追问答疑）。

优先级:
  1. `/maa ...` 指令 → 交给 CommandHandler
  2. **引用**了 Bot 分析结果消息的发言 → 作为追问交给 AI 续答
  3. 其他文本 → 忽略（普通闲聊不响应）

追问答疑必须引用 Bot 发送的分析结果消息，避免群里任何发言都被当作追问。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from features.maa.commands import CommandHandler
from features.maa.followup import FollowupStore
from features.maa.text_utils import split_text

if TYPE_CHECKING:
    from core.service import MaaService
    from features.maa.analyzer import MaaAnalyzer

logger = logging.getLogger("Maa.Message")


class MessageHandler:
    """把群文本消息路由到指令系统或追问答疑。"""

    def __init__(self, service: "MaaService", analyzer: "MaaAnalyzer",
                 followup: FollowupStore, history=None, segment_sender=None):
        self.s = service
        self.analyzer = analyzer
        self.followup = followup
        self.history = history
        # watcher.send_segments 回调，用于复用图文混排发送逻辑
        self.segment_sender = segment_sender
        self.commands = CommandHandler(service, followup, history_store=history)

    # ════════════════════════════════════════════════════════════
    # 入口
    # ════════════════════════════════════════════════════════════

    async def on_message_text(self, event: dict):
        """处理群内文本消息。"""
        group_id = event.get("group_id")
        if group_id is None:
            return
        text = str(event.get("_text") or "").strip()
        if not text:
            return

        group_str = str(group_id)
        user_id = str(event.get("user_id", ""))
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        role = str(sender.get("role") or "").lower()
        is_admin = role in ("admin", "owner")

        # ── 1. 指令 ──
        cfg = self.s.resolve_config(group_str)
        if cfg is not None and cfg.commands.enabled:
            args = CommandHandler.match(text, cfg.commands)
            if args is not None:
                result = await self.commands.handle(
                    group_id=group_str, user_id=user_id,
                    args=args, is_admin=is_admin,
                )
                await self._send_chunks(group_str, result.text,
                                        cfg.commands.reply_chunk_chars)
                return

        # ── 2. 追问答疑（必须引用 Bot 的分析结果消息）──
        reply_id = str(event.get("_reply_id") or "").strip()
        await self._handle_followup(group_str, user_id, text, reply_id, cfg)

    # ════════════════════════════════════════════════════════════
    # 追问答疑
    # ════════════════════════════════════════════════════════════

    async def _handle_followup(self, group_id: str, user_id: str, question: str,
                               reply_id: str, cfg) -> None:
        if not reply_id:
            return   # 未引用任何消息 → 不是追问

        settings = cfg.settings if cfg is not None else None
        if settings is not None:
            # 会话有效期跟随设置（0 = 跟随历史保留期）
            self.followup.set_window(self._window_minutes(settings))
            self.followup.set_max_turns(
                int(getattr(settings, "followup_max_turns", 10) or 10)
            )

        config_name = cfg.name if cfg is not None else ""
        session = self.followup.by_message_id(reply_id, config_name)
        if session is None:
            return   # 引用的不是 Bot 的分析结果消息

        if session.group_id != group_id:
            return   # 跨群引用，忽略

        if settings is None or not settings.followup_enabled:
            return

        commands = cfg.commands if cfg is not None else None
        if commands is not None:
            level = commands.level_of(user_id)
            if level < commands.followup_level:
                await self.s.send_text(
                    int(group_id),
                    f"权限不足：追问答疑需要等级 {commands.followup_level}。",
                )
                return

        logger.info(f"[追问] 群 {group_id} 引用 {reply_id} 提问：{question[:60]}")

        # 复用本次分析已就绪的仓库 provider；重启后内存缓存为空，
        # 这里按配置重建并确保就绪，否则仓库工具与 @项目 附件都会失效。
        provider = await self.analyzer.ensure_repo_provider(config_name, settings)
        answer = await self.analyzer.answer_followup(
            settings=settings,
            messages=session.messages,
            question=question,
            digest=getattr(session, "digest", None),
            provider=provider,
        )
        if answer is None or not answer.text:
            # 模型没给出任何可用内容（如全程只调工具、收敛也失败）。
            # 给一句可操作、可区分原因的提示，而不是笼统的「无效回复」。
            logger.warning(
                f"[追问] 群 {group_id} 未获得有效回答"
                f"（工具可用={provider is not None or getattr(session, 'digest', None) is not None}）"
            )
            await self.s.send_text(
                int(group_id),
                "这次追问没能整理出结论（可能是问题范围太大或检索未命中）。\n"
                "可以试着把问题说得更具体，或换一种问法再引用一次。",
            )
            return

        self.followup.append(group_id, question, answer.text)
        # 登记本轮回复的消息 ID，使其可被再次引用（支持连续追问）
        sent_ids = await self._send_answer(group_id, answer, settings)
        for mid in sent_ids:
            self.followup.register_message(group_id, mid)

    async def _send_answer(self, group_id: str, answer, settings) -> list:
        """
        按片段顺序发送追问回复（文本 / 图片 / 文件）。

        复用 watcher 的发送实现，使追问与主报告的图文混排行为一致。
        返回成功发出的 message_id 列表，供登记为新的引用锚点。
        """
        segments = getattr(answer, "segments", None)
        if not segments:
            return await self._send_chunks(
                group_id, answer.text, settings.reply_chunk_chars
            )

        max_chars = max(1000, int(settings.reply_chunk_chars or 3500))
        if self.segment_sender is not None:
            return await self.segment_sender(
                int(group_id), segments, settings, max_chars
            )

        # 没有 watcher 时退化为逐条发送
        sent: list = []
        for seg in segments:
            if seg.kind == "text" and seg.text.strip():
                sent.extend(
                    await self._send_chunks(group_id, seg.text, max_chars)
                )
            elif seg.kind == "image":
                try:
                    mid = await self.s.send_image(int(group_id), seg.data)
                    if mid:
                        sent.append(mid)
                except Exception as exc:
                    logger.warning(f"[追问] 发送图片失败（{seg.label}）：{exc}")
            elif seg.kind == "file":
                try:
                    await self.s.send_file(int(group_id), seg.name, seg.data)
                except Exception as exc:
                    logger.warning(f"[追问] 发送文件失败（{seg.name}）：{exc}")
        return sent

    # ════════════════════════════════════════════════════════════
    # 工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _window_minutes(settings) -> int:
        """会话有效期（分钟）；0 表示跟随历史保留期。"""
        raw = int(getattr(settings, "followup_window_minutes", 0) or 0)
        if raw > 0:
            return raw
        if not getattr(settings, "history_enabled", False):
            return 30
        hours = int(getattr(settings, "history_period_hours", 24) or 24)
        keep = int(getattr(settings, "history_keep_periods", 2) or 2)
        return max(30, hours * keep * 60)

    async def _send_chunks(self, group_id: str, text: str,
                           chunk_chars: int) -> list:
        """分段发送文本，返回成功发出的 message_id 列表。"""
        sent: list = []
        if not text:
            return sent
        for chunk in split_text(text, max(1000, int(chunk_chars or 3500))):
            mid = await self.s.send_text(int(group_id), chunk)
            if mid:
                sent.append(mid)
        return sent
