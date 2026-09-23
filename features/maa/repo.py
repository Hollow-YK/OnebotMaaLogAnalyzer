"""
项目代码仓库 — 供 AI 对照源码确认问题。

支持两种来源:
  - path: 本地已有仓库目录（优先，无需网络与 git）
  - url:  git 仓库地址（含镜像），浅克隆到 data/repos/<hash> 并复用

版本对齐:
  - 从日志包文件名提取版本号（如 MaaXXX-logs-1.0.0-alpha.9-20260907-033044.zip
    → 1.0.0-alpha.9），自动 checkout 对应 tag，使代码参考与日志实际版本一致
  - 支持按需拉取单个 tag（避免首次克隆拉取整个仓库历史）
  - 分析结束后切回最新（默认分支）

检索策略:
  1. 从日志摘要中提取高价值标识符（Maa 节点名、name=/entry= 字段、expected 文本）
  2. 单遍扫描仓库文本文件，用合并正则快速定位命中行
  3. 按「命中节点定义行数」「命中标识符数量」排序，取前 N 个文件
  4. 渲染为带行号的代码片段，受 max_chars 限制

安全性:
  - 只读取仓库内的文本文件，不执行任何仓库代码
  - git 操作限定为 clone / fetch / checkout / pull / tag / rev-parse
  - 扫描受文件大小、文件数量、字符总量三重限制
  - 所有路径读取前都会校验是否位于仓库根目录内
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from core.models import RepoConfig
from features.maa.log_digest import IMAGE_SUFFIXES
from features.maa.safety import describe_rejection, is_sensitive_path

logger = logging.getLogger("Maa.Repo")

# 仓库内可附带的图片扩展名（与日志包保持一致）
IMAGE_SUFFIXES_REPO = IMAGE_SUFFIXES

# 扫描时始终跳过的目录名
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", "target", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
}

# 从日志中提取标识符的模式（按可信度从高到低）
_IDENT_PATTERNS: list[tuple[re.Pattern, int]] = [
    # 日志字段：name=XXX / entry=XXX（Maa 节点或任务名）
    (re.compile(r"\b(?:name|entry)=([A-Za-z][A-Za-z0-9_.\-]{2,80})"), 3),
    # JSON 字段："name":"XXX" / "taskName":"XXX"
    (re.compile(r'"(?:name|entry|taskName|nodeName|resourceName|controllerName)"\s*:\s*'
                r'"([A-Za-z][A-Za-z0-9_.\-]{2,80})"'), 3),
    # Maa 节点名（含冒号，如 PVP_Click:StartBattle）
    (re.compile(r"\b([A-Z][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)+)\b"), 4),
    # 识别期望文本：expected="中文"
    (re.compile(r'expected="([^"\n]{2,60})"'), 5),
]

# 日志中高频出现但不值得检索的 C++ 符号 / 模块名
_NOISE_IDENTIFIERS = {
    "Logger", "Tasker", "Controller", "Resource", "Recognizer", "TemplateMatcher",
    "OCRer", "OptionMgr", "MaaFramework", "AgentClient", "PipelineTask",
    "EventDispatcher", "MaaTasker", "PipelineParser", "Adb", "Agent", "Vision",
    "CustomRecognizer", "CustomAction", "MaaGlobalSetOption", "MaaTaskerPostTask",
    "MaaTaskerPostStop", "MaaTaskerPost", "MaaResource", "MaaController",
    "handle_controller_wait", "handle_tasker_stopping", "handle_event_response",
}

# 形如源码文件名的标识符（多半是日志里的路径，不是节点名）
_SOURCE_SUFFIX = re.compile(
    r"\.(?:log|json|jsonc|py|pyc|yaml|yml|ts|js|tsx|jsx|cpp|hpp|h|c|cc|cs|java|kt|go|rs|toml|ini|cfg|md|txt|png|jpg|jpeg|webp|bmp)$",
    re.IGNORECASE,
)


# 日志包文件名中的版本号与时间戳
# 例：MaaXXX-logs-1.0.0-alpha.9-20260907-033044.zip → 1.0.0-alpha.9
_LOG_VERSION = re.compile(
    r"-logs?-(?P<version>.+?)-(?P<ts>\d{8}-\d{6})\.zip$", re.IGNORECASE
)
# 兑底：不带时间戳的版本号
_LOG_VERSION_FALLBACK = re.compile(r"-logs?-(?P<version>.+?)\.zip$", re.IGNORECASE)
# 版本号至少包含一个字母或点（排除 20260101-120000 这类纯时间戳片段）
_LOOKS_LIKE_VERSION = re.compile(r"[A-Za-z.]")


def extract_version(file_name: str) -> str:
    """
    从日志包文件名中提取版本号。

    MaaXXX-logs-1.0.0-alpha.9-20260907-033044.zip → '1.0.0-alpha.9'
    无法识别时返回空字符串。
    """
    name = str(file_name or "").strip()
    if not name:
        return ""

    match = _LOG_VERSION.search(name)
    if match:
        version = match.group("version").strip(" -_")
        if version and _LOOKS_LIKE_VERSION.search(version):
            return version

    # 主模式失败时兜底，但要求形似版本号，避免把时间戳当成版本
    match = _LOG_VERSION_FALLBACK.search(name)
    if match:
        version = match.group("version").strip(" -_")
        if version and _LOOKS_LIKE_VERSION.search(version):
            return version
    return ""


def extract_identifiers(digest_prompt: str, limit: int = 24) -> list[str]:
    """
    从日志摘要中提取值得检索的标识符，按可信度与出现频次排序。

    返回去重后的标识符列表，最多 limit 个。
    """
    if not digest_prompt or limit <= 0:
        return []

    scores: dict[str, int] = {}
    for pattern, weight in _IDENT_PATTERNS:
        for match in pattern.finditer(digest_prompt):
            ident = match.group(1).strip()
            if not _is_useful_identifier(ident):
                continue
            scores[ident] = scores.get(ident, 0) + weight

    # 按权重降序、同权重按名称升序，保证结果稳定
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [ident for ident, _ in ranked[:limit]]


def _is_useful_identifier(ident: str) -> bool:
    """过滤掉明显无检索价值的标识符。"""
    if len(ident) < 3:
        return False
    if ident in _NOISE_IDENTIFIERS:
        return False
    if _SOURCE_SUFFIX.search(ident):
        return False
    if "/" in ident or "\\" in ident:
        return False
    # 纯数字或纯符号
    if not any(ch.isalpha() for ch in ident):
        return False
    return True


class RepoProvider:
    """项目代码仓库访问 — 准备仓库并检索相关代码片段。"""

    def __init__(self, config: RepoConfig, data_dir: str | Path):
        self.cfg = config
        self._data_dir = Path(data_dir)
        self._ready = False
        self._last_error = ""
        self._current_ref = ""   # 当前已切到的 tag（空 = 默认分支）
        self._origin_ref = ""    # 分析前的位置，用于精确还原

    # ════════════════════════════════════════════════════════════
    # 路径
    # ════════════════════════════════════════════════════════════

    @property
    def root(self) -> Optional[Path]:
        """仓库根目录：优先本地 path，其次克隆缓存目录。"""
        if self.cfg.path:
            return Path(self.cfg.path).expanduser()
        if self.cfg.url:
            return self._cache_root()
        return None

    def _cache_root(self) -> Path:
        """按 url + branch 生成稳定的缓存目录。"""
        key = f"{self.cfg.url}|{self.cfg.branch or 'HEAD'}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return self._data_dir / "repos" / digest

    def describe(self) -> str:
        if self.cfg.path:
            base = f"本地路径 {self.cfg.path}"
        elif self.cfg.url:
            branch = f"（分支 {self.cfg.branch}）" if self.cfg.branch else ""
            base = f"{self.cfg.url}{branch}"
        else:
            return "（未配置）"
        return base

    def describe_version(self) -> str:
        """带版本标注的描述，供提示词头部使用。"""
        base = self.describe()
        if self._current_ref:
            return f"{base} · 版本 {self._current_ref}"
        return f"{base} · 默认分支最新"

    @property
    def last_error(self) -> str:
        return self._last_error

    # ════════════════════════════════════════════════════════════
    # 准备仓库
    # ════════════════════════════════════════════════════════════

    async def ensure_ready(self) -> bool:
        """确保仓库可用。返回 False 表示不可用（已记录原因）。"""
        if self._ready:
            return True

        root = self.root
        if root is None:
            self._last_error = "未配置 repo.path 或 repo.url"
            logger.warning(f"[仓库] {self._last_error}")
            return False

        if self.cfg.path:
            if not root.is_dir():
                self._last_error = f"本地路径不存在：{root}"
                logger.warning(f"[仓库] {self._last_error}")
                return False
            self._ready = True
            return True

        # url 模式：需要 git
        if shutil.which("git") is None:
            self._last_error = "未找到 git 可执行文件，无法克隆仓库"
            logger.warning(f"[仓库] {self._last_error}")
            return False

        if (root / ".git").exists():
            if self.cfg.update_on_analyze:
                await self._update(root)
            self._ready = True
            return True

        await self._clone(root)
        self._ready = True
        return True

    async def _clone(self, root: Path):
        root.parent.mkdir(parents=True, exist_ok=True)
        depth = max(0, int(self.cfg.clone_depth or 0))
        args = ["clone"]
        if depth > 0:
            args += ["--depth", str(depth), "--single-branch"]
        if self.cfg.branch:
            args += ["--branch", self.cfg.branch]
        args += [self.cfg.url, str(root)]

        logger.info(f"[仓库] 克隆 {self.describe()} → {root}")
        try:
            await _run_git(args, timeout=int(self.cfg.clone_timeout_seconds or 300))
            logger.info("[仓库] 克隆完成")
        except Exception as exc:
            # 克隆失败时清理残留目录，避免下次误判为已就绪
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            self._last_error = f"克隆失败：{exc}"
            raise RuntimeError(self._last_error) from exc

    async def _update(self, root: Path):
        logger.info("[仓库] 拉取更新 ...")
        try:
            await _run_git(["pull", "--ff-only", "--depth", "1"], cwd=root,
                           timeout=int(self.cfg.clone_timeout_seconds or 300))
            logger.info("[仓库] 更新完成")
        except Exception as exc:
            # 更新失败不阻断分析，沿用现有代码
            logger.warning(f"[仓库] 更新失败，沿用现有代码：{exc}")

    # ════════════════════════════════════════════════════════════
    # 版本切换
    # ════════════════════════════════════════════════════════════

    @property
    def current_ref(self) -> str:
        """当前已切到的 tag / 分支描述，供提示词标注代码版本。"""
        return self._current_ref or "（默认分支）"

    async def align_version(self, file_name: str) -> str:
        """
        根据日志包文件名把仓库切到对应版本 tag。

        返回实际切到的 ref（空字符串 = 未切换，仍为默认分支）。
        """
        self._current_ref = ""
        if not self.cfg.auto_checkout_version:
            return ""
        if not self.cfg.git_ops_allowed():
            return ""
        root = self.root
        if root is None or not (root / ".git").exists():
            # 本地 path 模式且非 git 仓库：无法切换版本，保持原样
            return ""

        # 记住分析前的位置，结束时精确还原（而非猜默认分支）
        if not self._origin_ref:
            await self.remember_current_ref(root)

        version = extract_version(file_name)
        if not version:
            logger.info("[仓库] 日志文件名中未识别到版本号，使用默认分支")
            return ""

        ref = await self._resolve_tag(root, version)
        if not ref and self.cfg.fetch_tags_on_demand:
            # 浅克隆可能只带了部分 tag，按需拉取候选 tag 再解析
            await self._fetch_tag_candidates(root, version)
            ref = await self._resolve_tag(root, version)
        if not ref:
            logger.info(f"[仓库] 未找到版本 {version} 对应的 tag，使用默认分支")
            return ""

        if not await self.checkout(root, ref):
            return ""
        return ref

    def _tag_candidates(self, version: str) -> list[str]:
        """日志版本号 → 可能的 tag 名列表。"""
        candidates = [version]
        if not version.lower().startswith("v"):
            candidates.append(f"v{version}")
        return candidates

    async def _fetch_tag_candidates(self, root: Path, version: str) -> None:
        """按需拉取日志版本号对应的候选 tag（忽略失败）。"""
        timeout = int(self.cfg.clone_timeout_seconds or 300)
        for candidate in self._tag_candidates(version):
            if await self._tag_exists(root, candidate):
                continue
            try:
                await _run_git(
                    ["fetch", "--depth", "1", "origin",
                     f"refs/tags/{candidate}:refs/tags/{candidate}"],
                    cwd=root, timeout=timeout,
                )
                logger.info(f"[仓库] 已拉取 tag {candidate}")
                return
            except Exception as exc:
                logger.debug(f"[仓库] 拉取 tag {candidate} 失败：{exc}")

    async def _resolve_tag(self, root: Path, version: str) -> str:
        """
        把日志版本号解析为仓库中实际存在的 tag。

        依次尝试：原样 → 加 v 前缀 → 前缀匹配（1.0.0 → v1.0.0 / 1.0.0）。
        """
        candidates = self._tag_candidates(version)

        known = await self.list_tags(root)
        if known:
            # 优先精确命中（忽略 v 前缀差异）
            for candidate in candidates:
                for tag in known:
                    if tag == candidate or tag.lstrip("v") == candidate.lstrip("v"):
                        return tag
            # 退化为前缀匹配（日志版本可能比 tag 更细，如带构建号）
            # 取最短匹配，避免 1.0.0-alpha.1 误配到 v1.0.0-alpha.10
            for candidate in candidates:
                prefix = candidate.lstrip("v")
                matches = [t for t in known if t.lstrip("v").startswith(prefix)]
                if matches:
                    return min(matches, key=len)
            return ""

        # 拿不到 tag 列表（浅克隆未拉取），按候选名逐个尝试
        for candidate in candidates:
            if await self._tag_exists(root, candidate):
                return candidate
        return ""

    async def list_tags(self, root: Optional[Path] = None) -> list[str]:
        """列出仓库中的 tag（本地已有的）。"""
        root = root or self.root
        if root is None or not (root / ".git").exists():
            return []
        try:
            out = await _run_git(["tag", "--list"], cwd=root, timeout=30)
            return [line.strip() for line in out.splitlines() if line.strip()]
        except Exception as exc:
            logger.debug(f"[仓库] 读取 tag 列表失败：{exc}")
            return []

    async def _tag_exists(self, root: Path, tag: str) -> bool:
        return await self.tag_exists(root, tag)

    async def tag_exists(self, root: Optional[Path], tag: str) -> bool:
        """判断仓库中是否存在指定 tag。"""
        root = root or self.root
        if root is None or not (root / ".git").exists():
            return False
        try:
            await _run_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
                           cwd=root, timeout=30)
            return True
        except Exception:
            return False

    async def is_dirty(self, root: Optional[Path] = None) -> bool:
        """仓库是否有未提交改动（含未跟踪文件）。"""
        root = root or self.root
        if root is None or not (root / ".git").exists():
            return False
        try:
            out = await _run_git(["status", "--porcelain"], cwd=root, timeout=30)
            return bool(out.strip())
        except Exception:
            # 判断失败时保守认为「有改动」，避免误切换破坏工作区
            return True

    async def checkout(self, root: Optional[Path], ref: str) -> bool:
        """
        切换到指定 tag / 分支。

        浅克隆仓库可能没有目标 tag，会先按需拉取再重试。
        仓库存在未提交改动时拒绝切换，避免破坏用户工作区。
        """
        root = root or self.root
        if root is None or not (root / ".git").exists():
            return False

        if self.cfg.refuse_dirty_worktree and await self.is_dirty(root):
            logger.warning("[仓库] 存在未提交改动，跳过版本切换以保护工作区")
            self._last_error = "仓库有未提交改动，已跳过版本切换"
            return False

        timeout = int(self.cfg.clone_timeout_seconds or 300)
        try:
            await _run_git(["checkout", "--force", ref], cwd=root, timeout=timeout)
            logger.info(f"[仓库] 已切换到 {ref}")
            self._current_ref = ref
            return True
        except Exception as exc:
            logger.info(f"[仓库] 切换 {ref} 失败，尝试拉取该 tag：{exc}")

        if not self.cfg.fetch_tags_on_demand:
            return False
        try:
            await _run_git(["fetch", "--depth", "1", "origin", f"refs/tags/{ref}:refs/tags/{ref}"],
                           cwd=root, timeout=timeout)
            await _run_git(["checkout", "--force", ref], cwd=root, timeout=timeout)
            logger.info(f"[仓库] 已拉取并切换到 {ref}")
            self._current_ref = ref
            return True
        except Exception as exc:
            logger.warning(f"[仓库] 拉取并切换 {ref} 失败：{exc}")
            return False

    async def remember_current_ref(self, root: Optional[Path] = None) -> str:
        """
        记录当前所在的 ref，供分析结束后精确还原。

        返回 'branch:名称' / 'tag:名称' / 'detached:提交号'，
        无法判断时返回空字符串。
        """
        root = root or self.root
        if root is None or not (root / ".git").exists():
            return ""
        try:
            name = (await _run_git(["rev-parse", "--abbrev-ref", "HEAD"],
                                   cwd=root, timeout=30)).strip()
        except Exception as exc:
            logger.debug(f"[仓库] 读取当前 ref 失败：{exc}")
            return ""

        if name and name != "HEAD":
            self._origin_ref = f"branch:{name}"
            return self._origin_ref

        # detached HEAD（可能停在某个 tag 上）
        try:
            commit = (await _run_git(["rev-parse", "HEAD"], cwd=root, timeout=30)).strip()
        except Exception:
            return ""
        if commit:
            self._origin_ref = f"detached:{commit}"
        return self._origin_ref

    async def restore_origin(self) -> bool:
        """
        还原到分析前记录的 ref。

        这比「切回默认分支」更安全：用户可能正在某个特性分支上工作。
        未记录过原始 ref 时退化为 restore_latest()。
        """
        if not self.cfg.restore_latest_after_analyze:
            return False
        root = self.root
        if root is None or not (root / ".git").exists():
            return False

        if not self._origin_ref:
            return await self.restore_latest()

        kind, _, value = self._origin_ref.partition(":")
        if not value:
            return await self.restore_latest()

        if kind == "detached":
            ok = await self.checkout(root, value)
        else:
            ok = await self.checkout(root, value)

        if ok:
            self._current_ref = ""
            logger.info(f"[仓库] 已还原到分析前的位置：{value}")
        else:
            logger.warning(f"[仓库] 还原到 {value} 失败，尝试默认分支")
            return await self.restore_latest()
        return ok

    async def restore_latest(self) -> bool:
        """切回默认分支最新（仅在无法还原原始位置时作为兜底）。"""
        if not self.cfg.restore_latest_after_analyze:
            return False
        root = self.root
        if root is None or not (root / ".git").exists():
            return False

        target = await self._latest_ref(root)
        if not target:
            return False
        ok = await self.checkout(root, target)
        if ok:
            self._current_ref = ""
        return ok

    async def _latest_ref(self, root: Path) -> str:
        """确定「最新」对应的 ref：优先配置，其次远端默认分支，最后 main/master。"""
        if self.cfg.default_branch:
            return self.cfg.default_branch

        timeout = int(self.cfg.clone_timeout_seconds or 300)
        try:
            out = await _run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                                 cwd=root, timeout=30)
            name = out.strip()
            if name:
                return name.split("/", 1)[-1]
        except Exception:
            pass

        for candidate in ("main", "master"):
            try:
                await _run_git(["rev-parse", "--verify", "--quiet", candidate],
                               cwd=root, timeout=30)
                return candidate
            except Exception:
                continue

        # 浅克隆常见情况：仅有一个远端分支，直接用 origin/HEAD 指向的提交
        try:
            out = await _run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"],
                                 cwd=root, timeout=timeout)
            return out.strip() or ""
        except Exception:
            return ""

    # ════════════════════════════════════════════════════════════
    # 检索
    # ════════════════════════════════════════════════════════════

    async def collect_references(self, identifiers: list[str]) -> str:
        """
        在仓库中检索标识符，返回可直接拼入提示词的代码片段段落。

        未找到任何命中时返回空字符串。
        """
        if not identifiers:
            return ""
        root = self.root
        if root is None or not root.is_dir():
            return ""

        try:
            return await asyncio.to_thread(self._scan, root, identifiers)
        except Exception as exc:
            logger.warning(f"[仓库] 检索失败：{exc}", exc_info=True)
            return ""

    def _scan(self, root: Path, identifiers: list[str]) -> str:
        """同步扫描仓库（在线程池中执行）。"""
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in (self.cfg.extensions or [])}
        max_file_bytes = max(16, int(self.cfg.max_file_kb or 256)) * 1024
        max_scan_files = max(100, int(self.cfg.max_scan_files or 20000))

        pattern = re.compile("|".join(re.escape(i) for i in identifiers))
        definition_patterns = {
            ident: re.compile(r"""["']""" + re.escape(ident) + r"""["']\s*:""")
            for ident in identifiers
        }

        # rel_path → {行号: 行内容}
        file_hits: dict[str, dict[int, str]] = {}
        # rel_path → 定义行号集合
        file_defs: dict[str, set[int]] = {}
        file_idents: dict[str, set[str]] = {}
        # rel_path → 完整行列表（仅命中文件，用于块展开）
        file_lines: dict[str, list[str]] = {}
        found: set[str] = set()
        scanned = 0
        truncated = False

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if exts and ext not in exts:
                    continue

                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > max_file_bytes:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except (OSError, ValueError):
                    continue

                scanned += 1
                if scanned > max_scan_files:
                    truncated = True
                    break

                lines = text.splitlines()
                rel = path.relative_to(root).as_posix()
                hits: dict[int, str] = {}
                defs: set[int] = set()
                idents_here: set[str] = set()

                for lineno, line in enumerate(lines, 1):
                    if not pattern.search(line):
                        continue
                    for ident in identifiers:
                        if ident not in line:
                            continue
                        hits[lineno] = line
                        idents_here.add(ident)
                        found.add(ident)
                        if definition_patterns[ident].search(line):
                            defs.add(lineno)

                if hits:
                    file_hits[rel] = hits
                    file_idents[rel] = idents_here
                    if defs:
                        file_defs[rel] = defs
                        file_lines[rel] = lines

            if truncated:
                break

        if not file_hits:
            logger.info(f"[仓库] 未找到任何命中（扫描 {scanned} 个文件）")
            return ""

        # 排序：先按命中定义行数，再按命中标识符种类数，最后按命中行数
        ranked = sorted(
            file_hits.items(),
            key=lambda kv: (
                -len(file_defs.get(kv[0], ())),
                -len(file_idents.get(kv[0], ())),
                -len(kv[1]),
            ),
        )[: max(1, int(self.cfg.max_files or 12))]

        logger.info(
            f"[仓库] 扫描 {scanned} 个文件，命中 {len(file_hits)} 个文件"
            f"（注入前 {len(ranked)} 个），标识符 {len(found)}/{len(identifiers)}"
        )

        return self._render(ranked, identifiers, found, truncated, file_lines, file_defs)

    def _render(self, ranked: list[tuple[str, dict[int, str]]],
                identifiers: list[str], found: set[str],
                truncated: bool, file_lines: dict[str, list[str]],
                file_defs: dict[str, set[int]]) -> str:
        """渲染代码片段段落，受 max_chars 限制。"""
        context = max(0, int(self.cfg.context_lines or 0))
        max_chars = max(2000, int(self.cfg.max_chars or 60000))
        max_block_lines = max(20, int(self.cfg.max_block_lines or 150))
        root = self.root

        parts: list[str] = [
            f"项目代码参考（来源：{self.describe_version()}）：",
            f"已命中标识符：{', '.join(sorted(found))}",
            "",
        ]

        for rel, hits in ranked:
            lines = file_lines.get(rel)
            ranges = self._hit_ranges(
                hits, context, lines, max_block_lines, file_defs.get(rel, set())
            )

            block = [f"### {rel}"]
            for start, end in ranges:
                block.append(f"-- 行 {start}-{end} --")
                for lineno in range(start, end + 1):
                    text = hits.get(lineno)
                    if text is None:
                        if lines is not None and 1 <= lineno <= len(lines):
                            text = lines[lineno - 1]
                        else:
                            text = self._read_line(root, rel, lineno)
                        if text is None:
                            continue
                    block.append(f"[行 {lineno}] {_clip(text)}")

            parts.extend(block)
            parts.append("")

            if sum(len(p) + 1 for p in parts) > max_chars:
                parts.append("...[代码片段达到 max_chars 上限，后续已省略]")
                break

        if truncated:
            parts.append("[仓库文件数超过 max_scan_files，扫描提前结束]")

        missing = [i for i in identifiers if i not in found]
        if missing:
            parts.append(f"（未在项目代码中找到：{', '.join(missing)}）")

        return "\n".join(parts).strip()

    @staticmethod
    def _hit_ranges(hits: dict[int, str], context: int,
                    lines: Optional[list[str]], max_block_lines: int,
                    definition_lines: set[int]) -> list[list[int]]:
        """
        把命中行转换为待渲染的行区间。

        - 命中 JSON 对象定义行（如 `"PVP_Click:StartBattle": {`）时，
          展开为整个对象块，确保 recognition / threshold / template / roi 一并呈现。
        - 其余命中行按 context 前后扩展。
        """
        ranges: list[list[int]] = []

        def add(start: int, end: int):
            start = max(1, start)
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([start, end])

        for lineno in sorted(hits):
            block_end = None
            if lines is not None and lineno in definition_lines and _is_object_start(hits[lineno]):
                block_end = _json_block_end(lines, lineno - 1, max_block_lines)
            if block_end is not None:
                add(lineno, block_end + 1)
            else:
                add(lineno - context, lineno + context)

        return ranges

    # ════════════════════════════════════════════════════════════
    # 模板图片查找（供分析结果附图）
    # ════════════════════════════════════════════════════════════

    def find_files(self, names: list[str], limit: int = 6,
                   *, images_only: bool = False) -> list[tuple[str, bytes]]:
        """
        在本地仓库中按**相对路径**精确查找文件，返回 [(相对路径, 字节)]。

        只接受相对路径且只做精确匹配（大小写不敏感）：
        不按文件名回退，避免模型写了不存在的路径却命中同名文件，
        从而发出非预期内容。找不到就返回空，由上层给出「未找到」提示。

        仅支持本地 path 模式（不做网络请求）。超出单文件大小上限的跳过。
        """
        root = self.root
        if root is None or not root.is_dir() or not names:
            return []

        max_bytes = max(64, int(self.cfg.max_file_kb or 256)) * 1024
        if not images_only:
            # 非图片附件可能较大，放宽到 2 MB
            max_bytes = max(max_bytes, 2 * 1024 * 1024)

        root_resolved = root.resolve()
        ordered: list[tuple[str, bytes]] = []
        seen: set[str] = set()

        for raw in names:
            # 只用 removeprefix 去掉 "./" 前缀，不能用 lstrip：
            # lstrip("./") 会剥掉开头所有 '.' 与 '/'，把 ".env" 变成 "env"，
            # 从而读到另一个文件（同名歧义）或绕过安全检查。
            rel = str(raw or "").strip().replace("\\", "/")
            while rel.startswith("./"):
                rel = rel[2:]
            rel = rel.lstrip("/")
            if not rel or rel in seen:
                continue
            seen.add(rel)

            # 凭据 / 密钥类文件禁止外发
            if is_sensitive_path(rel):
                logger.info(f"[仓库] 拒绝外发敏感文件：{describe_rejection(rel)}")
                continue

            candidate = root / rel
            # 沙箱校验：必须位于仓库根目录内
            try:
                resolved = candidate.resolve()
                if root_resolved not in resolved.parents:
                    logger.info(f"[仓库] 拒绝越界路径：{rel}")
                    continue
            except OSError:
                continue

            if not candidate.is_file():
                continue
            if images_only and candidate.suffix.lower() not in IMAGE_SUFFIXES_REPO:
                continue
            try:
                if candidate.stat().st_size > max_bytes:
                    logger.info(f"[仓库] 文件超过大小上限，已跳过：{rel}")
                    continue
                data = candidate.read_bytes()
            except (OSError, ValueError):
                continue

            ordered.append((rel, data))
            if len(ordered) >= limit:
                break

        return ordered

    @staticmethod
    def _read_line(root: Optional[Path], rel: str, lineno: int) -> Optional[str]:
        """读取文件指定行，用于补齐上下文。路径必须位于仓库内。"""
        if root is None:
            return None
        path = (root / rel).resolve()
        try:
            if root.resolve() not in path.parents and path.parent != root.resolve():
                return None
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for index, line in enumerate(handle, 1):
                    if index == lineno:
                        return line.rstrip("\r\n")
                    if index > lineno:
                        break
        except (OSError, ValueError):
            return None
        return None


