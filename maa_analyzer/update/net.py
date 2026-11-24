"""Minimal async HTTP client used by the release checker and installer."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

import aiohttp

logger = logging.getLogger("Maa.Update.Net")

USER_AGENT = "OnebotMaaLogAnalyzer-Updater/1"


class HttpError(RuntimeError):
    """A request failed, timed out or exceeded its size limit."""


class HttpClient:
    """One aiohttp session shared by a single update transaction."""

    def __init__(
        self,
        *,
        token: str = "",
        timeout_seconds: int = 30,
        max_download_bytes: int = 256 * 1024 * 1024,
    ):
        self.token = str(token or "")
        self.timeout_seconds = max(5, int(timeout_seconds or 30))
        self.max_download_bytes = max(1, int(max_download_bytes))
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        return self._session

    def _headers(
        self,
        extra: Mapping[str, str] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json, application/octet-stream",
        }
        # Never leak the API token to download mirrors or raw.githubusercontent.
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    async def get_json(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> tuple[int, Any, dict[str, str]]:
        """Return ``(status, decoded-json, response-headers)``."""
        session = await self._get_session()
        try:
            async with session.get(url, headers=self._headers(headers)) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if response.status == 304:
                    return 304, None, response_headers
                if response.status != 200:
                    body = (await response.read())[:500].decode("utf-8", "replace")
                    raise HttpError(
                        f"HTTP {response.status}：{body or response.reason}"
                    )
                raw = await response.read()
                try:
                    return 200, json.loads(raw.decode("utf-8")), response_headers
                except (UnicodeDecodeError, ValueError) as exc:
                    raise HttpError("响应不是合法 JSON") from exc
        except asyncio.TimeoutError as exc:
            raise HttpError("请求超时") from exc
        except aiohttp.ClientError as exc:
            raise HttpError(f"网络错误：{exc}") from exc

    async def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: int = 600,
    ) -> bytes:
        """Download at most ``max_bytes`` bytes."""
        limit = self.max_download_bytes if max_bytes is None else max(1, int(max_bytes))
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=max(10, int(timeout_seconds)))
        try:
            async with session.get(
                url,
                headers=self._headers(headers, auth=False),
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    body = (await response.read())[:300].decode("utf-8", "replace")
                    raise HttpError(
                        f"HTTP {response.status}：{body or response.reason}"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > limit:
                        raise HttpError(
                            f"文件超过大小限制（{declared} > {limit} 字节）"
                        )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HttpError(f"文件超过大小限制（{limit} 字节）")
                    chunks.append(chunk)
                return b"".join(chunks)
        except asyncio.TimeoutError as exc:
            raise HttpError("下载超时") from exc
        except aiohttp.ClientError as exc:
            raise HttpError(f"网络错误：{exc}") from exc

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


__all__ = ["HttpClient", "HttpError", "USER_AGENT"]
