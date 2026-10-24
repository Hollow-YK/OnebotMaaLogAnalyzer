"""
QQ 指令系统 — 群内通过指令查看与修改配置。

指令前缀默认 `/maa`，权限分三级（见 CommandConfig）：
  - 查看类（read_level，默认 0）：群内任何人可用
  - 修改类（write_level，默认 2）：仅超管可用
  - 追问答疑（followup_level，默认 0）

设置项从 pydantic 模型动态生成，新增字段自动出现在 `get` / `set` 中，
无需手工维护清单。
"""
from __future__ import annotations

import logging
import time
import typing
from dataclasses import dataclass
from typing import Any, Optional

from core.models import (
    AnalysisSettings,
    CommandConfig,
    ConfigState,
    ProjectConfig,
    ProjectUISettings,
    RepoConfig,
    normalize_ui_key,
)
from features.maa.repo import redact_remote_url

logger = logging.getLogger("Maa.Commands")

# 不允许通过指令修改的设置项（涉及安全或运行时结构）
_PROTECTED_FIELDS = {"system_prompt"}

# 设置项中文说明（未列出的直接显示字段名）
_FIELD_LABELS: dict[str, str] = {
    "enabled": "是否启用该配置",
    "merge_archive_parts": "自动合并分卷日志包",
    "archive_part_settle_seconds": "分卷静默期（连续无新分卷的秒数）",
    "max_maafw_bak_files": "最多处理几个 maafw.bak",
    "max_maafw_bak_mb": "单个 maafw.bak 大小上限 MB（0=不限）",
    "send_progress_message": "是否发送进度提示",
    "send_summary": "是否发送分析结果",
    "reply_chunk_chars": "回复分段字符数",
    "send_images": "报告后是否附带图片",
    "max_report_images": "最多附带图片数",
    "send_repo_images": "是否附带仓库模板图",
    "send_files": "是否允许附带任意文件",
    "max_report_files": "最多附带文件数",
    "max_file_attachment_mb": "单个附件文件大小上限 MB",
    "inline_attachments": "附件是否插在文字中间（图文混排）",
    "followup_enabled": "是否允许追问答疑",
    "followup_window_minutes": "追问会话有效分钟数（0=跟随历史保留期）",
    "followup_max_turns": "单会话最多追问轮数",
    "history_enabled": "是否归档分析历史",
    "history_period_hours": "历史保留周期（小时）",
    "history_keep_periods": "保留周期数（超出即删除）",
    "history_max_records": "历史记录条数上限",
    "history_max_total_mb": "历史占用上限 MB",
    "max_zip_size_mb": "zip 大小上限 MB",
    "max_log_files": "最多处理日志文件数",
    "max_prompt_chars": "摘要字符上限（token 预算的兜底）",
    "ai_stage_timeout_seconds": "整个 AI 阶段超时（秒）",
    "max_context_tokens": "上下文 token 预算（0=用全局默认）",
    "analysis_concurrency": "同时分析的任务数",
    "analysis_cache_hours": "分析结果缓存小时数（0=关闭）",
    "circuit_breaker_enabled": "连续失败时熔断端点",
}

_PROJECT_LABELS: dict[str, str] = {
    "name": "项目名（同时作为报告显示名）",
    "archive_match_mode": "识别方式 auto/known/prefix/any",
    "file_prefix": "前缀（仅 prefix 模式）",
    "require_log_layout": "prefix 模式是否额外校验内容",
}

_UI_LABELS: dict[str, str] = {
    "enabled": "是否分析该 UI 的日志",
}

_REPO_LABELS: dict[str, str] = {
    "enabled": "是否启用项目代码参考",
    "mode": "检索模式：inject / agent",
    "path": "本地仓库目录",
    "url": "git 仓库地址（支持镜像）",
    "mirrors": "备用镜像地址（按顺序尝试）",
    "branch": "分支 / tag",
    "preload_enabled": "启动时预下载仓库",
    "preload_retries": "启动预下载失败重试次数",
    "update_on_analyze": "每次分析日志前更新仓库",
    "allow_ai_update": "允许 AI 更新仓库",
    "auto_checkout_version": "按日志版本自动切 tag",
    "allow_ai_switch_tag": "允许 AI 获取并切换 tag / commit",
    "restore_latest_after_analyze": "分析后还原原位置",
    "max_tool_rounds": "agent 最大工具轮次",
    "max_tool_result_chars": "单次工具结果字符上限",
    "max_files": "注入提示词的文件数上限",
    "max_chars": "代码片段字符上限",
}

