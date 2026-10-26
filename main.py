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
from typing import Optional

from bot.api import OneBotAPI
from bot.client import OneBotWS
from bot.handler import EventHandler
from core.ai_usage import UsageStore
from core.data_manager import DataManager
from core.llm_pool import LLMPool, detect_legacy_llm, validate_llm_config
from core.log_context import ConfigNameFormatter
from core.service import MaaService
from features.maa.analyzer import MaaAnalyzer
from features.maa.followup import FollowupStore
from features.maa.history import HistoryStore
from features.maa.message_handler import MessageHandler
from features.maa.watcher import LogWatcher

logger = logging.getLogger("Maa")

# %(config_prefix)s 由 ConfigNameFormatter 填入当前配置名（如 " [MAN]"）
LOG_FORMAT = "%(asctime)s [%(levelname)s]%(config_prefix)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# 首次运行时的最小控制台日志：格式化器尚未装好，不能引用 config_prefix
BOOTSTRAP_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=BOOTSTRAP_LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
)


def _unique_log_file(log_dir: Path) -> Path:
    """生成不与已有文件冲突的日志文件名（同一秒内重载也不覆盖）。"""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = log_dir / f"{stamp}.log"
    index = 2
    while path.exists():
        path = log_dir / f"{stamp}_{index}.log"
        index += 1
    return path


