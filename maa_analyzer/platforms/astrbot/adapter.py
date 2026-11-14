"""AstrBot AIOCQHTTP implementation of the shared platform port.

This module intentionally uses duck typing rather than importing AstrBot. The
plugin shell owns framework-specific imports; the shared analyzer can therefore
be imported by the bare OneBot entry without AstrBot installed.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiohttp

from ...platform import (
    GroupFileMetadata,
    GroupMessageEvent,
    GroupUploadNotice,
    PlatformEvent,
    Sender,
)

logger = logging.getLogger("Maa.AstrBot")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _api_id(value: Any) -> int | str:
    raw = _text(value)
    return int(raw) if raw.isdigit() else raw


def _message_id(value: Any) -> str | None:
    """Extract an OneBot message ID from common AIOCQHTTP return shapes."""
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for child in value:
            found = _message_id(child)
            if found:
                return found
        return None
    if not isinstance(value, Mapping):
        for key in ("message_id", "messageId", "msg_id"):
            nested = getattr(value, key, None)
            if nested is not None:
                return str(nested)
        return None
    if not _api_success(value, allow_empty=True):
        return None
    for key, child in value.items():
        if key in {"message_id", "messageId", "msg_id"} and child is not None:
            return str(child)
        if key in {"data", "result"} and isinstance(
            child, (Mapping, list, tuple)
        ):
            found = _message_id(child)
            if found:
                return found
    return None


def _explicit_failure(value: Any) -> bool:
    """True only when a response carries a status or retcode saying it failed.

    AIOCQHTTP implementations differ in what ``call_action`` hands back: some
    return the whole envelope, some only its ``data`` field. For
    ``upload_group_file`` that ``data`` is usually ``null`` even on success, so a
    missing signal must not be read as a failure.
    """
    if isinstance(value, Mapping):
        status = _text(_get(value, "status")).lower()
        if status in {"failed", "error", "fail"}:
            return True
        retcode = _get(value, "retcode", _get(value, "code"))
        if retcode is not None:
            try:
                return int(retcode) != 0
            except (TypeError, ValueError):
                pass
    return False


def _api_success(value: Any, *, allow_empty: bool = False) -> bool:
    """Return whether a OneBot API response represents success."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, Mapping):
        if _explicit_failure(value):
            return False
        return allow_empty or bool(value)
    return True


