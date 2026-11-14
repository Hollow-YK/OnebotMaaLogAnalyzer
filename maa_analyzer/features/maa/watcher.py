"""
日志包监听器 — 日志分析的主要触发入口。

只对群内上传的日志压缩包作出反应：
  - notice.group_upload   群文件上传通知（主要触发路径）
  - message.file          消息中的文件段（部分 OneBot 实现以此上报）

文本消息（/maa 指令、引用式追问）由 MessageHandler 处理。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from ...core.archive_names import ui_key_from_file_name
from ...core.log_context import config_log_context
from ...core.models import ConfigState, LogFileItem
from ...features.maa.analyzer import MaaAnalyzer
from ...features.maa.archive_profile import parse_archive_part
from ...features.maa.attachments import OutSegment
from ...features.maa.text_utils import format_bytes, split_text

if TYPE_CHECKING:
    from ...core.service import MaaService

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
        self._recent: dict[tuple[str, str], float] = {}
        # (群, 归档标识) → 开始时间：正在分析中的日志包
        self._active: dict[tuple[str, str], float] = {}
        # (群, 归档标识) → 正在等待分卷到齐的任务
        self._part_batches: dict[tuple[str, str], dict] = {}
        # 已提示过「报告已回退纯文本」的配置（每进程每配置只提示一次）
        self._render_noticed: set[str] = set()

    # ════════════════════════════════════════════════════════════
    # 去重
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _part_key(group_id: str, file_name: str, settings) -> Optional[tuple[str, str]]:
        """分卷包对应的归档键；非分卷或未开启合并时返回 None。"""
        part = parse_archive_part(file_name)
        if part is None or not settings.merge_archive_parts:
            return None
        return (str(group_id), f"part:{part.base.lower()}")

    def _active_key(self, group_id: str, file_name: str, settings) -> tuple[str, str]:
        """分析占用键：分卷按归档归并，其余按文件名。"""
        return self._part_key(group_id, file_name, settings) or (
            str(group_id), str(file_name)
        )

    def _is_duplicate(self, group_id: str, file_name: str,
                      file_size: Optional[int], settings) -> bool:
        """
        判断是否为同一文件的重复上报。

        同一文件可能同时经 notice.group_upload 与 message.file 上报，
        在 duplicate_window_seconds 内只处理一次。窗口 <= 0 时不去重。
        分析耗时可能超过窗口，结束时由 `_touch_recent` 重新计时。
        """
        window = int(getattr(settings, "duplicate_window_seconds", 0) or 0)
        if window <= 0:
            return False

        now = time.monotonic()
        # 键**不含** file_size：同一文件经两条入口上报时，大小字段可能一个有
        # 一个没有，带上它就会判不出重复、导致重复分析 / 反复刷新静默计时。
        key = (str(group_id), str(file_name))

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

    def _archive_cooling(self, key: tuple[str, str], settings) -> bool:
        """同一组归档是否刚分析完（冷却期内不再触发）。"""
        window = int(getattr(settings, "duplicate_window_seconds", 0) or 0)
        if window <= 0:
            return False
        last = self._recent.get((str(key[0]), str(key[1])))
        return last is not None and (time.monotonic() - last) < window

    def _touch_recent(self, group_id: str, file_name: str,
                      file_size: Optional[int], settings) -> None:
        """分析结束后重新计时，避免长分析期间窗口过期而重复分析。"""
        if int(getattr(settings, "duplicate_window_seconds", 0) or 0) <= 0:
            return
        now = time.monotonic()
        group = str(group_id)
        self._recent[(group, str(file_name))] = now
        # 分卷：同一归档再按 base 记一条。否则"刚分析完"的冷却键与记录键形状
        # 不一致（一个用 base、一个用文件名），冷却永远命不中 → 补传就重复分析。
        part = parse_archive_part(file_name)
        if part is not None:
            self._recent[(group, f"part:{part.base.lower()}")] = now

    # ════════════════════════════════════════════════════════════
    # 事件入口
    # ════════════════════════════════════════════════════════════

    async def on_group_upload(self, event: dict) -> bool:
        """notice.group_upload — 群文件上传通知。"""
        group_id = event.get("group_id")
        if group_id is None:
            return False

        raw_file_info = event.get("file")
        file_info = raw_file_info if isinstance(raw_file_info, dict) else event
        file_name = self._pick_str(
            file_info, "name", "file_name", "filename", "file"
        )
        if not file_name:
            return False

        return await self._handle(
            group_id=str(group_id),
            file_name=file_name,
            file_info=file_info,
            uploader=str(event.get("user_id", "")),
            source="notice.group_upload",
        )

    async def on_message_file(self, event: dict) -> bool:
        """message.file — 消息中的文件段（由 service 注入 _file_segments）。"""
        group_id = event.get("group_id")
        if group_id is None:
            return False

        uploader = str(event.get("user_id", ""))
        consumed = False
        for seg in event.get("_file_segments") or []:
            if not isinstance(seg, dict):
                continue
            file_name = self._pick_str(seg, "file_name", "name", "file", "filename")
            if not file_name:
                continue
            consumed = await self._handle(
                group_id=str(group_id),
                file_name=file_name,
                file_info=seg,
                uploader=uploader,
                source="message.file",
            ) or consumed
        return consumed

    # ════════════════════════════════════════════════════════════
    # 处理逻辑
    # ════════════════════════════════════════════════════════════

    async def _handle(self, *, group_id: str, file_name: str,
                      file_info: dict, uploader: str,
                      source: str = "") -> bool:
        """判断是否为目标日志包，是则执行分析。"""
        cfg = self.s.resolve_config(group_id)
        if cfg is None:
            # 群未被监听，或同时属于多个配置（无法确定使用哪套设置）
            return False
        # 整段处理（下载 / 分析 / 发送）的日志都标注配置名
        with config_log_context(cfg.name):
            return await self._handle_for_config(
                cfg, group_id=group_id, file_name=file_name,
                file_info=file_info, uploader=uploader, source=source,
            )

    async def _handle_for_config(self, cfg: ConfigState, *, group_id: str,
                                 file_name: str, file_info: dict,
                                 uploader: str, source: str = "") -> bool:
        settings = cfg.settings
        if not settings.enabled:
            return False
        if not cfg.project.matches(file_name):
            return False

        # 文件名能确定 UI 且该 UI 已被关闭时，连下载都省掉
        ui_key = ui_key_from_file_name(file_name)
        if ui_key and not cfg.project.ui_enabled(ui_key):
            logger.info(f"[监听] 跳过 {file_name}：UI {ui_key} 已在 project.ui 中禁用")
            return False

        file_size = self._pick_int(file_info, "size", "file_size", "fileSize")

        # 同一归档正在分析时，后续到达的分卷 / 重复事件直接忽略，
        # 避免重复下载与重复报告（分析可能比去重窗口更久）。
        active_key = self._active_key(group_id, file_name, settings)
        if active_key in self._active:
            logger.info(
                f"[监听] 跳过进行中的重复触发：{file_name}"
                f"（群 {group_id}，来源 {source or '未知'}）"
            )
            return True

        # 分卷包：先收集，等到一段时间没有新分卷再统一分析
        part_key = self._part_key(group_id, file_name, settings)
        if part_key is not None:
            self._queue_part(
                cfg, group_id, part_key, file_name, file_info,
                file_size, uploader, source, settings,
            )
            return True

        # 非分卷：同一上传可能经多条路径到达（如 notice.group_upload 与
        # message.file 同时上报），在去重窗口内只处理一次。
        if self._is_duplicate(group_id, file_name, file_size, settings):
            logger.info(
                f"[监听] 跳过重复触发：{file_name}"
                f"（群 {group_id}，来源 {source or '未知'}，"
                f"{settings.duplicate_window_seconds}s 内已处理过）"
            )
            return True

        if file_size and file_size > settings.max_zip_bytes():
            await self.s.send_text(
                int(group_id),
                f"检测到日志包 {file_name}，但文件大小 {format_bytes(file_size)} "
                f"超过限制 {format_bytes(settings.max_zip_bytes())}，已跳过。",
            )
            self._record_skip(cfg, group_id, file_name, file_info, file_size, uploader,
                              "文件超过大小限制")
            return True

        file_id = self._pick_str(file_info, "id", "file_id", "fileId") or None
        file_url = self._pick_str(
            file_info, "url", "download_url", "downloadUrl"
        )

        logger.info(
            f"[监听] 触发分析：{file_name}（群 {group_id}，来源 {source or '未知'}）"
        )
        # 先登记再发进度，避免并发事件在 await 期间重复进入
        self._active[active_key] = time.monotonic()
        try:
            if settings.send_progress_message:
                logger.info(f"[监听] 开始发送分析进度消息：{file_name}")
                await self.s.send_text(
                    int(group_id),
                    f"检测到日志包：{file_name}\n正在下载并分析，请稍等。",
                )
                logger.info(f"[监听] 分析进度消息发送完成：{file_name}")
            await self._analyze(
                cfg, group_id, uploader,
                LogFileItem(
                    file_id=file_id or "",
                    file_name=file_name,
                    size=int(file_size or 0),
                    url=file_url,
                ),
            )
        finally:
            self._active.pop(active_key, None)
            # 分析可能超过去重窗口，结束时重新计时
            self._touch_recent(group_id, file_name, file_size, settings)
        return True

    # ════════════════════════════════════════════════════════════
    # 分卷：等待到齐再分析
    # ════════════════════════════════════════════════════════════

    def cancel_pending(self) -> None:
        """取消尚未触发的分卷计时（停止 / 重载运行时调用）。"""
        for batch in self._part_batches.values():
            task = batch.get("task")
            if task is not None and not task.done():
                task.cancel()
        self._part_batches.clear()

    def _queue_part(self, cfg: ConfigState, group_id: str,
                    key: tuple[str, str], file_name: str, file_info: dict,
                    file_size: Optional[int], uploader: str, source: str,
                    settings) -> None:
        """登记分卷并重置等待计时；静默一段时间后才触发分析。"""
        if self._archive_cooling(key, settings):
            logger.info(
                f"[监听] 跳过重复触发：{file_name}"
                f"（群 {group_id}，来源 {source or '未知'}，"
                f"{settings.duplicate_window_seconds}s 内已分析过该归档）"
            )
            return

        # 同一分卷经两条入口重复上报：不重启计时
        if self._is_duplicate(group_id, file_name, file_size, settings):
            logger.info(
                f"[监听] 跳过重复触发：{file_name}"
                f"（群 {group_id}，来源 {source or '未知'}，"
                f"{settings.duplicate_window_seconds}s 内已处理过）"
            )
            return

        batch = self._part_batches.get(key)
        if batch is None:
            batch = {"parts": [], "numbers": set(), "task": None}
            self._part_batches[key] = batch
        if file_name not in batch["parts"]:
            batch["parts"].append(file_name)
        parsed = parse_archive_part(file_name)
        if parsed is not None:
            batch.setdefault("numbers", set()).add(int(parsed.number))
        batch.update({
            "file_name": file_name,
            "file_info": file_info,
            "file_size": file_size,
            "uploader": uploader,
        })

        previous = batch.get("task")
        if previous is not None and not previous.done():
            previous.cancel()

        settle = max(0.0, float(settings.archive_part_settle_seconds or 0))
        batch["task"] = asyncio.create_task(
            self._run_part_batch(cfg, group_id, key, settle, settings)
        )
        logger.info(
            f"[监听] 分卷已登记：{file_name}（群 {group_id}，"
            f"已收集 {len(batch['parts'])} 个，来源 {source or '未知'}），"
            f"等待 {settle:g}s 无新分卷后分析"
        )

    async def _run_part_batch(self, cfg: ConfigState, group_id: str,
                              key: tuple[str, str], settle: float,
                              settings) -> None:
        """等待静默期结束，然后按合并分卷的方式分析一次。

        齐全性 = 「编号连续 + 内容特征」（见 `judge_parts_state`）：
        静默期到期但编号像是还没到齐（断档，或只收到孤零零的 part01）时，
        最多额外等 `archive_part_wait_extra_rounds` 轮；分析侧发现内容缺日志
        正文时同样再多等一轮，而不是静默丢卷或立刻出残报告。
        """
        try:
            if settle > 0:
                await asyncio.sleep(settle)
        except asyncio.CancelledError:
            return

        batch = self._part_batches.pop(key, None)
        if batch is None or key in self._active:
            return

        extra_rounds = max(
            0, int(getattr(settings, "archive_part_wait_extra_rounds", 0) or 0)
        )
        waits = int(batch.get("extra_waits", 0) or 0)
        require_content = bool(
            getattr(settings, "archive_part_require_content", True)
        )
        numbers = sorted(int(n) for n in (batch.get("numbers") or ()))
        contiguous = bool(numbers) and numbers == list(range(1, len(numbers) + 1))
        # 孤零零的 part01：编号连续，但样本显示主日志常落在后面的卷里，
        # 所以要求内容时保守地当作"还没到齐"。
        suspicious = (not contiguous) or (require_content and numbers == [1])
        if suspicious and waits < extra_rounds:
            batch["extra_waits"] = waits + 1
            batch["task"] = asyncio.create_task(
                self._run_part_batch(cfg, group_id, key, max(1.0, settle), settings)
            )
            self._part_batches[key] = batch
            logger.info(
                f"[监听] 分卷看起来还没到齐（已收 {len(numbers)} 卷），"
                f"额外等待 {max(1.0, settle):g}s 后再看：{batch['file_name']}"
            )
            return

        file_name = batch["file_name"]
        file_info = batch["file_info"]
        file_size = batch["file_size"]
        uploader = batch["uploader"]
        logger.info(
            f"[监听] 分卷已收齐（{len(batch['parts'])} 个），开始分析：{file_name}"
        )

        self._active[key] = time.monotonic()
        deferred = False
        try:
            if settings.send_progress_message:
                logger.info(f"[监听] 开始发送分析进度消息：{file_name}")
                await self.s.send_text(
                    int(group_id),
                    f"检测到日志包：{file_name}\n正在下载并分析，请稍等。",
                )
                logger.info(f"[监听] 分析进度消息发送完成：{file_name}")
            outcome = await self._analyze(
                cfg, group_id, uploader,
                LogFileItem(
                    file_id=self._pick_str(
                        file_info, "id", "file_id", "fileId"
                    ) or "",
                    file_name=file_name,
                    size=int(file_size or 0),
                    url=self._pick_str(
                        file_info, "url", "download_url", "downloadUrl"
                    ),
                ),
                defer_incomplete_parts=waits < extra_rounds,
            )
            if outcome is not None and getattr(outcome, "parts_incomplete", False):
                if waits < extra_rounds and not getattr(outcome, "skipped", False):
                    # 内容不完整（如只收到图片卷）：额外等一轮再试。
                    # 这里**不**记冷却，否则随后补上的卷会被冷却拦住。
                    deferred = True
                    batch["extra_waits"] = waits + 1
                    batch["task"] = asyncio.create_task(
                        self._run_part_batch(
                            cfg, group_id, key, max(1.0, settle), settings
                        )
                    )
                    self._part_batches[key] = batch
                    logger.info(
                        "[监听] 分卷内容不完整，额外等待 "
                        f"{max(1.0, settle):g}s 后再试：{file_name}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 后台任务没有事件分发器兜底，这里自行记录
            logger.exception(f"[监听] 分卷分析异常：{file_name}")
        finally:
            self._active.pop(key, None)
            if not deferred:
                self._touch_recent(group_id, file_name, file_size, settings)

    async def _analyze(self, cfg: ConfigState, group_id: str,
                       uploader: str, file_info: LogFileItem,
                       *, defer_incomplete_parts: bool = False,
                       ) -> Optional[Any]:
        """执行分析并发送全部回复（含错误提示）。返回 outcome 供分卷流程判断。"""
        settings = cfg.settings
        group_int = int(group_id)

        if not file_info.file_id and not file_info.url:
            await self.s.send_text(
                group_int,
                f"日志包 {file_info.file_name} 缺少下载链接和 file_id，"
                "无法下载分析。",
            )
            return None

        async def progress(text: str):
            if settings.send_progress_message:
                await self.s.send_text(group_int, text)

        logger.info(f"[监听] 开始调用分析器：{file_info.file_name}")
        outcome = await self.analyzer.analyze(
            config_name=cfg.name,
            settings=settings,
            project=cfg.project,
            group_id=group_int,
            file_name=file_info.file_name,
            file_id=file_info.file_id,
            file_url=file_info.url,
            file_size=file_info.size or None,
            uploader=uploader,
            progress=progress,
            defer_incomplete_parts=defer_incomplete_parts,
        )

        if defer_incomplete_parts and getattr(outcome, "parts_incomplete", False):
            # 分卷内容不完整：这次不回复用户，交给 _run_part_batch 额外等待后再试
            return outcome

        if not settings.send_summary:
            return outcome

        max_chars = max(1000, int(settings.reply_chunk_chars or 3500))
        # 按片段顺序发送（文本 / 图片 / 文件），收集 message_id 供引用追问
        sent_ids = await self._send_outcome(group_int, outcome, settings, max_chars)
        await self._notify_render_fallback(cfg, group_int, outcome)

        # 把消息 ID 补写到历史归档（使引用可跨天定位）
        await self._link_history(cfg, outcome, sent_ids)

        # 开启追问答疑会话（以本次分析发出的消息为引用锚点）
        await self._start_followup(cfg, group_id, outcome, sent_ids)

        # 报告发给通知群（若与监听群不同）
        notify = cfg.info.notify_group
        if notify and notify != group_id:
            await self._send_outcome(
                int(notify), outcome, settings, max_chars, link_history=False
            )
        return outcome

    async def _notify_render_fallback(self, cfg: ConfigState, group_id: int,
                                      outcome) -> None:
        """浏览器缺失 / 渲染失败时，向通知群提示一次（每配置每进程一次）。"""
        notice = str(getattr(outcome, "render_notice", "") or "")
        if not notice or cfg.name in self._render_noticed:
            return
        self._render_noticed.add(cfg.name)
        target = cfg.info.notify_group or str(group_id)
        try:
            await self.s.send_text(int(target), notice)
        except Exception as exc:
            logger.warning(f"[监听] 发送渲染降级提示失败：{exc}")

    async def _send_outcome(self, group_id: int, outcome, settings,
                            max_chars: int, *, link_history: bool = True) -> list:
        """
        按报告片段顺序发送内容，返回成功发出的 message_id 列表。

        图文保持模型给出的位置；转发模式下文件附件在卡片后单独上传。
        """
        segments = None
        if hasattr(outcome, "output_segments"):
            segments = outcome.output_segments()
        else:
            segments = getattr(outcome, "segments", None)

        if not segments:
            # 纯文本兜底同样遵守合并转发设置。
            segments = [OutSegment("text", text=chunk)
                        for chunk in outcome.report_chunks(max_chars)]

        return await self.send_segments(
            group_id, segments, settings, max_chars,
            forward=settings.forward_analysis_results,
        )

    async def send_segments(self, group_id: int, segments: list,
                            settings, max_chars: int, *, forward: bool = False) -> list:
        """
        按片段顺序组合相邻图文；主报告可选择包进一张 QQ 合并转发卡片。

        这样模型把 [附件@日志: ...] 写在两段文字之间时，图片会真正出现在
        两段文字中间，而不是被拆成三条独立消息。
        追问使用默认的普通消息发送。文件附件单独作为群文件上传；
        合并转发模式下在整张卡片发出后上传。
        """
        sent: list = []
        messages: list[list[dict]] = []
        files: list = []
        # 待合并的图文混排缓冲区（元素为消息段 dict）
        buffer: list[dict] = []
        buffer_len = 0

        async def flush() -> None:
            nonlocal buffer, buffer_len
            if not buffer:
                return
            if forward:
                messages.append(list(buffer))
            else:
                mid = await self.s.send_message(group_id, list(buffer))
                if mid:
                    sent.append(mid)
            buffer = []
            buffer_len = 0

        async def send_file(seg: OutSegment) -> None:
            try:
                ok = await self.s.send_file(group_id, seg.name, seg.data)
                if ok:
                    logger.info(f"[监听] 已发送文件：{seg.name}（{seg.label}）")
            except Exception as exc:
                logger.warning(f"[监听] 发送文件失败（{seg.name}）：{exc}")

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
                if not seg.data:
                    # 空数据会变成 base64:// 空图片，不能发出去
                    logger.warning(f"[监听] 图片片段内容为空，已跳过：{seg.label}")
                    continue
                buffer.append({"type": "image", "data": {
                    "file": "base64://" + base64.b64encode(seg.data).decode()
                }})
                continue

            # 文件：混排缓冲区先发完，再单独上传
            if not seg.data:
                logger.warning(f"[监听] 文件片段内容为空，已跳过：{seg.label}")
                continue
            await flush()
            if forward:
                files.append(seg)
                continue
            await send_file(seg)

        await flush()
        if forward and messages:
            mid = await self.s.send_forward_messages(group_id, messages)
            if mid:
                sent.append(mid)
        for seg in files:
            await send_file(seg)
        return sent

    async def _link_history(self, cfg: ConfigState, outcome, message_ids: list) -> None:
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
                await self.history.save_async(cfg.name)
                logger.info(
                    f"[历史] #{history_id} 已关联 {len(record.message_ids)} 条消息"
                )
                return
        except Exception as exc:
            logger.warning(f"[历史] 关联消息 ID 失败：{exc}")

    async def _start_followup(self, cfg: ConfigState, group_id: str, outcome,
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
        # 策略属于当前配置：会话刚建立就按它自己的窗口 / 轮数 / 会话数计算
        try:
            self.followup.apply_policy(cfg.name, settings)
        except Exception as exc:
            logger.warning(f"[监听] 下发追问策略失败：{exc}")
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
                session_id=getattr(outcome, "session_id", ""),
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
        self.s.schedule_save_config(cfg.name)

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
