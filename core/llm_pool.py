"""
LLM 候选池 — 把「配置级候选列表」编排到多个端点执行器上。

职责：
  - 解析 `bot.configs.<名称>.llm.models[]`，按数组顺序尝试
  - 能力过滤（tools / vision），过滤后为空时由上层降级
  - 端点级熔断与并发（配额是端点级的，跨配置共享）
  - 错误分类与顺序失败转移；上下文超限交给上层压缩后重试

不做：任务分流、权重、轮询、端点级能力声明。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

from core.ai_usage import SessionUsage, UsageRecord, UsageStore
from core.llm import (
    ChatMessage,
    ContextOverflowError,
    LLMClient,
    LLMError,
    RequestParams,
    backoff_seconds,
)
from core.models import (
    ConfigLLM,
    LLMGlobalConfig,
    LLMProviderConfig,
    ModelRef,
    ModelSpec,
)

logger = logging.getLogger("Maa.LLMPool")

# 当前会话（分析或某次追问）的标识。用 ContextVar 而非实例属性，
# 这样多个配置 / 多份分析并发时不会互相串号。
_SESSION: ContextVar[tuple[str, str, str]] = ContextVar(
    "maa_llm_session", default=("", "", "")
)


@contextmanager
def session_context(session_id: str, config_name: str = "",
                    file_name: str = "") -> Iterator[None]:
    """在 with 块内发起的模型调用都归入同一个会话。"""
    token = _SESSION.set((str(session_id or ""), str(config_name or ""),
                          str(file_name or "")))
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_session_id() -> str:
    return _SESSION.get()[0]


def new_session_id(config_name: str = "") -> str:
    """生成会话 ID：`<配置>-<时间戳>-<随机后缀>`。"""
    import uuid

    prefix = str(config_name or "session").replace(":", "_")
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


_LEGACY_KEYS = {
    "base_url", "api_key", "model", "fallback_models",
    "timeout_seconds", "temperature", "max_tokens",
}


def detect_legacy_llm(llm_cfg: dict) -> str:
    """
    检测旧版 `llm` 结构。

    返回迁移提示；不是旧结构时返回空串。
    """
    if not isinstance(llm_cfg, dict):
        return ""
    if llm_cfg.get("providers"):
        return ""
    if not any(key in llm_cfg for key in _LEGACY_KEYS):
        return ""
    return (
        "[配置] 检测到旧版 llm 结构（base_url/api_key/model），已不被支持。\n"
        "      全局 llm 改为 providers[]；可用模型请在 "
        "bot.configs.<名称>.llm.models[] 中按优先级声明。\n"
        "      可用 `python main.py --print-config-template` 输出新模板到标准输出。"
    )


def validate_llm_config(llm_cfg: dict, configs_cfg: dict) -> list[str]:
    """
    校验全局端点与各配置的候选引用。

    返回错误列表；空列表表示通过。启动与 `/maa reload` 都应调用。
    """
    errors: list[str] = []
    try:
        global_cfg = _parse_global(llm_cfg)
    except Exception as exc:
        return [f"llm 段无效：{exc}"]

    providers: dict[str, LLMProviderConfig] = {}
    for index, provider in enumerate(global_cfg.providers):
        name = str(provider.name or "").strip()
        if not name:
            errors.append(f"llm.providers[{index}] 缺少 name")
            continue
        if name in providers:
            errors.append(f"llm.providers 中端点名重复：{name}")
            continue
        providers[name] = provider
        if not str(provider.base_url or "").strip():
            errors.append(f"端点 {name} 缺少 base_url")
        model_names = [str(m.model or "").strip() for m in provider.models]
        if not any(model_names):
            errors.append(f"端点 {name} 未声明任何模型")
        duplicates = {m for m in model_names if m and model_names.count(m) > 1}
        if duplicates:
            errors.append(f"端点 {name} 中模型重复：{', '.join(sorted(duplicates))}")

    if not providers:
        errors.append("llm.providers 为空，AI 分析无法工作")

    if not isinstance(configs_cfg, dict):
        return errors
    for config_name, spec in configs_cfg.items():
        if not isinstance(spec, dict):
            continue
        llm_spec = spec.get("llm") or {}
        if not isinstance(llm_spec, dict):
            errors.append(f"配置「{config_name}」的 llm 段必须是对象")
            continue
        models = llm_spec.get("models") or []
        if not models:
            errors.append(
                f"配置「{config_name}」未声明 llm.models[]，无法选择模型"
            )
            continue
        seen: set[tuple[str, str]] = set()
        for index, ref in enumerate(models):
            if not isinstance(ref, dict):
                errors.append(f"配置「{config_name}」llm.models[{index}] 必须是对象")
                continue
            name = str(ref.get("name") or "").strip()
            model = str(ref.get("model") or "").strip()
            if not name or not model:
                errors.append(
                    f"配置「{config_name}」llm.models[{index}] 缺少 name 或 model"
                )
                continue
            provider = providers.get(name)
            if provider is None:
                errors.append(
                    f"配置「{config_name}」引用了不存在的端点：{name}"
                )
                continue
            if provider.find_model(model) is None:
                errors.append(
                    f"配置「{config_name}」引用了端点 {name} 上不存在的模型：{model}"
                )
                continue
            key = (name, model)
            if key in seen:
                continue
            seen.add(key)
    return errors


def _parse_global(llm_cfg: dict) -> LLMGlobalConfig:
    cfg = llm_cfg if isinstance(llm_cfg, dict) else {}
    return LLMGlobalConfig.model_validate(cfg)


@dataclass
class Candidate:
    """一个可尝试的候选：端点 + 模型 + 引用。"""

    provider: LLMProviderConfig
    client: LLMClient
    spec: ModelSpec
    ref: ModelRef

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider.name, self.spec.model)

    def label(self) -> str:
        return f"{self.provider.name}/{self.spec.model}"


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    auth_failed: bool = False
    last_error: str = ""


class CircuitOpenError(LLMError):
    """端点处于熔断状态。"""

    def __init__(self, provider: str):
        super().__init__(f"端点 {provider} 熔断中", kind="circuit")


class LLMPool:
    """候选编排 + 端点级熔断 / 并发。"""

    def __init__(self, llm_cfg: dict, *, usage: Optional[UsageStore] = None):
        self.cfg = _parse_global(llm_cfg)
        self.defaults = self.cfg.defaults
        self.breaker_cfg = self.cfg.circuit_breaker
        self.usage = usage
        self.clients: dict[str, LLMClient] = {}
        self.providers: dict[str, LLMProviderConfig] = {}
        self._breakers: dict[str, _BreakerState] = {}
        # 内存中的会话用量（按最近使用淘汰），用于报告页眉/页脚的即时统计
        self._sessions: "OrderedDict[str, SessionUsage]" = OrderedDict()
        self._max_sessions = 200

        for provider in self.cfg.providers:
            name = str(provider.name or "").strip()
            if not name:
                continue
            self.providers[name] = provider
            self.clients[name] = LLMClient(provider)
            self._breakers[name] = _BreakerState()

    # ── 描述 ──

    @property
    def configured(self) -> bool:
        return any(client.configured for client in self.clients.values())

    def describe_all(self) -> list[str]:
        """启动时打印的端点与能力清单（不含密钥）。"""
        lines: list[str] = []
        for name, provider in self.providers.items():
            client = self.clients[name]
            models = ", ".join(
                f"{spec.model}"
                f"[tools={'y' if spec.supports.tools else 'n'},"
                f"vision={'y' if spec.supports.vision else 'n'}]"
                for spec in provider.models
            ) or "（无模型）"
            lines.append(
                f"端点 {name}: {provider.base_url or '（未配置）'}"
                f" · 密钥指纹 {client.key_fingerprint} · {models}"
            )
        return lines

    # ── 候选解析 ──

    def resolve(self, config_llm: Optional[ConfigLLM],
                need: Iterable[str] = ()) -> list[Candidate]:
        """
        按配置顺序解析候选，并按能力过滤。

        need: ("tools",) / ("vision",) / 空。
        完全相同的相邻候选会被去重（保留首次）。
        """
        config_llm = config_llm or ConfigLLM()
        required = {item for item in need if item}
        result: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        for ref in config_llm.models:
            name = str(ref.name or "").strip()
            model = str(ref.model or "").strip()
            provider = self.providers.get(name)
            client = self.clients.get(name)
            if provider is None or client is None:
                logger.warning(f"[LLM] 跳过不存在的端点：{name}")
                continue
            spec = provider.find_model(model)
            if spec is None:
                logger.warning(f"[LLM] 跳过端点 {name} 上不存在的模型：{model}")
                continue
            if "tools" in required and not spec.supports.tools:
                continue
            if "vision" in required and not spec.supports.vision:
                continue
            key = (name, model)
            if key in seen:
                continue
            seen.add(key)
            result.append(Candidate(provider=provider, client=client,
                                    spec=spec, ref=ref))
        return result

    def has_capability(self, config_llm: Optional[ConfigLLM], need: str) -> bool:
        return bool(self.resolve(config_llm, (need,)))

    # ── 熔断 ──

    def _breaker(self, provider: str) -> _BreakerState:
        return self._breakers.setdefault(provider, _BreakerState())

    def _available(self, provider: str) -> bool:
        state = self._breaker(provider)
        if state.auth_failed:
            return False
        threshold = max(1, int(self.breaker_cfg.failure_threshold or 3))
        if state.failures < threshold:
            return True
        cooldown = max(1, int(self.breaker_cfg.cooldown_seconds or 300))
        if time.monotonic() - state.opened_at >= cooldown:
            # 冷却结束，半开：允许一次尝试
            state.failures = threshold - 1
            return True
        return False

    def _report_success(self, provider: str) -> None:
        state = self._breaker(provider)
        state.failures = 0
        state.opened_at = 0.0
        state.last_error = ""

    def _report_failure(self, provider: str, kind: str, error: str) -> None:
        state = self._breaker(provider)
        state.last_error = error
        if kind == "auth":
            state.auth_failed = True
            logger.error(
                f"[LLM] 端点 {provider} 鉴权失败，本次运行不再使用该端点。"
                "请核对 llm.providers[].api_key。"
            )
            return
        if kind in ("rate", "server", "network", "empty", "other"):
            state.failures += 1
            threshold = max(1, int(self.breaker_cfg.failure_threshold or 3))
            if state.failures >= threshold:
                state.opened_at = time.monotonic()
                cooldown = max(1, int(self.breaker_cfg.cooldown_seconds or 300))
                logger.warning(
                    f"[LLM] 端点 {provider} 连续失败 {state.failures} 次，"
                    f"熔断 {cooldown}s"
                )

    # ── 参数合并 ──

    def _params(self, candidate: Candidate, config_llm: ConfigLLM,
                *, temperature: Optional[float] = None,
                max_tokens: Optional[int] = None,
                top_p: Optional[float] = None,
                max_context_tokens: int = 0) -> RequestParams:
        spec = candidate.spec
        temp = temperature
        if temp is None:
            temp = config_llm.temperature
        if temp is None:
            temp = spec.temperature
        if temp is None:
            temp = self.defaults.temperature

        top = top_p
        if top is None:
            top = config_llm.top_p
        if top is None:
            top = spec.top_p
        if top is None:
            top = self.defaults.top_p

        tokens = max_tokens
        if tokens is None:
            tokens = config_llm.max_tokens
        if tokens is None:
            tokens = spec.max_tokens
        if not tokens:
            tokens = self.defaults.max_tokens

        context = int(max_context_tokens or 0)
        if context <= 0:
            context = int(spec.max_context_tokens or 0)
        if context <= 0:
            context = int(self.defaults.max_context_tokens or 48000)

        return RequestParams(
            temperature=float(temp),
            top_p=float(top),
            max_tokens=max(1, int(tokens)),
            max_context_tokens=max(1024, context),
        )

    def context_budget(self, config_llm: Optional[ConfigLLM],
                       override: int = 0) -> int:
        """本次调用的上下文 token 预算（取第一个候选的配置）。"""
        if override > 0:
            return int(override)
        candidates = self.resolve(config_llm)
        if candidates:
            spec = candidates[0].spec
            if spec.max_context_tokens > 0:
                return int(spec.max_context_tokens)
        return int(self.defaults.max_context_tokens or 48000)

    # ── 调用 ──

    async def chat(
        self,
        config_llm: Optional[ConfigLLM],
        messages: list[dict[str, Any]],
        *,
        config_name: str = "",
        need: Iterable[str] = (),
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        max_context_tokens: int = 0,
        phase: str = "",
        use_breaker: bool = True,
        session_id: str = "",
    ) -> Optional[ChatMessage]:
        """
        按候选顺序尝试，返回第一个成功结果；全部失败返回 None。

        上下文超限会抛出 `ContextOverflowError`（不切换候选）。
        """
        config_llm = config_llm or ConfigLLM()
        context_session, context_config, context_file = _SESSION.get()
        session_id = str(session_id or context_session)
        record_config = config_name or context_config
        record_file = context_file if context_session else ""
        candidates = self.resolve(config_llm, need)
        if not candidates:
            logger.error(
                f"[LLM] 配置「{config_name or '未知'}」没有可用候选"
                f"（能力要求：{','.join(sorted(need)) or '无'}）"
            )
            return None

        for candidate in candidates:
            if use_breaker and not self._available(candidate.provider.name):
                logger.info(f"[LLM] 跳过熔断中的端点：{candidate.provider.name}")
                continue

            params = self._params(
                candidate, config_llm,
                temperature=temperature, max_tokens=max_tokens, top_p=top_p,
                max_context_tokens=max_context_tokens,
            )
            attempts = max(0, int(candidate.provider.max_retries or 0)) + 1
            for attempt in range(1, attempts + 1):
                started = time.monotonic()
                try:
                    reply = await candidate.client.chat_messages(
                        messages,
                        model=candidate.spec.model,
                        spec=candidate.spec,
                        params=params,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    elapsed = int((time.monotonic() - started) * 1000)
                    if use_breaker:
                        self._report_success(candidate.provider.name)
                    self._record_usage(
                        UsageRecord(
                            session_id=session_id,
                            config=record_config,
                            file=record_file,
                            provider=candidate.provider.name,
                            model=candidate.spec.model,
                            phase=phase,
                            ok=True,
                            prompt_tokens=int(reply.usage.get("prompt_tokens") or 0),
                            completion_tokens=int(reply.usage.get("completion_tokens") or 0),
                            total_tokens=int(reply.usage.get("total_tokens") or 0),
                            elapsed_ms=elapsed,
                        ),
                        messages,
                    )
                    return reply
                except ContextOverflowError:
                    logger.warning(
                        f"[LLM] {candidate.label()} 上下文超限，交给上层压缩"
                    )
                    if use_breaker:
                        self._report_success(candidate.provider.name)
                    raise
                except LLMError as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    self._record_usage(
                        UsageRecord(
                            session_id=session_id,
                            config=record_config,
                            file=record_file,
                            provider=candidate.provider.name,
                            model=candidate.spec.model,
                            phase=phase,
                            ok=False,
                            error_kind=exc.kind,
                            elapsed_ms=elapsed,
                        ),
                        messages,
                    )
                    logger.warning(
                        f"[LLM] {candidate.label()} 第 {attempt} 次失败"
                        f"（{exc.kind}）：{exc}"
                    )
                    if use_breaker:
                        self._report_failure(
                            candidate.provider.name, exc.kind, str(exc)
                        )
                    if exc.kind in ("server", "network", "rate") and attempt < attempts:
                        await asyncio.sleep(backoff_seconds(attempt, exc.retry_after))
                        continue
                    break
                except Exception as exc:  # 防御性兜底
                    logger.warning(
                        f"[LLM] {candidate.label()} 未预期异常：{exc}", exc_info=True
                    )
                    self._report_failure(candidate.provider.name, "other", str(exc))
                    break

        logger.error(
            f"[LLM] 配置「{config_name or '未知'}」的全部候选均失败"
        )
        return None

    async def chat_text(
        self,
        config_llm: Optional[ConfigLLM],
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Optional[str]:
        reply = await self.chat(config_llm, messages, **kwargs)
        return reply.content if reply is not None else None

    def _record_usage(self, record: UsageRecord,
                      messages: list[dict[str, Any]]) -> None:
        if record.ok and not record.prompt_tokens:
            from core.tokens import estimate_messages_tokens

            record.prompt_tokens = estimate_messages_tokens(messages)
            if not record.total_tokens:
                record.total_tokens = (record.prompt_tokens
                                       + int(record.completion_tokens or 0))
        if record.session_id:
            self._accumulate_session(record)
        if self.usage is None:
            return
        try:
            self.usage.record(record)
        except Exception as exc:
            logger.debug(f"[用量] 记录失败：{exc}")

    def _accumulate_session(self, record: UsageRecord) -> None:
        """把一次调用并入内存中的会话统计（用于报告页眉/页脚）。"""
        group = self._sessions.get(record.session_id)
        if group is None:
            group = SessionUsage(
                session_id=record.session_id,
                config=record.config,
                file=record.file,
            )
            self._sessions[record.session_id] = group
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        group.add(record)
        self._sessions.move_to_end(record.session_id)

    def session_usage(self, session_id: str) -> Optional[SessionUsage]:
        """取某个会话的用量；未知会话返回 None。"""
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def session_summary(self, session_id: str) -> dict[str, Any]:
        """
        会话用量摘要（供模板占位符使用）。

        未知会话返回全 0 的摘要，模板始终可以安全渲染。
        """
        group = self.session_usage(session_id)
        if group is None:
            return {
                "calls": 0, "failed": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "elapsed_ms": 0,
                "providers": [], "models": [], "pairs": {},
            }
        return {
            "calls": group.calls,
            "failed": group.failed,
            "prompt_tokens": group.prompt_tokens,
            "completion_tokens": group.completion_tokens,
            "total_tokens": group.total(),
            "elapsed_ms": group.elapsed_ms,
            "providers": sorted(group.providers),
            "models": sorted(group.models),
            "pairs": dict(group.pairs),
        }

    async def close(self) -> None:
        """关闭所有端点连接池。"""
        clients = list(self.clients.values())
        self.clients = {}
        for client in clients:
            try:
                await client.close()
            except Exception as exc:
                logger.debug(f"[LLM] 关闭连接池失败：{exc}")
