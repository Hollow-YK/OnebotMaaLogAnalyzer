"""
Markdown → HTML（报告正文）。

职责：
  - 预解析内容块标记 `<!-- section: 标题 -->` 与分页标记 `<!-- page-break -->`
  - 用 mistune 把每个内容块转成 HTML（禁用原始 HTML）
  - 标签白名单 + 链接/图片属性收敛（AI 输出不可信）
  - 把 `[附件@...]` 指令替换为模板可定制的外壳
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

from features.maa.attachments import (
    Attachment,
    _ATTACHMENT_DIRECTIVE,
    _split_source_and_path,
    normalize_rel_path,
)

logger = logging.getLogger("Maa.Render.Markdown")

_SECTION_RE = re.compile(
    r"^\s*<!--\s*(?:section|小节)\s*[:：]\s*(?P<title>.*?)\s*-->\s*$",
    re.IGNORECASE,
)
_PAGE_BREAK_RE = re.compile(
    r"^\s*<!--\s*(?:page-break|分页)\s*-->\s*$",
    re.IGNORECASE,
)
_LEGACY_STRIP = re.compile(
    r"[\[【]\s*(?:附图|图片|image)\s*(?:[@＠][^:：\]]*)?[：:][^\]】\n]*[\]】]",
    re.IGNORECASE,
)
_VISION_STRIP = re.compile(
    r"[\[【]\s*(?:看图|读图|vision)\s*(?:[@＠][^:：\]]*)?[：:][^\]】\n]*[\]】]",
    re.IGNORECASE,
)
_PAGE_BREAK_TOKEN = "<!-- __PAGE_BREAK__ -->"
_LEAD_TITLE = "概览"

# AI 文本允许产出的标签（结构标签由插件自己生成，不经过白名单）
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "p", "ul", "ol", "li", "strong", "em", "del",
    "code", "pre", "blockquote", "hr", "table", "thead", "tbody", "tr",
    "th", "td", "a", "img", "br", "span",
}
_VOID_TAGS = {"hr", "img", "br"}


@dataclass
class ContentBlock:
    """一个内容块（对应一个 `<!-- section -->`）。"""

    title: str
    lead: bool = False
    html: str = ""
    force_break_after: bool = False


@dataclass
class ParsedReport:
    """解析后的报告：有序内容块 + 去掉内部标记的 Markdown。"""

    blocks: list[ContentBlock] = field(default_factory=list)
    clean_markdown: str = ""
    warnings: list[str] = field(default_factory=list)


class _Sanitizer(HTMLParser):
    """只保留白名单标签，并收敛属性。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        safe_attrs: list[str] = []
        attribute_map = {str(k).lower(): v for k, v in attrs}
        if tag == "a":
            href = str(attribute_map.get("href") or "")
            if re.match(r"^https?://", href, re.IGNORECASE):
                safe_attrs.append(f'href="{html.escape(href, quote=True)}"')
        elif tag == "img":
            src = str(attribute_map.get("src") or "")
            if not src.startswith("data:image/"):
                return
            safe_attrs.append(f'src="{html.escape(src, quote=True)}"')
            alt = str(attribute_map.get("alt") or "")
            safe_attrs.append(f'alt="{html.escape(alt, quote=True)}"')
        joined = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self.parts.append(f"<{tag}{joined}>")

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str):
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str):
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str):
        return


def sanitize_html(fragment: str) -> str:
    parser = _Sanitizer()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception as exc:  # 防御性兜底
        logger.warning(f"[渲染] HTML 清洗失败，按纯文本处理：{exc}")
        return f"<p>{html.escape(fragment)}</p>"
    return "".join(parser.parts)


def _markdown_engine():
    """惰性创建 mistune 引擎（未安装时返回 None）。"""
    try:
        import mistune
    except ImportError:  # pragma: no cover - 依赖缺失时的降级路径
        logger.warning("[渲染] 未安装 mistune，报告将回退为纯文本")
        return None
    try:
        return mistune.create_markdown(
            escape=True,
            hard_wrap=True,
            plugins=["strikethrough", "table"],
        )
    except TypeError:
        return mistune.create_markdown(escape=True)


def _convert_markdown(text: str, engine) -> str:
    if not text.strip():
        return ""
    if engine is None:
        return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"
    try:
        converted = engine(text)
    except Exception as exc:
        logger.warning(f"[渲染] Markdown 转换失败：{exc}")
        converted = "<p>" + html.escape(text) + "</p>"
    return sanitize_html(converted)


