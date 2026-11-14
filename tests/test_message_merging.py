"""Report forwarding preserves graph/text order and follow-up anchors."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.core.models import AnalysisSettings
from maa_analyzer.features.maa.attachments import OutSegment
from maa_analyzer.features.maa.message_handler import MessageHandler
from maa_analyzer.features.maa.watcher import LogWatcher


class MessageMergingTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, segments, settings, *, followup=False, max_chars=1000):
        calls = []

        async def message(group_id, content):
            calls.append(("message", content))
            return str(len(calls))

        async def forward(group_id, messages):
            calls.append(("forward", messages))
            return str(len(calls))

        async def file(group_id, name, data):
            calls.append(("file", name))
            return True

        service = SimpleNamespace(send_message=message, send_file=file,
                                  send_forward_messages=forward)
        watcher = LogWatcher(service, None)
        if followup:
            handler = MessageHandler.__new__(MessageHandler)
            handler.segment_sender = watcher.send_segments
            ids = await handler._send_answer(
                "123", SimpleNamespace(segments=segments), settings
            )
        else:
            ids = await watcher._send_outcome(
                123, SimpleNamespace(segments=segments), settings, max_chars
            )
        return calls, ids

    async def test_default_merges_adjacent_text_and_images(self):
        calls, ids = await self.send([
            OutSegment("text", text="before"),
            OutSegment("image", data=b"image"),
            OutSegment("text", text="after"),
        ], AnalysisSettings())
        self.assertEqual(calls[0][0], "message")
        self.assertEqual([s["type"] for s in calls[0][1]], ["text", "image", "text"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(ids, ["1"])

    async def test_forward_collects_all_content_then_uploads_files(self):
        calls, ids = await self.send([
            OutSegment("text", text="before"),
            OutSegment("image", data=b"image"),
            OutSegment("file", data=b"file", name="report.txt"),
            OutSegment("text", text="after"),
        ], AnalysisSettings(forward_analysis_results=True))
        self.assertEqual([c[0] for c in calls], ["forward", "file"])
        self.assertEqual([[s["type"] for s in node] for node in calls[0][1]],
                         [["text", "image"], ["text"]])
        self.assertEqual(ids, ["1"])

    async def test_followup_stays_normal_when_report_forwarding_enabled(self):
        calls, ids = await self.send([
            OutSegment("text", text="answer"), OutSegment("image", data=b"image"),
        ], AnalysisSettings(forward_analysis_results=True), followup=True)
        self.assertEqual([c[0] for c in calls], ["message"])
        self.assertEqual(ids, ["1"])

    async def test_both_modes_split_long_text_and_skip_empty_segments(self):
        for enabled in (False, True):
            with self.subTest(forward=enabled):
                calls, ids = await self.send([
                    OutSegment("text", text=" "), OutSegment("image"),
                    OutSegment("file", name="empty.txt"),
                    OutSegment("text", text="a" * 2500),
                ], AnalysisSettings(forward_analysis_results=enabled))
                bodies = calls[0][1] if enabled else [c[1] for c in calls]
                chunks = [s["data"]["text"] for body in bodies for s in body]
                self.assertEqual("".join(chunks), "a" * 2500)
                self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
                self.assertEqual(len(ids), 1 if enabled else 3)

    async def test_plain_text_fallback_uses_forward_and_card_id(self):
        service = SimpleNamespace(send_forward_messages=AsyncMock(return_value="card"))
        watcher = LogWatcher(service, None)
        ids = await watcher._send_outcome(
            123, SimpleNamespace(report_chunks=lambda _: ["one", "two"]),
            AnalysisSettings(forward_analysis_results=True), 1000,
        )
        self.assertEqual(ids, ["card"])
        service.send_forward_messages.assert_awaited_once()

    async def test_only_files_does_not_send_empty_forward(self):
        calls, ids = await self.send([
            OutSegment("file", data=b"file", name="report.txt"),
        ], AnalysisSettings(forward_analysis_results=True))
        self.assertEqual(calls, [("file", "report.txt")])
        self.assertEqual(ids, [])

    async def test_legacy_switch_is_ignored_and_does_not_enable_forwarding(self):
        settings = AnalysisSettings(merge_messages=False)
        self.assertFalse(settings.forward_analysis_results)
        self.assertNotIn("merge_messages", settings.model_dump())


if __name__ == "__main__":
    unittest.main()
