"""
HTML 模板加载与填充。

目录结构（工程根目录 `templates/`）：

    templates/
      default/
        page.html      # 必需，必须包含 {{content}}
        header.html    # 可选
        footer.html    # 可选
        style.css      # 可选

模板只描述「单页长什么样」；分页发生在正文内部，模板里不要写分页逻辑。
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Maa.Render.Template")

_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTENT = "{{content}}"
_HEADER = "{{header}}"
_FOOTER = "{{footer}}"
# 占位符的唯一来源：`page.html` / `header.html` / `footer.html` 完全共用这一套。
# 只有 `{{content}}` 例外 —— 它代表正文，只能出现在 page.html。
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")
_SCRIPT = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|<iframe\b[^>]*>.*?</iframe\s*>",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//[^"']*["']""",
    re.IGNORECASE,
)
_IMPORT = re.compile(r"@import\b[^;]*;", re.IGNORECASE)

# 页眉 / 页脚 / 页面骨架都可用（次序即文档展示顺序）
# 占位符分组（仅作文档参考；实际取值见 _DEFAULTS）：
#   元信息 title / project / file / generated_at
#   页码   page / pages（由参数注入）
#   用量   tokens_input / tokens_output / tokens_total / usage_calls / provider / model / usage
_DEFAULTS: dict[str, str] = {
    "title": "", "project": "", "file": "", "generated_at": "",
    "tokens_input": "0", "tokens_output": "0", "tokens_total": "0",
    "usage_calls": "0", "provider": "-", "model": "-", "usage": "",
}

# 未识别占位符原样保留，便于逐步调试。
# 从 _DEFAULTS 派生，新增占位符时不会出现「三处取值不一致」。
_KNOWN = {"content", "header", "footer", "page", "pages"} | set(_DEFAULTS)


def build_placeholder_values(meta: Optional[dict], page: int, pages: int
                             ) -> dict[str, str]:
    """
    构造一套占位符取值。

    `page.html` / `header.html` / `footer.html` 用**同一个** dict 填充，
    因此三者可用的占位符完全一致（除 `{{content}}` 只能用于 page.html）。
    """
    meta = meta or {}
    values: dict[str, str] = {}
    for key, default in _DEFAULTS.items():
        raw = meta.get(key)
        values[key] = html.escape(str(default if raw is None else raw))
    values["page"] = html.escape(str(page))
    values["pages"] = html.escape(str(pages))
    return values


_CACHE: dict[Path, tuple[float, str]] = {}


@dataclass
class TemplateSet:
    """一套已加载的模板。"""

    name: str
    page: str
    header: str = ""
    footer: str = ""
    style: str = ""
    warnings: list[str] = field(default_factory=list)

    def render_page(self, *, content: str, meta: dict, page: int,
                    pages: int) -> str:
        """
        填充单页 HTML（单趟替换，插入内容不会被二次解析）。

        `page.html` / `header.html` / `footer.html` 使用**同一份取值**，
        因此三者可用的占位符完全一致：报告元信息、页码、会话用量都能
        写在任意一个文件里。
        """
        values = build_placeholder_values(meta, page, pages)

        header_html = self._fill_static(self.header, values)
        footer_html = self._fill_static(self.footer, values)
        page_html = self._fill_static(self.page, values)

        # 只填充第一处 {{content}}，其余留空
        first = page_html.find(_CONTENT)
        if first >= 0:
            page_html = (
                page_html[:first] + content
                + page_html[first + len(_CONTENT):]
            )
            page_html = page_html.replace(_CONTENT, "")
        page_html = page_html.replace(_HEADER, header_html)
        page_html = page_html.replace(_FOOTER, footer_html)
        return page_html

    @staticmethod
    def _fill_static(text: str, values: dict[str, str]) -> str:
        def substitute(match: re.Match) -> str:
            key = match.group(1)
            if key in values:
                return values[key]
            return match.group(0)

        return _PLACEHOLDER.sub(substitute, text)

    def document(self, page_html: str, *, width: int, max_height: int,
                 font_family: str = "") -> str:
        """组装完整 HTML 文档（CSP + 尺寸约束）。"""
        font = font_family or (
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', "
            "'PingFang SC', 'Noto Sans CJK SC', 'Hiragino Sans GB', sans-serif"
        )
        base_css = (
            "html,body{margin:0;padding:0;background:transparent;}"
            f".page{{width:{int(width)}px;max-height:{int(max_height)}px;"
            "box-sizing:border-box;overflow:hidden;}"
            f":root{{--maa-content-max-height:{max(320, int(max_height) - 240)}px;}}"
            f"body{{font-family:{font};}}"
            "a{color:#5b6b7c;text-decoration:none;}"
            # 内联图片等比缩放并铺满正文宽度（两端对齐），
            # 高度超过可用正文高度时整体缩小，避免被分页裁切。
            ".attach-image{width:100%;}"
            ".attach-image img{display:block;width:100%;max-width:100%;"
            "height:auto;max-height:var(--maa-content-max-height);"
            "object-fit:contain;}"
        )
        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" "
            "content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">"
            f"<style>{self.style}\n{base_css}</style></head><body>"
            f"{page_html}</body></html>"
        )