def setup_logging(log_cfg: dict):
    """
    根据配置重新设置日志：控制台 + 可选文件输出。

    每次调用都会**另开一个新日志文件**（热重载等同于重启进程），
    旧 handler 会先关闭，避免文件句柄泄漏。
    """
    log_level = getattr(logging, str(log_cfg.get("log_level", "INFO")).upper(), logging.INFO)
    log_to_file = log_cfg.get("log_to_file", True)
    log_dir = log_cfg.get("log_dir", "logs")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(console)

    if log_to_file:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        log_file = _unique_log_file(log_path)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(ConfigNameFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
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
                    # 项目相关：名称 / 各 UI 命名 / 项目代码仓库
                    "project": {
                        # 项目名（同时作为报告与提示词中的显示名）
                        "name": "MaaXXX",
                        # 文件名初筛：auto=已知 UI 命名/名字像日志
                        #           known=仅已知 UI 命名
                        #           prefix=仅 file_prefix 开头
                        #           any=任意 zip，完全依赖下载后校验
                        "archive_match_mode": "auto",
                        # 仅 prefix 模式：是否额外做内容特征识别（默认 True）
                        "require_log_layout": True,
                        # 按 UI 开关：键名 mxu / mfaa / maafwapp / auto
                        # "ui": {"mfaa": {"enabled": False}},
                        # 项目代码参考：让 AI 对照源码核对节点定义、阈值与 expected 文本
                        "repo": {
                            "enabled": False,
                            # 检索模式：inject=预检索注入提示词（快/省 token）
                            #           agent =AI 自主调用工具检索（深/耗 token）
                            "mode": "inject",
                            # 二选一：本地仓库目录（推荐，无需网络）
                            "path": "",
                            # 或主 git 地址与按顺序尝试的备用镜像
                            "url": "https://github.com/MAAXYZ/MaaXXX",
                            "mirrors": [],
                            "branch": "",
                            # 启动预下载：完整克隆（含全部历史 / tag）并初始化子模块
                            # 关闭时改为首次分析时浅克隆
                            "preload_enabled": True,
                            "preload_retries": 3,
                            # 每份日志分析前更新；AI 也可按需调用 pull_latest
                            "update_on_analyze": False,
                            "allow_ai_update": True,
                            # 按压缩包内解析出的资源版本自动切到对应 tag
                            "auto_checkout_version": True,
                            # 允许 AI 请求切换其他 tag 复核
                            "allow_ai_switch_tag": True,
                            # 分析结束后还原到分析前所在的分支
                            "restore_latest_after_analyze": True,
                        },
                    },
                    # 本配置可用的模型（按顺序失败转移；引用全局 llm.providers）
                    "llm": {
                        "models": [
                            {"name": "default", "model": "gpt-4o-mini"},
                        ],
                    },
                    # 多模态：把错误截图 / 模板图交给视觉模型（默认开启）
                    "vision": {
                        "enabled": True,
                        "max_images": 4,
                        "max_image_kb": 800,
                        "detail": "high",
                        "attach_error_screenshots": True,
                        "attach_repo_templates": True,
                        "allow_model_request": True,
                    },
                    # 图片化报告：auto = 浏览器可用则图片，否则纯文本
                    "report": {
                        "render": "auto",
                        "template": "default",
                        "max_width_px": 1080,
                        "max_height_px": 4096,
                        "max_pages": 6,
                        "device_scale_factor": 1,
                        "font_family": "",
                        "browser_path": "",
                        "reuse_browser": True,
                        "render_timeout_ms": 8000,
                        "embed_attachments": True,
                        "attach_markdown": False,
                        "fallback_to_text": True,
                        "render_followup": True,
                    },
                    "settings": {
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
                        # 注意：git 仓库缓存（data/repo）不属于历史，永不删除
                        "history_enabled": True,
                        "history_period_hours": 24,
                        "history_keep_periods": 2,
                        # 整个 AI 阶段（含工具循环与收敛）的总时长上限
                        "ai_stage_timeout_seconds": 300,
                        # 0 = 用 llm.defaults.max_context_tokens
                        "max_context_tokens": 0,
                        "analysis_concurrency": 2,
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
        # 全局 LLM 端点与模型能力；具体用哪个模型由
        # bot.configs.<名称>.llm.models[] 按优先级声明
        "llm": {
            "providers": [
                {
                    "name": "default",
                    "kind": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "max_concurrency": 2,
                    "timeout_seconds": 180,
                    "connect_timeout_seconds": 10,
                    "max_retries": 2,
                    "models": [
                        {
                            "model": "gpt-4o-mini",
                            "supports": {"tools": True, "vision": True, "json_mode": True},
                            "max_tokens": 8192,
                            "max_context_tokens": 48000,
                        }
                    ],
                }
            ],
            "defaults": {
                "temperature": 0.2,
                "top_p": 1.0,
                "max_tokens": 8192,
                "max_context_tokens": 48000,
            },
            "circuit_breaker": {
                "failure_threshold": 3,
                "cooldown_seconds": 300,
                "rate_cooldown_seconds": 60,
                "auth_cooldown_seconds": 1800,
            },
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


def build_llm_pool(llm_cfg: dict, usage: Optional[UsageStore] = None) -> LLMPool:
    return LLMPool(llm_cfg, usage=usage)


def _print_config_template() -> bool:
    """`--print-config-template`：把默认配置模板输出到标准输出。"""
    print(json.dumps(default_config(), ensure_ascii=False, indent=2))
    return True


async def _history_cleanup_loop(history, service, interval_seconds: int = 3600):
    """
    周期清理分析历史（默认每小时检查一次）。

    每个周期（history_period_hours）删除超过 history_keep_periods 个周期的记录。
    仅清理 data/<配置>/history/ 目录，**不会**触碰 git 仓库缓存 data/repo。
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


class BotRuntime:
    """
    Bot 运行时 —— 可整体停止并按新配置重建。

    这样 `/maa reload` 与重启进程效果一致：日志另开新文件，
    onebot / log / debug / bot.data_dir 等段全部重新初始化，
    config.json 中删除的配置与设置也会真正失效。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api: Optional[OneBotAPI] = None
        self.ws: Optional[OneBotWS] = None
        self.dm: Optional[DataManager] = None
        self.llm: Optional[LLMPool] = None
        self.service: Optional[MaaService] = None
        self.history = None
        self.analyzer = None
        self.followup = None
        self.watcher = None
        self.messages = None
        self.handler = None
        self._tasks: list[asyncio.Task] = []

    # ════════════════════════════════════════════════════════════
    # 启动
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        cfg = self.cfg
        ob = cfg.get("onebot") or {}
        bot_cfg = cfg.get("bot") or {}
        llm_cfg = cfg.get("llm") or {}

        # 与重启一致：每次启动都重新初始化日志（另开新文件）
        setup_logging(cfg.get("log") or {})

        mode = str(ob.get("mode", "http_ws"))
        if mode not in MODES:
            raise ValueError(f"未知模式: {mode}，可选: {', '.join(MODES)}")

        token = ob.get("access_token", "")
        http_url = ob.get("http_url", "")
        ws_url = ob.get("ws_url", "")
        ws_reverse_port = int(ob.get("ws_reverse_port", 0))
        api_timeout = int(ob.get("api_timeout_seconds", 30) or 30)

        # HTTP 模式下 API 走 HTTP；WS / ws_reverse 模式下 api 走同一 WS 连接
        use_http = mode in ("http_ws", "http_ws_reverse")
        self.api = OneBotAPI(
            http_url=http_url if use_http else "",
            access_token=token,
            timeout_seconds=api_timeout,
        )
        self.ws = OneBotWS(access_token=token)
        if mode in ("ws", "ws_reverse"):
            self.api.set_ws_send(self.ws.send)
            self.ws.on_api_response(self.api.handle_ws_response)

        self.dm = DataManager(bot_cfg.get("data_dir", "data"))

        self.llm = build_llm_pool(llm_cfg, UsageStore(self.dm._dir))
        if not self.llm.configured:
            logger.warning("⚠ 未配置可用的 llm.providers[]，AI 分析将无法工作！")
        for line in self.llm.describe_all():
            logger.info(f"[LLM] {line}")

        self.service = MaaService(api=self.api, dm=self.dm)
        self.service.load()
        self.service.sync_from_config(bot_cfg.get("configs") or {})
        self.service.register_runtime_reload(self._handle_reload)

        if not self.service.configs:
            logger.warning("⚠ 未配置任何监听群，请在 config.json → bot.configs 中设置！")
        else:
            for name, state in self.service.configs.items():
                groups = ", ".join(sorted(state.info.listen_groups)) or "（无）"
                notify = state.info.notify_group or "（未设置）"
                logger.info(f"配置「{name}」：监听群 {groups}，通知群 {notify}")
                candidates = " → ".join(
                    f"{ref.name}/{ref.model}" for ref in state.llm.models
                ) or "（未声明）"
                logger.info(f"配置「{name}」模型候选：{candidates}")
                logger.info(
                    f"配置「{name}」报告：render={state.report.render_mode()}"
                    f" · template={state.report.template}"
                    f" · vision={'开' if state.vision.enabled else '关'}"
                )

        # 日志包监听 + 指令/追问答疑
        # 历史存储：归档日志包 + 追问上下文（周期性清理，git 缓存不受影响）
        self.history = HistoryStore(self.dm._dir)
        self.analyzer = MaaAnalyzer(self.service, self.llm, history=self.history)
        self.followup = FollowupStore(history=self.history)
        self.watcher = LogWatcher(self.service, self.analyzer, self.followup,
                                  history=self.history)
        self.messages = MessageHandler(
            self.service, self.analyzer, self.followup, history=self.history,
            segment_sender=self.watcher.send_segments,
        )

        self.service.register_event("notice.group_upload", self.watcher.on_group_upload)
        self.service.register_event("message.file", self.watcher.on_message_file)
        self.service.register_event("message.text", self.messages.on_message_text)

        self.handler = EventHandler(self.api, self.service)
        self.ws.on_event("message", self.handler.on_message)
        self.ws.on_event("notice", self.handler.on_notice)
        self.ws.on_event("request", self.handler.on_request)

        self._tasks = [
            asyncio.create_task(self._detect_backend()),
            asyncio.create_task(_history_cleanup_loop(self.history, self.service)),
        ]
        self._tasks.extend(self.analyzer.start_repo_preloads())
        if mode in ("http_ws", "ws"):
            self._tasks.append(asyncio.create_task(self.ws.connect(ws_url)))
        if mode in ("ws_reverse", "http_ws_reverse"):
            self._tasks.append(
                asyncio.create_task(self.ws.serve("0.0.0.0", ws_reverse_port))
            )

        debug_cfg = cfg.get("debug") or {}
        if debug_cfg.get("enabled", False):
            # 调试模块仅存在于源码仓库，不随发布包分发
            try:
                from debug.http_server import start_http_server
            except ImportError as exc:
                raise RuntimeError(
                    "配置启用了 debug.enabled，但未找到 debug 模块。"
                    "调试模式仅适用于源码仓库（发布包不含 debug/）。"
                    "请将 config.json 中的 debug.enabled 设为 false 后重启。"
                ) from exc
            http_port = int(debug_cfg.get("http_port", 8765))
            self._tasks.append(asyncio.create_task(
                start_http_server(http_port, self.service, cfg)
            ))

        logger.info(f"OnebotMaaLogAnalyzer 已就绪 (mode={mode})，等待日志包上传...")

    async def _detect_backend(self) -> None:
        """启动时探测协议端（用于兼容不同 OneBot 实现的群文件接口）。"""
        await asyncio.sleep(3)
        try:
            await self.service.detect_backend()
        except Exception as exc:
            logger.warning(f"协议端探测失败：{exc}")

    # ════════════════════════════════════════════════════════════
    # 停止 / 重建
    # ════════════════════════════════════════════════════════════

    async def stop(self) -> None:
        """停止所有后台任务与连接，并保存数据（相当于进程退出的收尾）。"""
        if self.watcher is not None:
            # 丢弃尚未触发的分卷计时，避免重载后仍用旧配置分析
            self.watcher.cancel_pending()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.ws is not None:
            try:
                await self.ws.stop()
            except Exception as exc:
                logger.warning(f"停止 WS 失败：{exc}")
        if self.api is not None:
            try:
                await self.api.close()
            except Exception as exc:
                logger.warning(f"关闭 API 失败：{exc}")
        if self.service is not None:
            try:
                self.service.save()
            except Exception as exc:
                logger.warning(f"保存配置失败：{exc}")
        if self.llm is not None:
            try:
                await self.llm.close()
            except Exception as exc:
                logger.warning(f"关闭 LLM 连接池失败：{exc}")
        try:
            from features.maa.render import close_all

            close_all()
        except Exception as exc:
            logger.warning(f"关闭渲染浏览器失败：{exc}")

    async def _handle_reload(self, new_cfg: dict,
                             notify_group: str = "") -> tuple[bool, str]:
        """`/maa reload` 的实际执行：整体重建，失败时回退到旧配置。"""
        previous = self.cfg
        logger.info("[重载] 正在停止当前运行时 ...")
        await self.stop()
        self.cfg = new_cfg
        try:
            await self.start()
        except Exception as exc:
            logger.exception("[重载] 按新配置启动失败，回退到重载前的配置")
            self.cfg = previous
            try:
                await self.stop()
                await self.start()
            except Exception:
                logger.exception("[重载] 回退启动也失败，请重启进程")
                return False, f"重载失败，且回退失败：{exc}"
            await self._notify(notify_group, f"重载失败，已回退到旧配置：{exc}")
            return False, f"重载失败，已回退到旧配置：{exc}"

        await self._notify(
            notify_group,
            "重载完成：配置、日志文件、LLM 与连接均已按新设置生效。",
        )
        return True, "已重新加载并重建运行时。"

    async def _notify(self, group_id: str, text: str) -> None:
        if not group_id or self.service is None:
            return
        try:
            await self.service.send_text(int(group_id), text)
        except Exception as exc:
            logger.warning(f"[重载] 发送结果消息失败：{exc}")

    async def supervise(self) -> None:
        """等待后台任务结束；重载替换任务列表后继续等待新任务。"""
        while True:
            tasks = list(self._tasks)
            if not tasks:
                await asyncio.sleep(1)
                continue
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.error(f"后台任务异常退出：{exc}", exc_info=exc)
            if list(self._tasks) == tasks and all(t.done() for t in tasks):
                return


def _validate_ai_config(cfg: dict) -> bool:
    """启动前校验 LLM 结构：旧结构 / 无效引用都拒绝启动。"""
    llm_cfg = cfg.get("llm") or {}
    legacy = detect_legacy_llm(llm_cfg)
    if legacy:
        logger.error(legacy)
        return False
    configs_cfg = (cfg.get("bot") or {}).get("configs") or {}
    errors = validate_llm_config(llm_cfg, configs_cfg)
    if errors:
        logger.error("[配置] LLM 配置无效，已拒绝启动：")
        for error in errors:
            logger.error(f"  - {error}")
        logger.error("可运行 `python main.py --print-config-template` 查看新模板。")
        return False
    return True


async def main():
    logger.info("OnebotMaaLogAnalyzer 启动中...")

    cfg = load_config()
    if not _validate_ai_config(cfg):
        sys.exit(2)
    runtime = BotRuntime(cfg)
    try:
        await runtime.start()
    except Exception as exc:
        logger.error(f"启动失败：{exc}")
        await runtime.stop()
        sys.exit(1)

    debug_enabled = bool((cfg.get("debug") or {}).get("enabled", False))
    if debug_enabled:
        try:
            from debug.cli import run_cli
        except ImportError:
            logger.error(
                "配置启用了 debug.enabled，但未找到 debug 模块。\n"
                "调试模式仅适用于源码仓库（发布包不含 debug/）。\n"
                "请将 config.json 中的 debug.enabled 设为 false 后重启。"
            )
            await runtime.stop()
            sys.exit(1)

        try:
            await run_cli(runtime.service, cfg)
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        finally:
            await runtime.stop()
            logger.info("数据已保存，退出。")
        return

    try:
        await runtime.supervise()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        await runtime.stop()
        logger.info("数据已保存，退出。")


if __name__ == "__main__":
    if "--print-config-template" in sys.argv[1:]:
        _print_config_template()
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
