"""Update checking, notification and automatic install orchestration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..core.concurrency import run_blocking
from ..core.models import UpdateConfig
from .github import (
    CheckResult,
    GitHubReleaseClient,
    ReleaseInfo,
    normalize_channel,
)
from .install import InstallInfo, detect_install
from .installer import PreparedUpdate, VersionInstaller
from .net import HttpClient
from .state import atomic_write_json, mask_secret, read_json
from .versioning import compare_versions

logger = logging.getLogger("Maa.Update")

STARTUP_DELAY_SECONDS = 10
MIN_RECHECK_SECONDS = 300
CHUNK_CHARS = 3000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class ApplyOutcome:
    ok: bool
    message: str
    version: str = ""
    mode: str = ""


class UpdateManager:
    """Own the background check loop and the auto-update transaction."""

    def __init__(
        self,
        *,
        data_dir: str | Path,
        cfg: dict[str, Any] | None = None,
        install: InstallInfo | None = None,
        config_path: str | Path | None = None,
        service: Any | None = None,
        restart_callback: Callable[[], None] | None = None,
        background: bool = True,
    ):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.install = install or detect_install()
        self.config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )
        self.service = service
        self.restart_callback = restart_callback
        self.background = bool(background)
        self.config = self._parse_config(cfg or {})
        self.state_path = self.data_dir / "updates" / "state.json"
        self.result_path = self.data_dir / "updates" / "last-result.json"
        self._state = self._load_state()
        self._http = HttpClient(token=self.config.github_token)
        self._task: asyncio.Task[None] | None = None
        self._check_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()
        self._last_outcome: CheckResult | None = None

    # ── config / state ───────────────────────────────────────────

    @staticmethod
    def _parse_config(raw: dict[str, Any]) -> UpdateConfig:
        try:
            return UpdateConfig.model_validate(raw or {})
        except ValidationError as exc:
            logger.warning("[更新] update 配置无效，使用默认值：%s", exc)
            return UpdateConfig()

    def _load_state(self) -> dict[str, Any]:
        data = read_json(self.state_path, {})
        if not isinstance(data, dict):
            return {}
        for key in ("etags", "cached", "notified"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save_state(self) -> None:
        try:
            atomic_write_json(self.state_path, self._state)
        except OSError as exc:
            logger.warning("[更新] 保存状态失败：%s", exc)

    def _last_result(self) -> dict[str, Any]:
        data = read_json(self.result_path, {})
        return data if isinstance(data, dict) else {}

    def _write_result(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _now()
        try:
            atomic_write_json(self.result_path, data)
        except OSError as exc:
            logger.warning("[更新] 保存结果失败：%s", exc)

    # ── lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        if not self.background:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="maa-update-check")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._http.close()

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY_SECONDS)
            await self._report_previous_result()
            if not self.config.check_enabled:
                return
            if not self.config.check_on_start:
                await asyncio.sleep(self.config.check_interval_hours * 3600)
            while True:
                try:
                    if self._should_check_now():
                        await self.check(auto_apply=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[更新] 自动检查失败，等待下次周期")
                await asyncio.sleep(self.config.check_interval_hours * 3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[更新] 自动检查循环异常退出")

    def _should_check_now(self) -> bool:
        last = str(self._state.get("last_check_at") or "")
        if not last:
            return True
        try:
            from datetime import datetime as _dt

            previous = _dt.fromisoformat(last)
            age = (_dt.now(previous.tzinfo) - previous).total_seconds()
            return age >= MIN_RECHECK_SECONDS
        except (ValueError, TypeError):
            return True

    # ── checking ────────────────────────────────────────────────

    async def check(
        self,
        *,
        channel: str | None = None,
        auto_apply: bool = False,
        notify: bool = True,
        force: bool = False,
    ) -> CheckResult:
        """Query all API bases once and optionally auto-install the newest."""
        async with self._check_lock:
            chosen = normalize_channel(channel or self.config.channel)
            same_channel = (
                str(self._state.get("last_channel") or self.config.channel)
                == chosen
            )
            client = GitHubReleaseClient(
                self._http,
                self.config.api_bases,
                channel=chosen,
            )
            result = await client.fetch(
                etags=(self._state.get("etags") or {}) if same_channel else {},
                cache=(self._state.get("cached") or {}) if same_channel else {},
            )
            self._last_outcome = result
            self._state["last_check_at"] = _now()
            self._state["etags"] = result.etags
            self._state["cached"] = result.cached
            self._state["last_channel"] = chosen
            self._save_state()

            if result.all_failed:
                if notify:
                    await self._notify_failure(
                        "检查更新失败", self._format_errors(result)
                    )
                return result

            current = detect_install()
            latest = result.latest
            if latest is None or compare_versions(
                latest.version_text, current.version
            ) <= 0:
                return result

            if notify:
                await self._notify_new_version(latest, chosen, current)
            if auto_apply and self.config.auto_update:
                if current.source:
                    key = f"auto-disabled:{latest.version_text}"
                    if self._state.get("notified", {}).get(key) != latest.version_text:
                        await self._notify(
                            "当前从源码运行，自动更新已禁用；"
                            "请使用 git pull 更新。"
                        )
                        self._state.setdefault("notified", {})[key] = (
                            latest.version_text
                        )
                        self._save_state()
                elif latest.version_text in current.bad_versions:
                    key = f"bad-skip:{latest.version_text}"
                    if self._state.get("notified", {}).get(key) != latest.version_text:
                        await self._notify(
                            f"上次更新 {latest.version_text} 失败并已回退，"
                            "自动更新将跳过该版本；修复后可手动 /maa update apply。"
                        )
                        self._state.setdefault("notified", {})[key] = (
                            latest.version_text
                        )
                        self._save_state()
                else:
                    await self.apply(latest, force=False)
            return result

    def _format_errors(self, result: CheckResult) -> str:
        lines = ["已尝试："]
        for base, error in result.errors:
            lines.append(f"- {base}：{error}")
        lines.append("不会自动重试。")
        return "\n".join(lines)

    async def _notify_new_version(
        self, release: ReleaseInfo, channel: str, current: InstallInfo
    ) -> None:
        key = f"{channel}:{release.version_text}"
        notified = self._state.setdefault("notified", {})
        if notified.get(key) == release.version_text:
            return
        text = self.format_release_message(release, channel, current)
        if await self._notify(text):
            notified[key] = release.version_text
            self._save_state()

    def format_release_message(
        self, release: ReleaseInfo, channel: str, current: InstallInfo
    ) -> str:
        label = "测试版" if release.prerelease else "正式版"
        del channel
        lines = [
            "【OnebotMaaLogAnalyzer 更新】",
            f"发现新版本：{release.tag or release.version_text}（{label}）",
            f"当前版本：{current.version}（{current.kind}）",
            f"发布页：{release.html_url or '-'}",
        ]
        body = "\n".join(
            line for line in str(release.body or "").splitlines() if line.strip()
        )
        if body:
            excerpt = body[:1200]
            lines.extend(["", "更新内容摘要：", excerpt])
            if len(body) > len(excerpt):
                lines.append("……")
        lines.extend(
            [
                "",
                "自动更新："
                + (
                    "已开启"
                    if self.config.auto_update and not current.source
                    else "已关闭"
                ),
                "发送 /maa update apply 可手动更新。",
            ]
        )
        return "\n".join(lines)

    # ── applying ────────────────────────────────────────────────

    async def apply(
        self,
        release: ReleaseInfo | None = None,
        *,
        force: bool = False,
        channel: str | None = None,
    ) -> ApplyOutcome:
        if release is None:
            result = await self.check(
                channel=channel, notify=False, force=force
            )
            release = result.latest
            if release is None and result.errors:
                detail = self._format_errors(result)
                await self._notify_failure("检查更新失败", detail)
                return ApplyOutcome(
                    False, "检查更新失败：\n" + detail
                )
        if release is None:
            return ApplyOutcome(False, "没有可用更新。")
        async with self._apply_lock:
            try:
                return await self._apply_locked(release, force=force)
            except Exception as exc:
                logger.exception("[更新] 安装失败")
                await self._notify_failure("更新失败", str(exc))
                self._write_result(
                    {
                        "status": "failed",
                        "version": release.version_text,
                        "error": str(exc),
                        "reported": True,
                    }
                )
                return ApplyOutcome(False, f"更新失败：{exc}")

    async def _apply_locked(
        self, release: ReleaseInfo, *, force: bool = False
    ) -> ApplyOutcome:
        current = detect_install()
        if current.source:
            raise RuntimeError("源码运行不支持自动更新，请使用 git pull。")
        if compare_versions(release.version_text, current.version) <= 0:
            raise RuntimeError(
                f"目标版本 {release.version_text} 不高于当前版本 {current.version}。"
            )
        if release.version_text in current.bad_versions and not force:
            raise RuntimeError(
                f"版本 {release.version_text} 上次启动失败并已回退，"
                "自动更新已跳过；如已修复可手动 /maa update apply。"
            )
        if not self.config.auto_update and not force:
            raise RuntimeError("自动更新未启用。")
        installer = VersionInstaller(
            install=current,
            data_dir=self.data_dir,
            config_path=self.config_path,
            download_mirrors=self.config.download_mirrors,
            http=self._http,
        )
        try:
            prepared = await installer.prepare(
                release, download_mode=self.config.download_mode
            )
            pointer = await run_blocking(installer.activate, prepared)
        finally:
            await installer.close()
        self._write_result(
            {
                "status": "activated",
                "version": release.version_text,
                "previous": pointer.get("previous") or "",
                "active": pointer.get("active") or "",
                "mode": prepared.mode,
                "changed": len(prepared.changed),
                "downloaded_bytes": prepared.downloaded_bytes,
                "reported": False,
            }
        )
        if self.restart_callback is not None:
            await self._notify(
                f"更新 {release.version_text} 已下载并校验完成，正在重启…"
            )
            self.restart_callback()
            return ApplyOutcome(
                True,
                f"已准备 {release.version_text}，正在重启。",
                release.version_text,
                prepared.mode,
            )
        await self._notify(
            f"更新 {release.version_text} 已下载并校验完成。\n"
            "请在 AstrBot 面板重载插件以启用新版本；"
            "若启动失败，引导器会自动回退。"
        )
        return ApplyOutcome(
            True,
            f"已安装 {release.version_text}，请重载插件。",
            release.version_text,
            prepared.mode,
        )

    async def request_apply(self, channel: str | None = None) -> str:
        """Start an update in the background and return a user-facing message."""
        if self.install.source:
            return "当前从源码运行，不支持自动更新；请使用 git pull。"
        # Manual ``/maa update apply`` is allowed even when auto is off.
        task = asyncio.create_task(
            self.apply(None, force=True, channel=channel),
            name="maa-update-apply",
        )
        task.add_done_callback(self._log_task)
        chosen = normalize_channel(channel or self.config.channel)
        return f"已开始检查并安装 {chosen} 通道的最新版本，完成后会私聊通知。"

    @staticmethod
    def _log_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("[更新] 后台更新任务异常")

    # ── notification ────────────────────────────────────────────

    async def _notify(self, text: str) -> bool:
        return await self._notify_qq(text)

    async def _notify_failure(self, title: str, detail: str) -> bool:
        text = f"【OnebotMaaLogAnalyzer {title}】\n{detail}"
        return await self._notify_qq(text)

    async def _notify_qq(self, text: str) -> bool:
        if not self.config.notify_qq:
            logger.warning("[更新] 未配置 update.notify_qq，跳过通知：%s", text[:80])
            return False
        sender = getattr(self.service, "send_private_text", None)
        if not callable(sender):
            logger.warning("[更新] 当前平台不支持私聊通知。")
            return False
        ok = False
        chunks = _chunk_text(text, CHUNK_CHARS)
        for qq in self.config.notify_qq:
            try:
                delivered = True
                for chunk in chunks:
                    result = await sender(int(str(qq)), chunk)
                    if result is None:
                        delivered = False
                        break
                if delivered:
                    ok = True
                else:
                    logger.warning(
                        "[更新] 私聊通知 %s 未被协议端接受。",
                        mask_secret(str(qq)),
                    )
            except Exception as exc:
                logger.warning(
                    "[更新] 私聊通知 %s 失败：%s", mask_secret(str(qq)), exc
                )
        return ok

    async def _report_previous_result(self) -> None:
        result = self._last_result()
        if not result or result.get("reported"):
            return
        status = str(result.get("status") or "")
        version = str(result.get("version") or "")
        try:
            current = detect_install()
        except Exception:
            return
        if status == "activated":
            if compare_versions(current.version, version) >= 0:
                await self._notify(f"已更新到 {version} 并成功启动。")
            else:
                await self._notify(
                    f"更新到 {version} 未生效，已回退到 {current.version}。"
                )
        elif status == "failed":
            await self._notify(
                f"上次更新 {version} 失败：{result.get('error') or '未知错误'}"
            )
        else:
            return
        result["reported"] = True
        result["reported_at"] = _now()
        self._write_result(result)

    # ── status ─────────────────────────────────────────────────

    def describe_status(self) -> str:
        current = detect_install()
        last = str(self._state.get("last_check_at") or "从未检查")
        result = self._last_result()
        lines = [
            "更新设置：",
            f"  当前版本：{current.version}",
            f"  安装类型：{current.kind}"
            + ("（源码运行，自动更新不可用）" if current.source else ""),
            f"  检查更新：{'开启' if self.config.check_enabled else '关闭'}",
            f"  通道：{self.config.channel}",
            f"  自动更新：{'开启' if self.config.auto_update else '关闭'}",
            f"  下载模式：{self.config.download_mode}",
            f"  通知 QQ：{', '.join(mask_secret(q) for q in self.config.notify_qq) or '（未设置）'}",
            f"  API：{', '.join(self.config.api_bases)}",
            f"  上次检查：{last}",
        ]
        if result:
            lines.append(
                "  最近结果："
                + str(result.get("status") or "-")
                + " "
                + str(result.get("version") or "")
            )
        return "\n".join(lines)

    def format_check_result(self, result: CheckResult) -> str:
        current = detect_install()
        if result.latest is None:
            if result.errors:
                return "检查更新失败：\n" + self._format_errors(result)
            return f"当前已是最新版本：{current.version}（{self.config.channel}）。"
        if compare_versions(result.latest.version_text, current.version) <= 0:
            return (
                f"当前已是最新版本：{current.version}"
                f"（通道 {self.config.channel}，最新 {result.latest.version_text}）。"
            )
        return self.format_release_message(
            result.latest, self.config.channel, current
        )


def _chunk_text(text: str, limit: int) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


__all__ = ["ApplyOutcome", "UpdateManager"]
