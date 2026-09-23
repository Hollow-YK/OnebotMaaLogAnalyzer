"""
批量测试运行器 — 从 JSON 文件加载测试场景，顺序执行并生成报告。

测试文件格式 (JSON):
{
  "name": "测试套件名称",
  "description": "可选描述",
  "setup": {
    "configs": {"测试组": {"listen_groups": ["123456"], "notify_group": "123456"}},
    "files": {"123456": [{"file_name": "MaaNTE-logs-a.zip", "file_id": "fid_a", "size": 2048}]}
  },
  "scenarios": [
    {
      "name": "上传日志包触发分析",
      "event": {"post_type": "notice", "notice_type": "group_upload", ...},
      "assert": {"reply_contains": "MaaNTE", "no_error": true}
    }
  ]
}

支持的断言:
  reply_contains          回复包含指定文本
  reply_not_contains      回复不包含指定文本
  api_count               API 调用数量 == / >= / <= 指定值
  api_actions_include     API 调用中包含指定 action
  api_actions_exclude     API 调用中不包含指定 action
  api_segments_include    消息段类型中包含指定值（如 image）
  api_segments_exclude    消息段类型中不包含指定值
  prompt_contains         用户提示词（注入内容）包含指定文本
  prompt_not_contains     用户提示词不包含指定文本
  system_prompt_contains  系统提示词包含指定文本
  system_prompt_not_contains 系统提示词不包含指定文本
  no_error                无异常 (true/false)
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.models import AnalysisSettings, CommandConfig, ConfigInfo, ConfigState

if TYPE_CHECKING:
    from debug import DebugManager

logger = logging.getLogger("Maa.Debug.Runner")


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class ScenarioResult:
    """单个场景的执行结果。"""

    name: str
    passed: bool = True
    error: Optional[str] = None
    failures: list[str] = field(default_factory=list)
    elapsed_ms: float = 0

    @property
    def status_icon(self) -> str:
        if self.error and not self.failures:
            return "⚠"
        return "✓" if self.passed else "✗"


@dataclass
class TestReport:
    """测试报告。"""

    name: str = ""
    scenarios: list[ScenarioResult] = field(default_factory=list)
    total_ms: float = 0

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scenarios if s.passed and not s.error)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.scenarios if not s.passed)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.scenarios if s.error and s.passed)

    @property
    def total(self) -> int:
        return len(self.scenarios)

    def __str__(self) -> str:
        lines = ["", "=" * 50, f"  测试报告: {self.name}", "=" * 50]
        for i, s in enumerate(self.scenarios, 1):
            lines.append(f"  {i:2d}. [{s.status_icon}] {s.name} ({s.elapsed_ms:.0f}ms)")
            if s.error:
                lines.append(f"      错误: {s.error}")
            for failure in s.failures:
                lines.append(f"      断言失败: {failure}")

        lines.append("=" * 50)
        error_str = f"异常: {self.errors}  " if self.errors else ""
        lines.append(
            f"  通过: {self.passed}  失败: {self.failed}  {error_str}"
            f"共 {self.total} 个场景, 耗时 {self.total_ms:.0f}ms"
        )
        lines.append("=" * 50)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "total": self.total,
            "total_ms": round(self.total_ms, 1),
            "scenarios": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "error": s.error,
                    "failures": s.failures,
                    "elapsed_ms": round(s.elapsed_ms, 1),
                }
                for s in self.scenarios
            ],
        }


# ════════════════════════════════════════════════════════════════
# 断言引擎
# ════════════════════════════════════════════════════════════════

def _as_list(value) -> list[str]:
    """把断言值统一成字符串列表（支持单个字符串或列表）。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


# 测试事件里可用的占位符
LAST_BOT_MSG = "$LAST_BOT_MSG"


def _resolve_placeholders(value, manager: "DebugManager",
                          variables: Optional[dict] = None):
    """
    把测试事件里的占位符替换为运行时的真实值。

    支持：
      $LAST_BOT_MSG      最近一次 Bot 发出的 message_id
      $<name>            场景 capture 捕获的具名变量（见下）

    具名变量用于「分析消息 ID 在后续追问后会漂移」的场景：
    先在分析场景 capture 住 ID，后续场景引用 $analysis_msg 即可。
    """
    last_id = getattr(manager.api, "last_message_id", 0)
    builtin = {LAST_BOT_MSG.lstrip("$"): str(last_id) if last_id else "0"}
    table = dict(builtin)
    for key, val in (variables or {}).items():
        table[str(key)] = str(val)

    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            def _sub(match):
                name = match.group(1)
                return table.get(name, match.group(0))
            return pattern.sub(_sub, node)
        return node

    return walk(value)


