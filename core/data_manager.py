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
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel, TypeAdapter

from .models import AnalysisSettings, CommandConfig, ConfigInfo, JobRecord

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("Maa.Data")


class DataManager:
    """JSON 文件持久化管理 — 多配置架构。"""

    def __init__(self, data_dir: str):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

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
        """.tmp 原子写入。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            if path.exists():
                path.unlink()
            tmp.rename(path)
        except OSError:
            if tmp.exists():
                tmp.unlink()
            raise

    # ==================== 模型序列化（基于路径） ====================

    @staticmethod
    def _load_models_file(path: Path, ta: TypeAdapter) -> list:
        """加载 JSON 数组 → pydantic 模型列表，失败时尝试 .tmp 恢复。"""
        data = DataManager._read_file(path)
        if isinstance(data, list):
            try:
                return ta.validate_python(data)
            except Exception:
                logger.warning(f"{path.name} 解析失败，尝试 .tmp 恢复")
        return DataManager._recover_models_file(path, ta)

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
            if path.exists():
                path.unlink()
            tmp.rename(path)
            logger.info(f"从 .tmp 恢复 {path.name} 成功 ({len(result)} 条)")
            return result
        except Exception:
            return []

    @staticmethod
    def _save_models_file(path: Path, models: list):
        """pydantic 模型列表 → JSON 文件。"""
        DataManager._write_file(path,
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
        p = self._dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ==================== 单配置的 groups.json ====================

    def load_config_info(self, name: str) -> ConfigInfo:
        p = self._config_dir(name) / "groups.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            try:
                return ConfigInfo.model_validate(data)
            except Exception:
                logger.warning(f"[{name}] groups.json 解析失败")
        return ConfigInfo()

    def save_config_info(self, name: str, info: ConfigInfo):
        p = self._config_dir(name) / "groups.json"
        self._write_file(p, info.model_dump(mode="json", by_alias=True))

    # ==================== 单配置的 commands.json ====================

    def load_config_commands(self, name: str) -> CommandConfig:
        p = self._config_dir(name) / "commands.json"
        data = self._read_file(p)
        if isinstance(data, dict):
            try:
                return CommandConfig.model_validate(data)
            except Exception:
                logger.warning(f"[{name}] commands.json 解析失败，使用默认值")
        return CommandConfig()

    def save_config_commands(self, name: str, commands: CommandConfig):
        p = self._config_dir(name) / "commands.json"
        self._write_file(p, commands.model_dump(mode="json", by_alias=True))

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
            except Exception:
                logger.warning("全局 settings.json 解析失败，使用默认值")
        settings = AnalysisSettings()
        self.save_global_settings(settings)
        return settings

    def save_global_settings(self, settings: AnalysisSettings):
        p = self._dir / "settings.json"
        self._write_file(p, settings.model_dump(mode="json", by_alias=True))

    # ==================== 单配置的 settings.json（覆盖全局） ====================

    def load_config_settings(self, name: str) -> Optional[dict]:
        """加载配置级设置覆盖（原始 dict，仅含显式设置项）。"""
        p = self._config_dir(name) / "settings.json"
        data = self._read_file(p)
        return data if isinstance(data, dict) else None

    def save_config_settings(self, name: str, settings: AnalysisSettings):
        p = self._config_dir(name) / "settings.json"
        self._write_file(p, settings.model_dump(mode="json", by_alias=True))

    # ==================== 生命周期 ====================

    def check_all(self):
        """确保全局 settings.json 存在。"""
        self.load_global_settings()

    def save_config(self, name: str, state) -> None:
        """持久化单个配置的全部数据。"""
        self.save_config_info(name, state.info)
        self.save_config_settings(name, state.settings)
        self.save_config_commands(name, state.commands)
        self.save_config_jobs(name, state.jobs)
