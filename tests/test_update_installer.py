"""Full/incremental materialization and crash-safe activation."""
from __future__ import annotations

import hashlib
import json
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from maa_analyzer.update.github import ReleaseAsset, ReleaseInfo
from maa_analyzer.update.install import asset_names, detect_install
from maa_analyzer.update.installer import VersionInstaller
from maa_analyzer.update.manifest import (
    build_manifest,
    bytes_sha256,
    raw_url,
)
from maa_analyzer.update.net import HttpError
from maa_analyzer.update.versioning import parse_version

BASE = Path("_tmp_update_tests")
REPO = Path(__file__).resolve().parents[1]

# 与真实入口同构：main.py 只做表态，--launcher-check 时立即退出。
LAUNCHER_V1 = (
    b"import sys\n"
    b"from maa_bootstrap import run_bare_launcher\n"
    b"if '--launcher-check' in sys.argv:\n"
    b"    sys.exit(0)\n"
    b"raise SystemExit(run_bare_launcher())\n"
)
LAUNCHER_V2 = LAUNCHER_V1 + b"# launcher v2\n"
BOOTSTRAP_V1 = b"def run_bare_launcher():\n    return 0\n"
BOOTSTRAP_V2 = BOOTSTRAP_V1 + b"# bootstrap v2\n"


def _clean(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _package_files(version: str) -> dict[str, bytes]:
    info = json.dumps({
        "schema": 1,
        "product": "OnebotMaaLogAnalyzer",
        "version": version,
        "kind": "standard",
        "repo": "Hollow-YK/OnebotMaaLogAnalyzer",
        "tag": f"v{version}",
    }).encode("utf-8")
    return {
        "main.py": b"import sys\nif '--launcher-check' in sys.argv: sys.exit(0)\n",
        "maa_bootstrap.py": b"",
        "maa_analyzer/__init__.py": b"",
        "maa_analyzer/bare_entry.py": (
            b"import sys\nif '--self-check' in sys.argv:\n"
            b"    print('ok')\n    sys.exit(0)\n"
        ),
        "templates/default/page.html": b"<html></html>",
        "build-info.json": info,
        "payload.txt": f"payload {version}\n".encode(),
    }


def _make_package(
    base: Path, version: str, *, files: dict[str, bytes] | None = None
) -> tuple[Path, Any, dict[str, bytes]]:
    package = base / f"pkg-{version}-{uuid.uuid4().hex[:6]}" / "OnebotMaaLogAnalyzer"
    package.mkdir(parents=True)
    contents = files or _package_files(version)
    for name, data in contents.items():
        _write(package / name, data)
    inline = {"build-info.json": contents["build-info.json"]}
    manifest = build_manifest(
        package,
        version=version,
        kind="standard",
        tag=f"v{version}",
        inline=inline,
    )
    manifest_bytes = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    ).encode()
    _write(package / "build-manifest.json", manifest_bytes)
    return package, manifest, contents


def _zip_package(package: Path, target: Path) -> bytes:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package.parent))
    return target.read_bytes()


def _release(
    version: str,
    *,
    manifest_bytes: bytes,
    archive_bytes: bytes,
    sums_bytes: bytes,
) -> ReleaseInfo:
    archive_name, manifest_name = asset_names(version, "standard")
    parsed = parse_version(version)
    assert parsed is not None
    return ReleaseInfo(
        tag=f"v{version}",
        version=parsed,
        prerelease=False,
        body="notes",
        html_url=f"https://example.test/releases/v{version}",
        api_base="https://api.example",
        assets=(
            ReleaseAsset(
                name=archive_name,
                url=f"https://dl.example/{archive_name}",
                size=len(archive_bytes),
            ),
            ReleaseAsset(
                name=manifest_name,
                url=f"https://dl.example/{manifest_name}",
                size=len(manifest_bytes),
            ),
            ReleaseAsset(
                name="SHA256SUMS.txt",
                url="https://dl.example/SHA256SUMS.txt",
                size=len(sums_bytes),
            ),
        ),
    )