def _check_assertions(assertions: dict, reply: Optional[str],
                      api_actions: list[str], api_count: int,
                      has_error: bool, prompts: Optional[list[str]] = None,
                      system_prompts: Optional[list[str]] = None,
                      segments: Optional[list[str]] = None,
                      history: Optional[dict] = None) -> list[str]:
    """
    检查断言，返回失败的断言描述列表。

    prompt_contains / prompt_not_contains 只检查**用户提示词**
    （即注入的摘要与代码参考），因为 system prompt 是固定人设文本。
    system_prompt_contains / system_prompt_not_contains 则检查系统提示词。
    """
    failures: list[str] = []
    prompt_text = "\n".join(prompts or [])
    system_text = "\n".join(system_prompts or [])

    if "prompt_contains" in assertions:
        if not prompts:
            failures.append("prompt_contains: 未捕获到任何提示词")
        else:
            for expected in _as_list(assertions["prompt_contains"]):
                if expected not in prompt_text:
                    failures.append(f"prompt_contains: 提示词中未找到 {expected!r}")

    if "prompt_not_contains" in assertions:
        for unexpected in _as_list(assertions["prompt_not_contains"]):
            if unexpected in prompt_text:
                failures.append(f"prompt_not_contains: 提示词中不应包含 {unexpected!r}")

    if "system_prompt_contains" in assertions:
        if not system_prompts:
            failures.append("system_prompt_contains: 未捕获到系统提示词")
        else:
            for expected in _as_list(assertions["system_prompt_contains"]):
                if expected not in system_text:
                    failures.append(f"system_prompt_contains: 系统提示词中未找到 {expected!r}")

    if "system_prompt_not_contains" in assertions:
        for unexpected in _as_list(assertions["system_prompt_not_contains"]):
            if unexpected in system_text:
                failures.append(f"system_prompt_not_contains: 系统提示词中不应包含 {unexpected!r}")

    if "no_error" in assertions:
        expect_no_error = bool(assertions["no_error"])
        if expect_no_error and has_error:
            failures.append("no_error: 期望无异常，实际有异常")
        elif not expect_no_error and not has_error:
            failures.append("no_error: 期望有异常，实际无异常")

    if "reply_contains" in assertions:
        for expected in _as_list(assertions["reply_contains"]):
            if not reply or expected not in reply:
                failures.append(
                    f"reply_contains: 回复中未找到 {expected!r}"
                    + (f" (回复: {reply[:80]!r})" if reply else " (回复为空)")
                )

    if "reply_not_contains" in assertions:
        for unexpected in _as_list(assertions["reply_not_contains"]):
            if reply and unexpected in reply:
                failures.append(f"reply_not_contains: 回复中不应包含 {unexpected!r}")

    if "api_count" in assertions:
        expected = assertions["api_count"]
        if isinstance(expected, int):
            if api_count != expected:
                failures.append(f"api_count: 期望 {expected}，实际 {api_count}")
        elif isinstance(expected, dict):
            if "==" in expected and api_count != expected["=="]:
                failures.append(f"api_count: 期望 =={expected['==']}，实际 {api_count}")
            if ">=" in expected and api_count < expected[">="]:
                failures.append(f"api_count: 期望 >={expected['>=']}，实际 {api_count}")
            if "<=" in expected and api_count > expected["<="]:
                failures.append(f"api_count: 期望 <={expected['<=']}，实际 {api_count}")

    if "api_actions_include" in assertions:
        required = assertions["api_actions_include"]
        if isinstance(required, str):
            required = [required]
        for action in required:
            if action not in api_actions:
                failures.append(
                    f"api_actions_include: API 调用中缺少 {action!r} (实际: {api_actions})"
                )

    if "api_actions_exclude" in assertions:
        forbidden = assertions["api_actions_exclude"]
        if isinstance(forbidden, str):
            forbidden = [forbidden]
        for action in forbidden:
            if action in api_actions:
                failures.append(f"api_actions_exclude: API 调用中不应包含 {action!r}")

    if "api_segments_include" in assertions:
        for kind in _as_list(assertions["api_segments_include"]):
            if kind not in (segments or []):
                failures.append(
                    f"api_segments_include: 消息段中缺少 {kind!r}（实际: {segments}）"
                )

    if "api_segments_exclude" in assertions:
        for kind in _as_list(assertions["api_segments_exclude"]):
            if kind in (segments or []):
                failures.append(f"api_segments_exclude: 消息段中不应包含 {kind!r}")

    if "history_count" in assertions:
        expected = int(assertions["history_count"])
        actual = int((history or {}).get("count", -1))
        if actual != expected:
            failures.append(f"history_count: 期望 {expected}，实际 {actual}")

    if "history_zips" in assertions:
        present = set((history or {}).get("zips", []))
        for name in _as_list(assertions["history_zips"]):
            if name not in present:
                failures.append(f"history_zips: 日志包未保留 {name!r}")

    if "history_no_zips" in assertions:
        present = set((history or {}).get("zips", []))
        for name in _as_list(assertions["history_no_zips"]):
            if name in present:
                failures.append(f"history_no_zips: 日志包未被删除 {name!r}")

    if "history_message_ids" in assertions:
        indexed = set((history or {}).get("message_ids", []))
        for mid in _as_list(assertions["history_message_ids"]):
            if mid not in indexed:
                failures.append(f"history_message_ids: 未记录消息 ID {mid!r}")

    return failures


