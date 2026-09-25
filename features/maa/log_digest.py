from __future__ import annotations

import json
import re
import zipfile
import codecs
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .text_utils import format_bytes, limit_text


ERROR_PATTERN = re.compile(
    r"\bERROR\b|\bERR\b|\bWARN\b|\bWRN\b|\bFAILED\b|\bFAIL\b|\bEXCEPTION\b|\bTRACEBACK\b|"
    r"Recognition\.Failed|RecognitionNode\.Failed|Action\.Failed|Task\.[^\s\]]*Failed|"
    r"拒绝访问|截图失败|失败|错误|异常|internal error",
    re.IGNORECASE,
)
TIMESTAMP_PATTERN = re.compile(r"\[?(\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")
TEXT_SUFFIXES = {".log", ".txt", ".json", ".yml", ".yaml", ".ini", ".cfg", ".conf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
KEYWORD_PATTERNS = [
    ("ERROR/ERR", re.compile(r"\bERROR\b|\bERR\b|错误", re.IGNORECASE)),
    ("WARN/WRN", re.compile(r"\bWARN\b|\bWRN\b", re.IGNORECASE)),
    ("失败/Failed", re.compile(r"FAILED|FAIL|失败", re.IGNORECASE)),
    ("Recognition.Failed", re.compile(r"Recognition\.Failed|RecognitionNode\.Failed", re.IGNORECASE)),
    ("OCR 异常", re.compile(r"Wrong ocr_result size|OCR", re.IGNORECASE)),
    ("截图失败", re.compile(r"截图失败|internal error: status 0", re.IGNORECASE)),
    ("拒绝访问", re.compile(r"拒绝访问|access denied", re.IGNORECASE)),
    ("Exception/Traceback", re.compile(r"EXCEPTION|TRACEBACK|异常", re.IGNORECASE)),
]


@dataclass
class DigestOptions:
    max_prompt_chars: int = 600000
    max_zip_members: int = 300
    max_total_uncompressed_bytes: int = 120 * 1024 * 1024
    max_file_read_bytes: int = 512 * 1024
    small_log_full_read_bytes: int = 5 * 1024 * 1024
    stream_medium_logs: bool = True
    max_large_log_read_bytes: int = 256 * 1024
    large_log_threshold_bytes: int = 50 * 1024 * 1024
    stream_large_logs: bool = True
    max_stream_log_bytes: int = 0
    max_log_files: int = 6
    max_maafw_bak_files: int = 1
    log_head_lines: int = 16
    context_before_lines: int = 80
    context_after_lines: int = 60
    max_sections_per_log: int = 60
    max_log_chars: int = 120000
    max_config_chars: int = 6000
    max_error_images: int = 20
    compress_context_noise: bool = True
    max_recognition_evidence_lines: int = 60
    max_recognition_evidence_chars: int = 35000


@dataclass
class DigestResult:
    prompt: str
    file_count: int
    total_uncompressed_size: int
    log_files: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    error_images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 压缩包内全部图片（供分析结果附图使用）
    all_images: list[str] = field(default_factory=list)
    # 压缩包内全部文件（供模型请求任意附件使用）
    all_members: list[str] = field(default_factory=list)
    # 压缩包路径（供后续按名提取图片字节）
    zip_path: str = ""


@dataclass
class LogProcessResult:
    time_range: str | None = None
    keyword_summary: str | None = None
    section: str | None = None


def build_log_digest(
    zip_path: str,
    *,
    original_file_name: str,
    group_id: str | int | None = None,
    project_name: str = "MaaXXX",
    options: DigestOptions | None = None,
) -> DigestResult:
    opts = options or DigestOptions()

    with zipfile.ZipFile(zip_path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        notes: list[str] = []
        if len(infos) > opts.max_zip_members:
            notes.append(f"压缩包文件数 {len(infos)} 超过限制 {opts.max_zip_members}，仅处理前者范围内的重点文件。")

        total_uncompressed = sum(max(0, int(info.file_size)) for info in infos)
        if total_uncompressed > opts.max_total_uncompressed_bytes:
            notes.append(
                f"压缩包解压后大小 {format_bytes(total_uncompressed)} 超过软限制 "
                f"{format_bytes(opts.max_total_uncompressed_bytes)}；插件未全量解压，仅按上限读取重点日志。"
            )

        safe_infos = [info for info in infos if _is_safe_member(info.filename)]
        if len(safe_infos) != len(infos):
            notes.append("压缩包中存在不安全路径，已跳过。")

        log_infos = _select_log_infos(safe_infos, opts)
        config_infos = _select_config_infos(safe_infos)
        error_images = _select_error_images(safe_infos, opts.max_error_images)
        all_images = _list_all_images(safe_infos)

        time_ranges: list[str] = []
        log_sections: list[str] = []
        keyword_summaries: list[str] = []
        for info in log_infos:
            processed = _process_log_member(archive, info, opts, notes)
            if processed.time_range:
                time_ranges.append(f"{info.filename}: {processed.time_range}")
            if processed.keyword_summary:
                keyword_summaries.append(processed.keyword_summary)
            if processed.section:
                log_sections.append(processed.section)

        config_sections: list[str] = []
        for info in config_infos:
            text, truncated = _read_text_member(archive, info, opts.max_file_read_bytes)
            if truncated:
                notes.append(f"{info.filename} 超过单文件读取限制，配置摘要可能不完整。")
            config_sections.append(_summarize_config(info.filename, text, opts.max_config_chars))

    prompt = _build_prompt(
        original_file_name=original_file_name,
        group_id=group_id,
        project_name=project_name,
        file_count=len(infos),
        total_uncompressed_size=total_uncompressed,
        log_files=[info.filename for info in log_infos],
        config_files=[info.filename for info in config_infos],
        error_images=error_images,
        time_ranges=time_ranges,
        keyword_summaries=keyword_summaries,
        config_sections=config_sections,
        log_sections=log_sections,
        notes=notes,
        max_prompt_chars=opts.max_prompt_chars,
    )

    return DigestResult(
        prompt=prompt,
        file_count=len(infos),
        total_uncompressed_size=total_uncompressed,
        log_files=[info.filename for info in log_infos],
        config_files=[info.filename for info in config_infos],
        error_images=error_images,
        notes=notes,
        all_images=all_images,
        all_members=[info.filename.replace("\\", "/") for info in safe_infos],
        zip_path=str(zip_path),
    )


def describe_zip(zip_path: str,
                 options: DigestOptions | None = None) -> DigestResult | None:
    """
    只索引压缩包内容（不生成摘要提示词）。

    用于重启后从历史归档恢复追问会话：此时只需知道包内有哪些成员
    （供 `[附图@日志: ...]` 附件与日志工具使用），无需重新提取摘要。
    压缩包不存在或不可读时返回 None。
    """
    opts = options or DigestOptions()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None

    safe_infos = [info for info in infos if _is_safe_member(info.filename)]
    return DigestResult(
        prompt="",
        file_count=len(infos),
        total_uncompressed_size=sum(max(0, int(info.file_size)) for info in infos),
        log_files=[info.filename for info in _select_log_infos(safe_infos, opts)],
        config_files=[info.filename for info in _select_config_infos(safe_infos)],
        error_images=_select_error_images(safe_infos, opts.max_error_images),
        all_images=_list_all_images(safe_infos),
        all_members=[info.filename.replace("\\", "/") for info in safe_infos],
        zip_path=str(zip_path),
    )


def _list_all_images(infos: list[zipfile.ZipInfo]) -> list[str]:
    """列出压缩包内全部图片（含 on_error 截图与资源图）。"""
    result: list[str] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES:
            result.append(name)
    return sorted(result)


def read_zip_member(zip_path: str, member_name: str,
                    max_bytes: int = 0) -> bytes | None:
    """
    从压缩包中读取任意成员的字节，供发送到群聊使用。

    超过 max_bytes 或读取失败时返回 None。
    """
    if not zip_path or not member_name:
        return None
    wanted = member_name.replace("\\", "/")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.filename.replace("\\", "/") != wanted:
                    continue
                if max_bytes > 0 and info.file_size > max_bytes:
                    return None
                return archive.read(info)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return None
    return None


def _is_safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return False
    return not any(part in ("", ".", "..") for part in path.parts)


def _select_log_infos(infos: list[zipfile.ZipInfo], opts: DigestOptions) -> list[zipfile.ZipInfo]:
    logs = [info for info in infos if info.filename.lower().endswith(".log")]

    dated_logs: list[zipfile.ZipInfo] = []
    maafw_current: list[zipfile.ZipInfo] = []
    maafw_bak: list[zipfile.ZipInfo] = []
    agent_logs: list[zipfile.ZipInfo] = []
    other_logs: list[zipfile.ZipInfo] = []

    for info in logs:
        name = info.filename.replace("\\", "/").lower()
        base = PurePosixPath(name).name
        if re.match(r"\d{4}[-.]\d{2}[-.]\d{2}", base):
            dated_logs.append(info)
        elif base == "maafw.log":
            maafw_current.append(info)
        elif base.startswith("maafw.bak"):
            maafw_bak.append(info)
        elif "agent" in base:
            agent_logs.append(info)
        else:
            other_logs.append(info)

    deduped: list[zipfile.ZipInfo] = []
    seen: set[str] = set()

    def add_info(info: zipfile.ZipInfo | None) -> None:
        if not info or len(deduped) >= opts.max_log_files:
            return
        key = info.filename.replace("\\", "/").lower()
        if key in seen:
            return
        seen.add(key)
        deduped.append(info)

    # 保底优先级：当前主日志、最新 GUI/日期日志、最新 agent 日志。
    add_info(_newest_log(maafw_current))
    add_info(_newest_log(dated_logs))
    add_info(_newest_log(agent_logs))

    for info in sorted(maafw_bak, key=_log_recency_key, reverse=True)[: max(0, opts.max_maafw_bak_files)]:
        add_info(info)

    for info in sorted(dated_logs + agent_logs + other_logs, key=_log_recency_key, reverse=True):
        add_info(info)

    return deduped


def _newest_log(infos: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    if not infos:
        return None
    return max(infos, key=_log_recency_key)


def _log_recency_key(info: zipfile.ZipInfo) -> tuple[tuple[int, int, int, int, int, int], str]:
    name = info.filename.replace("\\", "/")
    base = PurePosixPath(name).name
    parsed_time = _parse_log_time_from_name(base)
    return parsed_time or info.date_time, name.lower()


def _parse_log_time_from_name(base: str) -> tuple[int, int, int, int, int, int] | None:
    match = re.search(
        r"(\d{4})[-.](\d{2})[-.](\d{2})(?:[-_.](\d{2})[-.](\d{2})[-.](\d{2}))?",
        base,
    )
    if not match:
        return None
    year, month, day = (int(match.group(index)) for index in range(1, 4))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    return year, month, day, hour, minute, second


def _get_log_read_limit(info: zipfile.ZipInfo, opts: DigestOptions) -> int:
    if int(info.file_size) >= opts.large_log_threshold_bytes:
        return max(32 * 1024, opts.max_large_log_read_bytes)
    return max(32 * 1024, opts.max_file_read_bytes)


def _process_log_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    opts: DigestOptions,
    notes: list[str],
) -> LogProcessResult:
    file_size = int(info.file_size)
    if file_size <= opts.small_log_full_read_bytes:
        text, truncated = _read_text_member(archive, info, opts.small_log_full_read_bytes)
        if truncated:
            notes.append(f"{info.filename} 超过小日志完整读取限制，已截断。")
        return LogProcessResult(
            time_range=_extract_time_range(text),
            keyword_summary=_summarize_keywords(info.filename, text),
            section=_extract_error_context(info.filename, text, opts),
        )

    if opts.stream_medium_logs and file_size < opts.large_log_threshold_bytes:
        result, scanned_bytes, truncated = _stream_scan_log_member(archive, info, opts, stream_limit_bytes=0)
        if truncated:
            notes.append(
                f"{info.filename} 已流式扫描 {format_bytes(scanned_bytes)} 后停止；"
                "只保留去重后的关键错误片段。"
            )
        else:
            notes.append(
                f"{info.filename} 已流式扫描完整中等日志 {format_bytes(scanned_bytes)}；"
                "只保留去重后的关键错误片段。"
            )
        return result

    if opts.stream_large_logs and file_size >= opts.large_log_threshold_bytes:
        result, scanned_bytes, truncated = _stream_scan_log_member(
            archive,
            info,
            opts,
            stream_limit_bytes=opts.max_stream_log_bytes,
        )
        if truncated:
            notes.append(
                f"{info.filename} 已流式扫描 {format_bytes(scanned_bytes)} 后停止；"
                "只保留去重后的关键错误片段。"
            )
        else:
            notes.append(
                f"{info.filename} 已流式扫描完整日志 {format_bytes(scanned_bytes)}；"
                "只保留去重后的关键错误片段。"
            )
        return result

    read_limit = _get_log_read_limit(info, opts)
    text, truncated = _read_text_member(archive, info, read_limit)
    if truncated:
        notes.append(f"{info.filename} 超过读取限制 {format_bytes(read_limit)}，已截断。")
    return LogProcessResult(
        time_range=_extract_time_range(text),
        keyword_summary=_summarize_keywords(info.filename, text),
        section=_extract_error_context(info.filename, text, opts),
    )


def _stream_scan_log_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    opts: DigestOptions,
    stream_limit_bytes: int | None = None,
) -> tuple[LogProcessResult, int, bool]:
    max_bytes = max(0, int(opts.max_stream_log_bytes if stream_limit_bytes is None else stream_limit_bytes))
    before_lines: deque[tuple[int, str]] = deque(maxlen=_context_raw_scan_limit(opts.context_before_lines))
    tail_lines: deque[str] = deque(maxlen=80)
    pending_sections: list[dict[str, Any]] = []
    captured_sections: list[dict[str, Any]] = []
    signature_counts: dict[str, int] = {}
    keyword_counts: dict[str, int] = {}
    recognition_evidence: dict[str, str] = {}
    head_lines: list[str] = []

    first_time: str | None = None
    last_time: str | None = None
    line_no = 0
    scanned_bytes = 0
    truncated = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text_buffer = ""

    with archive.open(info, "r") as file:
        while True:
            read_size = 64 * 1024
            if max_bytes > 0:
                remaining = max_bytes - scanned_bytes
                if remaining <= 0:
                    truncated = True
                    break
                read_size = min(read_size, remaining)

            chunk = file.read(read_size)
            if not chunk:
                break
            scanned_bytes += len(chunk)
            text_buffer += decoder.decode(chunk)
            lines = text_buffer.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                text_buffer = lines.pop()
            else:
                text_buffer = ""
            for raw_line in lines:
                line_no += 1
                line = raw_line.rstrip("\r\n")
                _append_log_head_line(line, line_no, head_lines, opts)
                interesting = _line_may_have_keyword(line)
                first_time, last_time = _update_time_range(line, first_time, last_time)
                if interesting:
                    _update_keyword_counts(line, keyword_counts)
                    _append_recognition_evidence(
                        line,
                        line_no,
                        recognition_evidence,
                        opts,
                    )
                _consume_stream_line(
                    line,
                    line_no,
                    before_lines,
                    tail_lines,
                    pending_sections,
                    captured_sections,
                    signature_counts,
                    opts,
                    interesting,
                )

    rest = decoder.decode(b"", final=True)
    if text_buffer or rest:
        line_no += 1
        line = (text_buffer + rest).rstrip("\r\n")
        _append_log_head_line(line, line_no, head_lines, opts)
        interesting = _line_may_have_keyword(line)
        first_time, last_time = _update_time_range(line, first_time, last_time)
        if interesting:
            _update_keyword_counts(line, keyword_counts)
            _append_recognition_evidence(
                line,
                line_no,
                recognition_evidence,
                opts,
            )
        _consume_stream_line(
            line,
            line_no,
            before_lines,
            tail_lines,
            pending_sections,
            captured_sections,
            signature_counts,
            opts,
            interesting,
        )

    captured_sections.extend(pending_sections)
    _trim_stream_sections(captured_sections, [], opts.max_sections_per_log)
    section = _build_stream_section(
        info.filename,
        captured_sections,
        signature_counts,
        tail_lines,
        list(recognition_evidence.values()),
        head_lines,
        opts,
    )
    time_range = f"{first_time} ~ {last_time}" if first_time and last_time else None
    return (
        LogProcessResult(
            time_range=time_range,
            keyword_summary=_format_keyword_summary(info.filename, keyword_counts),
            section=section,
        ),
        scanned_bytes,
        truncated,
    )


def _consume_stream_line(
    line: str,
    line_no: int,
    before_lines: deque[tuple[int, str]],
    tail_lines: deque[str],
    pending_sections: list[dict[str, Any]],
    captured_sections: list[dict[str, Any]],
    signature_counts: dict[str, int],
    opts: DigestOptions,
    interesting: bool,
) -> None:
    compact_line = _compact_line(line)
    tail_lines.append(compact_line)

    finished: list[dict[str, Any]] = []
    for section in pending_sections:
        section["lines"].append((line_no, compact_line))
        section["end_line"] = line_no
        section["after_remaining"] -= 1
        if section["after_remaining"] <= 0:
            finished.append(section)
    if finished:
        captured_sections.extend(finished)
        pending_sections[:] = [section for section in pending_sections if section not in finished]

    if interesting and ERROR_PATTERN.search(line):
        signature = _make_error_signature(line)
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
        _drop_stream_section_by_signature(captured_sections, signature)
        _drop_stream_section_by_signature(pending_sections, signature)
        if opts.max_sections_per_log > 0:
            start_line = max(1, line_no - len(before_lines))
            pending_sections.append(
                {
                    "signature": signature,
                    "start_line": start_line,
                    "end_line": line_no,
                    "hit_line": line_no,
                    "after_remaining": _context_raw_scan_limit(opts.context_after_lines),
                    "lines": list(before_lines) + [(line_no, compact_line)],
                }
            )
            _trim_stream_sections(captured_sections, pending_sections, opts.max_sections_per_log)

    before_lines.append((line_no, compact_line))


def _drop_stream_section_by_signature(sections: list[dict[str, Any]], signature: str) -> None:
    sections[:] = [section for section in sections if section.get("signature") != signature]


def _trim_stream_sections(
    captured_sections: list[dict[str, Any]],
    pending_sections: list[dict[str, Any]],
    limit: int,
) -> None:
    if limit <= 0:
        captured_sections.clear()
        pending_sections.clear()
        return
    while len(captured_sections) + len(pending_sections) > limit:
        if captured_sections:
            captured_sections.pop(0)
        elif pending_sections:
            pending_sections.pop(0)
        else:
            break


def _context_raw_scan_limit(compressed_line_limit: int) -> int:
    if compressed_line_limit <= 0:
        return 0
    return min(5000, max(compressed_line_limit, compressed_line_limit * 8 + 200))


def _append_log_head_line(line: str, line_no: int, head_lines: list[str], opts: DigestOptions) -> None:
    if opts.log_head_lines <= 0 or len(head_lines) >= opts.log_head_lines:
        return
    head_lines.append(f"[行 {line_no}] {_compact_line(line, limit=900)}")


def _line_may_have_keyword(line: str) -> bool:
    text = line.lower()
    return (
        "error" in text
        or "[err]" in text
        or "warn" in text
        or "wrn" in text
        or "fail" in text
        or "exception" in text
        or "traceback" in text
        or "recognition.failed" in text
        or "recognitionnode.failed" in text
        or "action.failed" in text
        or "wrong ocr_result size" in text
        or "ocr" in text
        or "internal error" in text
        or "拒绝访问" in line
        or "截图失败" in line
        or "失败" in line
        or "错误" in line
        or "异常" in line
    )


def _update_time_range(line: str, first_time: str | None, last_time: str | None) -> tuple[str | None, str | None]:
    match = TIMESTAMP_PATTERN.search(line)
    if not match:
        return first_time, last_time
    value = match.group(1)
    return first_time or value, value


def _update_keyword_counts(line: str, keyword_counts: dict[str, int]) -> None:
    for label, pattern in KEYWORD_PATTERNS:
        count = len(pattern.findall(line))
        if count:
            keyword_counts[label] = keyword_counts.get(label, 0) + count


def _append_recognition_evidence(
    line: str,
    line_no: int,
    evidence: dict[str, str],
    opts: DigestOptions,
) -> None:
    limit = opts.max_recognition_evidence_lines
    if limit <= 0:
        return
    if not _is_recognition_evidence_line(line):
        return
    signature = _make_evidence_signature(line)
    evidence.pop(signature, None)
    evidence[signature] = f"[行 {line_no}] {_compact_line(line, limit=1200)}"
    while len(evidence) > limit:
        evidence.pop(next(iter(evidence)), None)


def _is_recognition_evidence_line(line: str) -> bool:
    text = line.lower()
    return (
        "ocrer" in text
        or "recognizer::recognize" in text
        or "recognition.failed" in text
        or "recognitionnode.failed" in text
        or "wrong ocr_result size" in text
        or "expected=" in text
        or '"expected"' in text
        or "filtered_results_" in text
        or '"filtered"' in text
        or "best_result" in text
        or '"best"' in text
        or "reco_details" in text
        or "reco [result=" in text
    )


def _make_evidence_signature(line: str) -> str:
    text = line.strip()
    text = re.sub(r"\[[^\]]*\d{4}[-/]\d{2}[-/]\d{2}[^\]]*\]", "", text)
    text = re.sub(r'"reco_id"\s*:\s*\d+', '"reco_id":<num>', text)
    text = re.sub(r'"task_id"\s*:\s*\d+', '"task_id":<num>', text)
    text = re.sub(r'"node_id"\s*:\s*\d+', '"node_id":<num>', text)
    text = re.sub(r"\breco_id=\d+\b", "reco_id=<num>", text)
    text = re.sub(r"\btask_id_?=\d+\b", "task_id=<num>", text)
    text = re.sub(r"\bnode_id=\d+\b", "node_id=<num>", text)
    text = re.sub(r"\bscore[\"_=:\s]+[0-9.]+", "score=<num>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcost=\d+ms\b", "cost=<num>ms", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b", "<uuid>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b0000[0-9A-Fa-f]{8,}\b", "<ptr>", text)
    text = re.sub(r"\s+", " ", text)
    return text[:400]


def _format_keyword_summary(name: str, keyword_counts: dict[str, int]) -> str:
    counts = [f"{label}={keyword_counts[label]}" for label, _ in KEYWORD_PATTERNS if keyword_counts.get(label)]
    if not counts:
        return ""
    return f"- {name}: " + ", ".join(counts)


def _make_error_signature(line: str) -> str:
    text = line.strip()
    message = _search_first(
        text,
        [
            r"\[msg=([A-Za-z0-9_.-]+)\]",
            r"\[message=([A-Za-z0-9_.-]+)\]",
            r'"msg"\s*:\s*"([^"]+)"',
            r'"message"\s*:\s*"([^"]+)"',
        ],
    )
    node_name = _search_first(
        text,
        [
            r'"name"\s*:\s*"([^"]+)"',
            r"\[entry=([A-Za-z0-9_.-]+)\]",
        ],
    )
    if message or node_name:
        return f"{message or 'event'}:{node_name or 'unknown'}"

    normalized = re.sub(r"\[[^\]]*\d{4}[-/]\d{2}[-/]\d{2}[^\]]*\]", "", text)
    normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b", "<uuid>", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b\d{4,}\b", "<num>", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:220]


def _search_first(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _compact_line(line: str, limit: int = 900) -> str:
    if len(line) <= limit:
        return line
    half = max(100, (limit - 20) // 2)
    return f"{line[:half]} ... {line[-half:]}"


def _compress_context_lines(lines: list[str], opts: DigestOptions) -> list[str]:
    return [
        unit["text"]
        for unit in _compress_context_items(
            [(index, line) for index, line in enumerate(lines, 1)],
            opts,
        )
    ]


def _compress_context_items(items: list[tuple[int, str]], opts: DigestOptions) -> list[dict[str, Any]]:
    if not opts.compress_context_noise:
        return [
            {"start_line": line_no, "end_line": line_no, "text": _compact_line(line)}
            for line_no, line in items
        ]

    result: list[dict[str, Any]] = []
    low_value_run: list[tuple[int, str]] = []

    def flush_run() -> None:
        nonlocal low_value_run
        if not low_value_run:
            return
        if len(low_value_run) >= 3:
            sample = _compact_line(low_value_run[0][1], limit=240)
            result.append(
                {
                    "start_line": low_value_run[0][0],
                    "end_line": low_value_run[-1][0],
                    "text": f"[已省略 {len(low_value_run)} 行重复低价值 TRACE/DEBUG 上下文；示例：{sample}]",
                }
            )
        else:
            result.extend(
                {"start_line": line_no, "end_line": line_no, "text": _compact_line(line)}
                for line_no, line in low_value_run
            )
        low_value_run = []

    for line_no, line in items:
        compacted = _compact_line(line)
        if _is_low_value_context_line(compacted):
            low_value_run.append((line_no, compacted))
            continue
        flush_run()
        result.append({"start_line": line_no, "end_line": line_no, "text": compacted})

    flush_run()
    return result


def _format_context_section(
    items: list[tuple[int, str]],
    hit_line: int,
    opts: DigestOptions,
) -> tuple[int, int, list[str]] | None:
    before_items = [item for item in items if item[0] < hit_line]
    hit_items = [item for item in items if item[0] == hit_line]
    after_items = [item for item in items if item[0] > hit_line]

    before_units = _compress_context_items(before_items, opts)
    hit_units = _compress_context_items(hit_items, opts)
    after_units = _compress_context_items(after_items, opts)

    before_selected = before_units[-opts.context_before_lines :] if opts.context_before_lines > 0 else []
    after_selected = after_units[: opts.context_after_lines] if opts.context_after_lines > 0 else []
    selected = before_selected + hit_units + after_selected
    if not selected:
        return None
    return (
        int(selected[0]["start_line"]),
        int(selected[-1]["end_line"]),
        [str(unit["text"]) for unit in selected],
    )


def _is_low_value_context_line(line: str) -> bool:
    if not line.strip() or _is_high_value_context_line(line):
        return False

    text = line.lower()
    is_trace_or_debug = "[trc]" in text or "[dbg]" in text
    if not is_trace_or_debug:
        return False

    low_value_markers = (
        "| enter",
        "| leave",
        "handle_event_response",
        "handle_tasker_stopping",
        "handle_tasker_controller",
        "handle_controller_wait",
        "handle_controller_cached_image",
        "_controllereventresponse",
        "_taskerstoppingreverserequest",
        "_taskercontrollerreverserequest",
        "_controllerwaitreverserequest",
        "_controllercachedimagereverserequest",
    )
    return any(marker in text for marker in low_value_markers)


def _is_high_value_context_line(line: str) -> bool:
    text = line.lower()
    high_value_markers = (
        "error",
        "[err]",
        "warn",
        "wrn",
        "fail",
        "exception",
        "traceback",
        "recognition",
        "recognizer",
        "ocr",
        "expected",
        "filtered",
        "best_result",
        "node.",
        "action.",
        "task.",
        "entry=",
        "context_run_recognition",
        "run_recognition",
        "拒绝访问",
        "截图失败",
        "失败",
        "错误",
        "异常",
    )
    return any(marker in text for marker in high_value_markers)


def _build_stream_section(
    name: str,
    sections: list[dict[str, Any]],
    signature_counts: dict[str, int],
    tail_lines: deque[str],
    recognition_evidence: list[str],
    head_lines: list[str],
    opts: DigestOptions,
) -> str:
    if not sections:
        if not tail_lines:
            return ""
        tail = "\n".join(_compress_context_lines(list(tail_lines), opts))
        parts = [f"### {name}"]
        _append_log_head_block(parts, head_lines)
        parts.extend(["未找到明显错误关键词，保留末尾日志：", tail])
        return limit_text("\n".join(parts), opts.max_log_chars)

    evidence_lines = _limit_recognition_evidence_chars(recognition_evidence, opts)
    parts = [f"### {name}", "流式扫描结果：重复错误已按签名折叠，仅保留最新上下文。"]
    _append_log_head_block(parts, head_lines)
    if evidence_lines:
        parts.append("识别/OCR 重点证据（保留最新去重项）：")
        parts.extend(evidence_lines)
    parts.append("普通错误片段（保留最新去重项）：")
    for index, section in enumerate(sections, 1):
        formatted = _format_context_section(
            section["lines"],
            int(section.get("hit_line", section.get("end_line", 0))),
            opts,
        )
        if not formatted:
            continue
        start_line, end_line, section_lines = formatted
        repeat = signature_counts.get(section["signature"], 1)
        repeat_text = f"，同类重复 {repeat} 次" if repeat > 1 else ""
        parts.append(
            f"-- 片段 {index}, 行 {start_line}-{end_line}{repeat_text} --"
        )
        parts.extend(section_lines)
    return limit_text("\n".join(parts), opts.max_log_chars)


def _select_config_infos(infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    wanted = []
    for info in infos:
        name = info.filename.replace("\\", "/").lower()
        if name in {"config/maa_option.json", "config/pip_config.json"}:
            wanted.append(info)
        elif name.startswith("config/") and name.endswith(".json"):
            wanted.append(info)
    return sorted(wanted, key=lambda item: item.filename.lower())[:8]


def _select_error_images(infos: list[zipfile.ZipInfo], limit: int) -> list[str]:
    images = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        suffix = PurePosixPath(name).suffix.lower()
        if name.lower().startswith("on_error/") and suffix in IMAGE_SUFFIXES:
            images.append(name)
    return sorted(images, reverse=True)[:limit]


def _read_text_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_bytes: int,
) -> tuple[str, bool]:
    with archive.open(info, "r") as file:
        data = file.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return _decode_bytes(data), truncated


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_time_range(text: str) -> str | None:
    matches = TIMESTAMP_PATTERN.findall(text)
    if not matches:
        return None
    return f"{matches[0]} ~ {matches[-1]}"


def _summarize_keywords(name: str, text: str) -> str:
    counts = []
    for label, pattern in KEYWORD_PATTERNS:
        count = len(pattern.findall(text))
        if count:
            counts.append(f"{label}={count}")
    if not counts:
        return ""
    return f"- {name}: " + ", ".join(counts)


def _extract_error_context(name: str, text: str, opts: DigestOptions) -> str:
    lines = text.splitlines()
    matches = [idx for idx, line in enumerate(lines) if ERROR_PATTERN.search(line)]
    recognition_evidence = _extract_recognition_evidence_from_lines(lines, opts)
    evidence_lines = _limit_recognition_evidence_chars(recognition_evidence, opts)
    head_lines = _extract_log_head_lines(lines, opts)
    if not matches:
        should_show_tail = opts.log_head_lines <= 0 or len(lines) > opts.log_head_lines
        tail = "\n".join(_compress_context_lines(lines[-80:], opts)) if should_show_tail else ""
        if not tail.strip() and not head_lines:
            return ""
        parts = [f"### {name}"]
        _append_log_head_block(parts, head_lines)
        if evidence_lines:
            parts.append("识别/OCR 重点证据（保留最新去重项）：")
            parts.extend(evidence_lines)
        if tail.strip():
            parts.extend(["未找到明显错误关键词，保留末尾日志：", tail])
        return limit_text("\n".join(parts), opts.max_log_chars)

    latest_match_by_signature: dict[str, int] = {}
    for idx in matches:
        signature = _make_error_signature(lines[idx])
        latest_match_by_signature.pop(signature, None)
        latest_match_by_signature[signature] = idx
        while len(latest_match_by_signature) > opts.max_sections_per_log:
            latest_match_by_signature.pop(next(iter(latest_match_by_signature)), None)

    sections = []
    for number, idx in enumerate(sorted(latest_match_by_signature.values()), 1):
        start = max(0, idx - _context_raw_scan_limit(opts.context_before_lines))
        end = min(len(lines), idx + _context_raw_scan_limit(opts.context_after_lines) + 1)
        items = [(line_index + 1, lines[line_index]) for line_index in range(start, end)]
        formatted = _format_context_section(items, idx + 1, opts)
        if not formatted:
            continue
        start_line, end_line, section_lines = formatted
        sections.append(f"-- 片段 {number}, 行 {start_line}-{end_line} --")
        sections.extend(section_lines)

    parts = [f"### {name}"]
    _append_log_head_block(parts, head_lines)
    if evidence_lines:
        parts.append("识别/OCR 重点证据（保留最新去重项）：")
        parts.extend(evidence_lines)
    parts.append("普通错误片段（保留最新去重项）：")
    parts.extend(sections)
    return limit_text("\n".join(parts), opts.max_log_chars)


def _extract_recognition_evidence_from_lines(lines: list[str], opts: DigestOptions) -> list[str]:
    evidence: dict[str, str] = {}
    for index, line in enumerate(lines, 1):
        _append_recognition_evidence(line, index, evidence, opts)
    return list(evidence.values())


def _extract_log_head_lines(lines: list[str], opts: DigestOptions) -> list[str]:
    if opts.log_head_lines <= 0:
        return []
    return [
        f"[行 {index}] {_compact_line(line, limit=900)}"
        for index, line in enumerate(lines[: opts.log_head_lines], 1)
    ]


def _append_log_head_block(parts: list[str], head_lines: list[str]) -> None:
    if not head_lines:
        return
    parts.append(f"日志开头（前 {len(head_lines)} 行）：")
    parts.extend(head_lines)


def _limit_recognition_evidence_chars(evidence: list[str], opts: DigestOptions) -> list[str]:
    char_limit = max(0, int(opts.max_recognition_evidence_chars))
    if not evidence or char_limit <= 0:
        return evidence

    selected: list[str] = []
    used = 0
    omitted = 0
    for line in reversed(evidence):
        newline_cost = 1 if selected else 0
        candidate = line
        needed = len(candidate) + newline_cost
        if used + needed <= char_limit:
            selected.append(candidate)
            used += needed
            continue

        remaining = char_limit - used - newline_cost
        if not selected and remaining >= 160:
            selected.append(_compact_line(candidate, limit=remaining))
            used = char_limit
        omitted += 1

    selected.reverse()
    if omitted:
        selected.insert(0, f"[已省略 {omitted} 条更早的 OCR 重点证据；受 max_recognition_evidence_chars 限制]")
    return selected


def _summarize_config(name: str, text: str, max_chars: int) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"### {name}\n配置不是合法 JSON，原文片段：\n{limit_text(text, max_chars)}"

    # mxu-<项目名>.json（如 mxu-MaaXXX.json / mxu-MaaAssistantKedrgame.json）
    # 结构一致，统一使用结构化摘要，避免退化为冗长的原始 JSON
    normalized = name.replace("\\", "/").lower()
    base_name = PurePosixPath(normalized).name
    if base_name.startswith("mxu-") and base_name.endswith(".json"):
        summary = _summarize_mxu_config(data)
        if summary:
            return f"### {name}\n{limit_text(summary, max_chars)}"

    if normalized.endswith("maa_option.json"):
        return f"### {name}\n{limit_text(json.dumps(data, ensure_ascii=False, indent=2), max_chars)}"

    return f"### {name}\n{limit_text(json.dumps(data, ensure_ascii=False, indent=2), max_chars)}"


def _summarize_mxu_config(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    lines = [f"version: {data.get('version', '未知')}"]
    instances = data.get("instances")
    if not isinstance(instances, list):
        return "\n".join(lines)

    for index, instance in enumerate(instances, 1):
        if not isinstance(instance, dict):
            continue
        lines.append(
            f"- 实例 {index}: name={instance.get('name', '未知')}, "
            f"controller={instance.get('controllerName', '未知')}, "
            f"resource={instance.get('resourceName', '未知')}"
        )
        tasks = instance.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_name = task.get("taskName", "未知任务")
            enabled = task.get("enabled", "未知")
            lines.append(f"  - task={task_name}, enabled={enabled}")
            options = _flatten_task_options(task.get("optionValues"))
            if options:
                lines.append("    options: " + "; ".join(options[:30]))
    return "\n".join(lines)


def _flatten_task_options(option_values: Any) -> list[str]:
    if not isinstance(option_values, dict):
        return []
    result: list[str] = []
    for key, value in option_values.items():
        if not isinstance(value, dict):
            result.append(f"{key}={value}")
            continue
        if "value" in value:
            result.append(f"{key}={value.get('value')}")
        elif "caseName" in value:
            result.append(f"{key}={value.get('caseName')}")
        elif isinstance(value.get("values"), dict):
            pairs = ", ".join(f"{k}={v}" for k, v in value["values"].items())
            result.append(f"{key}({pairs})")
        else:
            result.append(f"{key}={value}")
    return result


def _build_prompt(
    *,
    original_file_name: str,
    group_id: str | int | None,
    project_name: str = "MaaXXX",
    file_count: int,
    total_uncompressed_size: int,
    log_files: list[str],
    config_files: list[str],
    error_images: list[str],
    time_ranges: list[str],
    keyword_summaries: list[str],
    config_sections: list[str],
    log_sections: list[str],
    notes: list[str],
    max_prompt_chars: int,
) -> str:
    parts = [
        f"下面是 {project_name or 'MaaXXX'} 日志包的自动摘要，请只根据这些内容分析本次报错原因。",
        "",
        f"文件名：{original_file_name}",
        f"上传群：{group_id or '未知'}",
        f"压缩包文件数：{file_count}",
        f"解压后总大小：{format_bytes(total_uncompressed_size)}",
        "",
        "处理备注：",
        "\n".join(f"- {note}" for note in notes) if notes else "- 无",
        "",
        "日志文件：",
        "\n".join(f"- {name}" for name in log_files) if log_files else "- 未找到 .log 文件",
        "",
        "日志时间范围：",
        "\n".join(f"- {item}" for item in time_ranges) if time_ranges else "- 未识别到时间范围",
        "",
        "错误截图文件：",
        "\n".join(f"- {name}" for name in error_images) if error_images else "- 未找到 on_error 图片",
        "",
        "关键词统计：",
        "\n".join(keyword_summaries) if keyword_summaries else "- 未统计到重点关键词",
        "",
        "配置摘要：",
        "\n\n".join(config_sections) if config_sections else "未找到配置文件。",
        "",
        "关键日志片段：",
        "\n\n".join(log_sections) if log_sections else "未提取到关键日志片段。",
    ]
    return limit_text(
        "\n".join(parts),
        max_prompt_chars,
        suffix="\n...[摘要文本达到 max_prompt_chars 上限，后续已省略；日志读取状态请以“处理备注”为准]",
    )
