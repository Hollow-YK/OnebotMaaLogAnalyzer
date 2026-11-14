"""已知 MaaFramework UI 日志包的布局识别测试。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from maa_analyzer.core.archive_names import detect_ui_from_file_name, ui_key_from_file_name
from maa_analyzer.features.maa.archive_profile import (
    UI_MFAA,
    UI_MTA,
    UI_MWU,
    UI_MFW,
    detect_archive_profile,
)
from maa_analyzer.features.maa.log_digest import build_log_digest


class MaaTauriAndroidArchiveTests(unittest.TestCase):
    NAME = "maa_tauri_android-logs-1760000000000-8b0c2f3a-2b6e-4a90-9f0a-f30dc7a5b6d9.zip"

    def _archive(self, root: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("logs/maa_tauri_android-filtered.log", "no fatal\n")
            archive.writestr(
                "device-info.txt",
                "\n".join([
                    "Startup time : 2026-11-14 10:00:00",
                    "MaaTauriAndroid : 0.2.2",
                    "Framework : v5.3.1",
                    "Device : Pixel 8",
                    "Android : 14 (API 34)",
                    "ABI : arm64-v8a",
                ]) + "\n",
            )
            archive.writestr(
                "snapshots/project.json",
                json.dumps({
                    "name": "Demo Resource",
                    "label": "Demo",
                    "version": "v1.2.3",
                    "interfaceVersion": 2,
                }),
            )
            archive.writestr("logs/maa/maafw.log", "[Logger] Version v5.3.1\n")
        return buffer.getvalue()

    def test_file_name_and_layout_are_recognized(self):
        self.assertEqual(detect_ui_from_file_name(self.NAME), UI_MTA)
        archive = self._archive("")
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            profile = detect_archive_profile("", archive=z, infos=z.infolist())
        self.assertEqual(profile.ui_type, UI_MTA)
        self.assertTrue(profile.layout_detected)
        self.assertEqual(profile.ui_version, "0.2.2")
        self.assertEqual(profile.framework_version, "v5.3.1")
        self.assertEqual(profile.resource_version, "v1.2.3")
        self.assertEqual(profile.project_name, "Demo Resource")

    def test_config_key(self):
        self.assertEqual(detect_ui_from_file_name(self.NAME), UI_MTA)
        self.assertEqual(ui_key_from_file_name(self.NAME), "mta")


class MWUArchiveTests(unittest.TestCase):
    NAME = "mwu-logs.zip"

    def test_file_name_layout_and_log_selection_are_recognized(self):
        self.assertEqual(detect_ui_from_file_name(self.NAME), UI_MWU)
        self.assertEqual(ui_key_from_file_name(self.NAME), "mwu")

        with tempfile.TemporaryDirectory() as directory:
            zip_path = Path(directory) / self.NAME
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("mwu.log", "2026-11-14 10:00:00 登录成功\n")
                archive.writestr("debug/maafw.log", "[Logger] Version v5.3.1\n")
                archive.writestr("updater.log", "更新完成。\n")

            with zipfile.ZipFile(zip_path) as archive:
                profile = detect_archive_profile("", archive=archive, infos=archive.infolist())
            self.assertEqual(profile.ui_type, UI_MWU)
            self.assertTrue(profile.layout_detected)
            self.assertEqual(profile.framework_version, "v5.3.1")

            digest = build_log_digest(str(zip_path), original_file_name=self.NAME)
        self.assertEqual(digest.profile.ui_key, "mwu")
        self.assertTrue(digest.is_log_archive)
        self.assertEqual(
            [Path(name).as_posix() for name in digest.log_files],
            ["debug/maafw.log", "mwu.log", "updater.log"],
        )


class MFWArchiveTests(unittest.TestCase):
    NAME = "Demo-1.2.3-20261114_101010.zip"

    def test_file_name_layout_and_log_selection_are_recognized(self):
        self.assertEqual(detect_ui_from_file_name(self.NAME), UI_MFW)
        self.assertEqual(ui_key_from_file_name(self.NAME), "mfw")

        with tempfile.TemporaryDirectory() as directory:
            zip_path = Path(directory) / self.NAME
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "debug/gui.log",
                    "\n".join([
                        "[2026-11-14][INFO][main.py][L193][main] | MFW 版本:0.3.4",
                        "[2026-11-14][INFO][core.py][L237][core] | 资源版本: 9.9.9-beta",
                        "[2026-11-14][INFO][main.py][L194][main] | "
                        "当前工作目录: /tmp/MFW-Demo",
                    ]) + "\n",
                )
                archive.writestr("debug/maafw.log", "[Logger] Version v5.3.1\n")
                archive.writestr("debug/crash.log", "traceback omitted\n")

            with zipfile.ZipFile(zip_path) as archive:
                profile = detect_archive_profile(
                    "",
                    file_name=self.NAME,
                    archive=archive,
                    infos=archive.infolist(),
                )
            self.assertEqual(profile.ui_type, UI_MFW)
            self.assertTrue(profile.layout_detected)
            self.assertEqual(profile.ui_version, "0.3.4")
            self.assertEqual(profile.framework_version, "v5.3.1")
            self.assertEqual(profile.project_name, "Demo")
            self.assertEqual(profile.resource_version, "9.9.9-beta")
            self.assertEqual(profile.metadata.get("working"), "/tmp/MFW-Demo")

            digest = build_log_digest(str(zip_path), original_file_name=self.NAME)
        self.assertEqual(digest.profile.ui_key, "mfw")
        self.assertTrue(digest.is_log_archive)
        self.assertEqual(
            [Path(name).as_posix() for name in digest.log_files],
            ["debug/maafw.log", "debug/gui.log", "debug/crash.log"],
        )

    def test_mfaa_layout_is_not_reclassified_as_mfw(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("debug/maafw.log", "[Logger] Version v5.3.1\n")
            archive.writestr("logs/log-20261114.log", "程序版本:v2.16.2\n")

        with zipfile.ZipFile(buffer) as archive:
            profile = detect_archive_profile("", archive=archive, infos=archive.infolist())
        self.assertEqual(profile.ui_type, UI_MFAA)
        self.assertTrue(profile.layout_detected)


if __name__ == "__main__":
    unittest.main()
