"""
AI 用量记录 — JSONL 追加写入 + 读取查询。

记录字段：
  ts / session_id / config / file / provider / model / phase / ok / error_kind
  prompt_tokens / completion_tokens / total_tokens / elapsed_ms

会话（session）= 一次日志分析 + 它后续的全部追问，共用同一个 `session_id`。
`/maa ai` 按会话聚合，而不是按单次模型调用罗列。

只写 provider 名与模型名，不写密钥（密钥只用于指纹，见 LLMClient）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("Maa.Usage")

_MAX_READ_BYTES = 4 * 1024 * 1024


@dataclass
class UsageRecord:
    """一次模型调用的用量记录。"""

    ts: float = field(default_factory=time.time)
    # 一次分析 + 其全部追问共用；空 = 旧记录，只能按单次调用统计
    session_id: str = ""
    config: str = ""
    file: str = ""
    provider: str = ""
    model: str = ""
    phase: str = ""
    ok: bool = True
    error_kind: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionUsage:
    """一个会话（分析 + 追问）的用量汇总。"""

    session_id: str = ""
    config: str = ""
    file: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    calls: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0
    providers: dict[str, int] = field(default_factory=dict)   # 名称 → 调用次数
    models: dict[str, int] = field(default_factory=dict)
    pairs: dict[str, int] = field(default_factory=dict)       # provider/model → 次数
    phases: dict[str, int] = field(default_factory=dict)      # analysis / followup ...

    def total(self) -> int:
        if self.total_tokens:
            return self.total_tokens
        return int(self.prompt_tokens + self.completion_tokens)

    def add(self, record: UsageRecord) -> None:
        """把一条调用记录并入本会话。"""
        if not self.started_at or record.ts < self.started_at:
            self.started_at = record.ts
        self.updated_at = max(self.updated_at, record.ts)
        self.calls += 1
        if not record.ok:
            self.failed += 1
        self.prompt_tokens += int(record.prompt_tokens or 0)
        self.completion_tokens += int(record.completion_tokens or 0)
        self.total_tokens += int(
            record.total_tokens
            or (int(record.prompt_tokens or 0) + int(record.completion_tokens or 0))
        )
        self.elapsed_ms += int(record.elapsed_ms or 0)
        if record.provider:
            self.providers[record.provider] = self.providers.get(record.provider, 0) + 1
        if record.model:
            self.models[record.model] = self.models.get(record.model, 0) + 1
        if record.provider and record.model:
            pair = f"{record.provider}/{record.model}"
            self.pairs[pair] = self.pairs.get(pair, 0) + 1
        if record.phase:
            self.phases[record.phase] = self.phases.get(record.phase, 0) + 1
        if not self.config and record.config:
            self.config = record.config
        if not self.file and record.file:
            self.file = record.file

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def pair_label(self) -> str:
        """形如 `main/deepseek-v4.1-flash ×12` 的使用情况。"""
        if not self.pairs:
            return "-"
        return " · ".join(
            f"{pair} ×{count}"
            for pair, count in sorted(
                self.pairs.items(), key=lambda item: (-item[1], item[0])
            )
        )

    def phase_split(self) -> str:
        """分析 / 追问的调用次数拆分。"""
        analysis = sum(
            count for phase, count in self.phases.items()
            if phase.startswith("analysis")
        )
        followup = sum(
            count for phase, count in self.phases.items()
            if phase.startswith("followup")
        )
        parts: list[str] = []
        if analysis:
            parts.append(f"分析 {analysis} 次")
        if followup:
            parts.append(f"追问 {followup} 次")
        return " · ".join(parts) or f"{self.calls} 次调用"


class UsageStore:
    """按数据目录追加 JSONL 用量记录。"""

    def __init__(self, data_dir: str | Path = "data"):
        self.path = Path(data_dir) / "ai_usage.jsonl"
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        """追加一条记录（失败只记日志，不影响分析）。"""
        try:
            line = json.dumps(record.to_dict(), ensure_ascii=False)
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            logger.debug(f"[用量] 写入失败：{exc}")

    def sessions(self, limit: int = 10) -> list[SessionUsage]:
        """
        按会话聚合最近的记录（按最近活动倒序）。

        同一次分析与其后续追问共用 `session_id`，因此会被归为一组。
        旧记录没有 `session_id`，各自单独成组（在展示上标注为单次调用）。
        """
        records = self._read_all()
        groups: dict[str, SessionUsage] = {}
        anonymous: list[SessionUsage] = []
        for index, record in enumerate(records):
            if record.session_id:
                group = groups.get(record.session_id)
                if group is None:
                    group = SessionUsage(
                        session_id=record.session_id,
                        config=record.config,
                        file=record.file,
                    )
                    groups[record.session_id] = group
                group.add(record)
                continue
            single = SessionUsage(
                session_id=f"legacy:{index}",
                config=record.config,
                file=record.file,
            )
            single.add(record)
            anonymous.append(single)

        merged = list(groups.values()) + anonymous
        merged.sort(key=lambda item: item.updated_at, reverse=True)
        return merged[:max(1, int(limit or 10))]

    def _read_all(self) -> list[UsageRecord]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
            with self.path.open("rb") as handle:
                if size > _MAX_READ_BYTES:
                    handle.seek(size - _MAX_READ_BYTES)
                raw = handle.read().decode("utf-8", "ignore")
        except OSError as exc:
            logger.debug(f"[用量] 读取失败：{exc}")
            return []

        records: list[UsageRecord] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            record = UsageRecord()
            for key in record.__dataclass_fields__:
                if key in data:
                    setattr(record, key, data[key])
            records.append(record)
        return records


def format_tokens(value: Optional[int]) -> str:
    """把 token 数格式化为易读文本。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)
