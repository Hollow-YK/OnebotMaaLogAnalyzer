"""Platform-neutral analyzer ports and event DTOs.

The analyzer kernel only imports this module. Concrete connectors live under
``maa_analyzer.platforms`` and translate their native events into these DTOs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(slots=True)
class Sender:
    """Group message sender with the role used by command authorization."""

    user_id: str
    role: str = ""
    nickname: str = ""
    card: str = ""


@dataclass(slots=True)
class GroupFileMetadata:
    """A group file as exposed by the platform."""

    file_id: str = ""
    name: str = ""
    size: int = 0
    busid: int = 0
    url: str = ""

    def to_legacy_dict(self) -> dict[str, Any]:
        """Translate to the field names used by existing OneBot adapters."""
        result: dict[str, Any] = {
            "id": self.file_id,
            "file_id": self.file_id,
            "name": self.name,
            "file_name": self.name,
            "size": self.size,
        }
        if self.busid:
            result["busid"] = self.busid
        if self.url:
            result["url"] = self.url
        return result


@dataclass(slots=True)
class GroupMessageEvent:
    """Normalized group message event."""

    group_id: str
    user_id: str
    message_id: str = ""
    text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    raw_message: str = ""
    sender: Sender = field(default_factory=lambda: Sender(user_id=""))
    reply_id: str = ""
    file_segments: list[GroupFileMetadata] = field(default_factory=list)
    raw_event: Any = None

    def to_service_payload(self) -> dict[str, Any]:
        """Build the compatibility payload consumed by the shared features."""
        result: dict[str, Any] = {
            "post_type": "message",
            "message_type": "group",
            "group_id": self.group_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "message": list(self.segments),
            "raw_message": self.raw_message,
            "sender": {
                "user_id": self.sender.user_id or self.user_id,
                "role": self.sender.role,
                "nickname": self.sender.nickname,
                "card": self.sender.card,
            },
            "_text": self.text,
            "_reply_id": self.reply_id,
            "_file_segments": [
                item.to_legacy_dict() for item in self.file_segments
            ],
        }
        return result


@dataclass(slots=True)
class GroupUploadNotice:
    """Normalized group file upload notice."""

    group_id: str
    user_id: str
    file: GroupFileMetadata
    raw_event: Any = None

    def to_service_payload(self) -> dict[str, Any]:
        return {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": self.group_id,
            "user_id": self.user_id,
            "file": self.file.to_legacy_dict(),
        }


PlatformEvent = GroupMessageEvent | GroupUploadNotice


@runtime_checkable
class PlatformPort(Protocol):
    """Messaging and file operations required by the shared analyzer."""

    async def send_text(self, group_id: int, text: str) -> str | None:
        ...

    async def send_message(
        self, group_id: int, message: Sequence[Mapping[str, Any]]
    ) -> str | None:
        ...

    async def send_image(self, group_id: int, image_bytes: bytes) -> str | None:
        ...

    async def send_forward_messages(
        self, group_id: int, messages: Sequence[Sequence[Mapping[str, Any]]]
    ) -> str | None:
        """Send message bodies inside one QQ merged-forward card."""
        ...

    async def send_file(
        self, group_id: int, file_name: str, data: bytes
    ) -> bool:
        ...

    async def upload_group_file(
        self, group_id: int, file: str, name: str
    ) -> bool:
        ...

    async def get_group_root_files(
        self, group_id: int, file_count: int | None = None
    ) -> dict[str, Any]:
        ...

    async def get_group_files_by_folder(
        self, group_id: int, folder_id: str, file_count: int | None = None
    ) -> dict[str, Any]:
        ...

    async def get_group_file_url(
        self, group_id: int, file_id: str
    ) -> str | None:
        ...

    async def download_url(
        self,
        url: str,
        target_path: str | Path,
        *,
        max_bytes: int,
        timeout_seconds: int = 120,
    ) -> int:
        ...

    async def get_version_info(self) -> dict[str, Any]:
        ...

    async def get_group_member_info(
        self, group_id: int, user_id: int
    ) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...
