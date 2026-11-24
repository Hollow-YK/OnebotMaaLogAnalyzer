"""Apply typed AstrBot panel patches to the shared runtime configuration."""
from __future__ import annotations

import copy
from typing import Any, Mapping, get_args

from pydantic import BaseModel

from ..core.models import (
    AnalysisSettings,
    CommandConfig,
    ConfigLLM,
    LLMGlobalConfig,
    ProjectConfig,
    ReportConfig,
    VisionConfig,
)
from .panel_overrides import (
    GlobalLLMPatch,
    LLMEndpointPatch,
    LLMModelPatch,
    PanelPatch,
    ProfilePatch,
    validate_panel_patch,
)
from .panel_schema import (
    LEGACY_ENDPOINT_FIELDS,
    PANEL_DEFAULTS,
    normalize_panel_values,
    panel_changed,
    panel_value,
)


def _apply_panel_endpoints(result: dict, endpoints: list) -> None:
    """Apply each panel endpoint by name without dropping advanced endpoints."""
    llm_cfg = _ensure_dict(result, "llm")
    seen: set[str] = set()
    for entry in endpoints:
        if not isinstance(entry, Mapping):
            raise ValueError("llm_endpoints 每项必须是端点对象。")
        name = str(entry.get("name") or "").strip()
        if name in seen:
            raise ValueError(f"llm_endpoints 端点名重复：{name or '默认端点'}")
        seen.add(name)
        values = {legacy: entry[key] for key, legacy in LEGACY_ENDPOINT_FIELDS.items()
                  if key in entry}
        values["llm_endpoint_selector"] = name
        endpoint = LLMEndpointPatch.from_panel(values)
        model_patch = LLMModelPatch.from_panel(values)
        providers = llm_cfg.get("providers") or []
        existing = next((p for p in providers if isinstance(p, dict)
                         and (not name or p.get("name") == name)), None)
        model: LLMModelPatch | None = model_patch
        if not model_patch.selected_model and not (existing and existing.get("models")):
            model = None
        _apply_endpoint_patch(llm_cfg, result, endpoint, model)


def _apply_candidate_model_settings(result: dict, panel: Mapping[str, Any]) -> None:
    providers = (result.get("llm") or {}).get("providers") or []
    fields = ("support_vision_mode", "support_json_mode_mode",
              "model_max_tokens", "model_max_context_tokens")
    for candidate in panel.get("llm_models") or []:
        if not isinstance(candidate, Mapping) or not any(k in candidate for k in fields):
            continue
        name = str(candidate.get("name") or "").strip()
        provider = next((p for p in providers if isinstance(p, dict)
                         and (not name or p.get("name") == name)), None)
        if provider is None:
            continue  # Shared validation reports the missing endpoint.
        values = {LEGACY_ENDPOINT_FIELDS[k]: candidate[k] for k in fields if k in candidate}
        values["llm_endpoint_model"] = candidate.get("model", "")
        endpoint = LLMEndpointPatch.from_panel({"llm_endpoint_selector": provider.get("name")})
        _apply_endpoint_patch(result["llm"], result, endpoint, LLMModelPatch.from_panel(values))


def _target_profile(cfg: dict, panel: Mapping[str, Any]) -> str:
    bot_cfg = cfg.setdefault("bot", {})
    configs = bot_cfg.setdefault("configs", {})
    if not isinstance(configs, dict):
        configs = {}
        bot_cfg["configs"] = configs
    requested = str(panel_value(panel, "default_config") or "").strip()
    if requested:
        if requested in configs:
            return requested
        if not configs:
            configs[requested] = {}
            return requested
        raise ValueError(
            f"指定配置不存在：{requested}。"
            f"可选配置：{', '.join(str(name) for name in configs)}"
        )
    if configs:
        return next(iter(configs))
    target = "默认"
    configs[target] = {}
    return target


def _has_panel_changes(panel: Mapping[str, Any]) -> bool:
    return any(
        panel_changed(panel, key)
        for key in PANEL_DEFAULTS
        if key != "config_file"
    )


