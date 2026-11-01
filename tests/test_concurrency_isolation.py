"""
并发模型测试 — 「配置之间隔离、配置内排队、渲染排队」。

Run with: python -m unittest discover -s tests -v
"""
import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.concurrency import KeyedLocks, KeyedThreadLocks, run_blocking
from core.models import AnalysisSettings, ProjectConfig, ReportConfig
from core.service import MaaService
from features.maa.analyzer import AnalysisOutcome, MaaAnalyzer
from features.maa.followup import FollowupStore, resolve_window_minutes

import features.maa.render as render_mod


def _settings(**overrides) -> AnalysisSettings:
    base = dict(
        followup_window_minutes=10,
        followup_max_turns=3,
        followup_max_sessions=5,
        history_enabled=True,
        history_period_hours=24,
        history_keep_periods=2,
    )
    base.update(overrides)
    return AnalysisSettings(**base)


class KeyedLockTests(unittest.TestCase):
    def test_same_key_shares_lock_other_keys_are_independent(self):
        locks = KeyedLocks()
        self.assertIs(locks.get("a"), locks.get("a"))
        self.assertIsNot(locks.get("a"), locks.get("b"))

    def test_discard_keeps_busy_lock(self):
        locks = KeyedLocks()
        lock = locks.get("a")
        lock._locked = True  # 模拟持有中
        self.assertFalse(locks.discard("a"))
        lock._locked = False
        self.assertTrue(locks.discard("a"))

    def test_thread_locks_are_per_key(self):
        locks = KeyedThreadLocks()
        self.assertIs(locks.get("a"), locks.get("a"))
        self.assertIsNot(locks.get("a"), locks.get("b"))

    def test_lock_rebinds_across_event_loops(self):
        """同一进程里换了事件循环（asyncio.run 反复调用）不得复用旧锁。"""
        locks = KeyedLocks()

        async def use():
            async with locks.get("k"):
                return locks.get("k")

        async def twice():
            # 同一个循环内两次取到同一把锁
            return await use(), await use()

        a1, a2 = asyncio.run(twice())
        self.assertIs(a1, a2)
        b1, _b2 = asyncio.run(twice())
        self.assertIsNot(a1, b1)


class BlockingOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_blocking_leaves_event_loop_thread(self):
        loop_thread = threading.get_ident()
        seen: dict = {}

        def work(value):
            seen["thread"] = threading.get_ident()
            return value * 2

        result = await run_blocking(work, 21)
        self.assertEqual(result, 42)
        self.assertNotEqual(seen["thread"], loop_thread)

    async def test_run_blocking_respects_limit(self):
        active = 0
        peak = 0

        def work():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.05)
            active -= 1

        await asyncio.gather(*(run_blocking(work, limit=2) for _ in range(6)))
        self.assertLessEqual(peak, 2)


class AnalyzerSerializationTests(unittest.IsolatedAsyncioTestCase):
    """同一配置排队串行；不同配置互不影响。"""

    def _analyzer(self):
        return MaaAnalyzer(SimpleNamespace(), llm=SimpleNamespace(), history=None)

    def _stub(self, analyzer, events):
        async def fake_serialized(**kwargs):
            name = kwargs["config_name"]
            events.append(("start", name, time.monotonic()))
            await asyncio.sleep(0.05)
            events.append(("end", name, time.monotonic()))
            return AnalysisOutcome(ok=True, file_name=kwargs["file_name"])

        analyzer._analyze_serialized = fake_serialized

    async def _run_pair(self, analyzer, configs):
        settings = _settings()
        await asyncio.gather(*(
            analyzer.analyze(
                config_name=name, settings=settings, project=ProjectConfig(),
                group_id=1, file_name=f"{name}{index}.zip",
                file_id=str(index), file_size=10 + index,
            )
            for index, name in enumerate(configs)
        ))

    @staticmethod
    def _span(events, name):
        starts = [t for kind, n, t in events if n == name and kind == "start"]
        ends = [t for kind, n, t in events if n == name and kind == "end"]
        return starts, ends

    async def test_same_config_never_overlaps(self):
        analyzer = self._analyzer()
        events: list = []
        self._stub(analyzer, events)
        await self._run_pair(analyzer, ["a", "a"])
        starts, ends = self._span(events, "a")
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1], ends[0], "同一配置的分析不得并行")

    async def test_different_configs_run_in_parallel(self):
        analyzer = self._analyzer()
        events: list = []
        self._stub(analyzer, events)
        await self._run_pair(analyzer, ["a", "b"])
        starts_a, ends_a = self._span(events, "a")
        starts_b, ends_b = self._span(events, "b")
        # 不同配置应当有重叠区间，而不是排队等对方跑完
        self.assertLess(starts_b[0], ends_a[0], "B 应在 A 结束前就开始")
        self.assertLess(starts_a[0], ends_b[0], "A 应在 B 结束前就开始")

    def test_analysis_concurrency_setting_removed(self):
        self.assertFalse(hasattr(AnalysisSettings(), "analysis_concurrency"))


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    """同一 data_dir 的渲染排队；不同 data_dir 互不影响。"""

    async def _render_twice(self, data_dirs):
        active = 0
        peak = 0

        def fake_render_sync(markdown, report, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.05)
            active -= 1
            return render_mod.RenderOutcome(images=[b"png"], used_browser=True)

        report = ReportConfig(render="image")
        with patch.object(render_mod, "render_sync", fake_render_sync):
            await asyncio.gather(*(
                render_mod.render_report("body", report, meta={}, data_dir=name)
                for name in data_dirs
            ))
        return peak

    async def test_same_data_dir_serializes(self):
        self.assertEqual(await self._render_twice(["dataA", "dataA"]), 1)

    async def test_same_data_dir_serializes_with_different_browser_and_template(self):
        """排队按 data_dir：同一配置文件下 browser_path / template 不同也必须排队。"""
        active = 0
        peak = 0

        def fake_render_sync(markdown, report, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.05)
            active -= 1
            return render_mod.RenderOutcome(images=[b"png"], used_browser=True)

        with patch.object(render_mod, "render_sync", fake_render_sync):
            await asyncio.gather(
                render_mod.render_report(
                    "a", ReportConfig(render="image", browser_path="edge-a"),
                    meta={}, data_dir="dataA", template_root="tplA",
                ),
                render_mod.render_report(
                    "b", ReportConfig(render="image", browser_path="chrome-b"),
                    meta={}, data_dir="dataA", template_root="tplB",
                ),
            )
        self.assertEqual(peak, 1, "同一 data_dir 的渲染必须排队")

    async def test_different_data_dir_parallel(self):
        self.assertEqual(await self._render_twice(["dataA", "dataB"]), 2)

    async def test_text_mode_skips_queue(self):
        outcome = await render_mod.render_report(
            "body", ReportConfig(render="text"), meta={}, data_dir="dataA"
        )
        self.assertEqual(outcome.reason, "render=text")


