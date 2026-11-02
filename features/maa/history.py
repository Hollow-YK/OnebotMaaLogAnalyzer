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
    ├── .staging/<会话>/      # 分析进行中的解压暂存（崩溃遗留按周期清理）
    └── <记录ID>/
        ├── source.zip        # 日志压缩包原文
        ├── context.txt       # 追问上下文（日志摘要 + 代码参考）
        └── extracted/        # 已解压的日志文件（与记录同生命周期）

  解压结果跟着记录走：记录还在就能追问，日志工具直接复用 extracted/
  不再重复解压；记录被清理时整目录删除，无需额外回收逻辑。

注意:
  - git 仓库缓存（data/repo）**不属于**历史记录，本模块不会触碰
  - 除周期清理外，另有 max_records / max_total_mb 两个安全上限，
    防止磁盘被意外写满；清理时按记录目录的实际占用刷新 size_bytes，
    因此追问阶段才补解压的 extracted/ 也会计入
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from core.concurrency import run_blocking
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Maa.History")

# 记录元数据文件名
_RECORDS_FILE = "records.json"
_CONTEXT_FILE = "context.txt"
_ZIP_FILE = "source.zip"
# 解压后的日志文件（生命周期与历史记录一致，即“还能追问”的时间）
_EXTRACT_DIR = "extracted"
# 尚未归档（分析进行中）的解压暂存区
_STAGING_DIR = ".staging"
# 追问会话状态（按「群 + 记录」保存，使重启后仍能继续追问）
_FOLLOWUP_FILE = "followups.json"

# 历史目录名（位于各配置目录下）
HISTORY_DIR_NAME = "history"

# 记录目录名：`<纳秒>_<随机后缀>`；老格式 `<秒>_<序号>` 也匹配，便于识别孤儿目录
_RECORD_ID_RE = re.compile(r"^\d+_[0-9a-zA-Z]+$")


def new_record_id() -> str:
    """历史记录 ID：纳秒时间戳 + 随机后缀。

    旧格式 `int(time.time())_<记录数+1>` 在同一秒、以及记录被清空后都会复用，
    于是目录被 `mkdir(exist_ok=True)` 静默复用、归档被覆盖、追问串到别的记录。
    """
    return f"{time.time_ns()}_{uuid.uuid4().hex[:6]}"