def _ensure_dict(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _merge_patch(target: dict, patch: Mapping[str, Any]) -> None:
    """Recursively merge a sparse model dump into an existing JSON object."""
    for key, value in patch.items():
        if isinstance(value, Mapping):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _merge_patch(current, value)
        else:
            target[key] = copy.deepcopy(value)


def _model_field_alias(field: Any) -> str | None:
    alias = field.validation_alias or field.alias
    return alias if isinstance(alias, str) else None


def _normalize_model_aliases(data: dict, model: type[BaseModel]) -> None:
    """Keep camelCase JSON from taking precedence over new snake_case edits."""
    for name, field in model.model_fields.items():
        alias = _model_field_alias(field)
        if alias and alias != name and alias in data:
            data[name] = data.pop(alias)
        value = data.get(name)
        candidates = (field.annotation, *get_args(field.annotation))
        child = next(
            (
                item
                for item in candidates
                if isinstance(item, type) and issubclass(item, BaseModel)
            ),
            None,
        )
        if child is None:
            continue
        if isinstance(value, dict):
            _normalize_model_aliases(value, child)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _normalize_model_aliases(item, child)


def _profile_llm_changed(llm_patch: Any) -> bool:
    if llm_patch is None:
        return False
    fields = llm_patch.model_fields_set
    return bool(fields & {"models", "max_tokens"})


def _profile_changed(profile: ProfilePatch) -> bool:
    if profile.listen_groups is not None or profile.notify_group is not None:
        return True
    for section in (
        profile.settings,
        profile.project,
        profile.commands,
        profile.vision,
        profile.report,
    ):
        if section is not None and section.model_fields_set:
            return True
    return _profile_llm_changed(profile.llm)


def _panel_default_provider_name(cfg: dict, patch: PanelPatch) -> str:
    """Resolve the default endpoint used by model refs without a provider."""
    providers = (cfg.get("llm") or {}).get("providers") or []
    endpoint = patch.llm.endpoint if patch.llm else None
    name = str(
        (endpoint.name if endpoint else "")
        or (endpoint.selector if endpoint else "")
        or ""
    ).strip()
    provider = next(
        (
            item
            for item in providers
            if isinstance(item, dict)
            and (not name or item.get("name") == name)
        ),
        {},
    )
    return str(provider.get("name") or name)


def _apply_endpoint_patch(
    llm_cfg: dict,
    result: dict,
    endpoint: LLMEndpointPatch | None,
    model_patch: LLMModelPatch | None,
) -> None:
    if endpoint is None:
        return
    providers = llm_cfg.get("providers")
    if not isinstance(providers, list):
        providers = []
        llm_cfg["providers"] = providers

    selector = str(endpoint.selector or "").strip()
    provider = next(
        (
            item
            for item in providers
            if isinstance(item, dict)
            and str(item.get("name") or "").strip() == selector
        ),
        None,
    )
    if not selector:
        provider = next(
            (item for item in providers if isinstance(item, dict)),
            None,
        )

    if provider is None:
        new_name = selector or str(endpoint.name or "").strip()
        if not new_name or not endpoint.payload_changed:
            raise ValueError("llm_endpoint_selector 对应的端点不存在。")
        provider = {"name": new_name}
        providers.append(provider)

    if "name" in endpoint.model_fields_set and str(endpoint.name or "").strip():
        new_name = str(endpoint.name or "").strip()
        old_name = str(provider.get("name") or "").strip()
        if any(
            isinstance(item, dict)
            and item is not provider
            and str(item.get("name") or "").strip() == new_name
            for item in providers
        ):
            raise ValueError(f"LLM 端点名已存在：{new_name}")
        provider["name"] = new_name
        for profile in result.get("bot", {}).get("configs", {}).values():
            if not isinstance(profile, dict):
                continue
            profile_llm = profile.get("llm")
            if not isinstance(profile_llm, dict):
                continue
            candidates = profile_llm.get("models")
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("name") or "").strip() == old_name
                ):
                    candidate["name"] = new_name

    fields = endpoint.model_fields_set
    for key in ("kind", "base_url", "api_key"):
        if key in fields:
            provider[key] = getattr(endpoint, key)
    for key in (
        "max_concurrency",
        "timeout_seconds",
        "connect_timeout_seconds",
        "max_retries",
    ):
        if key in fields:
            provider[key] = getattr(endpoint, key)

    if model_patch is None or not model_patch.has_changes:
        return
    models = provider.get("models")
    if not isinstance(models, list):
        models = []
        provider["models"] = models
    selected_name = model_patch.selected_model if model_patch else ""
    model = next(
        (
            item
            for item in models
            if isinstance(item, dict)
            and str(item.get("model") or "").strip() == selected_name
        ),
        None,
    )
    if not selected_name:
        model = next(
            (item for item in models if isinstance(item, dict)),
            None,
        )
    if model is None:
        if not selected_name:
            raise ValueError(
                "所选 LLM 端点未声明模型，无法应用模型高级配置。"
            )
        model = {"model": selected_name}
        models.append(model)

    if model_patch is None:
        return
    supports = model.get("supports")
    if not isinstance(supports, dict):
        supports = {}
        model["supports"] = supports
    for key, setting in (
        ("supports_vision", "vision"),
        ("supports_json_mode", "json_mode"),
    ):
        if key in model_patch.model_fields_set:
            supports[setting] = getattr(model_patch, key)
    for key in ("max_tokens", "max_context_tokens"):
        if key in model_patch.model_fields_set:
            model[key] = getattr(model_patch, key)


