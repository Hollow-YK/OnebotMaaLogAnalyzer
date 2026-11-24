"""Detect how this process was installed and resolve package identities.

CI stamps every distribution archive with ``build-info.json``::

    {"schema": 1, "product": "...", "version": "0.3.0",
     "kind": "standard|light|astrbot", "repo": "...", "tag": "v0.3.0"}

A source checkout has no such file, so it is treated as ``source`` and may
check for updates but never replace itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .state import read_json

REPO = "Hollow-YK/OnebotMaaLogAnalyzer"
PRODUCT = "OnebotMaaLogAnalyzer"
DEFAULT_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
MANIFEST_NAME = "build-manifest.json"
BUILD_INFO_NAME = "build-info.json"
SUM_ASSET_NAME = "SHA256SUMS.txt"

KINDS = ("standard", "light", "astrbot")
BUILD_SCHEMA = 1

# Files a prepared version directory must contain before it may be activated.
REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "standard": (
        "maa_analyzer/__init__.py",
        "maa_analyzer/bare_entry.py",
        "templates/default/page.html",
        BUILD_INFO_NAME,
    ),
    "light": (
        "maa_analyzer/__init__.py",
        "maa_analyzer/bare_entry.py",
        "templates/default/page.html",
        BUILD_INFO_NAME,
    ),
    "astrbot": (
        "plugin_shell.py",
        "maa_analyzer/__init__.py",
        "maa_analyzer/platforms/astrbot/adapter.py",
        "templates/default/page.html",
        BUILD_INFO_NAME,
    ),
    "source": (
        "maa_analyzer/__init__.py",
        "maa_analyzer/bare_entry.py",
    ),
}

_MANAGED_NAMES = (
    "main.py",
    "maa_bootstrap.py",
    "plugin_shell.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "metadata.yaml",
    "_conf_schema.json",
    "docs",
    "maa_analyzer",
    "templates",
    "tools",
    BUILD_INFO_NAME,
    MANIFEST_NAME,
)


class InstallError(RuntimeError):
    """The current installation cannot be updated safely."""


@dataclass(frozen=True, slots=True)
class InstallInfo:
    root: Path
    app_root: Path
    kind: str
    version: str
    versioned: bool = False
    build_info: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None
    pointer: dict[str, Any] = field(default_factory=dict)

    @property
    def bad_versions(self) -> tuple[str, ...]:
        raw = self.pointer.get("bad") if isinstance(self.pointer, dict) else None
        if not isinstance(raw, list):
            return ()
        return tuple(
            str(item).strip() for item in raw if str(item).strip()
        )

    @property
    def source(self) -> bool:
        return self.kind == "source"

    @property
    def version_dir(self) -> Path | None:
        return self.app_root if self.versioned else None

    @property
    def asset_names(self) -> tuple[str, str]:
        return asset_names(self.version, self.kind)

    @property
    def managed_names(self) -> tuple[str, ...]:
        return _MANAGED_NAMES


def _source_version() -> str:
    try:
        from ..version import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


def _as_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in KINDS else "source"


def detect_install(app_root: str | Path | None = None) -> InstallInfo:
    """Return the installation identity for ``app_root`` (defaults to this package)."""
    if app_root is None:
        # .../maa_analyzer/update/install.py -> .../maa_analyzer -> app root
        app_root_path = Path(__file__).resolve().parents[2]
    else:
        app_root_path = Path(app_root).expanduser().resolve()

    versioned = False
    root = app_root_path
    pointer: dict[str, Any] = {}
    if app_root_path.parent.name == "versions":
        # A version directory is authoritative even when the pointer is
        # missing/corrupt; the stable launcher can recover by scanning
        # ``versions/`` and the app will recreate the pointer on confirmation.
        versioned = True
        root = app_root_path.parent.parent
        candidate_pointer = read_json(root / "current.json", {})
        if isinstance(candidate_pointer, dict):
            pointer = candidate_pointer
    if not pointer:
        candidate_pointer = read_json(app_root_path / "current.json", {})
        if isinstance(candidate_pointer, dict) and candidate_pointer:
            pointer = candidate_pointer

    raw_info = read_json(app_root_path / BUILD_INFO_NAME, {})
    if not isinstance(raw_info, dict):
        raw_info = {}
    kind = _as_kind(raw_info.get("kind"))
    version = str(raw_info.get("version") or "").strip()
    if not version and versioned:
        version = str(pointer.get("active") or "").strip()
    if kind == "source":
        kind = "source"
        version = version or _source_version()
    elif not version:
        version = _source_version()

    if versioned and not raw_info:
        raw_info = {
            "schema": BUILD_SCHEMA,
            "product": PRODUCT,
            "version": version,
            "kind": kind,
            "repo": REPO,
        }

    manifest = app_root_path / MANIFEST_NAME
    return InstallInfo(
        root=root,
        app_root=app_root_path,
        kind=kind,
        version=version,
        versioned=versioned,
        build_info=raw_info,
        manifest_path=manifest if manifest.is_file() else None,
        pointer=pointer,
    )


def asset_names(version: str, kind: str) -> tuple[str, str]:
    """Return ``(archive_name, manifest_name)`` for a release kind."""
    base = f"{PRODUCT}-{version}"
    if kind == "light":
        return f"{base}-light.zip", f"{base}-light.manifest.json"
    if kind == "astrbot":
        return f"{base}-astrbot.zip", f"{base}-astrbot.manifest.json"
    return f"{base}.zip", f"{base}.manifest.json"


def safe_version_name(value: object) -> str:
    """Return a filesystem-safe release version or raise."""
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise InstallError(f"非法版本名：{value!r}")
    if not re.fullmatch(r"[0-9A-Za-z._+-]+", text):
        raise InstallError(f"版本名包含非法字符：{text!r}")
    return text


def safe_relative_path(value: object) -> str:
    """Normalize a manifest path and reject traversal/absolute paths."""
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/"):
        raise InstallError(f"非法相对路径：{value!r}")
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise InstallError(f"非法相对路径：{value!r}")
    if any(part in {"", "."} for part in pure.parts):
        raise InstallError(f"非法相对路径：{value!r}")
    return pure.as_posix()


def required_files(kind: str) -> tuple[str, ...]:
    return REQUIRED_FILES.get(kind, REQUIRED_FILES["source"])


def is_valid_app_root(path: str | Path, kind: str) -> bool:
    root = Path(path)
    return all((root / name).is_file() for name in required_files(kind))


def write_build_info(directory: str | Path, *, version: str, kind: str, tag: str = "") -> Path:
    """Write the CI-compatible build marker for a prepared version directory."""
    target = Path(directory) / BUILD_INFO_NAME
    from .state import atomic_write_json

    atomic_write_json(
        target,
        {
            "schema": BUILD_SCHEMA,
            "product": PRODUCT,
            "version": version,
            "kind": kind,
            "repo": REPO,
            "tag": tag,
        },
    )
    return target


def managed_names_for(version_dir: str | Path) -> set[str]:
    """Top-level names managed by the updater's copy step."""
    del version_dir
    return set(_MANAGED_NAMES)


__all__ = [
    "BUILD_INFO_NAME",
    "BUILD_SCHEMA",
    "DEFAULT_API_BASE",
    "InstallError",
    "InstallInfo",
    "KINDS",
    "MANIFEST_NAME",
    "PRODUCT",
    "RAW_BASE",
    "REPO",
    "SUM_ASSET_NAME",
    "asset_names",
    "detect_install",
    "is_valid_app_root",
    "managed_names_for",
    "required_files",
    "safe_relative_path",
    "safe_version_name",
    "write_build_info",
]
