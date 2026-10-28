"""模型调用预算 — 裁剪收敛、图片计价、追问种子与工具读取上限。

Run with: python -m unittest discover -s tests -v.
"""
import asyncio
import os
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.models import AnalysisSettings, RepoConfig
from core.tokens import (
    IMAGE_PART_TOKENS,
    MIN_TOOL_RESULT_TOKENS,
    clamp_tool_result_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    fold_text,
    trim_to_tokens,
)
from features.maa.analyzer import MaaAnalyzer
from features.maa.archive_extract import (
    ArchiveProbe,
    cleanup_stale_dirs,
    probe_archive,
)
from features.maa.attachments import build_report_segments, text_segments
from features.maa.history import HistoryStore
from features.maa.log_digest import DigestResult
from features.maa.log_tools import LogArchiveTools
from features.maa.repo_tools import RepoTools


def _run_with_timeout(callable_, seconds=5):
    """在后台线程执行；超时即判定为不会收敛。"""
    box = []

    def target():
        try:
            box.append(("ok", callable_()))
        except Exception as exc:  # pragma: no cover - 仅用于失败诊断
            box.append(("error", exc))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if not box:
        raise AssertionError("裁剪未收敛（超时）")
    kind, value = box[0]
    if kind == "error":
        raise value
    return value


class TokenTrimTests(unittest.TestCase):
    SAMPLE = ("2026-10-28 05:00:00 ERROR failed to match node\n" * 200) + "尾部：节点 PVP 未识别\n"

    def test_tiny_budget_terminates(self):
        # 预算连说明后缀都放不下时返回空串；能放下则一定给出非空结果。
        # 关键是**不能卡住**——旧实现会在这里死循环。
        for budget in (1, 5):
            with self.subTest(budget=budget):
                self.assertEqual(
                    _run_with_timeout(lambda: trim_to_tokens(self.SAMPLE, budget)), ""
                )
                self.assertEqual(
                    _run_with_timeout(lambda: fold_text(self.SAMPLE, budget)), ""
                )
        for budget in (8, 12, 20):
            with self.subTest(budget=budget):
                self.assertLessEqual(
                    estimate_tokens(_run_with_timeout(
                        lambda: trim_to_tokens(self.SAMPLE, budget)
                    )),
                    budget,
                )
                self.assertLessEqual(
                    estimate_tokens(_run_with_timeout(
                        lambda: fold_text(self.SAMPLE, budget)
                    )),
                    budget,
                )

    def test_result_within_budget(self):
        for budget in (20, 64, 256, 512):
            with self.subTest(budget=budget):
                trimmed = trim_to_tokens(self.SAMPLE, budget)
                self.assertLessEqual(estimate_tokens(trimmed), budget)
                folded = fold_text(self.SAMPLE, budget)
                self.assertLessEqual(estimate_tokens(folded), budget)
                self.assertLess(len(folded), len(self.SAMPLE))
        # 预算足够时 fold 必须保留结尾（报错堆栈的关键信息通常在末尾）
        for budget in (256, 512):
            with self.subTest(budget=budget):
                self.assertIn("尾部：节点 PVP 未识别", fold_text(self.SAMPLE, budget))

    def test_short_text_untouched(self):
        self.assertEqual(trim_to_tokens("hi", 100), "hi")
        self.assertEqual(fold_text("hi", 100), "hi")
        self.assertEqual(trim_to_tokens("hi", 0), "")

    def test_clamp_tool_result_tokens(self):
        self.assertEqual(clamp_tool_result_tokens(0), 0)
        self.assertEqual(clamp_tool_result_tokens(-5), 0)
        self.assertEqual(clamp_tool_result_tokens(5), MIN_TOOL_RESULT_TOKENS)
        self.assertEqual(clamp_tool_result_tokens(4096), 4096)

    def test_image_parts_use_fixed_estimate(self):
        # 一张 800 KB 截图的 base64 约 1.07M 字符；按 4 字符/token 估会得到 26 万 token
        payload = "data:image/png;base64," + "A" * 1_070_000
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": payload, "detail": "high"}},
            ],
        }]
        tokens = estimate_messages_tokens(messages)
        self.assertLess(tokens, IMAGE_PART_TOKENS + 100)
        self.assertGreaterEqual(tokens, IMAGE_PART_TOKENS)


class AttachmentBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maa_attach_"))
        self.zip_path = self.tmp / "logs.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("on_error/a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            archive.writestr("on_error/b.png", b"\x89PNG\r\n\x1a\n" + b"1" * 64)
            archive.writestr("config/app.json", b'{"a": 1}')
            archive.writestr("config/empty.json", b"")
        self.digest = DigestResult(
            prompt="x", file_count=4, total_uncompressed_size=200,
            all_images=["on_error/a.png", "on_error/b.png"],
            all_members=[
                "on_error/a.png", "on_error/b.png",
                "config/app.json", "config/empty.json",
            ],
            zip_path=str(self.zip_path),
        )
        self.report = (
            "结论：阈值偏低。\n"
            "[附件@日志: on_error/a.png]\n"
            "[附件@日志: on_error/b.png]\n"
            "[附件@日志: config/app.json]\n"
            "[附件@日志: config/empty.json]\n完。\n"
        )

    def test_images_and_files_counted_separately(self):
        segments, notes = build_report_segments(
            self.report, self.digest, max_images=1, max_files=2
        )
        kinds = [seg.kind for seg in segments]
        self.assertEqual(kinds.count("image"), 1)
        self.assertEqual(kinds.count("file"), 1)
        self.assertEqual(notes, ["已达图片数上限 1，其余未发送"])

    def test_disabled_kinds_emit_text_only(self):
        segments, notes = build_report_segments(
            self.report, self.digest, max_images=0, max_files=0
        )
        self.assertTrue(all(seg.kind == "text" for seg in segments))
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(seg.data == b"" for seg in segments))

    def test_empty_member_never_becomes_attachment(self):
        segments, _ = build_report_segments(
            self.report, self.digest, max_images=4, max_files=4
        )
        labels = [seg.label for seg in segments if seg.kind != "text"]
        self.assertNotIn("config/empty.json", labels)
        self.assertEqual(len(labels), 3)

    def test_text_segments_helper_drops_attachments(self):
        self.assertTrue(all(seg.kind == "text" for seg in text_segments(self.report)))


class FollowupBudgetTests(unittest.TestCase):
    def test_context_trimmed_to_budget_and_keeps_tail(self):
        summary = "【日志摘要】\n" + ("ERROR 节点未识别 行号 123\n" * 3000)
        repo = "【代码参考】\n" + ("PVP.json:12 threshold 0.9\n" * 500)
        context = MaaAnalyzer._build_followup_context(
            "logs.zip", summary, repo, budget=4000
        )
        self.assertLessEqual(estimate_tokens(context), 4000)
        self.assertTrue(context.startswith("【日志摘要】"))
        self.assertIn("本次分析信息", context)
        self.assertIn("已按追问预算裁剪", context)

    def test_context_untouched_when_within_budget(self):
        context = MaaAnalyzer._build_followup_context(
            "logs.zip", "短摘要", "", budget=4000
        )
        self.assertNotIn("裁剪", context)
        self.assertIn("短摘要", context)

    def test_seed_budget_prefers_summary(self):
        summary = "【日志摘要】\n" + ("ERROR 行\n" * 4000)
        report = "【分析报告】\n" + ("结论：阈值偏低\n" * 800)
        messages = [
            {"role": "system", "content": "系统提示词" * 50},
            {"role": "user", "content": summary},
            {"role": "assistant", "content": report},
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        budget = 3000
        out = MaaAnalyzer._budget_followup_seed(messages, budget)
        total = sum(estimate_tokens(str(m.get("content") or "")) for m in out)
        self.assertLessEqual(total, budget)
        self.assertIn("追问摘要已按预算裁剪", out[1]["content"])
        self.assertIn("上次报告已按追问预算裁剪", out[2]["content"])
        self.assertEqual(out[3]["content"], "之前的问题")
        self.assertEqual(out[4]["content"], "之前的回答")

    def test_seed_budget_is_noop_when_roomy(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "摘要"},
            {"role": "assistant", "content": "报告"},
        ]
        self.assertEqual(MaaAnalyzer._budget_followup_seed(messages, 999999), messages)


class ToolReadCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maa_cap_"))
        self.zip_path = self.tmp / "logs.zip"
        line = "2026-10-28 05:00:00 ERROR node not found 节点未识别\n"
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("logs/huge.log", line * 40000)          # ~2 MB
            archive.writestr("logs/oneline.log", "E" * 2_000_000)    # 没有换行
            archive.writestr("logs/small.log", "first\nsecond ERROR line\n")
            archive.writestr("logs/empty.log", "")
        self.repo_root = self.tmp / "repo"
        self.repo_root.mkdir()
        (self.repo_root / "big.txt").write_text(
            "line ERROR 节点未识别\n" * 40000, encoding="utf-8"
        )
        (self.repo_root / "oneline.txt").write_text("E" * 200_000, encoding="utf-8")

    def _log_tools(self, **kwargs):
        return LogArchiveTools(str(self.zip_path), max_file_kb=64, **kwargs)

    def test_log_read_is_byte_capped(self):
        # 结果字符预算放大到 200k，让 64 KB 的单文件读取上限先触发
        tools = self._log_tools(max_result_chars=200000)
        try:
            out = asyncio.run(tools.read_file("logs/huge.log"))
            self.assertIn("仅前 64 KB", out)
            self.assertIn("仅读取前 64 KB", out)
            self.assertLess(len(out), 120000)
        finally:
            tools.close()

    def test_log_read_bounds_newline_free_line(self):
        """没有换行的巨型成员不能整行读进内存/结果。"""
        tools = self._log_tools()
        try:
            out = asyncio.run(tools.read_file("logs/oneline.log"))
            self.assertIn("行已截断到", out)
            self.assertLess(len(out), 4000)     # 单行上限 + 头尾说明
        finally:
            tools.close()

    def test_log_read_start_line_out_of_range(self):
        tools = self._log_tools()
        try:
            out = asyncio.run(tools.read_file("logs/small.log",
                                             start_line=99))
            self.assertIn("超出范围", out)
            self.assertIn("2 行", out)
        finally:
            tools.close()

    def test_log_read_end_line_offers_continuation(self):
        tools = self._log_tools()
        try:
            out = asyncio.run(tools.read_file("logs/huge.log",
                                             start_line=1, end_line=2))
            self.assertIn("已到 end_line", out)
            self.assertIn("start_line=3", out)
        finally:
            tools.close()

    def test_log_read_handles_empty_member(self):
        tools = self._log_tools()
        try:
            out = asyncio.run(tools.read_file("logs/empty.log"))
            self.assertIn("没有可读取的文本内容", out)
        finally:
            tools.close()

    def test_repo_read_is_byte_capped(self):
        cfg = RepoConfig(max_file_kb=64, max_tool_result_chars=300000,
                         max_tool_result_tokens=100000)
        provider = SimpleNamespace(root=self.repo_root, current_ref="（默认分支）", cfg=cfg)
        tools = RepoTools(provider, cfg)
        out = asyncio.run(tools.execute("read_file", {"path": "big.txt"}))
        self.assertIn("仅前 64 KB", out)
        self.assertLess(len(out), 300000)

    def test_repo_read_truncates_single_line(self):
        cfg = RepoConfig(max_file_kb=256, max_tool_result_chars=300000,
                         max_tool_result_tokens=100000)
        provider = SimpleNamespace(root=self.repo_root, current_ref="（默认分支）", cfg=cfg)
        tools = RepoTools(provider, cfg)
        out = asyncio.run(tools.execute("read_file", {"path": "oneline.txt"}))
        self.assertIn("行已截断到", out)
        self.assertLess(len(out), 4000)

    def test_repo_read_start_line_out_of_range(self):
        cfg = RepoConfig(max_file_kb=256)
        provider = SimpleNamespace(root=self.repo_root, current_ref="（默认分支）", cfg=cfg)
        tools = RepoTools(provider, cfg)
        out = asyncio.run(tools.execute(
            "read_file", {"path": "oneline.txt", "start_line": 5}
        ))
        self.assertIn("超出范围", out)
        self.assertIn("1 行", out)


