"""Semantic-version parsing and ordering for release tags.

The project tags releases as ``v0.2.1`` but may also publish pre-releases such
as ``v0.3.0-beta.2`` or ``v0.3.0-rc1``.  This module deliberately implements a
small, dependency-free subset of SemVer so the updater can run before any
third-party package is importable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_TAG_RE = re.compile(
    r"^[vV]?"
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)

_PRE_RELEASE_WORDS = ("alpha", "beta", "rc", "dev", "preview", "pre")


def _pre_key(identifier: str) -> tuple[int, object]:
    """Sort numeric identifiers numerically and textual ones lexically."""
    if identifier.isdigit():
        return (0, int(identifier))
    return (1, identifier.lower())


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A parsed semantic version."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: str = ""
    raw: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def _cmp_key(self) -> tuple:
        # A release sorts after all of its pre-releases.
        if not self.prerelease:
            pre: tuple = (1,)
        else:
            pre = (0, tuple(_pre_key(item) for item in self.prerelease))
        return (self.major, self.minor, self.patch, pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp_key() < other._cmp_key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._cmp_key() == other._cmp_key()

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + self.build
        return text


def parse_version(value: object) -> Version | None:
    """Parse a tag or version string; return ``None`` when it is not SemVer."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _TAG_RE.match(text)
    if match is None:
        return None
    prerelease = tuple(
        item for item in (match.group("pre") or "").split(".") if item
    )
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=prerelease,
        build=match.group("build") or "",
        raw=text,
    )


def looks_prerelease(value: object) -> bool:
    """Fallback heuristic for tags that are not strict SemVer."""
    text = str(value or "").strip().lower()
    return any(word in text for word in _PRE_RELEASE_WORDS)


def compare_versions(left: object, right: object) -> int:
    """Return -1/0/1 for two version strings, treating invalid input as 0.0.0."""
    a = parse_version(left) or Version(0, 0, 0, raw=str(left or ""))
    b = parse_version(right) or Version(0, 0, 0, raw=str(right or ""))
    if a < b:
        return -1
    if b < a:
        return 1
    return 0


def latest_version(values: list[object]) -> Version | None:
    """Return the highest parsable version from ``values``."""
    parsed = [item for item in (parse_version(value) for value in values) if item]
    return max(parsed) if parsed else None


__all__ = [
    "Version",
    "compare_versions",
    "latest_version",
    "looks_prerelease",
    "parse_version",
]
