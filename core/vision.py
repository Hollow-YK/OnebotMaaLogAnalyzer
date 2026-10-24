"""
多模态（看图）— 图片选取、体积过滤与 data URL 构造。

约束：
  - **不做缩放**：只按原始字节大小过滤，避免引入图片处理库
  - 图片只从日志包与项目仓库读取，复用现有路径沙箱与敏感文件策略
  - 图片字节只在内存中流转，不落地临时文件
"""
from __future__ import annotations

import base64
import logging
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from core.models import VisionConfig
from features.maa.safety import is_sensitive_path

logger = logging.getLogger("Maa.Vision")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def mime_for(path: str) -> str:
    suffix = PurePosixPath(str(path or "").replace("\\", "/")).suffix.lower()
    return _MIME.get(suffix, "application/octet-stream")


def max_image_bytes(vision: VisionConfig) -> int:
    return max(64, int(vision.max_image_kb or 800)) * 1024


def image_part(data: bytes, path: str, detail: str = "high") -> dict[str, Any]:
    """构造 OpenAI 兼容的 image_url 内容段。"""
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime_for(path)};base64,{encoded}",
            "detail": str(detail or "high"),
        },
    }


def read_zip_image(zip_path: str, rel: str,
                   limit_bytes: int) -> Optional[bytes]:
    """从日志压缩包读取一张图片；超限或不存在返回 None。"""
    target = str(rel or "").replace("\\", "/").lstrip("/")
    if not target or is_sensitive_path(target):
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            info = next(
                (i for i in archive.infolist()
                 if not i.is_dir()
                 and i.filename.replace("\\", "/") == target),
                None,
            )
            if info is None:
                return None
            if limit_bytes > 0 and int(info.file_size or 0) > limit_bytes:
                logger.info(
                    f"[看图] 跳过超过体积上限的图片：{target}"
                    f"（{info.file_size} 字节 > {limit_bytes}）"
                )
                return None
            with archive.open(info) as handle:
                data = handle.read()
            if limit_bytes > 0 and len(data) > limit_bytes:
                return None
            return data
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        logger.debug(f"[看图] 读取日志包图片失败：{target}：{exc}")
        return None


def read_repo_image(root: Optional[Path], rel: str,
                    limit_bytes: int) -> Optional[bytes]:
    """从项目仓库读取一张图片（严格沙箱）；超限或不存在返回 None。"""
    if root is None:
        return None
    target = str(rel or "").replace("\\", "/").lstrip("/")
    if not target or is_sensitive_path(target):
        return None
    try:
        root_real = Path(root).resolve()
        resolved = (root_real / target).resolve()
        if resolved != root_real and root_real not in resolved.parents:
            return None
        if not resolved.is_file():
            return None
        if limit_bytes > 0 and resolved.stat().st_size > limit_bytes:
            logger.info(f"[看图] 跳过超过体积上限的图片：{target}")
            return None
        return resolved.read_bytes()
    except (OSError, RuntimeError) as exc:
        logger.debug(f"[看图] 读取仓库图片失败：{target}：{exc}")
        return None


def select_error_screenshots(digest: Any, vision: VisionConfig,
                             already: int = 0) -> list[tuple[str, bytes]]:
    """
    从摘要里的 on_error 截图清单挑出可直接注入的图片。

    返回 [(路径, 字节)]，最多 `max_images - already` 张。
    """
    if digest is None or not vision.enabled or not vision.attach_error_screenshots:
        return []
    zip_path = str(getattr(digest, "zip_path", "") or "")
    if not zip_path:
        return []
    room = max(0, int(vision.max_images or 0) - max(0, already))
    if room <= 0:
        return []
    limit = max_image_bytes(vision)
    picked: list[tuple[str, bytes]] = []
    for rel in (getattr(digest, "error_images", None) or []):
        if len(picked) >= room:
            break
        data = read_zip_image(zip_path, rel, limit)
        if data:
            picked.append((rel, data))
    return picked


def describe_skipped(paths: Iterable[str]) -> str:
    """把被跳过的图片路径整理成一行说明（用于文本降级）。"""
    items = [str(p) for p in paths if p]
    if not items:
        return ""
    return "未能读取截图：" + "、".join(items[:4])