def _clip(line: str, limit: int = 500) -> str:
    """截断过长行，避免单行占满预算。"""
    text = line.rstrip("\r\n")
    if len(text) <= limit:
        return text
    half = max(80, (limit - 20) // 2)
    return f"{text[:half]} ... {text[-half:]}"


_OBJECT_START = re.compile(r"""^\s*(?:"[^"]+"|'[^']+')\s*:\s*\{\s*$""")


def _is_object_start(line: str) -> bool:
    """判断该行是否为 `"key": {` 形式的 JSON 对象起始行。"""
    return bool(_OBJECT_START.match(line))


def _json_block_end(lines: list[str], start: int, max_lines: int) -> Optional[int]:
    """
    从 start（0 基）的 `"key": {` 行出发，找到对应 `}` 的 0 基行号。

    使用花括号计数并跳过字符串字面量，避免 `"}"` 之类的干扰。
    超过 max_lines 或未闭合时返回 None。
    """
    depth = 0
    limit = min(len(lines), start + max_lines)
    for index in range(start, limit):
        line = lines[index]
        in_string = False
        escaped = False
        for ch in line:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return index
    return None


async def _run_git(args: list[str], cwd: Optional[Path] = None,
                   timeout: int = 300) -> str:
    """执行 git 命令并返回 stdout；失败时抛出 RuntimeError。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(10, timeout))
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"git 命令超时（{timeout}s）")
    if proc.returncode != 0:
        message = err.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message[:400] or f"git 退出码 {proc.returncode}")
    return out.decode("utf-8", errors="replace")