class FollowupIsolationTests(unittest.TestCase):
    """追问策略按配置保存，一个配置改设置不影响另一个配置。"""

    def test_policy_is_per_config(self):
        store = FollowupStore()
        store.apply_policy("a", _settings(followup_max_turns=3))
        store.apply_policy("b", _settings(followup_max_turns=9))
        self.assertEqual(store.policy("a").max_turns, 3)
        self.assertEqual(store.policy("b").max_turns, 9)

        store.apply_policy("a", _settings(followup_max_turns=42))
        self.assertEqual(store.policy("b").max_turns, 9, "改 A 不得影响 B")

    def test_legacy_setters_only_touch_default(self):
        store = FollowupStore()
        store.apply_policy("a", _settings(followup_max_turns=3))
        store.set_window(120)
        store.set_max_turns(50)
        self.assertEqual(store.policy("a").max_turns, 3)
        self.assertEqual(store.policy("unknown").max_turns, 50)

    def test_window_follows_history_period(self):
        settings = _settings(followup_window_minutes=0)
        # 0 → 跟随历史保留期：24h × 2 周期 = 2880 分钟
        self.assertEqual(resolve_window_minutes(settings), 2880)

    def test_max_sessions_prunes_per_config(self):
        store = FollowupStore()
        store.apply_policy("a", _settings(followup_max_sessions=1))
        store.apply_policy("b", _settings(followup_max_sessions=5))
        for group in ("g1", "g2"):
            store.start(group_id=group, config_name="a", file_name="x.zip",
                        system_context="ctx", report="report")
        store.start(group_id="g3", config_name="b", file_name="y.zip",
                    system_context="ctx", report="report")
        store._prune()
        self.assertEqual(store.count_for("b"), 1, "B 的会话不该被 A 的容量上限挤掉")
        self.assertLessEqual(store.count_for("a"), 1)

    def test_close_has_single_working_definition(self):
        store = FollowupStore()
        self.assertFalse(store.close("missing-group"))
        store.start(group_id="g", config_name="a", file_name="x.zip",
                    system_context="ctx", report="report")
        self.assertTrue(store.close("g"))
        self.assertFalse(store.close("g"))


class ConfigSaveOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_save_offloads_and_coalesces(self):
        calls: list = []
        dm = SimpleNamespace(
            save_config=lambda name, state: calls.append(name)
        )
        service = MaaService(api=SimpleNamespace(), dm=dm)
        service.configs["a"] = SimpleNamespace(name="a")

        await service.save_config_async("a")
        self.assertEqual(calls, ["a"])

        calls.clear()
        for _ in range(3):
            service.schedule_save_config("a")
        for _ in range(50):
            if not service._save_tasks:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(calls, ["a"], "同一配置的连续写盘应合并成一次")


if __name__ == "__main__":
    unittest.main()