"""模板编辑器预览：内置示例附件同时覆盖图片与文件两种外壳。"""
import importlib.util
import unittest
from pathlib import Path

from maa_analyzer.features.maa.attachments import parse_attachments
from maa_analyzer.features.maa.render import _attachment_html
from maa_analyzer.features.maa.render.markdown import parse_report
from maa_analyzer.features.maa.render.template import load_template

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "template_editor", ROOT / "tools" / "templates" / "editor.py"
)
editor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(editor)


class EditorPreviewTests(unittest.TestCase):
    def test_sample_declares_image_and_file_attachments(self):
        kinds = {item.kind for item in parse_attachments(editor.SAMPLE_MARKDOWN)}
        self.assertEqual(kinds, {"image", "file"})
        self.assertIn(editor.DEMO_IMAGE_PATH, editor.SAMPLE_MARKDOWN)
        self.assertIn(editor.DEMO_FILE_PATH, editor.SAMPLE_MARKDOWN)

    def test_demo_attachments_cover_both_shells_without_dir(self):
        items = editor.index_attachments("")
        image = items[f"zip:{editor.DEMO_IMAGE_PATH.lower()}"]
        file_item = items[f"repo:{editor.DEMO_FILE_PATH.lower()}"]
        self.assertEqual(image.kind, "image")
        self.assertEqual(image.data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(file_item.kind, "file")
        self.assertIn(b"threshold", file_item.data)

    def test_preview_html_contains_image_file_and_missing(self):
        template = load_template(ROOT / "templates", "default")
        attachments = editor.index_attachments("")
        parsed = parse_report(
            editor.SAMPLE_MARKDOWN,
            lambda item: _attachment_html(template, item, attachments, True),
        )
        html = "".join(block.html for block in parsed.blocks)
        self.assertIn('class="attach-image"', html)
        self.assertIn('class="attach-file"', html)
        self.assertIn("attach-missing", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("PVP.json", html)
        self.assertNotIn("{{", html)
