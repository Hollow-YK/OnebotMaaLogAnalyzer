"""
日志包索引与格式说明 — 不做内容摘要，只描述「这个包长什么样」。

设计原则：

  - **不提取日志正文**。分析阶段的模型一律通过工具（`log_list_files` /
    `log_search` / `log_read_file` / `log_list_images`）自行查看日志，
    因此这里只做一次廉价的中央目录索引：成员清单、UI 布局识别、版本信息、
    错误截图与配置文件的相对路径。
  - 发送给模型的是**固定格式的说明**：按自动识别出的 UI 布局解释每个目录
    与文件的作用，而不是把日志内容摘要成几段文字。
  - 索引同时供附件（`[附件@日志: …]`）、看图（`[看图@日志: …]`）与追问
    恢复使用，所以即使不生成说明也要能独立构造（`describe_zip`）。

解析失败一律降级为「未知 UI + 通用说明」，绝不抛异常。
"""
from __future__ import annotations

import fnmatch
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from .archive_profile import (
    UI_MAAFWAPP,
    UI_MFAA,
    UI_MFW,
    UI_MTA,
    UI_MWU,
    UI_MXU,
    UI_UNKNOWN,
    ArchiveProfile,
    detect_archive_profile,
)
from .safety import is_sensitive_path, normalize_tool_path
from .text_utils import format_bytes

logger = logging.getLogger("Maa.Archive")

LOG_SUFFIXES = (".log", ".jsonl")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# 文件名中的日期（2026-11-14 / 2026.11.14），可带时间
_DATE_PREFIX = re.compile(r"\d{4}[-.]\d{2}[-.]\d{2}")
_DATE_STAMP = re.compile(
    r"(\d{4})[-.](\d{2})[-.](\d{2})(?:[-_.](\d{2})[-.](\d{2})[-.](\d{2}))?"
)

# 优先展示的日志文件数（框架日志 / UI 日志 / 运行记录等）
PRIMARY_LOG_LIMIT = 6
# 单个 maafw.bak 参与优先展示的数量与大小上限
MAX_BAK_FILES = 1
MAX_BAK_BYTES = 64 * 1024 * 1024
# 配置文件中列出条数上限
CONFIG_LIST_LIMIT = 8
# 错误截图 / 自定义截图列出数量
IMAGE_LIST_LIMIT = 20
# 格式说明里逐条列出的成员行数上限
MEMBER_LIST_LIMIT = 240
# 同一目录下逐条列出的成员数
MEMBER_PER_DIR_LIMIT = 12


