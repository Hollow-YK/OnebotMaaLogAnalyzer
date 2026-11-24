"""Boot confirmation for the versioned installation layout.

The stable launcher starts an unconfirmed version in a child process and waits
for ``confirm_boot()`` to be called after the runtime has started.  If that
never happens, the launcher rolls the pointer back to the previous version.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .install import InstallInfo, detect_install, is_valid_app_root
from .state import atomic_write_json, read_json

logger = logging.getLogger("Maa.Update.Boot")

POINTER_NAME = "current.json"
LAST_GOOD_NAME = "last-good.json"
MAX_BOOT_ATTEMPTS = 2


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def confirm_boot(info: InstallInfo | None = None) -> bool:
    """Mark the currently running version as successfully started.

    Returns ``True`` only for a versioned installation whose pointer names the
    running version.  Source and flat installations are a no-op.
    """
    current = info or detect_install()
    if not current.versioned:
        return False
    pointer = read_json(current.root / POINTER_NAME, {})
    if not isinstance(pointer, dict):
        pointer = {}
    active = str(pointer.get("active") or "").strip()
    running = current.app_root.name
    if active and active != running:
        active_dir = current.root / "versions" / active
        if is_valid_app_root(active_dir, current.kind):
            logger.warning(
                "[更新] 启动确认版本不一致：指针=%s 运行=%s", active, running
            )
            return False
        logger.warning(
            "[更新] 指针版本 %s 无效，恢复为正在运行的 %s", active, running
        )
    pointer.setdefault("schema", 1)
    pointer["active"] = running
    pointer.setdefault("previous", "")
    pointer["kind"] = current.kind
    pointer["confirmed"] = True
    pointer["attempts"] = 0
    # Only clear the version that just proved it can start; keep other bad
    # versions blocked so a fallback does not immediately retry them.
    pointer["bad"] = [
        str(item).strip()
        for item in (pointer.get("bad") or [])
        if str(item).strip() and str(item).strip() != running
    ]
    pointer["confirmed_at"] = _now()
    pointer["running_app_root"] = str(current.app_root)
    try:
        # The supervisor watches ``last-good.json``; write it first so a later
        # pointer write failure cannot turn a successfully started version into
        # a timeout rollback.
        atomic_write_json(
            current.root / LAST_GOOD_NAME,
            {
                "schema": 1,
                "version": running,
                "kind": current.kind,
                "confirmed_at": _now(),
            },
        )
        atomic_write_json(current.root / POINTER_NAME, pointer)
    except OSError as exc:
        logger.warning("[更新] 写启动确认失败：%s", exc)
        return False
    logger.info("[更新] 版本 %s 启动确认完成", running)
    return True


def restore_backup_paths(root: str | Path, pointer: dict) -> list[str]:
    """Restore files listed in a pointer's ``restore`` map (best-effort)."""
    import shutil

    base = Path(root)
    restored: list[str] = []
    entries = pointer.get("restore")
    if not isinstance(entries, list):
        return restored
    for item in entries:
        if not isinstance(item, dict):
            continue
        target_rel = str(item.get("target") or "").strip()
        backup_rel = str(item.get("backup") or "").strip()
        if not target_rel or not backup_rel:
            continue
        target = base / target_rel
        backup = base / backup_rel
        if not backup.is_file():
            continue
        temp = target.with_name(f".{target.name}.restore.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, temp)
            import os

            os.replace(temp, target)
            restored.append(target_rel)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("[更新] 恢复 %s 失败：%s", target_rel, exc)
    return restored


__all__ = [
    "LAST_GOOD_NAME",
    "MAX_BOOT_ATTEMPTS",
    "POINTER_NAME",
    "confirm_boot",
    "restore_backup_paths",
]
