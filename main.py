#!/usr/bin/env python3
"""
OnebotMaaLogAnalyzer — MaaXXX 日志分析 Bot
基于 OneBot v11 标准协议，纯 Python 实现。

监听群内上传的日志压缩包，当文件名匹配配置的前缀（默认 MaaXXX-logs*.zip）时，
自动下载日志包，提取错误片段、配置摘要与 on_error 截图列表，
再用独立的 MaaXXX 日志诊断提示词调用 AI 分析，并把结论发回群内。
群内可引用分析结果继续追问，也可用 /maa 指令查看与修改配置。

通信模式 (config.json → onebot.mode):
  "ws"             — 正向 WS Universal（单连接，API+事件共线）
  "http_ws"        — HTTP API + 正向 WS 事件（默认，最常用）
  "ws_reverse"     — 反向 WS Universal（Bot 监听，OneBot 连过来）
  "http_ws_reverse"— HTTP API + 反向 WS 事件
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from bot.api import OneBotAPI
from bot.client import OneBotWS
from bot.handler import EventHandler
from core.data_manager import DataManager
from core.llm import LLMClient
from core.service import MaaService
from features.maa.analyzer import MaaAnalyzer
from features.maa.followup import FollowupStore
from features.maa.history import HistoryStore
from features.maa.message_handler import MessageHandler
from features.maa.watcher import LogWatcher

logger = logging.getLogger("Maa")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 最小控制台日志 — 确保首次运行（无 config.json）时也能看到输出
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)


def setup_logging(log_cfg: dict):
    """根据配置重新设置日志：控制台 + 可选文件输出。"""
    log_level = getattr(logging, str(log_cfg.get("log_level", "INFO")).upper(), logging.INFO)
    log_to_file = log_cfg.get("log_to_file", True)
    log_dir = log_cfg.get("log_dir", "logs")

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(console)

    if log_to_file:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        log_file = log_path / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(file_handler)
        logger.info(f"日志输出到文件: {log_file} (level={logging.getLevelName(log_level)})")


MODES = ("ws", "http_ws", "ws_reverse", "http_ws_reverse")


def default_config() -> dict:
    """首次运行时生成的默认配置。"""
    return {
        "onebot": {
            "mode": "http_ws",
            "http_url": "http://127.0.0.1:3000",
            "ws_url": "ws://127.0.0.1:3001",
            "ws_reverse_port": 8080,
            "access_token": "",
            "api_timeout_seconds": 30,
        },
        "bot": {
            "data_dir": "data",
            "configs": {
                "默认": {
                    "listen_groups": ["123456789"],
                    "notify_group": "123456789",
                    "settings": {
                        # 日志包文件名前缀（默认 MaaXXX-logs*.zip）
                        "file_prefix": "MaaXXX-logs",
                        # 结果附图 / 附件：报告后附带错误截图、模板图或相关文件
                        "send_images": True,
                        "max_report_images": 4,
                        "send_repo_images": True,
                        "send_files": True,
                        "max_report_files": 2,
                        "max_file_attachment_mb": 20,
                        # 追问答疑：分析后在该群继续提问
                        "followup_enabled": True,
                        # 0 = 跟随历史保留期（下面 history_* 设置）
                        "followup_window_minutes": 0,
                        # 分析历史：归档日志包 + 追问消息 ID，超期自动清理
                        # 默认保留周期 1d、保留 2 个周期 ⇒ 可追问约 2 天内的日志
                        # 注意：git 仓库缓存（data/repos）不属于历史，永不删除
                        "history_enabled": True,
                        "history_period_hours": 24,
                        "history_keep_periods": 2,
                        # 项目代码参考：让 AI 对照源码核对节点定义、阈值与 expected 文本
                        "repo": {
                            "enabled": False,
                            # 检索模式：inject=预检索注入提示词（快/省 token）
                            #           agent =AI 自主调用工具检索（深/耗 token）
                            "mode": "inject",
                            # 二选一：本地仓库目录（推荐，无需网络）
                            "path": "",
                            # 或 git 地址（支持镜像），默认 MaaXXX 官方仓库
                            "url": "https://github.com/MAAXYZ/MaaXXX",
                            "branch": "",
                            # 按日志包文件名中的版本号自动切到对应 tag
                            "auto_checkout_version": True,
                            # 允许 AI 请求切换其他 tag 复核
                            "allow_ai_switch_tag": True,
                            # 分析结束后还原到分析前所在的分支
                            "restore_latest_after_analyze": True,
                        },
                    },
                    # QQ 指令（默认前缀 /maa，发 `/maa help` 查看全部）
                    "commands": {
                        "enabled": True,
                        "prefix": "/maa",
                        # 超管：可修改所有配置
                        "owners": [],
                        # 管理员：等级 1，可配合 write_level=1 授予写权限
                        "admins": [],
                        # 查看类指令所需等级（0=任意群成员）
                        "read_level": 0,
                        # 修改类指令所需等级（2=仅 owners）
                        "write_level": 2,
                        "followup_level": 0,
                    },
                }
            },
        },
        "llm": {
            "base_url": "https://api.openai.com",
            "api_key": "",
            "model": "",
            "temperature": 0.2,
            "timeout_seconds": 900,
            "fallback_models": [],
            "max_tokens": 0,
        },
        "debug": {
            "enabled": False,
            "http_port": 8765,
            "data_dir": "data/test",
        },
        "log": {
            "log_to_file": True,
            "log_level": "INFO",
            "log_dir": "logs",
        },
    }


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        p.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        logger.info(f"已生成默认配置: {path}，请填写监听群与 LLM 后重启。")
        sys.exit(0)
    return json.loads(p.read_text(encoding="utf-8"))


def build_llm_client(llm_cfg: dict) -> LLMClient:
    return LLMClient(
        base_url=str(llm_cfg.get("base_url", "") or ""),
        api_key=str(llm_cfg.get("api_key", "") or ""),
        model=str(llm_cfg.get("model", "") or ""),
        temperature=float(llm_cfg.get("temperature", 0.2) or 0.2),
        timeout_seconds=int(llm_cfg.get("timeout_seconds", 900) or 900),
        fallback_models=[str(m) for m in (llm_cfg.get("fallback_models") or []) if m],
        max_tokens=int(llm_cfg.get("max_tokens", 0) or 0),
    )


async def _history_cleanup_loop(history, service, interval_seconds: int = 3600):
    """
    周期清理分析历史（默认每小时检查一次）。

    每个周期（history_period_hours）删除超过 history_keep_periods 个周期的记录。
    仅清理 data/<配置>/history/ 目录，**不会**触碰 git 仓库缓存 data/repos。
    """
    while True:
        try:
            await asyncio.sleep(max(300, int(interval_seconds or 3600)))
            results = history.cleanup_all(
                {name: state.settings for name, state in service.configs.items()}
            )
            for name, res in results.items():
                if res.removed:
                    logger.info(
                        f"[历史] 配置「{name}」清理 {res.removed} 条记录，"
                        f"释放 {res.freed_bytes} 字节（{res.reason}）"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[历史] 周期清理失败：{exc}")


async def main():
    logger.info("OnebotMaaLogAnalyzer 启动中...")

    cfg = load_config()
    ob = cfg.get("onebot", {})
    bot_cfg = cfg.get("bot", {})
    llm_cfg = cfg.get("llm", {})
    log_cfg = cfg.get("log", {})

    setup_logging(log_cfg)

    mode = ob.get("mode", "http_ws")
    if mode not in MODES:
        logger.error(f"未知模式: {mode}，可选: {', '.join(MODES)}")
        sys.exit(1)

    token = ob.get("access_token", "")
    http_url = ob.get("http_url", "")
    ws_url = ob.get("ws_url", "")
    ws_reverse_port = int(ob.get("ws_reverse_port", 0))
    api_timeout = int(ob.get("api_timeout_seconds", 30) or 30)

    # HTTP 模式下 API 走 HTTP；WS / ws_reverse 模式下 api 走同一 WS 连接
    use_http = mode in ("http_ws", "http_ws_reverse")
    api = OneBotAPI(
        http_url=http_url if use_http else "",
        access_token=token,
        timeout_seconds=api_timeout,
    )

    ws = OneBotWS(access_token=token)

    if mode in ("ws", "ws_reverse"):
        api.set_ws_send(ws.send)
        ws.on_api_response(api.handle_ws_response)

    dm = DataManager(bot_cfg.get("data_dir", "data"))

    llm = build_llm_client(llm_cfg)
    if not llm.configured:
        logger.warning("⚠ 未配置 llm.base_url / llm.model，AI 分析将无法工作！")

    service = MaaService(api=api, dm=dm)
    service.load()
    service.sync_from_config(bot_cfg.get("configs") or {})

    if not service.configs:
        logger.warning("⚠ 未配置任何监听群，请在 config.json → bot.configs 中设置！")
    else:
        for name, state in service.configs.items():
            groups = ", ".join(sorted(state.info.listen_groups)) or "（无）"
            notify = state.info.notify_group or "（未设置）"
            logger.info(f"配置「{name}」：监听群 {groups}，通知群 {notify}")

    # 日志包监听 + 指令/追问答疑
    # 历史存储：归档日志包 + 追问上下文（周期性清理，git 缓存不受影响）
    history = HistoryStore(dm._dir)
    analyzer = MaaAnalyzer(service, llm, history=history)
    followup = FollowupStore(history=history)
    watcher = LogWatcher(service, analyzer, followup, history=history)
    messages = MessageHandler(
        service, analyzer, followup, history=history,
        segment_sender=watcher.send_segments,
    )

    service.register_event("notice.group_upload", watcher.on_group_upload)
    service.register_event("message.file", watcher.on_message_file)
    service.register_event("message.text", messages.on_message_text)

    handler = EventHandler(api, service)
    ws.on_event("message", handler.on_message)
    ws.on_event("notice", handler.on_notice)
    ws.on_event("request", handler.on_request)

    tasks = []

    debug_cfg = cfg.get("debug", {})
    if debug_cfg.get("enabled", False):
        # 调试模块仅存在于源码仓库，不随发布包分发
        try:
            from debug.cli import run_cli
            from debug.http_server import start_http_server
        except ImportError:
            logger.error(
                "配置启用了 debug.enabled，但未找到 debug 模块。\n"
                "调试模式仅适用于源码仓库（发布包不含 debug/）。\n"
                "请将 config.json 中的 debug.enabled 设为 false 后重启。"
            )
            sys.exit(1)

        http_port = debug_cfg.get("http_port", 8765)
        tasks.append(asyncio.create_task(
            start_http_server(http_port, service, cfg)))
        tasks.append(asyncio.create_task(
            _history_cleanup_loop(history, service)))

        logger.info(f"OnebotMaaLogAnalyzer 已就绪 (调试模式) "
                    f"HTTP→127.0.0.1:{http_port}, CLI→当前终端")
        try:
            await run_cli(service, cfg)
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            for t in tasks:
                t.cancel()
            await ws.stop()
            await api.close()
            service.save()
            logger.info("数据已保存，退出。")
        return

    # 启动时探测协议端（用于兼容不同 OneBot 实现的群文件接口）
    async def detect_backend():
        await asyncio.sleep(3)
        try:
            await service.detect_backend()
        except Exception as exc:
            logger.warning(f"协议端探测失败：{exc}")

    if mode in ("http_ws", "ws"):
        tasks.append(asyncio.create_task(ws.connect(ws_url)))
    if mode in ("ws_reverse", "http_ws_reverse"):
        tasks.append(asyncio.create_task(ws.serve("0.0.0.0", ws_reverse_port)))

    tasks.append(asyncio.create_task(detect_backend()))
    tasks.append(asyncio.create_task(_history_cleanup_loop(history, service)))

    logger.info(f"OnebotMaaLogAnalyzer 已就绪 (mode={mode})，等待日志包上传...")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        for t in tasks:
            t.cancel()
        await ws.stop()
        await api.close()
        service.save()
        logger.info("数据已保存，退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