# 布尔值的可接受写法
_TRUE_WORDS = {"1", "on", "true", "yes", "y", "开", "开启", "是", "启用"}
_FALSE_WORDS = {"0", "off", "false", "no", "n", "关", "关闭", "否", "禁用"}


def _format_ui_switches(project: ProjectConfig) -> str:
    """把 project.ui 渲染成 `mxu=开, mfaa=关` 形式。"""
    if not project.ui:
        return "（未配置，全部开启）"
    return ", ".join(
        f"{key}={'开' if item.enabled else '关'}"
        for key, item in sorted(project.ui.items())
    )


@dataclass
class CommandResult:
    """指令执行结果。"""

    text: str = ""
    ok: bool = True
    # 需要转发给追问流程时置 True（内容已作为普通消息处理）
    consumed: bool = False


class CommandHandler:
    """解析并执行 /maa 指令。"""

    def __init__(self, service, followup_store=None, history_store=None,
                 analyzer=None):
        self.s = service
        self.followup = followup_store
        self.history = history_store
        self.analyzer = analyzer
        self._current_group = ""   # 当前指令所属群（供 followup 子指令使用）

    # ════════════════════════════════════════════════════════════
    # 解析与权限
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def match(text: str, commands: CommandConfig) -> Optional[list[str]]:
        """
        判断文本是否为指令。是则返回去掉前缀后的参数列表，否则 None。
        """
        raw = str(text or "").strip()
        if not raw:
            return None
        prefix = str(commands.prefix or "/maa").strip()
        if not prefix:
            return None
        if raw == prefix:
            return []
        if not raw.startswith(prefix):
            return None
        rest = raw[len(prefix):]
        # 前缀后必须是空白或行尾，避免 /maaxxx 被误判
        if rest and not rest[0].isspace():
            return None
        return rest.split()

    def _resolve(self, group_id: str) -> tuple[Optional[ConfigState], Optional[str]]:
        """解析群对应的配置，返回 (配置, 错误文本)。"""
        cfg = self.s.resolve_config(str(group_id))
        if cfg is None:
            if self.s.is_listened(str(group_id)):
                return None, "该群同时属于多个配置，无法确定使用哪套设置，请检查 config.json。"
            return None, "该群未被监听，无法执行指令。"
        return cfg, None

    # ════════════════════════════════════════════════════════════
    # 入口
    # ════════════════════════════════════════════════════════════

    async def handle(self, *, group_id: str, user_id: str, args: list[str],
                     is_admin: bool = False) -> CommandResult:
        """
        执行指令。

        is_admin: 协议端上报的群管理员/群主身份（OneBot 的 sender.role），
                  可作为超管之外的额外授权来源。
        """
        cfg, error = self._resolve(group_id)
        if cfg is None:
            return CommandResult(text=error or "无法解析配置。", ok=False)

        self._current_group = str(group_id)
        commands = cfg.commands
        if not commands.enabled:
            return CommandResult(text="指令功能已关闭（commands.enabled=false）。", ok=False)

        level = commands.level_of(user_id)
        if is_admin and level < 1:
            level = 1  # 群管理员至少按管理员处理

        action = args[0].lower() if args else "help"
        rest = args[1:]

        handlers = {
            "help": (0, self._cmd_help),
            "status": (commands.read_level, self._cmd_status),
            "get": (commands.read_level, self._cmd_get),
            "project": (commands.read_level, self._cmd_project),
            "repo": (commands.read_level, self._cmd_repo),
            "jobs": (commands.read_level, self._cmd_jobs),
            "history": (commands.read_level, self._cmd_history),
            "ai": (commands.read_level, self._cmd_ai),
            "followup": (commands.followup_level, self._cmd_followup),
            "set": (commands.write_level, self._cmd_set),
            "mode": (commands.write_level, self._cmd_mode),
            "enable": (commands.write_level, self._cmd_enable),
            "disable": (commands.write_level, self._cmd_disable),
            "reload": (commands.write_level, self._cmd_reload),
        }

        entry = handlers.get(action)
        if entry is None:
            return CommandResult(
                text=f"未知指令：{action}\n发送 `{commands.prefix} help` 查看可用指令。",
                ok=False,
            )

        required, func = entry
        # repo 的写入子指令需写权限；纯查看保持读权限
        if action == "repo" and rest and rest[0].lower() in {
            "mode", "url", "path", "branch", "on", "enable", "开",
            "off", "disable", "关", "mirror", "preload",
        }:
            required = commands.write_level
        # history clean / purge 需写权限
        if action == "history" and rest and rest[0].lower() in {
            "clean", "clear", "清理", "purge", "清空",
        }:
            required = commands.write_level
        # project set 需写权限
        if action == "project" and rest and rest[0].lower() in {"set", "设置"}:
            required = commands.write_level

        if level < required:
            return CommandResult(
                text=f"权限不足：`{action}` 需要等级 {required}，你当前是 {level}。",
                ok=False,
            )

        try:
            return await func(cfg, rest)
        except Exception as exc:
            logger.exception(f"指令执行异常：{action}")
            return CommandResult(text=f"指令执行失败：{type(exc).__name__}: {exc}", ok=False)

    # ════════════════════════════════════════════════════════════
    # 查看类指令
    # ════════════════════════════════════════════════════════════

    async def _cmd_help(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        prefix = cfg.commands.prefix
        lines = [
            f"OnebotMaaLogAnalyzer 指令（前缀 {prefix}）",
            "",
            "查看类：",
            f"  {prefix} status            查看当前配置与运行状态",
            f"  {prefix} get               查看当前分析设置",
            f"  {prefix} project           查看项目配置（名称 / 各 UI / 仓库）",
            f"  {prefix} repo              查看项目代码仓库设置",
            f"  {prefix} jobs              查看最近分析记录",
            f"  {prefix} history           查看分析历史归档（可跨天追问）",
            f"  {prefix} ai [n]            查看最近 n 个会话（分析 + 追问）的用量",
            f"  {prefix} followup          查看追问答疑会话状态",
            "",
            "修改类（需权限）：",
            f"  {prefix} set <项> <值>      修改分析设置",
            f"  {prefix} project set <项> <值>  修改项目配置",
            f"  {prefix} repo mode <模式>   切换 inject / agent",
            f"  {prefix} repo url <地址>    设置仓库主地址",
            f"  {prefix} repo mirror add <地址> 添加备用镜像",
            f"  {prefix} repo path <目录>   设置本地仓库目录",
            f"  {prefix} repo preload on|off 启停启动预下载",
            f"  {prefix} history clean      清理超期历史记录",
            f"  {prefix} history purge      清空全部分析历史",
            f"  {prefix} enable            启用本配置",
            f"  {prefix} disable           停用本配置",
            f"  {prefix} reload            重新加载 config.json",
            "",
            "追问答疑：",
            f"  {prefix} followup close    结束当前追问会话",
            "",
            "分析完成后，直接在本群发文字即可就该次分析继续提问。",
        ]
        return CommandResult(text="\n".join(lines))

    async def _cmd_status(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        settings = cfg.settings
        listen = "、".join(sorted(cfg.info.listen_groups)) or "（无）"
        lines = [
            f"配置名：{cfg.name}",
            f"监听群：{listen}",
            f"通知群：{cfg.info.notify_group or '（无）'}",
            f"状态：{'启用' if settings.enabled else '停用'}",
            f"项目：{cfg.project.name or '（未设置）'}（配置名 {cfg.name}）",
            f"识别方式：{cfg.project.archive_match_mode}"
            + (
                f"（前缀 {cfg.project.file_prefix or '（未配置）'}，"
                f"内容校验 {'开' if cfg.project.require_log_layout else '关'}）"
                if str(cfg.project.archive_match_mode).lower() == "prefix" else ""
            ),
            f"UI 开关：{_format_ui_switches(cfg.project)}",
            f"项目代码参考：{'开启' if cfg.project.repo.enabled else '关闭'}"
            f"（模式 {cfg.project.repo.mode}）",
            f"附带图片：{'开启' if settings.send_images else '关闭'}",
            f"追问答疑：{'开启' if settings.followup_enabled else '关闭'}",
            f"任务记录：{len(cfg.jobs)} 条",
        ]
        if self.followup is not None:
            lines.append(f"追问会话总数：{self.followup.count}")
        return CommandResult(text="\n".join(lines))

    async def _cmd_get(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        candidates = " → ".join(
            f"{ref.name}/{ref.model}" for ref in cfg.llm.models
        ) or "（未声明，AI 分析不可用）"
        lines = [
            "模型配置：",
            f"  候选顺序 = {candidates}",
            f"  温度 = {cfg.llm.temperature if cfg.llm.temperature is not None else '（跟随模型）'}",
            f"  输出上限 = {cfg.llm.max_tokens or '（跟随模型）'}",
            "看图（vision）："
            f"{'开' if cfg.vision.enabled else '关'}"
            f"，最多 {cfg.vision.max_images} 张"
            f"，单张上限 {cfg.vision.max_image_kb} KB",
            "报告（report）："
            f"{cfg.report.render_mode()}"
            f"，模板 {cfg.report.template}"
            f"，最多 {cfg.report.max_pages} 页"
            f"，附带 Markdown {'开' if cfg.report.attach_markdown else '关'}",
            "",
            "当前分析设置（可改项）：",
            "",
        ]
        for name, info in AnalysisSettings.model_fields.items():
            if name in _PROTECTED_FIELDS or name == "repo":
                continue
            if not _is_simple_type(info.annotation):
                continue
            value = getattr(cfg.settings, name, None)
            label = _FIELD_LABELS.get(name, "")
            shown = "空" if value in ("", None) else value
            suffix = f"  # {label}" if label else ""
            lines.append(f"  {name} = {shown}{suffix}")
        lines.append("")
        lines.append(f"修改：{cfg.commands.prefix} set <项> <值>")
        return CommandResult(text="\n".join(lines))

    async def _cmd_ai(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        """`/maa ai [n]`：按会话（分析 + 追问）查看模型用量。"""
        from core.ai_usage import UsageStore, format_tokens

        try:
            limit = int(args[0]) if args else 5
        except (TypeError, ValueError):
            return CommandResult(text="用法：/maa ai [会话数]", ok=False)
        limit = max(1, min(20, limit))

        store = UsageStore(getattr(self.s.dm, "_dir", "data"))
        sessions = store.sessions(limit)
        if not sessions:
            return CommandResult(text="还没有模型调用记录。")

        total_tokens = sum(item.total() for item in sessions)
        total_calls = sum(item.calls for item in sessions)
        failed = sum(item.failed for item in sessions)
        header = (
            f"最近 {len(sessions)} 个会话 · 共 {total_calls} 次调用"
            f" · 合计 {format_tokens(total_tokens)} tokens"
        )
        if failed:
            header += f" · 失败 {failed} 次"
        lines = [header, ""]
        for item in sessions:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(item.updated_at))
            title = f"{stamp} · {item.config or '-'}"
            if item.file:
                title += f" · {item.file}"
            lines.append(title)
            status = f"失败 {item.failed} 次" if item.failed else "全部成功"
            lines.append(f"  {item.phase_split()} · {status}")
            lines.append(
                f"  输入 {format_tokens(item.prompt_tokens)}"
                f" · 输出 {format_tokens(item.completion_tokens)}"
                f" · 合计 {format_tokens(item.total())} tokens"
                f" · 用时 {item.elapsed_ms / 1000:.1f}s"
            )
            lines.append(f"  {item.pair_label()}")
            lines.append("")
        lines.append("说明：同一份日志的分析与它后续的追问计为同一个会话。")
        return CommandResult(text="\n".join(lines).rstrip())

    async def _cmd_project(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        """
        查看 / 修改项目配置（config.json 的 project 段）。

        支持：
          /maa project
          /maa project set name MAK
          /maa project set archive_match_mode prefix
          /maa project set file_prefix MaaXXX-logs
          /maa project set ui.mfaa.enabled false
          /maa project set repo.mode agent
        """
        sub = args[0].lower() if args else "get"
        if sub in ("set", "设置"):
            return await self._set_project_field(cfg, args[1:])
        if sub not in ("get", "查看", ""):
            return CommandResult(
                text=f"用法：{cfg.commands.prefix} project [get] 或 "
                     f"{cfg.commands.prefix} project set <项> <值>",
                ok=False,
            )

        project = cfg.project
        lines = [
            f"项目配置（{cfg.name}）：",
            f"  name = {project.name or '（未设置）'}",
            "",
            "识别：",
        ]
        for name in _PROJECT_LABELS:
            if name == "name":
                continue
            value = getattr(project, name, None)
            lines.append(f"  {name} = {value}  # {_PROJECT_LABELS[name]}")

        lines.extend(["", "各 UI 开关："])
        if project.ui:
            for key, item in sorted(project.ui.items()):
                lines.append(f"  {key} = {'开' if item.enabled else '关'}")
        else:
            lines.append("  （未配置，全部开启）")

        lines.extend([
            "",
            f"repo：{'开启' if project.repo.enabled else '关闭'}（{project.repo.mode}）"
            f"  # 用 `{cfg.commands.prefix} repo` 查看详情",
            "",
            f"修改：{cfg.commands.prefix} project set <项> <值>",
            f"      {cfg.commands.prefix} project set ui.mfaa.enabled false",
        ])
        return CommandResult(text="\n".join(lines))

    async def _set_project_field(self, cfg: ConfigState,
                                 args: list[str]) -> CommandResult:
        if len(args) < 2:
            return CommandResult(
                text=f"用法：{cfg.commands.prefix} project set <项> <值>",
                ok=False,
            )
        name, raw_value = args[0], " ".join(args[1:])
        parts = name.split(".")

        # ui.<名称>.<字段>
        if len(parts) == 3 and parts[0].lower() == "ui":
            ui_key = normalize_ui_key(parts[1])
            field_name = parts[2]
            field = ProjectUISettings.model_fields.get(field_name)
            if field is None or not _is_simple_type(field.annotation):
                return CommandResult(text=f"ui 设置项无效：{field_name}", ok=False)
            value, error = _coerce_value(field.annotation, raw_value)
            if error:
                return CommandResult(text=f"值无效：{error}", ok=False)
            self.s.set_project_override(cfg.name, field_name, value, nested=f"ui.{ui_key}")
            self.s.save_config(cfg.name)
            return CommandResult(
                text=f"已设置 project.ui.{ui_key}.{field_name} = {value}"
                     f"（{_UI_LABELS.get(field_name, field_name)}）"
            )

        # repo.<字段>
        if len(parts) == 2 and parts[0].lower() == "repo":
            field_name = parts[1]
            field = RepoConfig.model_fields.get(field_name)
            if field is None or not _is_simple_type(field.annotation):
                return CommandResult(text=f"repo 设置项无效：{field_name}", ok=False)
            value, error = _coerce_value(field.annotation, raw_value)
            if error:
                return CommandResult(text=f"值无效：{error}", ok=False)
            self.s.set_project_override(cfg.name, field_name, value, nested="repo")
            self.s.save_config(cfg.name)
            shown_value = redact_remote_url(value) if field_name == "url" else value
            return CommandResult(
                text=f"已设置 project.repo.{field_name} = {shown_value}"
                     f"（{_REPO_LABELS.get(field_name, field_name)}）"
            )

        field = ProjectConfig.model_fields.get(name)
        if field is None or not _is_simple_type(field.annotation):
            return CommandResult(text=f"project 设置项无效：{name}", ok=False)
        value, error = _coerce_value(field.annotation, raw_value)
        if error:
            return CommandResult(text=f"值无效：{error}", ok=False)
        self.s.set_project_override(cfg.name, name, value)
        self.s.save_config(cfg.name)
        return CommandResult(
            text=f"已设置 project.{name} = {value}"
                 f"（{_PROJECT_LABELS.get(name, name)}）"
        )

    async def _cmd_repo_get(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        repo: RepoConfig = cfg.project.repo
        lines = [f"项目代码仓库设置：", ""]
        for name, info in RepoConfig.model_fields.items():
            if not _is_simple_type(info.annotation):
                continue
            value = getattr(repo, name, None)
            if name == "url" and value:
                value = redact_remote_url(value)
            label = _REPO_LABELS.get(name, "")
            shown = "空" if value in ("", None) else value
            suffix = f"  # {label}" if label else ""
            lines.append(f"  {name} = {shown}{suffix}")
        if repo.mirrors:
            lines.append(
                "  mirrors = "
                + ", ".join(redact_remote_url(url) for url in repo.mirrors)
            )
        if self.analyzer is not None:
            lines.extend([
                "",
                "预下载状态："
                + self.analyzer.repo_preload_status(cfg.name),
            ])
        lines.append("")
        lines.append(
            f"修改：{cfg.commands.prefix} repo mode agent / "
            f"{cfg.commands.prefix} repo mirror add <地址> / "
            f"{cfg.commands.prefix} repo preload on|off"
        )
        return CommandResult(text="\n".join(lines))

    async def _cmd_jobs(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        jobs = cfg.jobs[-max(1, cfg.commands.max_list_items):]
        if not jobs:
            return CommandResult(text="暂无分析记录。")
        lines = [f"最近 {len(jobs)} 条分析记录：", ""]
        for job in reversed(jobs):
            when = time.strftime("%m-%d %H:%M", time.localtime(job.started_at or 0))
            detail = f"（{job.detail}）" if job.detail else ""
            lines.append(
                f"  #{job.id} {when} {job.status} {job.file_name} "
                f"[日志 {job.log_count} / 截图 {job.image_count}]{detail}"
            )
        return CommandResult(text="\n".join(lines))

    async def _cmd_history(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        """查看 / 清理分析历史归档。"""
        if self.history is None:
            return CommandResult(text="分析历史功能未启用。", ok=False)

        settings = cfg.settings
        sub = args[0].lower() if args else "status"

        if sub in ("clean", "clear", "清理"):
            result = self.history.cleanup(
                cfg.name,
                period_hours=settings.history_period_hours,
                keep_periods=settings.history_keep_periods,
                max_records=settings.history_max_records,
                max_total_mb=settings.history_max_total_mb,
                force=True,
            )
            return CommandResult(
                text=f"已清理超期历史：删除 {result.removed} 条，"
                     f"释放 {_format_size(result.freed_bytes)}，"
                     f"保留 {result.kept} 条。（{result.reason or '未超期'}）",
                ok=True,
            )

        if sub in ("purge", "清空"):
            result = self.history.cleanup(
                cfg.name, period_hours=1, keep_periods=0,
                max_records=0, max_total_mb=0, force=True,
            )
            return CommandResult(
                text=f"已清空分析历史：删除 {result.removed} 条，"
                     f"释放 {_format_size(result.freed_bytes)}。\n"
                     f"（git 仓库缓存不受影响）",
                ok=True,
            )

        stats = self.history.stats(cfg.name)
        keep_hours = int(settings.history_period_hours) * int(settings.history_keep_periods)
        lines = [
            f"分析历史归档（配置「{cfg.name}」）：",
            f"  记录条数：{stats.get('count', 0)}",
            f"  占用空间：{_format_size(stats.get('bytes', 0))}",
            f"  保留策略：每 {settings.history_period_hours} 小时为 1 周期，"
            f"保留 {settings.history_keep_periods} 个周期（约 {keep_hours} 小时）",
            f"  上限：{settings.history_max_records} 条 / "
            f"{settings.history_max_total_mb} MB",
        ]
        oldest = stats.get("oldest")
        newest = stats.get("newest")
        if oldest:
            lines.append(f"  最早记录：{time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest))}")
        if newest:
            lines.append(f"  最新记录：{time.strftime('%Y-%m-%d %H:%M', time.localtime(newest))}")
        lines.append("")
        lines.append("提示：引用历史上任意一条分析结果消息即可继续追问，"
                     "无需重新上传日志。")
        lines.append(f"清理超期：{cfg.commands.prefix} history clean")
        lines.append(f"清空全部：{cfg.commands.prefix} history purge")
        lines.append("（git 仓库缓存不属于历史，不会被清理）")
        return CommandResult(text="\n".join(lines))

    async def _cmd_followup(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        if self.followup is None:
            return CommandResult(text="追问答疑功能未启用。", ok=False)
        sub = args[0].lower() if args else "status"
        if sub in ("close", "end", "结束"):
            closed = self.followup.close(self._current_group)
            return CommandResult(
                text="已结束追问会话。" if closed else "当前群没有进行中的追问会话。",
                ok=closed,
            )
        return CommandResult(text=self.followup.describe(self._current_group))

    # ════════════════════════════════════════════════════════════
    # 修改类指令
    # ════════════════════════════════════════════════════════════

    async def _cmd_set(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        if len(args) < 2:
            return CommandResult(text=f"用法：{cfg.commands.prefix} set <项> <值>", ok=False)
        name, raw_value = args[0], " ".join(args[1:])

        # 支持 repo.xxx 形式
        if name.startswith("repo."):
            return await self._set_repo_field(cfg, name[len("repo."):], raw_value)

        if name in _PROTECTED_FIELDS:
            return CommandResult(text=f"`{name}` 不允许通过指令修改。", ok=False)

        field = AnalysisSettings.model_fields.get(name)
        if field is None or not _is_simple_type(field.annotation):
            return CommandResult(text=f"未知或不可修改的设置项：{name}", ok=False)

        value, error = _coerce_value(field.annotation, raw_value)
        if error:
            return CommandResult(text=f"值无效：{error}", ok=False)

        self.s.set_setting_override(cfg.name, name, value)
        self.s.save_config(cfg.name)
        label = _FIELD_LABELS.get(name, name)
        logger.info(f"[指令] {cfg.name}: {name} = {value!r}")
        return CommandResult(text=f"已设置 {label}（{name}）= {value}")

    async def _cmd_mode(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult(
                text=f"当前模式：{cfg.project.repo.mode}\n"
                     f"用法：{cfg.commands.prefix} mode inject|agent", ok=False)
        return await self._set_repo_field(cfg, "mode", args[0])

    async def _cmd_enable(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        self.s.set_setting_override(cfg.name, "enabled", True)
        self.s.save_config(cfg.name)
        return CommandResult(text=f"已启用配置「{cfg.name}」。")

    async def _cmd_disable(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        self.s.set_setting_override(cfg.name, "enabled", False)
        self.s.save_config(cfg.name)
        return CommandResult(text=f"已停用配置「{cfg.name}」。停用后不再自动分析日志包。")

    async def _cmd_reload(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        requester = getattr(self.s, "request_reload", None)
        if requester is None:
            return CommandResult(text="当前版本不支持热重载，请重启进程。", ok=False)
        # 只登记重建：真正重建在回复发出后执行，避免拆掉发送中的连接
        ok, message = requester(notify_group=self._current_group)
        return CommandResult(text=message, ok=ok)

    # ════════════════════════════════════════════════════════════
    # repo 子指令
    # ════════════════════════════════════════════════════════════

    async def _cmd_repo(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        if not args:
            return await self._cmd_repo_get(cfg, [])
        sub = args[0].lower()
        if sub in ("mode", "url", "path", "branch"):
            if len(args) < 2:
                return CommandResult(
                    text=f"用法：{cfg.commands.prefix} repo {sub} <值>", ok=False)
            return await self._set_repo_field(cfg, sub, " ".join(args[1:]))
        if sub == "preload" and len(args) >= 2:
            value = args[1].lower()
            if value in {"on", "enable", "开"}:
                value = "true"
            elif value in {"off", "disable", "关"}:
                value = "false"
            return await self._set_repo_field(cfg, "preload_enabled", value)
        if sub == "mirror":
            return await self._cmd_repo_mirror(cfg, args[1:])
        if sub in ("on", "enable", "开"):
            return await self._set_repo_field(cfg, "enabled", "true")
        if sub in ("off", "disable", "关"):
            return await self._set_repo_field(cfg, "enabled", "false")
        return CommandResult(text=f"未知 repo 子指令：{sub}", ok=False)

    async def _cmd_repo_mirror(self, cfg: ConfigState,
                               args: list[str]) -> CommandResult:
        repo = cfg.project.repo
        action = args[0].lower() if args else ""
        mirrors = list(repo.mirrors)
        if action == "add" and len(args) >= 2:
            value = " ".join(args[1:]).strip()
            if not value:
                return CommandResult(text="镜像地址不能为空。", ok=False)
            if value in mirrors or value == repo.url:
                return CommandResult(text="该地址已在主地址或镜像列表中。", ok=False)
            mirrors.append(value)
        elif action == "remove" and len(args) >= 2:
            value = " ".join(args[1:]).strip()
            if value.isdigit():
                index = int(value) - 1
                if index < 0 or index >= len(mirrors):
                    return CommandResult(text="镜像序号不存在。", ok=False)
                mirrors.pop(index)
            elif value in mirrors:
                mirrors.remove(value)
            else:
                return CommandResult(text="镜像地址不存在。", ok=False)
        elif action == "clear":
            mirrors.clear()
        else:
            return CommandResult(
                text=f"用法：{cfg.commands.prefix} repo mirror add <地址> / "
                     f"remove <序号或地址> / clear",
                ok=False,
            )

        self.s.set_project_override(cfg.name, "mirrors", mirrors, nested="repo")
        self.s.save_config(cfg.name)
        return CommandResult(text="已更新 repo.mirrors（下次准备/更新仓库时生效）。")

    async def _set_repo_field(self, cfg: ConfigState, name: str,
                              raw_value: str) -> CommandResult:
        field = RepoConfig.model_fields.get(name)
        if field is None or not _is_simple_type(field.annotation):
            return CommandResult(text=f"未知或不可修改的仓库设置：{name}", ok=False)

        value, error = _coerce_value(field.annotation, raw_value)
        if error:
            return CommandResult(text=f"值无效：{error}", ok=False)

        # mode 只接受 inject / agent
        if name == "mode":
            mode = str(value).strip().lower()
            if mode not in ("inject", "agent"):
                return CommandResult(text="模式只能是 inject 或 agent。", ok=False)
            value = mode

        self.s.set_project_override(cfg.name, name, value, nested="repo")
        self.s.save_config(cfg.name)
        label = _REPO_LABELS.get(name, name)
        shown_value = redact_remote_url(value) if name == "url" else value
        logger.info(f"[指令] {cfg.name}: repo.{name} = {shown_value!r}")

        extra = ""
        if name == "mode":
            extra = ("\nagent 模式由 AI 自主调用工具检索，耗时与 token 消耗更高；"
                     "inject 模式更快更省。")
        elif name == "url":
            extra = "\n下次分析时会克隆该仓库（首次较慢），请确认地址可访问。"
        elif name == "path":
            extra = "\n请确认该目录存在且是有效的项目仓库。"
        elif name == "preload_enabled":
            extra = "\n启动预下载的启停变更将在下次启动或 /maa reload 后生效。"
        return CommandResult(
            text=f"已设置 {label}（repo.{name}）= {shown_value}{extra}"
        )


# ════════════════════════════════════════════════════════════════
# 类型工具
# ════════════════════════════════════════════════════════════════

def _format_size(num_bytes: Any) -> str:
    """把字节数格式化为易读字符串。"""
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _unwrap_optional(annotation: Any) -> Any:
    """去掉 Optional[...] 外壳，返回内部类型。"""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_simple_type(annotation: Any) -> bool:
    """是否为可通过指令编辑的简单类型（bool/int/float/str）。"""
    inner = _unwrap_optional(annotation)
    return inner in (bool, int, float, str)


def _coerce_value(annotation: Any, raw: str) -> tuple[Any, str]:
    """
    把指令文本转为字段类型。

    返回 (值, 错误信息)。错误信息非空时值无意义。
    """
    inner = _unwrap_optional(annotation)
    text = str(raw or "").strip()

    if inner is bool:
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True, ""
        if lowered in _FALSE_WORDS:
            return False, ""
        return None, f"布尔值请用 on/off、true/false 或 开/关，收到 {text!r}"

    if inner is int:
        try:
            return int(text), ""
        except ValueError:
            return None, f"需要整数，收到 {text!r}"

    if inner is float:
        if text == "":
            return None, "需要数字，收到空值"
        try:
            return float(text), ""
        except ValueError:
            return None, f"需要数字，收到 {text!r}"

    # str：允许用 - 清空
    if text == "-":
        return "", ""
    return text, ""
