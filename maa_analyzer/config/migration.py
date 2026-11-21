"""Versioned config.json loading and migrations."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..core.models import (
    AnalysisSettings,
    CommandConfig,
    ConfigLLM,
    LLMGlobalConfig,
    ProjectConfig,
    ReportConfig,
    VisionConfig,
)
from .constants import CONFIG_VERSION, CONFIG_VERSION_FIELD

logger = logging.getLogger("Maa")

_LEGACY_LLM_FIELDS = {
    "base_url",
    "api_key",
    "model",
    "fallback_models",
    "timeout_seconds",
    "temperature",
    "max_tokens",
    "max_context_tokens",
}


class ConfigVersionError(ValueError):
    """Raised when a config version cannot be handled safely."""


def migrate_config(data: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate a loaded config to the current version.

    The returned boolean is true only when the migrated document differs from
    the input and should be written back to disk.
    """
    cfg = copy.deepcopy(dict(data))
    raw_version = cfg.get(CONFIG_VERSION_FIELD, 1)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigVersionError(
            f"config.json 的 {CONFIG_VERSION_FIELD} 必须是整数，"
            f"当前为 {raw_version!r}。"
        )
    if raw_version < 1:
        raise ConfigVersionError(
            f"config.json 的 {CONFIG_VERSION_FIELD} 必须大于等于 1，"
            f"当前为 {raw_version}。"
        )
    if raw_version > CONFIG_VERSION:
        raise ConfigVersionError(
            f"config.json 版本为 {raw_version}，当前程序只支持到 "
            f"{CONFIG_VERSION}。请升级程序后再启动。"
        )
    if raw_version == CONFIG_VERSION:
        return cfg, False

    version = raw_version
    while version < CONFIG_VERSION:
        if version == 1:
            _migrate_v1_to_v2(cfg)
        elif version == 2:
            _migrate_v2_to_v3(cfg)
        else:
            raise ConfigVersionError(f"缺少配置版本 {version} 的迁移规则。")
        version += 1

    cfg = {
        CONFIG_VERSION_FIELD: CONFIG_VERSION,
        **{
            key: value
            for key, value in cfg.items()
            if key != CONFIG_VERSION_FIELD
        },
    }
    return cfg, True


def load_and_migrate_config(path: str | Path) -> dict[str, Any]:
    """Read config.json and atomically persist a required migration."""
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config.json 顶层必须是对象。")

    migrated, changed = migrate_config(data)
    if not changed:
        return migrated

    backup = target.with_name(f"{target.name}.v1.bak")
    if not backup.exists():
        shutil.copy2(target, backup)
        logger.info("[配置] 已备份迁移前的文件：%s", backup)
    _write_json_atomic(target, migrated)
    logger.info("[配置] 已自动迁移到 version=%s：%s", CONFIG_VERSION, target)
    return migrated


def _migrate_v1_to_v2(cfg: dict[str, Any]) -> None:
    """Upgrade the legacy flat LLM block while retaining current config fields."""
    llm = cfg.get("llm")
    if (
        isinstance(llm, dict)
        and "providers" not in llm
        and any(key in llm for key in _LEGACY_LLM_FIELDS)
    ):
        model_names = _legacy_model_names(llm)
        max_tokens = _positive_int(llm.get("max_tokens"), 8192)
        max_context_tokens = _positive_int(llm.get("max_context_tokens"), 48000)
        provider_name = "default"
        provider = {
            "name": provider_name,
            "kind": "openai",
            "base_url": str(llm.get("base_url") or "").strip(),
            "api_key": str(llm.get("api_key") or ""),
            "max_concurrency": _positive_int(llm.get("max_concurrency"), 2),
            "timeout_seconds": _positive_int(llm.get("timeout_seconds"), 180),
            "connect_timeout_seconds": _positive_int(
                llm.get("connect_timeout_seconds"), 10
            ),
            "max_retries": _nonnegative_int(llm.get("max_retries"), 2),
            "models": [
                {
                    "model": model_name,
                    "supports": {"vision": True, "json_mode": True},
                    "max_tokens": max_tokens,
                    "max_context_tokens": max_context_tokens,
                }
                for model_name in model_names
            ],
        }
        defaults = {
            "max_tokens": max_tokens,
            "max_context_tokens": max_context_tokens,
        }
        circuit_breaker = {
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rate_cooldown_seconds": 60,
            "auth_cooldown_seconds": 1800,
        }
        if isinstance(llm.get("circuit_breaker"), dict):
            circuit_breaker.update(
                {
                    key: value
                    for key, value in llm["circuit_breaker"].items()
                    if key in circuit_breaker
                }
            )

        cfg["llm"] = {
            "providers": [provider],
            "defaults": defaults,
            "circuit_breaker": circuit_breaker,
        }
        _ensure_profile_model_refs(cfg, provider_name, model_names)

    _prune_v2_sections(cfg)


