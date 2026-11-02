"""
分卷日志包：齐全性判定（编号 + 内容）、额外等待、去重与冷却。

夹具版式照 `logs/log_20261019_141709-part01/02/03.zip`：part01 只有图片、
part02 是主日志、part03 是错误截图 + 一份日志。

Run with: python -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

from core.archive_names import parse_archive_part
from core.models import AnalysisSettings
from features.maa.analyzer import (
    ArchivePartsState,
    MaaAnalyzer,
    decide_parts_action,
    describe_parts_gap,
    judge_parts_state,
)
from features.maa.watcher import LogWatcher

BASE = "log_20261019_141709"


@contextlib.contextmanager
def _tmpdir():
    """仓库根下的临时目录（沙箱里只有仓库根可写；删除失败忽略）。"""
    base = Path("_tmp_archive_parts")
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


def _zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _sample_parts(folder: Path) -> dict:
    """1×图片 / 1×主日志 / 11×图片+1×日志 的三卷（成员名互不重复）。"""
    return {
        1: _zip(folder / f"{BASE}-part01.zip",
                {"debug/custom/a.png": b"png"}),
        2: _zip(folder / f"{BASE}-part02.zip",
                {"debug/maafw.log": b"log"}),
        3: _zip(folder / f"{BASE}-part03.zip",
                {"debug/on_error/b.png": b"png", "logs/log-x.log": b"log"}),
    }


def _merge(parts: dict, numbers: list, target: Path) -> Path:
    MaaAnalyzer._merge_zip_parts([parts[n] for n in numbers], target)
    return target


class PartNameTests(unittest.TestCase):
    def test_parse_part(self):
        part = parse_archive_part(f"{BASE}-part03.zip")
        self.assertIsNotNone(part)
        self.assertEqual(part.base, BASE)
        self.assertEqual(part.number, 3)

    def test_merge_keeps_all_members(self):
        with _tmpdir() as tmp:
            parts = _sample_parts(tmp)
            merged = _merge(parts, [1, 2, 3], tmp / "merged.zip")
            with zipfile.ZipFile(merged) as archive:
                names = set(archive.namelist())
            self.assertEqual(names, {"debug/custom/a.png", "debug/maafw.log",
                                     "debug/on_error/b.png", "logs/log-x.log"})


class PartsStateTests(unittest.TestCase):
    def _state(self, tmp, numbers, candidates=None):
        parts = _sample_parts(tmp)
        merged = _merge(parts, numbers, tmp / "m.zip")
        used = {n: parts[n].name for n in numbers}
        return judge_parts_state(merged, used, candidates or {})

    def test_first_part_only_has_no_log_content(self):
        """样本里主日志在 part02：只到 part01 时必须判为不完整。"""
        with _tmpdir() as tmp:
            state = self._state(tmp, [1])
            self.assertFalse(state.has_log_content)
            self.assertTrue(state.incomplete)
            self.assertEqual(state.used, [1])

    def test_second_part_brings_the_log(self):
        with _tmpdir() as tmp:
            state = self._state(tmp, [1, 2])
            self.assertTrue(state.has_log_content)
            self.assertFalse(state.incomplete, "有日志正文就该能分析")

    def test_number_gap_is_incomplete(self):
        with _tmpdir() as tmp:
            state = self._state(tmp, [1, 3])
            self.assertIn("part02", state.missing)
            self.assertTrue(state.incomplete)

    def test_listed_but_not_downloaded_is_incomplete(self):
        with _tmpdir() as tmp:
            parts = _sample_parts(tmp)
            state = self._state(
                tmp, [1], candidates={1: (parts[1].name, "1", 1),
                                       2: (parts[2].name, "2", 2)},
            )
            self.assertIn(parts[2].name, state.missing)
            self.assertTrue(state.incomplete)

    def test_gap_text_is_human_readable(self):
        with _tmpdir() as tmp:
            state = self._state(tmp, [1, 3])
            text = describe_parts_gap(state)
            self.assertIn("缺", text)
        with _tmpdir() as tmp:
            text = describe_parts_gap(self._state(tmp, [1]))
            self.assertIn("没有日志正文", text)


class PartsDecisionTests(unittest.TestCase):
    def _state(self, **kwargs):
        return ArchivePartsState(**kwargs)

    def test_ok_when_complete(self):
        state = self._state(used=[1, 2], has_log_content=True)
        self.assertEqual(
            decide_parts_action(state, require_content=True,
                                action="analyze", defer=True),
            "ok",
        )

    def test_defer_wins_when_rounds_left(self):
        state = self._state(used=[1], has_log_content=False)
        self.assertEqual(
            decide_parts_action(state, require_content=True,
                                action="cancel", defer=True),
            "defer",
        )

    def test_cancel_action(self):
        state = self._state(used=[1], has_log_content=False)
        self.assertEqual(
            decide_parts_action(state, require_content=True,
                                action="cancel", defer=False),
            "cancel",
        )

    def test_analyze_action_by_default(self):
        state = self._state(used=[1], has_log_content=False)
        self.assertEqual(
            decide_parts_action(state, require_content=True,
                                action="analyze", defer=False),
            "analyze",
        )

    def test_content_check_can_be_turned_off(self):
        state = self._state(used=[1], has_log_content=False)
        self.assertEqual(
            decide_parts_action(state, require_content=False,
                                action="analyze", defer=False),
            "ok",
            "关掉内容判据后只看编号",
        )

    def test_none_state_is_ok(self):
        self.assertEqual(
            decide_parts_action(None, require_content=True,
                                action="analyze", defer=True),
            "ok",
        )


class DeduplicateTests(unittest.TestCase):
    """F6：去重键不能再带 file_size；冷却键要对分卷命中。"""

    def _watcher(self) -> LogWatcher:
        watcher = LogWatcher.__new__(LogWatcher)
        watcher._recent = {}
        return watcher

    def test_same_name_different_size_is_duplicate(self):
        watcher = self._watcher()
        settings = AnalysisSettings()
        self.assertFalse(watcher._is_duplicate("1", f"{BASE}-part01.zip", 100, settings))
        self.assertTrue(
            watcher._is_duplicate("1", f"{BASE}-part01.zip", 0, settings),
            "大小字段缺失不该让同一文件被判成两次上传",
        )

    def test_part_cooling_hits_after_analysis(self):
        watcher = self._watcher()
        settings = AnalysisSettings()
        watcher._touch_recent("1", f"{BASE}-part02.zip", 10, settings)
        key = ("1", f"part:{BASE.lower()}")
        self.assertTrue(
            watcher._archive_cooling(key, settings),
            "分卷归档刚分析完，冷却必须命中（否则补传会重复分析）",
        )


def _async_none(*args, **kwargs):
    async def _run():
        return None
    return _run()


class PartBatchFlowTests(unittest.IsolatedAsyncioTestCase):
    """watcher 的额外等待循环：编号判据 + 内容判据 + 用尽后收尾。"""

    def _settings(self, **overrides) -> AnalysisSettings:
        base = dict(archive_part_settle_seconds=0.0,
                    archive_part_require_content=True,
                    archive_part_wait_extra_rounds=1,
                    archive_part_incomplete_action="analyze")
        base.update(overrides)
        return AnalysisSettings(**base)

    def _watcher(self, analyze):
        watcher = LogWatcher.__new__(LogWatcher)
        watcher._active = {}
        watcher._part_batches = {}
        watcher._recent = {}
        watcher._render_noticed = set()
        watcher.s = SimpleNamespace(send_text=_async_none)
        watcher._analyze = analyze
        return watcher

    def _batch(self, numbers, extra_waits=0):
        last = max(numbers)
        return {
            "parts": [f"{BASE}-part{n:02d}.zip" for n in sorted(numbers)],
            "numbers": set(numbers),
            "extra_waits": extra_waits,
            "task": None,
            "file_name": f"{BASE}-part{last:02d}.zip",
            "file_info": {"id": "1", "size": 10},
            "file_size": 10,
            "uploader": "u",
        }

    async def _drain(self, watcher, batch):
        task = batch.get("task")
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def test_lone_first_part_waits_one_more_round(self):
        calls: list = []

        async def fake_analyze(cfg, group_id, uploader, item, *,
                               defer_incomplete_parts=False):
            calls.append(defer_incomplete_parts)
            return SimpleNamespace(parts_incomplete=True, skipped=False)

        watcher = self._watcher(fake_analyze)
        key = ("1", f"part:{BASE.lower()}")
        batch = self._batch({1})
        watcher._part_batches[key] = batch
        await watcher._run_part_batch(
            SimpleNamespace(name="c"), "1", key, 0.0, self._settings()
        )
        self.assertEqual(calls, [], "只收到 part01 时先额外等待，不该开始分析")
        self.assertEqual(batch.get("extra_waits"), 1)
        self.assertIn(key, watcher._part_batches, "批次要放回去等下一轮")
        self.assertIsNone(watcher._active.get(key))
        self.assertEqual(watcher._recent, {}, "等待期间不记冷却，补传的卷才进得来")
        await self._drain(watcher, batch)

    async def test_incomplete_content_waits_one_more_round(self):
        calls: list = []

        async def fake_analyze(cfg, group_id, uploader, item, *,
                               defer_incomplete_parts=False):
            calls.append(defer_incomplete_parts)
            return SimpleNamespace(parts_incomplete=True, skipped=False)

        watcher = self._watcher(fake_analyze)
        key = ("1", f"part:{BASE.lower()}")
        batch = self._batch({1, 2})
        watcher._part_batches[key] = batch
        await watcher._run_part_batch(
            SimpleNamespace(name="c"), "1", key, 0.0, self._settings()
        )
        self.assertEqual(calls, [True], "编号看着齐了，仍由内容判据要求再等一轮")
        self.assertEqual(batch.get("extra_waits"), 1)
        self.assertEqual(watcher._recent, {})
        await self._drain(watcher, batch)

    async def test_last_round_runs_analysis_and_records_cooling(self):
        calls: list = []

        async def fake_analyze(cfg, group_id, uploader, item, *,
                               defer_incomplete_parts=False):
            calls.append(defer_incomplete_parts)
            return SimpleNamespace(parts_incomplete=False, skipped=False)

        watcher = self._watcher(fake_analyze)
        key = ("1", f"part:{BASE.lower()}")
        batch = self._batch({1, 2}, extra_waits=1)   # 余量已用完
        watcher._part_batches[key] = batch
        await watcher._run_part_batch(
            SimpleNamespace(name="c"), "1", key, 0.0, self._settings()
        )
        self.assertEqual(calls, [False])
        self.assertNotIn(key, watcher._part_batches, "收尾后不该再排一轮")
        self.assertTrue(watcher._recent, "正常结束时按旧行为记冷却")


if __name__ == "__main__":
    unittest.main()