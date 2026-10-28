"""
日志压缩包工具 — 供 AI 像读仓库一样**自主检索日志包内的文件**。

分析时 Bot 只把「摘要」交给模型，摘要必然有取舍；当模型需要核对
摘要之外的原始内容（例如某条完整堆栈、某个配置项的原文、某个截图是否
存在）时，可以用这里的工具直接读取日志包。

实现方式与 dsh 对齐：先把压缩包**安全解压到临时目录**，工具本身只面向
解压后的普通文件（行号窗口、单行宽度、字节上限与仓库工具同一套规则）。
解压按需触发（模型第一次调用工具时才做），`close()` 时删除临时目录。

暴露的操作:
  - list_files  列出某个目录下的文件
  - search      在文本文件中按正则搜索
  - read_file   读取某个文本文件（支持行范围）
  - list_images 列出图片

安全边界:
  - 路径必须是相对路径，拒绝绝对路径与任何 `..` 上跳组件
  - 解压阶段拒绝符号链接、凭据类文件，并限制单文件/总字节/成员数
  - 单次读取受字节、行数与字符上限约束
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

from core.tokens import clamp_tool_result_tokens, trim_to_tokens
from features.maa.archive_extract import ExtractedArchive
from features.maa.log_digest import IMAGE_SUFFIXES
from features.maa.safety import (
    describe_rejection,
    is_sensitive_path,
    normalize_tool_path,
)
from features.maa.text_utils import MAX_LINE_CHARS, truncate_line

logger = logging.getLogger("Maa.LogTools")

# 逐块读取的块大小：内存占用与文件大小无关
_CHUNK_BYTES = 64 * 1024
# 单行缓冲上限（字节）。超过即视为超长行：丢弃多余字节并在输出时标注，
# 避免“整条日志没有换行”把内存吃满。
_MAX_LINE_BYTES = MAX_LINE_CHARS * 4
# 解压默认总上限（MB），与 settings.max_total_uncompressed_mb 默认值一致
_DEFAULT_TOTAL_MB = 120

# 明显是二进制、不适合按文本读取的扩展名
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".bin", ".dat", ".mp3",
    ".mp4", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".pdf", ".db",
    ".db3", ".sqlite", ".sqlite3",
}


class LogArchiveTools:
    """把日志压缩包解压到临时目录后执行 AI 请求的工具调用。"""

    def __init__(self, zip_path: str, *, max_file_kb: int = 512,
                 max_result_chars: int = 30000,
                 max_search_files: int = 2000,
                 max_result_tokens: int = 0,
                 max_total_mb: int = 0):
        self.zip_path = str(zip_path or "")
        self.max_read_bytes = max(64, int(max_file_kb or 512)) * 1024
        self.max_result_chars = max(2000, int(max_result_chars or 30000))
        self.max_search_files = max(50, int(max_search_files or 2000))
        self.max_result_tokens = clamp_tool_result_tokens(max_result_tokens)
        total_mb = int(max_total_mb or 0) or _DEFAULT_TOTAL_MB
        # 单文件解压上限取读取窗口的 4 倍：一次 read 最多消费一个窗口，
        # 留出 start_line 深入阅读的余量，同时避免单个成员吃掉全部磁盘配额。
        self._extract = ExtractedArchive(
            self.zip_path,
            max_total_bytes=total_mb * 1024 * 1024,
            max_file_bytes=max(1024 * 1024, self.max_read_bytes * 4),
            max_members=3000,
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    def available(self) -> bool:
        """压缩包是否可作为日志源（存在且是合法 zip；不触发解压）。"""
        if not self.zip_path:
            return False
        try:
            return zipfile.is_zipfile(self.zip_path)
        except OSError:
            return False

    def close(self) -> None:
        """删除解压出来的临时目录（分析/追问结束时调用）。"""
        self._extract.close()

    def _root(self) -> Optional[Path]:
        root = self._extract.ensure()
        if root is None:
            notes = [note for note in self._extract.notes if note]
            logger.info(
                "[日志工具] 解压不可用：" + ("；".join(notes[:3]) or "未知原因")
            )
        return root

    def _unavailable(self) -> str:
        notes = [note for note in self._extract.notes if note]
        detail = f"（{'；'.join(notes[:3])}）" if notes else ""
        return f"日志包不可用（可能已被清理或无法解压）{detail}"

    # ════════════════════════════════════════════════════════════
    # 内部工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize(rel: str) -> Optional[str]:
        """规范化包内相对路径；非法返回 None。"""
        return normalize_tool_path(rel)

    @staticmethod
    def _is_text_file(name: str) -> bool:
        suffix = PurePosixPath(name).suffix.lower()
        return suffix not in _BINARY_SUFFIXES

    def _finish(self, text: str) -> str:
        """按 token 预算裁剪工具结果（0 = 不限）。"""
        if self.max_result_tokens <= 0:
            return text
        return trim_to_tokens(text, self.max_result_tokens)

    @staticmethod
    def _append_line_bytes(buffer: bytearray, piece: bytes) -> None:
        """把片段追加到行缓冲；超过单行上限时丢弃多余部分。"""
        if not piece:
            return
        room = _MAX_LINE_BYTES - len(buffer)
        if room <= 0:
            return
        buffer += piece[:room]

    def _iter_file_lines(self, path: Path,
                         state: dict[str, Any]) -> Iterator[tuple[int, str]]:
        """
        逐块读取解压后的文件，产出 `(行号, 文本)`。

        内存占用与文件大小无关：每次只读 `_CHUNK_BYTES`、单行缓冲封顶、
        累计读取字节受 `max_read_bytes` 限制。

        `state` 回填：`truncated`（因字节上限提前停止）、
        `complete`（是否读到文件末尾）。
        """
        limit = int(self.max_read_bytes or 0)
        state["truncated"] = False
        state["complete"] = False
        used = 0
        line_no = 0
        buffer = bytearray()

        def take_line() -> tuple[int, str]:
            nonlocal line_no
            text = truncate_line(
                bytes(buffer).decode("utf-8", "ignore").rstrip("\r")
            )
            buffer.clear()
            line_no += 1
            return line_no, text

        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_CHUNK_BYTES)
                    if not chunk:
                        state["complete"] = True
                        break
                    hit_limit = False
                    if limit > 0:
                        room = limit - used
                        if room <= 0:
                            state["truncated"] = True
                            break
                        if len(chunk) > room:
                            chunk = chunk[:room]
                            hit_limit = True
                    used += len(chunk)
                    start = 0
                    while True:
                        newline = chunk.find(b"\n", start)
                        if newline < 0:
                            self._append_line_bytes(buffer, chunk[start:])
                            break
                        self._append_line_bytes(buffer, chunk[start:newline])
                        yield take_line()
                        start = newline + 1
                    if hit_limit:
                        state["truncated"] = True
                        break
                if buffer:
                    yield take_line()
        except OSError:
            # 读失败：交出已读到的部分并标记不完整
            state["truncated"] = True
            if buffer:
                yield take_line()

    # ════════════════════════════════════════════════════════════
    # 工具实现
    # ════════════════════════════════════════════════════════════

    async def list_files(self, path: str = "") -> str:
        """列出某个目录下的文件。"""
        rel = self._normalize(path)
        if rel is None:
            return f"路径非法（须为包内相对路径）：{path}"

        root = self._root()
        if root is None:
            return self._unavailable()
        prefix = f"{rel}/" if rel else ""
        files: list[str] = []
        dirs: set[str] = set()
        for member in self._extract.members:
            if prefix and not member.startswith(prefix):
                continue
            rest = member[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0] + "/")
                continue
            try:
                size = (root / member).stat().st_size
            except OSError:
                continue
            files.append(f"{rest}  ({size} 字节)")

        if not files and not dirs:
            where = rel or "压缩包根目录"
            return f"{where} 下没有文件"

        lines = [f"日志包 {rel or '/'} 下的内容（{len(dirs) + len(files)} 项）："]
        lines.extend(sorted(dirs))
        lines.extend(sorted(files))
        return self._finish("\n".join(lines[:300]))

    async def search(self, pattern: str, path: str = "",
                     max_results: int = 60) -> str:
        """在日志包的文本文件中按正则搜索。"""
        pattern_text = str(pattern or "").strip()
        if not pattern_text:
            return "缺少 pattern 参数"
        try:
            regex = re.compile(pattern_text)
        except re.error as exc:
            return f"正则表达式无效：{exc}"

        rel = self._normalize(path)
        if rel is None:
            return f"路径非法（须为包内相对路径）：{path}"

        try:
            limit = int(max_results or 60)
        except (TypeError, ValueError):
            limit = 60
        limit = max(1, min(200, limit))

        root = self._root()
        if root is None:
            return self._unavailable()

        hits: list[str] = []
        scanned = 0
        truncated = False
        capped: list[str] = []
        prefix = f"{rel}/" if rel else ""
        for member in self._extract.members:
            if prefix and not member.startswith(prefix):
                continue
            if not self._is_text_file(member):
                continue
            if scanned >= self.max_search_files:
                truncated = True
                break

            scanned += 1
            state: dict[str, Any] = {}
            iterator = self._iter_file_lines(root / member, state)
            try:
                for lineno, line in iterator:
                    if not regex.search(line):
                        continue
                    hits.append(f"{member}:{lineno}: {line.strip()[:300]}")
                    if len(hits) >= limit:
                        truncated = True
                        break
            finally:
                iterator.close()
            if state.get("truncated"):
                capped.append(member)
            if truncated:
                break

        if not hits:
            return (f"未在日志包中匹配到 {pattern_text!r}"
                    f"（已扫描 {scanned} 个文件）")
        text = (f"日志包中匹配 {pattern_text!r} 的结果（{len(hits)} 条"
                + ("，已达上限" if truncated else "")
                + f"，扫描 {scanned} 个文件）：\n")
        text += "\n".join(hits)
        if capped:
            text += (f"\n...[{len(capped)} 个文件超过单文件读取上限 "
                     f"{self.max_read_bytes // 1024} KB，只扫描了前 "
                     f"{self.max_read_bytes // 1024} KB："
                     + "、".join(capped[:3])
                     + ("…" if len(capped) > 3 else "") + "]")
        if truncated:
            text += "\n...[结果已截断，可用更精确的 pattern 或限定 path]"
        return self._finish(text)

    async def read_file(self, path: str, start_line: Optional[int] = None,
                        end_line: Optional[int] = None) -> str:
        """读取日志包内某个文本文件（支持行范围）。"""
        rel = self._normalize(path)
        if rel is None:
            return f"路径非法（须为包内相对路径）：{path}"
        if not rel:
            return "缺少 path 参数"

        root = self._root()
        if root is None:
            return self._unavailable()
        if is_sensitive_path(rel):
            return describe_rejection(rel)
        target = self._extract.file_path(rel)
        if target is None or not target.is_file():
            return f"日志包中不存在该文件：{rel}"
        if not self._is_text_file(rel):
            return f"这是二进制文件，无法按文本读取：{rel}"

        def _as_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        start = max(1, _as_int(start_line) or 1)
        requested_end = _as_int(end_line)
        if requested_end is not None:
            requested_end = max(start, requested_end)

        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        limit_kb = self.max_read_bytes // 1024
        budget = self.max_result_chars
        state: dict[str, Any] = {}
        rendered: list[str] = []
        used = 0
        seen = 0
        last = start - 1
        cut_by_budget = False
        iterator = self._iter_file_lines(target, state)
        try:
            for lineno, text in iterator:
                seen = lineno
                if lineno < start:
                    continue
                if requested_end is not None and lineno > requested_end:
                    break
                line = f"[行 {lineno}] {text}"
                if used + len(line) + 1 > budget:
                    cut_by_budget = True
                    break
                rendered.append(line)
                used += len(line) + 1
                last = lineno
        finally:
            iterator.close()

        if seen == 0:
            return f"日志包 {rel} 没有可读取的文本内容（{size} 字节）"
        if last < start:
            if state.get("complete"):
                return (f"start_line={start} 超出范围：日志包 {rel} "
                        f"共 {seen} 行")
            return (f"start_line={start} 超出已读取范围：日志包 {rel} 已读取 "
                    f"{seen} 行（仅前 {limit_kb} KB）")

        if state.get("complete"):
            total_detail = f"共 {seen} 行"
        elif state.get("truncated"):
            total_detail = f"已读取 {seen} 行（仅前 {limit_kb} KB）"
        else:
            total_detail = f"至少 {seen} 行"
        header = (f"日志包 {rel}（{total_detail}，{size} 字节）"
                  f" 显示 行 {start}-{last}：\n")
        body = "\n".join(rendered)
        if cut_by_budget:
            body += f"\n...[受结果长度限制，可用 start_line={last + 1} 继续读]"
        elif state.get("truncated"):
            body += f"\n...[仅读取前 {limit_kb} KB，其后内容未读取]"
        elif requested_end is not None and last >= requested_end:
            body += f"\n...[已到 end_line，可用 start_line={last + 1} 继续读]"
        return self._finish(header + body)

    async def list_images(self) -> str:
        """列出日志包内的全部图片（便于模型判断能附哪些图）。"""
        root = self._root()
        if root is None:
            return self._unavailable()
        images: list[str] = []
        for member in self._extract.members:
            if PurePosixPath(member).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                size = (root / member).stat().st_size
            except OSError:
                continue
            images.append(f"{member}  ({size} 字节)")
        if not images:
            return "日志包内没有图片"
        return self._finish(
            "日志包内的图片（可用 [附件@日志: 路径] 发送）：\n"
            + "\n".join(sorted(images))
        )