@dataclass
class ArchiveInfo:
    """日志包的静态索引（不含任何日志正文）。"""

    file_count: int = 0
    total_uncompressed_size: int = 0
    # 压缩包内全部成员（相对路径，已过滤不安全条目）
    all_members: list[str] = field(default_factory=list)
    all_images: list[str] = field(default_factory=list)
    # 本次优先查看的日志文件（按 UI 布局排序）
    log_files: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    # `on_error/` 下的失败截图
    error_images: list[str] = field(default_factory=list)
    # `custom/` 下的自定义识别截图
    custom_images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 报告与提示词使用的项目名（project.name > 压缩包解析 > MaaXXX）
    project_name: str = ""
    # 是否像日志包（有日志文件 / 已知 UI 布局 / 错误截图）
    is_log_archive: bool = True
    # 自动识别的 UI / 软件版本 / MaaFramework 版本
    profile: Optional[ArchiveProfile] = None
    zip_path: str = ""
    # 已解压目录（存在时日志工具直接复用，避免反复解压）
    extract_dir: str = ""

    # ── 说明文本 ──

    def format_prompt(self, *, original_file_name: str = "",
                      group_id: str | int | None = None) -> str:
        """生成发送给模型的**固定格式说明**（不含日志内容）。"""
        project = str(self.project_name or "").strip() or "MaaXXX"
        display_name = str(original_file_name or "").strip()
        if not display_name:
            display_name = PurePosixPath(str(self.zip_path or "")).name or "日志包"

        parts: list[str] = [
            f"下面是 {project} 日志包的**结构说明**（不含日志内容摘要）。"
            "请用工具自行查看日志后再给出分析结论。",
            "",
            f"文件名：{display_name}",
            f"上传群：{group_id or '未知'}",
            f"压缩包文件数：{self.file_count}",
            f"解压后总大小：{format_bytes(self.total_uncompressed_size)}",
            "",
            "运行环境（自动解析）：",
        ]
        lines = (
            self.profile.prompt_lines() if self.profile is not None else []
        )
        if lines:
            parts.extend(f"- {line}" for line in lines)
        else:
            parts.append("- 未能识别 UI 与版本信息")

        parts.extend(["", f"日志包格式（自动识别为 {self._layout_name()} 布局）："])
        names = {name.lower() for name in self.all_members}
        for kind, pattern, description in self._layout_entries():
            mark = "✔" if _matches(kind, pattern, names) else "—"
            parts.append(f"- [{mark}] {pattern} — {description}")

        parts.extend(["", self._member_overview()])

        if self.error_images:
            parts.extend(["", f"失败截图（on_error/，共 {len(self.error_images)} 张）："])
            parts.extend(f"- {name}" for name in self.error_images[:IMAGE_LIST_LIMIT])
        if self.custom_images:
            parts.extend([
                "",
                f"自定义识别截图（custom/，共 {len(self.custom_images)} 张）：",
            ])
            parts.extend(f"- {name}" for name in self.custom_images[:IMAGE_LIST_LIMIT])
        if self.config_files:
            parts.extend(["", "配置文件（可用 log_read_file 直接读取）："])
            parts.extend(f"- {name}" for name in self.config_files[:CONFIG_LIST_LIMIT])

        parts.extend(["", *_TOOL_SECTION])
        if self.notes:
            parts.extend(["", "处理备注："])
            parts.extend(f"- {note}" for note in self.notes)
        return "\n".join(parts)

    # ── 内部渲染 ──

    def _layout_name(self) -> str:
        if self.profile is not None and self.profile.ui_name:
            return self.profile.ui_name
        return "未知 UI"

    def _layout_entries(self) -> list[tuple[str, str, str]]:
        ui = self.profile.ui_type if self.profile is not None else UI_UNKNOWN
        return _LAYOUTS.get(ui, _LAYOUTS[UI_UNKNOWN])

    def _member_overview(self) -> str:
        """按顶层目录分组列出实际成员（限量）。"""
        groups: dict[str, list[str]] = {}
        for name in sorted(self.all_members):
            top = name.split("/", 1)[0] if "/" in name else ""
            key = f"{top}/" if top else ""
            groups.setdefault(key, []).append(name)

        primary = list(dict.fromkeys(self.log_files))
        lines = [f"包内实际内容（共 {len(self.all_members)} 个文件）："]
        if primary:
            lines.append("优先查看的日志：")
            lines.extend(f"- {name}" for name in primary)

        remaining = MEMBER_LIST_LIMIT - len(primary)
        # 根目录排最前，其余按名称
        for key in sorted(groups, key=lambda item: (item != "", item)):
            if remaining <= 0:
                lines.append("…（清单已截断，请用 log_list_files 查看完整目录）")
                break
            names = groups[key]
            label = key or "（根目录）"
            lines.append(f"- {label}（{len(names)} 项）")
            shown = names[:MEMBER_PER_DIR_LIMIT]
            for name in shown:
                if remaining <= 0:
                    break
                remaining -= 1
                leaf = name.split("/", 1)[1] if "/" in name else name
                lines.append(f"  - {leaf}")
            hidden = len(names) - len(shown)
            if hidden > 0:
                lines.append(f"  - …另有 {hidden} 个同类文件")
        return "\n".join(lines)


