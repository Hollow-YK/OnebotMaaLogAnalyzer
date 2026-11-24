"""GitHub release selection across channels and API bases."""
from __future__ import annotations

import unittest

from maa_analyzer.update.github import GitHubReleaseClient
from maa_analyzer.update.net import HttpError


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "body": f"notes for {tag}",
        "html_url": f"https://example.test/releases/{tag}",
        "assets": [{
            "name": f"OnebotMaaLogAnalyzer-{tag.lstrip('v')}.zip",
            "browser_download_url": f"https://example.test/{tag}.zip",
            "size": 123,
            "digest": "sha256:" + "a" * 64,
        }],
    }


class _FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    async def get_json(self, url, *, headers=None):
        self.calls.append(url)
        value = self.responses.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise HttpError("missing")
        return value


class ReleaseSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_ignores_prerelease(self):
        base = "https://api.example"
        url = f"{base}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30"
        http = _FakeHttp({url: (200, [
            _release("v0.2.1"),
            _release("v0.3.0-beta.1", prerelease=True),
        ], {})})
        client = GitHubReleaseClient(http, [base], channel="stable")
        result = await client.fetch()
        self.assertEqual(result.latest.version_text, "0.2.1")

    async def test_prerelease_channel_takes_newest_either_kind(self):
        base = "https://api.example"
        url = f"{base}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30"
        http = _FakeHttp({url: (200, [
            _release("v0.2.1"),
            _release("v0.3.0-beta.1", prerelease=True),
            _release("v0.4.0-draft", draft=True),
        ], {})})
        client = GitHubReleaseClient(http, [base], channel="prerelease")
        result = await client.fetch()
        self.assertEqual(result.latest.version_text, "0.3.0-beta.1")
        self.assertTrue(result.latest.prerelease)

    async def test_multiple_bases_choose_latest_and_keep_failures(self):
        good = "https://a.example"
        bad = "https://b.example"
        old = "https://c.example"
        http = _FakeHttp({
            f"{good}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30":
                (200, [_release("v0.2.1")], {}),
            f"{bad}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30":
                HttpError("HTTP 403"),
            f"{old}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30":
                (200, [_release("v0.2.0")], {}),
        })
        client = GitHubReleaseClient(http, [bad, good, old], channel="stable")
        result = await client.fetch()
        self.assertEqual(result.latest.version_text, "0.2.1")
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(result.all_failed)

    async def test_all_bases_failed(self):
        bad = "https://b.example"
        http = _FakeHttp({
            f"{bad}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30":
                HttpError("timeout"),
        })
        client = GitHubReleaseClient(http, [bad], channel="stable")
        result = await client.fetch()
        self.assertIsNone(result.latest)
        self.assertTrue(result.all_failed)

    async def test_etag_304_uses_cached_release(self):
        base = "https://api.example"
        url = f"{base}/repos/Hollow-YK/OnebotMaaLogAnalyzer/releases?per_page=30"
        http = _FakeHttp({url: (304, None, {"etag": '"next"'})})
        client = GitHubReleaseClient(http, [base], channel="stable")
        cached = {
            base: _release("v0.2.1"),
        }
        from maa_analyzer.update.github import ReleaseInfo

        cached_release = ReleaseInfo.from_dict(cached[base])
        assert cached_release is not None
        result = await client.fetch(
            etags={base: '"old"'},
            cache={base: cached_release.to_dict()},
        )
        self.assertEqual(result.latest.version_text, "0.2.1")
        self.assertEqual(result.etags[base], '"next"')


if __name__ == "__main__":
    unittest.main()
