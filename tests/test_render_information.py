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
    "project": "narutomobile",
    "project_configured": "MAN",
    "project_display": "MAN",
    "project_source": "配置",
    "ui": "MFAAvalonia",
    "ui_version": "1.2.3",
    "module_version": "",
    "resource_version": "1.0.0-alpha.9",
    "framework_version": "v5.3.1",
    "device": "Pixel 7",
    "os": "Android 13",
    "screen": "1080x2400",
    "file": "log_20260101.zip",
    "size": "12.3 MB",
    "log_count": "4",
    "image_count": "2",
    "file_count": "57",
    "uncompressed_size": "38.4 MB",
    "log_total": "12",
    "error_image_total": "3",
    "custom_image_total": "2",
    "config_total": "4",
    "repo_ref": "v1.2.3",
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
                             "v5.3.1", "Pixel 7", "Android 13", "1080x2400",
                             "log_20260101.zip", "12.3 MB", "57",
                             "38.4 MB", "v1.2.3"):
                    self.assertIn(text, info_html)

                first = template.render_page(
                    content="", meta={}, page=1, pages=2, information=info_html,
                    information_values=INFO,
                )
                later = template.render_page(
                    content="", meta={}, page=2, pages=2, information=info_html,
                    information_values=INFO,
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
                '<div class="page-header"><h1>{{meta.title}}</h1>'
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
        html = _fallback_information({
            **INFO,
            "project": "<script>x</script>",
            "project_display": "<script>x</script>",
        })
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("运行环境", html)

    def test_information_fields_are_available_in_every_page_frame(self):
        root = ROOT / "_tmp_info_fields"
        base = root / f"case-{uuid.uuid4().hex[:8]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATES / "default", base / "default")
            tpl = base / "default"
            (tpl / "header.html").write_text(
                '<div class="page-header">{{information.screen}} · '
                "{{information.file}} · {{information.project_display}}</div>",
                encoding="utf-8",
            )
            (tpl / "footer.html").write_text(
                '<div class="page-footer">{{information.log_total}} · '
                "{{information.repo_ref}} · {{meta.page}}</div>",
                encoding="utf-8",
            )
            template = load_template(base, "default")
            first = template.render_page(
                content="正文", meta={"title": "T"}, page=1, pages=2,
                information_values=INFO,
            )
            later = template.render_page(
                content="正文", meta={"title": "T"}, page=2, pages=2,
                information_values=INFO,
            )
            for html in (first, later):
                self.assertIn("1080x2400", html)
                self.assertIn("log_20260101.zip", html)
                self.assertIn("MAN", html)
                self.assertIn("12", html)
                self.assertIn("v1.2.3", html)
                self.assertNotIn("{{information.", html)
            self.assertIn("1", first)
            self.assertIn("2", later)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_information_fields_alone_do_not_enable_information_slot(self):
        root = ROOT / "_tmp_info_fields_only"
        base = root / f"case-{uuid.uuid4().hex[:8]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATES / "default", base / "default")
            tpl = base / "default"
            (tpl / "page.html").write_text(
                '<div class="page">{{header}}<div>{{information.screen}}</div>'
                '<main class="content">{{content}}</main>{{footer}}</div>',
                encoding="utf-8",
            )
            template = load_template(base, "default")
            self.assertFalse(template.information_slot)
            html = template.render_page(
                content="正文", meta={"title": "T"}, page=1, pages=1,
                information_values=INFO,
            )
            self.assertIn("1080x2400", html)
            self.assertNotIn("report-info", html)
            self.assertNotIn("{{information.", html)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_attach_values_are_not_available_outside_attachment_shells(self):
        template = load_template(TEMPLATES, "default")
        html = template.render_page(
            content="正文", meta={}, page=1, pages=1,
            information_values=INFO,
        )
        self.assertNotIn("{{attach.", html)

    def test_pack_blocks_uses_smaller_first_page(self):
        blocks = [BlockMetrics(index=i, height=120) for i in range(5)]
        pages, warnings = pack_blocks(blocks, 500, first_available_height=250)
        self.assertEqual(pages, [[0, 1], [2, 3, 4]])
        self.assertEqual(warnings, [])
