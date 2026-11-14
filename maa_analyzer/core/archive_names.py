"""
日志压缩包命名识别 — 纯字符串规则，不依赖任何业务模块。

放在 core 层供 `core.models.AnalysisSettings` 与 `features.maa` 共用，
避免框架层反向依赖功能层。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UI_MXU = "MXU"
UI_MFAA = "MFAA"
UI_MAAFWAPP = "MaaFwApp"
UI_MTA = "MaaTauriAndroid"
UI_MWU = "MWU"
UI_MFW = "MFW-PyQt6"
UI_UNKNOWN = "Unknown"

# UI 类型 → project.ui 的键名
_UI_KEYS = {
    UI_MXU: "mxu",
    UI_MFAA: "mfaa",
    UI_MAAFWAPP: "maafwapp",
    UI_MTA: "mta",
    UI_MWU: "mwu",
    UI_MFW: "mfw",
}

# 时间戳兼容 20260814-135755 与 2026-08-14-13-57-55 两种写法
_TIMESTAMP_PATTERN = (
    r"(?:\d{8}[-_]\d{6}|\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2})"
)

# MXU: MaaAssistantKedrgame-logs-1.0.0-alpha.4-20260814-135755.zip
_MXU_FILE = re.compile(
    rf"^(?P<project>.+?)-logs?-(?P<version>.+?)-(?P<timestamp>{_TIMESTAMP_PATTERN})"
    r"(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# 无版本号的 MXU 导出: MaaXXX-logs-20260101-120000.zip / MaaXXX-logs-20260101.zip
_MXU_TIMESTAMP_FILE = re.compile(
    r"^(?P<project>.+?)[-_]logs?[-_](?P<timestamp>"
    r"\d{8}(?:[-_]\d{6})?"
    r"|\d{4}[-_]\d{2}[-_]\d{2}(?:[-_]\d{2}[-_]\d{2}[-_]\d{2})?"
    r")(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# 通用 MXU 兜底: MaaXXX-logs-1.0.0.zip
_MXU_FALLBACK_FILE = re.compile(
    r"^(?P<project>.+?)[-_]logs?[-_](?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*?)"
    r"(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# MFAAvalonia: log_20261019_110324.zip / log_20261019_141709-part01.zip
_MFAA_FILE = re.compile(
    rf"^(?:log|logs)[-_](?P<timestamp>{_TIMESTAMP_PATTERN})(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# MaaFwApp: maafw_logs_20261022_195657.zip
_MAAFWAPP_FILE = re.compile(
    rf"^(?:maafw|maafwapp|maa)[-_]logs?[-_](?P<timestamp>{_TIMESTAMP_PATTERN})"
    r"(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# MaaTauriAndroid: maa_tauri_android-logs-1760000000000-00000000-...-0000.zip
_MTA_FILE = re.compile(
    r"^maa_tauri_android-logs?-\d{10,16}"
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# MWU: /api/logs/export 固定返回 mwu-logs.zip
_MWU_FILE = re.compile(r"^mwu[-_]logs?\.zip$", re.IGNORECASE)
# MFW-PyQt6: 资源名-资源版本-YYYYMMDD_HHMMSS.zip；真实 UI 需再由 debug/gui.log 校验
_MFW_FILE = re.compile(
    r"^(?P<project>.+)-(?P<version>[^-]+)-(?P<timestamp>\d{8}_\d{6})"
    r"(?:[-_]part\d+)?\.zip$",
    re.IGNORECASE,
)
# 分卷: <base>-part01.zip
_PART_FILE = re.compile(r"^(?P<base>.+?)[-_]part(?P<part>\d{1,3})\.zip$", re.IGNORECASE)


@dataclass(frozen=True)
class ArchivePart:
    """分卷日志包信息。"""

    base: str
    number: int
    name: str = ""


def normalize_archive_name(file_name: str) -> str:
    """去掉路径，只保留文件名（兼容 Windows 反斜杠）。"""
    text = str(file_name or "").strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def _looks_like_version(value: str) -> bool:
    return bool(value) and bool(re.search(r"[A-Za-z.]", value))


def detect_ui_from_file_name(file_name: str) -> str:
    """仅根据文件名推断 UI；无法判断时返回空字符串。"""
    name = normalize_archive_name(file_name)
    if not name.lower().endswith(".zip"):
        return ""
    if _MTA_FILE.match(name):
        return UI_MTA
    if _MWU_FILE.match(name):
        return UI_MWU
    if _MFW_FILE.match(name):
        return UI_MFW
    if _MAAFWAPP_FILE.match(name):
        return UI_MAAFWAPP
    if _MFAA_FILE.match(name):
        return UI_MFAA
    match = _MXU_FILE.match(name)
    if match and _looks_like_version(match.group("version")):
        return UI_MXU
    if _MXU_TIMESTAMP_FILE.match(name):
        return UI_MXU
    match = _MXU_FALLBACK_FILE.match(name)
    if match and _looks_like_version(match.group("version")):
        return UI_MXU
    return ""


def is_known_log_archive_name(file_name: str) -> bool:
    """是否属于已知 UI 的日志包文件名。"""
    return bool(detect_ui_from_file_name(file_name))


# 文件名中带独立的 log / logs / 日志 片段（避免 catalog / dialog 之类的误判）
_LOG_NAME_HINT = re.compile(r"(?:^|[-_.\s])logs?(?:[-_.\s]|$)|日志", re.IGNORECASE)


def looks_like_log_archive_name(file_name: str) -> bool:
    """
    粗略判断文件名是否像日志包（下载前的初筛）。

    只要求名字里出现独立的 log / logs / 日志 片段，真正的判定
    由下载后的压缩包布局校验（`require_log_layout`）完成。
    """
    name = normalize_archive_name(file_name)
    if not name.lower().endswith(".zip"):
        return False
    if is_known_log_archive_name(name):
        return True
    return bool(_LOG_NAME_HINT.search(name[:-4]))


def ui_key_from_file_name(file_name: str) -> str:
    """按文件名推断 UI 配置键（mxu / mfaa / maafwapp / mta / mwu / mfw）；无法判断返回空串。"""
    return _UI_KEYS.get(detect_ui_from_file_name(file_name), "")


def ui_key_from_type(ui_type: str) -> str:
    """UI 类型 → 配置键；未知类型返回 auto。"""
    return _UI_KEYS.get(str(ui_type or ""), "auto")


def parse_archive_part(file_name: str) -> ArchivePart | None:
    """解析 `*-partNN.zip` 分卷名。"""
    name = normalize_archive_name(file_name)
    match = _PART_FILE.match(name)
    if not match:
        return None
    try:
        number = int(match.group("part"))
    except (TypeError, ValueError):
        return None
    return ArchivePart(base=match.group("base"), number=number, name=name)


def is_part_of(file_name: str, base: str) -> bool:
    """判断文件是否属于指定分卷组。"""
    part = parse_archive_part(file_name)
    return part is not None and part.base.lower() == str(base or "").strip().lower()


def mxu_file_parts(file_name: str) -> tuple[str, str] | None:
    """返回 MXU 文件名中的 (项目名, 版本号)；不匹配时返回 None。"""
    name = normalize_archive_name(file_name)

    match = _MXU_FILE.match(name)
    if match:
        version = match.group("version").strip(" -_")
        if _looks_like_version(version):
            return match.group("project").strip(" -_"), version

    match = _MXU_TIMESTAMP_FILE.match(name)
    if match:
        return match.group("project").strip(" -_"), ""

    match = _MXU_FALLBACK_FILE.match(name)
    if match:
        version = match.group("version").strip(" -_")
        if _looks_like_version(version):
            return match.group("project").strip(" -_"), version
    return None


def mfw_file_parts(file_name: str) -> tuple[str, str] | None:
    """返回 MFW 文件名中的 (资源名, 资源版本)；不匹配时返回 None。"""
    name = normalize_archive_name(file_name)
    match = _MFW_FILE.match(name)
    if not match:
        return None
    project = match.group("project").strip(" -_")
    version = match.group("version").strip(" -_")
    if not project:
        return None
    return project, version
