"""
HistoryStore：记录 ID 唯一性、坏索引隔离、按配置清理、体积口径。

Run with: python -m unittest discover -s tests -v
"""
import contextlib
import json
import shutil
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.models import AnalysisSettings
from features.maa import history as history_mod
from features.maa.history import HistoryStore, new_record_id


@contextlib.contextmanager
def _tmpdir():
    """仓库根下的临时目录（沙箱里只有仓库根可写；删除失败忽略）。"""
    base = Path("_tmp_history_store")
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


def _store(tmp: Path) -> HistoryStore:
    return HistoryStore(tmp)


def _add(store: HistoryStore, name: str = "cfg", **kwargs):
    base = dict(config_name=name, group_id="1", file_name="logs.zip",
                context="ctx", report="report")
    base.update(kwargs)
    return store.add(**base)


class RecordIdTests(unittest.TestCase):
    def test_ids_are_unique_and_nanosecond_based(self):
        ids = {new_record_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for rid in ids:
            self.assertRegex(rid, r"^\d{15,}_[0-9a-f]{6}$")

    def test_add_never_reuses_an_existing_directory(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            first = _add(store)
            self.assertIsNotNone(first)
            # 固定返回同一个 ID：第二次必须拒绝覆盖，而不是复用目录
            with patch.object(history_mod, "new_record_id", return_value=first.id):
                second = _add(store)
            self.assertIsNone(second, "ID 冲突时应跳过归档而不是覆盖")
            self.assertTrue((store.record_dir("cfg", first.id) / "context.txt").is_file())
            self.assertEqual(len(store.load("cfg")), 1)

    def test_add_retries_when_first_candidate_collides(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            first = _add(store)
            second_id = f"{time.time_ns()}_{'beef01'}"
            with patch.object(history_mod, "new_record_id",
                              side_effect=[first.id, second_id]):
                second = _add(store)
            self.assertIsNotNone(second)
            self.assertEqual(second.id, second_id)
            self.assertEqual(len(store.load("cfg")), 2)


class BrokenIndexTests(unittest.TestCase):
    def test_bad_index_is_backed_up_not_overwritten(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            store._records_path("cfg").parent.mkdir(parents=True, exist_ok=True)
            store._records_path("cfg").write_text("{ 坏掉的 json", encoding="utf-8")

            self.assertEqual(store.load("cfg"), [])
            backups = list(store._config_dir("cfg").glob("records.json.bad-*"))
            self.assertEqual(len(backups), 1, "坏索引必须先备份")
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ 坏掉的 json")

            record = _add(store)
            self.assertIsNotNone(record)
            payload = json.loads(store._records_path("cfg").read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1, "重建后的索引只含新记录")
            self.assertTrue(backups[0].is_file(), "备份不能被后续写盘覆盖")

    def test_unwritable_when_backup_fails(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            path = store._records_path("cfg")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ 坏掉的 json", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("denied")):
                self.assertEqual(store.load("cfg"), [])
            self.assertIn("cfg", store._unwritable)

            _add(store)
            self.assertEqual(
                path.read_text(encoding="utf-8"), "{ 坏掉的 json",
                "保护状态下绝不能写回（否则唯一一份数据被覆盖成空表）",
            )


class PerConfigCleanupTests(unittest.TestCase):
    """C6 回归：每个配置各清各的，孤儿配置也要清。"""

    def _seed(self, store: HistoryStore, name: str, *, age_hours: float):
        record = _add(store, name=name)
        record.created_at = time.time() - age_hours * 3600
        store.save(name)
        return record

    def test_every_config_gets_cleaned(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            self._seed(store, "cfg-a", age_hours=48)
            self._seed(store, "cfg-b", age_hours=48)
            settings = AnalysisSettings()
            summary = store.cleanup_all({"cfg-a": settings, "cfg-b": settings})
            self.assertEqual(summary["cfg-a"].removed, 1)
            self.assertEqual(summary["cfg-b"].removed, 1, "第二个配置过去从不清理")
            self.assertEqual(store.load("cfg-a"), [])
            self.assertEqual(store.load("cfg-b"), [])

    def test_orphan_config_history_is_cleaned(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            self._seed(store, "已删除的配置", age_hours=48)
            summary = store.cleanup_all({})          # 配置列表里已经没有它
            self.assertEqual(summary["已删除的配置"].removed, 1)
            self.assertEqual(store.load("已删除的配置"), [])

    def test_disabled_history_is_still_cleaned(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            self._seed(store, "cfg", age_hours=48)
            settings = AnalysisSettings(history_enabled=False)
            summary = store.cleanup_all({"cfg": settings})
            self.assertEqual(summary["cfg"].removed, 1, "关掉历史不该留着旧归档")

    def test_force_does_not_reset_period_timer(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            _add(store)
            store.cleanup("cfg", period_hours=1, keep_periods=2, force=True)
            self.assertEqual(store._last_cleanup, {})
            store.cleanup("cfg", period_hours=1, keep_periods=2)
            self.assertIn("cfg", store._last_cleanup)


class OrphanAndAccountingTests(unittest.TestCase):
    def test_force_removes_orphan_record_dirs(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            history_dir = store._config_dir("cfg")
            history_dir.mkdir(parents=True, exist_ok=True)
            orphan = history_dir / f"{int(time.time())}_9"
            orphan.mkdir()
            (orphan / "context.txt").write_text("x", encoding="utf-8")

            store.cleanup("cfg", period_hours=1, keep_periods=2)   # 非 force 不动
            self.assertTrue(orphan.is_dir())

            result = store.cleanup("cfg", period_hours=1, keep_periods=2, force=True)
            self.assertFalse(orphan.exists())
            self.assertGreater(result.freed_bytes, 0)

    def test_remove_record_reports_nothing_when_delete_fails(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            record = _add(store)
            with patch.object(history_mod.shutil, "rmtree", lambda *a, **kw: None):
                freed = store._remove_record("cfg", record)
            self.assertEqual(freed, 0, "删不掉就不能算已释放")
            self.assertGreater(store._remove_record("cfg", record), 0)

    def test_index_bytes_count_into_stats_and_budget(self):
        with _tmpdir() as tmp:
            store = _store(tmp)
            _add(store)
            stats = store.stats("cfg")
            dir_only = sum(
                item.stat().st_size
                for item in store.record_dir("cfg", store.load("cfg")[0].id).rglob("*")
                if item.is_file()
            )
            self.assertGreater(stats["bytes"], dir_only, "报告正文在 records.json 里，必须计入")
            self.assertEqual(
                stats["bytes"], dir_only + store._index_bytes("cfg")
            )


if __name__ == "__main__":
    unittest.main()