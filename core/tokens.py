"""
轻量 token 估算与裁剪 — 不依赖任何分词库。

模型侧的真实 token 数只能由端点返回；这里用启发式估算，用于：
  - 上下文预算（避免每轮重发全量历史导致超限）
  - 工具结果裁剪
  - 摘要提示词的优先级预算

估算规则（保守，宁多勿少）：
  - CJK 字符按 1 token/字
  - 其他字符按 4 字符/token
"""
from __future__ import annotations

from typing import Iterable, Optional

# 工具结果的最小预算：低于该值时裁剪说明本身就占满预算，
# 只会得到空结果，属于配置错误而不是有效预算。
MIN_TOOL_RESULT_TOKENS = 64

# 单张图片的输入估算。DeepSeek 官方视觉计价对单图上限定为 1024 tokens，
# OpenAI 高细节模式同尺寸图片与之同量级；按 base64 长度折算（约 4 字符/token）
# 会把一张 800 KB 的截图估成 20 万 token，直接触发无意义的压缩与裁剪。
IMAGE_PART_TOKENS = 1024

_CJK_RANGES = (
    (0x2E80, 0x2FFF),    # 部首扩展 / 康熙部首
    (0x3000, 0x303F),    # CJK 标点
    (0x3040, 0x30FF),    # 日文假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xF900, 0xFAFF),    # 兼容表意文字
    (0xFF00, 0xFFEF),    # 全角字符
    (0x20000, 0x2FA1F),  # CJK 扩展 B+
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（空串返回 0）。"""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


def estimate_messages_tokens(messages: Iterable[dict]) -> int:
    """估算一组 chat messages 的 token 数（含角色与工具字段的开销）。"""
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += 4
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "image", "input_image"):
                    # 图片按固定视觉开销估算，不按 base64 长度折算
                    total += IMAGE_PART_TOKENS
                    continue
                total += estimate_tokens(str(part.get("text") or ""))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                function = call.get("function") or {}
                total += estimate_tokens(str(function.get("name") or ""))
                total += estimate_tokens(str(function.get("arguments") or ""))
    return total


def _fit_prefix(text: str, budget: int) -> str:
    """
    取最长前缀，使其估算 token 不超过 `budget`。

    `estimate_tokens` 随文本长度单调不减，因此可以二分字符长度——
    一次 `O(log n)` 收敛，绝不会像按比例回缩那样卡在固定下限上。
    """
    if not text or budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _fit_suffix(text: str, budget: int) -> str:
    """取最长后缀，使其估算 token 不超过 `budget`（同 `_fit_prefix`）。"""
    if not text or budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[-middle:]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[len(text) - low:] if low else ""


def trim_to_tokens(text: str, max_tokens: int,
                   suffix: str = "\n...[已截断]") -> str:
    """
    把文本裁剪到 token 预算内（保留头部，末尾加说明）。

    先为说明预留预算再二分取前缀，因此结果一定落在预算内且**必定终止**。
    预算连说明都放不下时返回空串——调用方按“无可用内容”处理，
    这比无限重试或输出一段本身超预算的文本更安全。
    """
    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    suffix_cost = estimate_tokens(suffix)
    if suffix_cost >= max_tokens:
        return ""
    head = _fit_prefix(text, max_tokens - suffix_cost)
    if not head:
        return ""
    return head.rstrip() + suffix


def fold_text(text: str, max_tokens: int,
              head_ratio: float = 0.4,
              suffix: str = "\n...[中间内容已折叠，仅保留首尾]") -> str:
    """
    折叠超长文本：保留开头与结尾，把中间替换为省略说明。

    用于工具结果——报错堆栈的结尾往往比中间更重要。
    说明的预算先预留，剩余部分按 `head_ratio` 二分取首尾，
    因此结果不会超过预算，也不会出现“回缩到下限后永不收敛”。
    """
    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    suffix_cost = estimate_tokens(suffix)
    if suffix_cost >= max_tokens:
        # 连说明都放不下：只保留头部（不使用这个更长的说明）
        return trim_to_tokens(text, max_tokens)

    kept = max_tokens - suffix_cost
    ratio = max(0.0, min(1.0, float(head_ratio)))
    head_budget = max(1, int(kept * ratio))
    tail_budget = max(0, kept - head_budget)
    head = _fit_prefix(text, head_budget)
    tail = _fit_suffix(text, tail_budget) if tail_budget else ""
    # 头尾长度之和覆盖全文说明预算比原文还大，此时只保留头部
    if head and tail and len(head) + len(tail) >= len(text):
        tail = ""

    result = (head.rstrip() + suffix + ("\n" + tail.lstrip() if tail else "")).strip()
    if not result:
        return ""
    if estimate_tokens(result) > max_tokens or len(result) >= len(text):
        # 防御：估算偏差或没有变短时退回纯头部裁剪
        return trim_to_tokens(text, max_tokens)
    return result


def tokens_to_chars(tokens: int) -> int:
    """把 token 预算折算为字符上限（用于兼容旧的字符配置）。"""
    return max(0, int(tokens or 0)) * 4


def clamp_tool_result_tokens(tokens: int) -> int:
    """
    规范化工具结果的 token 预算。

    `0` 表示不限制；非 0 时抬到 `MIN_TOOL_RESULT_TOKENS`，
    避免“说明后缀就占满预算”导致工具结果永远是空串。
    """
    value = int(tokens or 0)
    if value <= 0:
        return 0
    return max(MIN_TOOL_RESULT_TOKENS, value)


def context_budget(default_tokens: int, override_tokens: int = 0) -> int:
    """返回生效的上下文 token 预算（override 优先，0 = 用默认值）。"""
    value = int(override_tokens or 0)
    return value if value > 0 else max(1024, int(default_tokens or 0))


def optional_int(value: Optional[int], fallback: int = 0) -> int:
    try:
        return int(value) if value is not None else int(fallback)
    except (TypeError, ValueError):
        return int(fallback)