def _response_mapping(value: Any) -> dict[str, Any]:
    """Unwrap common ``data``/``result`` envelopes from an API response."""
    if not isinstance(value, Mapping):
        return {}
    if not _api_success(value, allow_empty=True):
        return {}
    for key in ("data", "result"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return dict(value)


def _kind(component: Any) -> str:
    value = _get(component, "type", "")
    value = getattr(value, "value", value)
    return _text(value).lower()


def _component_segment(component: Any) -> dict[str, Any]:
    kind = _kind(component)
    if kind in {"plain", "text"}:
        return {
            "type": "text",
            "data": {"text": str(_get(component, "text", "") or "")},
        }
    if kind == "file":
        name = _text(
            _get(component, "name")
            or _get(component, "file_name")
            or _get(component, "filename")
        )
        file_value = _text(
            _get(component, "file_")
            or _get(component, "path")
        )
        url = _text(_get(component, "url", ""))
        return {
            "type": "file",
            "data": {
                "file_id": _text(
                    _get(component, "file_id") or _get(component, "id")
                ),
                "file_name": name,
                "name": name,
                "file": file_value or url,
                "file_": file_value,
                "url": url,
            },
        }
    if kind == "reply":
        return {
            "type": "reply",
            "data": {"id": _text(_get(component, "id", ""))},
        }
    if kind == "image":
        return {
            "type": "image",
            "data": {
                "file": _text(
                    _get(component, "file_") or _get(component, "file")
                ),
                "file_": _text(_get(component, "file_")),
                "url": _text(_get(component, "url", "")),
            },
        }
    to_dict = _get(component, "toDict")
    if callable(to_dict):
        try:
            converted = to_dict()
            if not inspect.isawaitable(converted) and isinstance(
                converted, Mapping
            ):
                return dict(converted)
        except Exception:
            pass
    data = _mapping(_get(component, "data"))
    for key in (
        "id",
        "qq",
        "user_id",
        "file",
        "file_",
        "file_id",
        "name",
        "file_name",
        "url",
        "size",
        "busid",
    ):
        value = _get(component, key)
        if value is not None and key not in data:
            data[key] = value
    return {"type": kind, "data": data}


class AstrBotPlatformAdapter:
    """Translate AstrBot AIOCQHTTP events and operations to ``PlatformPort``."""

    def __init__(
        self,
        bot: Any | None = None,
        outbox_dir: str | Path | None = None,
        *,
        role_cache_seconds: int = 300,
    ):
        self.bot = bot
        self.outbox_dir = Path(outbox_dir) if outbox_dir else None
        self.role_cache_seconds = max(0, int(role_cache_seconds or 0))
        self._role_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._event_route: ContextVar[tuple[str, Any, str] | None] = ContextVar(
            "astrbot_event_route", default=None
        )
        self._group_routes: dict[str, tuple[Any, str]] = {}

    def bind_bot(self, bot: Any | None) -> None:
        """Bind or refresh the AIOCQHTTP client used for outbound operations."""
        if bot is not None:
            self.bot = bot

    def clear_role_cache(self) -> None:
        self._role_cache.clear()

    @contextmanager
    def event_context(self, event: Any):
        """Keep the inbound connection/account through awaits and child tasks."""
        raw = self._raw_event(event)
        group_id = self.event_hints(event)["group_id"]
        bot = _get(event, "bot")
        if bot is None:
            bot = self.bot
        self_id = _text(
            _get(raw, "self_id") or _get(_get(event, "message_obj"), "self_id")
        )
        route = (group_id, bot, self_id)
        if group_id and bot is not None:
            # Timers created before an event have no event context of their own.
            self._group_routes[group_id] = (bot, self_id)
        token = self._event_route.set(route)
        try:
            yield
        finally:
            self._event_route.reset(token)

    def _action_route(self, params: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
        group_id = _text(params.get("group_id"))
        route = self._event_route.get()
        if route is not None and (not group_id or route[0] == group_id):
            _, bot, self_id = route
        else:
            bot, self_id = self._group_routes.get(group_id, (self.bot, ""))
        routed = dict(params)
        if self_id:
            routed["self_id"] = _api_id(self_id)
        return bot, routed

    @staticmethod
    def _raw_event(event: Any) -> Any:
        message_obj = _get(event, "message_obj")
        raw = _get(message_obj, "raw_message")
        return raw if raw is not None else message_obj

    @classmethod
    def is_group_upload(cls, event: Any) -> bool:
        raw = cls._raw_event(event)
        return (
            _text(_get(raw, "post_type")).lower() == "notice"
            and _text(_get(raw, "notice_type")).lower() == "group_upload"
        )

    @classmethod
    def is_group_message(cls, event: Any) -> bool:
        raw = cls._raw_event(event)
        post_type = _text(_get(raw, "post_type")).lower()
        if post_type == "notice":
            return False
        message_type = _text(_get(raw, "message_type")).lower()
        if message_type and message_type != "group":
            return False
        if post_type and post_type != "message":
            return False
        get_group_id = _get(event, "get_group_id")
        group_id = (
            get_group_id()
            if callable(get_group_id)
            else _get(event, "group_id")
        )
        return bool(_text(group_id) or _get(raw, "group_id"))

    @classmethod
    def event_hints(cls, event: Any) -> dict[str, Any]:
        """Extract routing fields without calling the member-info API."""
        raw = cls._raw_event(event)
        message_obj = _get(event, "message_obj")
        get_group_id = _get(event, "get_group_id")
        group_id = _text(
            _get(raw, "group_id")
            or (get_group_id() if callable(get_group_id) else "")
            or _get(message_obj, "group_id")
        )

        text_parts: list[str] = []
        file_names: list[str] = []
        reply_id = ""
        for segment in cls._segments(event, raw):
            kind = _text(_get(segment, "type")).lower()
            data = _get(segment, "data")
            if kind == "text":
                text_parts.append(str(_get(data, "text", "") or ""))
            elif kind in {"file", "fileupload"}:
                name = _text(
                    _get(data, "file_name")
                    or _get(data, "name")
                    or _get(data, "filename")
                    or _get(data, "file")
                    or _get(data, "file_")
                )
                if name:
                    file_names.append(name)
            elif kind == "reply":
                reply_id = _text(
                    _get(data, "id") or _get(data, "message_id")
                )

        text = "".join(text_parts).strip()
        if not text:
            text = _text(_get(event, "message_str"))
        return {
            "group_id": group_id,
            "text": text,
            "file_names": file_names,
            "reply_id": reply_id,
        }

    @classmethod
    def _segments(cls, event: Any, raw: Any) -> list[dict[str, Any]]:
        raw_message = _get(raw, "message")
        if isinstance(raw_message, Sequence) and not isinstance(
            raw_message, (str, bytes, bytearray)
        ):
            segments = []
            for segment in raw_message:
                if isinstance(segment, Mapping):
                    item = dict(segment)
                else:
                    item = _component_segment(segment)
                data = item.get("data")
                if isinstance(data, Mapping):
                    item["data"] = dict(data)
                segments.append(item)
            if segments:
                return segments

        get_messages = _get(event, "get_messages")
        if callable(get_messages):
            messages = get_messages() or []
            return [_component_segment(item) for item in messages]
        return []

    @staticmethod
    def _file_metadata(data: Any) -> GroupFileMetadata:
        return GroupFileMetadata(
            file_id=_text(
                _get(data, "file_id")
                or _get(data, "id")
                or _get(data, "fileId")
            ),
            name=_text(
                _get(data, "file_name")
                or _get(data, "name")
                or _get(data, "filename")
                or _get(data, "file")
                or _get(data, "file_")
            ),
            size=_integer(
                _get(data, "size")
                or _get(data, "file_size")
                or _get(data, "fileSize")
            ),
            busid=_integer(_get(data, "busid")),
            url=_text(
                _get(data, "url")
                or _get(data, "download_url")
                or _get(data, "downloadUrl")
            ),
        )

    async def _resolve_role(
        self,
        event: Any,
        raw: Any,
        group_id: str,
        user_id: str,
    ) -> str:
        raw_sender = _get(raw, "sender")
        message_obj = _get(event, "message_obj")
        obj_sender = _get(message_obj, "sender")
        for sender in (raw_sender, obj_sender):
            role = _text(_get(sender, "role")).lower()
            if role:
                return role

        key = (group_id, user_id)
        cached = self._role_cache.get(key)
        if cached is not None:
            role, expires_at = cached
            if expires_at > time.monotonic():
                return role
            self._role_cache.pop(key, None)

        if not group_id or not user_id:
            return ""
        try:
            info = await self.get_group_member_info(
                _api_id(group_id), _api_id(user_id)
            )
        except Exception as exc:
            logger.debug(
                "读取群成员角色失败 group=%s user=%s: %s",
                group_id,
                user_id,
                exc,
            )
            return ""
        info = _response_mapping(info)
        role = _text(
            _get(info, "role")
            or _get(_get(info, "member"), "role")
        ).lower()
        if role and self.role_cache_seconds > 0:
            self._role_cache[key] = (
                role,
                time.monotonic() + self.role_cache_seconds,
            )
        return role

    async def map_group_upload(self, event: Any) -> GroupUploadNotice | None:
        raw = self._raw_event(event)
        if not self.is_group_upload(event):
            return None
        message_obj = _get(event, "message_obj")
        get_group_id = _get(event, "get_group_id")
        group_id = _text(
            _get(raw, "group_id")
            or (get_group_id() if callable(get_group_id) else "")
            or _get(message_obj, "group_id")
        )
        user_id = _text(
            _get(raw, "user_id")
            or _get(_get(message_obj, "sender"), "user_id")
        )
        file_data = _get(raw, "file")
        if not isinstance(file_data, Mapping):
            file_data = raw
        notice = GroupUploadNotice(
            group_id=group_id,
            user_id=user_id,
            file=self._file_metadata(file_data),
            raw_event=raw,
        )
        if not notice.group_id or not notice.file.name:
            return None
        return notice

    async def map_group_message(
        self, event: Any
    ) -> GroupMessageEvent | None:
        raw = self._raw_event(event)
        if not self.is_group_message(event):
            return None
        message_obj = _get(event, "message_obj")
        get_group_id = _get(event, "get_group_id")
        get_sender_id = _get(event, "get_sender_id")
        raw_sender = _get(raw, "sender")
        obj_sender = _get(message_obj, "sender")
        group_id = _text(
            _get(raw, "group_id")
            or (get_group_id() if callable(get_group_id) else "")
            or _get(message_obj, "group_id")
        )
        user_id = _text(
            _get(raw, "user_id")
            or _get(raw_sender, "user_id")
            or (get_sender_id() if callable(get_sender_id) else "")
            or _get(obj_sender, "user_id")
        )
        if not group_id or not user_id:
            return None

        segments = self._segments(event, raw)
        text_parts: list[str] = []
        file_segments: list[GroupFileMetadata] = []
        reply_id = ""
        for segment in segments:
            kind = _text(_get(segment, "type")).lower()
            data = _get(segment, "data")
            if kind == "text":
                text_parts.append(str(_get(data, "text", "") or ""))
            elif kind in {"file", "fileupload"}:
                metadata = self._file_metadata(data)
                if metadata.name:
                    file_segments.append(metadata)
            elif kind == "reply":
                reply_id = _text(
                    _get(data, "id") or _get(data, "message_id")
                )

        text = "".join(text_parts).strip()
        if not text:
            message_str = _get(event, "message_str")
            text = _text(message_str)
        role = await self._resolve_role(event, raw, group_id, user_id)
        return GroupMessageEvent(
            group_id=group_id,
            user_id=user_id,
            message_id=_text(
                _get(raw, "message_id") or _get(message_obj, "message_id")
            ),
            text=text,
            segments=segments,
            raw_message=_text(_get(raw, "raw_message")),
            sender=Sender(
                user_id=user_id,
                role=role,
                nickname=_text(_get(raw_sender, "nickname") or
                               _get(obj_sender, "nickname")),
                card=_text(_get(raw_sender, "card")),
            ),
            reply_id=reply_id,
            file_segments=file_segments,
            raw_event=raw,
        )

    async def map_event(self, event: Any) -> PlatformEvent | None:
        if self.is_group_upload(event):
            return await self.map_group_upload(event)
        return await self.map_group_message(event)

    async def _call_action_result(
        self, action: str, *, route_group_id: int | None = None, **params: Any
    ) -> tuple[bool, Any]:
        """Call a bot action and report whether the call itself succeeded.

        The flag is False only when no bot is bound or the call raised. A
        successful call can still carry a ``None`` payload, and the value is
        whatever the implementation handed back.
        """
        route_params = dict(params)
        if route_group_id is not None:
            route_params["group_id"] = route_group_id
        bot, routed = self._action_route(route_params)
        if "self_id" in routed:
            params["self_id"] = routed["self_id"]
        if bot is None:
            return False, None
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            try:
                return True, await call_action(action, **params)
            except Exception as exc:
                logger.warning(
                    "[AstrBot] %s 调用失败 group=%s self_id=%s：%s",
                    action, params.get("group_id"), params.get("self_id"), exc,
                )
                return False, None
        api = getattr(bot, "api", None)
        api_call = getattr(api, "call_action", None)
        if callable(api_call):
            try:
                return True, await api_call(action, **params)
            except Exception as exc:
                logger.warning(
                    "[AstrBot] %s 调用失败 group=%s self_id=%s：%s",
                    action, params.get("group_id"), params.get("self_id"), exc,
                )
        return False, None

    async def _call_action(self, action: str, **params: Any) -> Any:
        _, result = await self._call_action_result(action, **params)
        return result

    async def _send_group(
        self, group_id: int, message: Sequence[Mapping[str, Any]]
    ) -> Any:
        bot, params = self._action_route({
            "group_id": _api_id(group_id), "message": list(message),
        })
        if bot is None:
            return None
        send_group = getattr(bot, "send_group_msg", None)
        if callable(send_group):
            try:
                return await send_group(**params)
            except Exception as exc:
                logger.warning(
                    "[AstrBot] send_group_msg 失败 group=%s self_id=%s：%s",
                    group_id, params.get("self_id"), exc,
                )
                # AIOCQHTTP's dynamic send_group_msg is already call_action.
                # Retrying a rejected/uncertain send can duplicate the message.
                return None
        return await self._call_action(
            "send_group_msg",
            group_id=_api_id(group_id),
            message=list(message),
        )

    async def send_text(self, group_id: int, text: str) -> str | None:
        if not text:
            return None
        return await self.send_message(
            group_id, [{"type": "text", "data": {"text": text}}]
        )

    async def send_message(
        self, group_id: int, message: Sequence[Mapping[str, Any]]
    ) -> str | None:
        if not message:
            return None
        return _message_id(await self._send_group(group_id, message))

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
        _, route = self._action_route({"group_id": _api_id(group_id)})
        user_id = route.get("self_id")
        name = "Maa 日志分析"
        if not user_id:
            _, result = await self._call_action_result(
                "get_login_info", route_group_id=group_id
            )
            info = result.get("data", result) if isinstance(result, Mapping) else None
            if isinstance(info, Mapping):
                user_id = info.get("user_id")
                name = str(info.get("nickname") or name)
        if not user_id:
            logger.warning("[AstrBot] 无法获取机器人账号，取消发送合并转发")
            return None
        nodes = [{"type": "node", "data": {
            "uin": str(user_id), "name": name, "content": list(message),
        }} for message in messages]
        return _message_id(await self._call_action(
            "send_group_forward_msg", group_id=_api_id(group_id), messages=nodes,
        ))

    async def upload_group_file(
        self, group_id: int, file: str, name: str
    ) -> bool:
        called, result = await self._call_action_result(
            "upload_group_file",
            group_id=_api_id(group_id),
            file=file,
            name=name,
        )
        if not called:
            return False
        # 成功时 data 常常是 null；只在响应明确报失败时才判失败，否则
        # send_file 会再用 base64 传一遍，群里会出现重复文件。
        return not _explicit_failure(result)

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
                logger.warning("[AstrBot] 写临时文件失败，改用 base64：%s", exc)
                target = None

        if target is not None:
            try:
                if await self.upload_group_file(
                    group_id, str(target.resolve()), name
                ):
                    self._sweep_outbox()
                    return True
            except Exception as exc:
                logger.warning(
                    "[AstrBot] 本地文件上传失败，改用 base64：%s", exc
                )

        try:
            return await self.upload_group_file(
                group_id,
                f"base64://{base64.b64encode(data).decode()}",
                name,
            )
        except Exception as exc:
            logger.warning("[AstrBot] 文件上传失败（%s）：%s", name, exc)
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
        params: dict[str, Any] = {"group_id": _api_id(group_id)}
        if file_count is not None:
            params["file_count"] = int(file_count)
        result = await self._call_action("get_group_root_files", **params)
        return _response_mapping(result)

    async def get_group_files_by_folder(
        self, group_id: int, folder_id: str, file_count: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "group_id": _api_id(group_id),
            "folder_id": folder_id,
        }
        if file_count is not None:
            params["file_count"] = int(file_count)
        result = await self._call_action(
            "get_group_files_by_folder", **params
        )
        return _response_mapping(result)

    async def get_group_file_url(
        self, group_id: int, file_id: str
    ) -> str | None:
        result = await self._call_action(
            "get_group_file_url",
            group_id=_api_id(group_id),
            file_id=file_id,
        )
        if isinstance(result, str):
            return result or None
        data = _response_mapping(result)
        return _text(
            _get(data, "url") or _get(data, "download_url")
        ) or None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def download_url(
        self,
        url: str,
        target_path: str | Path,
        *,
        max_bytes: int,
        timeout_seconds: int = 120,
    ) -> int:
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        timeout = aiohttp.ClientTimeout(
            total=max(10, int(timeout_seconds or 120))
        )
        session = await self._get_session()
        temp_path: Path | None = None
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"下载失败，HTTP 状态码：{response.status}"
                    )
                content_length = _integer(response.headers.get("Content-Length"))
                if max_bytes > 0 and content_length > max_bytes:
                    raise RuntimeError(
                        f"文件超过大小限制（{max_bytes // 1024 // 1024} MB），"
                        "已停止下载。"
                    )
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{path.name}.",
                    suffix=".part",
                    dir=path.parent,
                    delete=False,
                ) as file:
                    temp_path = Path(file.name)
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if max_bytes > 0 and downloaded > max_bytes:
                            raise RuntimeError(
                                f"文件超过大小限制"
                                f"（{max_bytes // 1024 // 1024} MB），"
                                "已停止下载。"
                            )
                        file.write(chunk)
            if temp_path is None:
                raise RuntimeError("下载未产生临时文件。")
            os.replace(temp_path, path)
            temp_path = None
            return downloaded
        except BaseException:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    async def get_version_info(self) -> dict[str, Any]:
        result = await self._call_action("get_version_info")
        return _response_mapping(result)

    async def get_group_member_info(
        self, group_id: int | str, user_id: int | str
    ) -> dict[str, Any]:
        result = await self._call_action(
            "get_group_member_info",
            group_id=_api_id(group_id),
            user_id=_api_id(user_id),
            no_cache=False,
        )
        return _response_mapping(result)

    async def close(self) -> None:
        self._group_routes.clear()
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()
