"""
调试核心引擎 — 通过模拟 OneBot 事件注入来测试业务逻辑。

提供:
  - DebugAPI:      与 OneBotAPI 签名一致的模拟 API，记录所有调用
  - DebugManager:   构建完整测试管线的事件注入器
  - DebugResult:    注入结果（回复 + API 调用记录 + 异常）
  - APICall:        单次 API 调用记录

使用方式:
    manager = DebugManager.from_config(cfg, data_dir="data/test")
    result = await manager.inject_upload(123456, 10001, "MaaNTE-logs-1.zip")
    print(result.reply, result.api_calls)
"""
from __future__ import annotations

import base64
import io
import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.handler import EventHandler
from core.data_manager import DataManager
from core.llm import ChatMessage, LLMClient, ToolCall
from core.service import MaaService
from features.maa.analyzer import MaaAnalyzer
from features.maa.followup import FollowupStore
from features.maa.history import HistoryStore
from features.maa.message_handler import MessageHandler
from features.maa.watcher import LogWatcher

logger = logging.getLogger("Maa.Debug")

# 调试模式的固定报告文本（供断言匹配）
# 行内附图（@日志）+ 行内文件附件（@日志），用于验证图文混排与文件发送
_DEBUG_REPORT = (
    "结论：\n"
    "（调试模式模拟输出）检测到 1 条 ERROR 记录，最可能是任务配置缺失。\n\n"
    "关键证据：\n"
    "1. [调试] ERROR 日志命中\n\n"
    "下面这张错误截图可以直接看出界面停在哪里：\n"
    "[附图@日志: on_error/on_error_20260101_120002.png]\n"
    "可以看到界面没有进入主界面。\n\n"
    "对应的配置内容如下（已附上原文件）：\n"
    "[附件@日志: config/mxu-MaaXXX.json]\n"
    "其中 instances 的 tasks 为空，这解释了为什么任务没有启动。\n\n"
    "可能原因：\n"
    "1. 调试模式不执行真实 AI 分析\n\n"
    "处理建议：\n"
    "1. 配置真实 llm.base_url / llm.api_key / llm.model 后重试\n\n"
    "需要补充：\n"
    "无需补充。"
)


# ════════════════════════════════════════════════════════════════
# 样例日志包
# ════════════════════════════════════════════════════════════════

_SAMPLE_MAAFW_LOG = """[2026-01-01 12:00:00.000][INF][Px1][Tx1][Logger] MaaFramework 启动
[2026-01-01 12:00:00.100][INF][Px1][Tx1][Resource] 资源加载完成
[2026-01-01 12:00:01.200][TRC][Px1][Tx1][Tasker] handle_controller_wait | enter
[2026-01-01 12:00:02.300][ERR][Px1][Tx1][Tasker] Task.StartUp Failed [msg=Task.StartUp][entry=StartUp]
[2026-01-01 12:00:02.400][WRN][Px1][Tx1][Controller] 截图失败 internal error: status 0
[2026-01-01 12:00:03.500][ERR][Px1][Tx1][Recognizer] Recognition.Failed: OCRer best_result score=0.12 expected="开始"
[2026-01-01 12:00:04.600][INF][Px1][Tx1][Tasker] 任务结束
"""

_SAMPLE_CONFIG = """{
  "version": "1.2.0",
  "instances": [
    {
      "name": "默认实例",
      "controllerName": "Adb",
      "resourceName": "MaaNTE",
      "tasks": [
        {"taskName": "StartUp", "enabled": true, "optionValues": {"Server": {"value": "CN"}}}
      ]
    }
  ]
}
"""