class ArchiveExtractionTests(unittest.TestCase):
    """解压到临时目录的安全边界（对齐 dsh 的“工具读真实文件”）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maa_extract_"))
        self.zip_path = self.tmp / "logs.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("logs/ok.log", "hello\nworld\n")
            archive.writestr("logs/sub/deep.log", "deep\n")
            archive.writestr("logs/.env", "SECRET=1\n")
            archive.writestr("../escape.txt", "escaped\n")
            archive.writestr("logs/", "")
            link = zipfile.ZipInfo("logs/link.log")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../../etc/passwd")

    def test_extracts_only_safe_members(self):
        tools = LogArchiveTools(str(self.zip_path))
        try:
            root_listing = asyncio.run(tools.list_files())
            self.assertIn("logs/", root_listing)
            listing = asyncio.run(tools.list_files("logs"))
            self.assertIn("ok.log", listing)
            self.assertIn("sub/", listing)
            self.assertNotIn(".env", listing)
            self.assertNotIn("link.log", listing)
            self.assertNotIn("escape", listing)
            out = asyncio.run(tools.read_file("logs/ok.log"))
            self.assertIn("[行 1] hello", out)
            self.assertIn("[行 2] world", out)
        finally:
            tools.close()

    def test_rejects_traversal_escape_and_sensitive_reads(self):
        tools = LogArchiveTools(str(self.zip_path))
        try:
            self.assertIn("路径非法", asyncio.run(tools.read_file("../escape.txt")))
            self.assertIn("拒绝", asyncio.run(tools.read_file("logs/.env")))
        finally:
            tools.close()

    def test_available_does_not_extract(self):
        tools = LogArchiveTools(str(self.zip_path))
        try:
            self.assertTrue(tools.available())
            self.assertIsNone(tools._extract.root)   # 未触发解压
            asyncio.run(tools.list_files())
            self.assertIsNotNone(tools._extract.root)  # 首次工具调用才解压
        finally:
            tools.close()

    def test_temp_dir_removed_on_close_and_reused_between_calls(self):
        tools = LogArchiveTools(str(self.zip_path))
        asyncio.run(tools.list_files())
        root = tools._extract.root
        self.assertIsNotNone(root)
        self.assertTrue(Path(root).is_dir())
        asyncio.run(tools.list_files())            # 复用同一目录
        self.assertEqual(tools._extract.root, root)
        tools.close()
        # close 只释放内存状态：临时目录由 discard 负责删除
        self.assertTrue(Path(root).is_dir())
        tools.discard()
        self.assertFalse(Path(root).exists())

    def test_extract_dir_is_reused_without_unzipping(self):
        """指定解压目录后：第二次调用直接复用，不再解压。"""
        target = self.tmp / "persist"
        first = LogArchiveTools(str(self.zip_path), extract_dir=str(target))
        asyncio.run(first.list_files())
        self.assertFalse(first._extract.reused)
        marker = target / ".maa-extracted.json"
        self.assertTrue(marker.is_file())
        mtime = marker.stat().st_mtime_ns
        first.close()                                   # 只释放，不删除

        second = LogArchiveTools(str(self.zip_path), extract_dir=str(target))
        try:
            listing = asyncio.run(second.list_files("logs"))
            self.assertTrue(second._extract.reused)
            self.assertIn("ok.log", listing)
            self.assertEqual(marker.stat().st_mtime_ns, mtime)   # 未重新解压
        finally:
            second.close()
        self.assertTrue(target.is_dir())

    def test_stale_marker_triggers_reextract(self):
        target = self.tmp / "changed"
        first = LogArchiveTools(str(self.zip_path), extract_dir=str(target))
        asyncio.run(first.list_files())
        first.close()
        # 改动压缩包 → 指纹不匹配 → 重新解压并覆盖
        with zipfile.ZipFile(self.zip_path, "a") as archive:
            archive.writestr("logs/extra.log", "extra\n")
        second = LogArchiveTools(str(self.zip_path), extract_dir=str(target))
        try:
            asyncio.run(second.list_files())
            self.assertFalse(second._extract.reused)
            self.assertIn("logs/extra.log", second._extract.members)
        finally:
            second.close()

    def test_stale_staging_cleanup(self):
        staging = self.tmp / "history" / ".staging"
        old = staging / "old"
        fresh = staging / "fresh"
        for path in (old, fresh):
            path.mkdir(parents=True)
            (path / "a.log").write_text("x", encoding="utf-8")
        past = time.time() - 7 * 3600
        os.utime(old, (past, past))
        removed = cleanup_stale_dirs(staging, max_age_hours=6)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_total_cap_aborts_extraction(self):
        """解压后总大小超限：一个字节都不落盘，工具直接拒绝服务。"""
        big = self.tmp / "big.zip"
        with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("a.log", "x" * (2 * 1024 * 1024))
            archive.writestr("b.log", "y" * (2 * 1024 * 1024))
        tools = LogArchiveTools(str(big), max_total_mb=1)
        try:
            out = asyncio.run(tools.list_files())
            self.assertIn("内容过多", out)
            self.assertTrue(tools._extract.overflow)
            self.assertIsNone(tools._extract.root)      # 未创建临时目录
            self.assertEqual(tools._extract.members, [])
        finally:
            tools.close()

    def test_runtime_guard_catches_lying_headers(self):
        """zip 头谎报大小时，解压途中按实际字节复核并整次中止。"""
        liar = self.tmp / "liar.zip"
        with zipfile.ZipFile(liar, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("a.log", "x" * (3 * 1024 * 1024))
        # 假装 probe 通过（模拟头声明 1 字节、实际 3 MB）
        with mock.patch(
            "features.maa.archive_extract.probe_archive",
            return_value=ArchiveProbe(ok=True, total_bytes=1, member_count=1),
        ):
            tools = LogArchiveTools(str(liar), max_total_mb=1)
            try:
                out = asyncio.run(tools.list_files())
                self.assertIn("内容过多", out)
                self.assertTrue(tools._extract.overflow)
                self.assertIsNone(tools._extract.root)   # 已清理，不留半成品
            finally:
                tools.close()

    def test_missing_zip_reports_unavailable(self):
        tools = LogArchiveTools(str(self.tmp / "nope.zip"))
        try:
            self.assertFalse(tools.available())
            self.assertIn("不可用", asyncio.run(tools.read_file("a.log")))
        finally:
            tools.close()


class ArchiveLimitGateTests(unittest.TestCase):
    """解压后总大小闸门：下载后立即判定，超限不做任何后续分析。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maa_gate_"))
        self.small = self.tmp / "small.zip"
        with zipfile.ZipFile(self.small, "w") as archive:
            archive.writestr("logs/a.log", "x" * 1024)
        self.big = self.tmp / "big.zip"
        with zipfile.ZipFile(self.big, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("logs/huge.log", "x" * (12 * 1024 * 1024))

    def test_probe_accepts_within_limit(self):
        probe = probe_archive(str(self.small), 1)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.member_count, 1)
        self.assertGreater(probe.total_bytes, 0)
        self.assertEqual(probe.reason, "")

    def test_probe_rejects_over_limit(self):
        probe = probe_archive(str(self.big), 1)
        self.assertFalse(probe.ok)
        self.assertIn("内容过多", probe.reason)
        self.assertIn("max_total_uncompressed_mb", probe.reason)
        self.assertGreaterEqual(probe.total_bytes, 12 * 1024 * 1024)

    def test_probe_handles_missing_and_broken_files(self):
        self.assertIn("不存在", probe_archive(str(self.tmp / "nope.zip"), 1).reason)
        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"not a zip")
        self.assertIn("无法读取", probe_archive(str(broken), 1).reason)

    def test_analyzer_gate_returns_user_message(self):
        settings = AnalysisSettings(max_total_uncompressed_mb=1)
        self.assertEqual(
            MaaAnalyzer.archive_limit_error(settings, str(self.small)), ""
        )
        reason = MaaAnalyzer.archive_limit_error(settings, str(self.big))
        self.assertIn("内容过多", reason)
        self.assertIn("已跳过分析", reason)

    def test_gate_respects_configured_limit(self):
        settings = AnalysisSettings(max_total_uncompressed_mb=64)
        self.assertEqual(
            MaaAnalyzer.archive_limit_error(settings, str(self.big)), ""
        )

    def test_default_limit_is_one_gigabyte(self):
        self.assertEqual(AnalysisSettings().max_total_uncompressed_mb, 1024)


