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
                if isinstance(part, dict):
                    total += estimate_tokens(str(part.get("text") or ""))
                    # 图片按 data URL 长度折算（Base64 约 4 字符/token）
                    url = part.get("image_url")
                    if isinstance(url, dict):
                        total += estimate_tokens(str(url.get("url") or ""))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                function = call.get("function") or {}
                total += estimate_tokens(str(function.get("name") or ""))
                total += estimate_tokens(str(function.get("arguments") or ""))
    return total


def trim_to_tokens(text: str, max_tokens: int,
                   suffix: str = "\n...[已截断]") -> str:
    """把文本裁剪到 token 预算内（保留头部）。"""
    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    # 粗略换算成字符数后二分，确保裁剪结果仍在预算内
    budget_chars = max(16, int(max_tokens * 4))
    head = text[:budget_chars]
    while head and estimate_tokens(head + suffix) > max_tokens:
        budget_chars = max(16, int(len(head) * 0.8))
        head = text[:budget_chars]
    return head.rstrip() + suffix


def fold_text(text: str, max_tokens: int,
              head_ratio: float = 0.4,
              suffix: str = "\n...[中间内容已折叠，仅保留首尾]") -> str:
    """
    折叠超长文本：保留开头与结尾，把中间替换为省略说明。

    用于工具结果——报错堆栈的结尾往往比中间更重要。
    """
    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    kept_tokens = max(8, max_tokens - estimate_tokens(suffix))
    head_tokens = max(4, int(kept_tokens * max(0.0, min(1.0, head_ratio))))
    tail_tokens = max(4, kept_tokens - head_tokens)

    head_chars = max(16, head_tokens * 4)
    tail_chars = max(16, tail_tokens * 4)
    head = text[:head_chars]
    tail = text[-tail_chars:] if len(text) > head_chars else ""

    # 二分压缩到预算内
    while head and estimate_tokens(head + suffix + tail) > max_tokens:
        if len(head) > len(tail):
            head = head[: max(16, int(len(head) * 0.8))]
        else:
            tail = tail[max(1, len(tail) // 5):]
    return (head.rstrip() + suffix + "\n" + tail.lstrip()).strip()


def tokens_to_chars(tokens: int) -> int:
    """把 token 预算折算为字符上限（用于兼容旧的字符配置）。"""
    return max(0, int(tokens or 0)) * 4


def context_budget(default_tokens: int, override_tokens: int = 0) -> int:
    """返回生效的上下文 token 预算（override 优先，0 = 用默认值）。"""
    value = int(override_tokens or 0)
    return value if value > 0 else max(1024, int(default_tokens or 0))


def optional_int(value: Optional[int], fallback: int = 0) -> int:
    try:
        return int(value) if value is not None else int(fallback)
    except (TypeError, ValueError):
        return int(fallback)
