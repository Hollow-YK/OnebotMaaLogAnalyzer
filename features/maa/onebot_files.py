"""
群文件访问 — 递归列出群文件、按名称查找、下载文件。

通过 MaaService 提供的 Bot 能力 API 访问 OneBot v11 群文件接口，
不直接引用 bot/ 通信层。

兼容性说明:
  - NapCat 支持 get_group_root_files(file_count=...)，LLOneBot 不支持
  - 因此先尝试带参数调用，失败后回退到无参数调用
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.models import LogFileItem

if TYPE_CHECKING:
    from core.service import MaaService

logger = logging.getLogger("Maa.Files")

# 单次文件列表请求最多返回的文件数（NapCat 支持）
_FILE_COUNT = 2000


async def _call_with_optional_count(func, *args, count: int = _FILE_COUNT, **kwargs) -> dict:
    """
    调用群文件接口，带 file_count 失败时回退到不带该参数的调用。

    NapCat 的 get_group_root_files 接受 file_count，LLOneBot 不接受多余参数。
    """
    try:
        result = await func(*args, file_count=count, **kwargs)
        if isinstance(result, dict) and (result.get("files") is not None or result.get("folders") is not None):
            return result
    except TypeError:
        pass
    return await func(*args, **kwargs)


async def get_all_files(s: "MaaService", group_id: int) -> list[LogFileItem]:
    """
    递归获取群内所有文件（含子目录）。

    返回 LogFileItem 列表，每项的 relative_path 为相对群文件根目录的路径。
    """
    all_files: list[LogFileItem] = []
    # (folder_id | None, relative_dir)
    queue: list[tuple[Optional[str], str]] = [(None, "")]
    visited_folders: set[str] = set()

    while queue:
        folder_id, relative_dir = queue.pop(0)

        if folder_id is None:
            payload = await _call_with_optional_count(s.get_group_root_files, group_id)
        else:
            if folder_id in visited_folders:
                continue
            visited_folders.add(folder_id)
            payload = await _call_with_optional_count(
                s.get_group_files_by_folder, group_id, folder_id
            )

        if not isinstance(payload, dict):
            logger.warning(f"[群文件] 获取目录列表失败：group={group_id}, folder={folder_id}")
            continue

        for raw in payload.get("files") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = LogFileItem.model_validate(raw)
            except Exception:
                continue
            item.parent_id = folder_id or "/"
            item.relative_path = os.path.join(relative_dir, item.file_name) if relative_dir else item.file_name
            all_files.append(item)

        for folder in payload.get("folders") or []:
            if not isinstance(folder, dict):
                continue
            child_id = str(folder.get("folder_id") or folder.get("folderId") or "")
            child_name = str(folder.get("folder_name") or folder.get("folderName") or "")
            if child_id and child_id not in visited_folders:
                child_dir = os.path.join(relative_dir, child_name) if relative_dir else child_name
                queue.append((child_id, child_dir))

    return all_files


async def find_group_file_by_name(
    s: "MaaService",
    group_id: int,
    file_name: str,
    *,
    file_size: Optional[int] = None,
) -> Optional[LogFileItem]:
    """
    按文件名查找群文件，可选用文件大小过滤，返回最新修改的一项。
    """
    files = await get_all_files(s, group_id)
    candidates = [item for item in files if item.file_name == file_name]
    if not candidates:
        return None
    if file_size:
        sized = [item for item in candidates if int(item.size or 0) == int(file_size)]
        if sized:
            candidates = sized
    candidates.sort(key=lambda item: item.sort_timestamp(), reverse=True)
    return candidates[0]


async def download_group_file(
    s: "MaaService",
    group_id: int,
    file_id: str,
    target_path: str | Path,
    *,
    max_bytes: int,
    semaphore: asyncio.Semaphore,
    timeout_seconds: int = 120,
) -> int:
    """
    通过 get_group_file_url 获取下载链接并下载到 target_path。

    返回实际下载字节数；超过 max_bytes 时抛出 RuntimeError 并删除临时文件。
    """
    url = await s.get_group_file_url(group_id, file_id)
    if not url:
        raise RuntimeError("无法获取群文件下载链接。")

    async with semaphore:
        return await s.download_url(
            url,
            target_path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
