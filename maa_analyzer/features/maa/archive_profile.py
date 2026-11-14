"""
日志压缩包识别 — UI 类型、软件版本、MaaFramework 版本。

不同 MaaFramework UI 导出的日志包布局与文件名差异很大：

  - MXU：`<项目>-logs-<版本>-<时间戳>.zip`
    根目录 `maafw.log`、`on_error/`、`config/mxu-*.json`、`mxu-agent-*.log`
  - MFAAvalonia：`log_<时间戳>.zip`（分卷为 `log_<时间戳>-partNN.zip`）
    `debug/maafw.log`、`logs/log-YYYYMMDD.log`、`debug/on_error/`
  - MaaFwApp（Android）：`maafw_logs_<时间戳>.zip`
    `log/maafw.log`、`log/app.log`、`device_info.txt`、`pi/debug/maafw.log`
  - MWU：`mwu-logs.zip`，`mwu.log` + `debug/maafw.log` + `updater.log`
  - MFW-PyQt6：`<资源名>-<资源版本>-<时间戳>.zip`，`debug/gui.log` + `debug/maafw.log`

本模块只做只读识别：读取少量元数据文件，不修改压缩包。
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ...core.archive_names import (
    UI_MAAFWAPP,
    UI_MFAA,
    UI_MTA,
    UI_MWU,
    UI_MFW,
    UI_MXU,
    UI_UNKNOWN,
    ArchivePart,
    detect_ui_from_file_name,
    is_known_log_archive_name,
    is_part_of,
    mfw_file_parts,
    mxu_file_parts,
    normalize_archive_name,
    parse_archive_part,
    ui_key_from_type,
)

from .text_utils import decode_bytes

__all__ = [
    "ArchivePart",
    "ArchiveProfile",
    "UI_DISPLAY_NAMES",
    "UI_MAAFWAPP",
    "UI_MFAA",
    "UI_MTA",
    "UI_MWU",
    "UI_MFW",
    "UI_MXU",
    "UI_UNKNOWN",
    "detect_archive_profile",
    "detect_ui_from_file_name",
    "is_known_log_archive_name",
    "is_part_of",
    "normalize_archive_name",
    "parse_archive_part",
]

UI_DISPLAY_NAMES = {
    UI_MXU: "MXU",
    UI_MFAA: "MFAAvalonia",
    UI_MAAFWAPP: "MaaFwApp",
    UI_MTA: "MaaTauriAndroid",
    UI_MWU: "MWU",
    UI_MFW: "MFW-PyQt6",
    UI_UNKNOWN: "未知 UI",
}

# ── 元数据解析 ────────────────────────────────────────────────
_KEY_VALUE_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _.\-]*?)\s*:\s*(.+?)\s*$")
_MAFW_LOG_VERSION = re.compile(r"\bVersion\s+([vV]?\d[^\s]*)")
_MAFW_LOG_OS = re.compile(r"\b(Windows|Linux|macOS|Darwin)\s+([^\s]+)")
_ANDROID_PACKAGE = re.compile(r"Android/data/([^/\s]+)/", re.IGNORECASE)

_MFAA_UI_VERSION = re.compile(r"程序版本\s*[:：]\s*([^\s，,]+)")
_MFAA_RESOURCE_VERSION = re.compile(r"资源版本\s*[:：]\s*([^\s，,]+)")
_MFAA_FRAMEWORK_VERSION = re.compile(r"MaaFramework\s*版本\s*[:：]\s*([^\s，,]+)")
_MFAA_INSTALL_DIR = re.compile(r"应用安装目录\s*[:：]\s*(.+)")
_MFAA_RUNTIME = re.compile(r"\.NET\s*运行时\s*[:：]\s*(.+)")
_MFW_UI_VERSION = re.compile(r"\bMFW\s*版本\s*[:：]\s*([^\s]+)")
_MFW_RESOURCE_VERSION = re.compile(r"资源版本\s*[:：]\s*([^\s]+)")
_MFW_WORKING_DIR = re.compile(r"当前工作目录\s*[:：]\s*(.+)")

_METADATA_LABELS = {
    "export time": "导出时间",
    "built at": "构建时间",
    "working": "工作目录",
    "logging": "日志路径",
    "abi": "ABI",
    "screen": "屏幕",
    "ram": "内存",
    "storage": "存储",
    "security": "安全补丁",
    "battery opt": "电池优化",
    "selinux": "SELinux",
    ".net runtime": ".NET",
    "startup time": "启动时间",
}


@dataclass
class ArchiveProfile:
    """从日志压缩包自动解析出的运行环境信息。"""

    ui_type: str = UI_UNKNOWN
    # UI 是否由压缩包布局（而非文件名）确认
    layout_detected: bool = False
    ui_version: str = ""
    module_version: str = ""
    resource_version: str = ""
    framework_version: str = ""
    project_name: str = ""
    package_name: str = ""
    device: str = ""
    os_version: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ui_name(self) -> str:
        return UI_DISPLAY_NAMES.get(self.ui_type, self.ui_type or UI_DISPLAY_NAMES[UI_UNKNOWN])

    @property
    def ui_key(self) -> str:
        """project.ui 的键名（mxu / mfaa / maafwapp / mta / mwu / mfw / auto）。"""
        return ui_key_from_type(self.ui_type)

    @property
    def repo_version(self) -> str:
        """可用于对齐项目仓库 tag 的版本号。"""
        return str(self.resource_version or "").strip()

    def version_summary(self) -> str:
        """逐行列出的版本摘要，用于文本和图片报告头。"""
        parts = [self.ui_name]
        if self.ui_version:
            parts.append(f"软件版本：{self.ui_version}")
        if self.module_version:
            parts.append(f"MaaFwApp：{self.module_version}")
        if self.resource_version and self.resource_version != self.ui_version:
            parts.append(f"资源版本：{self.resource_version}")
        if self.framework_version:
            parts.append(f"MaaFramework：{self.framework_version}")
        if self.device:
            parts.append(f"设备：{self.device}")
        if self.os_version:
            parts.append(f"系统：{self.os_version}")
        return "\n".join(parts)

    def prompt_lines(self) -> list[str]:
        """提示词中的运行环境段落。"""
        lines = [f"UI 类型：{self.ui_name}（自动识别）"]
        if self.ui_version:
            lines.append(f"软件版本：{self.ui_version}")
        if self.module_version:
            lines.append(f"MaaFwApp 模块版本：{self.module_version}")
        if self.resource_version:
            lines.append(f"项目/资源版本：{self.resource_version}")
        if self.framework_version:
            lines.append(f"MaaFramework 版本：{self.framework_version}")
        if self.package_name:
            lines.append(f"应用包名：{self.package_name}")
        if self.device:
            lines.append(f"设备：{self.device}")
        if self.os_version:
            lines.append(f"系统：{self.os_version}")
        for key, label in _METADATA_LABELS.items():
            value = self.metadata.get(key)
            if value and key not in {"working", "logging"}:
                lines.append(f"{label}：{value}")
        return lines


# ════════════════════════════════════════════════════════════════
# 压缩包识别
# ════════════════════════════════════════════════════════════════


def detect_archive_profile(
    zip_path: str,
    *,
    file_name: str = "",
    archive: zipfile.ZipFile | None = None,
    infos: list[zipfile.ZipInfo] | None = None,
) -> ArchiveProfile:
    """
    读取压缩包布局与少量元数据文件，识别 UI / 软件版本 / MaaFramework 版本。

    任何解析失败都只记录 note，不抛出异常。
    """
    profile = ArchiveProfile()
    name = normalize_archive_name(file_name)
    file_ui = detect_ui_from_file_name(name)

    try:
        if archive is not None:
            return _detect_profile(
                profile, archive, infos if infos is not None else archive.infolist(), name, file_ui
            )
        with zipfile.ZipFile(zip_path) as opened:
            return _detect_profile(profile, opened, opened.infolist(), name, file_ui)
    except Exception as exc:
        profile.ui_type = file_ui or UI_UNKNOWN
        _apply_file_name(profile, name)
        profile.notes.append(f"压缩包无法读取，未能完整解析 UI/版本信息：{exc}")
        return profile


def _detect_profile(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    name: str,
    file_ui: str,
) -> ArchiveProfile:
    members = [info for info in infos if not info.is_dir()]
    names = [info.filename.replace("\\", "/") for info in members]
    lower_names = [item.lower() for item in names]

    layout_ui = _detect_ui_from_layout(lower_names)
    profile.layout_detected = bool(layout_ui)
    profile.ui_type = layout_ui or file_ui or UI_UNKNOWN
    _apply_file_name(profile, name)

    if profile.ui_type == UI_MTA:
        _parse_mta_device_info(profile, archive, members, lower_names)
        _parse_mta_project_snapshot(profile, archive, members, lower_names)
    elif profile.ui_type == UI_MAAFWAPP:
        _parse_device_info(profile, archive, members, lower_names)
        _parse_maafwapp_app_log(profile, archive, members, lower_names)
    elif profile.ui_type == UI_MFAA:
        _parse_mfaa_app_log(profile, archive, members, lower_names)
    elif profile.ui_type == UI_MXU:
        _parse_mxu_config_name(profile, lower_names)
    if profile.ui_type == UI_MFW:
        _parse_mfw_gui_log(profile, archive, members, lower_names)

    # 所有 UI 都尽量从 maafw.log 头部补齐框架版本
    _parse_maafw_header(profile, archive, members, lower_names)
    return profile


def _detect_ui_from_layout(lower_names: list[str]) -> str:
    name_set = set(lower_names)
    # MaaTauriAndroid 的导出有多个专属标记；`device-info.txt` 不能单独用它
    # 判定，否则会与 MaaFwApp 同名文件混淆。
    mta_markers = {
        "logs/maa_tauri_android-filtered.log",
        "logs/app/mta.log",
        "logs/runs/index.json",
        "snapshots/settings.json",
        "snapshots/project.json",
    }
    if any(name in mta_markers for name in lower_names):
        return UI_MTA
    if "mwu.log" in name_set:
        return UI_MWU
    if "debug/gui.log" in name_set:
        return UI_MFW
    if "device_info.txt" in name_set or "log/app.log" in name_set:
        return UI_MAAFWAPP
    if any(name.startswith("config/mxu-") for name in lower_names):
        return UI_MXU
    if any(PurePosixPath(name).name.startswith("mxu-agent-") for name in lower_names):
        return UI_MXU
    if any(name.startswith("logs/log-") for name in lower_names):
        return UI_MFAA
    if "debug/maafw.log" in name_set:
        return UI_MFAA
    if "log/maafw.log" in name_set or "pi/debug/maafw.log" in name_set:
        return UI_MAAFWAPP
    if "maafw.log" in name_set:
        return UI_MXU
    if any(name.startswith("logs/") and name.endswith(".log") for name in lower_names):
        return UI_MFAA
    return ""


def _apply_file_name(profile: ArchiveProfile, name: str) -> None:
    """从文件名补齐项目名 / 资源版本（MXU / MFW 命名）。"""
    if not name or profile.ui_type not in (UI_MXU, UI_MFW, UI_UNKNOWN):
        return

    parts = mfw_file_parts(name) if profile.ui_type == UI_MFW else mxu_file_parts(name)
    if parts is None:
        return
    project, version = parts
    if version and not profile.resource_version:
        profile.resource_version = version
    if project and not profile.project_name:
        profile.project_name = project


# ════════════════════════════════════════════════════════════════
# 各 UI 的元数据解析
# ════════════════════════════════════════════════════════════════


def _parse_mta_device_info(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_info(members, lower_names, "device-info.txt")
    values = _parse_key_values(_read_text(archive, info, limit=64 * 1024))
    if not values:
        return

    _set_if_empty(profile, "ui_version", _clean_version(values.get("maatauriandroid", "")))
    _set_if_empty(profile, "framework_version", _clean_version(values.get("framework", "")))
    _set_if_empty(profile, "device", values.get("device", ""))
    _set_if_empty(profile, "os_version", _android_os(values.get("android", "")))

    for key in ("abi",):
        if values.get(key):
            profile.metadata.setdefault(key, values[key])


def _parse_mta_project_snapshot(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_info(members, lower_names, "snapshots/project.json")
    text = _read_text(archive, info, limit=256 * 1024)
    if not text:
        return
    try:
        data = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return

    name = str(data.get("name", "") or data.get("label", "") or "").strip()
    version = str(data.get("version", "") or "").strip()
    _set_if_empty(profile, "project_name", name)
    _set_if_empty(profile, "resource_version", _clean_version(version))


def _find_info(
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
    wanted: str,
) -> zipfile.ZipInfo | None:
    target = wanted.lower()
    for info, name in zip(members, lower_names):
        if name == target:
            return info
    return None


def _find_newest(
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
    predicate,
) -> zipfile.ZipInfo | None:
    matched = [
        info for info, name in zip(members, lower_names)
        if predicate(name)
    ]
    if not matched:
        return None
    return max(matched, key=lambda info: (info.date_time, info.filename.lower()))


def _read_text(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    *,
    limit: int = 256 * 1024,
) -> str:
    if info is None:
        return ""
    try:
        with archive.open(info) as handle:
            data = handle.read(max(1, limit) + 1)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return ""
    return decode_bytes(data[:limit])


def _parse_key_values(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = _KEY_VALUE_LINE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key and value:
            result[key] = value
    return result


def _set_if_empty(profile: ArchiveProfile, attr: str, value: str) -> None:
    text = str(value or "").strip()
    if text and not getattr(profile, attr, ""):
        setattr(profile, attr, text)


def _clean_version(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s*\([^)]*\)", "", text).strip()
    text = re.sub(r"\s+release$", "", text, flags=re.IGNORECASE).strip()
    return text


def _android_os(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("android"):
        return text
    return f"Android {text}"


def _parse_device_info(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_info(members, lower_names, "device_info.txt")
    values = _parse_key_values(_read_text(archive, info, limit=64 * 1024))
    if not values:
        return

    _set_if_empty(profile, "ui_version", _clean_version(values.get("version", "")))
    _set_if_empty(profile, "module_version", _clean_version(values.get("maafwapp", "")))
    _set_if_empty(profile, "framework_version", _clean_version(values.get("framework", "")))
    _set_if_empty(profile, "package_name", values.get("app", ""))
    _set_if_empty(profile, "device", values.get("device", ""))
    _set_if_empty(profile, "os_version", _android_os(values.get("android", "") or values.get("os", "")))

    for key in ("export time", "abi", "screen", "ram", "storage", "security",
                "battery opt", "selinux"):
        if values.get(key):
            profile.metadata.setdefault(key, values[key])


def _parse_maafwapp_app_log(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_info(members, lower_names, "log/app.log")
    values = _parse_key_values(_read_text(archive, info, limit=256 * 1024))
    if not values:
        return

    _set_if_empty(profile, "ui_version", _clean_version(values.get("version", "")))
    _set_if_empty(profile, "module_version", _clean_version(values.get("maafwapp", "")))
    _set_if_empty(profile, "framework_version", _clean_version(values.get("framework", "")))
    _set_if_empty(profile, "device", values.get("device", ""))
    _set_if_empty(profile, "os_version", _android_os(values.get("os", "")))

    for key in ("startup time", "abi"):
        if values.get(key):
            profile.metadata.setdefault(key, values[key])


def _parse_mfaa_app_log(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_newest(
        members,
        lower_names,
        lambda name: name.startswith("logs/") and PurePosixPath(name).name.startswith("log-"),
    )
    if info is None:
        info = _find_newest(
            members,
            lower_names,
            lambda name: name.startswith("logs/") and name.endswith(".log"),
        )
    text = _read_text(archive, info, limit=256 * 1024)
    if not text:
        return

    def last_match(pattern: re.Pattern) -> str:
        matches = pattern.findall(text)
        return str(matches[-1]).strip() if matches else ""

    _set_if_empty(profile, "ui_version", _clean_version(last_match(_MFAA_UI_VERSION)))
    _set_if_empty(profile, "resource_version", _clean_version(last_match(_MFAA_RESOURCE_VERSION)))
    _set_if_empty(profile, "framework_version", _clean_version(last_match(_MFAA_FRAMEWORK_VERSION)))

    install_dir = last_match(_MFAA_INSTALL_DIR)
    if install_dir:
        project = _project_from_install_dir(install_dir)
        _set_if_empty(profile, "project_name", project)
        profile.metadata.setdefault("working", install_dir.strip())

    runtime = last_match(_MFAA_RUNTIME)
    if runtime:
        profile.metadata.setdefault(".net runtime", runtime.strip())


def _parse_mfw_gui_log(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    info = _find_info(members, lower_names, "debug/gui.log")
    text = _read_text(archive, info, limit=256 * 1024)
    if not text:
        return

    for line in text.splitlines():
        resource_match = _MFW_RESOURCE_VERSION.search(line)
        if resource_match:
            profile.resource_version = _clean_version(resource_match.group(1))
        ui_match = _MFW_UI_VERSION.search(line)
        if ui_match:
            _set_if_empty(profile, "ui_version", _clean_version(ui_match.group(1)))
            continue
        work_match = _MFW_WORKING_DIR.search(line)
        if work_match:
            profile.metadata.setdefault("working", work_match.group(1).strip())
            break


def _project_from_install_dir(path: str) -> str:
    base = str(path or "").strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not base:
        return ""
    match = re.match(r"^(?P<name>.+?)-(?:win|linux|mac|osx|android|ios)[-_].*$", base, re.IGNORECASE)
    if match:
        return match.group("name").strip(" -_")
    return base


def _parse_maafw_header(
    profile: ArchiveProfile,
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    lower_names: list[str],
) -> None:
    # 按优先级选择框架日志：当前 UI 的主日志优先
    preferred = [
        "log/maafw.log", "debug/maafw.log", "maafw.log",
        "logs/maa/maafw.log", "pi/debug/maafw.log",
    ]
    candidates = [
        info for info, name in zip(members, lower_names)
        if PurePosixPath(name).name == "maafw.log"
    ]
    if not candidates:
        return

    def priority(info: zipfile.ZipInfo) -> tuple[int, int]:
        normalized = info.filename.replace("\\", "/").lower()
        try:
            index = preferred.index(normalized)
        except ValueError:
            index = len(preferred)
        return index, -int(info.file_size)

    info = min(candidates, key=priority)
    text = _read_text(archive, info, limit=32 * 1024)
    if not text:
        return

    match = _MAFW_LOG_VERSION.search(text)
    if match:
        _set_if_empty(profile, "framework_version", _clean_version(match.group(1)))

    os_match = _MAFW_LOG_OS.search(text)
    if os_match:
        _set_if_empty(profile, "os_version", f"{os_match.group(1)} {os_match.group(2)}")

    if not profile.package_name:
        package_match = _ANDROID_PACKAGE.search(text)
        if package_match:
            profile.package_name = package_match.group(1)

    for line in text.splitlines()[:24]:
        for key in ("Built at", "Working", "Logging"):
            prefix = f"[Logger] {key} "
            if prefix in line:
                value = line.split(prefix, 1)[1].strip()
                if value:
                    profile.metadata.setdefault(key.lower(), value)
                break


def _parse_mxu_config_name(profile: ArchiveProfile, lower_names: list[str]) -> None:
    for name in lower_names:
        if not name.startswith("config/mxu-") or not name.endswith(".json"):
            continue
        base = PurePosixPath(name).name
        project = base[len("mxu-"):-len(".json")]
        _set_if_empty(profile, "project_name", project)
        return
