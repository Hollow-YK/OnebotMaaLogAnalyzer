"""附件外壳由主题的 attach-img.html / attach-file.html 提供。"""
import shutil
import unittest
import uuid
from pathlib import Path

from maa_analyzer.features.maa.attachments import Attachment
from maa_analyzer.features.maa.render import RenderAttachment, _attachment_html
from maa_analyzer.features.maa.render.markdown import parse_report
from maa_analyzer.features.maa.render.template import load_template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
BUILTIN = ("default", "default-dark", "gh", "mobile")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32      # 40 字节
JSON = b'{"a": 1}'                             # 8 字节


class AttachmentShellTests(unittest.TestCase):
    def setUp(self):
        self.attachments = {
            "zip:debug/20261122.png": RenderAttachment(
                path="debug/20261122.png", kind="image", data=PNG,
                origin="日志包 debug/20261122.png"),
            "repo:config/maa.json": RenderAttachment(
                path="config/maa.json", kind="file", data=JSON,
                origin="项目 config/maa.json"),
        }

    def render(self, attachment: Attachment, *, template: str = "default",
               embed: bool = True) -> str:
        return _attachment_html(
            load_template(TEMPLATES, template), attachment, self.attachments, embed
        )

    # ── 取值 ──

    def test_image_shell_exposes_name_path_source_size(self):
        html = self.render(Attachment(path="debug/20261122.png", kind="image",
                                      source="zip"))
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("20261122.png", html)          # 文件名
        self.assertIn("debug/20261122.png", html)    # 相对路径
        self.assertIn("日志包", html)                 # 来源
        self.assertIn("40.0 B", html)                # 大小
        self.assertNotIn("{{", html)

    def test_file_shell_uses_repo_source_and_size(self):
        html = self.render(Attachment(path="config/maa.json", kind="file",
                                      source="repo"))
        self.assertIn("maa.json", html)
        self.assertIn("config/maa.json", html)
        self.assertIn("仓库", html)
        self.assertIn("8.0 B", html)
        self.assertIn("attach-file", html)
        self.assertNotIn("data:image/", html)

    # ── 状态 ──

    def test_missing_attachment_keeps_path_and_marks_status(self):
        html = self.render(Attachment(path="debug/nope.png", kind="image",
                                      source="zip"))
        self.assertIn("debug/nope.png", html)
        self.assertIn("未找到附件", html)
        self.assertIn("attach-missing", html)
        self.assertNotIn("data:image/", html)

    def test_embed_disabled_falls_back_to_file_shell(self):
        html = self.render(Attachment(path="debug/20261122.png", kind="image",
                                      source="zip"), embed=False)
        self.assertIn("图片未内联", html)
        self.assertIn("attach-image-skipped", html)
        self.assertIn("40.0 B", html)     # 大小仍然可见
        self.assertNotIn("data:image/", html)
        self.assertNotIn("{{", html)

    def test_standalone_attachment_is_not_wrapped_in_paragraph(self):
        template = load_template(TEMPLATES, "default")
        parsed = parse_report(
            "<!-- section: 证据 -->\n[附件@日志: debug/20261122.png]\n",
            lambda item: _attachment_html(template, item, self.attachments, True),
        )
        html = parsed.blocks[0].html
        self.assertIn('<figure class="attach-image">', html)
        self.assertNotIn("<p><figure", html)
        self.assertNotIn("<p></p>", html)

    # ── 主题契约 ──

    def test_builtin_templates_ship_both_attachment_shells(self):
        for name in BUILTIN:
            with self.subTest(template=name):
                template = load_template(TEMPLATES, name)
                self.assertTrue(template.attach_img.strip())
                self.assertTrue(template.attach_file.strip())
                file_html = _attachment_html(
                    template,
                    Attachment(path="config/maa.json", kind="file", source="repo"),
                    self.attachments, True,
                )
                image_html = _attachment_html(
                    template,
                    Attachment(path="debug/20261122.png", kind="image", source="zip"),
                    self.attachments, True,
                )
                for html in (file_html, image_html):
                    self.assertNotIn("{{", html)
                self.assertIn("config/maa.json", file_html)
                self.assertIn("debug/20261122.png", image_html)

    def test_template_without_attachment_shells_is_incomplete(self):
        # 仓库根目录是唯一稳定可写的位置（同 test_repo_acquisition）
        root = ROOT / "_tmp_attach_theme"
        base = root / f"case-{uuid.uuid4().hex[:8]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATES / "default", base / "default")
            shutil.copytree(TEMPLATES / "default", base / "broken")
            (base / "broken" / "attach-file.html").unlink()
            template = load_template(base, "broken")
            self.assertEqual(template.name, "default")
            self.assertTrue(any("attach-file.html" in w
                                for w in template.warnings))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # ── 安全 ──

    def test_attachment_values_are_escaped(self):
        self.attachments["zip:a/<b>&.txt"] = RenderAttachment(
            path="a/<b>&.txt", kind="file", data=b"x",
        )
        html = self.render(Attachment(path="a/<b>&.txt", kind="file",
                                      source="zip"))
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;b&gt;", html)
