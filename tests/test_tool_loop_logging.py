"""Tool-loop summaries distinguish configured rounds from observed calls."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from maa_analyzer.core.llm import ChatMessage, ContextOverflowError, ToolCall
from maa_analyzer.core.models import AnalysisSettings, ProjectConfig
from maa_analyzer.features.maa.analyzer import MaaAnalyzer


class ToolLoopLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, replies, rounds=2, clock=None):
        llm = SimpleNamespace(chat=AsyncMock(side_effect=replies))
        analyzer = MaaAnalyzer(None, llm)
        analyzer._llm_cfg = MagicMock(return_value=None)
        analyzer._context_budget = MagicMock(return_value=10000)
        analyzer._take_vision_requests = MagicMock(return_value=("", [], ""))
        analyzer._dispatch_agent_tool = AsyncMock(return_value="result")
        compactor = MagicMock()
        compactor.prepare.side_effect = lambda messages, **kwargs: messages
        compactor.needs_compaction.return_value = False
        compactor.force_compact = AsyncMock(side_effect=lambda messages: messages)
        compactor.tokens.return_value = 1
        analyzer._enforce_context_budget = MagicMock(side_effect=lambda messages, *_: messages)
        analyzer._shrink_tool_results = MagicMock(side_effect=lambda messages, **_: messages)
        project = ProjectConfig.model_validate({"repo": {"max_tool_rounds": rounds}})
        with self.assertLogs("Maa.Analyze", level="INFO") as captured:
            with patch("maa_analyzer.features.maa.analyzer.time", clock or SimpleNamespace(monotonic=lambda: 0)):
                result = await analyzer._run_tool_loop(
                    config_name="default", settings=AnalysisSettings(), messages=[], tools=[],
                    repo_tools=None, log_tools=None, project=project, compactor=compactor,
                )
        self.assertEqual(result, "final")
        return "\n".join(captured.output), llm

    @staticmethod
    def tool_reply(count=1):
        return ChatMessage(tool_calls=[ToolCall(id=str(i), name="test") for i in range(count)])

    async def test_round_limit_logs_rounds_and_total_calls_separately(self):
        logs, llm = await self.run_loop([self.tool_reply(2), self.tool_reply(3), ChatMessage(content="final")])
        self.assertIn("达到工具轮次上限；已进行 2/2 轮，共处理 5 次工具调用", logs)
        self.assertEqual(llm.chat.await_args_list[-1].kwargs["tool_choice"], "none")

    async def test_timeout_is_not_reported_as_round_limit(self):
        values = iter([0, 0, 601])
        logs, _ = await self.run_loop([self.tool_reply(), ChatMessage(content="final")],
                                     rounds=4, clock=SimpleNamespace(monotonic=lambda: next(values)))
        self.assertIn("达到工具循环时长上限；已进行 1/4 轮", logs)
        self.assertNotIn("达到工具轮次上限", logs)

    async def test_failed_model_request_and_overflow_have_correct_reason(self):
        for failure, reason, rounds in (
            ([None], "模型请求失败", 4),
            ([ContextOverflowError("too large")] * 3, "上下文多次超限", 5),
        ):
            with self.subTest(reason=reason):
                logs, _ = await self.run_loop([self.tool_reply(), *failure, ChatMessage(content="final")], rounds)
                self.assertIn(f"工具循环结束：{reason}", logs)
                self.assertNotIn("达到工具轮次上限", logs)
