"""Small JSON state helpers used by the updater and the stable bootstrap.

Every file the updater relies on across a crash is written with a temp file,
``fsync`` and an atomic ``os.replace``.  Readers never observe a half-written
document, and an interrupted write leaves the previous document intact.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None) -> Any:
    """Read a JSON object, returning ``default`` on any read/parse failure."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync; Windows has no portable equivalent."""
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


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Atomically replace ``path`` with ``data`` on the same filesystem."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(path: str | Path, data: Any) -> Path:
    """Atomically write a UTF-8 JSON document."""
    return atomic_write_bytes(
        path,
        (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def mask_secret(value: str, *, keep: int = 4) -> str:
    """Mask tokens and QQ numbers for logs and command output."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}***{text[-keep:]}"


__all__ = ["atomic_write_bytes", "atomic_write_json", "mask_secret", "read_json"]