class _FakeHttp:
    def __init__(self, data: dict[str, bytes]):
        self.data = data
        self.calls: list[str] = []

    async def get_bytes(self, url, *, max_bytes=None, headers=None):
        self.calls.append(url)
        if url not in self.data:
            raise HttpError(f"missing {url}")
        return self.data[url]

    async def close(self):
        pass


def _sums(*items: tuple[str, bytes]) -> bytes:
    return "".join(
        f"{bytes_sha256(data)}  {name}\n" for name, data in items
    ).encode()


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clean(BASE)
        BASE.mkdir(parents=True, exist_ok=True)
        self.root = BASE / f"install-{uuid.uuid4().hex[:8]}"
        self.root.mkdir()

    def tearDown(self):
        _clean(BASE)

    def _install_current(
        self, version: str = "0.1.0", extra: dict[str, bytes] | None = None
    ) -> None:
        files = dict(_package_files(version))
        files.update(extra or {})
        for name, data in files.items():
            _write(self.root / name, data)
        package = self.root
        manifest = build_manifest(
            package, version=version, kind="standard", tag=f"v{version}",
            inline={"build-info.json": (package / "build-info.json").read_bytes()},
        )
        _write(
            self.root / "build-manifest.json",
            (json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n").encode(),
        )

    async def test_full_update_activates_new_version_and_pointer(self):
        self._install_current()
        package, manifest, _ = _make_package(BASE, "0.2.0")
        archive = _zip_package(package, BASE / "new.zip")
        manifest_bytes = (package / "build-manifest.json").read_bytes()
        sums = _sums(
            ("OnebotMaaLogAnalyzer-0.2.0.zip", archive),
            ("OnebotMaaLogAnalyzer-0.2.0.manifest.json", manifest_bytes),
        )
        release = _release(
            "0.2.0",
            manifest_bytes=manifest_bytes,
            archive_bytes=archive,
            sums_bytes=sums,
        )
        fake = _FakeHttp({
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.zip": archive,
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.manifest.json": manifest_bytes,
            "https://dl.example/SHA256SUMS.txt": sums,
        })
        installer = VersionInstaller(
            install=detect_install(self.root),
            data_dir=self.root / "data",
            config_path=self.root / "config.json",
            http=fake,
        )
        prepared = await installer.prepare(release, download_mode="full")
        self.assertEqual(prepared.mode, "full")
        pointer = installer.activate(prepared)
        self.assertEqual(pointer["active"], "0.2.0")
        self.assertTrue((self.root / "versions" / "0.2.0" / "payload.txt").is_file())
        self.assertTrue((self.root / "main.py.legacy").is_file())
        current = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(current["active"], "0.2.0")
        self.assertFalse(current["confirmed"])

    async def test_incremental_downloads_only_changed_raw_file(self):
        self._install_current()
        files = dict(_package_files("0.2.0"))
        files["payload.txt"] = b"changed payload\n"
        package, manifest, contents = _make_package(BASE, "0.2.0", files=files)
        manifest_bytes = (package / "build-manifest.json").read_bytes()
        # Zip is intentionally absent: incremental must not request it.
        sums = _sums(
            ("OnebotMaaLogAnalyzer-0.2.0.zip", b"unused"),
            ("OnebotMaaLogAnalyzer-0.2.0.manifest.json", manifest_bytes),
        )
        release = _release(
            "0.2.0",
            manifest_bytes=manifest_bytes,
            archive_bytes=b"unused",
            sums_bytes=sums,
        )
        changed_url = raw_url("v0.2.0", manifest.files["payload.txt"].source)
        fake = _FakeHttp({
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.manifest.json": manifest_bytes,
            "https://dl.example/SHA256SUMS.txt": sums,
            changed_url: b"changed payload\n",
        })
        installer = VersionInstaller(
            install=detect_install(self.root),
            data_dir=self.root / "data",
            config_path=self.root / "config.json",
            http=fake,
        )
        prepared = await installer.prepare(release, download_mode="incremental")
        self.assertEqual(prepared.mode, "incremental")
        self.assertIn("payload.txt", prepared.changed)
        self.assertFalse(any("0.2.0.zip" in url for url in fake.calls))
        self.assertTrue(any(changed_url in url for url in fake.calls))
        self.assertEqual(
            (prepared.version_dir / "payload.txt").read_bytes(),
            b"changed payload\n",
        )

    async def test_auto_mode_falls_back_to_full_when_incremental_fails(self):
        self._install_current()
        files = dict(_package_files("0.2.0"))
        files["payload.txt"] = b"changed payload\n"
        package, manifest, _ = _make_package(BASE, "0.2.0", files=files)
        archive = _zip_package(package, BASE / "fallback.zip")
        manifest_bytes = (package / "build-manifest.json").read_bytes()
        sums = _sums(
            ("OnebotMaaLogAnalyzer-0.2.0.zip", archive),
            ("OnebotMaaLogAnalyzer-0.2.0.manifest.json", manifest_bytes),
        )
        release = _release(
            "0.2.0",
            manifest_bytes=manifest_bytes,
            archive_bytes=archive,
            sums_bytes=sums,
        )
        # Deliberately omit the raw changed file: incremental fails, then the
        # auto mode must fetch and verify the complete archive instead.
        fake = _FakeHttp({
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.zip": archive,
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.manifest.json": manifest_bytes,
            "https://dl.example/SHA256SUMS.txt": sums,
        })
        installer = VersionInstaller(
            install=detect_install(self.root),
            data_dir=self.root / "data",
            http=fake,
        )
        prepared = await installer.prepare(release, download_mode="auto")
        self.assertEqual(prepared.mode, "full")
        self.assertTrue(prepared.warnings)
        self.assertTrue(any("0.2.0.zip" in c for c in fake.calls))

    async def test_checksum_mismatch_refuses_activation(self):
        self._install_current()
        package, _, _ = _make_package(BASE, "0.2.0")
        archive = _zip_package(package, BASE / "new.zip")
        manifest_bytes = (package / "build-manifest.json").read_bytes()
        bad_sums = _sums(
            ("OnebotMaaLogAnalyzer-0.2.0.zip", archive),
            ("OnebotMaaLogAnalyzer-0.2.0.manifest.json", b"not the manifest"),
        )
        release = _release(
            "0.2.0",
            manifest_bytes=manifest_bytes,
            archive_bytes=archive,
            sums_bytes=bad_sums,
        )
        fake = _FakeHttp({
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.manifest.json": manifest_bytes,
            "https://dl.example/SHA256SUMS.txt": bad_sums,
        })
        installer = VersionInstaller(
            install=detect_install(self.root),
            data_dir=self.root / "data",
            http=fake,
        )
        with self.assertRaises(Exception):
            await installer.prepare(release, download_mode="full")
        self.assertFalse((self.root / "current.json").exists())
        self.assertFalse((self.root / "versions" / "0.2.0").exists())

    async def test_unlisted_files_are_removed(self):
        package, manifest, _ = _make_package(BASE, "0.2.0")
        extra = package / "evil.py"
        extra.write_text("print('evil')", encoding="utf-8")
        VersionInstaller._prune_unlisted(package, manifest)
        self.assertFalse(extra.exists())

    async def test_zip_slip_is_rejected(self):
        archive = BASE / "evil.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("OnebotMaaLogAnalyzer/../evil.txt", b"evil")
        with self.assertRaises(Exception):
            VersionInstaller._extract_archive(
                archive.read_bytes(), BASE / "extract"
            )

    # ── 内置模板 / 用户模板 ──────────────────────────────────────

    async def _prepare_and_activate(
        self,
        files: dict[str, bytes],
        *,
        download_mode: str = "full",
        update_templates: bool = True,
        raw: dict[str, bytes] | None = None,
    ) -> tuple[Any, _FakeHttp]:
        """Build a 0.2.0 release from ``files``, prepare it and activate it."""
        package, _, _ = _make_package(BASE, "0.2.0", files=files)
        archive = _zip_package(package, BASE / f"new-{uuid.uuid4().hex[:6]}.zip")
        manifest_bytes = (package / "build-manifest.json").read_bytes()
        sums = _sums(
            ("OnebotMaaLogAnalyzer-0.2.0.zip", archive),
            ("OnebotMaaLogAnalyzer-0.2.0.manifest.json", manifest_bytes),
        )
        release = _release(
            "0.2.0",
            manifest_bytes=manifest_bytes,
            archive_bytes=archive,
            sums_bytes=sums,
        )
        served = {
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.zip": archive,
            "https://dl.example/OnebotMaaLogAnalyzer-0.2.0.manifest.json": (
                manifest_bytes
            ),
            "https://dl.example/SHA256SUMS.txt": sums,
        }
        for path, data in (raw or {}).items():
            served[raw_url("v0.2.0", path)] = data
        fake = _FakeHttp(served)
        installer = VersionInstaller(
            install=detect_install(self.root),
            data_dir=self.root / "data",
            config_path=self.root / "config.json",
            http=fake,
        )
        prepared = await installer.prepare(release, download_mode=download_mode)
        installer.activate(prepared, update_templates=update_templates)
        return prepared, fake

    async def test_locally_modified_file_is_redownloaded_not_copied(self):
        """用户改过的文件不能进新版本快照，否则整次更新会校验失败。"""
        self._install_current()
        _write(self.root / "templates/default/page.html", b"<html>user</html>")
        files = dict(_package_files("0.2.0"))
        files["payload.txt"] = b"changed payload\n"
        prepared, fake = await self._prepare_and_activate(
            files,
            download_mode="auto",
            raw={
                "templates/default/page.html": b"<html></html>",
                "payload.txt": b"changed payload\n",
            },
        )
        self.assertEqual(prepared.mode, "incremental")
        self.assertFalse(any("0.2.0.zip" in url for url in fake.calls))
        self.assertEqual(
            (prepared.version_dir / "templates/default/page.html").read_bytes(),
            b"<html></html>",
        )
        # 安装根里的用户改动仍然保留
        self.assertEqual(
            (self.root / "templates/default/page.html").read_bytes(),
            b"<html>user</html>",
        )

    async def test_update_templates_disabled_freezes_builtin_templates(self):
        self._install_current()
        files = dict(_package_files("0.2.0"))
        files["templates/default/page.html"] = b"<html>NEW</html>"
        files["templates/fresh/page.html"] = b"<html>NEW THEME</html>"
        await self._prepare_and_activate(files, update_templates=False)
        self.assertEqual(
            (self.root / "templates/default/page.html").read_bytes(),
            b"<html></html>",
        )
        self.assertFalse((self.root / "templates/fresh").exists())

    async def test_builtin_templates_follow_release_and_user_files_stay(self):
        self._install_current(
            extra={"templates/default/style.css": b"/* old css */"}
        )
        _write(self.root / "templates/default/style.css", b"/* edited by user */")
        _write(self.root / "templates/default/notes.txt", b"user note")
        _write(self.root / "templates/mine/page.html", b"<html>{{content}}</html>")
        files = dict(_package_files("0.2.0"))
        files["templates/default/page.html"] = b"<html>NEW</html>"
        files["templates/default/style.css"] = b"/* release css */"
        files["templates/default/extra.html"] = b"<i>new optional file</i>"
        await self._prepare_and_activate(files)
        templates = self.root / "templates"
        # 内置文件跟随新版本
        self.assertEqual(
            (templates / "default/page.html").read_bytes(), b"<html>NEW</html>"
        )
        self.assertTrue((templates / "default/extra.html").is_file())
        # 用户改过的内置文件、用户自建文件、用户自建主题都保留
        self.assertEqual(
            (templates / "default/style.css").read_bytes(),
            b"/* edited by user */",
        )
        self.assertEqual(
            (templates / "default/notes.txt").read_bytes(), b"user note"
        )
        self.assertEqual(
            (templates / "mine/page.html").read_bytes(),
            b"<html>{{content}}</html>",
        )

    async def test_removed_builtin_template_is_deleted(self):
        self._install_current(
            extra={"templates/default/style.css": b"/* old css */"}
        )
        files = dict(_package_files("0.2.0"))  # 新版本不再内置 style.css
        await self._prepare_and_activate(files)
        templates = self.root / "templates"
        self.assertFalse((templates / "default/style.css").exists())
        self.assertTrue((templates / "default/page.html").is_file())

    # ── 引导器成对替换 ──────────────────────────────────────────

    def _launcher_install(self) -> None:
        """当前安装使用真实的“入口 + 引导器”结构。"""
        self._install_current(
            extra={"main.py": LAUNCHER_V1, "maa_bootstrap.py": BOOTSTRAP_V1}
        )

    async def test_launcher_pair_crash_states_are_validated(self):
        """两次 rename 之间的中间态也必须先跑通。"""
        self._launcher_install()
        seen: list[tuple[bytes, bytes]] = []
        original = VersionInstaller._check_launcher_pair

        def spy(installer, stage):
            seen.append(
                (
                    (stage / "main.py").read_bytes(),
                    (stage / "maa_bootstrap.py").read_bytes(),
                )
            )
            return original(installer, stage)

        files = dict(_package_files("0.2.0"))
        files.update({"main.py": LAUNCHER_V2, "maa_bootstrap.py": BOOTSTRAP_V2})
        with patch.object(VersionInstaller, "_check_launcher_pair", spy):
            await self._prepare_and_activate(files)
        self.assertIn((LAUNCHER_V1, BOOTSTRAP_V2), seen)  # 崩溃窗口
        self.assertIn((LAUNCHER_V2, BOOTSTRAP_V2), seen)  # 最终态

    async def test_broken_launcher_pair_leaves_install_root_untouched(self):
        self._launcher_install()
        files = dict(_package_files("0.2.0"))
        files.update(
            {
                "main.py": LAUNCHER_V2,
                "maa_bootstrap.py": b"raise ImportError('broken bootstrap')\n",
            }
        )
        with self.assertRaises(Exception):
            await self._prepare_and_activate(files)
        self.assertEqual((self.root / "main.py").read_bytes(), LAUNCHER_V1)
        self.assertEqual(
            (self.root / "maa_bootstrap.py").read_bytes(), BOOTSTRAP_V1
        )
        self.assertFalse((self.root / "current.json").exists())
        updates = self.root / ".updates"
        if updates.is_dir():
            self.assertFalse(
                any(p.name.startswith("launcher-") for p in updates.iterdir())
            )

    async def test_replaced_launchers_keep_prev_copies(self):
        self._launcher_install()
        files = dict(_package_files("0.2.0"))
        files.update({"main.py": LAUNCHER_V2, "maa_bootstrap.py": BOOTSTRAP_V2})
        await self._prepare_and_activate(files)
        self.assertEqual((self.root / "main.py").read_bytes(), LAUNCHER_V2)
        self.assertEqual(
            (self.root / "maa_bootstrap.py").read_bytes(), BOOTSTRAP_V2
        )
        self.assertEqual((self.root / "main.py.prev").read_bytes(), LAUNCHER_V1)
        self.assertEqual(
            (self.root / "maa_bootstrap.py.prev").read_bytes(), BOOTSTRAP_V1
        )

    async def test_real_launcher_pair_passes_staging_check(self):
        """仓库里的真实入口/引导器在暂存目录里也能对着安装根自检。"""
        launcher = (REPO / "main.py").read_bytes()
        bootstrap = (REPO / "maa_bootstrap.py").read_bytes()
        self._install_current(
            extra={"main.py": launcher, "maa_bootstrap.py": bootstrap}
        )
        files = dict(_package_files("0.2.0"))
        files.update({"main.py": launcher, "maa_bootstrap.py": bootstrap})
        await self._prepare_and_activate(files)
        self.assertTrue((self.root / "current.json").is_file())
        self.assertEqual((self.root / "main.py").read_bytes(), launcher)


if __name__ == "__main__":
    unittest.main()
