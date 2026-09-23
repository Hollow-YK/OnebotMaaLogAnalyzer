"""
路径安全策略 — 判断某个相对路径是否**禁止外发**。

附件功能会把日志包或项目仓库中的文件发送到群聊，因此必须挡住
凭据类文件，避免密钥、令牌、私钥被无意发到群里。

被拒绝的类别：
  - 版本控制内部目录：`.git` / `.hg` / `.svn`（remote URL 常含 token）
  - 云凭据目录：`.ssh` / `.aws` / `.gnupg` / `.docker` / `.kube`
  - 环境变量文件：`.env` / `.env.local` / `.env.production` ...
  - 凭据清单：`credentials` / `.netrc` / `.npmrc` / `.pgpass` / `.htpasswd`
  - 私钥与证书：`.pem` / `.key` / `.pfx` / `.p12` / `.ppk` / `id_rsa*` / `id_ed25519*`

这些规则对**日志包内**与**项目仓库内**的路径同样生效。
"""
from __future__ import annotations

from pathlib import PurePosixPath

# 整个目录禁止（出现在路径任意层级都拒绝）
SENSITIVE_DIRS = {
    ".git", ".hg", ".svn", ".bzr",
    ".ssh", ".aws", ".gnupg", ".gpg",
    ".docker", ".kube", ".azure", ".config",
}

# 精确文件名（小写比较）
SENSITIVE_NAMES = {
    "credentials", "credential", "secrets", "secret",
    ".netrc", "_netrc", ".npmrc", ".pgpass", ".htpasswd",
    ".git-credentials", ".gitconfig", ".dockercfg", ".pypirc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "authorized_keys", "known_hosts",
    "keystore", "truststore",
}

# 文件名前缀（小写比较）
SENSITIVE_PREFIXES = (
    ".env",          # .env / .env.local / .env.production
    "id_rsa",        # id_rsa / id_rsa.pub
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "secrets",
)

# 扩展名（小写比较）
SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".ppk",
    ".keystore", ".jks", ".asc", ".gpg", ".kdbx",
}


def is_sensitive_path(rel: str) -> bool:
    """
    判断相对路径是否涉及凭据 / 密钥，禁止外发。

    路径已由 normalize_rel_path() 规范化（相对、无 .. 上跳）。
    """
    text = str(rel or "").strip().replace("\\", "/").lower()
    if not text:
        return False

    parts = PurePosixPath(text).parts
    if not parts:
        return False

    # 任一目录层级命中即拒绝（如 sub/.git/config）
    if any(part in SENSITIVE_DIRS for part in parts[:-1]):
        return True
    # 目录名本身也检查（防止把目录当文件请求）
    if any(part in SENSITIVE_DIRS for part in parts):
        return True

    name = parts[-1]
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(SENSITIVE_PREFIXES):
        return True
    if PurePosixPath(name).suffix in SENSITIVE_SUFFIXES:
        return True

    return False


def describe_rejection(rel: str) -> str:
    """生成拒绝原因说明（供日志与提示使用）。"""
    return f"{rel}（涉及凭据或密钥，已拒绝外发）"
