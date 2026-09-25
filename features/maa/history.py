"""
分析历史 — 持久化「用于追问的消息 ID + 日志压缩包 + 报告」，并定期清理。

保留策略（按周期计算，不是滑动窗口）:
  每过一个 period_hours 检查一次，删除创建时间超过
  period_hours × keep_periods 的记录。

  默认 1d 周期 / 保留 2 周期 → 记录可存活 2~3 天，
  因此这段时间内的日志都还能被追问。

存储布局:
  data/<配置名>/history/
    ├── records.json          # 全部记录的元数据
    └── <记录ID>/
        ├── source.zip        # 日志压缩包原文
        └── context.txt       # 追问上下文（日志摘要 + 代码参考）

注意:
  - git 仓库缓存（data/repos）**不属于**历史记录，本模块不会触碰
  - 除周期清理外，另有 max_records / max_total_mb 两个安全上限，
    防止磁盘被意外写满
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Maa.History")

# 记录元数据文件名
_RECORDS_FILE = "records.json"
_CONTEXT_FILE = "context.txt"
_ZIP_FILE = "source.zip"
# 追问会话状态（按「群 + 记录」保存，使重启后仍能继续追问）
_FOLLOWUP_FILE = "followups.json"

# 历史目录名（位于各配置目录下）
HISTORY_DIR_NAME = "history"


@dataclass
class HistoryRecord:
    """一次分析的历史记录。"""

    id: str
    config_name: str
    group_id: str
    file_name: str
    file_id: str = ""
    uploader: str = ""
    created_at: float = 0.0
    size_bytes: int = 0
    log_count: int = 0
    image_count: int = 0
    # 本次分析 Bot 发出的消息 ID（引用它们即可追问）
    message_ids: list[str] = field(default_factory=list)
    report: str = ""
    has_context: bool = False
    has_zip: bool = False

    def age_hours(self) -> float:
        return max(0.0, (time.time() - self.created_at) / 3600.0)


@dataclass
class CleanupResult:
    """一次清理的结果。"""

    removed: int = 0
    freed_bytes: int = 0
    kept: int = 0
    reason: str = ""


class HistoryStore:
    """按配置管理分析历史，并负责定期清理。"""

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._records: dict[str, list[HistoryRecord]] = {}
        self._followups: dict[str, dict] = {}
        self._loaded = False
        self.last_cleanup: float = 0.0

    # ════════════════════════════════════════════════════════════
    # 目录
    # ════════════════════════════════════════════════════════════

    def _config_dir(self, config_name: str) -> Path:
        safe = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(config_name)
        )[:60] or "default"
        return self._data_dir / safe / HISTORY_DIR_NAME

    def _records_path(self, config_name: str) -> Path:
        return self._config_dir(config_name) / _RECORDS_FILE

    def _followups_path(self, config_name: str) -> Path:
        return self._config_dir(config_name) / _FOLLOWUP_FILE

    def record_dir(self, config_name: str, record_id: str) -> Path:
        return self._config_dir(config_name) / str(record_id)

    # ════════════════════════════════════════════════════════════
    # 读写
    # ════════════════════════════════════════════════════════════

    def load(self, config_name: str) -> list[HistoryRecord]:
        """读取某配置的历史记录（带缓存）。"""
        if config_name in self._records:
            return self._records[config_name]

        path = self._records_path(config_name)
        records: list[HistoryRecord] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    known = set(HistoryRecord.__dataclass_fields__.keys())
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        records.append(HistoryRecord(
                            **{k: v for k, v in item.items() if k in known}
                        ))
            except Exception as exc:
                logger.warning(f"[历史] {config_name} records.json 读取失败：{exc}")
        self._records[config_name] = records
        return records

    def save(self, config_name: str) -> None:
        """持久化某配置的历史记录（原子写）。"""
        records = self._records.get(config_name)
        if records is None:
            return
        path = self._records_path(config_name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning(f"[历史] {config_name} records.json 写入失败：{exc}")

    # ════════════════════════════════════════════════════════════
    # 追问会话持久化
    # ════════════════════════════════════════════════════════════

    def load_followups(self, config_name: str) -> dict:
        """读取某配置的追问会话状态（带缓存）。"""
        if config_name in self._followups:
            return self._followups[config_name]

        path = self._followups_path(config_name)
        data: dict = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {
                        str(k): v for k, v in raw.items()
                        if isinstance(v, dict)
                    }
            except Exception as exc:
                logger.warning(f"[历史] {config_name} followups.json 读取失败：{exc}")
        self._followups[config_name] = data
        return data

    def save_followups(self, config_name: str) -> None:
        """持久化某配置的追问会话状态（原子写）。"""
        data = self._followups.get(config_name)
        if data is None:
            return
        path = self._followups_path(config_name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning(f"[历史] {config_name} followups.json 写入失败：{exc}")

    def set_followup(self, config_name: str, key: str,
                     state: dict) -> None:
        """保存（或覆盖）某个追问会话状态。key 通常为历史记录 ID。"""
        if not config_name or not key:
            return
        self.load_followups(config_name)[str(key)] = dict(state or {})
        self.save_followups(config_name)

    def get_followup(self, config_name: str, key: str) -> Optional[dict]:
        """取出某个追问会话状态。"""
        return self.load_followups(config_name).get(str(key))

    def remove_followup(self, config_name: str, key: str) -> None:
        """删除某个追问会话状态（会话结束 / 历史记录被清理时调用）。"""
        data = self.load_followups(config_name)
        if data.pop(str(key), None) is not None:
            self.save_followups(config_name)

    # ════════════════════════════════════════════════════════════
    # 新增记录
    # ════════════════════════════════════════════════════════════

    def add(self, *, config_name: str, group_id: str, file_name: str,
            source_zip: Optional[Path] = None, context: str = "",
            report: str = "", message_ids: Optional[list] = None,
            file_id: str = "", uploader: str = "",
            log_count: int = 0, image_count: int = 0) -> Optional[HistoryRecord]:
        """
        新增一条历史记录：把日志包与上下文归档到历史目录。

        任何失败都只记日志并返回 None，不影响分析结果。
        """
        try:
            record_id = f"{int(time.time())}_{len(self.load(config_name)) + 1}"
            target = self.record_dir(config_name, record_id)
            target.mkdir(parents=True, exist_ok=True)

            size_bytes = 0
            has_zip = False
            if source_zip is not None and Path(source_zip).exists():
                dest = target / _ZIP_FILE
                # 同一文件系统下用 move，避免复制大文件
                try:
                    Path(source_zip).replace(dest)
                except OSError:
                    shutil.copy2(source_zip, dest)
                    Path(source_zip).unlink(missing_ok=True)
                size_bytes = dest.stat().st_size
                has_zip = True

            has_context = False
            if context:
                ctx_path = target / _CONTEXT_FILE
                ctx_path.write_text(context, encoding="utf-8")
                size_bytes += ctx_path.stat().st_size
                has_context = True

            record = HistoryRecord(
                id=record_id,
                config_name=config_name,
                group_id=str(group_id),
                file_name=file_name,
                file_id=str(file_id or ""),
                uploader=str(uploader or ""),
                created_at=time.time(),
                size_bytes=size_bytes,
                log_count=int(log_count),
                image_count=int(image_count),
                message_ids=[str(m) for m in (message_ids or []) if m],
                report=report,
                has_context=has_context,
                has_zip=has_zip,
            )
            self.load(config_name).append(record)
            self.save(config_name)
            logger.info(
                f"[历史] 已归档 #{record_id} {file_name}"
                f"（zip={has_zip} context={has_context}，{size_bytes} 字节）"
            )
            return record
        except Exception as exc:
            logger.warning(f"[历史] 归档失败，已跳过：{exc}", exc_info=True)
            return None

    # ════════════════════════════════════════════════════════════
    # 查询
    # ════════════════════════════════════════════════════════════

    def find_by_message_id(self, config_name: str,
                           message_id) -> Optional[HistoryRecord]:
        """通过消息 ID 反查历史记录（用于跨会话追问）。"""
        mid = str(message_id or "").strip()
        if not mid:
            return None
        for record in reversed(self.load(config_name)):
            if mid in record.message_ids:
                return record
        return None

    def find_by_id(self, config_name: str,
                   record_id: str) -> Optional[HistoryRecord]:
        """通过记录 ID 定位历史记录。"""
        rid = str(record_id or "").strip()
        if not rid:
            return None
        for record in self.load(config_name):
            if record.id == rid:
                return record
        return None

    def find_latest_by_group(self, config_name: str,
                             group_id: str) -> Optional[HistoryRecord]:
        """取某群最近一条历史记录（用于恢复追问会话）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return None
        for record in reversed(self.load(config_name)):
            if record.group_id == gid:
                return record
        return None

    def add_message_ids(self, config_name: str, record_id: str,
                        message_ids: Optional[list]) -> bool:
        """
        把消息 ID 追加到历史记录（去重）并落盘。

        追问回复的消息 ID 必须一并归档，否则重启后引用追问回复无法定位会话。
        """
        record = self.find_by_id(config_name, record_id)
        if record is None:
            return False
        changed = False
        existing = {str(m) for m in record.message_ids}
        for mid in (message_ids or []):
            mid = str(mid or "").strip()
            if mid and mid not in existing:
                record.message_ids.append(mid)
                existing.add(mid)
                changed = True
        if changed:
            self.save(config_name)
        return changed

    def list_configs(self) -> list[str]:
        """列出已有历史记录的配置名（含仅存在于磁盘、当前未加载的）。"""
        names = set(self._records)
        try:
            for child in self._data_dir.iterdir():
                if child.is_dir() and (child / HISTORY_DIR_NAME).is_dir():
                    names.add(child.name)
        except OSError:
            pass
        return sorted(n for n in names if n)

    def read_context(self, config_name: str, record: HistoryRecord) -> str:
        """读取记录归档的追问上下文。"""
        if not record.has_context:
            return ""
        path = self.record_dir(config_name, record.id) / _CONTEXT_FILE
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"[历史] 读取上下文失败 #{record.id}：{exc}")
            return ""

    def read_zip(self, config_name: str, record: HistoryRecord) -> Optional[Path]:
        """返回记录归档的日志包路径（不存在时 None）。"""
        if not record.has_zip:
            return None
        path = self.record_dir(config_name, record.id) / _ZIP_FILE
        return path if path.exists() else None

    # ════════════════════════════════════════════════════════════
    # 清理
    # ════════════════════════════════════════════════════════════

    def cleanup(self, config_name: str, *, period_hours: int = 24,
                keep_periods: int = 2, max_records: int = 200,
                max_total_mb: int = 4096, force: bool = False) -> CleanupResult:
        """
        清理过期记录。

        周期语义：删除创建时间超过 period_hours × keep_periods 的记录。
        keep_periods <= 0 表示全部记录都过期（清空）。
        force=False 时，距上次清理不足一个周期则跳过。
        """
        result = CleanupResult()
        period = max(1, int(period_hours or 24))
        keep = int(keep_periods or 0)

        now = time.time()
        if not force and self.last_cleanup:
            if (now - self.last_cleanup) < period * 3600:
                result.kept = len(self.load(config_name))
                result.reason = "未到清理周期"
                return result

        records = self.load(config_name)
        if not records:
            self.last_cleanup = now
            result.reason = "无记录"
            return result

        # keep_periods <= 0 → 全部视为过期
        cutoff = (now + 1.0) if keep <= 0 else (now - keep * period * 3600)
        expired = [r for r in records if r.created_at < cutoff]
        survivors = [r for r in records if r.created_at >= cutoff]

        # 安全上限 1：记录条数（保留最新的）
        if max_records > 0 and len(survivors) > max_records:
            survivors.sort(key=lambda r: r.created_at)
            extra = survivors[: len(survivors) - max_records]
            expired.extend(extra)
            survivors = survivors[len(survivors) - max_records:]
            result.reason = "超出记录数上限"

        # 安全上限 2：历史目录总大小（从最旧开始删）
        if max_total_mb > 0:
            budget = max_total_mb * 1024 * 1024
            total = sum(r.size_bytes for r in survivors)
            if total > budget:
                survivors.sort(key=lambda r: r.created_at)
                while survivors and total > budget:
                    victim = survivors.pop(0)
                    total -= victim.size_bytes
                    expired.append(victim)
                result.reason = "超出历史目录大小上限"

        if not expired:
            self.last_cleanup = now
            result.kept = len(survivors)
            return result

        for record in expired:
            result.freed_bytes += self._remove_record(config_name, record)
            result.removed += 1
            # 记录已删除：对应的追问会话状态也没有意义了
            self.remove_followup(config_name, record.id)

        self._records[config_name] = survivors
        self.save(config_name)
        self.last_cleanup = now
        result.kept = len(survivors)

        logger.info(
            f"[历史] {config_name} 清理完成：删除 {result.removed} 条"
            f"（释放 {result.freed_bytes // 1024} KB），保留 {result.kept} 条"
            + (f"（{result.reason}）" if result.reason else "")
        )
        return result

    def _remove_record(self, config_name: str, record: HistoryRecord) -> int:
        """删除单条记录的目录，返回释放的字节数。"""
        target = self.record_dir(config_name, record.id)
        # 安全校验：只删历史目录内、且以记录 ID 命名的子目录
        try:
            root = self._config_dir(config_name).resolve()
            resolved = target.resolve()
            if root not in resolved.parents:
                logger.warning(f"[历史] 拒绝删除越界路径：{resolved}")
                return 0
        except OSError:
            return 0

        freed = record.size_bytes
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        return freed

    def cleanup_all(self, settings_of: dict, *, force: bool = False) -> dict:
        """
        对所有已知配置执行清理。

        settings_of: {配置名: AnalysisSettings}，用于取各自的周期配置。
        """
        summary: dict[str, CleanupResult] = {}
        for name, settings in settings_of.items():
            if not getattr(settings, "history_enabled", True):
                continue
            summary[name] = self.cleanup(
                name,
                period_hours=getattr(settings, "history_period_hours", 24),
                keep_periods=getattr(settings, "history_keep_periods", 2),
                max_records=getattr(settings, "history_max_records", 200),
                max_total_mb=getattr(settings, "history_max_total_mb", 4096),
                force=force,
            )
        return summary

    def stats(self, config_name: str) -> dict:
        """历史使用情况，供指令展示。"""
        records = self.load(config_name)
        return {
            "count": len(records),
            "bytes": sum(r.size_bytes for r in records),
            "oldest": min((r.created_at for r in records), default=0),
            "newest": max((r.created_at for r in records), default=0),
        }
