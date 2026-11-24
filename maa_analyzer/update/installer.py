"""Crash-safe installation of a prepared release.

The running version is never modified.  A new complete snapshot is built in
``versions/.tmp-*``, verified and self-checked, renamed into place, and only
then exposed through the atomic ``current.json`` pointer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from ..core.concurrency import run_blocking
from .boot import LAST_GOOD_NAME, POINTER_NAME
from .github import ReleaseInfo
from .install import (
    BUILD_INFO_NAME,
    MANIFEST_NAME,
    SUM_ASSET_NAME,
    InstallError,
    InstallInfo,
    asset_names,
    detect_install,
    safe_relative_path,
    safe_version_name,
)
from .manifest import (
    FileEntry,
    ReleaseManifest,
    bytes_sha256,
    diff_manifests,
    file_sha256,
    mirror_urls,
    parse_sums,
    raw_url,
    verify_tree,
)
from .net import HttpClient, HttpError
from .state import atomic_write_json, read_json

logger = logging.getLogger("Maa.Update.Installer")

MAX_ZIP_MEMBERS = 20000
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
SELF_CHECK_TIMEOUT = 180
INCREMENTAL_MAX_FILES = 200
INCREMENTAL_MAX_RATIO = 0.6


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class PreparedUpdate:
    release: ReleaseInfo
    manifest: ReleaseManifest
    version_dir: Path
    mode: str
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    downloaded_bytes: int = 0
    downloaded_files: int = 0
    warnings: list[str] = field(default_factory=list)


class VersionInstaller:
    """Download, materialize, verify and activate one release."""

    def __init__(
        self,
        install: InstallInfo | None = None,
        *,
        data_dir: str | Path = "data",
        config_path: str | Path | None = None,
        download_mirrors: Iterable[str] = (),
        http: HttpClient | None = None,
    ):
        self.install = install or detect_install()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )
        self.download_mirrors = [
            str(item).strip() for item in download_mirrors if str(item).strip()
        ]
        self.http = http or HttpClient()
        self._owns_http = http is None

    async def close(self) -> None:
        if self._owns_http:
            await self.http.close()

    # ── download helpers ─────────────────────────────────────────

    async def _asset(
        self,
        release: ReleaseInfo,
        name: str,
        *,
        max_bytes: int,
    ) -> bytes:
        asset = release.asset(name)
        if asset is None or not asset.url:
            raise InstallError(f"Release 缺少资产：{name}")
        digest = asset.digest.removeprefix("sha256:") if asset.digest else ""
        errors: list[str] = []
        for url in mirror_urls(asset.url, self.download_mirrors):
            try:
                data = await self.http.get_bytes(url, max_bytes=max_bytes)
            except HttpError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if digest and bytes_sha256(data) != digest:
                errors.append(f"{url}: SHA256 与 GitHub asset digest 不一致")
                continue
            return data
        raise InstallError("下载失败：" + "；".join(errors[-3:]))

    async def _manifest(
        self, release: ReleaseInfo
    ) -> tuple[ReleaseManifest, dict[str, str]]:
        sums_bytes = await self._asset(
            release, SUM_ASSET_NAME, max_bytes=2 * 1024 * 1024
        )
        sums = parse_sums(sums_bytes.decode("utf-8", "replace"))
        archive, manifest_name = asset_names(release.version_text, self.install.kind)
        del archive
        manifest_asset = release.asset(manifest_name)
        if manifest_asset is None:
            raise InstallError(f"Release 缺少 manifest：{manifest_name}")
        expected = sums.get(manifest_name)
        if not expected:
            raise InstallError(f"{SUM_ASSET_NAME} 中没有 {manifest_name} 的哈希")
        data = await self._asset(
            release, manifest_name, max_bytes=8 * 1024 * 1024
        )
        if bytes_sha256(data) != expected:
            raise InstallError("manifest SHA256 校验失败")
        import json

        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InstallError("manifest 不是合法 JSON") from exc
        manifest = ReleaseManifest.from_dict(raw)
        if manifest.version != release.version_text:
            raise InstallError(
                f"manifest 版本 {manifest.version} 与 Release "
                f"{release.version_text} 不一致"
            )
        if manifest.kind != self.install.kind:
            raise InstallError(
                f"manifest 类型 {manifest.kind} 与本地安装 "
                f"{self.install.kind} 不一致"
            )
        return manifest, sums

    def _local_manifest(self) -> ReleaseManifest | None:
        path = self.install.manifest_path
        if path is None or not path.is_file():
            return None
        raw = read_json(path, None)
        if not isinstance(raw, dict):
            return None
        try:
            return ReleaseManifest.from_dict(raw)
        except InstallError as exc:
            logger.warning("[更新] 本地 manifest 无效：%s", exc)
            return None

    async def _download_raw(self, entry: FileEntry, tag: str) -> bytes:
        url = raw_url(tag, entry.source or entry.path)
        errors: list[str] = []
        for candidate in mirror_urls(url, self.download_mirrors):
            try:
                data = await self.http.get_bytes(
                    candidate,
                    max_bytes=entry.size if entry.size else MAX_FILE_BYTES,
                )
            except HttpError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if len(data) != entry.size and entry.size:
                errors.append(
                    f"{candidate}: 大小不一致（{len(data)} != {entry.size}）"
                )
                continue
            if bytes_sha256(data) != entry.sha256:
                errors.append(f"{candidate}: SHA256 不一致")
                continue
            return data
        raise InstallError(
            f"下载 {entry.path} 失败：" + "；".join(errors[-3:])
        )

    # ── materialization ─────────────────────────────────────────

    async def prepare(
        self, release: ReleaseInfo, *, download_mode: str = "auto"
    ) -> PreparedUpdate:
        mode = str(download_mode or "auto").strip().lower()
        if mode not in {"auto", "full", "incremental"}:
            mode = "auto"
        if self.install.source:
            raise InstallError("源码运行不支持自动更新，请使用 git pull。")
        if self.install.kind not in {"standard", "light", "astrbot"}:
            raise InstallError(f"未知安装类型：{self.install.kind}")

        manifest, sums = await self._manifest(release)
        local = self._local_manifest()
        diff = diff_manifests(local, manifest)
        archive_name, _ = asset_names(release.version_text, self.install.kind)
        archive_asset = release.asset(archive_name)
        archive_size = archive_asset.size if archive_asset else 0

        use_incremental = mode == "incremental"
        if mode == "auto":
            use_incremental = bool(
                local is not None
                and diff.changed
                and len(diff.changed) <= INCREMENTAL_MAX_FILES
                and (
                    archive_size <= 0
                    or sum(
                        manifest.files[path].size for path in diff.changed
                    )
                    <= archive_size * INCREMENTAL_MAX_RATIO
                )
            )
        if mode == "auto" and not local:
            use_incremental = False

        versions_dir = self.install.root / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        temp = versions_dir / f".tmp-{uuid.uuid4().hex}"
        temp.mkdir(parents=True, exist_ok=False)
        try:
            if use_incremental:
                try:
                    prepared = await self._materialize_incremental(
                        release, manifest, diff, temp
                    )
                except Exception:
                    if mode != "auto":
                        raise
                    logger.warning(
                        "[更新] 增量构建失败，回退整包下载。", exc_info=True
                    )
                    shutil.rmtree(temp, ignore_errors=True)
                    temp.mkdir(parents=True, exist_ok=False)
                    prepared = await self._materialize_full(
                        release, manifest, temp, archive_name, sums
                    )
                    prepared.warnings.append("增量构建失败，已回退整包")
            else:
                prepared = await self._materialize_full(
                    release, manifest, temp, archive_name, sums
                )
            await self._verify_and_check(temp, manifest)
            final = versions_dir / safe_version_name(release.version_text)
            if final.exists():
                if self._referenced(final):
                    raise InstallError(f"目标版本目录已存在且正在使用：{final}")
                trash = versions_dir / f".trash-{uuid.uuid4().hex}"
                final.rename(trash)
            temp.rename(final)
            self._fsync_dir(versions_dir)
            prepared.version_dir = final
            prepared.manifest = manifest
            return prepared
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    async def _materialize_full(
        self,
        release: ReleaseInfo,
        manifest: ReleaseManifest,
        temp: Path,
        archive_name: str,
        sums: dict[str, str],
    ) -> PreparedUpdate:
        archive = await self._asset(
            release, archive_name, max_bytes=MAX_ZIP_BYTES
        )
        expected = sums.get(archive_name)
        if not expected:
            raise InstallError(f"{SUM_ASSET_NAME} 中没有 {archive_name} 的哈希")
        if bytes_sha256(archive) != expected:
            raise InstallError(f"{archive_name} SHA256 校验失败")
        await run_blocking(self._extract_archive, archive, temp)
        self._write_manifest(temp, manifest)
        return PreparedUpdate(
            release=release,
            manifest=manifest,
            version_dir=temp,
            mode="full",
            changed=tuple(manifest.sorted_paths()),
            downloaded_bytes=len(archive),
            downloaded_files=1,
        )

    async def _materialize_incremental(
        self,
        release: ReleaseInfo,
        manifest: ReleaseManifest,
        diff,
        temp: Path,
    ) -> PreparedUpdate:
        changed = set(diff.changed)
        pending: list[FileEntry] = []
        downloaded_bytes = 0
        changed_files = 0
        for path in manifest.sorted_paths():
            entry = manifest.files[path]
            target = temp / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if path not in changed:
                source = self.install.app_root / path
                if source.is_file():
                    if self.install.versioned:
                        self._link_or_copy(source, target)
                    else:
                        # Flat installs remain mutable (manual unzip/edits), so
                        # never share an inode with the old root files.
                        shutil.copy2(source, target)
                    continue
                # Unchanged per manifest but locally missing: download it too.
                changed.add(path)
            content = entry.content
            if content is not None:
                if bytes_sha256(content) != entry.sha256:
                    raise InstallError(f"manifest 内嵌内容哈希错误：{path}")
                target.write_bytes(content)
                downloaded_bytes += len(content)
                changed_files += 1
                continue
            pending.append(entry)

        semaphore = asyncio.Semaphore(4)
        errors: list[BaseException] = []

        async def _finish(entry: FileEntry) -> None:
            nonlocal downloaded_bytes, changed_files
            async with semaphore:
                try:
                    data = await self._download_raw(entry, release.tag)
                except BaseException as exc:
                    errors.append(exc)
                    return
                target = temp / entry.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                downloaded_bytes += len(data)
                changed_files += 1

        await asyncio.gather(*(_finish(entry) for entry in pending))
        if errors:
            raise InstallError(str(errors[0]))
        self._write_manifest(temp, manifest)
        return PreparedUpdate(
            release=release,
            manifest=manifest,
            version_dir=temp,
            mode="incremental",
            changed=tuple(sorted(changed)),
            unchanged=tuple(diff.unchanged),
            stale=tuple(diff.stale),
            downloaded_bytes=downloaded_bytes,
            downloaded_files=changed_files,
        )

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
            return
        except OSError:
            pass
        shutil.copy2(source, target)

    @staticmethod
    def _write_manifest(directory: Path, manifest: ReleaseManifest) -> None:
        atomic_write_json(directory / MANIFEST_NAME, manifest.to_dict())

    def _referenced(self, path: Path) -> bool:
        pointer = read_json(self.install.root / POINTER_NAME, {})
        last_good = read_json(self.install.root / LAST_GOOD_NAME, {})
        names: set[str] = set()
        if isinstance(pointer, dict):
            names.update(
                str(pointer.get(key) or "")
                for key in ("active", "previous")
            )
        if isinstance(last_good, dict):
            names.add(str(last_good.get("version") or ""))
        return path.name in {name for name in names if name}

    async def _verify_and_check(
        self, directory: Path, manifest: ReleaseManifest
    ) -> None:
        self._prune_unlisted(directory, manifest)
        bad = await run_blocking(verify_tree, directory, manifest)
        if bad:
            raise InstallError(
                "文件校验失败：" + "、".join(bad[:5])
                + (" 等" if len(bad) > 5 else "")
            )
        raw_info = read_json(directory / BUILD_INFO_NAME, {})
        if not isinstance(raw_info, dict):
            raise InstallError("缺少 build-info.json")
        if str(raw_info.get("kind") or "") != self.install.kind:
            raise InstallError("build-info.json 的 kind 不匹配")
        if str(raw_info.get("version") or "") != manifest.version:
            raise InstallError("build-info.json 的 version 不匹配")
        await run_blocking(self._self_check, directory)
        # AstrBot's compileall self-check may create __pycache__ directories;
        # keep the prepared snapshot exactly equal to the manifest.
        self._prune_unlisted(directory, manifest)

    @staticmethod
    def _prune_unlisted(directory: Path, manifest: ReleaseManifest) -> None:
        """Remove extra files so the version directory is an exact snapshot."""
        allowed = set(manifest.files)
        allowed.add(MANIFEST_NAME)
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
                continue
            relative = path.relative_to(directory).as_posix()
            if relative not in allowed:
                try:
                    path.unlink()
                except OSError as exc:
                    raise InstallError(
                        f"无法清理未列出的文件：{relative}（{exc}）"
                    ) from exc

    def _self_check(self, directory: Path) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(directory) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.install.kind == "astrbot":
            script = (
                "import compileall, sys\n"
                f"sys.path.insert(0, {str(directory)!r})\n"
                "import maa_analyzer, maa_analyzer.runtime, maa_analyzer.config\n"
                "import maa_analyzer.platforms.astrbot.adapter\n"
                f"ok = compileall.compile_dir({str(directory)!r}, quiet=1)\n"
                "sys.exit(0 if ok else 1)\n"
            )
            command = [sys.executable, "-c", script]
        else:
            entry_script = directory / "maa_analyzer" / "bare_entry.py"
            runner = (
                "import runpy, sys; "
                f"sys.path.insert(0, {str(directory)!r}); "
                f"script = {str(entry_script)!r}; "
                "sys.argv = [script, '--self-check']; "
                "runpy.run_path(script, run_name='__main__')"
            )
            command = [sys.executable, "-c", runner]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.install.root),
                env=env,
                capture_output=True,
                text=True,
                timeout=SELF_CHECK_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError(f"新版本自检无法执行：{exc}") from exc
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            raise InstallError(f"新版本自检失败：{tail or completed.returncode}")

    @staticmethod
    def _extract_archive(data: bytes, destination: Path) -> None:
        with tempfile.NamedTemporaryFile(
            prefix=".maa-update-", suffix=".zip", delete=False
        ) as handle:
            archive_path = Path(handle.name)
            handle.write(data)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ZIP_MEMBERS:
                    raise InstallError("压缩包文件数量异常")
                total = sum(info.file_size for info in infos)
                if total > MAX_ZIP_BYTES:
                    raise InstallError("压缩包解压体积异常")
                names = [info.filename.replace("\\", "/") for info in infos]
                for name in names:
                    if name.startswith("/") or ".." in PurePosixPath(name).parts:
                        raise InstallError(f"压缩包包含非法路径：{name}")
                top = {name.split("/", 1)[0] for name in names if "/" in name}
                strip = next(iter(top)) if len(top) == 1 else ""
                for info, name in zip(infos, names):
                    if name.endswith("/"):
                        continue
                    relative = name[len(strip) + 1 :] if strip else name
                    if not relative:
                        continue
                    safe_relative_path(relative)
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise InstallError(f"压缩包包含符号链接：{name}")
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        finally:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ── activation ──────────────────────────────────────────────

    def activate(self, prepared: PreparedUpdate) -> dict:
        """Back up mutable files, install the launcher and switch the pointer."""
        root = self.install.root
        version = prepared.release.version_text
        versions = root / "versions"
        versions.mkdir(parents=True, exist_ok=True)

        previous = str(self.install.pointer.get("active") or "").strip()
        if not previous and self.install.versioned:
            previous = self.install.version
        if not previous:
            # A flat installation keeps its original ``main.py.legacy`` as the
            # fallback instead of pretending the old layout is a version dir.
            previous = ""

        txn = f"{version}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        backup_dir = root / ".updates" / "backups" / txn
        backup_dir.mkdir(parents=True, exist_ok=True)
        restore: list[dict[str, str]] = []
        for path in self._restore_candidates():
            if not path.is_file():
                continue
            backup = backup_dir / f"{abs(hash(str(path))):x}-{path.name}"
            shutil.copy2(path, backup)
            restore.append(
                {"target": str(path), "backup": str(backup)}
            )

        pointer_written = False
        try:
            self._install_root_assets(prepared.version_dir, backup_dir, restore)
            self._sync_root_templates(
                prepared.version_dir, backup_dir, restore
            )
            self._install_bootstrap(prepared.version_dir, backup_dir, restore)
            pointer = {
                "schema": 1,
                "active": version,
                "previous": previous,
                "kind": self.install.kind,
                "attempts": 0,
                "confirmed": False,
                "bad": list(self.install.bad_versions),
                "updated_at": _now(),
                "restore": restore,
            }
            atomic_write_json(root / POINTER_NAME, pointer)
            pointer_written = True
            self._fsync_dir(root)
            return pointer
        except BaseException:
            if not pointer_written:
                self._restore(restore)
            raise

    def _restore_candidates(self) -> list[Path]:
        paths: list[Path] = []
        if self.config_path is not None:
            paths.append(self.config_path)
        root = self.install.root
        for name in ("metadata.yaml", "_conf_schema.json"):
            candidate = root / name
            if candidate.is_file():
                paths.append(candidate)
        data_dir = self.data_dir
        if data_dir.is_dir():
            for child in data_dir.iterdir():
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                for name in ("settings.json", "groups.json", "commands.json", "project.json"):
                    candidate = child / name
                    if candidate.is_file():
                        paths.append(candidate)
        return paths

    def _install_root_assets(
        self, version_dir: Path, backup_dir: Path, restore: list[dict[str, str]]
    ) -> None:
        if self.install.kind != "astrbot":
            return
        for name in ("metadata.yaml", "_conf_schema.json"):
            source = version_dir / name
            if not source.is_file():
                continue
            target = self.install.root / name
            if target.is_file():
                backup = backup_dir / f"{name}.root"
                shutil.copy2(target, backup)
                if not any(item["target"] == str(target) for item in restore):
                    restore.append({"target": str(target), "backup": str(backup)})
            self._atomic_copy(source, target)

    def _sync_root_templates(
        self, version_dir: Path, backup_dir: Path, restore: list[dict[str, str]]
    ) -> None:
        """Refresh root templates while preserving user-edited files.

        The running app reads ``<install root>/templates``.  A file is replaced
        only when it still matches the previous release manifest; otherwise it
        is treated as a user customization and left alone.
        """
        source_root = version_dir / "templates"
        if not source_root.is_dir():
            return
        old_manifest = self._local_manifest()
        for source in sorted(source_root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(version_dir).as_posix()
            target = self.install.root / relative
            if target.is_file():
                old_entry = (
                    old_manifest.files.get(relative)
                    if old_manifest is not None
                    else None
                )
                if old_entry is not None:
                    if file_sha256(target) != old_entry.sha256:
                        logger.info(
                            "[更新] 保留用户修改的模板：%s", relative
                        )
                        continue
                else:
                    # Legacy flat installs may not have a manifest.  Keep the
                    # existing file rather than silently discarding possible
                    # user edits; new template files are still added below.
                    logger.info("[更新] 保留旧安装模板：%s", relative)
                    continue
                backup = backup_dir / (
                    "templates-" + str(abs(hash(relative))) + "-" + target.name
                )
                shutil.copy2(target, backup)
                restore.append(
                    {"target": str(target), "backup": str(backup)}
                )
            self._atomic_copy(source, target)

    def _install_bootstrap(
        self, version_dir: Path, backup_dir: Path, restore: list[dict[str, str]]
    ) -> None:
        root = self.install.root
        bootstrap_source = version_dir / "maa_bootstrap.py"
        launcher_source = version_dir / "main.py"
        if bootstrap_source.is_file():
            target = root / "maa_bootstrap.py"
            if not target.is_file() or file_sha256(target) != file_sha256(bootstrap_source):
                if target.is_file():
                    backup = backup_dir / "maa_bootstrap.py"
                    shutil.copy2(target, backup)
                    restore.append({"target": str(target), "backup": str(backup)})
                new_file = root / "maa_bootstrap.py.new"
                shutil.copy2(bootstrap_source, new_file)
                self._check_bootstrap(new_file)
                os.replace(new_file, target)
        if not launcher_source.is_file():
            return
        target_main = root / "main.py"
        legacy = root / "main.py.legacy"
        if target_main.is_file() and not legacy.exists():
            try:
                existing = target_main.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                existing = ""
            if "run_bare_launcher" not in existing and "load_astrbot_main" not in existing:
                shutil.copy2(target_main, legacy)
        if target_main.is_file() and file_sha256(target_main) == file_sha256(launcher_source):
            return
        new_main = root / "main.py.new"
        shutil.copy2(launcher_source, new_main)
        self._check_launcher(new_main)
        os.replace(new_main, target_main)

    def _check_bootstrap(self, path: Path) -> None:
        script = (
            "import importlib.util, sys; "
            f"spec = importlib.util.spec_from_file_location('_maa_bootstrap_check', {str(path)!r}); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(self.install.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-800:]
            raise InstallError(f"引导器模块自检失败：{tail or completed.returncode}")

    def _check_launcher(self, path: Path) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.install.root) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.install.kind == "astrbot":
            command = [sys.executable, "-m", "py_compile", str(path)]
        else:
            command = [sys.executable, str(path), "--launcher-check"]
        completed = subprocess.run(
            command,
            cwd=str(self.install.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-800:]
            raise InstallError(f"引导器自检失败：{tail or completed.returncode}")

    @staticmethod
    def _atomic_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temp)
        os.replace(temp, target)

    @staticmethod
    def _restore(restore: list[dict[str, str]]) -> None:
        for item in restore:
            target = Path(str(item.get("target") or ""))
            backup = Path(str(item.get("backup") or ""))
            if not target.name or not backup.is_file():
                continue
            temp: Path | None = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.restore.tmp")
                shutil.copy2(backup, temp)
                os.replace(temp, target)
            except OSError:
                if temp is not None:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = [
    "INCREMENTAL_MAX_FILES",
    "INCREMENTAL_MAX_RATIO",
    "PreparedUpdate",
    "VersionInstaller",
]