def _split_markers(report: str) -> list[tuple[str, str, bool]]:
    """
    把报告切成 (标题, markdown, 强制分页) 列表。

    空标题的 section 并入上一块；首个标记前的内容归入「概览」。
    """
    lines = str(report or "").splitlines()
    blocks: list[tuple[str, list[str]]] = []
    current_title: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_lines
        # 空块（只有标题、没有任何正文）不产出，避免图片里出现孤零零的空小节
        has_body = any(
            line.strip() and line.strip() != _PAGE_BREAK_TOKEN
            for line in current_lines
        )
        if not has_body:
            current_title = None
            current_lines = []
            return
        blocks.append((current_title or _LEAD_TITLE, current_lines))
        current_title = None
        current_lines = []

    for line in lines:
        section = _SECTION_RE.match(line)
        if section:
            title = section.group("title").strip()
            if not title:
                continue
            flush()
            current_title = title
            continue
        if _PAGE_BREAK_RE.match(line):
            if current_title is None and not any(l.strip() for l in current_lines):
                continue
            current_lines.append(_PAGE_BREAK_TOKEN)
            continue
        current_lines.append(line)

    flush()

    result: list[tuple[str, str, bool]] = []
    for title, body_lines in blocks:
        force_break = any(_PAGE_BREAK_TOKEN in line for line in body_lines)
        body = "\n".join(
            line for line in body_lines if _PAGE_BREAK_TOKEN not in line
        )
        result.append((title, body, force_break))
    return result


def parse_report(report: str,
                 attachment_html: Optional[Callable[[Attachment], str]] = None,
                 ) -> ParsedReport:
    """
    解析报告为内容块。

    attachment_html: 把附件指令转成 HTML 的回调；None 时指令被忽略。
    """
    parsed = ParsedReport()
    engine = _markdown_engine()

    for title, body, force_break in _split_markers(report):
        body, attachment_parts = _replace_attachments(body, attachment_html)
        markdown_html = _convert_markdown(body, engine)
        html_parts = _interleave(markdown_html, attachment_parts)
        lead = title == _LEAD_TITLE
        classes = "block block-lead" if lead else "block"
        parsed.blocks.append(ContentBlock(
            title=title,
            lead=lead,
            html=(
                f'<section class="{classes}">'
                f'<h2 class="block-title">{html.escape(title)}</h2>'
                f"{html_parts}"
                f"</section>"
            ),
            force_break_after=force_break,
        ))

    parsed.clean_markdown = clean_markdown(report)
    return parsed


def _replace_attachments(body: str,
                         renderer: Optional[Callable[[Attachment], str]]
                         ) -> tuple[str, list[str]]:
    """把附件指令替换为占位符，并生成对应 HTML。"""
    parts: list[str] = []

    def substitute(match: re.Match) -> str:
        source, raw_path = _split_source_and_path(
            match.group("label"), match.group("src1") or "", match.group("path")
        )
        pieces = [
            piece.strip()
            for piece in re.split(r"[,，、;；]", raw_path)
            if piece.strip()
        ]
        rendered: list[str] = []
        for piece in pieces:
            path = normalize_rel_path(piece)
            if path is None or not source:
                continue
            attachment = Attachment(path=path, kind="", source=source)
            if renderer is not None:
                rendered.append(renderer(attachment))
        if not rendered:
            return ""
        index = len(parts)
        parts.append("".join(rendered))
        return f"\x00ATTACH:{index}\x00"

    text = _ATTACHMENT_DIRECTIVE.sub(substitute, body)
    return text, parts


def _interleave(markdown_html: str, parts: list[str]) -> str:
    """把占位符替换为附件 HTML。"""
    for index, fragment in enumerate(parts):
        markdown_html = markdown_html.replace(f"\x00ATTACH:{index}\x00", fragment)
    return markdown_html


def clean_markdown(report: str) -> str:
    """移除内部标记，保留用户可读的 Markdown（用于可选 .md 附件）。"""
    text = _SECTION_RE.sub("", str(report or ""))
    text = _PAGE_BREAK_RE.sub("", text)
    text = _VISION_STRIP.sub("", text)
    text = _LEGACY_STRIP.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
