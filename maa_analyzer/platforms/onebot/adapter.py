"""OneBot v11 implementation of :class:`maa_analyzer.platform.PlatformPort`."""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...platform import (
    GroupFileMetadata,
    GroupMessageEvent,
    GroupUploadNotice,
    Sender,
)

from .api import OneBotAPI

logger = logging.getLogger("Maa.OneBot")


def _message_id(data: Any) -> str | None:
    if isinstance(data, Mapping):
        value = data.get("message_id") or data.get("messageId")
        if value is not None:
            return str(value)
    if isinstance(data, (str, int)):
        return str(data)
    return None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class OneBotPlatformAdapter:
    """Adapter from OneBot API/event shapes to the shared platform contract."""

    def __init__(self, api: OneBotAPI, outbox_dir: str | Path | None = None):
        self.api = api
        self.outbox_dir = Path(outbox_dir) if outbox_dir else None

    async def send_text(self, group_id: int, text: str) -> str | None:
        data = await self.api.send_group_msg(group_id, text)
        return _message_id(data)

    async def send_message(
        self, group_id: int, message: Sequence[Mapping[str, Any]]
    ) -> str | None:
        data = await self.api.send_group_msg(group_id, list(message))
        return _message_id(data)

    async def send_image(self, group_id: int, image_bytes: bytes) -> str | None:
        encoded = base64.b64encode(image_bytes).decode()
        return await self.send_message(group_id, [{
            "type": "image",
            "data": {"file": f"base64://{encoded}"},
        }])

    async def send_forward_messages(
        self, group_id: int, messages: Sequence[Sequence[Mapping[str, Any]]]
    ) -> str | None:
        if not messages:
            return None
        info = await self.api.get_login_info()
        if not info or not info.get("user_id"):
            logger.warning("[发送] 无法获取机器人账号，取消发送合并转发")
            return None
        nodes = [{"type": "node", "data": {
            "uin": str(info["user_id"]),
            "name": str(info.get("nickname") or "Maa 日志分析"),
            "content": list(message),
        }} for message in messages]
        return _message_id(await self.api.send_group_forward_msg(group_id, nodes))

    async def upload_group_file(
        self, group_id: int, file: str, name: str
    ) -> bool:
        result = await self.api.upload_group_file(group_id, file, name)
        return result is not None

    async def send_file(
        self, group_id: int, file_name: str, data: bytes
    ) -> bool:
        name = Path(str(file_name or "file.bin")).name or "file.bin"
        target: Path | None = None
        if self.outbox_dir is not None:
            try:
                self.outbox_dir.mkdir(parents=True, exist_ok=True)
                target = self.outbox_dir / f"{int(time.time() * 1000)}_{name}"
                target.write_bytes(data)
            except OSError as exc:
                logger.warning(f"[发送] 写临时文件失败，改用 base64：{exc}")
                target = None

        if target is not None:
            try:
                if await self.upload_group_file(
                    group_id, str(target.resolve()), name
                ):
                    self._sweep_outbox()
                    return True
                logger.warning("[发送] upload_group_file 返回空，尝试 base64")
            except Exception as exc:
                logger.warning(
                    f"[发送] upload_group_file 失败，尝试 base64：{exc}"
                )

        try:
            return await self.upload_group_file(
                group_id,
                f"base64://{base64.b64encode(data).decode()}",
                name,
            )
        except Exception as exc:
            logger.warning(f"[发送] 发送文件失败（{name}）：{exc}")
            return False

    def _sweep_outbox(self, keep_seconds: int = 600) -> None:
        if self.outbox_dir is None:
            return
        try:
            cutoff = time.time() - max(60, int(keep_seconds))
            for item in self.outbox_dir.iterdir():
                try:
                    if item.is_file() and item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    async def get_group_root_files(
        self, group_id: int, file_count: int | None = None
    ) -> dict[str, Any]:
        if file_count is None:
            result = await self.api.get_group_root_files(group_id)
        else:
            result = await self.api.get_group_root_files(
                group_id, file_count=file_count
            )
        return result if isinstance(result, dict) else {}

    async def get_group_files_by_folder(
        self, group_id: int, folder_id: str, file_count: int | None = None
    ) -> dict[str, Any]:
        if file_count is None:
            result = await self.api.get_group_files_by_folder(group_id, folder_id)
        else:
            result = await self.api.get_group_files_by_folder(
                group_id, folder_id, file_count=file_count
            )
        return result if isinstance(result, dict) else {}

    async def get_group_file_url(
        self, group_id: int, file_id: str
    ) -> str | None:
        return await self.api.get_group_file_url(group_id, file_id)

    async def download_url(
        self,
        url: str,
        target_path: str | Path,
        *,
        max_bytes: int,
        timeout_seconds: int = 120,
    ) -> int:
        return await self.api.download_url(
            url,
            target_path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )

    async def get_version_info(self) -> dict[str, Any]:
        result = await self.api.get_version_info()
        return result if isinstance(result, dict) else {}

    async def get_group_member_info(
        self, group_id: int, user_id: int
    ) -> dict[str, Any]:
        result = await self.api.get_group_member_info(group_id, user_id)
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        await self.api.close()

    @staticmethod
    def map_message(event: Mapping[str, Any]) -> GroupMessageEvent:
        segments = [
            dict(segment)
            for segment in event.get("message") or []
            if isinstance(segment, Mapping)
        ]
        file_segments: list[GroupFileMetadata] = []
        reply_id = ""
        text_parts: list[str] = []
        for segment in segments:
            kind = str(segment.get("type") or "")
            data = segment.get("data")
            data = dict(data) if isinstance(data, Mapping) else {}
            if kind == "text":
                text_parts.append(str(data.get("text") or ""))
            elif kind == "file":
                file_segments.append(GroupFileMetadata(
                    file_id=str(data.get("file_id") or data.get("id") or ""),
                    name=str(
                        data.get("file_name")
                        or data.get("name")
                        or data.get("file")
                        or ""
                    ),
                    size=_integer(data.get("size")),
                    busid=_integer(data.get("busid")),
                    url=str(data.get("url") or ""),
                ))
            elif kind == "reply":
                reply_id = str(
                    data.get("id") or data.get("message_id") or ""
                )
        sender_data = event.get("sender")
        sender_data = sender_data if isinstance(sender_data, Mapping) else {}
        user_id = str(event.get("user_id") or sender_data.get("user_id") or "")
        return GroupMessageEvent(
            group_id=str(event.get("group_id") or ""),
            user_id=user_id,
            message_id=str(event.get("message_id") or ""),
            text=(
                "".join(text_parts).strip()
                or str(event.get("raw_message") or "").strip()
            ),
            segments=segments,
            raw_message=str(event.get("raw_message") or ""),
            sender=Sender(
                user_id=user_id,
                role=str(sender_data.get("role") or "").lower(),
                nickname=str(sender_data.get("nickname") or ""),
                card=str(sender_data.get("card") or ""),
            ),
            reply_id=reply_id,
            file_segments=file_segments,
            raw_event=event,
        )

    @staticmethod
    def map_notice(event: Mapping[str, Any]) -> GroupUploadNotice:
        file_data = event.get("file")
        file_data = file_data if isinstance(file_data, Mapping) else event
        return GroupUploadNotice(
            group_id=str(event.get("group_id") or ""),
            user_id=str(event.get("user_id") or ""),
            file=GroupFileMetadata(
                file_id=str(
                    file_data.get("id")
                    or file_data.get("file_id")
                    or ""
                ),
                name=str(
                    file_data.get("name")
                    or file_data.get("file_name")
                    or file_data.get("file")
                    or ""
                ),
                size=_integer(file_data.get("size")),
                busid=_integer(file_data.get("busid")),
                url=str(file_data.get("url") or ""),
            ),
            raw_event=event,
        )