def _dir_size(path: Path) -> int:
    """目录内所有文件的总字节数（用于历史容量统计）。"""
    total = 0
    try:
        for item in Path(path).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


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
        # 配置名 → 上次清理时间。以前是实例级字段，导致 cleanup_all 只清第一个配置
        self._last_cleanup: dict[str, float] = {}
        # records.json 解析失败且备份也失败时，禁止写回（别把坏文件覆盖成空表）
        self._unwritable: set[str] = set()

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

    def extracted_dir(self, config_name: str, record_id: str) -> Path:
        """某条历史记录的解压目录（与记录同生命周期）。"""
        return self.record_dir(config_name, record_id) / _EXTRACT_DIR

    def staging_dir(self, config_name: str, token: str) -> Path:
        """
        分析进行中的解压暂存目录。

        归档成功后会整体重命名为 `history/<记录 ID>/extracted`，避免再来回复制；
        归档失败/跳过时由调用方 `discard()` 删除。
        """
        safe = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in str(token)
        )[:80] or "session"
        return self._config_dir(config_name) / _STAGING_DIR / safe

    def cleanup_staging(self, max_age_hours: float = 6.0) -> int:
        """清理崩溃遗留的暂存解压目录（已归档的都在记录目录里）。"""
        from features.maa.archive_extract import cleanup_stale_dirs

        removed = 0
        for name in self._known_config_names():
            removed += cleanup_stale_dirs(
                self._config_dir(name) / _STAGING_DIR, max_age_hours
            )
        return removed

    def _known_config_names(self) -> list[str]:
        """扫描数据目录，列出已有历史目录的配置名。"""
        names: list[str] = []
        try:
            for entry in self._data_dir.iterdir():
                if not entry.is_dir():
                    continue
                if (entry / HISTORY_DIR_NAME).is_dir():
                    names.append(entry.name)
        except OSError:
            return []
        return names

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
                self._quarantine_records(config_name, path, exc)
        self._records[config_name] = records
        return records

    def _quarantine_records(self, config_name: str, path: Path,
                            exc: Exception) -> None:
        """records.json 解析失败：先备份再重建索引，绝不静默覆盖。

        备份成功 → 坏文件被移走（之后 save 写的是新索引，原文件仍可人工恢复）；
        备份也失败 → 把该配置标为不可写：宁可不落盘，也不覆盖唯一一份数据。
        """
        backup = path.with_name(f"{path.name}.bad-{int(time.time())}")
        try:
            path.replace(backup)
        except OSError as move_exc:
            self._unwritable.add(config_name)
            logger.error(
                f"[历史] {config_name} records.json 解析失败，且备份也失败"
                f"（{move_exc}）；已停止写回以免覆盖原文件：{exc}"
            )
            return
        logger.warning(
            f"[历史] {config_name} records.json 解析失败，已备份为 {backup.name} "
            f"并重建索引：{exc}"
        )

    def save(self, config_name: str) -> None:
        """持久化某配置的历史记录（原子写）。"""
        if config_name in self._unwritable:
            logger.warning(
                f"[历史] {config_name} records.json 处于保护状态，跳过写回"
            )
            return
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

    # ── 异步写盘入口 ──
    # 归档 zip 复制、records.json 重写、目录遍历都是阻塞 I/O。调用方在事件
    # 循环里时必须走这些包装，否则一个配置归档大日志会卡住所有配置。

    async def save_async(self, config_name: str) -> None:
        """把 records.json 的写盘下沉到线程。"""
        await run_blocking(self.save, config_name)

    async def add_async(self, **kwargs) -> HistoryRecord:
        """归档记录（复制 zip + 写盘）在线程里执行。"""
        return await run_blocking(self.add, **kwargs)

    async def cleanup_async(self, config_name: str, **kwargs) -> CleanupResult:
        """历史清理（遍历目录、删除文件）在线程里执行。"""
        return await run_blocking(self.cleanup, config_name, **kwargs)

    async def cleanup_all_async(self, settings_of: dict,
                                *, force: bool = False) -> dict:
        """全量周期清理在线程里执行。"""
        return await run_blocking(self.cleanup_all, settings_of, force=force)

    async def cleanup_staging_async(self, max_age_hours: float = 6.0) -> int:
        """清理遗留的解压暂存目录（目录扫描）在线程里执行。"""
        return await run_blocking(self.cleanup_staging, max_age_hours)

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
            log_count: int = 0, image_count: int = 0,
            extract_dir: Optional[Path] = None) -> Optional[HistoryRecord]:
        """
        新增一条历史记录：把日志包、解压结果与上下文归档到历史目录。

        `extract_dir` 是分析阶段已经解压好的目录，会整体移动到
        `history/<记录 ID>/extracted`（同一文件系统下是重命名），
        其生命周期由此与记录、追问能力对齐。

        任何失败都只记日志并返回 None，不影响分析结果。
        """
        try:
            # 绝不覆盖已有目录：ID 唯一性不能靠"记录数 + 1"（清空后会回退复用）
            record_id = ""
            target: Optional[Path] = None
            for _ in range(5):
                candidate = new_record_id()
                path = self.record_dir(config_name, candidate)
                if not path.exists():
                    record_id, target = candidate, path
                    break
            if target is None:
                logger.warning(
                    f"[历史] {config_name} 无法分配唯一记录 ID，已跳过归档"
                )
                return None
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

            if extract_dir is not None and Path(extract_dir).is_dir():
                dest_extract = target / _EXTRACT_DIR
                try:
                    Path(extract_dir).replace(dest_extract)
                except OSError as exc:
                    logger.info(f"[历史] 解压目录移动失败，已跳过保留：{exc}")
                else:
                    size_bytes += _dir_size(dest_extract)
                    logger.info(f"[历史] 已保留解压结果：{dest_extract}")

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

        清理前会按记录目录的**实际磁盘占用**刷新 `size_bytes`：追问阶段
        才补生成的 `extracted/` 也算进容量上限，避免账本小于真实占用。
        """
        result = CleanupResult()
        period = max(1, int(period_hours or 24))
        keep = int(keep_periods or 0)

        now = time.time()
        last = self._last_cleanup.get(config_name, 0.0)
        if not force and last:
            if (now - last) < period * 3600:
                result.kept = len(self.load(config_name))
                result.reason = "未到清理周期"
                return result

        records = self.load(config_name)

        # 手工清理顺带收掉"索引里没有、目录还在"的孤儿记录目录
        # （records.json 曾损坏重建、或索引被手工删过）。放在"无记录"提前返回
        # 之前，否则一个只剩孤儿目录的配置永远清不掉。
        if force:
            orphan_removed = 0
            for path in self._orphan_record_dirs(config_name, records):
                size = _dir_size(path)
                shutil.rmtree(path, ignore_errors=True)
                if path.exists():
                    logger.warning(f"[历史] 孤儿记录目录删除失败：{path.name}")
                    continue
                orphan_removed += 1
                result.freed_bytes += size
            if orphan_removed:
                logger.info(
                    f"[历史] {config_name} 额外清理 {orphan_removed} 个孤儿记录目录"
                )

        if not records:
            self._touch_cleanup(config_name, now, force=force)
            result.reason = "无记录"
            return result

        # 体积以磁盘实际占用为准：分析结束后追问时才解压的 extracted/
        # 不会经过 `add()`，只更新 records.json 里的旧值会漏算。
        size_changed = self._refresh_sizes(config_name, records)

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

        # 安全上限 2：历史目录总大小（从最旧开始删）。
        # records.json / followups.json 也在该目录里，且**报告正文就存在
        # records.json 中**——不计入就会一直低估真实占用。
        index_bytes = self._index_bytes(config_name)
        if max_total_mb > 0:
            budget = max_total_mb * 1024 * 1024
            total = sum(r.size_bytes for r in survivors) + index_bytes
            if total > budget:
                survivors.sort(key=lambda r: r.created_at)
                while survivors and total > budget:
                    victim = survivors.pop(0)
                    total -= victim.size_bytes
                    expired.append(victim)
                result.reason = "超出历史目录大小上限"

        if not expired:
            if size_changed:
                self.save(config_name)
            self._touch_cleanup(config_name, now, force=force)
            result.kept = len(survivors)
            return result

        for record in expired:
            result.freed_bytes += self._remove_record(config_name, record)
            result.removed += 1
            # 记录已删除：对应的追问会话状态也没有意义了
            self.remove_followup(config_name, record.id)

        self._records[config_name] = survivors
        self.save(config_name)
        self._touch_cleanup(config_name, now, force=force)
        result.kept = len(survivors)

        logger.info(
            f"[历史] {config_name} 清理完成：删除 {result.removed} 条"
            f"（释放 {result.freed_bytes // 1024} KB），保留 {result.kept} 条"
            + (f"（{result.reason}）" if result.reason else "")
        )
        return result

    def _refresh_sizes(self, config_name: str,
                       records: list[HistoryRecord]) -> bool:
        """按记录目录的实际体积刷新 `size_bytes`（含追问补解压的目录）。"""
        changed = False
        for record in records:
            actual = _dir_size(self.record_dir(config_name, record.id))
            if actual != record.size_bytes:
                record.size_bytes = actual
                changed = True
        return changed

    def _remove_record(self, config_name: str, record: HistoryRecord) -> int:
        """删除单条记录的目录，返回实际释放的字节数。"""
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

        # 以删除前的实际占用为准：records.json 里的 size_bytes 可能偏小
        freed = _dir_size(target)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            # 删除失败（文件被占用等）：不能算作已释放，否则容量统计失真
            logger.warning(
                f"[历史] 删除 #{record.id} 目录失败，仍占用 {freed} 字节"
            )
            return 0
        return freed

    def cleanup_all(self, settings_of: dict, *, force: bool = False) -> dict:
        """
        对所有**已知**配置执行清理。

        settings_of: {配置名: AnalysisSettings}，用于取各自的周期配置。

        除了当前配置，还覆盖"数据目录里已有历史目录、但已不在配置列表里"的
        （配置改名 / 删除 / 关闭历史后的残留），否则这些目录只增不减。
        `history_enabled=false` 只是不再新增归档，既有历史仍按周期清理。
        """
        names = list(settings_of)
        names.extend(n for n in self._known_config_names() if n not in settings_of)
        summary: dict[str, CleanupResult] = {}
        for name in names:
            summary[name] = self.cleanup(
                name, force=force, **self._cleanup_kwargs(settings_of.get(name))
            )
        return summary

    @staticmethod
    def _cleanup_kwargs(settings) -> dict:
        """按配置取清理参数；配置已不存在时（settings=None）用默认值。"""
        def _int(key: str, default: int) -> int:
            raw = getattr(settings, key, default)
            if raw is None:
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        return dict(
            period_hours=_int("history_period_hours", 24),
            keep_periods=_int("history_keep_periods", 2),
            max_records=_int("history_max_records", 200),
            max_total_mb=_int("history_max_total_mb", 4096),
        )

    def _touch_cleanup(self, config_name: str, now: float, *,
                       force: bool) -> None:
        """记录本次清理时间；手动清理（force）不算作一次周期清理。"""
        if not force:
            self._last_cleanup[config_name] = now

    def _index_bytes(self, config_name: str) -> int:
        """records.json + followups.json 的占用（报告正文就在 records.json 里）。"""
        total = 0
        for path in (self._records_path(config_name),
                     self._followups_path(config_name)):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def _orphan_record_dirs(self, config_name: str,
                            records: list[HistoryRecord]) -> list[Path]:
        """索引里没有、目录还在的记录目录（只按 ID 形状识别，避免误删）。"""
        root = self._config_dir(config_name)
        known = {str(r.id) for r in records}
        found: list[Path] = []
        try:
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name in known:
                    continue
                if entry.name.startswith(".") or not _RECORD_ID_RE.match(entry.name):
                    continue
                found.append(entry)
        except OSError:
            return []
        return found

    def stats(self, config_name: str) -> dict:
        """历史使用情况，供指令展示（按目录实际体积统计）。"""
        records = self.load(config_name)
        return {
            "count": len(records),
            # 记录目录 + 索引文件（报告正文存在 records.json 里，必须计入）
            "bytes": sum(
                _dir_size(self.record_dir(config_name, record.id))
                for record in records
            ) + self._index_bytes(config_name),
            "oldest": min((r.created_at for r in records), default=0),
            "newest": max((r.created_at for r in records), default=0),
        }
