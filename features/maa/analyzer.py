"""
日志分析核心 — 下载日志包、提取摘要、调用 LLM、格式化报告。

不依赖任何 Bot 框架，通过 MaaService 获取配置与 Bot 能力。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.llm import LLMClient
from core.models import AnalysisSettings
from features.maa.log_digest import (
    IMAGE_SUFFIXES,
    DigestOptions,
    DigestResult,
    build_log_digest,
)
from features.maa.log_tools import LogArchiveTools
from features.maa.onebot_files import download_group_file
from features.maa.prompts import (
    AGENT_TOOL_HINT,
    AGENT_TOOL_HINT_FOLLOWUP,
    AGENT_TOOL_HINT_LOG_ONLY,
    AGENT_TOOL_HINT_REPO_ONLY,
    build_system_prompt,
)
from features.maa.repo import RepoProvider, extract_identifiers
from features.maa.repo_tools import LOG_TOOL_DEFINITIONS, TOOL_DEFINITIONS, RepoTools
from features.maa.attachments import (
    OutSegment,
    build_report_segments,
    find_repo_attachment_names,
    split_report,
    strip_attachments,
)
from features.maa.text_utils import format_bytes, split_text

if TYPE_CHECKING:
    from core.service import MaaService

logger = logging.getLogger("Maa.Analyze")

# AI 在报告末尾请求核对其他版本：单独一行 [需要核对版本: v1.0.0]
_VERSION_REQUEST = re.compile(r"\[需要核对版本[:：]\s*([^\]\n]+)\]")


def _extract_version_request(text: str) -> str:
    """从 AI 输出中解析请求核对的版本号，未请求时返回空字符串。"""
    if not text:
        return ""
    matches = _VERSION_REQUEST.findall(text)
    return matches[-1].strip() if matches else ""


def _strip_version_request(text: Optional[str]) -> Optional[str]:
    """移除给用户看的报告中的内部版本核对指令，并清理多余空行。"""
    if not text:
        return text
    cleaned = _VERSION_REQUEST.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# 模型在工具循环里常见的英文过渡语（应只出现在 content 开头，不应发给用户）
_PREAMBLE_PATTERNS = [
    re.compile(r"^\s*I have enough (?:evidence|information)[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^\s*(?:Now )?I (?:have|can) (?:enough|sufficient)[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^\s*Let me (?:summarize|conclude|give)[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^\s*Based on (?:the )?(?:evidence|above)[^\n]*\n+", re.IGNORECASE),
    re.compile(r"^\s*好的[，,]?\s*(?:我(?:已经)?(?:有|收集到)?足够的?(?:证据|信息)[^\n]*)\n+"),
]


def _strip_model_preamble(text: Optional[str]) -> Optional[str]:
    """
    去掉模型在报告正文前的英文/口语化过渡语。

    只处理开头，且必须后面还存在正式内容（含「结论」等小节）才裁剪，
    避免把正文本身误删。
    """
    if not text:
        return text
    cleaned = text.strip()
    for _ in range(3):  # 可能连续多句
        before = cleaned
        for pattern in _PREAMBLE_PATTERNS:
            candidate = pattern.sub("", cleaned, count=1)
            # 裁剪后必须仍保留报告主体，否则放弃
            if candidate != cleaned and ("结论" in candidate or "关键证据" in candidate):
                cleaned = candidate.lstrip()
                break
        if cleaned == before:
            break
    return cleaned.strip() or text


def _append_text_segment(segments: list[OutSegment],
                         notes: list[str]) -> list[OutSegment]:
    """
    把备注追加为末尾文本片段（与 response_text 保持一致）。

    已有末尾文本片段时直接拼接，避免产生过多碎片。
    """
    if not notes:
        return segments
    addition = "\n".join(f"（{note}）" for note in notes)
    if segments and segments[-1].kind == "text":
        segments = list(segments)
        segments[-1] = OutSegment(
            kind="text",
            text=f"{segments[-1].text.rstrip()}\n\n{addition}",
        )
        return segments
    return list(segments) + [OutSegment(kind="text", text=addition)]


def build_digest_options(settings: AnalysisSettings) -> DigestOptions:
    """把分析设置转换为摘要提取选项。"""
    return DigestOptions(
        max_prompt_chars=max(5000, int(settings.max_prompt_chars or 600000)),
        max_zip_members=max(20, int(settings.max_zip_members or 300)),
        max_total_uncompressed_bytes=max(10, int(settings.max_total_uncompressed_mb or 120)) * 1024 * 1024,
        max_file_read_bytes=max(64, int(settings.max_log_file_read_kb or 512)) * 1024,
        small_log_full_read_bytes=max(1, int(settings.small_log_full_read_mb or 5)) * 1024 * 1024,
        stream_medium_logs=bool(settings.stream_medium_logs),
        max_large_log_read_bytes=max(32, int(settings.max_large_log_read_kb or 256)) * 1024,
        large_log_threshold_bytes=max(1, int(settings.large_log_threshold_mb or 50)) * 1024 * 1024,
        stream_large_logs=bool(settings.stream_large_logs),
        max_stream_log_bytes=max(0, int(settings.max_stream_log_mb or 0)) * 1024 * 1024,
        max_log_files=max(1, int(settings.max_log_files or 6)),
        max_maafw_bak_files=max(0, int(settings.max_maafw_bak_files or 0)),
        log_head_lines=max(0, int(settings.log_head_lines or 0)),
        context_before_lines=max(0, int(settings.context_before_lines or 0)),
        context_after_lines=max(0, int(settings.context_after_lines or 0)),
        max_sections_per_log=max(1, int(settings.max_sections_per_log or 60)),
        max_log_chars=max(2000, int(settings.max_log_chars or 120000)),
        compress_context_noise=bool(settings.compress_context_noise),
        max_recognition_evidence_lines=max(0, int(settings.max_recognition_evidence_lines or 0)),
        max_recognition_evidence_chars=max(0, int(settings.max_recognition_evidence_chars or 0)),
    )


@dataclass
class RepoPrep:
    """项目代码准备结果。"""

    provider: Optional[RepoProvider] = None
    context: str = ""        # inject 模式下的预检索片段


@dataclass
class FollowupAnswer:
    """一次追问的回答（含已解析的附件片段）。"""

    text: str = ""
    segments: list[OutSegment] = field(default_factory=list)


@dataclass
class AnalysisOutcome:
    """一次分析的结构化结果，供上层格式化输出。"""

    ok: bool
    file_name: str
    downloaded: int = 0
    log_count: int = 0
    image_count: int = 0
    response_text: str = ""
    error: str = ""
    digest: Optional[DigestResult] = None
    notes: list[str] = field(default_factory=list)
    # 报告的有序输出片段（文本 / 图片 / 文件，保持模型给出的位置）
    segments: list[OutSegment] = field(default_factory=list)
    # 追问上下文（摘要 + 代码参考），供分析后答疑使用
    followup_context: str = ""
    # 历史记录 ID（用于把消息 ID 补充到归档记录）
    history_id: str = ""
    # 日志包归档后的路径（供指令/调试使用）
    archived_zip: str = ""
    # 项目名（从 file_prefix 推导，用于报告头）
    project_name: str = "MaaXXX"

    def header_text(self) -> str:
        """报告头（项目名 + 文件信息）。"""
        project = self.project_name or "MaaXXX"
        if not self.ok:
            return f"{project} 日志分析失败：{self.error}" if self.error else ""
        return (
            f"{project} 日志分析结果\n"
            f"文件：{self.file_name}\n"
            f"大小：{format_bytes(self.downloaded)}\n"
            f"日志文件：{self.log_count} 个\n"
            f"错误截图：{self.image_count} 张\n\n"
        )

    def report_chunks(self, max_chars: int = 3500) -> list[str]:
        """格式化最终报告并分段（适配 QQ 消息长度限制）。"""
        if not self.ok:
            return [self.header_text()] if self.error else []
        return split_text(self.header_text() + self.response_text, max_chars)

    def output_segments(self) -> list[OutSegment]:
        """
        返回带报告头的有序输出片段。

        片段模型下 header 不会自动包含，这里显式拼到首个文本片段前，
        保证「项目名 + 文件信息」始终出现在报告开头。
        """
        header = self.header_text()
        if not self.segments:
            text = header + self.response_text
            return [OutSegment(kind="text", text=text)] if text else []

        out: list[OutSegment] = []
        prepended = False
        for seg in self.segments:
            if not prepended and seg.kind == "text":
                out.append(OutSegment(kind="text", text=header + seg.text))
                prepended = True
            else:
                out.append(seg)
        if not prepended:
            # 报告以附件开头，则把 header 作为独立文本片段放最前
            out.insert(0, OutSegment(kind="text", text=header))
        return out


class MaaAnalyzer:
    """
    日志包分析器 — 串起下载 → 摘要 → LLM 三段流程。

    并发控制:
      - download_semaphore: 限制同时下载数
      - analysis_semaphore: 限制同时调用 LLM 数
    """
    def __init__(self, service: "MaaService", llm: LLMClient, history=None):
        self.s = service
        self.llm = llm
        # 历史存储：归档日志包与追问上下文（None 表示不启用）
        self.history = history
        # config_name → (download_n, analysis_n, download_sem, analysis_sem)
        self._semaphores: dict[str, tuple[int, int, asyncio.Semaphore, asyncio.Semaphore]] = {}
        # config_name → RepoProvider（复用克隆结果与索引）
        self._repos: dict[str, RepoProvider] = {}
        self.active_jobs: set[str] = set()

    # ════════════════════════════════════════════════════════════
    # 并发控制
    # ════════════════════════════════════════════════════════════

    def _semaphore_pair(self, config_name: str, settings: AnalysisSettings):
        """按配置维护下载/分析信号量，设置变更时自动重建。"""
        download_n = max(1, int(settings.download_concurrency or 1))
        analysis_n = max(1, int(settings.analysis_concurrency or 1))
        cached = self._semaphores.get(config_name)
        if cached and cached[0] == download_n and cached[1] == analysis_n:
            return cached[2], cached[3]
        download_sem = asyncio.Semaphore(download_n)
        analysis_sem = asyncio.Semaphore(analysis_n)
        self._semaphores[config_name] = (download_n, analysis_n, download_sem, analysis_sem)
        return download_sem, analysis_sem

    # ════════════════════════════════════════════════════════════
    # 主流程
    # ════════════════════════════════════════════════════════════

    async def analyze(
        self,
        *,
        config_name: str,
        settings: AnalysisSettings,
        group_id: int,
        file_name: str,
        file_id: Optional[str],
        file_size: Optional[int],
        uploader: str = "",
        progress: Optional[callable] = None,
    ) -> AnalysisOutcome:
        """
        下载并分析一个日志包。

        progress: 可选异步回调 async (text: str) -> None，用于发送进度消息。
        """
        job_key = f"{group_id}:{file_id or file_name}:{file_size or 0}"
        if job_key in self.active_jobs:
            return AnalysisOutcome(ok=False, file_name=file_name, error="这个日志包正在分析中，请稍等。")

        self.active_jobs.add(job_key)
        work_dir = self._build_work_dir(config_name, group_id, file_name, file_id)
        zip_path = work_dir / "source.zip"
        job = None

        try:
            job = self.s.add_job(
                config_name,
                config_name=config_name,
                group_id=str(group_id),
                file_name=file_name,
                file_id=str(file_id or ""),
                file_size=int(file_size or 0),
                uploader=str(uploader),
                started_at=int(time.time()),
                status="执行中",
            )

            resolved_id = file_id
            if not resolved_id:
                await asyncio.sleep(max(0.0, float(settings.file_list_delay_seconds or 0)))
                resolved_id = await self._resolve_file_id(group_id, file_name, file_size, settings)
                if not resolved_id:
                    self._finish_job(config_name, job, "执行失败", "无法解析 file_id")
                    return AnalysisOutcome(
                        ok=False, file_name=file_name,
                        error="无法获取该群文件的 file_id，暂时不能下载分析。",
                    )

            download_sem, analysis_sem = self._semaphore_pair(config_name, settings)

            downloaded = await download_group_file(
                self.s,
                group_id,
                resolved_id,
                zip_path,
                max_bytes=settings.max_zip_bytes(),
                semaphore=download_sem,
                timeout_seconds=max(10, int(settings.download_timeout_seconds or 120)),
            )
            logger.info(f"[分析] 已下载 {file_name}，大小 {format_bytes(downloaded)}")
            if progress:
                await progress("日志包已下载，正在提取错误摘要。")

            digest = await self._build_digest(zip_path, file_name, group_id, settings)
            logger.info(
                f"[分析] 摘要完成：logs={len(digest.log_files)}, "
                f"configs={len(digest.config_files)}, images={len(digest.error_images)}"
            )
            debug_path = self._save_digest_debug_if_enabled(config_name, settings, file_name, group_id, digest.prompt)
            if debug_path:
                logger.info(f"[分析] 已保存摘要调试文件：{debug_path}")

            if progress:
                await progress(
                    f"摘要完成，提取到 {len(digest.log_files)} 个日志文件、"
                    f"{len(digest.error_images)} 张错误截图，正在调用 AI。"
                )

            prompt = digest.prompt
            repo_prep = await self._prepare_repo(config_name, settings, prompt, file_name=file_name)
            if repo_prep.context:
                prompt = f"{prompt}\n\n{repo_prep.context}"
                logger.info(f"[分析] 已注入项目代码参考（{len(repo_prep.context)} 字符）")

            async with analysis_sem:
                # 需要工具时（agent 模式，或 inject 模式下日志包仍可用）
                use_tools = settings.repo.use_agent() or bool(settings.log_tools_enabled)
                if use_tools and (repo_prep.provider is not None or zip_path.exists()):
                    response_text = await asyncio.wait_for(
                        self._run_repo_agent(
                            settings, prompt, repo_prep.provider,
                            LogArchiveTools(
                                str(zip_path),
                                max_file_kb=max(64, int(settings.max_log_file_read_kb or 512)),
                                max_result_chars=max(
                                    2000, int(settings.repo.max_tool_result_chars or 30000)
                                ),
                            ) if settings.log_tools_enabled else None,
                        ),
                        timeout=max(60, int(settings.llm_timeout_seconds or 900)),
                    )
                else:
                    response_text = await asyncio.wait_for(
                        self._call_ai(settings, prompt),
                        timeout=max(30, int(settings.llm_timeout_seconds or 900)),
                    )
                    # AI 可请求切换到其他 tag 复核（受 max_tag_switch_rounds 限制）
                    response_text = await self._maybe_recheck_with_other_tag(
                        config_name, settings, prompt, response_text, repo_prep.context
                    )
            # 内部指令与模型过渡语不应出现在给用户的报告里
            response_text = _strip_version_request(response_text)
            response_text = _strip_model_preamble(response_text)

            # 附件：模型可在报告任意位置声明 [附图: ...] / [附件: ...]
            # 这里解析为有序片段，从而支持「文字 → 图 → 文字」的排版
            segments, attach_notes = await self.build_report_segments(
                settings, response_text, digest, repo_prep.provider
            )
            response_text = strip_attachments(response_text) or ""
            if attach_notes:
                response_text = f"{response_text}\n\n" + "\n".join(
                    f"（{note}）" for note in attach_notes
                )
                # 备注追加在末尾，同步反映到片段序列
                segments = _append_text_segment(segments, attach_notes)

            if not response_text:
                self._finish_job(config_name, job, "执行失败", "AI 未返回有效内容")
                return AnalysisOutcome(
                    ok=False, file_name=file_name,
                    error="AI 分析没有返回有效内容，请稍后重试。",
                )

            job.status = "已执行"
            job.log_count = len(digest.log_files)
            job.image_count = len(digest.error_images)
            self._finish_job(config_name, job, "已执行", "")

            followup_context = self._build_followup_context(
                file_name, prompt, repo_prep.context, response_text
            )
            # 归档日志包与上下文（使追问可跨天）。必须在 finally 清理前完成。
            history_id, archived_zip = self._archive_history(
                config_name=config_name,
                settings=settings,
                group_id=group_id,
                file_name=file_name,
                file_id=file_id or "",
                uploader=uploader,
                zip_path=zip_path,
                context=followup_context,
                report=response_text,
                log_count=len(digest.log_files),
                image_count=len(digest.error_images),
            )
            # 归档后 zip 已移出工作目录（工作目录稍后会被清理），
            # 把 digest 的路径指向归档副本，使追问仍能从日志包取附件。
            if archived_zip:
                digest.zip_path = archived_zip

            return AnalysisOutcome(
                ok=True,
                file_name=file_name,
                downloaded=downloaded,
                log_count=len(digest.log_files),
                image_count=len(digest.error_images),
                response_text=response_text,
                digest=digest,
                notes=list(digest.notes),
                segments=segments,
                followup_context=followup_context,
                history_id=history_id,
                archived_zip=archived_zip,
                project_name=settings.project_name(),
            )

        except asyncio.TimeoutError:
            logger.error(f"[分析] 超时：{file_name}", exc_info=True)
            if job is not None:
                self._finish_job(config_name, job, "执行失败", "超时")
            return AnalysisOutcome(
                ok=False, file_name=file_name,
                error=(
                    "分析超时。可以尝试调大 digest_timeout_seconds 或 llm_timeout_seconds。"
                ),
            )
        except Exception as exc:
            logger.error(f"[分析] 失败：{exc}", exc_info=True)
            if job is not None:
                self._finish_job(config_name, job, "执行失败", str(exc)[:300])
            return AnalysisOutcome(ok=False, file_name=file_name, error=str(exc))
        finally:
            self.active_jobs.discard(job_key)
            # 无论成功失败，都把仓库切回最新版本，避免影响下一次分析
            await self._restore_repo_latest(config_name, settings)
            if bool(settings.cleanup_after_done):
                self._cleanup_work_dir(work_dir)

    async def _restore_repo_latest(self, config_name: str,
                                   settings: AnalysisSettings) -> None:
        """分析结束后把仓库还原到分析前的位置（失败不影响分析）。"""
        provider = self._repos.get(config_name)
        if provider is None:
            return
        try:
            if await provider.restore_origin():
                logger.info("[分析] 项目代码已还原到分析前的位置")
        except Exception as exc:
            logger.warning(f"[分析] 还原仓库位置失败：{exc}")

    def _finish_job(self, config_name: str, job, status: str, detail: str) -> None:
        """更新任务记录终态并持久化。"""
        job.status = status
        job.detail = detail
        job.finished_at = int(time.time())
        self.s.trim_jobs(config_name)
        self.s.save_config(config_name)

    # ════════════════════════════════════════════════════════════
    # 子步骤
    # ════════════════════════════════════════════════════════════

    async def _resolve_file_id(self, group_id: int, file_name: str,
                               file_size: Optional[int],
                               settings: AnalysisSettings) -> Optional[str]:
        from features.maa.onebot_files import find_group_file_by_name

        found = await find_group_file_by_name(
            self.s, group_id, file_name, file_size=file_size
        )
        return found.file_id if found else None

    async def _build_digest(self, zip_path: Path, file_name: str,
                            group_id: int, settings: AnalysisSettings) -> DigestResult:
        timeout = int(settings.digest_timeout_seconds or 0)
        logger.info(f"[分析] 开始提取日志摘要：{file_name}")
        task = asyncio.to_thread(
            build_log_digest,
            str(zip_path),
            original_file_name=file_name,
            group_id=str(group_id),
            project_name=settings.project_name(),
            options=build_digest_options(settings),
        )
        if timeout > 0:
            return await asyncio.wait_for(task, timeout=max(10, timeout))
        return await task

    async def _call_ai(self, settings: AnalysisSettings, prompt: str) -> Optional[str]:
        system_prompt = self._system_prompt(settings)
        temperature = settings.temperature
        if temperature is None:
            temperature = self.llm.temperature
        return await self.llm.chat(
            prompt,
            system_prompt=system_prompt,
            temperature=float(temperature),
            model_override=str(settings.model or "").strip(),
        )

    @staticmethod
    def _system_prompt(settings: AnalysisSettings) -> str:
        """系统提示词：配置自定义优先，否则按项目名生成默认人设。"""
        custom = str(settings.system_prompt or "").strip()
        return custom or build_system_prompt(settings.project_name())

    # ════════════════════════════════════════════════════════════
    # 分析历史
    # ════════════════════════════════════════════════════════════

    def _archive_history(self, *, config_name: str, settings: AnalysisSettings,
                         group_id: int, file_name: str, file_id: str,
                         uploader: str, zip_path: Path, context: str,
                         report: str, log_count: int,
                         image_count: int) -> tuple[str, str]:
        """
        归档日志包与追问上下文到历史目录。

        返回 (history_id, 归档后的 zip 路径)；未启用或失败时返回 ("", "")。
        归档成功后工作目录中的 source.zip 会被移走，因此 cleanup 不会误删。
        """
        if self.history is None or not settings.history_enabled:
            return "", ""
        try:
            record = self.history.add(
                config_name=config_name,
                group_id=str(group_id),
                file_name=file_name,
                source_zip=zip_path,
                context=context,
                report=report,
                message_ids=[],   # 消息 ID 稍后由 watcher 补充
                file_id=file_id,
                uploader=uploader,
                log_count=log_count,
                image_count=image_count,
            )
            if record is None:
                return "", ""
            archived = self.history.read_zip(config_name, record)
            return record.id, str(archived) if archived else ""
        except Exception as exc:
            logger.warning(f"[分析] 归档历史失败，已跳过：{exc}", exc_info=True)
            return "", ""

    # ════════════════════════════════════════════════════════════
    # 结果附图 / 附件
    # ════════════════════════════════════════════════════════════

    async def build_report_segments(
        self, settings: AnalysisSettings, report: str,
        digest: Optional[DigestResult],
        provider: Optional[RepoProvider],
        *, allow_files: bool = True,
    ) -> tuple[list[OutSegment], list[str]]:
        """
        把报告切成「文本 / 图片 / 文件」有序片段，并为附件取出字节。

        这是附图与附件发送的统一入口，分析路径与追问路径共用。
        """
        if not report:
            return [], []
        try:
            max_images = max(0, int(settings.max_report_images or 0))
            max_files = max(0, int(settings.max_report_files or 0))
            if not settings.send_images:
                max_images = 0
            if not (settings.send_files and allow_files):
                max_files = 0
            if max_images <= 0 and max_files <= 0:
                # 不允许任何附件，但仍需剥离指令行
                return split_report(report), []

            max_image_bytes = max(64, int(settings.max_image_mb or 5)) * 1024 * 1024
            max_file_bytes = max(
                64, int(settings.max_file_attachment_mb or 20)
            ) * 1024 * 1024

            # 从本地仓库取附件（仅 @项目 来源；@日志 由压缩包直接读取）
            repo_files: dict[str, bytes] = {}
            if (settings.send_repo_images and provider is not None
                    and not settings.repo.use_agent()):
                wanted = find_repo_attachment_names(report)
                # 图片与文件分别限流，避免把整仓库读进内存
                wanted = self._limit_attachment_requests(
                    wanted, max_images, max_files
                )
                if wanted:
                    found = await asyncio.to_thread(
                        provider.find_files, wanted, max(max_images, max_files) + 4
                    )
                    repo_files = {rel: data for rel, data in found}

            return build_report_segments(
                report, digest,
                repo_files=repo_files,
                max_attachments=max_images + max_files,
                max_image_bytes=max_image_bytes,
                max_file_bytes=max_file_bytes,
                allow_files=max_files > 0,
            )
        except Exception as exc:
            logger.warning(f"[附件] 解析失败，已跳过：{exc}", exc_info=True)
            return split_report(report), []

    @staticmethod
    def _limit_attachment_requests(paths: list[str], max_images: int,
                                   max_files: int) -> list[str]:
        """按图片/文件各自上限裁剪仓库检索请求，避免无谓遍历。"""
        images: list[str] = []
        files: list[str] = []
        for path in paths:
            suffix = Path(path.replace("\\", "/")).suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                if len(images) < max_images:
                    images.append(path)
            elif len(files) < max_files:
                files.append(path)
        return images + files

    # ════════════════════════════════════════════════════════════
    # 追问答疑
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _build_followup_context(file_name: str, prompt: str, repo_context: str,
                                report: str) -> str:
        """
        构造追问会话的首轮上下文。

        包含日志摘要与代码参考，使后续追问无需重新上传日志。
        两部分各有字符预算，避免摘要过长把代码参考挤成 0 字符。
        """
        total_limit = 120000
        head = [f"以下是一次已完成的 MaaXXX 日志分析，请基于它回答后续追问。", ""]
        head.append(f"【日志包】{file_name}")
        head.append("")

        repo_budget = int(total_limit * 0.35) if repo_context else 0
        prompt_budget = max(20000, total_limit - repo_budget)

        parts = list(head)
        if len(prompt) <= prompt_budget:
            parts.append("【日志摘要】")
            parts.append(prompt)
        else:
            parts.append("【日志摘要】（已截断）")
            parts.append(prompt[:prompt_budget] + "\n...[摘要已截断]")

        if repo_context:
            parts.append("")
            parts.append("【项目代码参考】")
            if len(repo_context) <= repo_budget:
                parts.append(repo_context)
            else:
                parts.append(repo_context[:repo_budget] + "\n...[代码参考已截断]")

        return "\n".join(parts)

    async def answer_followup(self, *, settings: AnalysisSettings,
                              messages: list[dict], question: str,
                              digest: Optional[DigestResult] = None,
                              provider: Optional[RepoProvider] = None,
                              ) -> Optional["FollowupAnswer"]:
        """
        基于已有会话上下文回答追问。

        把历史消息 + 新问题发给模型；失败时返回 None。

        返回 FollowupAnswer，其中 segments 已解析附件指令并按原位置排列，
        text 是剥离指令后的纯文本，供上层发送。
        """
        system_prompt = self._system_prompt(settings)
        temperature = settings.temperature
        if temperature is None:
            temperature = self.llm.temperature

        # 追问是否开放工具：日志包可用（或 agent 模式的仓库可用）
        allow_tools = bool(settings.log_tools_enabled) or (
            provider is not None and settings.repo.use_agent()
        )

        followup_system = (
            f"{system_prompt}\n\n"
            "【追问答疑模式】\n"
            "用户正在就上面那次分析继续提问。请基于已提供的日志摘要与代码参考回答，"
            "不要要求用户重新上传日志。如果问题涉及摘要中未包含的信息，"
            "明确说明“摘要中未包含”，并指出需要补充什么。"
            "回答要简洁、面向普通用户，不要输出 Markdown 大段代码。\n"
            "追问时同样可以声明附带内容，必须指定来源与相对路径：\n"
            "  `[附图@日志: on_error/xxx.png]`、`[附件@项目: assets/.../PVP.json]`\n"
            "（`@日志` = 本次上传的日志压缩包，`@项目` = 项目代码仓库）\n"
            "图片会与相邻文字合并为同一条消息，写在想要出现的位置即可。"
            "如果确实找不到，直接说明找不到，不要编造路径。"
            + (AGENT_TOOL_HINT_FOLLOWUP if allow_tools else "")
        )

        payload: list[dict] = [{"role": "system", "content": followup_system}]
        payload.extend(messages)
        payload.append({"role": "user", "content": question})

        # 追问同样开放工具：让 AI 能读日志包原始文件与项目代码，
        # 而不是只能看到首轮摘要里已发送的内容。
        log_tools = None
        if settings.log_tools_enabled and digest is not None:
            zip_path = str(getattr(digest, "zip_path", "") or "")
            if zip_path:
                candidate = LogArchiveTools(
                    zip_path,
                    max_file_kb=max(64, int(settings.max_log_file_read_kb or 512)),
                    max_result_chars=max(
                        2000, int(settings.repo.max_tool_result_chars or 30000)
                    ),
                )
                if candidate.available():
                    log_tools = candidate

        tools: list[dict] = []
        if provider is not None and settings.repo.use_agent():
            tools = list(TOOL_DEFINITIONS)
        if log_tools is not None:
            tools = tools + LOG_TOOL_DEFINITIONS

        try:
            if tools:
                raw = await self._run_tool_conversation(
                    settings, payload, tools,
                    repo_tools=RepoTools(provider, settings.repo)
                    if (provider is not None and settings.repo.use_agent()) else None,
                    log_tools=log_tools,
                )
            else:
                reply = await self.llm.chat_messages(
                    payload,
                    temperature=float(temperature),
                    model_override=str(settings.model or "").strip(),
                )
                raw = reply.content if reply is not None else None
        except Exception as exc:
            logger.warning(f"[追问] 调用失败：{exc}", exc_info=True)
            return None

        if not raw:
            return None
        raw = _strip_model_preamble(raw) or ""
        if not raw:
            return None

        # 追问同样支持附图/附件：解析为有序片段，避免指令行漏成文字
        segments, notes = await self.build_report_segments(
            settings, raw, digest, provider
        )
        text = strip_attachments(raw) or ""
        if notes:
            text = f"{text}\n\n" + "\n".join(f"（{note}）" for note in notes)
            segments = _append_text_segment(segments, notes)

        return FollowupAnswer(text=text, segments=segments)

    async def _run_tool_conversation(self, settings: AnalysisSettings,
                                     messages: list[dict], tools: list[dict],
                                     *, repo_tools: Optional[RepoTools],
                                     log_tools: Optional["LogArchiveTools"],
                                     ) -> Optional[str]:
        """
        执行一段工具对话（追问场景），返回模型最终文本。

        与 agent 分析共用 `_dispatch_agent_tool`，限制逻辑一致。
        """
        repo_cfg = settings.repo
        max_rounds = max(1, int(repo_cfg.max_tool_rounds or 8))
        deadline = time.monotonic() + max(60, int(repo_cfg.agent_deadline_seconds or 600))
        temperature = settings.temperature
        if temperature is None:
            temperature = self.llm.temperature
        model_override = str(settings.model or "").strip()

        last_text = ""
        for round_index in range(1, max_rounds + 1):
            if time.monotonic() > deadline:
                logger.info("[追问] 工具循环达到时长上限，使用已有内容")
                break

            reply = await self.llm.chat_messages(
                messages, temperature=float(temperature),
                model_override=model_override, tools=tools,
            )
            if reply is None:
                break
            if reply.content:
                last_text = reply.content
            if not reply.wants_tools:
                return last_text or None

            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        },
                    }
                    for call in reply.tool_calls
                ],
            })

            per_round = max(1, int(repo_cfg.max_tool_calls_per_round or 6))
            for call in reply.tool_calls[:per_round]:
                if call.parse_error:
                    result = f"参数解析失败：{call.parse_error}"
                else:
                    result = await self._dispatch_agent_tool(
                        call.name, call.arguments, repo_tools, log_tools
                    )
                logger.info(f"[追问] 工具 {call.name} → {len(result)} 字符")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        # 轮次用尽：要求收敛
        if not last_text:
            return None
        messages.append({
            "role": "user",
            "content": "已达到检索轮次上限，请立即基于现有信息作答，不要再调用工具。",
        })
        try:
            final = await self.llm.chat_messages(
                messages, temperature=float(temperature),
                model_override=model_override,
            )
            if final and final.content:
                return final.content
        except Exception as exc:
            logger.warning(f"[追问] 收敛调用失败：{exc}")
        return last_text

    # ════════════════════════════════════════════════════════════
    # 项目代码参考
    # ════════════════════════════════════════════════════════════

    def _repo_provider(self, config_name: str,
                       settings: AnalysisSettings) -> Optional[RepoProvider]:
        """按配置缓存 RepoProvider，来源变化时自动重建。"""
        repo_cfg = settings.repo
        if not repo_cfg or not repo_cfg.enabled or not repo_cfg.usable():
            return None
        cache_key = f"{config_name}|{repo_cfg.path}|{repo_cfg.url}|{repo_cfg.branch}"
        cached = self._repos.get(config_name)
        if cached and getattr(cached, "_cache_key", "") == cache_key:
            return cached
        provider = RepoProvider(repo_cfg, self._repo_data_dir())
        provider._cache_key = cache_key  # type: ignore[attr-defined]
        self._repos[config_name] = provider
        return provider

    def _repo_data_dir(self) -> Path:
        return Path(self.s.dm._dir)

    async def _prepare_repo(self, config_name: str,
                            settings: AnalysisSettings,
                            prompt: str,
                            file_name: str = "") -> "RepoPrep":
        """
        准备项目代码：确保仓库可用并切到日志对应版本。

        inject 模式下顺带预检索代码片段；agent 模式只准备仓库（由 AI 自主检索）。
        任何失败都只记录日志，绝不阻断分析。
        """
        prep = RepoPrep()
        provider = self._repo_provider(config_name, settings)
        if provider is None:
            return prep

        try:
            if not await provider.ensure_ready():
                logger.warning(f"[分析] 项目代码不可用：{provider.last_error}")
                return prep

            if file_name:
                ref = await provider.align_version(file_name)
                if ref:
                    logger.info(f"[分析] 项目代码已对齐到版本 {ref}")

            prep.provider = provider

            if settings.repo.use_agent():
                logger.info("[分析] 项目代码使用 agent 模式，由 AI 自主检索")
                return prep

            identifiers = extract_identifiers(
                prompt, limit=max(1, int(settings.repo.max_identifiers or 24))
            )
            if not identifiers:
                logger.info("[分析] 摘要中未提取到可检索的标识符")
                return prep

            logger.info(f"[分析] 检索项目代码，标识符 {len(identifiers)} 个：{', '.join(identifiers[:8])}")
            prep.context = await provider.collect_references(identifiers)
        except Exception as exc:
            logger.warning(f"[分析] 项目代码准备异常，已跳过：{exc}", exc_info=True)
        return prep

    # ════════════════════════════════════════════════════════════
    # agent 模式：AI 自主调用仓库工具
    # ════════════════════════════════════════════════════════════

    async def _run_repo_agent(self, settings: AnalysisSettings, prompt: str,
                              provider: Optional[RepoProvider],
                              log_tools: Optional["LogArchiveTools"] = None,
                              ) -> Optional[str]:
        """
        让 AI 通过工具调用自主检索**项目仓库**与**日志压缩包**，最后输出报告。

        仓库工具与日志工具合并为同一套（用 source 参数区分），
        因此模型可以像读仓库一样读取日志包内的原始文件。

        循环受三重限制：最大轮次、总时长、单次工具结果字符数。
        任何异常都退化为「返回已有文本」，不会让分析整体失败。
        """
        repo_cfg = settings.repo
        repo_tools = RepoTools(provider, repo_cfg) if provider is not None else None
        if repo_tools is None and log_tools is None:
            return None

        # 合并工具定义：有仓库则用仓库工具，另加日志工具
        tools = list(TOOL_DEFINITIONS) if repo_tools is not None else []
        if log_tools is not None:
            tools = tools + LOG_TOOL_DEFINITIONS

        max_rounds = max(1, int(repo_cfg.max_tool_rounds or 8))
        deadline = time.monotonic() + max(60, int(repo_cfg.agent_deadline_seconds or 600))

        system_prompt = self._system_prompt(settings)
        temperature = settings.temperature
        if temperature is None:
            temperature = self.llm.temperature
        model_override = str(settings.model or "").strip()

        hint = AGENT_TOOL_HINT
        if repo_tools is None:
            hint = AGENT_TOOL_HINT_LOG_ONLY
        elif log_tools is None:
            hint = AGENT_TOOL_HINT_REPO_ONLY

        messages: list[dict] = [
            {"role": "system", "content": system_prompt + hint},
            {"role": "user", "content": prompt},
        ]

        last_text = ""
        tool_calls_total = 0

        for round_index in range(1, max_rounds + 1):
            if time.monotonic() > deadline:
                logger.warning("[分析] agent 循环达到时长上限，使用已有结论")
                break

            reply = await self.llm.chat_messages(
                messages,
                temperature=float(temperature),
                model_override=model_override,
                tools=tools,
            )
            if reply is None:
                logger.warning("[分析] agent 轮次调用失败，使用已有结论")
                break

            if reply.content:
                last_text = reply.content

            if not reply.wants_tools:
                logger.info(f"[分析] agent 完成，共 {round_index - 1} 轮工具调用")
                return last_text or reply.content or None

            # 记录助手消息（含 tool_calls）以保持上下文连续
            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.raw_arguments},
                    }
                    for call in reply.tool_calls
                ],
            })

            # 单轮工具数上限，避免一次请求打爆 IO
            per_round = max(1, int(repo_cfg.max_tool_calls_per_round or 6))
            calls = reply.tool_calls[:per_round]
            if len(reply.tool_calls) > per_round:
                logger.info(f"[分析] 本轮 {len(reply.tool_calls)} 个工具调用，"
                            f"仅执行前 {per_round} 个")

            for call in calls:
                tool_calls_total += 1
                if call.parse_error:
                    result = f"参数解析失败：{call.parse_error}"
                else:
                    result = await self._dispatch_agent_tool(
                        call.name, call.arguments, repo_tools, log_tools
                    )
                logger.info(f"[分析] 工具 {call.name} → {len(result)} 字符")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        # 轮次/时长用尽：要求模型基于已有信息收敛出报告
        if tool_calls_total == 0:
            return last_text or None

        logger.info(f"[分析] agent 达到轮次上限（共 {tool_calls_total} 次工具调用），要求收敛")
        messages.append({
            "role": "user",
            "content": ("已达到检索轮次上限，请立即基于目前掌握的信息输出最终分析报告，"
                        "不要再请求调用工具。格式与之前一致。"),
        })
        try:
            final = await self.llm.chat_messages(
                messages,
                temperature=float(temperature),
                model_override=model_override,
            )
            if final and final.content:
                return final.content
        except Exception as exc:
            logger.warning(f"[分析] agent 收敛调用失败：{exc}")

        return last_text or None

    @staticmethod
    async def _dispatch_agent_tool(name: str, arguments: dict,
                                   repo_tools: Optional[RepoTools],
                                   log_tools: Optional["LogArchiveTools"]) -> str:
        """
        分发一次工具调用到仓库工具或日志工具。

        日志工具统一加 `log_` 前缀，避免与仓库工具重名。
        """
        if name.startswith("log_"):
            if log_tools is None:
                return "本次分析未提供日志包工具（日志包可能已被清理）"
            args = arguments or {}
            try:
                if name == "log_list_files":
                    return await log_tools.list_files(args.get("path", ""))
                if name == "log_search":
                    return await log_tools.search(
                        args.get("pattern", ""), args.get("path", ""),
                        args.get("max_results", 60),
                    )
                if name == "log_read_file":
                    return await log_tools.read_file(
                        args.get("path", ""),
                        args.get("start_line"), args.get("end_line"),
                    )
                if name == "log_list_images":
                    return await log_tools.list_images()
            except Exception as exc:
                logger.warning(f"[分析] 日志工具 {name} 异常：{exc}", exc_info=True)
                return f"日志工具 {name} 执行失败：{type(exc).__name__}: {exc}"
            return f"未知日志工具：{name}"

        if repo_tools is None:
            return "本次分析未启用项目代码仓库"
        return await repo_tools.execute(name, arguments)

    async def _maybe_recheck_with_other_tag(
        self, config_name: str, settings: AnalysisSettings, prompt: str,
        response_text: Optional[str], repo_context: str,
    ) -> Optional[str]:
        """
        如果 AI 在报告末尾请求核对其他 tag，则切换后重新分析一次。

        请求格式（AI 输出末尾单独一行）：
            [需要核对版本: v1.0.0]

        受 repo.max_tag_switch_rounds 限制，且每次都会切回最新版本。
        """
        repo_cfg = settings.repo
        if not response_text or not repo_context:
            return response_text
        if not repo_cfg or not repo_cfg.allow_ai_switch_tag:
            return response_text

        rounds = max(0, int(repo_cfg.max_tag_switch_rounds or 0))
        if rounds <= 0:
            return response_text

        provider = self._repos.get(config_name)
        if provider is None:
            return response_text

        tried: set[str] = {provider.current_ref}
        for _ in range(rounds):
            wanted = _extract_version_request(response_text)
            if not wanted or wanted in tried:
                break

            logger.info(f"[分析] AI 请求核对版本 {wanted}，切换后重新分析")
            if not await provider.checkout(provider.root, wanted):
                logger.warning(f"[分析] 无法切换到版本 {wanted}，保留原结论")
                break
            tried.add(wanted)

            try:
                identifiers = extract_identifiers(
                    prompt, limit=max(1, int(repo_cfg.max_identifiers or 24))
                )
                new_context = await provider.collect_references(identifiers)
                if not new_context:
                    break
                # 用新版本代码替换旧段落，并明确要求 AI 给出最终结论
                base_prompt = prompt.split("\n\n项目代码参考", 1)[0]
                follow_up = (
                    f"{base_prompt}\n\n{new_context}\n\n"
                    f"（以上是版本 {wanted} 的代码。请基于它核对前一次的判断，"
                    f"直接输出最终分析报告，格式与之前一致；"
                    f"如仍需核对其他版本，在最后单独一行写 [需要核对版本: 版本号]。）"
                )
                response_text = await asyncio.wait_for(
                    self._call_ai(settings, follow_up),
                    timeout=max(30, int(settings.llm_timeout_seconds or 900)),
                ) or response_text
            except Exception as exc:
                logger.warning(f"[分析] 版本 {wanted} 复核失败：{exc}", exc_info=True)
                break

        return response_text

    # ════════════════════════════════════════════════════════════
    # 临时目录
    # ════════════════════════════════════════════════════════════

    def _jobs_root(self) -> Path:
        base = Path(self.s.dm._dir) / "_jobs"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _build_work_dir(self, config_name: str, group_id: int,
                        file_name: str, file_id: Optional[str]) -> Path:
        safe_file = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in file_name)[:80]
        safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (file_id or "no_file_id"))[:40]
        safe_cfg = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in config_name)[:40]
        path = self._jobs_root() / f"{int(time.time() * 1000)}_{safe_cfg}_{group_id}_{safe_id}_{safe_file}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_work_dir(self, work_dir: Path):
        try:
            root = self._jobs_root().resolve()
            target = work_dir.resolve()
            if root in target.parents:
                shutil.rmtree(target, ignore_errors=True)
        except Exception as exc:
            logger.warning(f"[分析] 清理临时目录失败：{exc}")

    def _save_digest_debug_if_enabled(
        self,
        config_name: str,
        settings: AnalysisSettings,
        file_name: str,
        group_id: int,
        prompt: str,
    ) -> Optional[Path]:
        if not bool(settings.save_digest_debug):
            return None
        try:
            debug_dir = Path(self.s.dm._dir) / "debug_digests"
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_file = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in file_name)[:100]
            safe_cfg = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in config_name)[:40]
            path = debug_dir / f"{int(time.time() * 1000)}_{safe_cfg}_{group_id}_{safe_file}.txt"
            path.write_text(prompt, encoding="utf-8")
            return path
        except Exception as exc:
            logger.warning(f"[分析] 保存摘要调试文件失败：{exc}", exc_info=True)
            return None