def _apply_setup(manager: "DebugManager", setup: dict):
    """应用测试文件的 setup 段。"""
    if "files" in setup:
        for gid_str, files in setup["files"].items():
            manager.api.set_mock_files(int(gid_str), files)

    # 先物化模拟仓库，再把路径注入配置，供项目代码参考功能做离线验证
    mock_repo = setup.get("mock_repo")
    repo_path = ""
    if isinstance(mock_repo, dict) and mock_repo:
        repo_path = str(_materialize_mock_repo(manager, mock_repo))

    if "configs" in setup:
        # 清空上次运行残留的内存配置，避免测试相互污染（磁盘数据保留）
        manager.service.configs.clear()
        for name, spec in setup["configs"].items():
            info = ConfigInfo(
                listenGroups=set(str(g) for g in spec.get("listen_groups", [])),
                notifyGroup=spec.get("notify_group"),
            )
            state = ConfigState(name=name, info=info)
            raw_settings = spec.get("settings")
            if isinstance(raw_settings, dict):
                raw_settings = json.loads(json.dumps(raw_settings))
                if repo_path:
                    raw_settings.setdefault("repo", {}).setdefault("path", repo_path)
                state.settings = AnalysisSettings.model_validate(raw_settings)

            raw_commands = spec.get("commands")
            if isinstance(raw_commands, dict):
                state.commands = CommandConfig.model_validate(
                    json.loads(json.dumps(raw_commands))
                )

            manager.service.configs[name] = state

    # 播种历史记录（可指定 age_hours 构造过期记录）
    if "history" in setup:
        _seed_history(manager, setup["history"])
    # 播种后立即执行一次清理（验证周期策略）
    if setup.get("history_cleanup"):
        _run_history_cleanup(manager, setup["history_cleanup"])
    # 预置 sentinel 文件（用于验证 git 仓库缓存不被清理）
    for rel in (setup.get("create_files") or []):
        sentinel = Path(getattr(manager.service.dm, "_dir", "data/test")) / rel
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("sentinel", encoding="utf-8")
    # 覆盖模拟模型的报告（用于测试特定附件指令 / 敏感文件请求）
    if "model_report" in setup and hasattr(manager.llm, "report_override"):
        manager.llm.report_override = str(setup["model_report"])


def _history_config_name(manager: "DebugManager", spec: dict) -> str:
    """解析历史操作作用的配置名。"""
    if isinstance(spec, dict) and spec.get("config"):
        return str(spec["config"])
    names = list(manager.service.configs)
    return names[0] if names else "默认"


