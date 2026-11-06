"""Report and follow-up message grouping preserves content order and anchors."""
import unittest
from types import SimpleNamespace

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

        async def file(group_id, name, data):
            calls.append(("file", name))
            return True

        service = SimpleNamespace(send_message=message, send_file=file)
        watcher = LogWatcher(service, None)
        if followup:
            handler = MessageHandler.__new__(MessageHandler)
            handler.segment_sender = watcher.send_segments
            ids = await handler._send_answer(
                "123", SimpleNamespace(segments=segments), settings
            )
        else:
            ids = await watcher.send_segments(123, segments, settings, max_chars)
        return calls, ids

    async def test_default_merges_adjacent_text_and_images(self):
        calls, ids = await self.send([
            OutSegment("text", text="before"),
            OutSegment("image", data=b"image"),
            OutSegment("text", text="after"),
        ], AnalysisSettings())
        self.assertEqual([s["type"] for s in calls[0][1]], ["text", "image", "text"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(ids, ["1"])

    async def test_disabled_separates_each_segment_and_keeps_file_order(self):
        segments = [
            OutSegment("text", text="before"),
            OutSegment("image", data=b"image"),
            OutSegment("image", data=b"second image"),
            OutSegment("text", text="after"),
            OutSegment("file", data=b"file", name="report.txt"),
            OutSegment("text", text="end"),
        ]
        for followup in (False, True):
            with self.subTest(followup=followup):
                calls, ids = await self.send(
                    segments, AnalysisSettings(merge_messages=False), followup=followup
                )
                self.assertEqual([c[0] for c in calls], ["message"] * 4 + ["file", "message"])
                self.assertEqual(
                    [c[1][0]["type"] for c in calls if c[0] == "message"],
                    ["text", "image", "image", "text", "text"],
                )
                self.assertTrue(all(len(c[1]) == 1 for c in calls if c[0] == "message"))
                self.assertEqual(ids, ["1", "2", "3", "4", "6"])

    async def test_both_modes_split_long_text_and_skip_empty_segments(self):
        for enabled in (False, True):
            with self.subTest(merge_messages=enabled):
                calls, ids = await self.send([
                    OutSegment("text", text=" "),
                    OutSegment("image"),
                    OutSegment("file", name="empty.txt"),
                    OutSegment("text", text="a" * 2500),
                ], AnalysisSettings(merge_messages=enabled))
                chunks = [s["data"]["text"] for c in calls for s in c[1]]
                self.assertEqual("".join(chunks), "a" * 2500)
                self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
                self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main()
