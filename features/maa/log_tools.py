"""
日志压缩包工具 — 供 AI 像读仓库一样**自主检索日志包内的文件**。

分析时 Bot 只把「摘要」交给模型，摘要必然有取舍；当模型需要核对
摘要之外的原始内容（例如某条完整堆栈、某个配置项的原文、某个截图是否
存在）时，可以用这里的工具直接读取日志包。

实现方式与 dsh 对齐：先把压缩包**安全解压到目录**，工具本身只面向
解压后的普通文件（行号窗口、单行宽度、读取窗口预算）。解压按需触发
（模型第一次调用工具时才做）；目录是否保留由调用方决定，`close()`
只释放内存，`discard()` / `release()` 负责删除。

暴露的操作:
  - list_files  列出某个目录下的文件
  - search      在文本文件中按正则搜索（流式扫描完整文件）
  - read_file   读取某个文本文件（支持行范围，`start_line` 可越过前缀）
  - list_images 列出图片

安全边界:
  - 路径必须是相对路径，拒绝绝对路径与任何 `..` 上跳组件
  - 解压阶段拒绝符号链接、凭据类文件，并限制总字节与成员数
  - 单次读取受行窗口、单行宽度、字节与字符上限约束
"""
from __future__ import annotations

import logging
import re
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

from core.concurrency import run_blocking
from core.tokens import clamp_tool_result_tokens, trim_to_tokens
from features.maa.archive_extract import (
    DEFAULT_TOTAL_LIMIT_MB,
    ExtractedArchive,
)
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

# 明显是二进制、不适合按文本读取的扩展名
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".bin", ".dat", ".mp3",
    ".mp4", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".pdf", ".db",
    ".db3", ".sqlite", ".sqlite3",
}


# 同时进行的日志包扫描/解压线程数上限（同一实例内部还会串行）
LOG_TOOL_THREADS = 4


class LogArchiveTools:
    """把日志压缩包解压到目录后执行 AI 请求的工具调用。"""

    def __init__(self, zip_path: str, *, max_file_kb: int = 512,
                 max_result_chars: int = 30000,
                 max_search_files: int = 2000,
                 max_result_tokens: int = 0,
                 max_total_mb: int = 0,
                 extract_dir: str = ""):
        self.zip_path = str(zip_path or "")
        self.max_read_bytes = max(64, int(max_file_kb or 512)) * 1024
        self.max_result_chars = max(2000, int(max_result_chars or 30000))
        self.max_search_files = max(50, int(max_search_files or 2000))
        self.max_result_tokens = clamp_tool_result_tokens(max_result_tokens)
        total_mb = int(max_total_mb or 0) or DEFAULT_TOTAL_LIMIT_MB
        # 解压后总大小由 `probe_archive` 统一把关（下载后、AI 之前），
        # 这里只负责把同一上限交给解压器复核，不再单独设单文件上限。
        # extract_dir 为空时退化为系统临时目录（用完即删）。
        # 同一实例的解压 / 读取串行：zipfile 句柄与解压目录是可变状态，
        # 而工具调用、关闭、归档可能在同一分析内交错发生。
        self._io_lock = threading.RLock()
        # 拿不到锁时暂存的释放动作：由正在跑的那次工具调用收尾（见 _run_io）
        self._pending_release = ""
        self._extract = ExtractedArchive(
            self.zip_path,
            target_dir=extract_dir,
            max_total_mb=total_mb,
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

    async def _run_io(self, func, **kwargs):
        """在线程里执行阻塞的压缩包操作，同一实例内串行。

        解压、逐行扫描、stat 都是同步 I/O；放在事件循环里会让**所有配置**
        一起卡住，所以统一下沉到线程。
        """
        def _call():
            with self._io_lock:
                try:
                    return func(**kwargs)
                finally:
                    # 期间被请求的释放在这里收尾（仍持锁，顺序正确）
                    self._flush_pending_release()

        return await run_blocking(_call, limit=LOG_TOOL_THREADS)

    # ── 释放 ──
    # 这三个入口以前直接 `with self._io_lock`：一旦在"阶段超时取消后的 finally"
    # 里执行、而此刻正有工具在别的线程持锁读文件，就会在**事件循环线程**上等
    # threading.Lock —— 整个 Bot 全部配置停摆。所以改成：拿不到锁就登记一个待
    # 释放动作，交给那次正在跑的工具调用收尾，自身立即返回。

    def close(self) -> None:
        """释放内存状态，不删除已解压目录（下次追问可直接复用）。"""
        self._request_release("close")

    def discard(self) -> None:
        """删除解压目录（没有历史记录、或本次分析未能归档时调用）。"""
        self._request_release("discard")

    def release(self) -> None:
        """结束使用：目录可复用时只释放内存，临时目录直接删掉。"""
        self._request_release("close" if self.persistent else "discard")

    def _request_release(self, action: str) -> None:
        """执行释放；拿不到锁就让正在跑的工具调用收尾（绝不阻塞调用方）。"""
        if self._io_lock.acquire(blocking=False):
            try:
                self._apply_release(action)
            finally:
                self._io_lock.release()
            return
        self._pending_release = action

    def _apply_release(self, action: str) -> None:
        if action == "close":
            self._extract.close()
        elif action == "discard":
            self._extract.discard()

    def _flush_pending_release(self) -> None:
        """在持锁的工具调用收尾时执行挂起的释放（调用方需已持锁）。"""
        action, self._pending_release = self._pending_release, ""
        if action:
            self._apply_release(action)

    @property
    def persistent(self) -> bool:
        """解压结果是否由调用方指定位置持久保存。"""
        return bool(self._extract.target_dir)

    @property
    def extract_dir(self) -> Optional[Path]:
        """解压目录（尚未解压时返回计划位置）。"""
        return self._extract.root or self._extract.target_dir

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
        if self._extract.overflow:
            return (f"日志包内容过多，工具不可用{detail}")
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
                         state: dict[str, Any],
                         *, truncate: bool = True,
                         ) -> Iterator[tuple[int, str]]:
        """
        逐块读取解压后的文件，产出 `(行号, 文本)`。

        内存占用与文件大小无关：每次只读 `_CHUNK_BYTES`，单行缓冲封顶
        `_MAX_LINE_BYTES`。`truncate` 控制超长行是否压到展示宽度：
        读取工具需要截断（结果可读），搜索工具不截断（否则行尾命中会漏）。

        `state` 回填 `complete`（是否读到文件末尾）；调用方提前停止读取时
        该值为 False。
        """
        state["complete"] = False
        line_no = 0
        buffer = bytearray()

        def take_line() -> tuple[int, str]:
            nonlocal line_no
            text = bytes(buffer).decode("utf-8", "ignore").rstrip("\r")
            buffer.clear()
            line_no += 1
            if truncate:
                text = truncate_line(text)
            return line_no, text

        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_CHUNK_BYTES)
                    if not chunk:
                        state["complete"] = True
                        break
                    start = 0
                    while True:
                        newline = chunk.find(b"\n", start)
                        if newline < 0:
                            self._append_line_bytes(buffer, chunk[start:])
                            break
                        self._append_line_bytes(buffer, chunk[start:newline])
                        yield take_line()
                        start = newline + 1
                if buffer:
                    yield take_line()
        except OSError:
            # 读失败：交出已读到的部分，`complete` 保持 False
            if buffer:
                yield take_line()

    # ════════════════════════════════════════════════════════════
    # 工具实现
    # ════════════════════════════════════════════════════════════

    async def list_files(self, path: str = "") -> str:
        """列出某个目录下的文件（阻塞扫描在线程里跑）。"""
        return await self._run_io(self._list_files_sync, path=path)

    def _list_files_sync(self, path: str = "") -> str:
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
        """在日志包的文本文件中按正则搜索（阻塞扫描在线程里跑）。"""
        return await self._run_io(
            self._search_sync, pattern=pattern, path=path,
            max_results=max_results,
        )

    def _search_sync(self, pattern: str, path: str = "",
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
            # 完整流式扫描：不按读取窗口截断，匹配也用未截断的行，
            # 否则文件后半段/超长行尾部的命中会被静默漏掉。
            iterator = self._iter_file_lines(root / member, state, truncate=False)
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
            if truncated:
                break

        if not hits:
            return (f"未在日志包中匹配到 {pattern_text!r}"
                    f"（已扫描 {scanned} 个文件"
                    + ("，已达文件数上限" if truncated else "") + "）")
        text = (f"日志包中匹配 {pattern_text!r} 的结果（{len(hits)} 条"
                + ("，已达上限" if truncated else "")
                + f"，扫描 {scanned} 个文件）：\n")
        text += "\n".join(hits)
        if truncated:
            text += "\n...[结果已截断，可用更精确的 pattern 或限定 path]"
        return self._finish(text)

    async def read_file(self, path: str, start_line: Optional[int] = None,
                        end_line: Optional[int] = None) -> str:
        """读取日志包内某个文本文件（支持行范围；阻塞读取在线程里跑）。"""
        return await self._run_io(
            self._read_file_sync, path=path,
            start_line=start_line, end_line=end_line,
        )

    def _read_file_sync(self, path: str, start_line: Optional[int] = None,
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
        limit = int(self.max_read_bytes or 0)
        limit_kb = limit // 1024
        budget = self.max_result_chars
        state: dict[str, Any] = {}
        rendered: list[str] = []
        used_chars = 0
        window_bytes = 0
        seen = 0
        last = start - 1
        cut_by_budget = False
        cut_by_bytes = False
        iterator = self._iter_file_lines(target, state)
        try:
            for lineno, text in iterator:
                seen = lineno
                if lineno < start:
                    # 跳到 start_line 之前读到的行不计入返回窗口预算
                    continue
                if requested_end is not None and lineno > requested_end:
                    break
                line = f"[行 {lineno}] {text}"
                line_bytes = len(line.encode("utf-8")) + 1
                # 字符预算与字节预算都只作用于 start_line 之后的返回窗口
                if used_chars + len(line) + 1 > budget:
                    cut_by_budget = True
                    break
                if limit > 0 and window_bytes + line_bytes > limit:
                    cut_by_bytes = True
                    break
                rendered.append(line)
                used_chars += len(line) + 1
                window_bytes += line_bytes
                last = lineno
        finally:
            iterator.close()

        if seen == 0:
            return f"日志包 {rel} 没有可读取的文本内容（{size} 字节）"
        if last < start:
            if cut_by_budget or cut_by_bytes:
                return (f"start_line={start} 处单行超过单次返回上限"
                        f"（{budget} 字符 / {limit_kb} KB），无法显示")
            if state.get("complete"):
                return (f"start_line={start} 超出范围：日志包 {rel} "
                        f"共 {seen} 行")
            return (f"start_line={start} 之前读取失败：日志包 {rel} "
                    f"只读到第 {seen} 行")

        if state.get("complete") and not (cut_by_budget or cut_by_bytes):
            total_detail = f"共 {seen} 行"
        elif cut_by_bytes:
            total_detail = f"已读取到第 {last} 行（本次窗口 {limit_kb} KB）"
        elif cut_by_budget:
            total_detail = f"已读取到第 {last} 行"
        else:
            total_detail = f"至少 {seen} 行"
        header = (f"日志包 {rel}（{total_detail}，{size} 字节）"
                  f" 显示 行 {start}-{last}：\n")
        body = "\n".join(rendered)
        if cut_by_budget:
            body += f"\n...[受结果长度限制，可用 start_line={last + 1} 继续读]"
        elif cut_by_bytes:
            body += (f"\n...[本次已读取满 {limit_kb} KB，可用 "
                     f"start_line={last + 1} 继续读]")
        elif (requested_end is not None and last >= requested_end
              and not state.get("complete")):
            body += f"\n...[已到 end_line，可用 start_line={last + 1} 继续读]"
        return self._finish(header + body)

    async def list_images(self) -> str:
        """列出日志包内的全部图片（阻塞扫描在线程里跑）。"""
        return await self._run_io(self._list_images_sync)

    def _list_images_sync(self) -> str:
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