def _read_cached(path: Path) -> str:
    """按 mtime 缓存模板文件，避免每份报告重复读盘。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"[渲染] 读取模板失败：{path}：{exc}")
        return ""
    _CACHE[path] = (mtime, text)
    return text


def _clean_template(text: str) -> str:
    """剥离脚本 / iframe 与外部 URL 引用，并把中文标签别名归一化。"""
    text = _SCRIPT.sub("", text or "")
    text = _IMPORT.sub("", text)
    text = _EXTERNAL.sub('data-blocked="1"', text)
    # 中文标签别名：<内容> / <页眉> / <页脚>（也接受 {{内容}} 写法）
    for alias, target in _ALIASES:
        text = text.replace(alias, target)
    return text


# 中文占位符别名 → 标准占位符（单趟替换，替换结果不再二次解析）
_ALIASES = (
    ("<内容>", _CONTENT), ("{{内容}}", _CONTENT),
    ("<页眉>", _HEADER), ("{{页眉}}", _HEADER),
    ("<页脚>", _FOOTER), ("{{页脚}}", _FOOTER),
)


def _load_single(root: Path, name: str, warnings: list[str]) -> Optional[TemplateSet]:
    directory = root / name
    page_path = directory / "page.html"
    if not page_path.is_file():
        warnings.append(f"模板 {name} 缺少 page.html")
        return None
    page = _clean_template(_read_cached(page_path))
    if _CONTENT not in page:
        warnings.append(f"模板 {name} 的 page.html 缺少 {{{{content}}}}")
        return None
    if page.count(_CONTENT) > 1:
        warnings.append(
            f"模板 {name} 的 page.html 含多个 {{{{content}}}}，只填充第一处"
        )
        logger.warning(f"[渲染] 模板 {name} 含多个 {{{{content}}}}，只填充第一处")
    header = _clean_template(_read_cached(directory / "header.html"))
    footer = _clean_template(_read_cached(directory / "footer.html"))
    # {{content}} 是正文插槽，只能出现在 page.html
    for label, text in (("header.html", header), ("footer.html", footer)):
        if _CONTENT in text:
            warnings.append(
                f"模板 {name} 的 {label} 含 {{{{content}}}}（正文只能放在 page.html），已忽略"
            )
            logger.warning(
                f"[渲染] 模板 {name} 的 {label} 含 {{{{content}}}}，已忽略"
            )
    header = header.replace(_CONTENT, "")
    footer = footer.replace(_CONTENT, "")
    _warn_unknown(page, name, "page.html")
    _warn_unknown(header, name, "header.html")
    _warn_unknown(footer, name, "footer.html")
    return TemplateSet(
        name=name,
        page=page,
        header=header,
        footer=footer,
        style=_clean_template(_read_cached(directory / "style.css")),
        warnings=warnings,
    )


def _warn_unknown(text: str, name: str, label: str = "") -> None:
    """未识别的占位符原样保留，仅记 debug 日志（不报错）。"""
    unknown = {
        key for key in _PLACEHOLDER.findall(text or "") if key not in _KNOWN
    }
    if unknown:
        where = f"{name}/{label}" if label else name
        logger.debug(
            f"[渲染] 模板 {where} 含未识别占位符（原样保留）："
            f"{', '.join(sorted(unknown))}"
        )


def load_template(root: str | Path, name: str) -> TemplateSet:
    """
    加载模板目录；非法名或缺失时回退 `default`。

    目录名只允许 `[A-Za-z0-9_-]`（拒绝 `..` 与路径分隔符）。
    """
    base = Path(root)
    warnings: list[str] = []
    requested = str(name or "default").strip() or "default"
    if not _TEMPLATE_NAME.match(requested):
        warnings.append(f"模板名非法：{requested}，已回退 default")
        logger.warning(f"[渲染] 模板名非法：{requested}")
        requested = "default"

    template = _load_single(base, requested, warnings)
    if template is not None:
        template.warnings = warnings
        return template

    logger.warning(f"[渲染] 模板 {requested} 不可用，回退 default")
    fallback = _load_single(base, "default", warnings)
    if fallback is None:
        raise FileNotFoundError(f"未找到可用模板：{base}")
    fallback.warnings = warnings
    return fallback
