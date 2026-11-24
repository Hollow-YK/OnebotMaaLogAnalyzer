"""Stable launcher candidate selection and rollback."""
from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import maa_bootstrap

BASE = Path("_tmp_update_bootstrap")


def _clean(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _version(root: Path, version: str, *, kind: str = "standard") -> Path:
    directory = root / "versions" / version
    _write(directory / "build-info.json", json.dumps({
        "schema": 1, "version": version, "kind": kind,
        "repo": "Hollow-YK/OnebotMaaLogAnalyzer",
    }))
    _write(directory / "maa_analyzer" / "__init__.py")
    _write(directory / "maa_analyzer" / "bare_entry.py")
    if kind == "astrbot":
        _write(directory / "plugin_shell.py", "class Main:\n    pass\n")
    return directory


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        _clean(BASE)
        BASE.mkdir(parents=True, exist_ok=True)
        self.root = BASE / f"root-{uuid.uuid4().hex[:8]}"
        self.root.mkdir()
        self.patch = patch.object(maa_bootstrap, "ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        _clean(BASE)

    def test_bad_active_version_is_skipped(self):
        _version(self.root, "0.1.0")
        _version(self.root, "0.2.0")
        _write(self.root / "current.json", json.dumps({
            "active": "0.2.0", "previous": "0.1.0",
            "kind": "standard", "attempts": 2, "confirmed": False,
        }))
        candidates = maa_bootstrap.bare_candidates()
        self.assertEqual(candidates[0]["version"], "0.1.0")
        self.assertFalse(any(c["version"] == "0.2.0" for c in candidates))

    def test_rollback_marks_bad_and_restores_config(self):
        _version(self.root, "0.1.0")
        _version(self.root, "0.2.0")
        _write(self.root / "config.json", "new")
        backup = self.root / ".updates" / "config.json"
        _write(backup, "old")
        _write(self.root / "current.json", json.dumps({
            "active": "0.2.0", "previous": "0.1.0",
            "kind": "standard", "confirmed": False, "attempts": 1,
            "restore": [{"target": str(self.root / "config.json"),
                         "backup": str(backup)}],
        }))
        pointer = maa_bootstrap.rollback("boom")
        self.assertEqual(pointer["active"], "0.1.0")
        self.assertIn("0.2.0", pointer["bad"])
        self.assertEqual((self.root / "config.json").read_text(), "old")

    def test_astrbot_shim_loads_versioned_plugin(self):
        directory = _version(self.root, "0.1.0", kind="astrbot")
        _write(self.root / "current.json", json.dumps({
            "active": "0.1.0", "kind": "astrbot", "confirmed": True,
        }))
        main_cls = maa_bootstrap.load_astrbot_main()
        self.assertIsNotNone(main_cls)
        self.assertTrue((directory / "plugin_shell.py").is_file())

    def test_unconfirmed_failing_version_is_marked_bad(self):
        directory = _version(self.root, "0.2.0")
        _write(
            directory / "maa_analyzer" / "bare_entry.py",
            "import sys\nsys.exit(1)\n",
        )
        _write(self.root / "current.json", json.dumps({
            "active": "0.2.0", "previous": "", "kind": "standard",
            "attempts": 0, "confirmed": False,
        }))
        with patch.object(maa_bootstrap, "BOOT_TIMEOUT_SECONDS", 1):
            code = maa_bootstrap.run_bare_launcher()
        self.assertEqual(code, 1)
        pointer = json.loads((self.root / "current.json").read_text())
        self.assertIn("0.2.0", pointer["bad"])

    def test_launcher_check_requires_a_candidate(self):
        self.assertEqual(maa_bootstrap.launcher_check(), 1)
        _version(self.root, "0.1.0")
        self.assertEqual(maa_bootstrap.launcher_check(), 0)

    def test_astrbot_check_requires_a_candidate(self):
        self.assertEqual(maa_bootstrap.astrbot_check(), 1)
        _version(self.root, "0.1.0", kind="astrbot")
        self.assertEqual(maa_bootstrap.astrbot_check(), 0)

    def test_install_root_comes_from_environment(self):
        """暂存自检靠 MAA_INSTALL_ROOT 指向真实安装根。"""
        import importlib
        import os

        key = "MAA_INSTALL_ROOT"
        previous = os.environ.get(key)
        os.environ[key] = str(self.root)
        try:
            importlib.reload(maa_bootstrap)
            self.assertEqual(maa_bootstrap.ROOT, self.root.resolve())
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
            importlib.reload(maa_bootstrap)
        self.assertEqual(
            maa_bootstrap.ROOT,
            Path(maa_bootstrap.__file__).resolve().parent,
        )


if __name__ == "__main__":
    unittest.main()
