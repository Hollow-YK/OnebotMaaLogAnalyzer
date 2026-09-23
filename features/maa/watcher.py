"""
日志包监听器 — 日志分析的主要触发入口。

只对群内上传的日志压缩包作出反应：
  - notice.group_upload   群文件上传通知（主要触发路径）
  - message.file          消息中的文件段（部分 OneBot 实现以此上报）

文本消息（/maa 指令、引用式追问）由 MessageHandler 处理。
"""
from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from core.models import ConfigState, LogFileItem
from features.maa.analyzer import MaaAnalyzer
from features.maa.text_utils import format_bytes, split_text

if TYPE_CHECKING:
    from core.service import MaaService

logger = logging.getLogger("Maa.Watch")


class LogWatcher:
    """监听群文件上传，匹配到日志包时自动下载并分析。"""

    def __init__(self, service: "MaaService", analyzer: MaaAnalyzer,
                 followup=None, history=None):
        self.s = service
        self.analyzer = analyzer
        self.followup = followup
        self.history = history
        # (群, 文件名, 大小) → 最近一次处理时间（用于跨入口去重）
        self._recent: dict[tuple[str, str, int], float] = {}

    # ════════════════════════════════════════════════════════════
    # 去重
    # ════════════════════════════════════════════════════════════

    def _is_duplicate(self, group_id: str, file_name: str,
                      file_size: Optional[int], settings) -> bool:
        """
        判断是否为重复触发。

        同一文件可能同时经 notice.group_upload 与 message.file 上报，
        在 duplicate_window_seconds 内只处理一次。窗口 <= 0 时不去重。
        """
        window = int(getattr(settings, "duplicate_window_seconds", 0) or 0)
        if window <= 0:
            return False

        now = time.monotonic()
        key = (str(group_id), str(file_name), int(file_size or 0))

        # 顺手清理过期条目，避免长期运行后无限增长
        if len(self._recent) > 256:
            cutoff = now - window
            self._recent = {
                k: t for k, t in self._recent.items() if t >= cutoff
            }

        last = self._recent.get(key)
        if last is not None and (now - last) < window:
            return True

        self._recent[key] = now
        return False

    # ════════════════════════════════════════════════════════════
    # 事件入口
    # ════════════════════════════════════════════════════════════

    async def on_group_upload(self, event: dict):
        """notice.group_upload — 群文件上传通知。"""
        group_id = event.get("group_id")
        if group_id is None:
            return

        file_info = event.get("file") if isinstance(event.get("file"), dict) else event
        file_name = self._pick_str(
            file_info, "name", "file_name", "filename", "file"
        )
        if not file_name:
            return

        await self._handle(
            group_id=str(group_id),
            file_name=file_name,
            file_info=file_info,
            uploader=str(event.get("user_id", "")),
        )

    async def on_message_file(self, event: dict):
        """message.file — 消息中的文件段（由 service 注入 _file_segments）。"""
        group_id = event.get("group_id")
        if group_id is None:
            return

        uploader = str(event.get("user_id", ""))
        for seg in event.get("_file_segments") or []:
            if not isinstance(seg, dict):
                continue
            file_name = self._pick_str(seg, "file_name", "name", "file", "filename")
            if not file_name:
                continue
            await self._handle(
                group_id=str(group_id),
                file_name=file_name,
                file_info=seg,
                uploader=uploader,
            )

    # ════════════════════════════════════════════════════════════
    # 处理逻辑
    # ════════════════════════════════════════════════════════════

    async def _handle(self, *, group_id: str, file_name: str,
                      file_info: dict, uploader: str) -> None:
        """判断是否为目标日志包，是则执行分析。"""
        cfg = self.s.resolve_config(group_id)
        if cfg is None:
            # 群未被监听，或同时属于多个配置（无法确定使用哪套设置）
            return

        settings = cfg.settings
        if not settings.enabled:
            return
        if not settings.matches(file_name):
            return

        file_size = self._pick_int(file_info, "size", "file_size", "fileSize")

        # 同一上传可能经多条路径到达（如 notice.group_upload 与 message.file
        # 同时上报），在去重窗口内只处理一次。
        if self._is_duplicate(group_id, file_name, file_size, settings):
            logger.info(
                f"[监听] 跳过重复触发：{file_name}（群 {group_id}，"
                f"{settings.duplicate_window_seconds}s 内已处理过）"
            )
            return

        if file_size and file_size > settings.max_zip_bytes():
            await self.s.send_text(
                int(group_id),
                f"检测到日志包 {file_name}，但文件大小 {format_bytes(file_size)} "
                f"超过限制 {format_bytes(settings.max_zip_bytes())}，已跳过。",
            )
            self._record_skip(cfg, group_id, file_name, file_info, file_size, uploader,
                              "文件超过大小限制")
            return

        file_id = self._pick_str(file_info, "id", "file_id", "fileId") or None

        if settings.send_progress_message:
            await self.s.send_text(
                int(group_id),
                f"检测到日志包：{file_name}\n正在下载并分析，请稍等。",
            )

        await self._analyze(
            cfg, group_id, uploader,
            LogFileItem(
                file_id=file_id or "",
                file_name=file_name,
                size=int(file_size or 0),
            ),
        )

    async def _analyze(self, cfg: ConfigState, group_id: str,
                       uploader: str, file_info: LogFileItem) -> None:
        """执行分析并发送全部回复（含错误提示）。"""
        settings = cfg.settings
        group_int = int(group_id)

        if not file_info.file_id:
            await self.s.send_text(
                group_int,
                f"日志包 {file_info.file_name} 缺少 file_id，无法下载分析。",
            )
            return

        async def progress(text: str):
            if settings.send_progress_message:
                await self.s.send_text(group_int, text)

        outcome = await self.analyzer.analyze(
            config_name=cfg.name,
            settings=settings,
            group_id=group_int,
            file_name=file_info.file_name,
            file_id=file_info.file_id,
            file_size=file_info.size or None,
            uploader=uploader,
            progress=progress,
        )

        if not settings.send_summary:
            return

        max_chars = max(1000, int(settings.reply_chunk_chars or 3500))
        # 按片段顺序发送（文本 / 图片 / 文件），收集 message_id 供引用追问
        sent_ids = await self._send_outcome(group_int, outcome, settings, max_chars)

        # 把消息 ID 补写到历史归档（使引用可跨天定位）
        self._link_history(cfg, outcome, sent_ids)

        # 开启追问答疑会话（以本次分析发出的消息为引用锚点）
        self._start_followup(cfg, group_id, outcome, sent_ids)

        # 报告发给通知群（若与监听群不同）
        notify = cfg.info.notify_group
        if notify and notify != group_id:
            await self._send_outcome(
                int(notify), outcome, settings, max_chars, link_history=False
            )

    async def _send_outcome(self, group_id: int, outcome, settings,
                            max_chars: int, *, link_history: bool = True) -> list:
        """
        按报告片段顺序发送内容，返回成功发出的 message_id 列表。

        片段保持模型给出的位置，因此附件可以插在文字中间。
        """
        sent: list = []
        segments = None
        if hasattr(outcome, "output_segments"):
            segments = outcome.output_segments()
        else:
            segments = getattr(outcome, "segments", None)

        if not segments:
            # 兜底：没有片段时按纯文本发送
            for chunk in outcome.report_chunks(max_chars):
                mid = await self.s.send_text(group_id, chunk)
                if mid:
                    sent.append(mid)
            return sent

        return await self.send_segments(group_id, segments, settings, max_chars)

    async def send_segments(self, group_id: int, segments: list,
                            settings, max_chars: int) -> list:
        """
        按片段顺序发送，图片与相邻文字合并为同一条消息（图文混排）。

        这样模型把 [附图: ...] 写在两段文字之间时，图片会真正出现在
        两段文字中间，而不是被拆成三条独立消息。
        文件附件无法混排，单独作为群文件上传。
        """
        sent: list = []
        # 待合并的图文混排缓冲区（元素为消息段 dict）
        buffer: list[dict] = []
        buffer_len = 0

        async def flush() -> None:
            nonlocal buffer, buffer_len
            if not buffer:
                return
            mid = await self.s.send_message(group_id, list(buffer))
            if mid:
                sent.append(mid)
            buffer = []
            buffer_len = 0

        for seg in segments:
            if seg.kind == "text":
                text = seg.text
                if not text.strip():
                    continue
                # 文本超过单条上限时先冲刷，再分段续发
                for chunk in split_text(text, max_chars):
                    if buffer_len + len(chunk) > max_chars and buffer:
                        await flush()
                    buffer.append({"type": "text", "data": {"text": chunk}})
                    buffer_len += len(chunk)
                continue

            if seg.kind == "image":
                buffer.append({"type": "image", "data": {
                    "file": "base64://" + base64.b64encode(seg.data).decode()
                }})
                continue

            # 文件：混排缓冲区先发完，再单独上传
            await flush()
            try:
                ok = await self.s.send_file(group_id, seg.name, seg.data)
                if ok:
                    logger.info(f"[监听] 已发送文件：{seg.name}（{seg.label}）")
            except Exception as exc:
                logger.warning(f"[监听] 发送文件失败（{seg.name}）：{exc}")

        await flush()
        return sent

    def _link_history(self, cfg: ConfigState, outcome, message_ids: list) -> None:
        """把本次发出的消息 ID 补写到历史记录，供后续引用反查。"""
        if self.history is None:
            return
        history_id = getattr(outcome, "history_id", "")
        if not history_id or not message_ids:
            return
        try:
            records = self.history.load(cfg.name)
            for record in records:
                if record.id != history_id:
                    continue
                record.message_ids = [str(m) for m in message_ids if m]
                self.history.save(cfg.name)
                logger.info(
                    f"[历史] #{history_id} 已关联 {len(record.message_ids)} 条消息"
                )
                return
        except Exception as exc:
            logger.warning(f"[历史] 关联消息 ID 失败：{exc}")

    def _start_followup(self, cfg: ConfigState, group_id: str, outcome,
                        message_ids: Optional[list] = None) -> None:
        """分析成功后开启追问答疑会话（需引用这些消息才能追问）。"""
        if self.followup is None:
            return
        settings = cfg.settings
        if not settings.followup_enabled:
            return
        context = getattr(outcome, "followup_context", "")
        if not context or not outcome.response_text:
            return
        try:
            self.followup.start(
                group_id=group_id,
                config_name=cfg.name,
                file_name=outcome.file_name,
                system_context=context,
                report=outcome.response_text,
                message_ids=message_ids or [],
                history_id=getattr(outcome, "history_id", ""),
                digest=getattr(outcome, "digest", None),
            )
        except Exception as exc:
            logger.warning(f"[监听] 开启追问会话失败：{exc}")

    def _record_skip(self, cfg: ConfigState, group_id: str, file_name: str,
                     file_info: dict, file_size: Optional[int],
                     uploader: str, reason: str) -> None:
        """记录被跳过的任务，便于排查。"""
        import time

        job = self.s.add_job(
            cfg.name,
            config_name=cfg.name,
            group_id=str(group_id),
            file_name=file_name,
            file_id=self._pick_str(file_info, "id", "file_id", "fileId") or "",
            file_size=int(file_size or 0),
            uploader=str(uploader),
            started_at=int(time.time()),
            finished_at=int(time.time()),
            status="已跳过",
            detail=reason,
        )
        logger.info(f"[监听] 跳过 {file_name}（{reason}），记录 #{job.id}")
        self.s.trim_jobs(cfg.name)
        self.s.save_config(cfg.name)

    # ════════════════════════════════════════════════════════════
    # 工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _pick_str(data: Any, *keys: str) -> str:
        """按顺序取第一个非空字符串字段（兼容不同 OneBot 实现的字段名）。"""
        if not isinstance(data, dict):
            return ""
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _pick_int(data: Any, *keys: str) -> Optional[int]:
        """按顺序取第一个可转为 int 的字段。"""
        if not isinstance(data, dict):
            return None
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None