class ArchivePersistenceTests(unittest.TestCase):
    """解压结果跟着历史记录走：归档后可直接复用，记录清理时一并删除。"""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="maa_hist_"))
        self.zip_path = self.data / "logs.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("logs/a.log", "line1\nline2\n")
        self.store = HistoryStore(self.data)

    def test_add_moves_extraction_into_record(self):
        staging = self.store.staging_dir("cfg", "sess-1")
        tools = LogArchiveTools(str(self.zip_path), extract_dir=str(staging))
        asyncio.run(tools.list_files())
        tools.close()

        record = self.store.add(
            config_name="cfg", group_id="1", file_name="logs.zip",
            source_zip=self.zip_path, context="ctx", report="report",
            extract_dir=staging,
        )
        self.assertIsNotNone(record)
        extracted = self.store.extracted_dir("cfg", record.id)
        self.assertTrue((extracted / "logs" / "a.log").is_file())
        self.assertFalse(staging.exists())            # 暂存区已整体移走
        self.assertGreaterEqual(record.size_bytes, (extracted / "logs" / "a.log").stat().st_size)

        # 追问时按记录 ID 推导出同一目录，直接复用
        archived_zip = self.store.read_zip("cfg", record)
        self.assertIsNotNone(archived_zip)
        tools2 = LogArchiveTools(
            str(archived_zip), extract_dir=str(extracted)
        )
        try:
            out = asyncio.run(tools2.read_file("logs/a.log"))
            self.assertIn("[行 2] line2", out)
            self.assertTrue(tools2._extract.reused)
        finally:
            tools2.close()

    def test_record_cleanup_removes_extraction(self):
        staging = self.store.staging_dir("cfg", "sess-2")
        tools = LogArchiveTools(str(self.zip_path), extract_dir=str(staging))
        asyncio.run(tools.list_files())
        tools.close()
        record = self.store.add(
            config_name="cfg", group_id="1", file_name="logs.zip",
            source_zip=self.zip_path, context="", report="",
            extract_dir=staging,
        )
        extracted = self.store.extracted_dir("cfg", record.id)
        self.assertTrue(extracted.is_dir())
        result = self.store.cleanup("cfg", period_hours=1, keep_periods=0)
        self.assertEqual(result.removed, 1)
        self.assertFalse(extracted.exists())

    def test_missing_extract_dir_is_ignored(self):
        record = self.store.add(
            config_name="cfg", group_id="1", file_name="logs.zip",
            source_zip=self.zip_path, context="", report="",
            extract_dir=self.data / "not-there",
        )
        self.assertIsNotNone(record)
        self.assertFalse(
            self.store.extracted_dir("cfg", record.id).exists()
        )

    def test_release_drops_temp_dir_but_keeps_persistent_one(self):
        temp_tools = LogArchiveTools(str(self.zip_path))
        asyncio.run(temp_tools.list_files())
        temp_root = Path(temp_tools._extract.root)
        self.assertTrue(temp_root.is_dir())
        temp_tools.release()
        self.assertFalse(temp_root.exists())          # 临时目录用完即删

        target = self.store.staging_dir("cfg", "sess-3")
        kept = LogArchiveTools(str(self.zip_path), extract_dir=str(target))
        asyncio.run(kept.list_files())
        kept.release()
        self.assertTrue((target / "logs" / "a.log").is_file())   # 指定目录保留

    def test_cleanup_staging_scans_configs(self):
        stale = self.store.staging_dir("cfg", "dead-session")
        stale.mkdir(parents=True)
        (stale / "a.log").write_text("x", encoding="utf-8")
        past = time.time() - 7 * 3600
        os.utime(stale, (past, past))
        fresh = self.store.staging_dir("cfg", "live-session")
        fresh.mkdir(parents=True)

        self.assertEqual(self.store.cleanup_staging(max_age_hours=6), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())               # 未超龄的不动


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
