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

持久化:
  会话状态（消息 ID、追问问答、轮数）会写入历史归档目录的
  followups.json。因此**进程重启后**仍可引用保留周期内的旧消息
  继续追问，且追问过的轮数与上下文不会丢失。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("Maa.Followup")

# 会话首轮固定写入的种子消息数（1 条上下文 + 1 条报告），
# 持久化时只保存其后的追问问答，避免与 context.txt 重复占盘。
_SEED_MESSAGES = 2


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
    # 是否已结束（达到轮数上限或用户主动关闭）：结束后不再响应引用
    closed: bool = False

    def touch(self) -> None:
        self.last_active = time.time()

    def age_seconds(self) -> float:
        return time.time() - self.last_active

    def qa_messages(self) -> list[dict]:
        """只取追问产生的问答（不含首轮种子上下文）。"""
        return [dict(m) for m in self.messages[_SEED_MESSAGES:]]

    def to_state(self) -> dict:
        """序列化为可落盘的会话状态。"""
        return {
            "history_id": self.history_id,
            "config_name": self.config_name,
            "file_name": self.file_name,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "turns": self.turns,
            "closed": self.closed,
            "message_ids": sorted(self.message_ids),
            "messages": self.qa_messages(),
        }


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

    def set_max_turns(self, turns: int) -> None:
        """动态调整单会话最大追问轮数。"""
        self.max_turns = max(1, int(turns or 10))

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

    # ── 持久化 ──

    def _persist(self, session: FollowupSession) -> None:
        """
        把会话状态写入历史归档（按「配置 + 历史记录 ID」索引）。

        重启后 `_restore_from_history` 依赖这些状态还原追问轮数与上下文。
        写入失败只记日志，绝不影响本次追问。
        """
        if self.history is None or not session.config_name:
            return
        key = session.history_id or session.group_id
        try:
            self.history.set_followup(
                session.config_name, key, session.to_state()
            )
        except Exception as exc:
            logger.warning(f"[追问] 保存会话状态失败：{exc}")

    def _forget(self, session: FollowupSession) -> None:
        """标记会话已结束并落盘，使重启后不再恢复。"""
        if self.history is None or not session.config_name:
            return
        session.closed = True
        self._persist(session)

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
        # 新分析替换该群**当前**会话（旧记录仍留在历史里，可继续被引用）
        self._drop(key)

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

        # 历史记录中补上本次分析的消息 ID（重启后据此反查记录）
        if self.history is not None and session.history_id:
            try:
                self.history.add_message_ids(
                    config_name, session.history_id, sorted(session.message_ids)
                )
            except Exception as exc:
                logger.warning(f"[追问] 归档消息 ID 失败：{exc}")
        self._persist(session)

        logger.info(
            f"[追问] 已开启会话：群 {key} / {file_name}"
            f"（可引用 {len(session.message_ids)} 条消息）"
        )
        return session

    def register_message(self, group_id: str, message_id) -> None:
        """
        把后续发出的消息（如附图、追问回复）登记到已有会话，使其也可被引用。

        追问回复的 message_id 会同时写入历史归档，因此重启后引用
        「上一条追问的回复」仍能进入同一会话。
        """
        session = self._sessions.get(str(group_id))
        if session is None or message_id is None:
            return
        mid = str(message_id)
        if not mid:
            return
        session.message_ids.add(mid)
        self._index[mid] = str(group_id)
        if self.history is not None and session.history_id:
            try:
                self.history.add_message_ids(
                    session.config_name, session.history_id, [mid]
                )
            except Exception as exc:
                logger.warning(f"[追问] 归档消息 ID 失败：{exc}")
        self._persist(session)

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
                return None if session.closed else session
            if session is not None:
                self._drop(group_id)

        # 内存里没有 → 尝试从历史归档恢复
        return self._restore_from_history(mid, config_name)

    def _restore_from_history(self, message_id: str,
                              config_name: str) -> Optional[FollowupSession]:
        """
        从历史记录恢复一个追问会话。

        除首轮上下文（context.txt）与报告外，还会还原：
          - 已追问的轮数与问答历史（followups.json）
          - 日志摘要索引（由归档 zip 重建），使 `@日志` 附件与日志工具可用
        """
        if self.history is None:
            return None
        names = [config_name] if config_name else self._history_config_names()
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
                digest=self._restore_digest(name, record),
                last_active=time.time(),
            )
            # 还原追问轮数与问答历史
            self._apply_saved_state(session, name)
            if session.closed:
                # 会话已结束（达到轮数上限或用户关闭）：不再恢复
                logger.info(f"[追问] 历史 #{record.id} 的会话已结束，不恢复")
                return None

            # 载入内存，后续轮次不再重复读盘
            self._sessions[session.group_id] = session
            for mid in session.message_ids:
                self._index[mid] = session.group_id
            logger.info(
                f"[追问] 已从历史 #{record.id} 恢复会话：群 {session.group_id}"
                f" / {record.file_name}（已追问 {session.turns} 轮）"
            )
            return session
        return None

    def _history_config_names(self) -> list[str]:
        """当前历史存储中已知的全部配置名。"""
        names = list(getattr(self.history, "_records", {}) or {})
        lister = getattr(self.history, "list_configs", None)
        if callable(lister):
            try:
                names.extend(lister())
            except Exception as exc:
                logger.warning(f"[追问] 枚举历史配置失败：{exc}")
        return sorted({n for n in names if n})

    def _apply_saved_state(self, session: FollowupSession, name: str) -> None:
        """把落盘的会话状态（追问问答 / 轮数 / 消息 ID）合并进恢复的会话。"""
        getter = getattr(self.history, "get_followup", None)
        if not callable(getter):
            return
        try:
            state = getter(name, session.history_id)
        except Exception as exc:
            logger.warning(f"[追问] 读取会话状态失败：{exc}")
            return
        if not isinstance(state, dict):
            return

        for item in (state.get("messages") or []):
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                session.messages.append({
                    "role": str(item["role"]),
                    "content": str(item.get("content") or ""),
                })
        try:
            session.turns = max(0, int(state.get("turns") or 0))
        except (TypeError, ValueError):
            session.turns = 0
        session.closed = bool(state.get("closed"))

        for mid in (state.get("message_ids") or []):
            mid = str(mid or "").strip()
            if mid:
                session.message_ids.add(mid)
        # 落盘的 last_active 用于判断会话是否仍在保留期内
        try:
            saved = float(state.get("last_active") or 0)
        except (TypeError, ValueError):
            saved = 0.0
        if saved > 0:
            session.last_active = saved

    def _restore_digest(self, name: str, record) -> Any:
        """
        由归档 zip 重建一个「仅索引」的摘要结果。

        重启后没有原始 DigestResult，但附件与日志工具只需要 zip 路径
        和成员列表，因此这里重新扫描归档包即可。
        """
        if self.history is None:
            return None
        try:
            zip_path = self.history.read_zip(name, record)
        except Exception as exc:
            logger.warning(f"[追问] 读取归档日志包失败：{exc}")
            return None
        if zip_path is None:
            return None
        try:
            from features.maa.log_digest import describe_zip

            digest = describe_zip(str(zip_path))
        except Exception as exc:
            logger.warning(f"[追问] 重建日志索引失败：{exc}")
            return None
        if digest is not None:
            logger.info(
                f"[追问] 已由归档日志包重建索引（{len(digest.all_members)} 个成员）"
            )
        return digest

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
        """追加一轮问答（达到轮数上限时结束会话并落盘标记）。"""
        session = self.get(group_id)
        if session is None:
            return
        session.messages.append({"role": "user", "content": question})
        session.messages.append({"role": "assistant", "content": answer})
        session.turns += 1
        session.touch()
        if session.turns >= self.max_turns:
            logger.info(f"[追问] 群 {group_id} 达到轮数上限，会话结束")
            session.closed = True
            self._persist(session)
            self._drop(str(group_id))
            return
        self._persist(session)

    def close(self, group_id: str) -> bool:
        """主动结束会话（同时清除已落盘状态，重启后不再恢复）。"""
        key = str(group_id)
        session = self._sessions.get(key)
        existed = session is not None
        if session is not None:
            self._forget(session)
        else:
            # 内存里没有：可能是重启后由历史归档恢复的会话
            existed = self._forget_persisted(key)
        self._drop(key)
        return existed

    def close(self, group_id: str) -> bool:
        """主动结束会话（落盘标记，重启后也不再恢复）。"""
        key = str(group_id)
        session = self._sessions.get(key)
        existed = session is not None
        if session is not None:
            self._forget(session)
        else:
            # 内存里没有：可能是重启后由历史归档恢复的会话
            existed = self._mark_closed_persisted(key)
        self._drop(key)
        return existed

    def _mark_closed_persisted(self, group_id: str) -> bool:
        """把落盘的会话状态标记为已结束（用于内存中无会话的 close）。"""
        if self.history is None:
            return False
        removed = False
        for name in self._history_config_names():
            try:
                states = self.history.load_followups(name) or {}
                for key, state in list(states.items()):
                    if not isinstance(state, dict):
                        continue
                    record = self.history.find_by_id(
                        name, str(state.get("history_id") or "")
                    )
                    if record is None or record.group_id != str(group_id):
                        continue
                    state["closed"] = True
                    self.history.set_followup(name, key, state)
                    removed = True
            except Exception as exc:
                logger.warning(f"[追问] 结束落盘会话失败（{name}）：{exc}")
        return removed

    def describe(self, group_id: str) -> str:
        """会话状态描述，供指令展示（内存无会话时尝试从历史恢复）。"""
        session = self.get(group_id)
        if session is None:
            session = self._restore_latest_for_group(group_id)
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

    def _restore_latest_for_group(self, group_id: str) -> Optional[FollowupSession]:
        """重启后按群恢复最近一条历史记录对应的会话（供指令查询状态）。"""
        if self.history is None:
            return None
        for name in self._history_config_names():
            try:
                record = self.history.find_latest_by_group(name, str(group_id))
            except Exception as exc:
                logger.warning(f"[追问] 查询历史失败（{name}）：{exc}")
                continue
            if record is None:
                continue
            anchor = next((mid for mid in record.message_ids if mid), "")
            if anchor:
                session = self._restore_from_history(anchor, name)
                if session is not None:
                    return session
        return None

    @property
    def count(self) -> int:
        self._prune()
        return len(self._sessions)
