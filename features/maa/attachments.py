"""
结果附件 — 由**模型自行决定**是否附带图片或文件。

模型可在报告的**任意位置**用指令声明要附带的附件，并**必须指定来源**：

    [附件@日志: on_error/on_error_20260101_120002.png]
    [附件@项目: assets/resource/image/Start2.png]
    [附件@日志: config/mxu-MaaXXX.json]
    [附件@项目: assets/resource/pipeline/PVP.json]

来源标记（`@日志` / `@项目`）与相对路径都是**必需**的：
  - 只从指定来源查找，避免同名文件命中非预期内容
  - 路径必须是**相对路径**，拒绝绝对路径与 `..` 上跳，防止越界读取

图片与文件统一使用「附件」指令，Bot 按扩展名决定呈现方式：
  - 图片（png/jpg/webp/bmp）→ 内联进报告（图片化报告）或 image 消息段
  - 其他文件               → 文件卡片 + 群文件上传（upload_group_file）

另有一种**只给模型看**的指令（不发送给用户）：

    [看图@日志: on_error/xxx.png]
    [看图@项目: assets/resource/image/Start2.png]

旧的 `[附图@...]` 已取消，仅作为普通文本被剥离，不再发送图片。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional

from features.maa.log_digest import (
    IMAGE_SUFFIXES,
    DigestResult,
    read_zip_member,
)
from features.maa.safety import describe_rejection, is_sensitive_path

logger = logging.getLogger("Maa.Attach")

# 附件指令：可出现在任意位置（行首、行内、行尾）
#   [附件@日志: a.png]   [附件@项目: b.json]   【文件@日志: c.txt】
#   图片与文件统一用「附件」，由扩展名自动区分。
#   兼容 [附件: 日志:a.png] 形式
_ATTACHMENT_DIRECTIVE = re.compile(
    r"[\[【]\s*"
    r"(?P<label>附件|文件|file)"
    r"(?:\s*[@＠]\s*(?P<src1>日志|压缩包|zip|log|项目|仓库|repo|project))?"
    r"\s*[:：]\s*"
    r"(?P<path>[^\]】\n]+?)\s*"
    r"[\]】]",
    re.IGNORECASE,
)

# 旧版 [附图@...] / [图片@...]：已取消，按普通文本剥离，不再发送图片
_LEGACY_DIRECTIVE = re.compile(
    r"[\[【]\s*(?:附图|图片|image)"
    r"(?:\s*[@＠]\s*[^\]】:：\s]+)?"
    r"\s*[:：][^\]】\n]*[\]】]",
    re.IGNORECASE,
)

# 看图指令：只给模型看，不发送给用户
_VISION_DIRECTIVE = re.compile(
    r"[\[【]\s*(?:看图|读图|vision)"
    r"(?:\s*[@＠]\s*(?P<src>日志|压缩包|zip|log|项目|仓库|repo|project))?"
    r"\s*[:：]\s*"
    r"(?P<path>[^\]】\n]+?)\s*"
    r"[\]】]",
    re.IGNORECASE,
)

# 来源别名 → 规范化来源
_SOURCE_ZIP = {"日志", "压缩包", "zip", "log"}
_SOURCE_REPO = {"项目", "仓库", "repo", "project"}

# 来源标识（用于展示与校验）
SOURCE_ZIP = "zip"
SOURCE_REPO = "repo"
SOURCE_NAMES = {SOURCE_ZIP: "日志包", SOURCE_REPO: "项目"}

# 单个非图片附件的默认上限（MB）
DEFAULT_MAX_FILE_MB = 20


@dataclass
class Attachment:
    """模型声明的一个附件。"""

    path: str          # 相对路径（已校验）
    kind: str          # image / file
    source: str        # zip / repo

    def key(self) -> str:
        return f"{self.source}:{self.path.replace(chr(92), '/').lower()}"


@dataclass
class OutSegment:
    """
    报告的一个输出片段（保持原始顺序）。

    kind:
      - text   文本内容（text）
      - image  图片（data 为字节，label 为来源说明）
      - file   其他文件（data 为字节，name 为文件名，label 为来源说明）
    """

    kind: str
    text: str = ""
    data: bytes = b""
    name: str = ""
    label: str = ""    # 相对路径（仅附件片段）
    source: str = ""   # zip / repo（仅附件片段有意义）
    origin: str = ""   # 人类可读的来源说明（日志包 xxx / 项目 yyy）


# ════════════════════════════════════════════════════════════════
# 解析
# ════════════════════════════════════════════════════════════════

def _normalize_source(raw: str) -> str:
    """来源别名 → 规范化来源（zip / repo）；无法识别返回空串。"""
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    if token in _SOURCE_ZIP:
        return SOURCE_ZIP
    if token in _SOURCE_REPO:
        return SOURCE_REPO
    return ""


def normalize_rel_path(raw: str) -> Optional[str]:
    """
    规范化相对路径；非法（绝对路径 / 上跳 / 空）返回 None。

    这是防越界的关键：只允许仓库或压缩包内的相对路径。

    注意：这里只做**路径形状**校验，不判断内容敏感性；
    凭据类文件由 `safety.is_sensitive_path()` 单独拦截。
    """
    text = str(raw or "").strip().strip("`\"'")
    if not text:
        return None
    # 统一分隔符，去掉开头的 ./ 与 /
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/"):
        return None
    # 驱动器盘符（C:/...）或 UNC 路径一律拒绝
    if re.match(r"^[A-Za-z]:", text):
        return None
    parts = PurePosixPath(text).parts
    if not parts:
        return None
    if any(part in ("..", "") for part in parts):
        return None
    return "/".join(parts)


def _split_source_and_path(label: str, src1: str, raw: str) -> tuple[str, str]:
    """
    解析来源与路径。

    支持两种写法：
      [附图@日志: a.png]      → 来源在 @ 后
      [附图: 日志:a.png]      → 来源在冒号后
    """
    source = _normalize_source(src1)
    text = str(raw or "").strip()

    if not source:
        # 尝试从路径前缀里识别来源
        match = re.match(r"^\s*([^:：/]+)\s*[:：]\s*(.+)$", text)
        if match:
            candidate = _normalize_source(match.group(1))
            if candidate:
                source = candidate
                text = match.group(2).strip()

    return source, text


def _kind_for(label: str, path: str) -> str:
    """按扩展名判定附件呈现方式（图片内联 / 其他文件卡片）。"""
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    return "image" if suffix in IMAGE_SUFFIXES else "file"


def _collect_directives(report: str, pattern: re.Pattern,
                        source_group: str) -> list[Attachment]:
    """解析某一类指令（附件 / 看图）为去重后的 Attachment 列表。"""
    if not report:
        return []
    result: list[Attachment] = []
    seen: set[str] = set()
    for match in pattern.finditer(report):
        label = match.group("label") if "label" in pattern.groupindex else ""
        source, raw_path = _split_source_and_path(
            label, match.group(source_group) or "", match.group("path")
        )
        for piece in re.split(r"[,，、;；]", raw_path):
            piece = piece.strip()
            if not piece:
                continue
            if not source:
                logger.info(f"[附件] 缺少来源标记，已忽略：{piece}")
                continue
            path = normalize_rel_path(piece)
            if path is None:
                logger.info(f"[附件] 路径非法（须为相对路径），已忽略：{piece}")
                continue
            if is_sensitive_path(path):
                logger.info(f"[附件] {describe_rejection(path)}")
                continue
            attachment = Attachment(
                path=path, kind=_kind_for(label, path), source=source
            )
            key = attachment.key()
            if key in seen:
                continue
            seen.add(key)
            result.append(attachment)
    return result


def parse_attachments(report: str) -> list[Attachment]:
    """
    解析全部附件指令，返回 Attachment 列表（去重，保持出现顺序）。

    缺少来源标记或路径非法的条目会被丢弃（并记日志）。
    """
    return _collect_directives(report, _ATTACHMENT_DIRECTIVE, "src1")


def parse_vision_requests(report: str) -> list[Attachment]:
    """解析 `[看图@...]`：只给模型看，不进入报告，也不发送给用户。"""
    return _collect_directives(report, _VISION_DIRECTIVE, "src")


def strip_vision_requests(report: Optional[str]) -> Optional[str]:
    """移除 `[看图@...]` 指令（不给用户看）。"""
    if not report:
        return report
    cleaned = _VISION_DIRECTIVE.sub("", report)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_attachments(report: Optional[str]) -> Optional[str]:
    """
    移除给用户看的报告中的附件指令。

    行内指令会被替换为空格（保留前后文字），随后清理多余空白。
    """
    if not report:
        return report
    cleaned = _ATTACHMENT_DIRECTIVE.sub("", report)
    cleaned = _LEGACY_DIRECTIVE.sub("", cleaned)
    cleaned = _VISION_DIRECTIVE.sub("", cleaned)
    # 行内指令移除后可能留下多余空格
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ════════════════════════════════════════════════════════════════
# 分段（保留附件在原文中的位置）
# ════════════════════════════════════════════════════════════════

def split_report(report: str) -> list[OutSegment]:
    """
    把报告按附件指令切分为有序片段。

    文本与附件交替出现，从而支持「文字 → 图 → 文字」的排版。
    """
    if not report:
        return []

    # 看图指令与旧版附图指令都不进入报告文本
    report = _LEGACY_DIRECTIVE.sub("", report)
    report = _VISION_DIRECTIVE.sub("", report)

    segments: list[OutSegment] = []
    cursor = 0
    for match in _ATTACHMENT_DIRECTIVE.finditer(report):
        before = report[cursor:match.start()]
        if before.strip():
            segments.append(OutSegment(kind="text", text=before))

        label = match.group("label")
        source, raw_path = _split_source_and_path(
            label, match.group("src1"), match.group("path")
        )
        # 一行可写多个附件，逐个展开
        for piece in re.split(r"[,，、;；]", raw_path):
            piece = piece.strip()
            if not piece or not source:
                continue
            path = normalize_rel_path(piece)
            if path is None:
                continue
            if is_sensitive_path(path):
                logger.info(f"[附件] {describe_rejection(path)}")
                continue
            segments.append(OutSegment(
                kind=_kind_for(label, path),
                name=PurePosixPath(path).name,
                label=path,
                source=source,
            ))
        cursor = match.end()

    tail = report[cursor:]
    if tail.strip():
        segments.append(OutSegment(kind="text", text=tail))

    return segments


# ════════════════════════════════════════════════════════════════
# 取内容
# ════════════════════════════════════════════════════════════════

def build_report_segments(
    report: str,
    digest: Optional[DigestResult],
    *,
    repo_files: Optional[dict[str, bytes]] = None,
    max_attachments: int = 4,
    max_image_bytes: int = 0,
    max_file_bytes: int = DEFAULT_MAX_FILE_MB * 1024 * 1024,
    allow_files: bool = True,
) -> tuple[list[OutSegment], list[str]]:
    """
    把报告切成片段，并为每个附件取出字节。

    repo_files: {仓库相对路径: 字节}，由调用方预先收集（支持任意扩展名）。
    max_image_bytes: `0` = 不限制（图片在渲染时按内容宽度等比缩放）。
    返回 (片段列表, 备注列表)。未声明附件时只返回文本片段。
    """
    segments = split_report(report)
    notes: list[str] = []
    missing: list[str] = []

    # 日志包索引：仅完整相对路径（不做文件名回退，避免误命中）
    zip_full: dict[str, str] = {}
    if digest and digest.zip_path:
        for name in (getattr(digest, "all_members", None) or digest.all_images):
            zip_full.setdefault(name.replace("\\", "/").lower(), name)

    # 仓库索引：仅完整相对路径
    repo_full: dict[str, tuple[str, bytes]] = {}
    for rel, data in (repo_files or {}).items():
        repo_full.setdefault(rel.replace("\\", "/").lower(), (rel, data))

    used: set[str] = set()
    sent = 0
    out: list[OutSegment] = []

    for seg in segments:
        if seg.kind == "text":
            out.append(seg)
            continue

        if seg.kind == "file" and not allow_files:
            notes.append(f"{seg.label} 是文件附件，当前已关闭文件发送")
            continue

        if max_attachments > 0 and sent >= max_attachments:
            notes.append(f"已达附件数上限 {max_attachments}，其余未发送")
            continue

        wanted = seg.label.replace("\\", "/").lower()
        limit = max_image_bytes if seg.kind == "image" else max_file_bytes
        where = SOURCE_NAMES.get(seg.source, seg.source or "未知来源")

        data: Optional[bytes] = None
        source = ""

        # 严格按模型声明的来源查找，不跨来源回退，避免命中同名非预期文件
        if seg.source == SOURCE_ZIP:
            member = zip_full.get(wanted)
            if member and digest and digest.zip_path:
                data = read_zip_member(digest.zip_path, member, max_bytes=limit)
                if data:
                    source = f"日志包 {member}"
        elif seg.source == SOURCE_REPO:
            hit = repo_full.get(wanted)
            if hit:
                rel, blob = hit
                # limit <= 0 表示不限制（图片在渲染时按内容宽度缩放）
                if limit <= 0 or len(blob) <= limit:
                    data = blob
                    source = f"项目 {rel}"

        if data is None:
            missing.append(f"{seg.label}（{where}）")
            logger.info(f"[附件] 未在{where}中找到：{seg.label}")
            continue

        if source.lower() in used:
            continue
        used.add(source.lower())
        sent += 1

        path = seg.label.replace("\\", "/")
        if seg.kind == "image":
            out.append(OutSegment(
                kind="image", data=data, label=path,
                source=seg.source, origin=source,
            ))
        else:
            out.append(OutSegment(
                kind="file", data=data,
                name=PurePosixPath(path).name,
                label=path, source=seg.source, origin=source,
            ))

    if missing:
        notes.append("以下附件未找到：" + "、".join(missing[:6]))

    logger.info(
        f"[附件] 解析 {len(segments)} 段，发送 "
        f"{sum(1 for s in out if s.kind != 'text')} 个附件，缺失 {len(missing)} 个"
    )
    return out, notes


def find_repo_attachment_names(report: str) -> list[str]:
    """从附件指令中提取需要在**项目仓库**里查找的相对路径。"""
    return [a.path for a in parse_attachments(report) if a.source == SOURCE_REPO]
