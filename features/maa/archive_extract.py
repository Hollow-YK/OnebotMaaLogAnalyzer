"""
安全解压日志压缩包 — 让文件工具按**普通文件**读取，而不是在内存里解压 ZIP 成员。

对齐 dsh 的做法：模型侧的文件工具面向真实文件系统（`read` / 搜索 / 列目录），
解压是一次性的准备工作。这样：

  - 读取逻辑与仓库工具一致（行号窗口、单行宽度、字节上限、续读提示）；
  - 多次工具调用不必反复打开压缩包、重建成员索引；
  - 内存占用与成员大小无关（解压是流式落盘，不是整体读入）。

解压本身是攻击面，所以所有校验都在**写盘之前**完成：

  - 成员名走 `normalize_tool_path`：拒绝绝对路径、盘符、`..` 与空字节
  - 拒绝符号链接条目（zip 的高位属性），避免把链接指向根目录之外
  - 拒绝凭据 / 密钥类文件名（`.env`、`.git`、私钥等）
  - 单文件、总字节与成员数三重上限；按**实际写出字节**计数，不信任 zip 头声明
  - 落盘路径必须位于解压根目录内（`resolve` 后再校验一次）

失败一律降级：跳过该成员/停止解压并记录 notes，绝不向调用方抛异常。
"""
from __future__ import annotations

import logging
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from features.maa.safety import (
    is_sensitive_path,
    normalize_tool_path,
    resolve_repo_path,
)

logger = logging.getLogger("Maa.ArchiveExtract")

_CHUNK_BYTES = 64 * 1024


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    """zip 成员是否声明为符号链接（unix 模式位在高 16 位）。"""
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


class ExtractedArchive:
    """一个已安全解压到临时目录的压缩包（按需解压、可重复读取）。"""

    def __init__(self, zip_path: str, *, max_total_bytes: int,
                 max_file_bytes: int, max_members: int = 3000):
        self.zip_path = str(zip_path or "")
        self.max_total_bytes = max(0, int(max_total_bytes or 0))
        self.max_file_bytes = max(0, int(max_file_bytes or 0))
        self.max_members = max(1, int(max_members or 3000))
        self.root: Optional[Path] = None
        # 已成功落盘的成员（posix 相对路径），按压缩包内顺序
        self.members: list[str] = []
        self.notes: list[str] = []
        self._done = False
        self._written = 0

    # ── 生命周期 ──

    def ensure(self) -> Optional[Path]:
        """按需解压一次；返回根目录，失败返回 None。"""
        if self._done:
            return self.root
        self._done = True
        if not self.zip_path or not Path(self.zip_path).is_file():
            self.notes.append("压缩包不存在或不可读")
            return None
        try:
            self.root = Path(tempfile.mkdtemp(prefix="maa_logs_"))
            self._extract()
            if not self.members:
                self.notes.append("压缩包内没有可读取的文件")
        except Exception as exc:   # 解压失败一律降级为“工具不可用”
            logger.warning(f"[解压] 失败，工具不可用：{exc}")
            self.notes.append(f"解压失败：{type(exc).__name__}")
            self.close()
        return self.root

    def close(self) -> None:
        """删除临时目录（分析/追问结束时调用，可重复调用）。"""
        root, self.root = self.root, None
        self.members = []
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    # ── 解压 ──

    def _extract(self) -> None:
        root = self.root
        assert root is not None
        with zipfile.ZipFile(self.zip_path) as archive:
            infos = archive.infolist()
            if len(infos) > self.max_members:
                self.notes.append(
                    f"压缩包成员 {len(infos)} 个，超过上限 {self.max_members}，"
                    f"只解压前 {self.max_members} 个"
                )
            for info in infos[: self.max_members]:
                if info.is_dir():
                    continue
                rel = normalize_tool_path(info.filename)
                if not rel:
                    self.notes.append(f"跳过非法路径：{info.filename}")
                    continue
                if is_sensitive_path(rel):
                    self.notes.append(f"跳过敏感文件：{rel}")
                    continue
                if _is_symlink_entry(info):
                    self.notes.append(f"跳过符号链接：{rel}")
                    continue
                if not self._extract_one(archive, info, rel):
                    # 总字节上限已到：后续成员不再解压
                    self.notes.append("已达总解压上限，其余文件未解压")
                    break

    def _extract_one(self, archive: zipfile.ZipFile,
                     info: zipfile.ZipInfo, rel: str) -> bool:
        """解压单个成员；返回 False 表示总字节上限已到，应停止。"""
        root = self.root
        assert root is not None
        candidate = root / rel
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # 先落一个空文件，再用严格解析确认真实路径仍在根目录内；
            # 解压过程只写我们自己创建的文件，不跟随任何符号链接。
            candidate.touch(exist_ok=True)
        except OSError as exc:
            self.notes.append(f"跳过无法创建的路径 {rel}：{exc.__class__.__name__}")
            return True
        target, _error = resolve_repo_path(root, rel)
        if target is None:
            self.notes.append(f"跳过越界路径：{rel}")
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        per_file_cap = self.max_file_bytes
        total_cap = self.max_total_bytes
        written = 0
        reason = ""
        try:
            with archive.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    # 先判断上限再写：written 始终等于真正落盘的字节数
                    if per_file_cap > 0 and written + len(chunk) > per_file_cap:
                        reason = "单文件上限"
                        break
                    if (total_cap > 0
                            and self._written + written + len(chunk) > total_cap):
                        reason = "总解压上限"
                        break
                    sink.write(chunk)
                    written += len(chunk)
        except Exception as exc:
            # 单个成员失败（损坏的压缩数据、非法文件名等）只跳过该成员
            self.notes.append(f"跳过无法解压的 {rel}：{exc.__class__.__name__}")
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        self._written += written
        self.members.append(rel)
        if reason:
            self.notes.append(
                f"{rel} 触发{reason}（仅保留前 {written // 1024} KB）"
            )
            return reason != "总解压上限"
        return True

    # ── 查询 ──

    def file_path(self, rel: str) -> Optional[Path]:
        """把包内相对路径解析为解压后的真实路径（越界/敏感返回 None）。"""
        root = self.root
        if root is None:
            return None
        path, _error = resolve_repo_path(root, rel)
        return path
