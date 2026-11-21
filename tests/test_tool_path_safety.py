"""Run with: python -m unittest discover -s tests -v."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from maa_analyzer.core.models import RepoConfig
from maa_analyzer.features.maa.attachments import normalize_rel_path, parse_attachments
from maa_analyzer.features.maa.archive_index import (
    build_archive_info,
    describe_zip,
    read_zip_member,
)
from maa_analyzer.features.maa.log_tools import LogArchiveTools
from maa_analyzer.features.maa.repo import RepoProvider
from maa_analyzer.features.maa.repo_tools import RepoTools
from maa_analyzer.features.maa.safety import normalize_tool_path, resolve_repo_path


INVALID_PATHS = (
    "../outside.txt", "sub/../../outside.txt", r"..\outside.txt",
    "sub/../normal.txt", r"sub\..\normal.txt", "sub/..",
    "/absolute.txt", r"\absolute.txt", r"C:\outside.txt", "C:outside.txt",
    "./C:/outside.txt", r"\\server\share\file.txt", "bad\x00name",
)


class NormalizeToolPathTests(unittest.TestCase):
    def test_rejects_absolute_and_parent_paths(self):
        for path in INVALID_PATHS:
            with self.subTest(path=path):
                self.assertIsNone(normalize_tool_path(path))

    def test_preserves_relative_paths(self):
        for path, expected in (
            ("", ""), (".", ""), ("./", ""), ("././debug/", "debug"),
            (r"debug\maafw.log", "debug/maafw.log"),
            ("配置//./日志.txt", "配置/日志.txt"),
            ("sub/..normal.txt", "sub/..normal.txt"),
        ):
            with self.subTest(path=path):
                self.assertEqual(normalize_tool_path(path), expected)


    def test_attachment_paths_reject_parent_components_for_both_sources(self):
        for path in INVALID_PATHS:
            with self.subTest(path=path):
                self.assertIsNone(normalize_rel_path(path))
                for source in ("项目", "日志"):
                    self.assertEqual(parse_attachments(f"[附件@{source}: {path}]"), [])


class RepoPathErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.tools = RepoTools(SimpleNamespace(root=self.root), RepoConfig())

    async def test_missing_paths_are_reported_without_host_paths(self):
        for name in ("read_file", "list_dir", "search_repo"):
            with self.subTest(tool=name):
                result = await self.tools.execute(name, {"path": "missing/file.txt", "pattern": "."})
                self.assertEqual(result, "路径不存在：missing/file.txt")

    def test_missing_root_is_reported_without_host_paths(self):
        resolved, error = resolve_repo_path(self.root / "missing", "file.txt")
        self.assertIsNone(resolved)
        self.assertEqual(error, "路径不存在：file.txt")

    def test_resolution_errors_do_not_expose_exception_paths(self):
        for exc in (PermissionError(f"denied: {self.root}"), RuntimeError(f"loop: {self.root}")):
            with self.subTest(error=type(exc).__name__), patch.object(Path, "resolve", side_effect=exc):
                resolved, error = resolve_repo_path(self.root, "file.txt")
                self.assertIsNone(resolved)
                self.assertEqual(error, "路径无法解析，已拒绝：file.txt")


class RepoToolPathTests(unittest.IsolatedAsyncioTestCase):
    def _symlink(self, link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            self.skipTest(f"当前环境无法创建符号链接：{exc}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "repo"
        self.root.mkdir()
        (base / "outside.txt").write_text("OUTSIDE_MARKER\n")
        (self.root / "normal.txt").write_bytes(b"NORMAL_MARKER\n")
        self._symlink(self.root / "linked.txt", base / "outside.txt")
        self._symlink(self.root / "internal.txt", self.root / "normal.txt")
        self._symlink(self.root / "external_dir", base, target_is_directory=True)
        (self.root / "secrets.json").write_text("SECRET_MARKER\n")
        self._symlink(self.root / "alias.txt", self.root / "secrets.json")
        (self.root / ".env").write_text("SECRET_MARKER\n")
        (self.root / ".aws").mkdir()
        (self.root / ".aws" / "settings.txt").write_text("SECRET_MARKER\n")
        # The requested name and resolved target must both pass the policy.
        self._symlink(self.root / "credentials.txt", self.root / "normal.txt")
        self.tools = RepoTools(SimpleNamespace(root=self.root), RepoConfig(extensions=[]))

    async def test_rejects_invalid_arguments_for_all_path_tools(self):
        for name in ("read_file", "list_dir", "search_repo"):
            for path in INVALID_PATHS:
                with self.subTest(tool=name, path=path):
                    result = await self.tools.execute(name, {"path": path, "pattern": "."})
                    self.assertTrue("路径非法" in result or "已拒绝" in result, result)

    async def test_read_rejects_external_and_sensitive_symlinks(self):
        for path in ("linked.txt", "external_dir/outside.txt", "alias.txt", "credentials.txt"):
            with self.subTest(path=path):
                result = await self.tools.execute("read_file", {"path": path})
                self.assertIn("拒绝", result)
                self.assertNotIn("MARKER", result)

    async def test_search_does_not_leak_external_or_sensitive_content(self):
        result = await self.tools.execute("search_repo", {"pattern": "MARKER"})
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("OUTSIDE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)
        self.assertNotIn("credentials.txt", result)

    async def test_listing_hides_rejected_entries(self):
        result = await self.tools.execute("list_dir", {})
        self.assertIn("normal.txt", result)
        self.assertIn("internal.txt", result)
        for name in ("linked.txt", "external_dir", "secrets.json", "alias.txt", ".env", ".aws", "credentials.txt"):
            with self.subTest(name=name):
                self.assertNotIn(name, result)

    async def test_internal_symlinks_and_nested_reads_still_work(self):
        result = await self.tools.execute("read_file", {"path": "internal.txt"})
        self.assertIn("NORMAL_MARKER", result)
        nested = self.root / "sub"
        nested.mkdir()
        (nested / "ok.txt").write_text("NESTED_MARKER\n")
        result = await self.tools.execute("search_repo", {"path": "sub", "pattern": "NESTED"})
        self.assertIn("sub/ok.txt:1: NESTED_MARKER", result)
        result = await self.tools.execute("list_dir", {"path": "sub"})
        self.assertIn("ok.txt", result)


    async def test_parent_paths_are_rejected_even_with_internal_symlinks(self):
        sub = self.root / "sub"
        (sub / "deep").mkdir(parents=True)
        (sub / "normal.txt").write_text("ACTUAL_TARGET\n")
        self._symlink(self.root / "dir_link", sub / "deep", target_is_directory=True)
        for path in ("sub/deep/../normal.txt", "dir_link/../normal.txt"):
            with self.subTest(path=path):
                result = await self.tools.execute("read_file", {"path": path})
                self.assertIn("路径非法", result)
                self.assertNotIn("ACTUAL_TARGET", result)
                self.assertNotIn("NORMAL_MARKER", result)
        path, error = resolve_repo_path(self.root, "external_dir/../normal.txt")
        self.assertIsNone(path)
        self.assertIn("路径非法", error)

    async def test_prefix_sibling_and_symlink_loop_are_rejected(self):
        sibling = self.root.with_name("repo-backup")
        sibling.mkdir()
        (sibling / "normal.txt").write_text("OUTSIDE_MARKER")
        self._symlink(self.root / "sibling", sibling, target_is_directory=True)
        self._symlink(self.root / "loop", "loop")
        for path in ("sibling/normal.txt", "loop"):
            with self.subTest(path=path):
                resolved, error = resolve_repo_path(self.root, path)
                self.assertIsNone(resolved)
                self.assertIn("拒绝", error)

    async def test_provider_attachments_and_references_share_policy(self):
        provider = RepoProvider(RepoConfig(path=str(self.root), extensions=[]), self.root.parent)
        paths = ["internal.txt", "linked.txt", "alias.txt", "credentials.txt", *INVALID_PATHS]
        self.assertEqual(provider.find_files(paths), [("internal.txt", b"NORMAL_MARKER\n")])
        for path in ("linked.txt", "alias.txt", "credentials.txt", "../outside.txt"):
            self.assertIsNone(provider._read_line(self.root, path, 1))
        self.assertEqual(provider._read_line(self.root, "normal.txt", 1), "NORMAL_MARKER")
        result = provider._scan(self.root, ["MARKER"])
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("OUTSIDE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)

    async def test_provider_rejects_internal_parent_paths(self):
        sub = self.root / "sub"
        sub.mkdir()
        provider = RepoProvider(RepoConfig(path=str(self.root)), self.root.parent)
        self.assertEqual(provider.find_files(["sub/../normal.txt"]), [])
        self.assertIsNone(provider._read_line(self.root, "sub/../normal.txt", 1))


class SymlinkSupportTests(unittest.TestCase):
    def test_symlink_permission_failure_skips_instead_of_errors(self):
        case = RepoToolPathTests("test_listing_hides_rejected_entries")
        self.addCleanup(case.doCleanups)
        with patch.object(Path, "symlink_to", side_effect=OSError("symlink privilege unavailable")):
            with self.assertRaisesRegex(unittest.SkipTest, "无法创建符号链接"):
                case.setUp()


class ArchiveProfilePathTests(unittest.TestCase):
    def test_profile_ignores_unsafe_members_with_and_without_safe_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            zip_path = Path(directory) / "logs.zip"
            for safe_log in (False, True):
                with self.subTest(safe_log=safe_log):
                    with zipfile.ZipFile(zip_path, "w") as archive:
                        for name in ("../maafw.log", "/maafw.log", "C:/maafw.log", ".aws/maafw.log"):
                            archive.writestr(name, "Version v9.9.9-evil\n")
                        archive.writestr("../mxu-agent-evil.log", "unsafe layout")
                        if safe_log:
                            archive.writestr("debug/maafw.log", "Version v1.2.3\n")
                    for result in (
                        build_archive_info(str(zip_path), original_file_name="logs.zip"),
                        describe_zip(str(zip_path)),
                    ):
                        self.assertIsNotNone(result)
                        self.assertEqual(result.profile.framework_version, "v1.2.3" if safe_log else "")
                        self.assertEqual(result.profile.layout_detected, safe_log)
                        self.assertEqual(result.all_members, ["debug/maafw.log"] if safe_log else [])
                        self.assertNotIn(
                            "v9.9.9-evil",
                            result.format_prompt(original_file_name="logs.zip"),
                        )


class LogToolPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.zip_path = Path(self.temp.name) / "logs.zip"
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            archive.writestr("./debug/maafw.log", "NORMAL_MARKER\n")
            archive.writestr(r"debug\other.txt", "NORMAL_MARKER\n")
            archive.writestr("./images/ok.png", b"image")
            for name in INVALID_PATHS[:-1]:
                archive.writestr(name, "UNSAFE_MARKER\n")
                image_name = name + ("/image.png" if name.endswith("..") else ".png")
                archive.writestr(image_name, b"image")
            archive.writestr(".aws/settings.txt", "SECRET_MARKER\n")
            archive.writestr(".aws/hidden.png", b"image")
        self.tools = LogArchiveTools(str(self.zip_path))
        self.addCleanup(self.tools.close)

    async def test_rejects_invalid_arguments(self):
        for path in INVALID_PATHS:
            for operation in (
                lambda: self.tools.read_file(path),
                lambda: self.tools.list_files(path),
                lambda: self.tools.search("MARKER", path),
            ):
                with self.subTest(path=path):
                    self.assertIn("路径非法", await operation())

    async def test_search_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.search("MARKER")
        self.assertIn("NORMAL_MARKER", result)
        self.assertNotIn("UNSAFE_MARKER", result)
        self.assertNotIn("SECRET_MARKER", result)

    async def test_listing_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.list_files()
        self.assertIn("debug/", result)
        for name in ("..", "absolute", "C:", "server", ".aws"):
            with self.subTest(name=name):
                self.assertNotIn(name, result)

    async def test_image_listing_ignores_unsafe_and_sensitive_members(self):
        result = await self.tools.list_images()
        self.assertIn("images/ok.png", result)
        self.assertEqual(result.count("字节"), 1)

    async def test_reads_normalized_member_names(self):
        for path in ("debug/maafw.log", "./debug/maafw.log", r"debug\other.txt"):
            with self.subTest(path=path):
                self.assertIn("NORMAL_MARKER", await self.tools.read_file(path))


    async def test_internal_parent_paths_are_rejected_for_zip_reads(self):
        result = await self.tools.read_file("debug/nested/../maafw.log")
        self.assertIn("路径非法", result)
        self.assertIsNone(read_zip_member(str(self.zip_path), "debug/nested/../maafw.log"))

    async def test_direct_zip_reads_enforce_the_same_policy(self):
        for path in (*INVALID_PATHS, ".aws/settings.txt"):
            with self.subTest(path=path):
                self.assertIsNone(read_zip_member(str(self.zip_path), path))


if __name__ == "__main__":
    unittest.main()
