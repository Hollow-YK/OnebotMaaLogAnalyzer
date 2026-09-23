"""
HTTP 调试端点 — 提供 REST API 进行模拟事件注入。

端点:
  POST /debug/event     注入 OneBot 事件，返回 DebugResult JSON
  POST /debug/upload    便捷群文件上传注入
  POST /debug/message   便捷普通消息注入
  POST /debug/files     预置群文件列表
  POST /debug/run       运行 JSON 测试文件
  GET  /debug/health    健康检查
  GET  /debug/configs   查看已加载配置
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from core.service import MaaService
from debug import DebugManager, DebugResult

logger = logging.getLogger("Maa.Debug.HTTP")


def _result_to_dict(result: DebugResult) -> dict:
    """将 DebugResult 转为 JSON 可序列化字典。"""
    return {
        "reply": result.reply,
        "replies": result.replies,
        "api_calls": [
            {
                "action": c.action,
                "params": {
                    k: v if isinstance(v, (int, float, bool, type(None), str, list, dict))
                    else str(v)
                    for k, v in c.params.items()
                },
            }
            for c in result.api_calls
        ],
        "error": result.error,
        "elapsed_ms": round(result.elapsed_ms, 1),
    }


class DebugHTTPHandler:
    """aiohttp HTTP 请求处理器。"""

    def __init__(self, manager: DebugManager):
        self.manager = manager

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "mode": "debug"})

    async def configs(self, request: web.Request) -> web.Response:
        service = self.manager.service
        return web.json_response({
            "count": len(service.configs),
            "configs": {
                name: {
                    "listen_groups": sorted(state.info.listen_groups),
                    "notify_group": state.info.notify_group,
                    "enabled": state.settings.enabled,
                    "file_prefix": state.settings.file_prefix,
                    "job_count": len(state.jobs),
                }
                for name, state in service.configs.items()
            },
        })

    async def _read_json(self, request: web.Request):
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            return None, web.json_response({"error": f"Invalid JSON: {exc}"}, status=400)
        if not isinstance(body, dict):
            return None, web.json_response({"error": "body must be a JSON object"}, status=400)
        return body, None

    async def inject_event(self, request: web.Request) -> web.Response:
        """POST /debug/event — 注入 OneBot 事件。"""
        body, error = await self._read_json(request)
        if error:
            return error
        result = await self.manager.inject_event(body)
        return web.json_response(_result_to_dict(result))

    async def inject_upload(self, request: web.Request) -> web.Response:
        """POST /debug/upload — 便捷群文件上传注入。"""
        body, error = await self._read_json(request)
        if error:
            return error

        group_id = body.get("group_id")
        user_id = body.get("user_id")
        file_name = body.get("file_name") or body.get("name")

        if not group_id or not user_id or not file_name:
            return web.json_response(
                {"error": "group_id, user_id and file_name are required"}, status=400
            )

        result = await self.manager.inject_upload(
            group_id=int(group_id),
            user_id=int(user_id),
            file_name=str(file_name),
            file_id=str(body.get("file_id", "debug-file-id")),
            size=int(body.get("size", 0) or 0),
            busid=int(body.get("busid", 0) or 0),
        )
        return web.json_response(_result_to_dict(result))

    async def inject_message(self, request: web.Request) -> web.Response:
        """POST /debug/message — 便捷普通消息注入。"""
        body, error = await self._read_json(request)
        if error:
            return error

        group_id = body.get("group_id")
        user_id = body.get("user_id")
        raw_message = body.get("raw_message", body.get("text", ""))

        if not group_id or not user_id:
            return web.json_response({"error": "group_id and user_id are required"}, status=400)

        result = await self.manager.inject_message(
            group_id=int(group_id),
            user_id=int(user_id),
            raw_message=str(raw_message),
            sender_card=str(body.get("sender_card", "")),
            at_list=body.get("at_list"),
        )
        return web.json_response(_result_to_dict(result))

    async def set_files(self, request: web.Request) -> web.Response:
        """POST /debug/files — 预置群文件列表。"""
        body, error = await self._read_json(request)
        if error:
            return error

        group_id = body.get("group_id")
        files = body.get("files", [])

        if not group_id:
            return web.json_response({"error": "group_id is required"}, status=400)
        if not isinstance(files, list):
            return web.json_response({"error": "files must be a list"}, status=400)

        self.manager.api.set_mock_files(int(group_id), files, body.get("folders"))

        return web.json_response({
            "status": "ok",
            "group_id": group_id,
            "file_count": len(files),
        })

    async def run_tests(self, request: web.Request) -> web.Response:
        """POST /debug/run — 运行测试文件。"""
        body, error = await self._read_json(request)
        if error:
            return error

        file_path = body.get("file")
        if not file_path:
            return web.json_response({"error": "file path is required"}, status=400)

        from debug.runner import run_test_file
        path = Path(file_path)
        if not path.exists():
            return web.json_response({"error": f"File not found: {file_path}"}, status=404)

        report = await run_test_file(str(path), self.manager)
        return web.json_response(report.to_dict())

    def register(self, app: web.Application):
        app.router.add_get("/debug/health", self.health)
        app.router.add_get("/debug/configs", self.configs)
        app.router.add_post("/debug/event", self.inject_event)
        app.router.add_post("/debug/upload", self.inject_upload)
        app.router.add_post("/debug/message", self.inject_message)
        app.router.add_post("/debug/files", self.set_files)
        app.router.add_post("/debug/run", self.run_tests)


async def start_http_server(port: int, service: MaaService, cfg: dict):
    """启动 HTTP 调试服务器（后台任务）。"""
    manager = DebugManager.from_config(cfg)
    handler = DebugHTTPHandler(manager)

    app = web.Application()
    handler.register(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)

    try:
        await site.start()
        logger.info(f"[Debug HTTP] 监听 http://127.0.0.1:{port}")
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("[Debug HTTP] 正在关闭...")
    finally:
        await runner.cleanup()
