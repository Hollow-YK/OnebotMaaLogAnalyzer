"""
LLM 端点的单次请求执行器 —— 支持两种协议：

  - `LLMClient`（kind=openai）→ `/v1/chat/completions`
  - `AnthropicClient`（kind=anthropic）→ `/v1/messages`

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

from ..core.models import LLMProviderConfig, ModelSpec

logger = logging.getLogger("Maa.LLM")

# 400 响应里常见的「上下文超限」特征
_CONTEXT_ERROR = re.compile(
    r"context[\s_-]*(length|window|overflow|too long)"
    r"|maximum context"
    r"|too many tokens"
    r"|prompt is too long"
    r"|(request|payload|entity) (is )?too large"
    r"|exceeds? the (maximum|max) (context|token)",
    re.IGNORECASE,
)

# 429 响应里常见的「配额/额度」特征：这类限流按分钟或更长时间恢复，
# 秒级重试没有意义，应直接冷却端点而不是消耗重试次数。
_QUOTA_ERROR = re.compile(
    r"\btpm\b|\btpd\b|\brpm\b|\brpd\b"
    r"|quota|exhausted|insufficient|balance"
    r"|per[\s_-]*minute|per[\s_-]*day"
    r"|token.{0,12}limit",
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
    provider.kind 决定协议：openai（/v1/chat/completions）或
    anthropic（/v1/messages，DeepSeek 的 anthropic 端点同理）
    quota: kind=rate 时，是否属于配额耗尽（TPM/RPM/额度）而非瞬时限流
    """

    def __init__(self, message: str, status: int = 0, kind: str = "other",
                 retry_after: float = 0.0, quota: bool = False):
        super().__init__(message)
        self.status = int(status or 0)
        self.kind = str(kind or "other")
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.quota = bool(quota)


