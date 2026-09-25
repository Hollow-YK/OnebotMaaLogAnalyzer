"""
跨进程事件注入工作器 — 支撑「真实重启」测试。

由 `debug.runner` 以**子进程**方式启动：父进程把任务 JSON 写入本进程
stdin，本进程从零构建一个完整的 `DebugManager`（等价于重启后的新进程：
全部内存状态丢失，只有磁盘上的数据目录得以延续），注入一个事件后把结果
JSON 写到 stdout。

这样测试里声明的「重启」就是真正的进程级重启 —— 与手动 kill 后重新
`python main.py` 等价，而不是在同一个进程里清几个字典。

用法（一般不手工调用）:
    python -m debug.worker        # 从 stdin 读任务，向 stdout 写结果
"""
from __future__ import annotations

import asyncio
import json
import sys

# 结果行的前缀标记，父进程据此从 stdout 中提取结果
RESULT_MARKER = "@@RESULT@@"


def _emit(payload: dict) -> None:
    """把结果以单行标记格式写到 stdout。"""
    sys.stdout.write(
        RESULT_MARKER + json.dumps(payload, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()


async def _run(job: dict) -> dict:
    """构建管线、注入事件，并收集断言所需信息。"""
    from debug import DebugManager

    data_dir = str(job.get("data_dir") or "data/test")
    manager = DebugManager.from_config(job.get("cfg") or {}, data_dir=data_dir)

    # 群文件列表属于内存态，重启后需要重新预置
    for gid, files in (job.get("files") or {}).items():
        manager.api.set_mock_files(int(gid), list(files))

    # 模拟报告覆盖（附件 / 敏感文件场景）同样需要在重启后复现
    report = str(job.get("model_report") or "")
    if report and hasattr(manager.llm, "report_override"):
        manager.llm.report_override = report

    # 消息 ID 计数器续用：真实 QQ 的 message_id 由服务端全局发号，
    # 新进程不会重号；调试 API 是本地计数器，需显式续上避免撞号。
    try:
        manager.api._next_message_id = max(
            100000, int(job.get("next_message_id") or 100000)
        )
    except (TypeError, ValueError):
        pass

    result = await manager.inject_event(job.get("event") or {})

    segments: list[str] = []
    for call in result.api_calls:
        for kind in (call.params.get("segments") or []):
            segments.append(str(kind))

    return {
        "reply": result.reply,
        "replies": list(result.replies),
        "error": result.error,
        "api_actions": [c.action for c in result.api_calls],
        "api_count": len(result.api_calls),
        "segments": segments,
        "prompts": list(getattr(manager.llm, "prompts", [])),
        "system_prompts": list(getattr(manager.llm, "system_prompts", [])),
        "tools": _tool_names(manager),
        "tool_results": list(getattr(manager.llm, "tool_results", []) or []),
        "history": _history_snapshot(manager),
        "last_message_id": getattr(manager.api, "last_message_id", 0),
    }


def _tool_names(manager) -> list[str]:
    """收集本次实际提供给模型的工具名（跨轮次去重）。"""
    names: list[str] = []
    for round_tools in (getattr(manager.llm, "tool_rounds", None) or []):
        for item in (round_tools or []):
            if not isinstance(item, dict):
                continue
            name = (item.get("function") or {}).get("name")
            if name and name not in names:
                names.append(str(name))
    return names


def _history_snapshot(manager) -> dict:
    from debug.runner import _history_snapshot as snapshot

    try:
        return snapshot(manager)
    except Exception:
        return {}


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _emit({"error": f"任务 JSON 解析失败: {exc}"})
        return 1

    try:
        payload = asyncio.run(_run(job))
    except Exception as exc:
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
