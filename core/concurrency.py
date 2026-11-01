"""
并发原语 — 把「谁和谁必须排队」集中成可复用的工具。

本项目采用「按配置隔离 + 配置内排队」的模型（见 docs/architecture.md「并发模型」）：

- **不同配置互不影响**：每个配置一份状态（信号量、仓库、缓存、追问策略），
  键是配置名；配置之间没有共享的可变运行时状态。
- **同一配置内排队**：一个配置的分析、追问、仓库操作共用同一把工作锁，
  不并行执行——仓库工作区、历史目录、报告渲染都归该配置独占。
- **按资源独占**：渲染浏览器这类按资源（data_dir）共享的东西，
  用资源键排队，而不是配置键。
- **阻塞 I/O 不占事件循环**：压缩包解压、目录遍历、写盘一律下沉到线程，
  否则一个配置解压大日志会卡住所有配置。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class KeyedLocks:
    """按 key 分发 asyncio.Lock：同一个 key 串行，不同 key 互不等待。

    `asyncio.Lock` 与事件循环绑定：同一进程里可能出现多个循环（`asyncio.run`
    被反复调用、调试套件逐个场景重建运行时、测试框架每个用例一个循环）。
    所以这里连锁所属的循环一起记下来，循环换了就换一把新锁，避免
    `is bound to a different event loop`。
    """

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.AbstractEventLoop | None,
                                     asyncio.Lock]] = {}

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def get(self, key: str) -> asyncio.Lock:
        loop = self._running_loop()
        entry = self._locks.get(key)
        if entry is not None:
            bound, lock = entry
            # 同一循环（或没有循环参与的同步调用）复用；换了循环则重建
            if bound is loop or bound is None or loop is None:
                return lock
        lock = asyncio.Lock()
        self._locks[key] = (loop, lock)
        return lock

    def discard(self, key: str) -> bool:
        """空闲时回收一把锁（配置被删除时调用）；仍在使用则保留。"""
        entry = self._locks.get(key)
        if entry is None:
            return False
        lock = entry[1]
        if lock.locked() or lock._waiters:
            return False
        self._locks.pop(key, None)
        return True

    def busy_depth(self, key: str) -> int:
        """该 key 上「排队 + 正在执行」的任务数，仅用于状态展示。"""
        entry = self._locks.get(key)
        if entry is None:
            return 0
        lock = entry[1]
        return len(lock._waiters or ()) + (1 if lock.locked() else 0)

    def keys(self) -> list[str]:
        return list(self._locks)

    def keys(self) -> list[str]:
        return list(self._locks)


class KeyedThreadLocks:
    """按 key 分发 threading.Lock：给在线程池里跑的阻塞代码排队。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


_LIMITERS: dict[int, asyncio.Semaphore] = {}
_LIMITER_GUARD = threading.Lock()


def _limiter(limit: int) -> asyncio.Semaphore:
    with _LIMITER_GUARD:
        sem = _LIMITERS.get(limit)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            _LIMITERS[limit] = sem
        return sem


async def run_blocking(func: Callable[..., T], /, *args: Any,
                       limit: int = 0, **kwargs: Any) -> T:
    """在线程里执行阻塞函数，不占用事件循环。

    limit > 0 时用共享信号量限制同时运行的线程数（默认交给 Python 线程池）。
    共享信号量按上限值复用，因此不会随调用点增长。

    注意：`func` 必须是纯阻塞实现；需要共享可变状态（如 zipfile 句柄）时，
    调用方自己用 threading.Lock 串行化。
    """
    if limit > 0:
        async with _limiter(limit):
            return await asyncio.to_thread(func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)