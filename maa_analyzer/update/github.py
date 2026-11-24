"""GitHub Releases lookup across one or more API bases.

The checker queries each configured base exactly once per cycle (no automatic
retry), then picks the highest version seen.  This lets users list mirrors in
order while a stale mirror cannot hide a newer release from another one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from .install import DEFAULT_API_BASE, REPO
from .net import HttpClient, HttpError
from .versioning import Version, parse_version

logger = logging.getLogger("Maa.Update.GitHub")

CHANNELS = ("stable", "prerelease")


def normalize_channel(value: object) -> str:
    text = str(value or "").strip().lower()
    return "prerelease" if text in {"prerelease", "pre", "beta", "test"} else "stable"


def normalize_api_bases(values: object) -> list[str]:
    raw = values if isinstance(values, (list, tuple)) else [values]
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip().rstrip("/")
        if not text:
            continue
        if not text.startswith(("http://", "https://")):
            continue
        if text not in result:
            result.append(text)
    return result or [DEFAULT_API_BASE]


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "size": self.size,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReleaseAsset":
        return cls(
            name=str(data.get("name") or ""),
            url=str(data.get("url") or ""),
            size=int(data.get("size") or 0),
            digest=str(data.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    tag: str
    version: Version
    prerelease: bool
    body: str = ""
    html_url: str = ""
    api_base: str = ""
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    @property
    def version_text(self) -> str:
        return str(self.version)

    def asset(self, name: str) -> ReleaseAsset | None:
        return next((item for item in self.assets if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "version": self.version_text,
            "prerelease": self.prerelease,
            "body": self.body,
            "html_url": self.html_url,
            "api_base": self.api_base,
            "assets": [item.to_dict() for item in self.assets],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReleaseInfo | None":
        tag = str(data.get("tag") or data.get("tag_name") or "")
        version = parse_version(data.get("version") or tag)
        if version is None:
            return None
        assets = []
        for item in data.get("assets") or []:
            if not isinstance(item, Mapping):
                continue
            if not item.get("url") and item.get("browser_download_url"):
                item = {**item, "url": item.get("browser_download_url")}
            assets.append(ReleaseAsset.from_dict(item))
        return cls(
            tag=tag,
            version=version,
            prerelease=bool(data.get("prerelease")),
            body=str(data.get("body") or ""),
            html_url=str(data.get("html_url") or ""),
            api_base=str(data.get("api_base") or ""),
            assets=tuple(assets),
        )


@dataclass(slots=True)
class CheckResult:
    latest: ReleaseInfo | None
    errors: list[tuple[str, str]] = field(default_factory=list)
    etags: dict[str, str] = field(default_factory=dict)
    cached: dict[str, dict[str, Any]] = field(default_factory=dict)
    bases: tuple[str, ...] = ()

    @property
    def all_failed(self) -> bool:
        return bool(self.errors) and self.latest is None and all(
            base in {err[0] for err in self.errors} for base in self.bases
        )


def _parse_release(raw: Mapping[str, Any], api_base: str) -> ReleaseInfo | None:
    if bool(raw.get("draft")):
        return None
    tag = str(raw.get("tag_name") or "").strip()
    version = parse_version(tag)
    if version is None:
        logger.warning("[更新] 忽略非 SemVer 标签：%s", tag)
        return None
    assets = tuple(
        ReleaseAsset(
            name=str(item.get("name") or ""),
            url=str(item.get("browser_download_url") or item.get("url") or ""),
            size=int(item.get("size") or 0),
            digest=str(item.get("digest") or ""),
        )
        for item in (raw.get("assets") or [])
        if isinstance(item, Mapping)
    )
    return ReleaseInfo(
        tag=tag,
        version=version,
        prerelease=bool(raw.get("prerelease")),
        body=str(raw.get("body") or ""),
        html_url=str(raw.get("html_url") or ""),
        api_base=api_base,
        assets=assets,
    )


def _select_latest(
    releases: object, api_base: str, channel: str
) -> ReleaseInfo | None:
    if not isinstance(releases, list):
        return None
    candidates: list[ReleaseInfo] = []
    for raw in releases:
        if not isinstance(raw, Mapping):
            continue
        release = _parse_release(raw, api_base)
        if release is None:
            continue
        if channel == "stable" and release.prerelease:
            continue
        candidates.append(release)
    return max(candidates, key=lambda item: item.version) if candidates else None


class GitHubReleaseClient:
    """Query several GitHub API bases and return the newest eligible release."""

    def __init__(
        self,
        http: HttpClient,
        api_bases: object = None,
        *,
        repo: str = REPO,
        channel: str = "stable",
    ):
        self.http = http
        self.api_bases = normalize_api_bases(api_bases)
        self.repo = repo
        self.channel = normalize_channel(channel)

    async def fetch(
        self,
        *,
        etags: Mapping[str, str] | None = None,
        cache: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> CheckResult:
        old_etags = dict(etags or {})
        old_cache = dict(cache or {})
        result = CheckResult(latest=None, bases=tuple(self.api_bases))
        for base in self.api_bases:
            url = f"{base}/repos/{self.repo}/releases?per_page=30"
            headers = {"X-GitHub-Api-Version": "2022-11-28"}
            etag = old_etags.get(base)
            if etag:
                headers["If-None-Match"] = etag
            try:
                status, payload, response_headers = await self.http.get_json(
                    url, headers=headers
                )
            except HttpError as exc:
                result.errors.append((base, str(exc)))
                logger.warning("[更新] %s 查询失败：%s", base, exc)
                continue
            if status == 304:
                cached = old_cache.get(base)
                release = (
                    ReleaseInfo.from_dict(cached)
                    if isinstance(cached, Mapping)
                    else None
                )
                if release is not None:
                    result.cached[base] = release.to_dict()
                    if _eligible(release, self.channel):
                        result.latest = _newer(result.latest, release)
                else:
                    result.errors.append((base, "304 但本地缓存缺失"))
                result.etags[base] = (
                    response_headers.get("etag") or etag or ""
                )
                continue
            latest = _select_latest(payload, base, self.channel)
            new_etag = response_headers.get("etag") or ""
            if new_etag:
                result.etags[base] = new_etag
            if latest is not None:
                result.cached[base] = latest.to_dict()
                result.latest = _newer(result.latest, latest)
        return result


def _eligible(release: ReleaseInfo, channel: str) -> bool:
    return channel != "stable" or not release.prerelease


def _newer(current: ReleaseInfo | None, candidate: ReleaseInfo) -> ReleaseInfo:
    if current is None:
        return candidate
    return candidate if candidate.version > current.version else current


__all__ = [
    "CHANNELS",
    "CheckResult",
    "GitHubReleaseClient",
    "ReleaseAsset",
    "ReleaseInfo",
    "normalize_api_bases",
    "normalize_channel",
]