class ContextOverflowError(LLMError):
    """上下文超限：上层应压缩历史后重试，而不是切换候选。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message, status=status, kind="context")


def normalize_usage(usage: Optional[dict]) -> dict[str, int]:
    """
    把不同厂商的 usage 归一化为互不重叠的四个桶。

    兼容：
      - DeepSeek：prompt_tokens（含命中）+ prompt_cache_hit_tokens
      - OpenAI：prompt_tokens + prompt_tokens_details.cached_tokens
      - Anthropic 风格：input_tokens / cache_read_input_tokens /
        cache_creation_input_tokens

    返回：prompt_tokens（计费输入总数）、uncached_prompt_tokens、
    cache_read_tokens、cache_write_tokens、completion_tokens、total_tokens。
    """
    data = usage if isinstance(usage, dict) else {}
    details = data.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt = _int(data.get("prompt_tokens"))
    input_tokens = _int(data.get("input_tokens"))
    completion = _int(data.get("completion_tokens")) or _int(data.get("output_tokens"))

    cache_read = (
        _int(data.get("prompt_cache_hit_tokens"))
        or _int(details.get("cached_tokens"))
        or _int(data.get("cache_read_input_tokens"))
    )
    cache_write = _int(data.get("cache_creation_input_tokens"))

    if prompt > 0:
        # OpenAI / DeepSeek：prompt_tokens 已包含缓存命中部分
        billed = prompt
        uncached = max(0, prompt - cache_read - cache_write)
    else:
        # Anthropic 风格：input_tokens 只是未命中缓存的部分
        uncached = input_tokens
        billed = uncached + cache_read + cache_write

    total = _int(data.get("total_tokens")) or (billed + completion)
    return {
        "prompt_tokens": billed,
        "uncached_prompt_tokens": uncached,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def cache_hit_rate(uncached: int, cache_read: int, cache_write: int = 0) -> float:
    """缓存命中率：命中读取 / 计费输入（未命中 + 读取 + 写入）。"""
    billed = max(0, int(uncached)) + max(0, int(cache_read)) + max(0, int(cache_write))
    if billed <= 0:
        return 0.0
    return max(0, int(cache_read)) / billed


@dataclass
class RequestParams:
    """一次请求实际使用的采样参数（已按优先级合并）。"""

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
    """OpenAI 兼容端点（/v1/chat/completions）的执行器。"""

    def __init__(
        self,
        provider: Optional[LLMProviderConfig] = None,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
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
        model_name = self._require_model(model, spec)
        params = params or RequestParams()
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": int(params.max_tokens),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        data = await self._post_with_fallbacks(payload)
        message = self._extract_message(data)
        if message is None:
            raise LLMError("返回内容为空", kind="empty")
        message.model = model_name
        return message

    async def _post_with_fallbacks(self, payload: dict[str, Any]) -> Any:
        """
        OpenAI 侧的参数兼容：部分实现/新模型会拒绝旧字段。

        - 新模型要求 `max_completion_tokens` 而非 `max_tokens`
        - 部分网关不接受 `tool_choice`
        每种情况只降级一次，避免掩盖真实错误。
        """
        for _ in range(3):
            try:
                return await self._post_json(payload)
            except LLMError as exc:
                if exc.status != 400:
                    raise
                detail = str(exc).lower()
                if "max_completion_tokens" in detail and "max_tokens" in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                elif "tool_choice" in detail and "tool_choice" in payload:
                    payload.pop("tool_choice", None)
                else:
                    raise
        return await self._post_json(payload)

    # ── 传输 ──

    def _require_model(self, model: str, spec: Optional[ModelSpec]) -> str:
        model_name = str(model or (spec.model if spec is not None else "")).strip()
        if not model_name:
            raise LLMError("缺少模型名", kind="other")
        if not self.base_url:
            raise LLMError(
                f"端点 {self.name or '（未命名）'} 未配置 base_url", kind="auth"
            )
        return model_name

    async def _post_json(self, payload: dict[str, Any]) -> Any:
        """发送请求并解析 JSON 响应（错误已按协议分类）。"""
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
                        return json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise LLMError(
                            f"响应不是合法 JSON：{body[:200]}", kind="other"
                        ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise LLMError(
                    f"网络错误：{type(exc).__name__}: {exc}", kind="network"
                ) from exc

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
            quota = bool(_QUOTA_ERROR.search(body or ""))
            return LLMError(f"HTTP 429: {snippet}", status, "rate",
                            retry_after, quota=quota)
        # 不同实现用 400 / 413 / 422 表示输入超限，统一归一化为上下文超限
        if status in (400, 413, 422) and _CONTEXT_ERROR.search(body or ""):
            return ContextOverflowError(
                f"HTTP {status}（上下文超限）: {snippet}", status
            )
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


# ════════════════════════════════════════════════════════════════
# Anthropic Messages 协议
# ════════════════════════════════════════════════════════════════

ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_KINDS = {"anthropic", "anthropic-messages", "claude", "messages"}


def _anthropic_image_block(url: str) -> Optional[dict]:
    """把 data URL 转成 Anthropic image 内容块。"""
    text = str(url or "")
    if not text.startswith("data:"):
        return None
    header, _, payload = text.partition(",")
    if not payload or ";base64" not in header:
        return None
    media_type = header[5:].split(";", 1)[0] or "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": payload},
    }


def _anthropic_content(content: Any) -> list[dict]:
    """把 OpenAI 风格 content 转成 Anthropic 内容块。"""
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": content}] if text else []
    if not isinstance(content, list):
        return []

    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text.strip():
                blocks.append({"type": "text", "text": text})
        elif kind == "image_url":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            block = _anthropic_image_block(str(url or ""))
            if block is not None:
                blocks.append(block)
    return blocks


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """
    把 OpenAI 风格 messages 转成 Anthropic 的 (system, messages)。

    - system / developer 消息合并进顶层 system
    - assistant.tool_calls → tool_use 内容块
    - tool 消息 → user 消息里的 tool_result 块
    - 相邻同角色消息合并（Anthropic 要求 user/assistant 交替）
    """
    systems: list[str] = []
    wire: list[dict] = []

    def _push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        previous = wire[-1] if wire else None
        if previous is not None and previous["role"] == role:
            previous["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")

        if role in ("system", "developer"):
            systems.extend(
                block["text"] for block in _anthropic_content(content)
                if block.get("type") == "text"
            )
            continue

        if role == "tool":
            text = str(content or "")
            if not isinstance(content, str):
                text = "\n".join(
                    block["text"] for block in _anthropic_content(content)
                    if block.get("type") == "text"
                )
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if not tool_call_id:
                continue
            _push("user", [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": text or "（空结果）",
            }])
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = _anthropic_content(content)
        if role == "assistant":
            for index, call in enumerate(message.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or "").strip()
                if not name:
                    continue
                raw_args = function.get("arguments")
                arguments: Any = raw_args
                if isinstance(raw_args, str):
                    try:
                        arguments = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or f"toolu_{index}"),
                    "name": name,
                    "input": arguments,
                })
        _push(role, blocks)

    if wire and wire[0]["role"] != "user":
        wire.insert(0, {"role": "user", "content": [{"type": "text", "text": "（接续上文）"}]})
    return "\n\n".join(part for part in systems if part), wire


def to_anthropic_tools(tools: Optional[list[dict]]) -> list[dict]:
    """OpenAI function 定义 → Anthropic 工具定义。"""
    result: list[dict] = []
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        result.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "input_schema": schema if isinstance(schema, dict) else {
                "type": "object", "properties": {},
            },
        })
    return result


def to_anthropic_tool_choice(tool_choice: str) -> Optional[dict]:
    """OpenAI tool_choice → Anthropic tool_choice（None = 不发送）。"""
    value = str(tool_choice or "auto").strip().lower()
    if value in ("", "auto"):
        return {"type": "auto"}
    if value in ("none",):
        return {"type": "none"}
    if value in ("required", "any"):
        return {"type": "any"}
    return {"type": "auto"}


class AnthropicClient(LLMClient):
    """Anthropic Messages（/v1/messages）端点执行器。"""

    kind = "anthropic"

    def _endpoint(self) -> str:
        """拼出 /v1/messages 完整地址（兼容 base_url 已含 /v1 或 /anthropic）。"""
        base = self.base_url
        if not base:
            return ""
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self.api_key:
            # 官方 Anthropic 用 x-api-key，部分网关只认 Bearer，两者都带
            headers["x-api-key"] = self.api_key
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

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
        model_name = self._require_model(model, spec)
        params = params or RequestParams()
        system, wire = to_anthropic_messages(messages)
        if not wire:
            raise LLMError("没有可发送的消息", kind="other")

        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max(1, int(params.max_tokens)),
            "messages": wire,
        }
        if system:
            payload["system"] = system
        wire_tools = to_anthropic_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools
            choice = to_anthropic_tool_choice(tool_choice)
            if choice is not None:
                payload["tool_choice"] = choice

        try:
            data = await self._post_json(payload)
        except LLMError as exc:
            detail = str(exc).lower()
            if exc.status != 400:
                raise
            # 部分网关不支持 tool_choice="none"：去掉该字段重试一次
            if tools and tool_choice == "none" and "tool_choice" in detail:
                payload.pop("tool_choice", None)
            elif "thinking" in detail and "thinking" not in payload:
                # DeepSeek 的 anthropic 端点要求显式声明 thinking
                payload["thinking"] = {"type": "disabled"}
            else:
                raise
            data = await self._post_json(payload)

        message = self._extract_message(data)
        if message is None:
            raise LLMError("返回内容为空", kind="empty")
        message.model = model_name
        return message

    @classmethod
    def _error_for(cls, status: int, body: str, headers: Any) -> LLMError:
        """解析 Anthropic 错误信封，再复用通用分类。"""
        text = body or ""
        error_type = ""
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                error_type = str(error.get("type") or "")
                detail = " ".join(
                    str(error.get(key) or "")
                    for key in ("type", "message")
                ).strip()
                if detail:
                    text = detail
        snippet = text[:300]
        if status in (401, 403) or error_type in (
            "authentication_error", "permission_error",
        ):
            return LLMError(f"HTTP {status}: {snippet}", status, "auth")
        if error_type == "rate_limit_error" or status in (402, 429):
            retry_after = 0.0
            try:
                retry_after = float(headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                retry_after = 0.0
            quota = status == 402 or bool(_QUOTA_ERROR.search(text))
            return LLMError(f"HTTP {status}: {snippet}", status, "rate",
                            retry_after, quota=quota)
        if status in (400, 413, 422) and _CONTEXT_ERROR.search(text):
            return ContextOverflowError(
                f"HTTP {status}（上下文超限）: {snippet}", status
            )
        if status >= 500 or error_type in ("api_error", "overloaded_error"):
            return LLMError(f"HTTP {status}: {snippet}", status, "server")
        return LLMError(f"HTTP {status}: {snippet}", status, "other")

    @classmethod
    def _extract_message(cls, data: Any) -> Optional[ChatMessage]:
        """解析 Anthropic Messages 响应。"""
        if not isinstance(data, dict):
            return None

        blocks = data.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            blocks = []

        texts: list[str] = []
        calls: list[ToolCall] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                name = str(block.get("name") or "").strip()
                if not name:
                    continue
                arguments = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                calls.append(ToolCall(
                    id=str(block.get("id") or f"toolu_{index}"),
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                ))

        content = "".join(texts).strip()
        if not content and not calls:
            return None

        stop_reason = str(data.get("stop_reason") or "")
        finish_reason = {
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "end_turn": "stop",
            "stop_sequence": "stop",
            "refusal": "stop",
        }.get(stop_reason, stop_reason)

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return ChatMessage(
            content=content,
            tool_calls=calls,
            finish_reason=finish_reason,
            raw={"content": blocks},
            usage=usage,
            model=str(data.get("model") or ""),
        )


def provider_kind(provider: LLMProviderConfig) -> str:
    """归一化端点协议类型（默认 openai）。"""
    kind = str(provider.kind or "").strip().lower()
    return "anthropic" if kind in _ANTHROPIC_KINDS else "openai"


def create_llm_client(provider: LLMProviderConfig) -> LLMClient:
    """按 provider.kind 创建 OpenAI 或 Anthropic 执行器。"""
    if provider_kind(provider) == "anthropic":
        return AnthropicClient(provider)
    return LLMClient(provider)
