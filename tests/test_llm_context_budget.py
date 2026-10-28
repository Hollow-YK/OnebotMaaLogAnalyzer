"""模型调用预算 — 裁剪收敛、图片计价、追问种子与工具读取上限。

Run with: python -m unittest discover -s tests -v.
"""
import asyncio
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from core.models import RepoConfig
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
from features.maa.attachments import build_report_segments, text_segments
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

    def _log_tools(self):
        return LogArchiveTools(str(self.zip_path), max_file_kb=64)

    def test_log_read_is_byte_capped(self):
        # 结果字符预算放大到 200k，让 64 KB 的单文件读取上限先触发
        tools = LogArchiveTools(str(self.zip_path), max_file_kb=64,
                                max_result_chars=200000)
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


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
