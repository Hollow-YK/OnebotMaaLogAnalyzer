#!/usr/bin/env python3
"""Stable bootstrap for the versioned installation layout.

This file is intentionally dependency-free: it must keep working even when the
newly installed application is broken.  It is never modified in place during an
update; the updater writes a candidate, checks it, and atomically replaces it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
POINTER = "current.json"
LAST_GOOD = "last-good.json"
RESTART_CODE = 87
MAX_BOOT_ATTEMPTS = 2
BOOT_TIMEOUT_SECONDS = 120
MAX_RESTARTS = 5


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            try:
                fd_dir = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd_dir)
                finally:
                    os.close(fd_dir)
            except OSError:
                pass
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _pointer() -> dict[str, Any]:
    data = _read_json(ROOT / POINTER, {})
    return data if isinstance(data, dict) else {}


def _last_good() -> dict[str, Any]:
    data = _read_json(ROOT / LAST_GOOD, {})
    return data if isinstance(data, dict) else {}


def _version_info(path: Path) -> dict[str, Any]:
    data = _read_json(path / "build-info.json", {})
    return data if isinstance(data, dict) else {}


def _valid_app_root(path: Path, kind: str) -> bool:
    if not path.is_dir():
        return False
    if not (path / "build-info.json").is_file():
        return False
    if not (path / "maa_analyzer" / "__init__.py").is_file():
        return False
    if kind == "astrbot":
        return (path / "plugin_shell.py").is_file()
    return (path / "maa_analyzer" / "bare_entry.py").is_file()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _version_dir_names() -> list[str]:
    versions = ROOT / "versions"
    try:
        items = [item for item in versions.iterdir() if item.is_dir()]
    except OSError:
        return []
    items.sort(key=_mtime, reverse=True)
    return [item.name for item in items if not item.name.startswith(".")]


def _candidate(
    *,
    label: str,
    app_root: Path,
    version: str,
    kind: str,
    versioned: bool,
    script: Path,
) -> dict[str, Any]:
    return {
        "label": label,
        "app_root": app_root,
        "version": version,
        "kind": kind,
        "versioned": versioned,
        "script": script,
    }


def _versioned_candidates(kind: str) -> list[dict[str, Any]]:
    pointer = _pointer()
    last_good = _last_good()
    names: list[str] = []
    for value in (
        pointer.get("active"),
        pointer.get("previous"),
        last_good.get("version"),
    ):
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)
    for name in _version_dir_names():
        if name not in names:
            names.append(name)

    active = str(pointer.get("active") or "").strip()
    confirmed = bool(pointer.get("confirmed"))
    attempts = _as_int(pointer.get("attempts"))
    bad = {
        str(item).strip()
        for item in (pointer.get("bad") or [])
        if str(item).strip()
    }
    current_kind = str(pointer.get("kind") or kind or "").strip() or kind or "standard"

    result: list[dict[str, Any]] = []
    for name in names:
        if name in bad:
            continue
        if name == active and not confirmed and attempts >= MAX_BOOT_ATTEMPTS:
            continue
        path = ROOT / "versions" / name
        info = _version_info(path)
        candidate_kind = str(info.get("kind") or current_kind)
        if not _valid_app_root(path, candidate_kind):
            continue
        script = (
            path / "plugin_shell.py"
            if candidate_kind == "astrbot"
            else path / "maa_analyzer" / "bare_entry.py"
        )
        result.append(
            _candidate(
                label=f"版本 {name}",
                app_root=path,
                version=name,
                kind=candidate_kind,
                versioned=True,
                script=script,
            )
        )
    return result


def _bare_legacy_candidates() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    legacy = ROOT / "main.py.legacy"
    if legacy.is_file():
        result.append(
            _candidate(
                label="旧版回退",
                app_root=ROOT,
                version="legacy",
                kind="standard",
                versioned=False,
                script=legacy,
            )
        )
    entry = ROOT / "maa_analyzer" / "bare_entry.py"
    if entry.is_file():
        result.append(
            _candidate(
                label="本地源码/扁平安装",
                app_root=ROOT,
                version="flat",
                kind=str(_read_json(ROOT / "build-info.json", {}).get("kind") or "source"),
                versioned=False,
                script=entry,
            )
        )
    return result


def bare_candidates() -> list[dict[str, Any]]:
    kind = str(_pointer().get("kind") or "")
    candidates = _versioned_candidates(kind or "standard")
    candidates.extend(_bare_legacy_candidates())
    return candidates


def _initialize_attempt(candidate: dict[str, Any]) -> None:
    if not candidate.get("versioned"):
        return
    pointer = _pointer()
    if not pointer:
        pointer = {
            "schema": 1,
            "active": candidate["version"],
            "previous": "",
            "kind": candidate["kind"],
            "attempts": 0,
            "confirmed": False,
            "bad": [],
        }
    if str(pointer.get("active") or "") != candidate["version"]:
        return
    if pointer.get("confirmed"):
        return
    try:
        pointer["attempts"] = _as_int(pointer.get("attempts")) + 1
        _write_json(ROOT / POINTER, pointer)
    except OSError:
        pass


def _restore_backups(pointer: dict[str, Any]) -> None:
    entries = pointer.get("restore")
    if not isinstance(entries, list):
        return
    for item in entries:
        if not isinstance(item, dict):
            continue
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


def rollback(reason: str) -> dict[str, Any]:
    """Switch the pointer to the best fallback and restore backed-up files."""
    pointer = _pointer()
    _restore_backups(pointer)
    failed = str(pointer.get("active") or "").strip()
    bad = [
        str(item).strip()
        for item in (pointer.get("bad") or [])
        if str(item).strip()
    ]
    if failed and failed not in bad:
        bad.append(failed)
    pointer["bad"] = bad
    fallback = ""
    previous = str(pointer.get("previous") or "").strip()
    last_good = str(_last_good().get("version") or "").strip()
    for name in (previous, last_good):
        if (
            name
            and name not in bad
            and _valid_app_root(
                ROOT / "versions" / name,
                str(pointer.get("kind") or "standard"),
            )
        ):
            fallback = name
            break
    pointer.update(
        {
            "active": fallback,
            "previous": "",
            "attempts": 0,
            "confirmed": True,
            "rollback_reason": reason,
            "rolled_back_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    )
    try:
        _write_json(ROOT / POINTER, pointer)
    except OSError:
        pass
    print(f"[更新] 启动失败，已回退：{reason}", file=sys.stderr)
    return pointer


def _wait_for_confirm(version: str, child: subprocess.Popen, timeout: int) -> bool:
    deadline = time.monotonic() + max(5, timeout)
    while time.monotonic() < deadline:
        data = _read_json(ROOT / LAST_GOOD, {})
        if isinstance(data, dict) and str(data.get("version") or "") == version:
            return True
        if child.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            child.kill()
        except OSError:
            pass


def _spawn(candidate: dict[str, Any]) -> subprocess.Popen:
    env = os.environ.copy()
    env["MAA_SUPERVISED"] = "1"
    env["MAA_INSTALL_ROOT"] = str(ROOT)
    env["MAA_ACTIVE_VERSION"] = str(candidate.get("version") or "")
    env["MAA_ACTIVE_KIND"] = str(candidate.get("kind") or "")
    env["PYTHONPATH"] = (
        str(candidate["app_root"])
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    app_root = str(candidate["app_root"])
    script = str(candidate["script"])
    # Never run the app module as a file: its directory would become
    # ``sys.path[0]`` and ``maa_analyzer/platform.py`` would shadow the stdlib
    # ``platform`` module.  ``runpy`` keeps the package root first instead.
    runner = (
        "import runpy, sys; "
        f"sys.path.insert(0, {app_root!r}); "
        f"script = {script!r}; "
        "sys.argv[0] = script; "
        "runpy.run_path(script, run_name='__main__')"
    )
    command = [sys.executable, "-c", runner, *sys.argv[1:]]
    return subprocess.Popen(command, env=env, cwd=os.getcwd())


def _forward_signal(signum: int, _frame: Any) -> None:
    child = getattr(_forward_signal, "_child", None)
    if isinstance(child, subprocess.Popen) and child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass


def _ignore_signal(_signum: int, _frame: Any) -> None:
    """Let the child handle Ctrl+C so it can persist state before exiting."""
    return None


def _install_signal_handlers(child: subprocess.Popen) -> None:
    _forward_signal._child = child  # type: ignore[attr-defined]
    for name, handler in (("SIGTERM", _forward_signal), ("SIGINT", _ignore_signal)):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def launcher_check() -> int:
    candidates = bare_candidates()
    if not candidates:
        print("[启动] 未找到可运行的版本目录。", file=sys.stderr)
        return 1
    for candidate in candidates:
        print(f"[启动] 候选：{candidate['label']} -> {candidate['script']}")
    return 0


def run_bare_launcher() -> int:
    if "--launcher-check" in sys.argv[1:]:
        return launcher_check()
    restarts = 0
    while restarts <= MAX_RESTARTS:
        pointer = _pointer()
        active = str(pointer.get("active") or "").strip()
        if (
            active
            and not pointer.get("confirmed")
            and _as_int(pointer.get("attempts")) >= MAX_BOOT_ATTEMPTS
        ):
            rollback("启动尝试次数超限")
        candidates = bare_candidates()
        if not candidates:
            print("[启动] 没有可用版本，请重新解压发布包。", file=sys.stderr)
            return 1
        candidate = candidates[0]
        pointer = _pointer()
        active = str(pointer.get("active") or "")
        active_valid = bool(
            active
            and _valid_app_root(ROOT / "versions" / active, candidate["kind"])
        )
        versioned_active = bool(
            candidate["versioned"]
            and (active == candidate["version"] or not active or not active_valid)
        )
        confirmed = bool(pointer.get("confirmed"))
        wait_confirm = versioned_active and not confirmed
        if wait_confirm:
            _initialize_attempt(candidate)
            pointer = _pointer()
            if _as_int(pointer.get("attempts")) > MAX_BOOT_ATTEMPTS:
                rollback("启动尝试次数超限")
                restarts += 1
                continue
        child = _spawn(candidate)
        _install_signal_handlers(child)
        if wait_confirm:
            if not _wait_for_confirm(candidate["version"], child, BOOT_TIMEOUT_SECONDS):
                _terminate(child)
                rollback(
                    f"新版本 {candidate['version']} 未在 "
                    f"{BOOT_TIMEOUT_SECONDS}s 内完成启动确认"
                )
                restarts += 1
                continue
        code = child.wait()
        if code == RESTART_CODE:
            restarts += 1
            continue
        return int(code or 0)
    print("[启动] 多次重启仍未成功，退出。", file=sys.stderr)
    return 1


# ── AstrBot plugin shim ─────────────────────────────────────────


def _astrbot_candidates() -> list[dict[str, Any]]:
    pointer = _pointer()
    kind = str(pointer.get("kind") or "astrbot")
    candidates = _versioned_candidates(kind)
    legacy = ROOT / "main.py.legacy"
    if legacy.is_file():
        candidates.append(
            _candidate(
                label="旧版插件回退",
                app_root=ROOT,
                version="legacy",
                kind="astrbot",
                versioned=False,
                script=legacy,
            )
        )
    flat = ROOT / "plugin_shell.py"
    if flat.is_file():
        candidates.append(
            _candidate(
                label="扁平插件安装",
                app_root=ROOT,
                version="flat",
                kind="astrbot",
                versioned=False,
                script=flat,
            )
        )
    return candidates


def _try_astrbot_candidates() -> Any:
    errors: list[str] = []
    for candidate in _astrbot_candidates():
        pointer = _pointer()
        active = str(pointer.get("active") or "")
        if candidate["versioned"] and (
            active == candidate["version"] or not active
        ):
            if bool(pointer.get("confirmed")):
                pass
            elif _as_int(pointer.get("attempts")) >= MAX_BOOT_ATTEMPTS:
                continue
            else:
                _initialize_attempt(candidate)
        app_root = str(candidate["app_root"])
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        module_name = "maa_plugin_shell_" + str(candidate["version"]).replace(
            ".", "_"
        ).replace("-", "_")
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, str(candidate["script"])
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载 {candidate['script']}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            main_cls = getattr(module, "Main", None)
            if main_cls is None:
                raise ImportError("插件入口缺少 Main 类")
            return main_cls
        except Exception as exc:
            errors.append(f"{candidate['label']}: {exc}")
            continue
    raise ImportError("\n".join(errors))


def load_astrbot_main() -> Any:
    pointer = _pointer()
    active = str(pointer.get("active") or "").strip()
    if (
        active
        and not pointer.get("confirmed")
        and _as_int(pointer.get("attempts")) >= MAX_BOOT_ATTEMPTS
    ):
        rollback("AstrBot 启动尝试次数超限")
    try:
        return _try_astrbot_candidates()
    except ImportError as first:
        pointer = _pointer()
        active = str(pointer.get("active") or "").strip()
        if active and not pointer.get("confirmed"):
            # The newly activated plugin shell cannot even be imported.  Mark
            # it bad, restore the previous config and try the fallback once.
            rollback(f"AstrBot 插件导入失败：{first}")
            try:
                return _try_astrbot_candidates()
            except ImportError as second:
                raise ImportError(
                    "无法加载任何 OnebotMaaLogAnalyzer 插件版本：\n"
                    + str(first)
                    + "\n回退后仍失败：\n"
                    + str(second)
                ) from second
        raise ImportError(
            "无法加载任何 OnebotMaaLogAnalyzer 插件版本：\n" + str(first)
        ) from first


__all__ = [
    "MAX_BOOT_ATTEMPTS",
    "POINTER",
    "RESTART_CODE",
    "ROOT",
    "bare_candidates",
    "launcher_check",
    "load_astrbot_main",
    "rollback",
    "run_bare_launcher",
]
