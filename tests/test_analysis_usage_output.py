"""主分析显示本次 token 用量与缓存命中率，追问不追加用量行。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from maa_analyzer.core.models import AnalysisSettings, ReportConfig
from maa_analyzer.features.maa.analyzer import MaaAnalyzer
from maa_analyzer.features.maa.attachments import OutSegment
from maa_analyzer.features.maa.render import RenderOutcome
from maa_analyzer.features.maa.render.template import load_template


class AnalysisUsageOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.analyzer = MaaAnalyzer.__new__(MaaAnalyzer)
        self.analyzer.s = SimpleNamespace(dm=SimpleNamespace(_dir="data"))
        self.analyzer._template_root_path = None
        self.analyzer.llm = SimpleNamespace(session_summary=Mock(return_value={
            "calls": 3, "prompt_tokens": 12000, "completion_tokens": 345,
            "total_tokens": 12345, "cache_read_tokens": 9600,
            "cache_write_tokens": 0, "cache_hit_rate": 0.8,
        }))

    async def prepare(self, mode="text", *, analysis=True, images=None):
        self.analyzer._report_cfg = Mock(return_value=ReportConfig(render=mode))
        notes = ["已有备注"]
        with patch("maa_analyzer.features.maa.analyzer.render_report", new_callable=AsyncMock,
                   return_value=RenderOutcome(images=images or [])) as render:
            result = await self.analyzer._prepare_output(
                config_name="main", settings=AnalysisSettings(), raw_report="分析正文",
                information={"project": "MAN", "file": "demo.zip"}, archive=None,
                segments=[OutSegment(kind="text", text="分析正文")], notes=notes,
                render_followup=True, session_id="analysis-session",
                show_analysis_usage=analysis,
            )
        self.assertEqual(notes, ["已有备注"])
        return result, render

    async def test_text_includes_exact_total_in_segments_and_report(self):
        (segments, text, rendered, _), _ = await self.prepare()
        self.assertIn("本次分析 Token 用量：输入 12,000 · 输出 345 · 合计 12,345 tokens · 缓存命中率：80.0%", text)
        self.assertIn("本次分析 Token 用量：输入 12,000 · 输出 345 · 合计 12,345 tokens · 缓存命中率：80.0%", segments[-1].text)
        self.assertFalse(rendered)
        self.analyzer.llm.session_summary.assert_called_with("analysis-session")

    async def test_image_and_fallback_include_total(self):
        for images in ([b"png"], []):
            with self.subTest(images=bool(images)):
                (segments, text, rendered, _), render = await self.prepare("image", images=images)
                self.assertIn("本次分析 Token 用量：输入 12,000 · 输出 345 · 合计 12,345 tokens · 缓存命中率：80.0%", render.call_args.args[0])
                self.assertIn("本次分析 Token 用量：输入 12,000 · 输出 345 · 合计 12,345 tokens · 缓存命中率：80.0%", text)
                self.assertEqual(rendered, bool(images))
                if not rendered:
                    self.assertIn("本次分析 Token 用量：输入 12,000 · 输出 345 · 合计 12,345 tokens · 缓存命中率：80.0%", segments[-1].text)

    async def test_report_head_is_not_injected_into_markdown(self):
        (_, _, _, _), render = await self.prepare("image", images=[b"png"])
        markdown = render.call_args.args[0]
        self.assertNotIn("<!-- information -->", markdown)
        self.assertNotIn("<!-- section: 概览 -->", markdown)
        self.assertIn("分析正文", markdown)

    async def test_followup_does_not_append_usage_line(self):
        for mode in ("text", "image"):
            with self.subTest(mode=mode):
                (segments, text, _, _), render = await self.prepare(mode, analysis=False)
                self.assertNotIn("本次分析 Token 用量", text)
                self.assertTrue(all("本次分析 Token 用量" not in seg.text for seg in segments))
                if render.called:
                    self.assertNotIn("本次分析 Token 用量", render.call_args.args[0])
                    meta = render.call_args.kwargs["meta"]
                    for key in ("tokens_input", "tokens_output", "tokens_total", "usage", "usage_calls", "cache_hit_rate", "usage_summary"):
                        self.assertEqual(meta[key], "")
                    self.assertEqual(meta["usage_style"], "display: none")

    async def test_no_cache_records_are_not_presented_as_confirmed_zero_hit_rate(self):
        usage = self.analyzer.llm.session_summary.return_value
        usage.update(cache_read_tokens=0, cache_write_tokens=0, cache_hit_rate=0)
        (segments, text, _, _), _ = await self.prepare()
        self.assertIn("缓存命中率：暂无缓存数据", text)
        self.assertIn("缓存命中率：暂无缓存数据", segments[-1].text)

    async def test_cache_writes_with_no_reads_show_zero_percent(self):
        usage = self.analyzer.llm.session_summary.return_value
        usage.update(cache_read_tokens=0, cache_write_tokens=12000, cache_hit_rate=0)
        (_, text, _, _), _ = await self.prepare()
        self.assertIn("缓存命中率：0.0%", text)

    async def test_builtin_footers_render_complete_usage_and_hide_followup_usage(self):
        for analysis in (True, False):
            (_, _, _, _), render = await self.prepare("image", analysis=analysis, images=[b"png"])
            meta = render.call_args.kwargs["meta"]
            for name in ("default", "default-dark", "mobile", "gh"):
                with self.subTest(analysis=analysis, template=name):
                    template = load_template(self.analyzer._template_root, name)
                    html = template.render_page(content="正文", meta=meta, page=1, pages=1)
                    self.assertNotIn("{{usage_style}}", html)
                    self.assertNotIn("{{usage_summary}}", html)
                    if analysis:
                        self.assertIn("输入 12.0k · 输出 345 · 合计 12.3k tokens", html)
                        self.assertIn("缓存命中率：80.0%", html)
                    else:
                        self.assertIn('class="footer-usage-right" style="display: none"', html)
                        self.assertNotIn("缓存命中率", html)
