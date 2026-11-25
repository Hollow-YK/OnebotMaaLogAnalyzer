"""报告命名空间、项目名来源与日志包精确计数。"""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from maa_analyzer.core.models import AnalysisSettings, ProjectConfig, ReportConfig
from maa_analyzer.features.maa.analyzer import MaaAnalyzer
from maa_analyzer.features.maa.archive_index import ArchiveInfo, build_archive_info
from maa_analyzer.features.maa.archive_profile import ArchiveProfile
from maa_analyzer.features.maa.attachments import OutSegment
from maa_analyzer.features.maa.render import RenderOutcome
from maa_analyzer.features.maa.render.template import load_template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def _profile() -> ArchiveProfile:
    return ArchiveProfile(
        project_name="Detected Project",
        device="Pixel 7",
        os_version="Android 13",
        metadata={"screen": "1080x2400"},
    )


class ReportInfoValueTests(unittest.TestCase):
    def test_project_sources_are_split_and_file_uses_upload_name(self):
        archive = ArchiveInfo(
            project_name="Resolved Project",
            original_file_name="real-logs.zip",
            file_count=57,
            total_uncompressed_size=40 * 1024 * 1024,
            log_total=12,
            error_image_total=3,
            custom_image_total=2,
            config_total=4,
            profile=_profile(),
        )
        values = MaaAnalyzer._report_info_values(
            archive=archive,
            file_name="real-logs.zip",
            downloaded=12 * 1024 * 1024,
            project=ProjectConfig(name="Configured Project"),
            log_count=4,
            image_count=2,
            repo_ref="v1.2.3",
        )
        self.assertEqual(values["project"], "Detected Project")
        self.assertEqual(values["project_configured"], "Configured Project")
        self.assertEqual(values["project_display"], "Resolved Project")
        self.assertEqual(values["project_source"], "配置")
        self.assertEqual(values["file"], "real-logs.zip")
        self.assertEqual(values["screen"], "1080x2400")
        self.assertEqual(values["file_count"], "57")
        self.assertEqual(values["log_total"], "12")
        self.assertEqual(values["error_image_total"], "3")
        self.assertEqual(values["custom_image_total"], "2")
        self.assertEqual(values["config_total"], "4")
        self.assertEqual(values["repo_ref"], "v1.2.3")

    def test_project_source_falls_back_to_log_and_default(self):
        archive = ArchiveInfo(project_name="Detected Project", profile=_profile())
        values = MaaAnalyzer._report_info_values(
            archive=archive, file_name="", downloaded=0,
            project=ProjectConfig(), log_count=0, image_count=0,
        )
        self.assertEqual(values["project_configured"], "")
        self.assertEqual(values["project_source"], "日志")

        # 主分析在回填 project.name 之后仍用回填前的配置值判断来源。
        backfilled = MaaAnalyzer._report_info_values(
            archive=archive, file_name="", downloaded=0,
            project=ProjectConfig(name="Detected Project"), log_count=0,
            image_count=0, configured_project_name="",
        )
        self.assertEqual(backfilled["project_configured"], "")
        self.assertEqual(backfilled["project_source"], "日志")
        self.assertEqual(backfilled["project_display"], "Detected Project")

        plain = MaaAnalyzer._report_info_values(
            archive=ArchiveInfo(), file_name="", downloaded=0,
            project=ProjectConfig(), log_count=0, image_count=0,
        )
        self.assertEqual(plain["project_display"], "MaaXXX")
        self.assertEqual(plain["project_source"], "默认")

    def test_repo_ref_normalization(self):
        self.assertEqual(MaaAnalyzer._repo_ref_value(None), "")
        self.assertEqual(
            MaaAnalyzer._repo_ref_value(SimpleNamespace(current_ref="（默认分支）")),
            "默认分支",
        )
        self.assertEqual(
            MaaAnalyzer._repo_ref_value(SimpleNamespace(current_ref="v1.2.3")),
            "v1.2.3",
        )


class PrepareOutputMetaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.analyzer = MaaAnalyzer.__new__(MaaAnalyzer)
        self.analyzer.s = SimpleNamespace(dm=SimpleNamespace(_dir="data"))
        self.analyzer._template_root_path = None
        self.analyzer.llm = SimpleNamespace(session_summary=Mock(return_value={
            "calls": 1, "prompt_tokens": 1, "completion_tokens": 1,
            "total_tokens": 2,
        }))
        self.analyzer._report_cfg = Mock(return_value=ReportConfig(render="image"))

    async def test_meta_has_configured_project_and_no_file(self):
        information = {
            "project": "Detected Project",
            "project_display": "Configured Project",
            "project_configured": "Configured Project",
            "project_source": "配置",
            "file": "real-logs.zip",
            "screen": "1080x2400",
        }
        archive = ArchiveInfo(zip_path="work/source.zip", project_name="Configured Project")
        with patch(
            "maa_analyzer.features.maa.analyzer.render_report",
            new_callable=AsyncMock,
            return_value=RenderOutcome(images=[b"png"]),
        ) as render:
            await self.analyzer._prepare_output(
                config_name="main",
                settings=AnalysisSettings(),
                raw_report="正文",
                information=information,
                archive=archive,
                project=ProjectConfig(name="Configured Project"),
                segments=[OutSegment(kind="text", text="正文")],
                notes=[],
                render_followup=True,
                session_id="s",
                show_analysis_usage=True,
            )
        meta = render.call_args.kwargs["meta"]
        self.assertEqual(meta["project"], "Configured Project")
        self.assertNotIn("file", meta)
        self.assertTrue(meta["tool_version"])
        self.assertEqual(meta["template"], "default")
        self.assertEqual(render.call_args.kwargs["information_values"], information)


class TemplateNamespaceTests(unittest.TestCase):
    def test_unnamespaced_and_out_of_scope_placeholders_stay_literal(self):
        root = ROOT / "_tmp_namespace_break"
        base = root / "case"
        try:
            root.mkdir(parents=True, exist_ok=True)
            if base.exists():
                import shutil
                shutil.rmtree(base)
            import shutil
            shutil.copytree(TEMPLATES / "default", base / "default")
            (base / "default" / "page.html").write_text(
                '<div class="page">{{header}}'
                '<main class="content">{{content}}</main>'
                '<div>{{file}}</div><div>{{attach.name}}</div>'
                '<div>{{meta.title}}</div>{{footer}}</div>',
                encoding="utf-8",
            )
            template = load_template(base, "default")
            html = template.render_page(
                content="正文", meta={"title": "T"}, page=1, pages=1,
                information_values={"file": "real.zip"},
            )
            self.assertIn("{{file}}", html)
            self.assertIn("{{attach.name}}", html)
            self.assertIn("T", html)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_information_shell_only_accepts_information_namespace(self):
        root = ROOT / "_tmp_info_scope"
        base = root / "case"
        try:
            root.mkdir(parents=True, exist_ok=True)
            if base.exists():
                import shutil
                shutil.rmtree(base)
            import shutil
            shutil.copytree(TEMPLATES / "default", base / "default")
            (base / "default" / "information.html").write_text(
                "<div>{{meta.title}}|{{project}}|"
                "{{information.project}}</div>",
                encoding="utf-8",
            )
            template = load_template(base, "default")
            html = template.render_information({"project": "P"})
            # information.html 只认 information.*；裸字段和 meta.* 都保留原文。
            self.assertIn("{{meta.title}}", html)
            self.assertIn("{{project}}", html)
            self.assertIn("|P</div>", html)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class ArchiveCountTests(unittest.TestCase):
    def test_exact_counts_exceed_display_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(8):
                    archive.writestr(f"logs/log-{index}.log", "x\n")
                for index in range(21):
                    archive.writestr(
                        f"on_error/on_error_{index:02d}.png", b"\x89PNG\r\n\x1a\n"
                    )
                for index in range(2):
                    archive.writestr(
                        f"debug/custom/custom_{index}.png", b"\x89PNG\r\n\x1a\n"
                    )
                for index in range(9):
                    archive.writestr(f"config/mxu-{index}.json", "{}")
                archive.writestr("maafw.log", "[Logger] Version v5.3.1\n")
            info = build_archive_info(
                str(path), original_file_name="real-logs.zip", project_name="CFG"
            )
        self.assertEqual(info.original_file_name, "real-logs.zip")
        self.assertEqual(info.log_total, 9)
        self.assertEqual(len(info.log_files), 6)
        self.assertEqual(info.error_image_total, 21)
        self.assertEqual(len(info.error_images), 20)
        self.assertEqual(info.custom_image_total, 2)
        self.assertEqual(info.config_total, 9)
        self.assertEqual(len(info.config_files), 8)


if __name__ == "__main__":
    unittest.main()
