"""
OneBot v11 WebSocket — 正向 / 反向 / Universal。

正向 WS:  OneBot 监听 → Bot connect() 过去
反向 WS:  Bot 监听   → OneBot 连过来
Universal: / 路径，API + 事件共线

消息类型判别:
  post_type 存在   → 事件
  action 存在      → API 调用（反向模式时 OneBot 发来）
  echo + status   → API 响应（回复我们发出的 API 调用）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("Maa.WS")

EventHandler = Callable[[dict], Awaitable[None]]
APIHandler = Callable[[dict], Awaitable[Optional[dict]]]
APIResponseHandler = Callable[[dict], None]


class OneBotWS:
    """OneBot v11 WebSocket。"""

    def __init__(self, access_token: str = ""):
        self.access_token = access_token
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._api_handler: Optional[APIHandler] = None
        self._api_response_handler: Optional[APIResponseHandler] = None
        self._running = False
        self._ws: Optional[object] = None
        self._server = None
        self._connection_count = 0

    def on_event(self, post_type: str, h: EventHandler):
        self._event_handlers.setdefault(post_type, []).append(h)

    def on_api(self, h: APIHandler):
        """WS 上的 API 调用（反向 / Universal 模式）。"""
        self._api_handler = h

    def on_api_response(self, h: APIResponseHandler):
        """WS 上的 API 响应（我们发出调用后 OneBot 回复）。"""
        self._api_response_handler = h

    async def send(self, raw: str):
        """通过当前 WS 连接发送数据（API 调用等）。"""
        ws = self._ws
        if ws is not None:
            await ws.send(raw)

    # ==================== 分发 ====================

    async def _dispatch_event(self, event: dict):
        pt = event.get("post_type", "")
        for h in self._event_handlers.get(pt, []):
            try:
                await h(event)
            except Exception:
                logger.exception(f"事件异常 (post_type={pt})")
        for h in self._event_handlers.get("*", []):
            try:
                await h(event)
            except Exception:
                logger.exception("通配符事件异常")

    # ==================== 正向 WS ====================

    async def connect(self, ws_url: str):
        """正向 WS — Bot 连接 OneBot。"""
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        self._running = True
        while self._running:
            try:
                logger.info(f"[WS→] 连接 {ws_url} ...")
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20, ping_timeout=10,
                    max_size=64 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    logger.info("[WS→] 已连接")
                    await self._handle_ws(ws)
            except websockets.ConnectionClosed as exc:
                logger.warning(f"[WS→] 断开: {exc}")
            except OSError as exc:
                logger.error(f"[WS→] 网络错误: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WS→] 异常")
            finally:
                self._ws = None
            if self._running:
                logger.info("[WS→] 5 秒后重连...")
                await asyncio.sleep(5)

    # ==================== 反向 WS ====================

    async def serve(self, host: str, port: int):
        """反向 WS — Bot 监听，OneBot 连接过来。"""
        logger.info(f"[WS←] 监听 {host}:{port} ...")
        self._running = True

        async def on_connect(ws):
            self._connection_count += 1
            role = ""
            try:
                role = ws.request.headers.get("X-Client-Role", "Universal")
            except Exception:
                role = "Universal"
            peer = getattr(ws, "remote_address", None)
            logger.info(f"[WS←] OneBot 连接: {peer} (role={role})")
            self._ws = ws
            try:
                await self._handle_ws(ws)
            except websockets.ConnectionClosed as exc:
                logger.info(f"[WS←] OneBot 断开: {peer} ({exc})")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[WS←] 异常: {peer}")
            finally:
                if self._ws is ws:
                    self._ws = None

        try:
            self._server = await websockets.serve(
                on_connect, host, port, max_size=64 * 1024 * 1024
            )
        except TypeError:
            # 旧版 websockets 无 max_size 关键字
            self._server = await websockets.serve(on_connect, host, port)

        await asyncio.Future()

    # ==================== 消息处理 ====================

    async def _handle_ws(self, ws):
        """统一处理 WS 消息。"""
        async for raw in ws:
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("无法解码的二进制 WS 消息，已忽略。")
                    continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"非 JSON: {str(raw)[:200]}")
                continue
            if not isinstance(msg, dict):
                continue

            if "post_type" in msg:
                asyncio.create_task(self._dispatch_event(msg))

            elif "action" in msg:
                # API 调用（反向模式下 OneBot 发给我们）
                echo = msg.get("echo")
                try:
                    result = await self._dispatch_api(msg)
                    if result is not None:
                        resp = {"status": "ok", "retcode": 0, "data": result}
                    else:
                        resp = {"status": "failed", "retcode": 1404, "data": None}
                except Exception as exc:
                    resp = {"status": "failed", "retcode": 1400, "data": None}
                    logger.error(f"WS API 异常: {exc}")
                if echo is not None:
                    resp["echo"] = echo
                await ws.send(json.dumps(resp, ensure_ascii=False))

            elif "echo" in msg and "status" in msg:
                if self._api_response_handler:
                    self._api_response_handler(msg)

    async def _dispatch_api(self, msg: dict) -> Optional[dict]:
        if self._api_handler:
            return await self._api_handler(msg)
        return None

    # ==================== 生命周期 ====================

    async def stop(self):
        self._running = False
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self._ws = None
        server = self._server
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            self._server = None
