"""Release manifests, file hashes, diff calculation and download URLs.

A release manifest describes every file in one distribution archive.  The
updater compares it with the manifest of the active version and downloads only
the files whose hashes differ.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from .install import RAW_BASE, REPO, InstallError, safe_relative_path
from .versioning import parse_version

MANIFEST_SCHEMA = 1
_CHUNK = 1024 * 1024


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    sha256: str
    size: int = 0
    source: str = ""
    content_b64: str = ""

    @property
    def content(self) -> bytes | None:
        if not self.content_b64:
            return None
        try:
            return base64.b64decode(self.content_b64.encode("ascii"))
        except (ValueError, TypeError):
            return None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"sha256": self.sha256, "size": self.size}
        if self.source and self.source != self.path:
            data["source"] = self.source
        if self.content_b64:
            data["content_b64"] = self.content_b64
        return data


@dataclass(slots=True)
class ReleaseManifest:
    version: str
    kind: str
    tag: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    repo: str = REPO
    schema: int = MANIFEST_SCHEMA

    @property
    def total_size(self) -> int:
        return sum(entry.size for entry in self.files.values())

    def sorted_paths(self) -> list[str]:
        return sorted(self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "product": "OnebotMaaLogAnalyzer",
            "repo": self.repo,
            "version": self.version,
            "kind": self.kind,
            "tag": self.tag,
            "files": {
                path: entry.to_dict() for path, entry in sorted(self.files.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReleaseManifest":
        if not isinstance(data, Mapping):
            raise InstallError("manifest 必须是 JSON 对象。")
        schema = data.get("schema", MANIFEST_SCHEMA)
        if schema != MANIFEST_SCHEMA:
            raise InstallError(f"不支持的 manifest schema：{schema!r}")
        version = str(data.get("version") or "").strip()
        kind = str(data.get("kind") or "").strip()
        tag = str(data.get("tag") or "").strip()
        if not version or not kind:
            raise InstallError("manifest 缺少 version 或 kind。")
        if parse_version(version) is None:
            raise InstallError(f"manifest 版本号非法：{version!r}")
        raw_files = data.get("files")
        if not isinstance(raw_files, Mapping) or not raw_files:
            raise InstallError("manifest 缺少 files。")
        files: dict[str, FileEntry] = {}
        for raw_path, raw_entry in raw_files.items():
            path = safe_relative_path(raw_path)
            if not isinstance(raw_entry, Mapping):
                raise InstallError(f"manifest 文件项非法：{path}")
            sha = str(raw_entry.get("sha256") or "").strip().lower()
            if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
                raise InstallError(f"manifest 文件哈希非法：{path}")
            size = raw_entry.get("size", 0)
            try:
                size_value = max(0, int(size or 0))
            except (TypeError, ValueError):
                size_value = 0
            source = str(raw_entry.get("source") or path).strip()
            source = safe_relative_path(source) if source else path
            content_b64 = str(raw_entry.get("content_b64") or "").strip()
            files[path] = FileEntry(
                path=path,
                sha256=sha,
                size=size_value,
                source=source,
                content_b64=content_b64,
            )
        return cls(
            version=version,
            kind=kind,
            tag=tag,
            files=files,
            repo=str(data.get("repo") or REPO),
            schema=int(schema),
        )


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def changed_size(self) -> int:
        return sum(1 for _ in self.changed)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed or self.stale)


def diff_manifests(
    local: ReleaseManifest | None,
    remote: ReleaseManifest,
) -> ManifestDiff:
    """Compare a local manifest with the release manifest."""
    if local is None:
        return ManifestDiff(
            changed=tuple(remote.sorted_paths()),
            unchanged=(),
            stale=(),
        )
    changed: list[str] = []
    unchanged: list[str] = []
    for path, entry in remote.files.items():
        local_entry = local.files.get(path)
        if local_entry is not None and local_entry.sha256 == entry.sha256:
            unchanged.append(path)
        else:
            changed.append(path)
    stale = sorted(path for path in local.files if path not in remote.files)
    return ManifestDiff(
        changed=tuple(sorted(changed)),
        unchanged=tuple(sorted(unchanged)),
        stale=tuple(stale),
    )


def verify_tree(
    root: str | Path,
    manifest: ReleaseManifest,
    *,
    ignore: Iterable[str] = (),
) -> list[str]:
    """Return the manifest paths whose local content is missing or wrong."""
    base = Path(root)
    ignored = set(ignore)
    bad: list[str] = []
    for path, entry in manifest.files.items():
        if path in ignored:
            continue
        target = base / path
        if not target.is_file():
            bad.append(path)
            continue
        if target.stat().st_size != entry.size and entry.size:
            bad.append(path)
            continue
        if file_sha256(target) != entry.sha256:
            bad.append(path)
    return bad


def raw_url(tag: str, source: str, *, repo: str = REPO, raw_base: str = RAW_BASE) -> str:
    """Build the raw.githubusercontent URL for one package file."""
    source_path = safe_relative_path(source)
    return f"{raw_base.rstrip('/')}/{repo}/{quote(tag, safe='')}/{quote(source_path, safe='/')}"


def mirror_urls(url: str, mirrors: Iterable[str]) -> list[str]:
    """Expand configured download mirrors, ending with the original URL."""
    result: list[str] = []
    for raw in mirrors:
        mirror = str(raw or "").strip()
        if not mirror:
            continue
        if "{url_encoded}" in mirror:
            candidate = mirror.replace("{url_encoded}", quote(url, safe=""))
        elif "{url}" in mirror:
            candidate = mirror.replace("{url}", url)
        else:
            candidate = mirror.rstrip("/") + "/" + url
        if candidate not in result:
            result.append(candidate)
    if url not in result:
        result.append(url)
    return result


def parse_sums(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` output into ``{filename: hash}``."""
    result: dict[str, str] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].strip().lower(), parts[1].strip().lstrip("*")
        if len(digest) == 64 and name:
            result[name] = digest
    return result


def build_manifest(
    directory: str | Path,
    *,
    version: str,
    kind: str,
    tag: str,
    inline: Mapping[str, bytes] | None = None,
    source_map: Mapping[str, str] | None = None,
    repo: str = REPO,
) -> ReleaseManifest:
    """Hash a prepared package directory into a release manifest.

    ``inline`` files are written from the manifest itself (CI-generated files
    that do not exist verbatim in the repository).  ``source_map`` maps package
    paths to repository-relative raw download paths.
    """
    root = Path(directory)
    inline_map = dict(inline or {})
    source_paths = dict(source_map or {})
    files: dict[str, FileEntry] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "build-manifest.json":
            continue
        data = path.read_bytes()
        files[relative] = FileEntry(
            path=relative,
            sha256=bytes_sha256(data),
            size=len(data),
            source=source_paths.get(relative, relative),
        )
    for relative, data in inline_map.items():
        safe = safe_relative_path(relative)
        if safe == "build-manifest.json":
            continue
        files[safe] = FileEntry(
            path=safe,
            sha256=bytes_sha256(data),
            size=len(data),
            source=safe,
            content_b64=base64.b64encode(data).decode("ascii"),
        )
    return ReleaseManifest(
        version=version, kind=kind, tag=tag, files=files, repo=repo
    )


__all__ = [
    "FileEntry",
    "MANIFEST_SCHEMA",
    "ManifestDiff",
    "ReleaseManifest",
    "build_manifest",
    "bytes_sha256",
    "diff_manifests",
    "file_sha256",
    "mirror_urls",
    "parse_sums",
    "raw_url",
    "verify_tree",
]
