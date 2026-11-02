"""
数据持久化 — JSON 文件读写，.tmp 原子写入，.tmp 备份恢复。
序列化/反序列化委托给 pydantic。

多配置架构：
  data/
  ├── settings.json          # 全局分析设置（默认值）
  └── <配置名>/               # 各配置独立目录
      ├── groups.json        # 监听群 + 通知群
      ├── settings.json      # 分析设置覆盖（可选）
      └── jobs/records.json  # 分析任务记录
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel, TypeAdapter

from .models import AnalysisSettings, CommandConfig, ConfigInfo, JobRecord

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("Maa.Data")


def safe_name(name: str) -> str:
    """把配置名收敛成安全的单层目录名。

    只保留字母数字与 `._-`（中文等 Unicode 字母数字会保留），其余替换为 `_`；
    路径分隔符、`..`、绝对路径因此都进不来；截断到 60 字符，全空/全点用
    `default` 兜底。
    """
    raw = str(name or "").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)[:60]
    if not cleaned or not cleaned.strip("."):
        return "default"
    return cleaned


class DataManager:
    """JSON 文件持久化管理 — 多配置架构。"""

    def __init__(self, data_dir: str):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # 损坏且备份失败的路径：禁止再写回（宁可用默认值跑，也不覆盖唯一一份数据）
        self._protected: set[Path] = set()

    # ==================== 底层文件读写 ====================

    @staticmethod
    def _read_file(path: Path) -> list | dict | None:
        """读 JSON 文件，解析失败返回 None。"""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
            return json.loads(text) if text else None
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_file(path: Path, data: list | dict):
        """.tmp 原子写入。

        用 `os.replace` 覆盖：Windows 上 `unlink` + `rename` 之间存在
        "文件不存在"的窗口，写入中途被打断会直接丢文件。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def _write_guarded(self, path: Path, data: list | dict):
        """写盘入口：处于保护状态的路径直接跳过（见 `_quarantine`）。"""
        if path in self._protected:
            logger.warning(f"[数据] {path.name} 处于保护状态，跳过写回")
            return
        self._write_file(path, data)

    def _quarantine(self, path: Path, reason: str) -> bool:
        """损坏文件先备份再重建；备份失败则列入写保护。

        返回 True 表示"可以安全重建"（原文件已备份或本来就不存在）；
        False 表示"禁止写回"——原文件还在原地，宁可用默认值跑也不覆盖。
        """
        if not path.exists():
            return True
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, backup)
        except OSError as exc:
            self._protected.add(path)
            logger.error(
                f"[数据] {path.name} {reason}，且备份失败（{exc}）；"
                "已停止写回以免覆盖原文件"
            )
            return False
        logger.warning(f"[数据] {path.name} {reason}，已备份为 {backup.name} 并重建")
        return True

    # ==================== 模型序列化（基于路径） ====================

    def _load_models_file(self, path: Path, ta: TypeAdapter) -> list:
        """加载 JSON 数组 → pydantic 模型列表，失败时尝试 .tmp 恢复。"""
        data = self._read_file(path)
        if isinstance(data, list):
            try:
                return ta.validate_python(data)
            except Exception as exc:
                logger.warning(f"{path.name} 解析失败，尝试 .tmp 恢复")
                self._quarantine(path, f"解析失败（{exc}）")
        elif path.exists():
            self._quarantine(path, "不是合法 JSON 数组")
        return self._recover_models_file(path, ta)

    @staticmethod
    def _recover_models_file(path: Path, ta: TypeAdapter) -> list:
        tmp = path.with_suffix(path.suffix + ".tmp")
        if not tmp.exists():
            return []
        try:
            data = json.loads(tmp.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            result = ta.validate_python(data)
            os.replace(tmp, path)
            logger.info(f"从 .tmp 恢复 {path.name} 成功 ({len(result)} 条)")
            return result
        except Exception:
            return []

    def _save_models_file(self, path: Path, models: list):
        """pydantic 模型列表 → JSON 文件。"""
        self._write_guarded(path,
            [m.model_dump(mode="json", by_alias=True, exclude_unset=True) for m in models])

    # ==================== 配置目录管理 ====================

    def list_configs(self) -> list[str]:
        """扫描 data/ 下所有配置子目录（含有 groups.json 的）。"""
        return sorted(
            d.name for d in self._dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_")
            and (d / "groups.json").exists()
        )

    def _config_dir(self, name: str) -> Path:
        """配置目录：先清洗（见 `safe_name`），并兼容历史遗留的目录名。

        旧版本直接 `data/<配置名>/`，像 `../x`、`a/b`、绝对路径都能越出数据目录。
        现在只允许清洗后的名字；若老目录已经存在（名字不同但确实是同一配置）、
        且原名不含路径成分，则沿用老目录，避免升级后"历史不见了"。
        """
        raw = str(name or "").strip()
        safe = safe_name(raw)
        target = self._dir / safe
        legacy_ok = (
            raw and raw != safe
            and raw == Path(raw).name and raw not in (".", "..")
        )
        if legacy_ok:
            legacy = self._dir / raw
            if legacy.is_dir() and not target.exists():
                logger.info(f"[数据] 沿用既有配置目录名：{raw!r}")
                target = legacy
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ==================== 单配置的 groups.json ====================

    def load_config_info(self, name: str) -> ConfigInfo:
        p = self._config_dir(name) / "groups.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            try:
                return ConfigInfo.model_validate(data)
            except Exception as exc:
                logger.warning(f"[{name}] groups.json 解析失败")
                self._quarantine(p, f"解析失败（{exc}）")
        elif p.exists():
            self._quarantine(p, "不是合法 JSON 对象")
        return ConfigInfo()

    def save_config_info(self, name: str, info: ConfigInfo):
        p = self._config_dir(name) / "groups.json"
        self._write_guarded(p, info.model_dump(mode="json", by_alias=True))

    # ==================== 单配置的 commands.json ====================

    def load_config_commands(self, name: str) -> CommandConfig:
        p = self._config_dir(name) / "commands.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            try:
                return CommandConfig.model_validate(data)
            except Exception as exc:
                logger.warning(f"[{name}] commands.json 解析失败，使用默认值")
                self._quarantine(p, f"解析失败（{exc}）")
        elif p.exists():
            self._quarantine(p, "不是合法 JSON 对象")
        return CommandConfig()

    def save_config_commands(self, name: str, commands: CommandConfig):
        p = self._config_dir(name) / "commands.json"
        self._write_guarded(p, commands.model_dump(mode="json", by_alias=True))

    # ==================== 单配置的 jobs/records.json ====================

    _jobs_ta = TypeAdapter(list[JobRecord])

    def _jobs_dir(self, name: str) -> Path:
        p = self._config_dir(name) / "jobs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_config_jobs(self, name: str) -> list[JobRecord]:
        p = self._jobs_dir(name) / "records.json"
        return self._load_models_file(p, self._jobs_ta)

    def save_config_jobs(self, name: str, jobs: list[JobRecord]):
        p = self._jobs_dir(name) / "records.json"
        self._save_models_file(p, jobs)

    # ==================== 全局 settings.json ====================

    def load_global_settings(self) -> AnalysisSettings:
        """加载全局分析设置，缺失字段使用模型默认值。"""
        p = self._dir / "settings.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            try:
                return AnalysisSettings.model_validate(data)
            except Exception as exc:
                logger.warning("全局 settings.json 解析失败，使用默认值")
                self._quarantine(p, f"解析失败（{exc}）")
        elif p.exists():
            self._quarantine(p, "不是合法 JSON")
        settings = AnalysisSettings()
        self.save_global_settings(settings)   # 保护状态下会自动跳过写回
        return settings

    def save_global_settings(self, settings: AnalysisSettings):
        p = self._dir / "settings.json"
        self._write_guarded(p, settings.model_dump(mode="json", by_alias=True))

    # ==================== 单配置的 settings.json（覆盖全局） ====================

    SETTINGS_VERSION = 2

    def load_config_settings(self, name: str) -> Optional[dict]:
        """
        加载配置级设置覆盖（原始 dict）。

        新版文件形如 {"version": 2, "overrides": {...}}，只含 `/maa set` 的
        显式覆盖；旧版文件是全量 dump，由 MaaService 转换为相对全局的增量。
        """
        p = self._config_dir(name) / "settings.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            return data
        if p.exists():
            self._quarantine(p, "不是合法 JSON 对象")
        return None

    def save_config_settings(self, name: str, overrides: dict):
        """保存配置级覆盖（仅显式设置项；为空时删除文件）。"""
        p = self._config_dir(name) / "settings.json"
        if not overrides:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._write_guarded(p, {"version": self.SETTINGS_VERSION, "overrides": overrides})

    # ==================== 单配置的 project.json（/maa project set 覆盖） ====================

    PROJECT_VERSION = 1

    def load_config_project(self, name: str) -> Optional[dict]:
        """加载配置级 project 覆盖（`/maa project set` 写入的增量）。"""
        p = self._config_dir(name) / "project.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            return data
        if p.exists():
            self._quarantine(p, "不是合法 JSON 对象")
        return None

    def save_config_project(self, name: str, overrides: dict):
        """保存 project 覆盖（为空时删除文件）。"""
        p = self._config_dir(name) / "project.json"
        if not overrides:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._write_guarded(p, {"version": self.PROJECT_VERSION, "overrides": overrides})

    # ==================== 生命周期 ====================

    def check_all(self):
        """确保全局 settings.json 存在。"""
        self.load_global_settings()

    def save_config(self, name: str, state) -> None:
        """持久化单个配置的全部数据。"""
        self.save_config_info(name, state.info)
        self.save_config_settings(name, state.settings_overrides)
        self.save_config_project(name, state.project_overrides)
        self.save_config_commands(name, state.commands)
        self.save_config_jobs(name, state.jobs)
