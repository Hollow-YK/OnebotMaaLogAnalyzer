"""
LLM 客户端 — 调用 OpenAI 兼容的 /chat/completions 接口。

支持:
  - 自定义 base_url / api_key / model
  - system_prompt 与 user prompt 分离
  - 超时与错误处理（返回 None 表示失败）
  - 备用模型列表（主模型失败时按序尝试）
  - tool calling（多轮工具调用，供仓库自主检索使用）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("Maa.LLM")


@dataclass
class ToolCall:
    """一次工具调用请求。"""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""


@dataclass
class ChatMessage:
    """模型返回的一条完整消息（可能同时含文本与工具调用）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    """OpenAI 兼容的 Chat Completions 客户端。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        *,
        temperature: float = 0.2,
        timeout_seconds: int = 900,
        fallback_models: Optional[list[str]] = None,
        max_tokens: int = 0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = float(temperature)
        self.timeout_seconds = max(10, int(timeout_seconds or 900))
        self.fallback_models = [m for m in (fallback_models or []) if m]
        self.max_tokens = max(0, int(max_tokens or 0))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

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
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _candidate_models(self, model_override: str = "") -> list[str]:
        models: list[str] = []
        if model_override:
            models.append(model_override)
        if self.model and self.model not in models:
            models.append(self.model)
        for item in self.fallback_models:
            if item not in models:
                models.append(item)
        return models

    async def chat(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        temperature: Optional[float] = None,
        model_override: str = "",
    ) -> Optional[str]:
        """
        调用模型并返回纯文本回复。
        全部候选模型都失败时返回 None。
        """
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        reply = await self.chat_messages(messages, temperature=temperature,
                                         model_override=model_override)
        return reply.content if reply else None

    async def chat_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: Optional[float] = None,
        model_override: str = "",
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
    ) -> Optional[ChatMessage]:
        """
        调用模型并返回完整消息（含 tool_calls）。

        tools 为空时退化为普通对话；全部候选模型都失败时返回 None。
        """
        if not self.configured:
            logger.error("[LLM] 未配置 base_url 或 model，无法调用 AI。")
            return None

        models = self._candidate_models(model_override)
        if not models:
            logger.error("[LLM] 没有可用模型名。")
            return None

        last_error: str = ""
        for index, model in enumerate(models):
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": float(self.temperature if temperature is None else temperature),
                "stream": False,
            }
            if self.max_tokens > 0:
                payload["max_tokens"] = self.max_tokens
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = tool_choice

            try:
                message = await self._post_message(payload)
                if message is not None:
                    if index > 0:
                        logger.info(f"[LLM] 备用模型 {model} 调用成功。")
                    return message
                last_error = "返回内容为空"
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"[LLM] 模型 {model} 调用失败：{exc}")

        logger.error(f"[LLM] 所有候选模型均失败：{last_error}")
        return None

    async def _post_message(self, payload: dict) -> Optional[ChatMessage]:
        endpoint = self._endpoint()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json=payload, headers=self._headers()) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"响应不是合法 JSON：{body[:200]}") from exc

        return self._extract_message(data)

    @classmethod
    def _extract_message(cls, data: Any) -> Optional[ChatMessage]:
        """解析响应为 ChatMessage（含 tool_calls）。"""
        if not isinstance(data, dict):
            return None

        choice = {}
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

        return ChatMessage(content=content, tool_calls=tool_calls,
                           finish_reason=finish_reason, raw=message)

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

        # 标准 OpenAI 结构
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
                        # 多段 content（部分实现）
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

        # 部分实现直接返回
        for key in ("content", "response", "output_text", "result"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None
