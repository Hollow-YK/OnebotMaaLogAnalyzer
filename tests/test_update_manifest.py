"""Manifest hashing, diffing and URL construction."""
from __future__ import annotations

import unittest
from pathlib import Path

from maa_analyzer.update.manifest import (
    FileEntry,
    ReleaseManifest,
    diff_manifests,
    mirror_urls,
    parse_sums,
    raw_url,
)
from maa_analyzer.update.install import InstallError


def _manifest(version: str, files: dict[str, str]) -> ReleaseManifest:
    return ReleaseManifest(
        version=version,
        kind="standard",
        tag=f"v{version}",
        files={
            path: FileEntry(path=path, sha256=digest, size=1, source=path)
            for path, digest in files.items()
        },
    )


class ManifestDiffTests(unittest.TestCase):
    def test_changed_unchanged_and_stale(self):
        local = _manifest("0.1.0", {"a": "1" * 64, "b": "2" * 64, "old": "3" * 64})
        remote = _manifest("0.2.0", {"a": "1" * 64, "b": "4" * 64, "new": "5" * 64})
        diff = diff_manifests(local, remote)
        self.assertEqual(diff.changed, ("b", "new"))
        self.assertEqual(diff.unchanged, ("a",))
        self.assertEqual(diff.stale, ("old",))

    def test_missing_local_manifest_means_everything_is_changed(self):
        remote = _manifest("0.2.0", {"a": "1" * 64})
        diff = diff_manifests(None, remote)
        self.assertEqual(diff.changed, ("a",))
        self.assertEqual(diff.unchanged, ())


class ManifestParsingTests(unittest.TestCase):
    def test_roundtrip(self):
        manifest = _manifest("0.2.0", {"a": "1" * 64})
        payload = manifest.to_dict()
        parsed = ReleaseManifest.from_dict(payload)
        self.assertEqual(parsed.version, "0.2.0")
        self.assertEqual(parsed.files["a"].sha256, "1" * 64)

    def test_rejects_bad_paths_and_hashes(self):
        with self.assertRaises(InstallError):
            ReleaseManifest.from_dict({
                "schema": 1, "version": "0.2.0", "kind": "standard",
                "files": {"../evil": {"sha256": "1" * 64}},
            })
        with self.assertRaises(InstallError):
            ReleaseManifest.from_dict({
                "schema": 1, "version": "0.2.0", "kind": "standard",
                "files": {"a": {"sha256": "not-a-hash"}},
            })

    def test_parse_sums_and_mirrors(self):
        sums = parse_sums("a" * 64 + "  one.zip\n" + "b" * 64 + " *two.zip\n")
        self.assertEqual(sums["one.zip"], "a" * 64)
        self.assertEqual(sums["two.zip"], "b" * 64)
        urls = mirror_urls(
            "https://example.test/f.zip",
            ["https://m/{url}", "https://n/?u={url_encoded}"],
        )
        self.assertEqual(urls[0], "https://m/https://example.test/f.zip")
        self.assertEqual(
            urls[1], "https://n/?u=https%3A%2F%2Fexample.test%2Ff.zip"
        )
        self.assertEqual(urls[-1], "https://example.test/f.zip")

    def test_raw_url_uses_tag_and_source_path(self):
        url = raw_url("v0.2.1", "astrbot_plugin/main.py")
        self.assertIn("/v0.2.1/astrbot_plugin/main.py", url)


if __name__ == "__main__":
    unittest.main()
