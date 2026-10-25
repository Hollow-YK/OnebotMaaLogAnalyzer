"""
日志上下文 — 让每条日志带上当前配置名。

多个配置（如 MAK / MAN）会并发处理日志包，同一段代码的日志交织在一起时
无法判断属于哪个配置。用 ContextVar 记录当前配置，由格式化器自动补上
`[配置名]` 前缀；它随 asyncio 任务自动继承，因此并发的分析不会串号。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_CURRENT_CONFIG: ContextVar[str] = ContextVar("maa_log_config", default="")


def current_config_name() -> str:
    """当前日志上下文里的配置名（无上下文时为空字符串）。"""
    return _CURRENT_CONFIG.get()


@contextmanager
def config_log_context(name: str) -> Iterator[None]:
    """
    在 with 块内（以及其派生的任务）打的日志都标注配置名。

    已处于某个配置的上下文时保持最外层，避免嵌套调用反复改写前缀。
    """
    value = str(name or "").strip()
    if not value or _CURRENT_CONFIG.get():
        yield
        return
    token = _CURRENT_CONFIG.set(value)
    try:
        yield
    finally:
        _CURRENT_CONFIG.reset(token)


class ConfigNameFormatter(logging.Formatter):
    """把当前配置名注入格式串的 `%(config_prefix)s` 占位符。"""

    def format(self, record: logging.LogRecord) -> str:
        name = getattr(record, "config_name", None)
        if not name:
            name = _CURRENT_CONFIG.get()
        record.config_prefix = f" [{name}]" if name else ""
        return super().format(record)