def _make_sample_log_zip() -> bytes:
    """构造一个最小 MaaXXX 日志包，用于调试模式覆盖完整分析流程。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("maafw.log", _SAMPLE_MAAFW_LOG)
        archive.writestr("config/mxu-MaaXXX.json", _SAMPLE_CONFIG)
        archive.writestr("on_error/on_error_20260101_120002.png", _SAMPLE_PNG)
    return buffer.getvalue()


# 最小合法 PNG（1x1 透明像素），用于验证附图链路
_SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class APICall:
    """单次 API 调用记录。"""

    action: str
    params: dict
    result: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class DebugResult:
    """事件注入结果。"""

    reply: Optional[str] = None          # 群消息回复（多条时合并）
    replies: list[str] = field(default_factory=list)  # 全部群消息回复
    api_calls: list[APICall] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_ms: float = 0


# ════════════════════════════════════════════════════════════════
# 模拟 API
# ════════════════════════════════════════════════════════════════

class DebugAPI:
    """与 OneBotAPI 签名一致的模拟 API。
    所有方法记录调用参数并返回模拟成功结果，不发起真实网络请求。"""

    def __init__(self):
        self.calls: list[APICall] = []
        self._files: dict[int, list[dict]] = {}
        self._folders: dict[tuple[int, str], dict] = {}
        self._file_payloads: dict[str, bytes] = {}
        self._next_message_id = 100000   # 单调递增，清空调用记录也不重复
        self.last_message_id = 0         # 最近一次发出的消息 ID（供测试引用回复）
        self.version_info: dict = {"app_name": "DebugOneBot", "app_version": "1.0.0"}

    def _record(self, action: str, params: dict, result: Any = True) -> Any:
        call = APICall(action=action, params=dict(params))
        call.result = result
        self.calls.append(call)
        return result

    def clear(self):
        """清空调用记录（保留预设数据）。"""
        self.calls.clear()

    # ── 数据预设 ──

    def set_mock_files(self, group_id: int, files: list[dict],
                       folders: Optional[list[dict]] = None):
        """预置群根目录文件列表。"""
        self._files[group_id] = list(files)
        if folders:
            self._folders[(group_id, "/")] = {"files": [], "folders": list(folders)}

    def set_mock_folder_files(self, group_id: int, folder_id: str,
                              files: list[dict], folders: Optional[list[dict]] = None):
        """预置子目录文件列表。"""
        self._folders[(group_id, folder_id)] = {
            "files": list(files),
            "folders": list(folders or []),
        }

    def set_mock_file_payload(self, file_id: str, data: bytes):
        """预置文件下载内容（用于构造真实 zip 测试摘要流程）。"""
        self._file_payloads[file_id] = data

    # ── 群文件上传 ──

    async def upload_group_file(self, group_id: int, file: str, name: str,
                                folder: str = "") -> Optional[Any]:
        """模拟上传群文件，记录调用供断言。"""
        return self._record(
            "upload_group_file",
            {"group_id": group_id, "file": file, "name": name, "folder": folder},
            {"message": "ok"},
        )

    # ── 消息发送 ──

    async def send_group_msg(self, group_id: int, message) -> Optional[int]:
        if isinstance(message, str):
            msg_repr: Any = message
            segments: list[str] = ["text"]
        elif isinstance(message, list):
            texts = [
                seg.get("data", {}).get("text", "")
                for seg in message
                if isinstance(seg, dict) and seg.get("type") == "text"
            ]
            msg_repr = "".join(texts) if texts else f"[{len(message)} segments]"
            segments = [
                str(seg.get("type")) for seg in message if isinstance(seg, dict)
            ]
        else:
            msg_repr = str(message)
            segments = []
        self._next_message_id += 1
        self.last_message_id = self._next_message_id
        self._record(
            "send_group_msg",
            {"group_id": group_id, "message": msg_repr, "segments": segments},
            {"message_id": self._next_message_id},
        )
        return self._next_message_id

    # ── 群文件 ──

    async def get_group_root_files(self, group_id: int,
                                   file_count: int = 2000) -> Optional[dict]:
        payload = {
            "files": list(self._files.get(group_id, [])),
            "folders": list(self._folders.get((group_id, "/"), {}).get("folders", [])),
        }
        return self._record("get_group_root_files", {"group_id": group_id}, payload)

    async def get_group_files_by_folder(self, group_id: int, folder_id: str,
                                        file_count: int = 2000) -> Optional[dict]:
        payload = self._folders.get((group_id, folder_id), {"files": [], "folders": []})
        return self._record(
            "get_group_files_by_folder",
            {"group_id": group_id, "folder_id": folder_id},
            payload,
        )

    async def get_group_file_url(self, group_id: int, file_id: str) -> Optional[str]:
        url = f"debug://group/{group_id}/file/{file_id}"
        return self._record("get_group_file_url",
                            {"group_id": group_id, "file_id": file_id}, url)

    async def download_url(self, url: str, target_path, *,
                           max_bytes: int, timeout_seconds: int = 120) -> int:
        """把预置的内存内容写入本地文件，模拟下载。"""
        self._record("download_url",
                     {"url": url, "target": str(target_path), "max_bytes": max_bytes},
                     None)
        file_id = url.rsplit("/", 1)[-1] if isinstance(url, str) else ""
        data = self._file_payloads.get(file_id)
        if data is None:
            # 未预置时生成一个最小 MaaNTE 日志包，便于覆盖完整分析流程
            data = _make_sample_log_zip()
        if len(data) > max_bytes:
            raise RuntimeError(f"文件超过大小限制（{max_bytes // 1024 // 1024} MB），已停止下载。")

        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    # ── 基础信息 ──

    async def get_login_info(self) -> Optional[dict]:
        return self._record("get_login_info", {}, {"user_id": 10000, "nickname": "DebugBot"})

    async def get_version_info(self) -> Optional[dict]:
        return self._record("get_version_info", {}, dict(self.version_info))

    async def close(self):
        return None


# ════════════════════════════════════════════════════════════════
# 模拟 LLM
# ════════════════════════════════════════════════════════════════

class DebugLLMClient(LLMClient):
    """不发起真实请求的 LLM 客户端，返回固定结构的模拟分析结果。

    agent 模式下会先请求一次 search_repo 工具，再输出最终报告，
    以便离线验证完整的工具调用循环。
    """

    def __init__(self):
        super().__init__(base_url="debug://llm", api_key="", model="debug-model")
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []
        self.tool_rounds: list[list[dict]] = []   # 每轮收到的 tools 定义
        self.tool_results: list[str] = []         # 收到的工具执行结果
        # 可覆盖的模拟报告（测试可注入含特定附件指令的报告）
        self.report_override: str = ""

    @property
    def report(self) -> str:
        return self.report_override or _DEBUG_REPORT

    @property
    def configured(self) -> bool:
        return True

    async def chat(self, prompt: str, *, system_prompt: str = "",
                   temperature: Optional[float] = None,
                   model_override: str = "") -> Optional[str]:
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        return self.report

    async def chat_messages(self, messages: list[dict], *, temperature=None,
                            model_override: str = "", tools=None,
                            tool_choice: str = "auto") -> Optional[ChatMessage]:
        """模拟一轮工具调用，然后给出最终报告。"""
        self.tool_rounds.append(list(tools or []))

        # 收集本轮之前已产生的工具结果
        for msg in messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content") or "")
                if content not in self.tool_results:
                    self.tool_results.append(content)

        system_text = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "system"), ""
        )
        self.system_prompts.append(system_text)
        self.prompts.append(next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "user"), ""
        ))

        # 第一轮且工具可用：请求一次搜索，验证工具链路
        if tools and not self.tool_results:
            # 优先用日志工具（若可用），否则用仓库工具
            names = {t.get("function", {}).get("name") for t in tools}
            if "log_search" in names:
                call = ToolCall(
                    id="debug_call_log",
                    name="log_search",
                    arguments={"pattern": "ERROR"},
                    raw_arguments='{"pattern": "ERROR"}',
                )
            else:
                call = ToolCall(
                    id="debug_call_1",
                    name="search_repo",
                    arguments={"pattern": "StartUp"},
                    raw_arguments='{"pattern": "StartUp"}',
                )
            return ChatMessage(
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
            )

        # 之后直接给结论
        return ChatMessage(content=self.report, finish_reason="stop")


# ════════════════════════════════════════════════════════════════
# 调试管理器
# ════════════════════════════════════════════════════════════════

class DebugManager:
    """构建完整测试管线，通过模拟事件注入测试业务逻辑。

    用法:
        cfg = json.loads(Path("config.json").read_text("utf-8"))
        manager = DebugManager.from_config(cfg)
        result = await manager.inject_upload(123456, 10001, "MaaNTE-logs-1.zip")
    """

    def __init__(self, api: DebugAPI, service: MaaService,
                 handler: EventHandler, llm: DebugLLMClient):
        self.api = api
        self.service = service
        self.handler = handler
        self.llm = llm
        self.watcher: Optional[LogWatcher] = None
        self.followup: Optional[FollowupStore] = None
        self.messages: Optional[MessageHandler] = None
        self.history: Optional[HistoryStore] = None
        # 构建时的原始配置，供「真实重启」测试在子进程里原样重建
        self.cfg: dict = {}
        self.data_dir: str = str(getattr(service.dm, "_dir", "data/test"))
        # 模拟报告覆盖（由测试 setup 设置），重启后需复现
        self.model_report: str = ""

    @classmethod
    def from_config(cls, cfg: dict, data_dir: str = "data/test") -> "DebugManager":
        """根据配置字典构建完整的调试管线。"""
        bot_cfg = cfg.get("bot", {})

        api = DebugAPI()
        dm = DataManager(bot_cfg.get("data_dir", data_dir))

        service = MaaService(api=api, dm=dm)  # type: ignore[arg-type]
        service.load()
        service.sync_from_config(bot_cfg.get("configs") or {})

        llm = DebugLLMClient()
        history = HistoryStore(dm._dir)
        analyzer = MaaAnalyzer(service, llm, history=history)
        followup = FollowupStore(history=history)
        watcher = LogWatcher(service, analyzer, followup, history=history)
        messages = MessageHandler(
            service, analyzer, followup, history=history,
            segment_sender=watcher.send_segments,
        )

        service.register_event("notice.group_upload", watcher.on_group_upload)
        service.register_event("message.file", watcher.on_message_file)
        service.register_event("message.text", messages.on_message_text)

        handler = EventHandler(api, service)  # type: ignore[arg-type]

        manager = cls(api=api, service=service, handler=handler, llm=llm)
        manager.watcher = watcher
        manager.followup = followup
        manager.messages = messages
        manager.history = history
        manager.cfg = cfg
        manager.data_dir = str(dm._dir)
        return manager

    # ── 事件注入 ──

    async def inject_event(self, event: dict) -> DebugResult:
        """注入模拟 OneBot 事件，捕获回复和 API 调用。"""
        self.api.clear()
        result = DebugResult()
        t0 = time.perf_counter()

        try:
            pt = event.get("post_type", "")

            if pt == "message":
                await self.handler.on_message(event)
            elif pt == "notice":
                await self.handler.on_notice(event)
            elif pt == "request":
                await self.handler.on_request(event)
            elif "meta_event_type" in event:
                pass
            else:
                result.error = f"未知事件类型: post_type={pt!r}"

            for call in self.api.calls:
                if call.action == "send_group_msg":
                    msg = call.params.get("message", "")
                    result.replies.append(msg if isinstance(msg, str) else str(msg))
            if result.replies:
                result.reply = "\n".join(result.replies)

        except Exception as exc:
            logger.exception(f"事件注入异常: {exc}")
            result.error = f"{type(exc).__name__}: {exc}"

        result.api_calls = list(self.api.calls)
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        return result

    # ── 便捷方法 ──

    async def inject_upload(self, group_id: int, user_id: int, file_name: str, *,
                            file_id: str = "debug-file-id",
                            size: int = 0, busid: int = 0) -> DebugResult:
        """便捷方法：构造并注入群文件上传通知事件。"""
        event = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": group_id,
            "user_id": user_id,
            "file": {
                "id": file_id,
                "name": file_name,
                "size": size,
                "busid": busid,
            },
        }
        return await self.inject_event(event)

    async def inject_message_file(self, group_id: int, user_id: int,
                                  file_name: str, *,
                                  file_id: str = "debug-file-id",
                                  size: int = 0) -> DebugResult:
        """便捷方法：构造并注入带文件段的消息事件。"""
        event = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 1,
            "group_id": group_id,
            "user_id": user_id,
            "raw_message": "",
            "message": [{
                "type": "file",
                "data": {"file_id": file_id, "file_name": file_name, "size": size},
            }],
            "sender": {"user_id": user_id, "nickname": f"user_{user_id}", "card": ""},
        }
        return await self.inject_event(event)

    async def inject_message(self, group_id: int, user_id: int,
                             raw_message: str, *,
                             sender_card: str = "",
                             at_list: Optional[list[str]] = None,
                             message_id: int = 1) -> DebugResult:
        """便捷方法：构造并注入一条普通群消息事件（用于验证 Bot 不响应指令）。"""
        message = raw_message
        if at_list:
            for qq in at_list:
                message = f"[CQ:at,qq={qq}] " + message

        event = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": message_id,
            "group_id": group_id,
            "user_id": user_id,
            "raw_message": raw_message,
            "message": message,
            "sender": {
                "user_id": user_id,
                "nickname": f"user_{user_id}",
                "card": sender_card or "",
            },
        }
        return await self.inject_event(event)

    async def inject_notice(self, notice_type: str, group_id: int,
                            user_id: int, **extra) -> DebugResult:
        """便捷方法：构造并注入一条通知事件。"""
        event = {
            "post_type": "notice",
            "notice_type": notice_type,
            "group_id": group_id,
            "user_id": user_id,
            **extra,
        }
        return await self.inject_event(event)

    async def inject_request(self, request_type: str, sub_type: str,
                             group_id: int, user_id: int,
                             **extra) -> DebugResult:
        """便捷方法：构造并注入一条请求事件。"""
        event = {
            "post_type": "request",
            "request_type": request_type,
            "sub_type": sub_type,
            "group_id": group_id,
            "user_id": user_id,
            **extra,
        }
        return await self.inject_event(event)