def _seed_history(manager: "DebugManager", spec: dict) -> None:
    """把 setup.history 里的记录写入历史存储，支持 age_hours 构造过期数据。"""
    store = getattr(manager, "history", None)
    if store is None or not isinstance(spec, dict):
        return
    config_name = _history_config_name(manager, spec)
    data_dir = Path(getattr(manager.service.dm, "_dir", "data/test"))

    if spec.get("reset"):
        # 清空该配置的历史，便于测试隔离
        store._records.pop(config_name, None)
        hist_dir = store._config_dir(config_name)
        if hist_dir.exists():
            shutil.rmtree(hist_dir, ignore_errors=True)

    for idx, item in enumerate(spec.get("records") or []):
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", "123456789"))
        file_name = str(item.get("file_name", f"MaaXXX-logs-seed-{idx}.zip"))

        source_zip = None
        if item.get("with_zip", True):
            source_zip = data_dir / f"_seed_{idx}.zip"
            source_zip.parent.mkdir(parents=True, exist_ok=True)
            source_zip.write_bytes(
                b"PK\x03\x04" + b"0" * int(item.get("zip_bytes", 2048))
            )

        record = store.add(
            config_name=config_name,
            group_id=group_id,
            file_name=file_name,
            source_zip=source_zip,
            context=str(item.get("context", "播种的追问上下文")),
            report=str(item.get("report", "播种的分析报告")),
            message_ids=[str(m) for m in (item.get("message_ids") or [])],
            log_count=int(item.get("log_count", 1)),
            image_count=int(item.get("image_count", 0)),
        )
        if record is None:
            continue
        # 把创建时间回拨，模拟“N 小时前”的记录
        age_hours = float(item.get("age_hours", 0) or 0)
        if age_hours > 0:
            record.created_at = time.time() - age_hours * 3600

    store.save(config_name)


def _run_history_cleanup(manager: "DebugManager", spec) -> None:
    """对指定配置执行一次强制清理。"""
    store = getattr(manager, "history", None)
    if store is None:
        return
    if spec is True:
        spec = {}
    if not isinstance(spec, dict):
        spec = {}
    config_name = _history_config_name(manager, spec)
    state = manager.service.configs.get(config_name)
    settings = state.settings if state is not None else AnalysisSettings()
    store.cleanup(
        config_name,
        period_hours=int(spec.get("period_hours", settings.history_period_hours)),
        keep_periods=int(spec.get("keep_periods", settings.history_keep_periods)),
        max_records=int(spec.get("max_records", settings.history_max_records)),
        max_total_mb=int(spec.get("max_total_mb", settings.history_max_total_mb)),
        force=True,
    )


def _history_snapshot(manager: "DebugManager") -> dict:
    """收集历史状态供断言使用。"""
    store = getattr(manager, "history", None)
    if store is None:
        return {}
    names = list(manager.service.configs) or list(store._records)
    count = 0
    zips: list[str] = []
    message_ids: list[str] = []
    for name in names:
        for record in store.load(name):
            count += 1
            message_ids.extend(record.message_ids)
            if store.read_zip(name, record) is not None:
                zips.append(record.file_name)
    return {"count": count, "zips": zips, "message_ids": message_ids}


def _check_files(manager: "DebugManager", assertions: dict) -> list[str]:
    """检查数据目录下文件是否存在（用于验证仓库缓存未被清理）。"""
    failures: list[str] = []
    base = Path(getattr(manager.service.dm, "_dir", "data/test"))

    if "files_exist" in assertions:
        for rel in _as_list(assertions["files_exist"]):
            if not (base / rel).exists():
                failures.append(f"files_exist: 文件不存在 {rel!r}")

    if "files_missing" in assertions:
        for rel in _as_list(assertions["files_missing"]):
            if (base / rel).exists():
                failures.append(f"files_missing: 文件仍然存在 {rel!r}")

    return failures


def _materialize_mock_repo(manager: "DebugManager", files: dict) -> Path:
    """把 setup.mock_repo 的 {相对路径: 内容} 写入临时目录并返回根目录。"""
    base = Path(getattr(manager.service.dm, "_dir", "data/test")) / "_mock_repo"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            target.write_text(json.dumps(content, ensure_ascii=False, indent=4),
                              encoding="utf-8")
        else:
            target.write_text(str(content), encoding="utf-8")
    logger.info(f"已生成模拟仓库: {base}（{len(files)} 个文件）")
    return base


