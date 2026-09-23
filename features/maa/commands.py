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

from core.models import AnalysisSettings, CommandConfig, ConfigState, RepoConfig

logger = logging.getLogger("Maa.Commands")

# 不允许通过指令修改的设置项（涉及安全或运行时结构）
_PROTECTED_FIELDS = {"system_prompt"}

# 设置项中文说明（未列出的直接显示字段名）
_FIELD_LABELS: dict[str, str] = {
    "enabled": "是否启用该配置",
    "file_prefix": "日志包文件名前缀",
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
    "max_prompt_chars": "摘要最大字符数",
    "model": "覆盖模型名（空=用全局）",
    "temperature": "覆盖温度（空=用全局）",
}

_REPO_LABELS: dict[str, str] = {
    "enabled": "是否启用项目代码参考",
    "mode": "检索模式：inject / agent",
    "path": "本地仓库目录",
    "url": "git 仓库地址（支持镜像）",
    "branch": "分支 / tag",
    "auto_checkout_version": "按日志版本自动切 tag",
    "allow_ai_switch_tag": "允许 AI 切换 tag",
    "restore_latest_after_analyze": "分析后还原原位置",
    "max_tool_rounds": "agent 最大工具轮次",
    "max_tool_result_chars": "单次工具结果字符上限",
    "max_files": "注入提示词的文件数上限",
    "max_chars": "代码片段字符上限",
}

# 布尔值的可接受写法
_TRUE_WORDS = {"1", "on", "true", "yes", "y", "开", "开启", "是", "启用"}
_FALSE_WORDS = {"0", "off", "false", "no", "n", "关", "关闭", "否", "禁用"}


@dataclass
class CommandResult:
    """指令执行结果。"""

    text: str = ""
    ok: bool = True
    # 需要转发给追问流程时置 True（内容已作为普通消息处理）
    consumed: bool = False


class CommandHandler:
    """解析并执行 /maa 指令。"""

    def __init__(self, service, followup_store=None, history_store=None):
        self.s = service
        self.followup = followup_store
        self.history = history_store
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
            "repo": (commands.read_level, self._cmd_repo),
            "jobs": (commands.read_level, self._cmd_jobs),
            "history": (commands.read_level, self._cmd_history),
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
            "off", "disable", "关",
        }:
            required = commands.write_level
        # history clean / purge 需写权限
        if action == "history" and rest and rest[0].lower() in {
            "clean", "clear", "清理", "purge", "清空",
        }:
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
            f"  {prefix} repo              查看项目代码仓库设置",
            f"  {prefix} jobs              查看最近分析记录",
            f"  {prefix} history           查看分析历史归档（可跨天追问）",
            f"  {prefix} followup          查看追问答疑会话状态",
            "",
            "修改类（需权限）：",
            f"  {prefix} set <项> <值>      修改分析设置",
            f"  {prefix} repo mode <模式>   切换 inject / agent",
            f"  {prefix} repo url <地址>    设置仓库地址（支持镜像）",
            f"  {prefix} repo path <目录>   设置本地仓库目录",
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
            f"日志前缀：{settings.file_prefix}",
            f"项目代码参考：{'开启' if settings.repo.enabled else '关闭'}"
            f"（模式 {settings.repo.mode}）",
            f"附带图片：{'开启' if settings.send_images else '关闭'}",
            f"追问答疑：{'开启' if settings.followup_enabled else '关闭'}",
            f"任务记录：{len(cfg.jobs)} 条",
        ]
        if self.followup is not None:
            lines.append(f"追问会话总数：{self.followup.count}")
        return CommandResult(text="\n".join(lines))

    async def _cmd_get(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        lines = ["当前分析设置（可改项）：", ""]
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

    async def _cmd_repo_get(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        repo: RepoConfig = cfg.settings.repo
        lines = [f"项目代码仓库设置：", ""]
        for name, info in RepoConfig.model_fields.items():
            if not _is_simple_type(info.annotation):
                continue
            value = getattr(repo, name, None)
            label = _REPO_LABELS.get(name, "")
            shown = "空" if value in ("", None) else value
            suffix = f"  # {label}" if label else ""
            lines.append(f"  {name} = {shown}{suffix}")
        lines.append("")
        lines.append(f"修改：{cfg.commands.prefix} repo mode agent   /   {cfg.commands.prefix} repo url <地址>")
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

        setattr(cfg.settings, name, value)
        self.s.save_config(cfg.name)
        label = _FIELD_LABELS.get(name, name)
        logger.info(f"[指令] {cfg.name}: {name} = {value!r}")
        return CommandResult(text=f"已设置 {label}（{name}）= {value}")

    async def _cmd_mode(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult(
                text=f"当前模式：{cfg.settings.repo.mode}\n"
                     f"用法：{cfg.commands.prefix} mode inject|agent", ok=False)
        return await self._set_repo_field(cfg, "mode", args[0])

    async def _cmd_enable(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        cfg.settings.enabled = True
        self.s.save_config(cfg.name)
        return CommandResult(text=f"已启用配置「{cfg.name}」。")

    async def _cmd_disable(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        cfg.settings.enabled = False
        self.s.save_config(cfg.name)
        return CommandResult(text=f"已停用配置「{cfg.name}」。停用后不再自动分析日志包。")

    async def _cmd_reload(self, cfg: ConfigState, args: list[str]) -> CommandResult:
        reloader = getattr(self.s, "reload_from_file", None)
        if reloader is None:
            return CommandResult(text="当前版本不支持热重载，请重启进程。", ok=False)
        ok, message = reloader()
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
        if sub in ("on", "enable", "开"):
            return await self._set_repo_field(cfg, "enabled", "true")
        if sub in ("off", "disable", "关"):
            return await self._set_repo_field(cfg, "enabled", "false")
        return CommandResult(text=f"未知 repo 子指令：{sub}", ok=False)

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

        setattr(cfg.settings.repo, name, value)
        self.s.save_config(cfg.name)
        label = _REPO_LABELS.get(name, name)
        logger.info(f"[指令] {cfg.name}: repo.{name} = {value!r}")

        extra = ""
        if name == "mode":
            extra = ("\nagent 模式由 AI 自主调用工具检索，耗时与 token 消耗更高；"
                     "inject 模式更快更省。")
        elif name == "url":
            extra = "\n下次分析时会克隆该仓库（首次较慢），请确认地址可访问。"
        elif name == "path":
            extra = "\n请确认该目录存在且是有效的项目仓库。"
        return CommandResult(text=f"已设置 {label}（repo.{name}）= {value}{extra}")


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
