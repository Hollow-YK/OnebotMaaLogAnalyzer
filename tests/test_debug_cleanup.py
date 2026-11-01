"""
调试/回归运行的收尾保证 —— 跑完不留进程、不留文件（N1）。

Run with: python -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from debug import DebugManager
from debug import runner as runner_mod


@contextlib.contextmanager
def _tmpdir():
    """仓库根下的临时目录。

    沙箱里只有仓库根可写（`data/`、`tests/`、系统 temp 都可能被 Windows 权限
    拒绝），所以不用 `tempfile`。删除失败忽略。
    """
    base = Path("_tmp_debug_cleanup")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()          # 空目录顺手收掉
        except OSError:
            pass


def _manager(data_dir) -> DebugManager:
    """收尾测试用的最小实例（close/clean_scratch 只用到 data_dir）。"""
    manager = DebugManager.__new__(DebugManager)
    manager.data_dir = str(data_dir)
    return manager


class DebugManagerCloseTests(unittest.TestCase):
    def test_close_stops_browsers_and_removes_run_scratch(self):
        with _tmpdir() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            profile = data / "_browser_profile"
            profile.mkdir()
            (profile / "lockfile").write_text("x", encoding="utf-8")
            (data / "_browser_stderr.log").write_text("x", encoding="utf-8")
            outbox = data / "_outbox"
            outbox.mkdir()
            (outbox / "sent.bin").write_bytes(b"x")
            repo = data / "repo"
            repo.mkdir()
            for name in (".temporary", ".clone-c-abc", ".stale-c-abc"):
                (repo / name).mkdir()
            keep_repo = repo / "c"
            keep_repo.mkdir()
            (keep_repo / "README.md").write_text("keep", encoding="utf-8")
            history = data / "history"
            history.mkdir()
            (history / "records.json").write_text("[]", encoding="utf-8")

            closed: list = []
            with patch("features.maa.render.close_all", lambda: closed.append(True)):
                _manager(data).close()

            self.assertEqual(
                closed, [True], "必须关闭常驻浏览器（否则残留进程占住 profile）"
            )
            self.assertFalse(profile.exists())
            self.assertFalse((data / "_browser_stderr.log").exists())
            self.assertFalse(outbox.exists())
            self.assertFalse((repo / ".temporary").exists())
            self.assertFalse((repo / ".clone-c-abc").exists())
            self.assertFalse((repo / ".stale-c-abc").exists())
            # 数据本体不动：仓库缓存与历史仍保留
            self.assertTrue((keep_repo / "README.md").exists())
            self.assertTrue((history / "records.json").exists())

    def test_clean_scratch_can_be_skipped(self):
        with _tmpdir() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            profile = data / "_browser_profile"
            profile.mkdir()
            closed: list = []
            with patch("features.maa.render.close_all", lambda: closed.append(True)):
                _manager(data).close(clean_scratch=False)
            self.assertEqual(closed, [True])
            self.assertTrue(profile.exists(), "关浏览器与清文件是两件事")

    def test_missing_data_dir_is_noop(self):
        with _tmpdir() as tmp:
            with patch("features.maa.render.close_all", lambda: None):
                _manager(Path(tmp) / "missing").close()   # 不该抛异常


class RunTestFileCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_test_file_always_closes_manager(self):
        closed: list = []
        manager = _manager(".")
        manager.close = lambda **kw: closed.append(kw)

        async def fake_inner(file_path, mgr):
            return "REPORT"

        with patch.object(runner_mod, "_run_test_file_inner", fake_inner):
            out = await runner_mod.run_test_file(
                "debug/examples/smoke_test.json", manager
            )
        self.assertEqual(out, "REPORT")
        self.assertEqual(len(closed), 1, "跑完必须收尾")

    async def test_run_test_file_closes_manager_even_on_error(self):
        closed: list = []
        manager = _manager(".")
        manager.close = lambda **kw: closed.append(kw)

        async def boom(file_path, mgr):
            raise RuntimeError("场景炸了")

        with patch.object(runner_mod, "_run_test_file_inner", boom):
            with self.assertRaises(RuntimeError):
                await runner_mod.run_test_file(
                    "debug/examples/smoke_test.json", manager
                )
        self.assertEqual(len(closed), 1, "异常路径也必须收尾")


if __name__ == "__main__":
    unittest.main()