def build_archive_info(
    zip_path: str,
    *,
    original_file_name: str = "",
    group_id: str | int | None = None,
    project_name: str = "MaaXXX",
    extra_notes: Optional[list[str]] = None,
) -> ArchiveInfo:
    """
    索引一个日志压缩包：成员清单、UI 布局、版本信息、截图与配置路径。

    只读 zip 中央目录（外加少量识别用成员片段），不提取日志正文。
    压缩包不可读时返回空索引（`file_count=0`）并在 notes 中说明。
    """
    notes: list[str] = [str(note) for note in (extra_notes or []) if note]
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        notes.append(f"压缩包无法读取（{type(exc).__name__}），无法列出包内文件。")
        return ArchiveInfo(notes=notes, zip_path=str(zip_path))

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        total = sum(max(0, int(info.file_size)) for info in infos)
        safe_infos = [info for info in infos if _is_safe_member(info.filename)]
        if len(safe_infos) != len(infos):
            notes.append(
                "压缩包中存在不安全路径（绝对路径 / `..` / 敏感文件），已跳过。"
            )
        profile = detect_archive_profile(
            zip_path,
            file_name=original_file_name or PurePosixPath(zip_path).name,
            archive=archive,
            infos=safe_infos,
        )
    notes.extend(profile.notes)

    members = [info.filename.replace("\\", "/") for info in safe_infos]
    info = ArchiveInfo(
        file_count=len(infos),
        total_uncompressed_size=total,
        all_members=members,
        all_images=_list_images(members),
        log_files=[
            m.filename.replace("\\", "/")
            for m in _select_primary_logs(safe_infos, profile, notes)
        ],
        config_files=[
            m.filename.replace("\\", "/") for m in _select_config_infos(safe_infos)
        ],
        error_images=_select_error_images(members),
        custom_images=_select_custom_images(members),
        notes=notes,
        project_name=_resolve_project_name(project_name, profile),
        is_log_archive=_looks_like_log_archive(profile, safe_infos, members),
        profile=profile,
        zip_path=str(zip_path),
    )
    logger.debug(
        "[索引] %s：%s 个成员，UI=%s，日志 %s 个，截图 %s 张",
        PurePosixPath(zip_path).name,
        info.file_count,
        profile.ui_name,
        len(info.log_files),
        len(info.error_images),
    )
    return info


