"""
追问答疑会话 — 分析完成后，通过「引用 Bot 的消息」继续提问。

触发条件（必须同时满足）:
  1. 用户发送的是**引用回复**（reply 段），且引用的 message_id
     属于 Bot 在该群发送的某条分析结果消息
  2. 该群有对应的有效会话（未超时、未超轮数）
  3. 该配置开启了追问答疑，且用户权限足够

因此普通闲聊、未引用的发言都不会触发任何响应。

会话按「群 + message_id」索引：同一次分析可能发出多条消息
（分段报告 + 图片），引用其中任意一条都能进入同一会话。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("Maa.Followup")


@dataclass
class FollowupSession:
    """一次分析的追问上下文。"""

    group_id: str
    config_name: str
    file_name: str
    created_at: float
    # 本次分析 Bot 发出的全部 message_id（引用任意一条都算）
    message_ids: set[str] = field(default_factory=set)
    # 送给模型的对话历史（含首轮摘要上下文与报告）
    messages: list[dict] = field(default_factory=list)
    # 关联的历史记录 ID（用于从历史归档重载上下文，支持跨天追问）
    history_id: str = ""
    # 本次分析的日志摘要（供追问时按名取出附件）
    digest: Any = None
    turns: int = 0
    last_active: float = 0.0

    def touch(self) -> None:
        self.last_active = time.time()

    def age_seconds(self) -> float:
        return time.time() - self.last_active


class FollowupStore:
    """按群维护追问会话，支持通过引用的 message_id 定位。"""

    def __init__(self, window_minutes: int = 30, max_turns: int = 10,
                 max_sessions: int = 50, history=None):
        # group_id → FollowupSession（每群只保留最近一次分析）
        self._sessions: dict[str, FollowupSession] = {}
        # message_id → group_id（用于引用回复反查）
        self._index: dict[str, str] = {}
        # 历史存储：内存会话被清理后，仍可从归档恢复上下文
        self.history = history
        self.window_seconds = max(60, int(window_minutes or 30) * 60)
        self.max_turns = max(1, int(max_turns or 10))
        self.max_sessions = max(1, int(max_sessions or 50))

    def set_window(self, minutes: int) -> None:
        """动态调整会话有效期（跟随历史保留期）。"""
        self.window_seconds = max(60, int(minutes or 30) * 60)

    # ── 容量与过期 ──

    def _prune(self) -> None:
        """清理过期会话及其索引；超出容量时丢弃最旧的。"""
        now = time.time()
        for key, session in list(self._sessions.items()):
            if now - session.last_active > self.window_seconds:
                self._drop(key)

        if len(self._sessions) > self.max_sessions:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_active)
            for key, _ in ordered[: len(self._sessions) - self.max_sessions]:
                self._drop(key)

    def _drop(self, group_id: str) -> None:
        session = self._sessions.pop(group_id, None)
        if session is None:
            return
        for mid in session.message_ids:
            if self._index.get(mid) == group_id:
                self._index.pop(mid, None)

    # ── 会话操作 ──

    def start(self, *, group_id: str, config_name: str, file_name: str,
              system_context: str, report: str,
              message_ids: Optional[list] = None,
              history_id: str = "", digest: Any = None) -> FollowupSession:
        """
        分析成功后创建会话。

        message_ids: 本次分析 Bot 发出的消息 ID 列表（引用它们即可追问）。
        history_id:  对应的历史记录 ID，内存会话过期后可据此恢复。
        digest:      本次分析的日志摘要，供追问时取出日志包内的附件。
        """
        self._prune()
        key = str(group_id)
        self._drop(key)   # 新分析替换旧会话

        session = FollowupSession(
            group_id=key,
            config_name=config_name,
            file_name=file_name,
            created_at=time.time(),
            message_ids={str(m) for m in (message_ids or []) if m},
            messages=[
                {"role": "user", "content": system_context},
                {"role": "assistant", "content": report},
            ],
            history_id=str(history_id or ""),
            digest=digest,
            last_active=time.time(),
        )
        self._sessions[key] = session
        for mid in session.message_ids:
            self._index[mid] = key

        logger.info(
            f"[追问] 已开启会话：群 {key} / {file_name}"
            f"（可引用 {len(session.message_ids)} 条消息）"
        )
        return session

    def register_message(self, group_id: str, message_id) -> None:
        """把后续发出的消息（如附图）登记到已有会话，使其也可被引用。"""
        session = self._sessions.get(str(group_id))
        if session is None or message_id is None:
            return
        mid = str(message_id)
        if not mid:
            return
        session.message_ids.add(mid)
        self._index[mid] = str(group_id)

    def by_message_id(self, message_id, config_name: str = "") -> Optional[FollowupSession]:
        """
        通过被引用的 message_id 定位会话。

        内存会话已过期/被清理时，若配置了历史存储，则尝试从归档恢复
        （使追问可跨越较长时间，不受内存会话有效期限制）。
        """
        self._prune()
        mid = str(message_id or "").strip()
        if not mid:
            return None

        group_id = self._index.get(mid)
        if group_id is not None:
            session = self._sessions.get(group_id)
            if session is not None and session.age_seconds() <= self.window_seconds:
                return session
            if session is not None:
                self._drop(group_id)

        # 内存里没有 → 尝试从历史归档恢复
        return self._restore_from_history(mid, config_name)

    def _restore_from_history(self, message_id: str,
                              config_name: str) -> Optional[FollowupSession]:
        """从历史记录恢复一个追问会话。"""
        if self.history is None:
            return None
        names = [config_name] if config_name else list(self.history._records.keys())
        for name in names:
            if not name:
                continue
            try:
                record = self.history.find_by_message_id(name, message_id)
            except Exception as exc:
                logger.warning(f"[追问] 查询历史失败（{name}）：{exc}")
                continue
            if record is None:
                continue

            context = self.history.read_context(name, record)
            if not context:
                logger.info(f"[追问] 历史 #{record.id} 无归档上下文，无法恢复")
                return None

            session = FollowupSession(
                group_id=str(record.group_id),
                config_name=name,
                file_name=record.file_name,
                created_at=record.created_at,
                message_ids=set(record.message_ids),
                messages=[
                    {"role": "user", "content": context},
                    {"role": "assistant", "content": record.report},
                ],
                history_id=record.id,
                last_active=time.time(),
            )
            # 载入内存，后续轮次不再重复读盘
            self._sessions[session.group_id] = session
            for mid in session.message_ids:
                self._index[mid] = session.group_id
            logger.info(
                f"[追问] 已从历史 #{record.id} 恢复会话：群 {session.group_id}"
                f" / {record.file_name}"
            )
            return session
        return None

    def get(self, group_id: str) -> Optional[FollowupSession]:
        """取出该群未过期的会话。"""
        self._prune()
        session = self._sessions.get(str(group_id))
        if session is None:
            return None
        if session.age_seconds() > self.window_seconds:
            self._drop(str(group_id))
            return None
        return session

    def append(self, group_id: str, question: str, answer: str) -> None:
        """追加一轮问答。"""
        session = self.get(group_id)
        if session is None:
            return
        session.messages.append({"role": "user", "content": question})
        session.messages.append({"role": "assistant", "content": answer})
        session.turns += 1
        session.touch()
        if session.turns >= self.max_turns:
            logger.info(f"[追问] 群 {group_id} 达到轮数上限，会话结束")
            self._drop(str(group_id))

    def close(self, group_id: str) -> bool:
        """主动结束会话。"""
        key = str(group_id)
        existed = key in self._sessions
        self._drop(key)
        return existed

    def describe(self, group_id: str) -> str:
        """会话状态描述，供指令展示。"""
        session = self.get(group_id)
        if session is None:
            return "当前群没有进行中的追问答疑会话。"
        remain_min = max(0, int((self.window_seconds - session.age_seconds()) / 60))
        return (
            f"追问答疑会话进行中\n"
            f"日志包：{session.file_name}\n"
            f"已追问：{session.turns}/{self.max_turns} 轮\n"
            f"剩余有效时间：约 {remain_min} 分钟\n"
            f"提示：引用 Bot 的分析结果消息即可提问。"
        )

    @property
    def count(self) -> int:
        self._prune()
        return len(self._sessions)
