"""
仓库工具集 — 供 AI 在 agent 模式下自主检索项目代码。

暴露的工具:
  - list_dir      列目录（快速了解仓库结构）
  - search_repo   正则搜索（定位节点定义、字段、阈值）
  - read_file     读文件（支持行范围，便于分段查看大文件）
  - list_tags     列出可用版本 tag
  - checkout_tag  切换到指定版本复核

安全边界:
  - 所有路径先 resolve 再校验必须位于仓库根目录内（防 ../ 逃逸与符号链接逃逸）
  - 读取受单文件字节上限、单次返回字符上限约束
  - 拒绝读取 .git 内部与明显的二进制文件
  - 只读仓库文件，绝不执行仓库中的任何代码
  - checkout_tag 受 refuse_dirty_worktree 保护，且计入总轮次
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from core.models import RepoConfig
from features.maa.repo import RepoProvider, _SKIP_DIRS
from features.maa.safety import describe_rejection, is_sensitive_path

logger = logging.getLogger("Maa.RepoTools")

# 明显是二进制 / 不值得读的扩展名
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".pyd", ".class", ".jar", ".bin", ".dat", ".mp3",
    ".mp4", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".pdf", ".db",
}

# 工具结果统一前缀，便于模型区分工具输出与对话
TOOL_RESULT_PREFIX = "工具结果"

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "列出项目仓库中某个目录的文件与子目录。用于快速了解仓库结构、"
                "确认某个路径是否存在。省略 path 时列出仓库根目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对仓库根目录的目录路径，如 assets/resource/pipeline。省略则列出根目录。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo",
            "description": (
                "在项目仓库的文本文件中按正则搜索，返回命中行及行号。"
                "用于定位节点定义（如 PVP_Click:StartBattle）、字段名（如 threshold）、"
                "或期望文本。比逐个读文件高效得多，建议优先使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Python 正则表达式，如 PVP_Click:StartBattle 或 \"threshold\"",
                    },
                    "path": {
                        "type": "string",
                        "description": "限定搜索的子目录，如 assets/resource/pipeline。省略则搜索整个仓库。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的命中行数，默认 60，上限 200。",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取项目仓库中某个文本文件的内容。大文件可用 start_line / end_line "
                "分段读取。返回内容带行号，便于引用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对仓库根目录的文件路径，如 assets/resource/pipeline/PVP.json",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始，含）。省略则从头开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（含）。省略则读到文件末尾或达到上限。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tags",
            "description": "列出项目仓库中可用的版本 tag（版本号）。用于确认日志版本对应的 tag 是否存在。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkout_tag",
            "description": (
                "把项目仓库切换到指定版本 tag，之后读取的代码即为该版本。"
                "仅在需要核对日志运行版本之外的其他版本时使用。"
                "分析结束后 Bot 会自动切回最新版本。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "版本 tag，如 v1.0.0-alpha.9",
                    },
                },
                "required": ["tag"],
            },
        },
    },
]


# 日志压缩包工具（供 AI 像读仓库一样读取日志包内的原始文件）
LOG_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "log_list_files",
            "description": (
                "列出**本次上传的日志压缩包**内某目录的文件。"
                "摘要之外需要确认某个文件是否存在时使用。省略 path 则列出根目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "包内相对路径，如 on_error 或 config。省略则列根目录。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_search",
            "description": (
                "在**日志压缩包**的文本文件（.log/.json/.txt 等）中按正则搜索，"
                "返回命中行与行号。用于在摘要之外查找原始证据，"
                "例如某个节点名的全部出现位置、某个错误码的上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Python 正则表达式，如 StartUp 或 \"threshold\"",
                    },
                    "path": {
                        "type": "string",
                        "description": "限定搜索的包内子目录，如 config。省略则搜索整个包。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的命中行数，默认 60，上限 200。",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_read_file",
            "description": (
                "读取**日志压缩包**内某个文本文件的内容，大文件可用 "
                "start_line / end_line 分段读取。返回内容带行号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "包内相对路径，如 config/mxu-MaaXXX.json 或 maafw.log",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始，含）。省略则从头开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（含）。省略则读到末尾或达到上限。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_list_images",
            "description": (
                "列出日志压缩包内的全部图片。"
                "用于确认可以用 `[附图@日志: 路径]` 发送哪些图。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


class RepoTools:
    """在仓库沙箱内执行 AI 请求的工具调用。"""

    def __init__(self, provider: RepoProvider, config: RepoConfig):
        self.provider = provider
        self.cfg = config
        self.tag_switches = 0          # 已切换 tag 次数（受 max_tag_switch_rounds 限制）
        self._max_read_bytes = max(16, int(config.max_file_kb or 256)) * 1024

    # ════════════════════════════════════════════════════════════
    # 入口
    # ════════════════════════════════════════════════════════════

    async def execute(self, name: str, arguments: dict) -> str:
        """执行一次工具调用，返回给模型看的文本。任何异常都转为文本而非抛出。"""
        handler = {
            "list_dir": self._list_dir,
            "search_repo": self._search_repo,
            "read_file": self._read_file,
            "list_tags": self._list_tags,
            "checkout_tag": self._checkout_tag,
        }.get(name)

        if handler is None:
            return f"未知工具：{name}。可用工具：list_dir, search_repo, read_file, list_tags, checkout_tag"

        try:
            return await handler(arguments or {})
        except Exception as exc:
            logger.warning(f"[工具] {name} 执行异常：{exc}", exc_info=True)
            return f"工具 {name} 执行失败：{type(exc).__name__}: {exc}"

    # ════════════════════════════════════════════════════════════
    # 路径沙箱
    # ════════════════════════════════════════════════════════════

    def _resolve(self, rel: str) -> tuple[Optional[Path], str]:
        """
        把相对路径解析为仓库内的绝对路径。

        返回 (路径, 错误信息)。校验失败时路径为 None。
        """
        root = self.provider.root
        if root is None:
            return None, "仓库不可用"
        try:
            root_real = root.resolve()
        except OSError as exc:
            return None, f"仓库路径无法解析：{exc}"

        raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
        if not raw:
            return root_real, ""

        candidate = (root_real / raw)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            return None, f"路径无法解析：{exc}"

        # 必须位于仓库根目录内（含自身）
        if resolved != root_real and root_real not in resolved.parents:
            return None, f"路径越出仓库范围，已拒绝：{rel}"

        # 拒绝访问 .git 内部
        try:
            rel_parts = resolved.relative_to(root_real).parts
        except ValueError:
            return None, f"路径越出仓库范围，已拒绝：{rel}"
        if rel_parts and rel_parts[0] == ".git":
            return None, "已拒绝访问 .git 目录"

        # 拒绝凭据 / 密钥类文件（.env、私钥、凭据清单等）
        if is_sensitive_path("/".join(rel_parts)):
            return None, describe_rejection("/".join(rel_parts))

        return resolved, ""

    @staticmethod
    def _display(root: Optional[Path], path: Path) -> str:
        """相对仓库根目录的展示路径。"""
        if root is None:
            return str(path)
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    def _is_readable(self, path: Path) -> bool:
        return path.suffix.lower() not in _BINARY_SUFFIXES

    # ════════════════════════════════════════════════════════════
    # 工具实现
    # ════════════════════════════════════════════════════════════

    async def _list_dir(self, args: dict) -> str:
        target, err = self._resolve(args.get("path", ""))
        if target is None:
            return err
        if not target.exists():
            return f"目录不存在：{self._display(self.provider.root, target)}"
        if not target.is_dir():
            return f"不是目录（如需读取文件请用 read_file）：{self._display(self.provider.root, target)}"

        entries: list[str] = []
        try:
            for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if entry.name in _SKIP_DIRS:
                    continue
                if entry.is_dir():
                    entries.append(f"{entry.name}/")
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(f"{entry.name}  ({size} 字节)")
        except OSError as exc:
            return f"读取目录失败：{exc}"

        if not entries:
            return f"{self._display(self.provider.root, target)} 为空目录"

        rel = self._display(self.provider.root, target)
        limit = 300
        shown = entries[:limit]
        text = f"{rel} 下的内容（{len(entries)} 项）：\n" + "\n".join(shown)
        if len(entries) > limit:
            text += f"\n...[还有 {len(entries) - limit} 项未显示]"
        return text

    async def _search_repo(self, args: dict) -> str:
        pattern_text = str(args.get("pattern") or "").strip()
        if not pattern_text:
            return "缺少 pattern 参数"

        base, err = self._resolve(args.get("path", ""))
        if base is None:
            return err
        if not base.is_dir():
            return f"搜索路径不是目录：{self._display(self.provider.root, base)}"

        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            return f"正则表达式无效：{exc}"

        try:
            max_results = int(args.get("max_results") or 60)
        except (TypeError, ValueError):
            max_results = 60
        max_results = max(1, min(200, max_results))

        exts = {e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in (self.cfg.extensions or [])}
        max_files = max(100, int(self.cfg.max_scan_files or 20000))

        hits: list[str] = []
        scanned = 0
        truncated = False
        stop = False

        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                path = Path(dirpath) / filename
                if not self._is_readable(path):
                    continue
                if exts and path.suffix.lower() not in exts:
                    continue
                try:
                    if path.stat().st_size > self._max_read_bytes:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except (OSError, ValueError):
                    continue

                scanned += 1
                if scanned > max_files:
                    truncated = True
                    stop = True
                    break

                rel = self._display(self.provider.root, path)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if not pattern.search(line):
                        continue
                    hits.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                    if len(hits) >= max_results:
                        truncated = True
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break

        if not hits:
            return f"未找到匹配 {pattern_text!r} 的内容（已扫描 {scanned} 个文件）"

        text = (f"匹配 {pattern_text!r} 的结果（{len(hits)} 条"
                + ("，已达上限" if truncated else "") + f"，扫描 {scanned} 个文件）：\n")
        text += "\n".join(hits)
        if truncated:
            text += "\n...[结果已截断，可用更精确的 pattern 或限定 path 缩小范围]"
        return text

    async def _read_file(self, args: dict) -> str:
        path_arg = str(args.get("path") or "").strip()
        if not path_arg:
            return "缺少 path 参数"

        target, err = self._resolve(path_arg)
        if target is None:
            return err
        if not target.exists():
            return f"文件不存在：{path_arg}"
        if target.is_dir():
            return f"这是目录（请用 list_dir）：{path_arg}"
        if not self._is_readable(target):
            return f"已拒绝读取二进制文件：{path_arg}"

        try:
            size = target.stat().st_size
        except OSError as exc:
            return f"无法读取文件信息：{exc}"

        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError) as exc:
            return f"读取失败：{exc}"

        lines = text.splitlines()
        total = len(lines)

        def _as_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        start = _as_int(args.get("start_line")) or 1
        end = _as_int(args.get("end_line")) or total
        start = max(1, min(start, total))
        end = max(start, min(end, total))

        # 单次返回字符预算
        budget = max(2000, int(self.cfg.max_tool_result_chars or 30000))
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

        rel = self._display(self.provider.root, target)
        header = (f"{rel}（共 {total} 行，{size} 字节）"
                  f" 显示 行 {start}-{last}：\n")
        body = "\n".join(chunk)
        if last < end:
            body += f"\n...[受 max_tool_result_chars 限制，行 {last + 1}-{end} 未显示，可用 start_line 继续读]"
        return header + body

    async def _list_tags(self, args: dict) -> str:
        tags = await self.provider.list_tags()
        current = self.provider.current_ref
        if not tags:
            return (f"未取到 tag 列表（当前版本：{current}）。"
                    f"可能是浅克隆尚未拉取 tag，或该仓库未打 tag。")
        shown = tags[-60:]
        text = f"可用 tag（共 {len(tags)} 个，当前代码版本：{current}）：\n"
        text += "\n".join(shown)
        if len(tags) > len(shown):
            text = f"（仅显示最近 {len(shown)} 个）\n" + text
        return text

    async def _checkout_tag(self, args: dict) -> str:
        tag = str(args.get("tag") or "").strip()
        if not tag:
            return "缺少 tag 参数"

        limit = max(0, int(self.cfg.max_tag_switch_rounds or 0))
        if not self.cfg.allow_ai_switch_tag:
            return "配置已禁用 AI 切换版本（repo.allow_ai_switch_tag=false）"
        if self.tag_switches >= limit:
            return f"已达到切换版本上限（{limit} 次），请基于当前版本给出结论"

        root = self.provider.root
        if root is None or not (root / ".git").exists():
            return "仓库不是 git 仓库（或未配置 git 来源），无法切换版本"

        if await self.provider.is_dirty(root):
            return ("仓库存在未提交改动，为保护工作区已拒绝切换版本。"
                    "请基于当前版本给出结论。")

        # 允许省略 v 前缀
        target = tag
        if not await self.provider.tag_exists(root, target):
            alt = tag if tag.startswith("v") else f"v{tag}"
            if await self.provider.tag_exists(root, alt):
                target = alt
            else:
                await self.provider._fetch_tag_candidates(root, tag)
                if await self.provider.tag_exists(root, alt):
                    target = alt
                elif not await self.provider.tag_exists(root, target):
                    return f"未找到 tag：{tag}（可用 list_tags 查看）"

        if not await self.provider.checkout(root, target):
            return f"切换到 {target} 失败：{self.provider.last_error or '未知原因'}"

        self.tag_switches += 1
        return (f"已切换到版本 {target}（本分析已切换 {self.tag_switches}/{limit} 次）。"
                f"后续 read_file / search_repo 读到的是该版本代码。"
                f"分析结束后会自动切回最新版本。")
