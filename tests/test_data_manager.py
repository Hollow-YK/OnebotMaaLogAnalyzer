"""
DataManager：原子写、损坏文件先备份、配置名清洗。

Run with: python -m unittest discover -s tests -v
"""
import contextlib
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core import data_manager as dm_mod
from core.data_manager import DataManager, safe_name
from core.models import AnalysisSettings


@contextlib.contextmanager
def _tmpdir():
    """仓库根下的临时目录（沙箱里只有仓库根可写；删除失败忽略）。"""
    base = Path("_tmp_data_manager")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()
        except OSError:
            pass


class SafeNameTests(unittest.TestCase):
    def test_keeps_unicode_alnum_and_safe_chars(self):
        self.assertEqual(safe_name("探针"), "探针")
        self.assertEqual(safe_name("cfg-A_1.2"), "cfg-A_1.2")

    def test_strips_path_components(self):
        self.assertEqual(safe_name("../evil"), ".._evil")
        self.assertEqual(safe_name("a/b"), "a_b")
        self.assertEqual(safe_name(r"a\b"), "a_b")
        self.assertEqual(safe_name("/abs"), "_abs")
        self.assertEqual(safe_name(r"C:\x"), "C__x")
        self.assertEqual(safe_name("cfg "), "cfg")     # 先 strip 再清洗

    def test_all_dots_and_empty_fall_back(self):
        self.assertEqual(safe_name(""), "default")
        self.assertEqual(safe_name("   "), "default")
        self.assertEqual(safe_name("."), "default")
        self.assertEqual(safe_name(".."), "default")
        self.assertEqual(safe_name("..."), "default")

    def test_truncates(self):
        self.assertEqual(len(safe_name("x" * 200)), 60)


class ConfigDirTests(unittest.TestCase):
    def test_never_escapes_data_dir(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp / "data"))
            for name in ("../evil", "a/b", r"..\evil", "/etc", r"C:\Windows"):
                target = dm._config_dir(name)
                self.assertEqual(
                    target.parent.resolve(), dm._dir.resolve(),
                    f"{name!r} 必须落在数据目录内",
                )

    def test_legacy_dir_is_reused(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp / "data"))
            legacy = dm._dir / "旧 名字"
            legacy.mkdir(parents=True, exist_ok=True)
            (legacy / "groups.json").write_text("{}", encoding="utf-8")
            self.assertEqual(dm._config_dir("旧 名字"), legacy, "升级后应沿用既有目录")


class AtomicWriteTests(unittest.TestCase):
    def test_no_tmp_left_and_content_readable(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            path = tmp / "x.json"
            dm._write_file(path, {"a": 1})
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})

    def test_failed_replace_keeps_original(self):
        """旧实现先 unlink 再 rename：写盘失败会直接丢文件。"""
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            path = tmp / "x.json"
            dm._write_file(path, {"old": True})
            with patch.object(dm_mod.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    dm._write_file(path, {"new": True})
            self.assertTrue(path.is_file(), "原文件不能被破坏")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_tmp_is_recovered(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            jobs = dm._config_dir("cfg") / "jobs"
            jobs.mkdir(parents=True, exist_ok=True)
            (jobs / "records.json.tmp").write_text("[]", encoding="utf-8")
            self.assertEqual(dm.load_config_jobs("cfg"), [])
            self.assertTrue((jobs / "records.json").is_file())
            self.assertFalse((jobs / "records.json.tmp").exists())


class CorruptFileTests(unittest.TestCase):
    def _backups(self, folder: Path, name: str) -> list:
        return list(folder.glob(f"{name}.corrupt-*"))

    def test_bad_global_settings_is_backed_up(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            path = tmp / "settings.json"
            path.write_text("{ 坏掉的", encoding="utf-8")

            settings = dm.load_global_settings()
            self.assertIsInstance(settings, AnalysisSettings)

            backups = self._backups(tmp, "settings.json")
            self.assertEqual(len(backups), 1, "坏文件必须先备份")
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ 坏掉的")
            # 重建后的 settings.json 必须是合法的 AnalysisSettings（落盘用别名）
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(
                AnalysisSettings.model_validate(payload), AnalysisSettings
            )

    def test_backup_failure_blocks_overwrite(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            path = tmp / "settings.json"
            path.write_text("{ 坏掉的", encoding="utf-8")

            with patch.object(dm_mod.os, "replace", side_effect=OSError("denied")):
                dm.load_global_settings()          # 备份失败 → 进入写保护
            self.assertIn(path, dm._protected)
            self.assertEqual(
                path.read_text(encoding="utf-8"), "{ 坏掉的",
                "保护状态下绝不能覆盖唯一一份数据",
            )

    def test_bad_groups_and_commands_are_backed_up(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            folder = dm._config_dir("cfg")
            (folder / "groups.json").write_text("[]", encoding="utf-8")     # 形状不对
            (folder / "commands.json").write_text("{ 坏", encoding="utf-8")

            dm.load_config_info("cfg")
            dm.load_config_commands("cfg")

            self.assertEqual(len(self._backups(folder, "groups.json")), 1)
            self.assertEqual(len(self._backups(folder, "commands.json")), 1)

    def test_bad_override_files_are_backed_up(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            folder = dm._config_dir("cfg")
            (folder / "settings.json").write_text("[1,2]", encoding="utf-8")
            (folder / "project.json").write_text("{ 坏", encoding="utf-8")

            self.assertIsNone(dm.load_config_settings("cfg"))
            self.assertIsNone(dm.load_config_project("cfg"))

            self.assertEqual(len(self._backups(folder, "settings.json")), 1)
            self.assertEqual(len(self._backups(folder, "project.json")), 1)

    def test_bad_jobs_records_are_backed_up(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            jobs = dm._config_dir("cfg") / "jobs"
            jobs.mkdir(parents=True, exist_ok=True)
            (jobs / "records.json").write_text("{ 坏", encoding="utf-8")

            self.assertEqual(dm.load_config_jobs("cfg"), [])
            self.assertEqual(len(self._backups(jobs, "records.json")), 1)

    def test_empty_overrides_delete_file(self):
        with _tmpdir() as tmp:
            dm = DataManager(str(tmp))
            dm.save_config_settings("cfg", {"a": 1})
            path = dm._config_dir("cfg") / "settings.json"
            self.assertTrue(path.is_file())
            dm.save_config_settings("cfg", {})
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()