"""SemVer parsing and channel ordering for the updater."""
from __future__ import annotations

import unittest

from maa_analyzer.update.versioning import (
    compare_versions,
    latest_version,
    parse_version,
)


class VersionParsingTests(unittest.TestCase):
    def test_accepts_v_prefix_and_prerelease(self):
        version = parse_version("v0.3.0-beta.2")
        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(version.core, (0, 3, 0))
        self.assertEqual(version.prerelease, ("beta", "2"))
        self.assertEqual(str(version), "0.3.0-beta.2")

    def test_rejects_non_semver(self):
        for value in ("", "latest", "v1.2", "1.2.3.4", "ci-abc"):
            self.assertIsNone(parse_version(value), value)

    def test_release_sorts_after_its_prereleases(self):
        self.assertLess(parse_version("0.3.0-alpha"), parse_version("0.3.0-beta"))
        self.assertLess(parse_version("0.3.0-beta.2"), parse_version("0.3.0-beta.10"))
        self.assertLess(parse_version("0.3.0-rc.1"), parse_version("0.3.0"))
        self.assertLess(parse_version("0.2.9"), parse_version("0.3.0-alpha"))

    def test_compare_handles_invalid_inputs_as_zero(self):
        self.assertEqual(compare_versions("ci-abc", "0.0.0"), 0)
        self.assertEqual(compare_versions("v0.2.1", "0.2.1"), 0)
        self.assertGreater(compare_versions("v0.2.2", "v0.2.1"), 0)

    def test_latest_version(self):
        self.assertEqual(str(latest_version(["v0.1.0", "v0.2.0", "bad"])), "0.2.0")
        self.assertIsNone(latest_version(["bad", ""]))


if __name__ == "__main__":
    unittest.main()
