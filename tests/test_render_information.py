"""报告头由主题的 information.html + {{information}} 插槽提供。"""
import shutil
import unittest
import uuid
from pathlib import Path

from maa_analyzer.features.maa.render import _fallback_information
from maa_analyzer.features.maa.render.paginate import BlockMetrics, pack_blocks
from maa_analyzer.features.maa.render.template import load_template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
BUILTIN = ("default", "default-dark", "gh", "mobile")

INFO = {
    "project": "MAA",
    "ui": "MFAAvalonia",
    "ui_version": "1.2.3",
    "module_version": "",
    "resource_version": "1.0.0-alpha.9",
    "framework_version": "v5.3.1",
    "device": "Pixel 7",
    "os": "Android 13",
    "file": "log_20260101.zip",
    "size": "12.3 MB",
    "log_count": "4",
    "image_count": "2",
    "part_note": "",
}


class InformationShellTests(unittest.TestCase):
    def test_builtin_templates_use_information_slot(self):
        for name in BUILTIN:
            with self.subTest(template=name):
                self.assertTrue(load_template(TEMPLATES, name).information_slot)

    def test_builtin_templates_render_header_on_first_page_only(self):
        for name in BUILTIN:
            with self.subTest(template=name):
                template = load_template(TEMPLATES, name)
                info_html = template.render_information(INFO)
                self.assertNotIn("{{", info_html)
                for text in ("MFAAvalonia", "1.2.3", "1.0.0-alpha.9",
                             "v5.3.1", "Pixel 7", "Android 13",
                             "log_20260101.zip", "12.3 MB"):
                    self.assertIn(text, info_html)

                first = template.render_page(
                    content="", meta={}, page=1, pages=2, information=info_html,
                )
                later = template.render_page(
                    content="", meta={}, page=2, pages=2, information=info_html,
                )
                self.assertIn("MFAAvalonia", first)
                self.assertNotIn("{{information}}", first)
                self.assertNotIn("MFAAvalonia", later)
                self.assertNotIn("{{information}}", later)

    def test_information_slot_accepts_custom_position(self):
        root = ROOT / "_tmp_info_slot"
        base = root / f"case-{uuid.uuid4().hex[:8]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATES / "default", base / "default")
            tpl = base / "default"
            (tpl / "page.html").write_text(
                '<div class="page">{{information}}{{header}}'
                '<main class="content">{{content}}</main>{{footer}}</div>',
                encoding="utf-8",
            )
            (tpl / "header.html").write_text(
                '<div class="page-header"><h1>{{title}}</h1>'
                "{{information}}</div>",
                encoding="utf-8",
            )
            template = load_template(base, "default")
            self.assertTrue(template.information_slot)
            first = template.render_page(
                content="正文", meta={}, page=1, pages=2,
                information="<div>H</div>",
            )
            self.assertEqual(first.count("<div>H</div>"), 2)   # page + header
            later = template.render_page(
                content="正文", meta={}, page=2, pages=2,
                information="<div>H</div>",
            )
            self.assertNotIn("<div>H</div>", later)
            self.assertNotIn("{{information}}", first + later)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_template_without_information_file_still_loads(self):
        root = ROOT / "_tmp_info_shell"
        base = root / f"case-{uuid.uuid4().hex[:8]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATES / "default", base / "default")
            (base / "default" / "information.html").unlink()
            template = load_template(base, "default")
            self.assertEqual(template.name, "default")
            self.assertEqual(template.information, "")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_fallback_shell_escapes_values(self):
        html = _fallback_information({**INFO, "project": "<script>x</script>"})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("运行环境", html)

    def test_pack_blocks_uses_smaller_first_page(self):
        blocks = [BlockMetrics(index=i, height=120) for i in range(5)]
        pages, warnings = pack_blocks(blocks, 500, first_available_height=250)
        self.assertEqual(pages, [[0, 1], [2, 3, 4]])
        self.assertEqual(warnings, [])
