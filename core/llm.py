"""
LLM 端点的单次请求执行器 — 调用 OpenAI 兼容的 /chat/completions 接口。

职责边界（编排在 `core/llm_pool.py`）：
  - 持有**一个端点**的连接池、密钥与并发信号量
  - 发出一次请求并解析响应（文本 / tool_calls / usage / finish_reason）
  - 把 HTTP 与网络错误分类，供上层决定重试还是切换候选

不做：候选模型列表、熔断、跨端点转移（见 LLMPool）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from core.models import LLMProviderConfig, ModelSpec

logger = logging.getLogger("Maa.LLM")

# 400 响应里常见的「上下文超限」特征
_CONTEXT_ERROR = re.compile(
    r"context[\s_-]*(length|window|overflow|too long)"
    r"|maximum context"
    r"|too many tokens"
    r"|prompt is too long"
    r"|exceeds? the (maximum|max) (context|token)",
    re.IGNORECASE,
)

# 重试后的退避上限（秒）
_MAX_BACKOFF = 8.0


@dataclass
class ToolCall:
    """一次工具调用请求。"""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""


class LLMError(RuntimeError):
    """
    LLM 调用错误，保留 HTTP 状态码与错误分类。

    kind: auth / rate / context / server / network / empty / other
    """

    def __init__(self, message: str, status: int = 0, kind: str = "other",
                 retry_after: float = 0.0):
        super().__init__(message)
        self.status = int(status or 0)
        self.kind = str(kind or "other")
        self.retry_after = max(0.0, float(retry_after or 0.0))


class ContextOverflowError(LLMError):
    """上下文超限：上层应压缩历史后重试，而不是切换候选。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message, status=status, kind="context")


@dataclass
class RequestParams:
    """一次请求实际使用的采样参数（已按优先级合并）。"""

    temperature: float = 0.2
    top_p: float = 1.0
    max_tokens: int = 8192
    max_context_tokens: int = 48000


