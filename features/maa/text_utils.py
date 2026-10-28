from __future__ import annotations

# 单行输出上限（字符）。超过时截断并标注——否则一行超长 JSON 会吃掉整段结果预算。
MAX_LINE_CHARS = 2000


def truncate_line(text: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """把单行裁剪到 `max_chars`，并标注这是行内截断（不是文件截断）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... (行已截断到 {max_chars} 字符)"


def decode_bytes(data: bytes) -> str:
    """按常见日志编码解码字节串。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_bytes(size: int | float | None) -> str:
    if size is None:
        return "未知大小"
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "未知大小"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.2f} {units[index]}"


def limit_text(text: str, max_chars: int, suffix: str = "\n...[已截断]") -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def split_text(text: str, max_chars: int = 3500) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > max_chars:
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks
