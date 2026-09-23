"""
日志压缩包工具 — 供 AI 像读仓库一样**自主检索日志包内的文件**。

分析时 Bot 只把「摘要」交给模型，摘要必然有取舍；当模型需要核对
摘要之外的原始内容（例如某条完整堆栈、某个配置项的原文、某个截图是否
存在）时，可以用这里的工具直接读取压缩包。

暴露的操作:
  - list_files  列出压缩包内某目录的文件
  - search      在压缩包的文本成员中按正则搜索
  - read_file   读取某个文本成员（支持行范围）

安全边界:
  - 路径必须是包内相对路径，拒绝绝对路径与 `..` 上跳
  - 拒绝凭据 / 密钥类文件名（与仓库侧共用 safety 策略）
  - 单文件读取受字节上限、单次返回受字符上限约束
  - 只读，不落盘（不把压缩包解压到磁盘）
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any, Optional

from features.maa.log_digest import IMAGE_SUFFIXES
from features.maa.safety import describe_rejection, is_sensitive_path

logger = logging.getLogger("Maa.LogTools")

# 明显是二进制、不适合按文本读取的扩展名
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".bin", ".dat", ".mp3",
    ".mp4", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".pdf", ".db",
    ".db3", ".sqlite", ".sqlite3",
}

# 日志包内可直接按文本读取的扩展名（其余需显式尝试）
_TEXT_SUFFIXES = {
    ".log", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".csv", ".md", ".xml", ".js", ".ts", ".py", ".bat",
    ".sh", ".ps1", ".env", ".properties",
}


class LogArchiveTools:
    """在日志压缩包内执行 AI 请求的工具调用（只读、不落盘）。"""

    def __init__(self, zip_path: str, *, max_file_kb: int = 512,
                 max_result_chars: int = 30000,
                 max_search_files: int = 2000):
        self.zip_path = str(zip_path or "")
        self.max_read_bytes = max(64, int(max_file_kb or 512)) * 1024
        self.max_result_chars = max(2000, int(max_result_chars or 30000))
        self.max_search_files = max(50, int(max_search_files or 2000))

    # ════════════════════════════════════════════════════════════
    # 内部工具
    # ════════════════════════════════════════════════════════════

    def available(self) -> bool:
        """压缩包是否可读（存在且是合法 zip）。"""
        if not self.zip_path:
            return False
        try:
            with zipfile.ZipFile(self.zip_path):
                return True
        except (zipfile.BadZipFile, OSError):
            return False

    @staticmethod
    def _normalize(rel: str) -> Optional[str]:
        """规范化包内相对路径；非法返回 None。"""
        text = str(rel or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        text = text.strip("/")
        if not text:
            return ""
        parts = PurePosixPath(text).parts
        if any(part in ("..", "") for part in parts):
            return None
        return "/".join(parts)

    @staticmethod
    def _is_text_member(name: str) -> bool:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in _BINARY_SUFFIXES:
            return False
        return True

    def _open(self) -> Optional[zipfile.ZipFile]:
        try:
            return zipfile.ZipFile(self.zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning(f"[日志工具] 无法打开压缩包：{exc}")
            return None

    @staticmethod
    def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        return [i for i in archive.infolist() if not i.is_dir()]

    # ════════════════════════════════════════════════════════════
    # 工具实现
    # ════════════════════════════════════════════════════════════

    async def list_files(self, path: str = "") -> str:
        """列出压缩包内某个目录下的文件。"""
        rel = self._normalize(path)
        if rel is None:
            return f"路径非法（须为包内相对路径）：{path}"

        archive = self._open()
        if archive is None:
            return "日志包不可用（可能已被清理）"
        try:
            prefix = f"{rel}/" if rel else ""
            files: list[str] = []
            dirs: set[str] = set()
            for info in self._members(archive):
                name = info.filename.replace("\\", "/")
                if prefix and not name.startswith(prefix):
                    continue
                rest = name[len(prefix):]
                if not rest:
                    continue
                if "/" in rest:
                    dirs.add(rest.split("/", 1)[0] + "/")
                else:
                    files.append(f"{rest}  ({info.file_size} 字节)")
        finally:
            archive.close()

        if not files and not dirs:
            where = rel or "压缩包根目录"
            return f"{where} 下没有文件"

        lines = [f"日志包 {rel or '/'} 下的内容（{len(dirs) + len(files)} 项）："]
        lines.extend(sorted(dirs))
        lines.extend(sorted(files))
        return "\n".join(lines[:300])

    async def search(self, pattern: str, path: str = "",
                     max_results: int = 60) -> str:
        """在压缩包的文本成员中按正则搜索。"""
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

        archive = self._open()
        if archive is None:
            return "日志包不可用（可能已被清理）"

        hits: list[str] = []
        scanned = 0
        truncated = False
        try:
            prefix = f"{rel}/" if rel else ""
            for info in self._members(archive):
                name = info.filename.replace("\\", "/")
                if prefix and not name.startswith(prefix):
                    continue
                if not self._is_text_member(name):
                    continue
                if is_sensitive_path(name):
                    continue
                if scanned >= self.max_search_files:
                    truncated = True
                    break

                scanned += 1
                try:
                    with archive.open(info) as handle:
                        for lineno, raw in enumerate(handle, 1):
                            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                            if not regex.search(line):
                                continue
                            hits.append(f"{name}:{lineno}: {line.strip()[:300]}")
                            if len(hits) >= limit:
                                truncated = True
                                break
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    continue
                if truncated:
                    break
        finally:
            archive.close()

        if not hits:
            return (f"未在日志包中匹配到 {pattern_text!r}"
                    f"（已扫描 {scanned} 个文件）")
        text = (f"日志包中匹配 {pattern_text!r} 的结果（{len(hits)} 条"
                + ("，已达上限" if truncated else "")
                + f"，扫描 {scanned} 个文件）：\n")
        text += "\n".join(hits)
        if truncated:
            text += "\n...[结果已截断，可用更精确的 pattern 或限定 path]"
        return text

    async def read_file(self, path: str, start_line: Optional[int] = None,
                        end_line: Optional[int] = None) -> str:
        """读取压缩包内某个文本成员（支持行范围）。"""
        rel = self._normalize(path)
        if rel is None:
            return f"路径非法（须为包内相对路径）：{path}"
        if not rel:
            return "缺少 path 参数"
        if is_sensitive_path(rel):
            return describe_rejection(rel)

        archive = self._open()
        if archive is None:
            return "日志包不可用（可能已被清理）"
        try:
            target = None
            for info in self._members(archive):
                if info.filename.replace("\\", "/") == rel:
                    target = info
                    break
            if target is None:
                return f"日志包中不存在该文件：{rel}"
            if not self._is_text_member(rel):
                return f"这是二进制文件，无法按文本读取：{rel}"

            try:
                raw = archive.read(target)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                return f"读取失败：{exc}"
        finally:
            archive.close()

        text = raw.decode("utf-8", "ignore")
        lines = text.splitlines()
        total = len(lines)

        def _as_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        start = _as_int(start_line) or 1
        end = _as_int(end_line) or total
        start = max(1, min(start, total)) if total else 1
        end = max(start, min(end, total)) if total else 1

        budget = self.max_result_chars
        chunk: list[str] = []
        used = 0
        last = start - 1
        for lineno in range(start, end + 1):
            line = f"[行 {lineno}] {lines[lineno - 1]}"
            if used + len(line) + 1 > budget:
                break
            chunk.append(line)
            used += len(line) + 1
            last = lineno

        header = (f"日志包 {rel}（共 {total} 行，{len(raw)} 字节）"
                  f" 显示 行 {start}-{last}：\n")
        body = "\n".join(chunk)
        if last < end:
            body += (f"\n...[受结果长度限制，行 {last + 1}-{end} 未显示，"
                     f"可用 start_line 继续读]")
        return header + body

    async def list_images(self) -> str:
        """列出压缩包内的全部图片（便于模型判断能附哪些图）。"""
        archive = self._open()
        if archive is None:
            return "日志包不可用（可能已被清理）"
        try:
            images = [
                f"{info.filename.replace(chr(92), '/')}  ({info.file_size} 字节)"
                for info in self._members(archive)
                if PurePosixPath(info.filename).suffix.lower() in IMAGE_SUFFIXES
            ]
        finally:
            archive.close()
        if not images:
            return "日志包内没有图片"
        return ("日志包内的图片（可用 [附图@日志: 路径] 发送）：\n"
                + "\n".join(sorted(images)))
