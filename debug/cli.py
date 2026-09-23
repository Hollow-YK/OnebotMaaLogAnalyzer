"""
CLI 交互式调试 REPL — 在终端中模拟 OneBot 事件注入。

命令:
  upload  group=<id> user=<id> name=<文件名> [size=<字节>] [id=<file_id>]
  msgfile group=<id> user=<id> name=<文件名> [size=<字节>] [id=<file_id>]
  msg     group=<id> user=<id> text="<原始消息>"
  notice  <type> group=<id> user=<id> [extra...]
  request <type> <sub> group=<id> user=<id> [extra...]
  files   group=<id> set <文件名>:<file_id>:<大小> [ ...]
  configs 查看当前加载的配置与监听群
  run     <test_file.json>
  help    显示帮助
  quit    退出
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Optional

from core.service import MaaService
from debug import DebugManager, DebugResult

logger = logging.getLogger("Maa.Debug.CLI")

PROMPT = "\033[36mdebug>\033[0m "
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

HELP_TEXT = f"""
{BOLD}{CYAN}OnebotMaaLogAnalyzer 调试 CLI{RESET}

{CYAN}日志包上传模拟:{RESET}
  {BOLD}upload{RESET} group={BOLD}<群号>{RESET} user={BOLD}<QQ>{RESET} name={BOLD}<文件名>{RESET} [size=<字节>] [id=<file_id>]
  示例: upload group=123456 user=10001 name=MaaXXX-logs-20260101.zip size=2048

{CYAN}消息内文件段模拟:{RESET}
  {BOLD}msgfile{RESET} group={BOLD}<群号>{RESET} user={BOLD}<QQ>{RESET} name={BOLD}<文件名>{RESET} [size=<字节>]

{CYAN}普通消息（指令 / 引用式追问）:{RESET}
  {BOLD}msg{RESET} group={BOLD}<群号>{RESET} user={BOLD}<QQ>{RESET} text="<消息>"

{CYAN}文件列表预设:{RESET}
  {BOLD}files{RESET} group={BOLD}<群号>{RESET} set <文件名>:<file_id>:<大小> [ ...]

{CYAN}通知/请求模拟:{RESET}
  {BOLD}notice{RESET} <notice_type> group={BOLD}<群号>{RESET} user={BOLD}<QQ>{RESET} [key=value ...]
  {BOLD}request{RESET} <request_type> <sub_type> group={BOLD}<群号>{RESET} user={BOLD}<QQ>{RESET}

{CYAN}批量测试:{RESET}
  {BOLD}run{RESET} <test_file.json>

{CYAN}其他:{RESET}
  {BOLD}configs{RESET} 查看配置   {BOLD}history{RESET} 查看最近事件
  {BOLD}clear{RESET}   清屏       {BOLD}quit{RESET}    退出