# ════════════════════════════════════════════════════════════════
# 运行器
# ════════════════════════════════════════════════════════════════

async def run_test_file(file_path: str,
                        manager: Optional["DebugManager"] = None) -> TestReport:
    """从 JSON 文件加载并运行测试场景。"""
    path = Path(file_path)
    # utf-8-sig 兼容带 BOM 的文件（Windows 编辑器常见）
    data = json.loads(path.read_text(encoding="utf-8-sig"))

    if manager is None:
        from debug import DebugManager

        cfg_path = path.parent / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        else:
            cfg = {
                "bot": {"data_dir": str(path.parent / "data"), "configs": {}},
            }
        manager = DebugManager.from_config(cfg)

    report = TestReport(name=data.get("name", path.stem))
    t0 = time.perf_counter()

    _apply_setup(manager, data.get("setup", {}))

    # 跨场景的具名变量（由 capture 写入，供 $name 引用）
    variables: dict[str, str] = {}

    for scenario in data.get("scenarios", []):
        name = scenario.get("name", "未命名场景")
        result = ScenarioResult(name=name)
        s0 = time.perf_counter()
        # 每个场景前重置 LLM 记录与追问会话，避免跨场景污染
        if hasattr(manager.llm, "prompts"):
            manager.llm.prompts.clear()
        if hasattr(manager.llm, "system_prompts"):
            manager.llm.system_prompts.clear()
        if hasattr(manager.llm, "tool_rounds"):
            manager.llm.tool_rounds.clear()
        if hasattr(manager.llm, "tool_results"):
            manager.llm.tool_results.clear()
        # 场景可声明 preserve_followup=true 来跨场景保留追问会话
        if not scenario.get("preserve_followup") and manager.followup is not None:
            manager.followup._sessions.clear()
            manager.followup._index.clear()
        # 场景可声明 preserve_dedup=true 来跨场景保留去重状态
        # （默认清空，使每个场景视为独立的上传）
        if not scenario.get("preserve_dedup") and manager.watcher is not None:
            manager.watcher._recent.clear()
        # 场景可声明 history_cleanup 在注入事件前先清理历史
        if scenario.get("history_cleanup"):
            _run_history_cleanup(manager, scenario["history_cleanup"])
        # 解析事件中的占位符（$LAST_BOT_MSG / $name）
        event = _resolve_placeholders(scenario.get("event", {}), manager, variables)
        try:
            inject_result = await manager.inject_event(event)
            # 场景可在捕获事件后记下变量，供后续场景引用
            # （分析消息 ID 在追问后会漂移，必须先捕获）
            capture = scenario.get("capture")
            if isinstance(capture, dict):
                last_id = getattr(manager.api, "last_message_id", 0)
                for var_name, expr in capture.items():
                    if str(expr).upper() in ("LAST_BOT_MSG", "$LAST_BOT_MSG"):
                        variables[str(var_name)] = str(last_id or 0)
                    else:
                        variables[str(var_name)] = str(expr)
            # 断言中的占位符在事件执行后解析，才能拿到本次发出的消息 ID
            assertions = _resolve_placeholders(
                scenario.get("assert", {}), manager, variables
            )
            api_actions = [c.action for c in inject_result.api_calls]
            segments: list[str] = []
            for call in inject_result.api_calls:
                for kind in (call.params.get("segments") or []):
                    segments.append(str(kind))
            failures = _check_assertions(
                assertions,
                inject_result.reply,
                api_actions,
                len(inject_result.api_calls),
                bool(inject_result.error),
                list(getattr(manager.llm, "prompts", [])),
                list(getattr(manager.llm, "system_prompts", [])),
                segments,
                _history_snapshot(manager),
            )
            failures.extend(_check_files(manager, assertions))
            result.failures = failures
            result.passed = not failures
            if inject_result.error:
                result.error = inject_result.error
        except Exception as exc:
            logger.exception(f"场景执行异常: {name}")
            result.passed = False
            result.error = f"{type(exc).__name__}: {exc}"

        result.elapsed_ms = (time.perf_counter() - s0) * 1000
        report.scenarios.append(result)

    report.total_ms = (time.perf_counter() - t0) * 1000
    return report
