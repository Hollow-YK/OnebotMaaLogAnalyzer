"""UpdateManager notification, dedupe and failure semantics."""
from __future__ import annotations

import json
import shutil
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from maa_analyzer.update.github import CheckResult, ReleaseInfo
from maa_analyzer.update.install import InstallInfo
from maa_analyzer.update.manager import UpdateManager
from maa_analyzer.update.versioning import parse_version

BASE = Path("_tmp_update_manager")


def _clean(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _release(version: str) -> ReleaseInfo:
    parsed = parse_version(version)
    assert parsed is not None
    return ReleaseInfo(
        tag=f"v{version}",
        version=parsed,
        prerelease=False,
        body="- change",
        html_url=f"https://example.test/v{version}",
    )


class _FakeService:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_private_text(self, user_id: int, text: str) -> str | None:
        self.messages.append((user_id, text))
        return "1"


class _FakeClient:
    result: CheckResult

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self, *, etags=None, cache=None):
        return _FakeClient.result


@dataclass
class _FakeHttp:
    async def close(self):
        pass


class _FakeInstaller:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def prepare(self, release, *, download_mode="auto"):
        return SimpleNamespace(
            mode="full",
            changed=("payload.txt",),
            downloaded_bytes=10,
            version_dir=Path("versions") / release.version_text,
        )

    def activate(self, prepared):
        return {"active": "0.2.0", "previous": "", "restore": []}

    async def close(self):
        pass


class UpdateManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clean(BASE)
        BASE.mkdir(parents=True, exist_ok=True)
        self.data = BASE / f"data-{uuid.uuid4().hex[:8]}"
        self.data.mkdir()
        self.service = _FakeService()
        self.install = InstallInfo(
            root=BASE, app_root=BASE, kind="standard", version="0.1.0"
        )

    def tearDown(self):
        _clean(BASE)

    def _manager(self, *, restart_callback=None, **cfg):
        manager = UpdateManager(
            data_dir=self.data,
            cfg={"notify_qq": ["123"], "channel": "stable", **cfg},
            install=self.install,
            service=self.service,
            restart_callback=restart_callback,
            background=False,
        )
        manager._http = _FakeHttp()  # type: ignore[assignment]
        return manager

    async def test_new_version_notifies_once(self):
        _FakeClient.result = CheckResult(latest=_release("0.2.0"))
        manager = self._manager()
        with patch("maa_analyzer.update.manager.GitHubReleaseClient", _FakeClient), \
             patch("maa_analyzer.update.manager.detect_install", return_value=self.install):
            await manager.check()
            await manager.check()
        self.assertEqual(len(self.service.messages), 1)
        self.assertIn("0.2.0", self.service.messages[0][1])
        self.assertEqual(self.service.messages[0][0], 123)

    async def test_all_bases_failure_notifies_without_retry(self):
        _FakeClient.result = CheckResult(
            latest=None,
            errors=[("https://a.example", "HTTP 403")],
            bases=("https://a.example",),
        )
        manager = self._manager()
        with patch("maa_analyzer.update.manager.GitHubReleaseClient", _FakeClient), \
             patch("maa_analyzer.update.manager.detect_install", return_value=self.install):
            await manager.check()
        self.assertEqual(len(self.service.messages), 1)
        self.assertIn("检查更新失败", self.service.messages[0][1])
        self.assertIn("不会自动重试", self.service.messages[0][1])

    async def test_source_auto_update_is_refused(self):
        source = InstallInfo(
            root=BASE, app_root=BASE, kind="source", version="0.3.0-dev"
        )
        _FakeClient.result = CheckResult(latest=_release("0.4.0"))
        manager = self._manager(auto_update=True)
        with patch("maa_analyzer.update.manager.GitHubReleaseClient", _FakeClient), \
             patch("maa_analyzer.update.manager.detect_install", return_value=source), \
             patch.object(manager, "apply") as apply:
            await manager.check(auto_apply=True)
        apply.assert_not_called()
        joined = "\n".join(text for _, text in self.service.messages)
        self.assertIn("源码运行", joined)

    async def test_manual_apply_activates_and_requests_restart(self):
        restarts: list[str] = []
        manager = self._manager(restart_callback=lambda: restarts.append("go"))
        async def _run(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("maa_analyzer.update.manager.VersionInstaller", _FakeInstaller), \
             patch("maa_analyzer.update.manager.run_blocking", new=AsyncMock(side_effect=_run)), \
             patch("maa_analyzer.update.manager.detect_install", return_value=self.install):
            outcome = await manager.apply(_release("0.2.0"), force=True)
        self.assertTrue(outcome.ok)
        self.assertEqual(restarts, ["go"])
        result = json.loads(
            (self.data / "updates" / "last-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["version"], "0.2.0")
        self.assertTrue(any("正在重启" in text for _, text in self.service.messages))

    async def test_status_and_check_message(self):
        _FakeClient.result = CheckResult(latest=_release("0.2.0"))
        manager = self._manager()
        with patch(
            "maa_analyzer.update.manager.detect_install", return_value=self.install
        ):
            status = manager.describe_status()
        self.assertIn("0.1.0", status)
        self.assertIn("stable", status)
        with patch("maa_analyzer.update.manager.detect_install", return_value=self.install):
            message = manager.format_check_result(_FakeClient.result)
        self.assertIn("0.2.0", message)


if __name__ == "__main__":
    unittest.main()
