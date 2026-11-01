"""
仓库获取路径测试 —— 分析只浅克隆、完整克隆只属于后台预下载、时长字段生效。

Run with: python -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from core.models import RepoConfig
from features.maa.analyzer import MaaAnalyzer
from features.maa.repo import RepoProvider


@contextlib.contextmanager
def _tmpdir():
    """测试用临时目录。

    放在仓库根目录下（沙箱里只有仓库根可写；`data/`、`tests/`、系统 temp
    都可能被 Windows 权限拒绝）。测试结束即删，删除失败忽略。
    """
    base = Path("_tmp_repo_acquisition")
    base.mkdir(parents=True, exist_ok=True)
    # 不用 tempfile.mkdtemp：它在 Windows 上建成 0700，沙箱下无法再建子目录
    path = base / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            base.rmdir()          # 空目录顺手收掉，保持工作区干净
        except OSError:
            pass


def _repo_cfg(**overrides) -> RepoConfig:
    base = dict(
        enabled=True,
        url="https://example.invalid/MaaXXX.git",
        preload_enabled=True,
    )
    base.update(overrides)
    return RepoConfig(**base)


def _make_repo(root: Path, *, shallow: bool) -> None:
    """只造出 .git 与 .git/shallow —— 形态判定只看这两个标记，不跑真 git。"""
    git_dir = Path(root) / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    marker = git_dir / "shallow"
    if shallow:
        marker.write_text("0000\n", encoding="utf-8")
    elif marker.exists():
        marker.unlink()


class TimeoutFieldTests(unittest.TestCase):
    def test_new_fields_have_defaults(self):
        cfg = RepoConfig()
        self.assertEqual(cfg.shallow_clone_timeout_seconds, 600)
        self.assertEqual(cfg.preload_timeout_seconds, 1800)
        self.assertEqual(cfg.update_timeout_seconds, 600)


class CanonicalShapeTests(unittest.TestCase):
    """canonical 能不能直接用于分析。"""

    def _provider(self, tmp: str, **overrides):
        cfg = _repo_cfg(**overrides)
        return RepoProvider(cfg, tmp, config_name="c")

    def test_full_clone_can_be_used_directly(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp)
            _make_repo(Path(provider.root), shallow=False)
            self.assertTrue(MaaAnalyzer._canonical_ready(provider))

    def test_shallow_clone_needs_upgrade_when_preloading(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp)
            _make_repo(Path(provider.root), shallow=True)
            self.assertFalse(MaaAnalyzer._canonical_ready(provider))

    def test_shallow_clone_is_final_without_preload(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp, preload_enabled=False)
            _make_repo(Path(provider.root), shallow=True)
            self.assertTrue(MaaAnalyzer._canonical_ready(provider))

    def test_missing_repo_is_not_ready(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp)
            self.assertFalse(MaaAnalyzer._canonical_ready(provider))

    def test_local_path_is_ready(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp, path=str(tmp), url="")
            self.assertTrue(MaaAnalyzer._canonical_ready(provider))


class CloneShapeTests(unittest.IsolatedAsyncioTestCase):
    """ensure_ready 必须把克隆形态与各步超时显式传下去。"""

    def _provider(self, tmp: str, **overrides):
        cfg = _repo_cfg(**overrides)
        return RepoProvider(cfg, tmp, config_name="c")

    async def _clone_calls(self, provider, **ready_kwargs):
        calls = []

        async def fake_clone(root, *, shallow=False, clone_timeout=0,
                             probe_timeout=0, submodule_timeout=0):
            calls.append((shallow, clone_timeout, probe_timeout, submodule_timeout))
            _make_repo(Path(root), shallow=shallow)

        provider._clone = fake_clone
        ok = await provider.ensure_ready(**ready_kwargs)
        return ok, calls

    async def test_analysis_path_is_always_shallow(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp)      # preload_enabled=True
            ok, calls = await self._clone_calls(provider, shallow_only=True)
            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            shallow, timeout, probe, submodule = calls[0]
            self.assertTrue(shallow, "分析现场只允许浅克隆")
            self.assertEqual(timeout, 600)      # shallow_clone_timeout_seconds
            self.assertEqual(probe, 60)
            self.assertEqual(submodule, 1200)

    async def test_analysis_budget_uses_configured_field(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp, shallow_clone_timeout_seconds=90)
            _ok, calls = await self._clone_calls(provider, shallow_only=True)
            self.assertEqual(calls[0][1], 90)

    async def test_budget_clamps_every_step(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp)
            _ok, calls = await self._clone_calls(
                provider, shallow_only=True, budget=123
            )
            # 克隆与子模块被预算压到 123；探测本来 60 已经更小
            self.assertEqual(calls[0], (True, 123, 60, 123))

    async def test_preload_upgrades_shallow_to_full(self):
        with _tmpdir() as tmp:
            provider = self._provider(tmp, preload_timeout_seconds=444)
            _make_repo(Path(provider.root), shallow=True)

            async def always_shallow(root=None):
                return True

            provider.is_shallow = always_shallow
            ok, calls = await self._clone_calls(provider)   # 后台预下载路径
            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            shallow, timeout, _probe, _submodule = calls[0]
            self.assertFalse(shallow, "预下载要完整克隆")
            self.assertEqual(timeout, 444)


class AnalyzerRepoChoiceTests(unittest.IsolatedAsyncioTestCase):
    """分析/追问选仓库：canonical 完整就直用，否则临时浅克隆 + 后台升级。"""

    def _analyzer(self, tmp: str) -> MaaAnalyzer:
        service = SimpleNamespace(dm=SimpleNamespace(_dir=Path(tmp)), configs={})
        return MaaAnalyzer(service, llm=SimpleNamespace(), history=None)

    async def test_full_canonical_is_used_directly(self):
        with _tmpdir() as tmp:
            analyzer = self._analyzer(tmp)
            cfg = _repo_cfg()
            canonical = analyzer._repo_provider("c", cfg)
            _make_repo(Path(canonical.root), shallow=False)
            self.assertIs(analyzer._provider_for_analysis("c", cfg), canonical)
            self.assertEqual(analyzer._analysis_repos, {})

    async def test_shallow_canonical_uses_temp_and_starts_background_clone(self):
        with _tmpdir() as tmp:
            analyzer = self._analyzer(tmp)
            cfg = _repo_cfg()
            canonical = analyzer._repo_provider("c", cfg)
            _make_repo(Path(canonical.root), shallow=True)

            started: list[str] = []

            async def fake_preload(config_name, repo_cfg):
                started.append(config_name)
                await asyncio.sleep(30)

            analyzer._preload_repo_task = fake_preload
            chosen = analyzer._provider_for_analysis("c", cfg)
            self.assertIsNot(chosen, canonical)
            self.assertIn(".temporary", str(chosen.root))
            task = analyzer._repo_preload_tasks.get("c")
            self.assertIsNotNone(task)
            await asyncio.sleep(0)
            self.assertEqual(started, ["c"])

            # 单克隆互斥：再选一次不会起第二个任务
            analyzer._provider_for_analysis("c", cfg)
            running = [t for t in analyzer._repo_preload_tasks.values() if not t.done()]
            self.assertEqual(len(running), 1)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_preload_failure_cooldown_avoids_repeat(self):
        with _tmpdir() as tmp:
            analyzer = self._analyzer(tmp)
            cfg = _repo_cfg()
            canonical = analyzer._repo_provider("c", cfg)
            _make_repo(Path(canonical.root), shallow=True)

            started: list[str] = []

            async def fake_preload(config_name, repo_cfg):
                started.append(config_name)

            analyzer._preload_repo_task = fake_preload
            analyzer._provider_for_analysis("c", cfg)
            await analyzer._repo_preload_tasks["c"]     # 任务立刻结束
            analyzer._provider_for_analysis("c", cfg)   # 冷却期内不再起
            self.assertEqual(started, ["c"])

    async def test_preload_task_runs_with_new_signature_on_local_path(self):
        """_preload_repo(配置, repo_cfg) 的签名与收尾在本地 path 下不炸（不联网）。"""
        with _tmpdir() as tmp:
            analyzer = self._analyzer(tmp)
            repo_dir = Path(tmp) / "localrepo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            cfg = _repo_cfg(path=str(repo_dir), url="", preload_retries=1)
            analyzer._repo_preload_status["c"] = {
                "state": "等待预下载", "attempt": 0, "detail": "",
            }
            await analyzer._preload_repo("c", cfg)
            # path 模式：仓库就绪，但「更新默认分支」不适用 → 预下载记失败
            self.assertEqual(
                analyzer._repo_preload_status["c"]["state"], "预下载失败"
            )

    async def test_settle_removes_temp_once_canonical_ready(self):
        with _tmpdir() as tmp:
            analyzer = self._analyzer(tmp)
            cfg = _repo_cfg()
            canonical = analyzer._repo_provider("c", cfg)
            _make_repo(Path(canonical.root), shallow=True)
            temp = analyzer._analysis_repo("c", cfg, canonical)
            temp_root = Path(temp.root)
            temp_root.mkdir(parents=True, exist_ok=True)
            (temp_root / "keep.txt").write_text("x", encoding="utf-8")

            analyzer._settle_analysis_repo_now("c")     # canonical 未就绪 → 保留
            self.assertTrue(temp_root.exists())

            _make_repo(Path(canonical.root), shallow=False)
            analyzer._settle_analysis_repo_now("c")
            self.assertFalse(temp_root.exists())
            self.assertEqual(analyzer._analysis_repos, {})


if __name__ == "__main__":
    unittest.main()