"""
OneBot v11 API 封装 — 支持 HTTP 和 WebSocket 两种传输。

HTTP 模式: POST http://host:port/{action}
WS 模式:   通过 WebSocket 发送 {"action":..., "params":..., "echo":...}

参考: https://github.com/botuniverse/onebot-11/blob/master/api/public.md
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger("Maa.API")

# WS 发送回调: (json_str) -> None
WSSend = Callable[[str], Awaitable[None]]


class OneBotAPI:
    """OneBot v11 API 调用 — HTTP + 可选 WS。"""

    def __init__(self, http_url: str = "", access_token: str = "",
                 *, timeout_seconds: int = 30):
        self.http_url = http_url.rstrip("/") if http_url else ""
        self.access_token = access_token
        self.timeout_seconds = max(5, int(timeout_seconds or 30))
        self._ws_send: Optional[WSSend] = None
        self._ws_responses: dict[str, asyncio.Future] = {}
        self._echo_counter = 0
        self._session: Optional[aiohttp.ClientSession] = None

    def set_ws_send(self, send: WSSend):
        """注入 WS 发送通道。设置后，无 HTTP 时 API 走 WS。"""
        self._ws_send = send

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ==================== 传输层 ====================

    async def _call(self, action: str, params: dict) -> Optional[Any]:
        """调用 OneBot action，优先 HTTP，无 HTTP 则走 WS。"""
        if self.http_url:
            return await self._call_http(action, params)
        if self._ws_send:
            return await self._call_ws(action, params)
        logger.error("[API] 无可用传输 (http_url 和 ws 均未配置)")
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        return self._session

    async def _call_http(self, action: str, params: dict) -> Optional[Any]:
        url = f"{self.http_url}/{action}"
        payload = {"action": action, "params": params}
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"[API] {action} HTTP {resp.status}")
                    return None
                result = await resp.json(content_type=None)
                status = result.get("status") if isinstance(result, dict) else None
                if status == "ok":
                    data = result.get("data")
                    return data if data is not None else True
                if status == "async":
                    logger.info(f"[API] {action} 异步调用已接受")
                    return True
                logger.warning(f"[API] {action} 失败: {result}")
                return None
        except aiohttp.ClientError as exc:
            logger.error(f"[API] {action} 网络错误: {exc}")
            return None
        except asyncio.TimeoutError:
            logger.error(f"[API] {action} 超时")
            return None
        except Exception as exc:
            logger.error(f"[API] {action} 未知错误: {exc}")
            return None

    async def _call_ws(self, action: str, params: dict) -> Optional[Any]:
        if not self._ws_send:
            return None
        self._echo_counter += 1
        echo = str(self._echo_counter)
        payload = {"action": action, "params": params, "echo": echo}

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ws_responses[echo] = future

        try:
            await self._ws_send(json.dumps(payload, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=self.timeout_seconds)
            if result.get("status") == "ok":
                data = result.get("data")
                return data if data is not None else True
            logger.warning(f"[API-WS] {action} 失败: {result}")
            return None
        except asyncio.TimeoutError:
            logger.error(f"[API-WS] {action} 超时")
            return None
        finally:
            self._ws_responses.pop(echo, None)

    def handle_ws_response(self, msg: dict):
        """处理 WS 上的 API 响应。"""
        echo = msg.get("echo")
        if echo is None:
            return
        future = self._ws_responses.pop(str(echo), None)
        if future and not future.done():
            future.set_result(msg)

    # ==================== 消息发送 ====================

    async def send_group_msg(self, group_id: int, message) -> Optional[int]:
        """发送群聊消息。message 为消息段数组（list[dict]）或纯文本。"""
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        data = await self._call("send_group_msg", {
            "group_id": group_id, "message": message,
        })
        return data.get("message_id") if isinstance(data, dict) else None

    # ==================== 群文件 ====================

    async def get_group_root_files(self, group_id: int,
                                   file_count: int = 2000) -> Optional[dict]:
        """获取群根目录文件列表。"""
        data = await self._call("get_group_root_files", {
            "group_id": group_id, "file_count": file_count,
        })
        return data if isinstance(data, dict) else None

    async def get_group_files_by_folder(self, group_id: int, folder_id: str,
                                        file_count: int = 2000) -> Optional[dict]:
        """获取群子目录文件列表。"""
        data = await self._call("get_group_files_by_folder", {
            "group_id": group_id, "folder_id": folder_id, "file_count": file_count,
        })
        return data if isinstance(data, dict) else None

    async def get_group_file_url(self, group_id: int, file_id: str) -> Optional[str]:
        """获取群文件下载链接。"""
        data = await self._call("get_group_file_url", {
            "group_id": group_id, "file_id": file_id,
        })
        if isinstance(data, dict):
            url = data.get("url")
            return url if isinstance(url, str) and url else None
        return None

    async def get_group_file_system_info(self, group_id: int) -> Optional[dict]:
        """获取群文件系统信息（部分实现提供）。"""
        data = await self._call("get_group_file_system_info", {"group_id": group_id})
        return data if isinstance(data, dict) else None

    async def upload_group_file(self, group_id: int, file: str, name: str,
                                folder: str = "") -> Optional[Any]:
        """
        上传文件到群文件（OneBot v11 标准接口）。

        file 支持本地绝对路径或 base64:// / http(s):// 形式。
        部分实现（如 NapCat）会异步返回，不保证立刻可下载。
        """
        return await self._call("upload_group_file", {
            "group_id": group_id, "file": file, "name": name, "folder": folder,
        })

    async def download_url(self, url: str, target_path, *,
                           max_bytes: int, timeout_seconds: int = 120) -> int:
        """
        下载任意 URL 到本地文件，返回字节数。

        群文件下载链接通常为临时 CDN 地址，不带 OneBot 鉴权头，
        因此这里独立发起 HTTP 请求。超过 max_bytes 时删除文件并抛错。
        """
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        timeout = aiohttp.ClientTimeout(total=max(10, int(timeout_seconds or 120)))

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"下载失败，HTTP 状态码：{response.status}")
                with path.open("wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 64):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            try:
                                file.close()
                                os.remove(path)
                            except OSError:
                                pass
                            raise RuntimeError(
                                f"文件超过大小限制（{max_bytes // 1024 // 1024} MB），已停止下载。"
                            )
                        file.write(chunk)
        return downloaded

    # ==================== 基础信息 ====================

    async def get_login_info(self) -> Optional[dict]:
        data = await self._call("get_login_info", {})
        return data if isinstance(data, dict) else None

    async def get_version_info(self) -> Optional[dict]:
        """获取协议端版本信息，用于探测实现（LLOneBot / NapCat 等）。"""
        data = await self._call("get_version_info", {})
        return data if isinstance(data, dict) else None

    async def get_group_member_list(self, group_id: int) -> Optional[list[dict]]:
        data = await self._call("get_group_member_list", {"group_id": group_id})
        return data if isinstance(data, list) else None

    async def get_group_member_info(self, group_id: int, user_id: int) -> Optional[dict]:
        data = await self._call("get_group_member_info", {
            "group_id": group_id, "user_id": user_id,
        })
        return data if isinstance(data, dict) else None