def describe_zip(zip_path: str) -> Optional[ArchiveInfo]:
    """
    只索引压缩包内容（不生成说明文本）。

    用于重启后从历史归档恢复追问会话：此时只需知道包内有哪些成员
    （供 `[附件@日志: ...]` 与日志工具使用），无需重新构造说明。
    压缩包不存在或不可读时返回 None。
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            safe_infos = [info for info in infos if _is_safe_member(info.filename)]
            profile = detect_archive_profile(
                zip_path,
                archive=archive,
                infos=safe_infos,
            )
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None

    members = [info.filename.replace("\\", "/") for info in safe_infos]
    notes: list[str] = []
    return ArchiveInfo(
        file_count=len(infos),
        total_uncompressed_size=sum(max(0, int(info.file_size)) for info in infos),
        all_members=members,
        all_images=_list_images(members),
        log_files=[
            m.filename.replace("\\", "/")
            for m in _select_primary_logs(safe_infos, profile, notes)
        ],
        config_files=[
            m.filename.replace("\\", "/") for m in _select_config_infos(safe_infos)
        ],
        error_images=_select_error_images(members),
        custom_images=_select_custom_images(members),
        notes=notes + profile.notes,
        project_name=profile.project_name,
        is_log_archive=_looks_like_log_archive(profile, safe_infos, members),
        profile=profile,
        zip_path=str(zip_path),
    )


def read_zip_member(zip_path: str, member_name: str,
                    max_bytes: int = 0) -> Optional[bytes]:
    """
    从压缩包中读取任意成员的字节，供发送到群聊 / 看图使用。

    超过 max_bytes 或读取失败时返回 None。
    """
    if not zip_path or not member_name:
        return None
    wanted = normalize_tool_path(member_name)
    if not wanted or is_sensitive_path(wanted):
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if not _is_safe_member(info.filename):
                    continue
                if normalize_tool_path(info.filename) != wanted:
                    continue
                if max_bytes > 0 and info.file_size > max_bytes:
                    return None
                return archive.read(info)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return None
    return None


# ════════════════════════════════════════════════════════════════
# 成员筛选
# ════════════════════════════════════════════════════════════════

def _is_safe_member(name: str) -> bool:
    normalized = normalize_tool_path(name)
    if not normalized:
        return False
    return not is_sensitive_path(name) and not is_sensitive_path(normalized)


def _is_image(name: str) -> bool:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower() in IMAGE_SUFFIXES


def _looks_like_log_archive(
    profile: Optional[ArchiveProfile],
    infos: list[zipfile.ZipInfo],
    members: list[str],
) -> bool:
    """
    判断压缩包是否像日志包。

    满足任一条件即可：包含日志文件、包含错误/自定义截图、或布局命中已知 UI。
    仅靠文件名的 UI 推断**不算**，避免误判普通 zip。
    """
    for info in infos:
        if PurePosixPath(info.filename).suffix.lower() in LOG_SUFFIXES:
            return True
    if _select_error_images(members) or _select_custom_images(members):
        return True
    return profile is not None and profile.layout_detected


def _list_images(members: list[str]) -> list[str]:
    """压缩包内全部图片（含 on_error 截图与资源图）。"""
    return sorted(name for name in members if _is_image(name))


def _is_on_error_image(name: str) -> bool:
    parts = PurePosixPath(name.replace("\\", "/").lower()).parts
    return "on_error" in parts[:-1]


def _is_custom_image(name: str) -> bool:
    parts = PurePosixPath(name.replace("\\", "/").lower()).parts
    return "custom" in parts[:-1]


def _select_error_images(members: list[str]) -> list[str]:
    images = [name for name in members if _is_image(name) and _is_on_error_image(name)]
    return sorted(images, reverse=True)[:IMAGE_LIST_LIMIT]


def _select_custom_images(members: list[str]) -> list[str]:
    """debug/custom 等目录下的自定义截图（非 on_error），最新优先。"""
    images = [
        name for name in members
        if _is_image(name) and _is_custom_image(name) and not _is_on_error_image(name)
    ]
    return sorted(images, reverse=True)[:IMAGE_LIST_LIMIT]


def _select_config_infos(infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    wanted = []
    for info in infos:
        name = info.filename.replace("\\", "/").lower()
        if name in {"config/maa_option.json", "config/pip_config.json", "device_info.txt"}:
            wanted.append(info)
        elif name.startswith("config/") and name.endswith((".json", ".jsonc")):
            wanted.append(info)
    return sorted(wanted, key=lambda item: item.filename.lower())[:CONFIG_LIST_LIMIT]


def _select_primary_logs(
    infos: list[zipfile.ZipInfo],
    profile: Optional[ArchiveProfile] = None,
    notes: Optional[list[str]] = None,
) -> list[zipfile.ZipInfo]:
    """
    按 UI 布局挑选优先查看的日志（框架日志 / UI 日志 / 运行记录 / 备份）。

    这里只决定「说明里先指向哪几个文件」，模型仍可用 `log_list_files`
    查看全部成员；因此数量上限只是展示顺序，而不是可分析范围。
    """
    ui = profile.ui_type if profile is not None else UI_UNKNOWN
    notes = notes if notes is not None else []

    logs = [
        info for info in infos
        if info.filename.lower().endswith(LOG_SUFFIXES)
    ]
    buckets: dict[str, list[zipfile.ZipInfo]] = {
        key: [] for key in (
            "maafw", "bak", "app", "dated", "agent",
            "custom", "run", "schedule", "debug", "other",
        )
    }

    for info in logs:
        name = info.filename.replace("\\", "/")
        lower = name.lower()
        base = PurePosixPath(lower).name

        if base == "maafw.log":
            role = "maafw"
        elif base.startswith("maafw.bak"):
            role = "bak"
        elif base.startswith("mxu-agent-"):
            role = "agent"
        elif "custom" in lower:
            role = "custom"
        elif ui == UI_MXU and _DATE_PREFIX.match(base):
            role = "dated"
        elif ui == UI_MFAA and lower.startswith("logs/") and base.startswith("log-"):
            role = "app"
        elif ui == UI_MFAA and lower.startswith("logs/") and base.endswith(".log"):
            role = "app"
        elif ui == UI_MTA and lower in {
            "logs/maa_tauri_android-filtered.log", "logs/app/mta.log",
        }:
            role = "app"
        elif ui == UI_MTA and lower.startswith("logs/runs/") and base.endswith(".jsonl"):
            role = "run"
        elif ui == UI_MTA and lower in {"logs/logcat-full.txt", "logs/properties.txt"}:
            role = "debug"
        elif ui == UI_MWU and lower == "mwu.log":
            role = "app"
        elif ui == UI_MWU and lower == "updater.log":
            role = "debug"
        elif ui == UI_MFW and lower == "debug/gui.log":
            role = "app"
        elif ui == UI_MFW and lower == "debug/crash.log":
            role = "debug"
        elif ui == UI_MAAFWAPP and lower == "log/app.log":
            role = "app"
        elif ui == UI_MAAFWAPP and lower.startswith("log/run/") and base.endswith(".jsonl"):
            role = "run"
        elif ui == UI_MAAFWAPP and (lower.startswith("debug/") or "logcat" in lower):
            role = "debug"
        elif _DATE_PREFIX.match(base):
            role = "dated"
        elif lower.startswith("logs/") and base.startswith("log-"):
            role = "app"
        elif base == "app.log":
            role = "app"
        elif base.endswith(".jsonl"):
            role = "run"
        elif lower.startswith("debug/"):
            role = "debug"
        elif base in {"schedule-trigger.log", "schedule.log"}:
            role = "schedule"
        else:
            role = "other"
        buckets[role].append(info)

    deduped: list[zipfile.ZipInfo] = []
    seen: set[str] = set()

    def add_info(info: Optional[zipfile.ZipInfo]) -> bool:
        if not info or len(deduped) >= PRIMARY_LOG_LIMIT:
            return False
        key = info.filename.replace("\\", "/").lower()
        if key in seen:
            return False
        seen.add(key)
        deduped.append(info)
        return True

    def newest(items: list[zipfile.ZipInfo], count: int = 1) -> list[zipfile.ZipInfo]:
        return sorted(items, key=_log_recency_key, reverse=True)[: max(0, count)]

    # 1. 框架日志：MaaFwApp 同时保留 UI 侧与 Agent 侧两份 maafw.log
    maafw_logs = buckets["maafw"]
    if ui == UI_MAAFWAPP:
        ordered = sorted(
            maafw_logs,
            key=lambda info: (
                0 if info.filename.replace("\\", "/").lower() == "log/maafw.log" else 1,
                _log_recency_key(info),
            ),
        )
        for info in ordered[:2]:
            add_info(info)
    elif maafw_logs:
        add_info(max(maafw_logs, key=_log_recency_key))

    # 2. UI 主日志
    for info in newest(buckets["app"], 1):
        add_info(info)

    # 3. 运行记录 / Agent / 自定义截图日志（按 UI 调整优先级）
    role_plan: tuple[tuple[str, int], ...]
    if ui == UI_MXU:
        role_plan = (("dated", 1), ("agent", 1), ("custom", 1), ("run", 1))
    elif ui == UI_MFW:
        role_plan = (("schedule", 1), ("custom", 1), ("run", 1),
                     ("agent", 1), ("dated", 1))
    else:
        # 定时任务触发记录往往直接包含 FAILED_START / INVALID_PLAN，优先保留
        role_plan = (("schedule", 1), ("run", 1), ("custom", 1),
                     ("agent", 1), ("dated", 1))
    for role, count in role_plan + (("debug", 1),):
        for info in newest(buckets[role], count):
            add_info(info)

    # 4. 备份日志（受数量与单文件大小限制，避免误列数百 MB 的 maafw.bak）
    backup_limit = MAX_BAK_FILES
    for info in sorted(buckets["bak"], key=_log_recency_key, reverse=True):
        if backup_limit <= 0 or len(deduped) >= PRIMARY_LOG_LIMIT:
            break
        if int(info.file_size) > MAX_BAK_BYTES:
            notes.append(
                f"{info.filename} 超过 {format_bytes(MAX_BAK_BYTES)}，"
                "已从优先日志中跳过（仍可用 log_read_file 按行读取）。"
            )
            continue
        if add_info(info):
            backup_limit -= 1

    # 5. 剩余日志按时间补齐
    remaining = [
        info for role, items in buckets.items() if role != "bak"
        for info in items
    ]
    for info in sorted(remaining, key=_log_recency_key, reverse=True):
        add_info(info)

    return deduped


def _log_recency_key(info: zipfile.ZipInfo) -> tuple[tuple[int, int, int, int, int, int], str]:
    name = info.filename.replace("\\", "/")
    base = PurePosixPath(name).name
    parsed_time = _parse_log_time_from_name(base)
    return parsed_time or info.date_time, name.lower()


def _parse_log_time_from_name(base: str) -> Optional[tuple[int, int, int, int, int, int]]:
    match = _DATE_STAMP.search(base)
    if not match:
        return None
    year, month, day = (int(match.group(index)) for index in range(1, 4))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    return year, month, day, hour, minute, second


def _resolve_project_name(
    project_name: str,
    profile: Optional[ArchiveProfile],
) -> str:
    """
    解析报告/提示词使用的项目名。

    优先级：`project.name` > 压缩包解析出的项目名 > MaaXXX。
    """
    configured = str(project_name or "").strip()
    if configured:
        return configured
    archive_name = (
        str(profile.project_name or "").strip() if profile is not None else ""
    )
    return archive_name or "MaaXXX"


# ════════════════════════════════════════════════════════════════
# 布局说明
# ════════════════════════════════════════════════════════════════

# (类型, 路径/文件名模式, 说明)
#   file: 精确相对路径；dir: 以该前缀开头的目录；glob: fnmatch 模式
_LAYOUTS: dict[str, list[tuple[str, str, str]]] = {
    UI_MXU: [
        ("file", "maafw.log",
         "MaaFramework 运行日志（Tasker / Controller / Recognizer / Action）"),
        ("glob", "*.log", "按日期轮转的历史运行日志（如 2026-11-14.log）"),
        ("glob", "mxu-agent-*.log", "Agent 进程日志（自定义识别 / 动作）"),
        ("dir", "on_error/", "识别或动作失败时的现场截图"),
        ("dir", "custom/", "自定义识别 / 动作的调试截图"),
        ("dir", "config/", "MXU 配置（mxu-<项目>.json：任务与选项）"),
        ("dir", "debug/", "调试输出"),
    ],
    UI_MFAA: [
        ("file", "debug/maafw.log",
         "MaaFramework 运行日志（Tasker / Controller / Recognizer / Action）"),
        ("glob", "logs/log-*.log",
         "MFAAvalonia UI 日志（含程序版本、资源版本、MaaFramework 版本）"),
        ("dir", "debug/on_error/", "识别或动作失败时的现场截图"),
        ("dir", "debug/custom/", "自定义识别 / 动作的调试截图"),
        ("file", "config/maa_option.json", "运行选项"),
        ("file", "config/pip_config.json", "安装 / 依赖配置"),
        ("dir", "debug/", "调试输出"),
    ],
    UI_MAAFWAPP: [
        ("file", "log/maafw.log", "MaaFramework 运行日志（UI 侧）"),
        ("file", "pi/debug/maafw.log", "内嵌 PI / Agent 侧运行日志"),
        ("file", "log/app.log", "应用日志（启动信息、版本、控制器）"),
        ("glob", "log/run/*.jsonl", "每次任务运行的记录（含节点流水）"),
        ("glob", "logcat*.txt", "设备 logcat"),
        ("dir", "debug/on_error/", "识别或动作失败时的现场截图"),
        ("dir", "debug/custom/", "自定义识别 / 动作的调试截图"),
        ("dir", "config/", "配置文件"),
        ("file", "device_info.txt", "设备与应用信息"),
        ("dir", "debug/", "调试输出"),
    ],
    UI_MTA: [
        ("file", "logs/maa/maafw.log", "MaaFramework 运行日志"),
        ("file", "logs/maa_tauri_android-filtered.log", "UI 过滤后的运行日志"),
        ("file", "logs/app/mta.log", "应用日志"),
        ("glob", "logs/runs/*.jsonl", "每次任务运行的记录"),
        ("file", "logs/logcat-full.txt", "设备 logcat"),
        ("file", "logs/properties.txt", "设备属性"),
        ("file", "snapshots/project.json", "项目快照（资源名与版本）"),
        ("file", "device-info.txt", "设备与应用信息"),
        ("dir", "on_error/", "识别或动作失败时的现场截图"),
        ("dir", "custom/", "自定义识别的调试截图"),
    ],
    UI_MWU: [
        ("file", "mwu.log", "MWU 主日志"),
        ("file", "debug/maafw.log", "MaaFramework 运行日志"),
        ("file", "updater.log", "更新器日志"),
        ("dir", "on_error/", "识别或动作失败时的现场截图"),
        ("dir", "debug/", "调试输出"),
    ],
    UI_MFW: [
        ("file", "debug/gui.log", "MFW-PyQt6 UI 日志（含资源版本、工作目录）"),
        ("file", "debug/maafw.log", "MaaFramework 运行日志"),
        ("file", "debug/crash.log", "崩溃日志"),
        ("dir", "debug/", "调试输出与识别日志"),
        ("dir", "live/", "运行期截图（live/*.png）"),
        ("dir", "on_error/", "识别或动作失败时的现场截图"),
    ],
    UI_UNKNOWN: [
        ("glob", "*.log", "运行日志"),
        ("glob", "*.jsonl", "运行记录"),
        ("dir", "on_error/", "识别或动作失败时的现场截图"),
        ("dir", "custom/", "自定义识别的调试截图"),
        ("dir", "config/", "配置文件"),
        ("dir", "debug/", "调试输出"),
    ],
}

_TOOL_SECTION = [
    "可用工具（只读，路径均相对日志包根目录）：",
    "- log_list_files  path            列出目录内容（省略 path 列出根目录）",
    "- log_search      pattern [path]  在文本文件中按正则搜索并返回行号",
    "- log_read_file   path [行范围]    按行读取文本文件（返回带行号的片段）",
    "- log_list_images                 列出全部图片及其相对路径",
    "",
    "工作方式：",
    "1. 先按上面的布局与文件清单确定该看哪个日志；不确定时用 log_list_files。",
    r"2. 用 log_search 定位关键证据（如 `ERROR|Failed|Recognition\.Failed|"
    r"截图失败|拒绝访问|Exception`），再用 log_read_file 读命中位置前后 80 行。",
    "3. 日志很大或结果被截断时，换更精确的 pattern 或用行范围分段读，"
    "不要因为读不全就放弃。",
    "4. 需要图片证据时报告里用 `[附件@日志: 相对路径]`；想自己看图用 "
    "`[看图@日志: 相对路径]`。",
    "5. 只依据工具返回的内容与截图下结论；证据不足就说“不确定”并说明还缺什么。",
]


def _matches(kind: str, pattern: str, names: set[str]) -> bool:
    lowered = pattern.lower()
    if kind == "dir":
        return any(name.startswith(lowered) for name in names)
    if kind == "glob":
        return any(fnmatch.fnmatchcase(name, lowered) for name in names)
    return lowered in names