def _apply_global_llm_patch(result: dict, patch: PanelPatch) -> None:
    global_patch: GlobalLLMPatch | None = patch.llm
    if global_patch is None or not global_patch.has_changes:
        return

    llm_cfg = _ensure_dict(result, "llm")
    if (
        global_patch.endpoint is not None
        and global_patch.endpoint.model_fields_set
    ):
        _apply_endpoint_patch(
            llm_cfg,
            result,
            global_patch.endpoint,
            global_patch.model,
        )
    if global_patch.defaults is not None:
        defaults = _ensure_dict(llm_cfg, "defaults")
        _merge_patch(defaults, global_patch.defaults.sparse_dump())
    if global_patch.circuit_breaker is not None:
        circuit = _ensure_dict(llm_cfg, "circuit_breaker")
        _merge_patch(
            circuit,
            global_patch.circuit_breaker.sparse_dump(),
        )


def _apply_profile_llm_candidates(
    profile: dict,
    result: dict,
    patch: PanelPatch,
) -> None:
    profile_patch = patch.profile
    llm_patch = profile_patch.llm if profile_patch else None
    if llm_patch is None or not _profile_llm_changed(llm_patch):
        return

    llm = _ensure_dict(profile, "llm")
    models = llm.get("models")
    if not isinstance(models, list):
        models = []
    if (
        "models" in llm_patch.model_fields_set
        and llm_patch.models
    ):
        default_provider = _panel_default_provider_name(result, patch)
        models = [
            {
                "name": candidate.name or default_provider,
                "model": candidate.model,
            }
            for candidate in llm_patch.models
        ]
        llm["models"] = models
        providers = (result.get("llm") or {}).get("providers") or []
        for candidate in models:
            provider = next(
                (item for item in providers if isinstance(item, dict)
                 and item.get("name") == candidate["name"]),
                None,
            )
            if provider is None:
                continue  # Common config validation reports the missing endpoint.
            declared = provider.setdefault("models", [])
            if isinstance(declared, list) and not any(
                isinstance(item, dict) and item.get("model") == candidate["model"]
                for item in declared
            ):
                # Conservative capabilities; advanced settings can opt in.
                declared.append({"model": candidate["model"]})


def apply_panel_overrides(
    cfg: Mapping[str, Any],
    panel: Mapping[str, Any] | None,
) -> dict:
    """
    Apply changed dashboard values to one profile without replacing advanced JSON.

    Supplied fields are authoritative, including empty strings and lists.
    Missing fields leave advanced JSON untouched; blank selectors select the
    first endpoint/model.
    """
    result = copy.deepcopy(dict(cfg))
    if not panel:
        return result
    panel = normalize_panel_values(panel)
    endpoints = panel.get("llm_endpoints") or []
    endpoints_configured = bool(endpoints or panel.get("_endpoints_configured"))
    endpoint_model = str(
        (endpoints[0].get("model") if endpoints and isinstance(endpoints[0], Mapping)
         else "" if endpoints_configured else panel.get("llm_endpoint_model")) or ""
    ).strip()
    if not panel.get("llm_models") and endpoint_model:
        panel["llm_models"] = [{
            "name": str(
                (endpoints[0].get("name") if endpoints and isinstance(endpoints[0], Mapping) else "")
                or panel.get("llm_endpoint_name")
                or panel.get("llm_endpoint_selector") or ""
            ).strip(),
            "model": endpoint_model,
        }]
    if endpoints_configured:
        panel = {key: value for key, value in panel.items()
                 if key not in set(LEGACY_ENDPOINT_FIELDS.values()) | {"llm_endpoint_selector"}}
    if not _has_panel_changes(panel):
        return result

    patch = validate_panel_patch(panel)
    bot_cfg = _ensure_dict(result, "bot")

    if patch.update is not None and patch.update.model_fields_set:
        update_cfg = _ensure_dict(result, "update")
        _merge_patch(update_cfg, patch.update.sparse_dump())
    if "data_dir" in patch.model_fields_set:
        bot_cfg["data_dir"] = str(patch.data_dir or "").strip()
    if patch.log is not None:
        log_cfg = _ensure_dict(result, "log")
        log_patch = patch.log.sparse_dump()
        if "log_level" in log_patch:
            log_patch["log_level"] = str(log_patch["log_level"]).upper()
        _merge_patch(log_cfg, log_patch)

    if isinstance(result.get("llm"), dict):
        _normalize_model_aliases(result["llm"], LLMGlobalConfig)
    if endpoints:
        _apply_panel_endpoints(result, endpoints)
    _apply_global_llm_patch(result, patch)
    _apply_candidate_model_settings(result, panel)

    target = _target_profile(result, panel)
    profile_patch = patch.profile
    if profile_patch is None or not _profile_changed(profile_patch):
        return result

    configs = _ensure_dict(bot_cfg, "configs")
    profile = configs.get(target)
    if not isinstance(profile, dict):
        profile = {}
        configs[target] = profile
    for section, model in (
        ("project", ProjectConfig),
        ("settings", AnalysisSettings),
        ("commands", CommandConfig),
        ("llm", ConfigLLM),
        ("vision", VisionConfig),
        ("report", ReportConfig),
    ):
        if isinstance(profile.get(section), dict):
            _normalize_model_aliases(profile[section], model)

    _apply_profile_llm_candidates(profile, result, patch)
    _merge_patch(profile, profile_patch.sparse_profile())
    return result