"""


def _parse_kv(args: list[str]) -> dict:
    """解析 key=value 和 key=val1,val2 参数。"""
    result = {}
    for arg in args:
        if "=" not in arg:
            continue
        key, val = arg.split("=", 1)
        if "," in val:
            result[key] = [v.strip() for v in val.split(",")]
        else:
            try:
                result[key] = int(val)
            except ValueError:
                result[key] = val
    return result


def _format_result(result: DebugResult) -> str:
    """格式化 DebugResult 为可读文本。"""
    lines = [f"{CYAN}⏱  耗时: {result.elapsed_ms:.1f}ms{RESET}"]

    if result.error:
        lines.append(f"{RED}✗ 错误: {result.error}{RESET}")
        return "\n".join(lines)

    if result.reply:
        reply_text = result.reply[:600] + ("..." if len(result.reply) > 600 else "")
        lines.append(f"{GREEN}📤 回复 ({len(result.replies)} 条):{RESET}")
        for line in reply_text.split("\n"):
            lines.append(f"   {line}")
    elif result.api_calls:
        lines.append(f"{YELLOW}📤 (已通过 API 发送，无文本回复){RESET}")
    else:
        lines.append(f"{YELLOW}📤 (无回复){RESET}")

    if result.api_calls:
        lines.append(f"{MAGENTA}🔧 API 调用 ({len(result.api_calls)}):{RESET}")
        for i, call in enumerate(result.api_calls, 1):
            params_summary = ", ".join(f"{k}={v}" for k, v in call.params.items())
            if len(params_summary) > 90:
                params_summary = params_summary[:87] + "..."
            lines.append(f"   {i}. {BOLD}{call.action}{RESET}({params_summary})")
    else:
        lines.append(f"{MAGENTA}🔧 API 调用: 无{RESET}")

    return "\n".join(lines)


class CLISession:
    """CLI 交互会话。"""

    def __init__(self, manager: DebugManager):
        self.manager = manager
        self.history: list[tuple[str, Optional[DebugResult]]] = []

    async def handle(self, line: str) -> Optional[str]:
        """处理一行输入，返回输出文本或 None 表示退出。"""
        line = line.strip()
        if not line:
            return ""

        self.history.append((line, None))

        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return f"{RED}解析错误: {exc}{RESET}"

        if not parts:
            return ""

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("quit", "exit", "q"):
            return None
        if cmd in ("help", "h"):
            return HELP_TEXT
        if cmd in ("clear", "cls"):
            print("\033[2J\033[H", end="")
            return ""
        if cmd == "history":
            return self._show_history()
        if cmd == "configs":
            return self._show_configs()
        if cmd == "upload":
            return await self._cmd_upload(args, as_message=False)
        if cmd == "msgfile":
            return await self._cmd_upload(args, as_message=True)
        if cmd in ("msg", "message"):
            return await self._cmd_msg(args)
        if cmd == "files":
            return await self._cmd_files(args)
        if cmd == "notice":
            return await self._cmd_notice(args)
        if cmd == "request":
            return await self._cmd_request(args)
        if cmd == "run":
            return await self._cmd_run(args)

        return f"{RED}未知命令: {cmd}，输入 help 查看帮助{RESET}"

    async def _cmd_upload(self, args: list[str], *, as_message: bool) -> str:
        kv = _parse_kv(args)
        group_id = kv.get("group")
        user_id = kv.get("user")
        name = kv.get("name")

        label = "msgfile" if as_message else "upload"
        if not group_id or not user_id or not name:
            return (f"{RED}用法: {label} group=<群号> user=<QQ> name=<文件名> "
                    f"[size=<字节>] [id=<file_id>]{RESET}")

        file_id = str(kv.get("id", "debug-file-id"))
        size = int(kv.get("size", 0) or 0)

        if as_message:
            result = await self.manager.inject_message_file(
                group_id=int(group_id), user_id=int(user_id),
                file_name=str(name), file_id=file_id, size=size,
            )
        else:
            result = await self.manager.inject_upload(
                group_id=int(group_id), user_id=int(user_id),
                file_name=str(name), file_id=file_id, size=size,
            )
        self.history[-1] = (f"{label} group={group_id} name={name}", result)
        return _format_result(result)

    async def _cmd_msg(self, args: list[str]) -> str:
        kv = _parse_kv(args)
        group_id = kv.get("group")
        user_id = kv.get("user")
        text = kv.get("text", "")

        if not group_id or not user_id:
            return f'{RED}用法: msg group=<群号> user=<QQ> text="<消息>"{RESET}'

        result = await self.manager.inject_message(
            group_id=int(group_id), user_id=int(user_id),
            raw_message=str(text), sender_card=str(kv.get("card", "")),
        )
        self.history[-1] = (f"msg group={group_id} user={user_id} text={text!r}", result)
        return _format_result(result)

    async def _cmd_files(self, args: list[str]) -> str:
        if len(args) < 2 or args[1].lower() != "set":
            return f"{RED}用法: files group=<群号> set <文件名>:<file_id>:<大小> ...{RESET}"

        kv = _parse_kv([args[0]])
        group_id = kv.get("group")
        if not group_id:
            return f"{RED}用法: files group=<群号> set <文件名>:<file_id>:<大小> ...{RESET}"

        files = []
        for item in args[2:]:
            chunks = item.split(":")
            if len(chunks) >= 3:
                files.append({"file_name": chunks[0], "file_id": chunks[1],
                              "size": int(chunks[2])})
            elif len(chunks) == 2:
                files.append({"file_name": chunks[0], "file_id": chunks[1], "size": 0})
            else:
                files.append({"file_name": chunks[0], "file_id": chunks[0], "size": 0})

        self.manager.api.set_mock_files(int(group_id), files)
        return f"{GREEN}✓ 群 {group_id} 已预设 {len(files)} 个文件{RESET}"

    async def _cmd_notice(self, args: list[str]) -> str:
        if not args:
            return f"{RED}用法: notice <type> group=<群号> user=<QQ>{RESET}"

        notice_type = args[0]
        kv = _parse_kv(args[1:])
        group_id = kv.pop("group", None)
        user_id = kv.pop("user", None)

        if not group_id or not user_id:
            return f"{RED}用法: notice {notice_type} group=<群号> user=<QQ>{RESET}"

        result = await self.manager.inject_notice(
            notice_type=notice_type, group_id=int(group_id),
            user_id=int(user_id), **kv,
        )
        self.history[-1] = (f"notice {notice_type} group={group_id} user={user_id}", result)
        return _format_result(result)

    async def _cmd_request(self, args: list[str]) -> str:
        if len(args) < 2:
            return f"{RED}用法: request <type> <sub_type> group=<群号> user=<QQ>{RESET}"

        request_type, sub_type = args[0], args[1]
        kv = _parse_kv(args[2:])
        group_id = kv.pop("group", None)
        user_id = kv.pop("user", None)

        if not group_id or not user_id:
            return (f"{RED}用法: request {request_type} {sub_type} "
                    f"group=<群号> user=<QQ>{RESET}")

        result = await self.manager.inject_request(
            request_type=request_type, sub_type=sub_type,
            group_id=int(group_id), user_id=int(user_id), **kv,
        )
        self.history[-1] = (
            f"request {request_type}/{sub_type} group={group_id} user={user_id}", result)
        return _format_result(result)

    async def _cmd_run(self, args: list[str]) -> str:
        if not args:
            return f"{RED}用法: run <test_file.json>{RESET}"

        from debug.runner import run_test_file
        path = Path(args[0])
        if not path.exists():
            return f"{RED}文件不存在: {path}{RESET}"

        report = await run_test_file(str(path), self.manager)
        return str(report)

    def _show_configs(self) -> str:
        service = self.manager.service
        if not service.configs:
            return f"{YELLOW}(无配置){RESET}"
        lines = [f"{CYAN}已加载配置 ({len(service.configs)}):{RESET}"]
        for name, state in service.configs.items():
            groups = ", ".join(sorted(state.info.listen_groups)) or "（无）"
            notify = state.info.notify_group or "（未设置）"
            lines.append(f"  {BOLD}{name}{RESET}")
            lines.append(f"     监听群: {groups}")
            lines.append(f"     通知群: {notify}")
            lines.append(f"     启用: {state.settings.enabled}  "
                         f"前缀: {state.settings.file_prefix}  "
                         f"任务数: {len(state.jobs)}")
        return "\n".join(lines)

    def _show_history(self) -> str:
        if not self.history:
            return f"{YELLOW}(无历史记录){RESET}"

        lines = [f"{CYAN}历史记录 ({len(self.history)}):{RESET}"]
        for i, (cmd, result) in enumerate(self.history[-20:], 1):
            if result is None:
                status = f"{YELLOW}·{RESET}"
            elif result.error:
                status = f"{RED}✗{RESET}"
            else:
                status = f"{GREEN}✓{RESET}"
            lines.append(f"  {i}. {status} {cmd[:60]}")
        return "\n".join(lines)


async def run_cli(service: MaaService, cfg: dict):
    """启动 CLI 交互式 REPL（阻塞直到用户输入 quit）。"""
    manager = DebugManager.from_config(cfg)
    session = CLISession(manager)

    print(f"\n{BOLD}{CYAN}═══ OnebotMaaLogAnalyzer 调试 CLI ═══{RESET}")
    print(f"  输入 {BOLD}help{RESET} 查看命令，{BOLD}quit{RESET} 退出\n")

    loop = asyncio.get_running_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, input, PROMPT)
        except EOFError:
            print()
            break

        try:
            output = await session.handle(line)
        except Exception as exc:
            logger.exception("CLI 命令异常")
            print(f"{RED}内部错误: {exc}{RESET}")
            continue

        if output is None:
            break
        if output:
            print(output)
            print()

    print(f"\n{CYAN}调试 CLI 已退出。{RESET}")