def _migrate_v2_to_v3(cfg: dict[str, Any]) -> None:
    """
    v3：移除摘要相关设置与 `repo.mode`（分析一律走工具），并清掉遗留字段。

    未知字段由 pydantic 模型白名单裁剪掉。同时把端点模型的
    `supports.tools` 显式写为 true：v3 的分析恒走工具模式，旧配置里
    `tools: false` 在 v2 实际上就被忽略，这里保持同样的有效行为。
    """
    _prune_v2_sections(cfg)
    llm = cfg.get("llm")
    providers = llm.get("providers") if isinstance(llm, dict) else None
    if not isinstance(providers, list):
        return
    for provider in providers:
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, list):
            continue
        for spec in models:
            if not isinstance(spec, dict):
                continue
            supports = spec.get("supports")
            if not isinstance(supports, dict):
                supports = {}
                spec["supports"] = supports
            supports["tools"] = True


def _prune_v2_sections(cfg: dict[str, Any]) -> None:
    """Drop v1 fields that have no meaning in the current schema."""
    _prune_model_section(cfg, "llm", LLMGlobalConfig)

    bot = cfg.get("bot")
    if not isinstance(bot, dict):
        return
    configs = bot.get("configs")
    if not isinstance(configs, dict):
        return

    section_models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("project", ProjectConfig),
        ("llm", ConfigLLM),
        ("vision", VisionConfig),
        ("report", ReportConfig),
        ("settings", AnalysisSettings),
        ("commands", CommandConfig),
    )
    for profile in configs.values():
        if not isinstance(profile, dict):
            continue
        for key, model in section_models:
            _prune_model_section(profile, key, model)


def _prune_model_section(
    parent: dict[str, Any],
    key: str,
    model: type[BaseModel],
) -> None:
    """Keep only fields known to the current model for a v1 section."""
    raw = parent.get(key)
    if not isinstance(raw, dict):
        return
    try:
        parsed = model.model_validate(raw)
    except ValidationError:
        return
    parent[key] = parsed.model_dump(exclude_unset=True)


def _legacy_model_names(llm: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    primary = str(llm.get("model") or "").strip()
    if primary:
        names.append(primary)

    fallbacks = llm.get("fallback_models") or []
    if isinstance(fallbacks, str):
        fallbacks = [fallbacks]
    if isinstance(fallbacks, list):
        for item in fallbacks:
            if isinstance(item, Mapping):
                name = str(item.get("model") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
    return list(dict.fromkeys(names))


def _ensure_profile_model_refs(
    cfg: dict[str, Any],
    provider_name: str,
    model_names: list[str],
) -> None:
    bot = cfg.get("bot")
    if not isinstance(bot, dict):
        bot = {}
        cfg["bot"] = bot
    configs = bot.get("configs")
    if not isinstance(configs, dict):
        configs = {}
        bot["configs"] = configs
    if not configs:
        configs["默认"] = {}

    refs = [
        {"name": provider_name, "model": model_name}
        for model_name in model_names
    ]
    for profile in configs.values():
        if not isinstance(profile, dict):
            continue
        llm = profile.get("llm")
        if not isinstance(llm, dict):
            llm = {}
            profile["llm"] = llm
        models = llm.get("models")
        if not isinstance(models, list) or not models:
            llm["models"] = copy.deepcopy(refs)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