@dataclass
class ChatMessage:
    """模型返回的一条完整消息（可能同时含文本与工具调用）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def truncated(self) -> bool:
        """是否因 max_tokens 被截断。"""
        return str(self.finish_reason or "").lower() == "length"


class LLMClient:
    """OpenAI 兼容端点的执行器（连接池复用 + 端点级并发）。"""

    def __init__(
        self,
        provider: Optional[LLMProviderConfig] = None,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        temperature: float = 0.2,
        timeout_seconds: int = 180,
        connect_timeout_seconds: int = 10,
        max_concurrency: int = 2,
        max_retries: int = 2,
        fallback_models: Optional[list[str]] = None,
        max_tokens: int = 0,
    ):
        if provider is not None:
            self.name = provider.name
            self.base_url = (provider.base_url or "").rstrip("/")
            self.api_key = provider.api_key or ""
            self.timeout_seconds = max(10, int(provider.timeout_seconds or 180))
            self.connect_timeout_seconds = max(1, int(provider.connect_timeout_seconds or 10))
            self.max_concurrency = max(1, int(provider.max_concurrency or 2))
            self.max_retries = max(0, int(provider.max_retries or 0))
            self.models: list[ModelSpec] = list(provider.models)
        else:
            self.name = base_url or "default"
            self.base_url = (base_url or "").rstrip("/")
            self.api_key = api_key or ""
            self.timeout_seconds = max(10, int(timeout_seconds or 180))
            self.connect_timeout_seconds = max(1, int(connect_timeout_seconds or 10))
            self.max_concurrency = max(1, int(max_concurrency or 2))
            self.max_retries = max(0, int(max_retries or 0))
            specs = [ModelSpec(model=m) for m in (fallback_models or []) if m]
            if model:
                specs.insert(0, ModelSpec(model=model))
            self.models = specs
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    # ── 描述 ──

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.models)

    @property
    def key_fingerprint(self) -> str:
        """密钥指纹（sha256 前 8 位），用于日志比对且不泄露密钥。"""
        if not self.api_key:
            return "未配置"
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:8]

    def describe(self) -> str:
        models = ", ".join(spec.model for spec in self.models) or "（无模型）"
        return (f"端点 {self.base_url or '（未配置）'}"
                f" · 模型 {models}"
                f" · 密钥指纹 {self.key_fingerprint}")

    def _endpoint(self) -> str:
        """拼出 chat/completions 完整地址。"""
        base = self.base_url
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        """惰性创建并复用连接池（避免每轮工具调用重新握手）。"""
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(
                    total=self.timeout_seconds,
                    sock_connect=self.connect_timeout_seconds,
                )
                connector = aiohttp.TCPConnector(
                    limit=max(4, self.max_concurrency * 2),
                    ttl_dns_cache=300,
                )
                self._session = aiohttp.ClientSession(
                    timeout=timeout, connector=connector
                )
        return self._session

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    # ── 请求 ──

    async def chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        spec: Optional[ModelSpec] = None,
        params: Optional[RequestParams] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
    ) -> ChatMessage:
        """
        发出一次请求并返回完整消息。

        失败时抛出 `LLMError`（上下文超限抛 `ContextOverflowError`）。
        并发由端点级信号量限制。
        """
        model_name = str(model or (spec.model if spec is not None else "")).strip()
        if not model_name:
            raise LLMError("缺少模型名", kind="other")
        if not self.base_url:
            raise LLMError(f"端点 {self.name or '（未命名）'} 未配置 base_url", kind="auth")

        params = params or RequestParams()
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": float(params.temperature),
            "top_p": float(params.top_p),
            "max_tokens": int(params.max_tokens),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        endpoint = self._endpoint()
        async with self._semaphore:
            session = await self._get_session()
            try:
                async with session.post(
                    endpoint, json=payload, headers=self._headers()
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise self._error_for(resp.status, body, resp.headers)
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise LLMError(
                            f"响应不是合法 JSON：{body[:200]}", kind="other"
                        ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise LLMError(
                    f"网络错误：{type(exc).__name__}: {exc}", kind="network"
                ) from exc

        message = self._extract_message(data)
        if message is None:
            raise LLMError("返回内容为空", kind="empty")
        message.model = model_name
        return message

    @staticmethod
    def _error_for(status: int, body: str, headers: Any) -> LLMError:
        """按 HTTP 状态与响应体分类错误。"""
        snippet = (body or "")[:300]
        if status in (401, 403):
            return LLMError(f"HTTP {status}: {snippet}", status, "auth")
        if status == 429:
            retry_after = 0.0
            try:
                retry_after = float(headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                retry_after = 0.0
            return LLMError(f"HTTP 429: {snippet}", status, "rate", retry_after)
        if status == 400 and _CONTEXT_ERROR.search(body or ""):
            return ContextOverflowError(f"HTTP 400（上下文超限）: {snippet}", status)
        if status >= 500:
            return LLMError(f"HTTP {status}: {snippet}", status, "server")
        return LLMError(f"HTTP {status}: {snippet}", status, "other")

    @classmethod
    def _extract_message(cls, data: Any) -> Optional[ChatMessage]:
        """解析响应为 ChatMessage（含 tool_calls / usage）。"""
        if not isinstance(data, dict):
            return None

        choice: dict = {}
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]

        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}

        content = cls._extract_content(message.get("content"))
        if not content:
            # 部分实现把文本放在 choice 顶层
            content = cls._extract_content(choice.get("text")) or ""

        tool_calls = cls._extract_tool_calls(message.get("tool_calls"))
        finish_reason = str(choice.get("finish_reason") or "")

        if not content and not tool_calls:
            # 兜底：非标准结构直接返回文本
            fallback = cls._extract_text(data)
            if not fallback:
                return None
            content = fallback

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return ChatMessage(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=message,
            usage=usage,
            model=str(data.get("model") or ""),
        )

    @staticmethod
    def _extract_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                seg.get("text", "")
                for seg in content
                if isinstance(seg, dict) and seg.get("type") == "text"
            ]
            return "".join(parts).strip()
        return ""

    @staticmethod
    def _extract_tool_calls(raw: Any) -> list[ToolCall]:
        """解析 tool_calls，容忍参数非法 JSON。"""
        if not isinstance(raw, list):
            return []

        calls: list[ToolCall] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            raw_args = function.get("arguments")
            if not isinstance(raw_args, str):
                raw_args = json.dumps(raw_args or {}, ensure_ascii=False)

            arguments: dict = {}
            parse_error = ""
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    parse_error = "参数不是 JSON 对象"
            except json.JSONDecodeError as exc:
                parse_error = f"参数不是合法 JSON：{exc}"

            calls.append(ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments=arguments,
                raw_arguments=raw_args,
                parse_error=parse_error,
            ))
        return calls

    @staticmethod
    def _extract_text(data: Any) -> Optional[str]:
        """兼容 OpenAI / 部分第三方实现的响应结构。"""
        if not isinstance(data, dict):
            return None

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                    if isinstance(content, list):
                        parts = [
                            seg.get("text", "")
                            for seg in content
                            if isinstance(seg, dict) and seg.get("type") == "text"
                        ]
                        joined = "".join(parts).strip()
                        if joined:
                            return joined
                text = first.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

        for key in ("content", "response", "output_text", "result"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None


def backoff_seconds(attempt: int, retry_after: float = 0.0) -> float:
    """指数退避（带抖动上限），优先使用服务端 Retry-After。"""
    if retry_after > 0:
        return min(max(retry_after, 0.5), 30.0)
    return min(_MAX_BACKOFF, 0.5 * (2 ** max(0, attempt - 1)))
