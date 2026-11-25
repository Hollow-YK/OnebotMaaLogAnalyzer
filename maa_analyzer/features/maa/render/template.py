"""
HTML 模板加载与填充。

目录结构（工程根目录 `templates/`）：

    templates/
      default/
        page.html       # 必需，必须包含 {{content}}
        attach-img.html # 必需，一张内联图片的外壳
        attach-file.html  # 必需，一个文件卡片的外壳
        information.html  # 可选，报告头（概览）外壳
        header.html     # 可选
        footer.html     # 可选
        style.css       # 可选

模板只描述「单页长什么样」；分页发生在正文内部，模板里不要写分页逻辑。
附件的 HTML 外壳也由模板提供（`attach-img.html` / `attach-file.html`），
Python 只负责提供 `{{attach.*}}` 取值。

占位符命名空间：

    {{header}} / {{information}} / {{content}} / {{footer}}   结构槽，保持裸名
    {{meta.*}}          报告页元信息、用量、页码、工具版本
    {{information.*}}   日志包 / 运行环境 / 报告事实
    {{attach.*}}        附件，仅两个附件外壳可用

`{{information}}` 是整块报告头 HTML（仅第 1 页），`{{information.file}}`
是单个字段文本；两者靠「有没有点号」区分。
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
_INFORMATION = "{{information}}"
# 占位符：裸名是结构槽，`namespace.field` 是数据字段。
_PLACEHOLDER = re.compile(
    r"\{\{\s*([A-Za-z_]+(?:\.[A-Za-z_]+)?)\s*\}\}"
)
_SCRIPT = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|<iframe\b[^>]*>.*?</iframe\s*>",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//[^"']*["']""",
    re.IGNORECASE,
)
_IMPORT = re.compile(r"@import\b[^;]*;", re.IGNORECASE)

# 页眉 / 页脚 / page.html 共用（次序即文档展示顺序）。
# 只放「报告页自身」的元信息；项目/文件名等日志事实在 information.*。
_META_DEFAULTS: dict[str, str] = {
    "title": "", "project": "", "generated_at": "",
    "tokens_input": "0", "tokens_output": "0", "tokens_total": "0",
    "usage_calls": "0", "provider": "-", "model": "-", "usage": "",
    "usage_style": "", "cache_hit_rate": "暂无缓存数据", "usage_summary": "",
    "tool_version": "", "template": "",
}

# 报告头外壳（information.html）的字段。它们也能在 page/header/footer
# 里通过 {{information.<field>}} 使用。
_INFORMATION_KNOWN = {
    # 项目名的三个来源 / 合并结果
    "project", "project_configured", "project_display", "project_source",
    "ui", "ui_version", "module_version", "resource_version",
    "framework_version", "device", "os", "screen",
    "file", "size", "log_count", "image_count",
    "file_count", "uncompressed_size", "log_total", "error_image_total",
    "custom_image_total", "config_total", "repo_ref", "part_note",
}

# 附件外壳（attach-img.html / attach-file.html）的字段。
_ATTACH_KNOWN = {
    "name", "path", "source", "size", "status", "kind", "class", "src",
}

# 每个模板文件允许的命名空间与结构槽。
# 结构槽保持裸名；数据字段必须带命名空间。
_SCOPES: dict[str, dict[str, set[str]]] = {
    "page": {
        "slots": {"content", "header", "information", "footer"},
        "namespaces": {"meta", "information"},
    },
    "header": {
        "slots": {"information"},
        "namespaces": {"meta", "information"},
    },
    "footer": {
        "slots": {"information"},
        "namespaces": {"meta", "information"},
    },
    "information": {
        "slots": set(),
        "namespaces": {"information"},
    },
    "attach_img": {
        "slots": set(),
        "namespaces": {"attach"},
    },
    "attach_file": {
        "slots": set(),
        "namespaces": {"attach"},
    },
}

PAGE_FILE = "page.html"
ATTACH_IMG_FILE = "attach-img.html"
ATTACH_FILE_FILE = "attach-file.html"
INFORMATION_FILE = "information.html"


def _fill_values(text: str, values: dict[str, str],
                 raw_keys: tuple[str, ...] = ()) -> str:
    """单趟替换：`raw_keys` 里的键原样插入（HTML），其余按文本转义。"""
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        raw = values.get(key)
        if raw is None:
            return match.group(0)
        if key in raw_keys:
            return str(raw)
        return html.escape(str(raw))

    return _PLACEHOLDER.sub(substitute, text)


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def build_meta_values(meta: Optional[dict], page: int, pages: int
                      ) -> dict[str, str]:
    """构造 `meta.*` 页面元信息（已转义）。"""
    meta = meta or {}
    values: dict[str, str] = {}
    for key, default in _META_DEFAULTS.items():
        raw = meta.get(key)
        values[f"meta.{key}"] = _escape(default if raw is None else raw)
    values["meta.page"] = _escape(page)
    values["meta.pages"] = _escape(pages)
    return values


def build_information_values(values: Optional[dict], *, escape: bool = True
                             ) -> dict[str, str]:
    """构造 `information.*` 字段值。

    `escape=False` 给 `information.html` 用（随后交给 `_fill_values` 转义）；
    `escape=True` 给 page/header/footer 用（值直接进入 `_fill_static`）。
    """
    values = values or {}
    result: dict[str, str] = {}
    for field_name in _INFORMATION_KNOWN:
        raw = values.get(field_name)
        text = "" if raw is None else str(raw)
        result[f"information.{field_name}"] = _escape(text) if escape else text
    return result


def _known_for_scope(kind: str, key: str) -> bool:
    """占位符是否属于该模板文件允许的结构槽 / 命名空间。"""
    scope = _SCOPES.get(kind)
    if scope is None:
        return False
    if key in scope["slots"]:
        return True
    if "." not in key:
        return False
    namespace, field_name = key.split(".", 1)
    if namespace not in scope["namespaces"]:
        return False
    if namespace == "meta":
        return field_name in _META_DEFAULTS or field_name in {"page", "pages"}
    if namespace == "information":
        return field_name in _INFORMATION_KNOWN
    if namespace == "attach":
        if field_name == "src":
            return kind == "attach_img"
        return field_name in _ATTACH_KNOWN
    return False


def _uses_information_fields(text: str) -> bool:
    return any(
        token.startswith("information.")
        for token in _PLACEHOLDER.findall(text or "")
    )


_CACHE: dict[Path, tuple[float, str]] = {}


@dataclass
class TemplateSet:
    """一套已加载的模板。"""

    name: str
    page: str
    header: str = ""
    footer: str = ""
    style: str = ""
    # 附件外壳：图片 / 文件（含未找到、未内联两种状态）
    attach_img: str = ""
    attach_file: str = ""
    # 报告头（概览）外壳：缺失时由渲染器使用内置默认外壳
    information: str = ""
    # 主题是否用裸 {{information}} 自己指定了报告头位置
    information_slot: bool = False
    warnings: list[str] = field(default_factory=list)

    def render_attach(self, values: dict[str, str], *, image: bool = False) -> str:
        """
        用一条附件的取值填充外壳（单趟替换）。

        `{{attach.src}}` 是图片 data URI，原样插入；其余占位符按文本转义。
        `attach.src` 只对 `attach-img.html` 提供。
        """
        text = self.attach_img if image else self.attach_file
        prefixed: dict[str, str] = {}
        for key in _ATTACH_KNOWN:
            if key == "src" and not image:
                continue
            prefixed[f"attach.{key}"] = str(values.get(key) or "")
        return _fill_values(text, prefixed, ("attach.src",))

    def render_information(self, values: dict[str, str]) -> str:
        """填充报告头外壳（所有字段都按文本转义）。"""
        return _fill_values(
            self.information, build_information_values(values, escape=False)
        )

    def render_page(self, *, content: str, meta: dict, page: int,
                    pages: int, information: str = "",
                    information_values: Optional[dict] = None) -> str:
        """
        填充单页 HTML（单趟替换，插入内容不会被二次解析）。

        `page.html` / `header.html` / `footer.html` 共用 `meta.*` 与
        `information.*`；结构槽 `{{header}}` / `{{footer}}` 只在 page.html
        有值，`{{information}}` 是整块报告头 HTML（仅第 1 页）。
        """
        values = build_meta_values(meta, page, pages)
        values.update(build_information_values(information_values))
        # 报告头是插件生成的 HTML，原样插入；只在首屏出现一次。
        values["information"] = (information or "") if page == 1 else ""

        header_html = self._fill_static(self.header, values)
        footer_html = self._fill_static(self.footer, values)
        values["header"] = header_html
        values["footer"] = footer_html
        page_html = self._fill_static(self.page, values)

        # 只填充第一处 {{content}}，其余留空；正文最后插入，避免二次替换。
        first = page_html.find(_CONTENT)
        if first >= 0:
            page_html = (
                page_html[:first] + content
                + page_html[first + len(_CONTENT):]
            )
            page_html = page_html.replace(_CONTENT, "")
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
            # 模板用 {{attach.class}} 的默认类名即可直接生效；
            # 自定义类名由模板自己控制。
            ".attach-image{width:100%;box-sizing:border-box;}"
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
    page_path = directory / PAGE_FILE
    missing = [
        filename
        for filename in (PAGE_FILE, ATTACH_IMG_FILE, ATTACH_FILE_FILE)
        if not (directory / filename).is_file()
    ]
    if missing:
        warnings.append(f"模板 {name} 缺少 {' / '.join(missing)}")
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
    attach_img = _clean_template(_read_cached(directory / ATTACH_IMG_FILE))
    attach_file = _clean_template(_read_cached(directory / ATTACH_FILE_FILE))
    # 报告头外壳可选：缺失时渲染器用内置默认外壳，旧主题不受影响
    info_path = directory / INFORMATION_FILE
    information = (
        _clean_template(_read_cached(info_path)) if info_path.is_file() else ""
    )
    # 结构槽 {{content}} 只能出现在 page.html
    for label, text in (
        ("header.html", header),
        ("footer.html", footer),
        (ATTACH_IMG_FILE, attach_img),
        (ATTACH_FILE_FILE, attach_file),
        (INFORMATION_FILE, information),
    ):
        if _CONTENT in text:
            warnings.append(
                f"模板 {name} 的 {label} 含 {{{{content}}}}（正文只能放在 page.html），已忽略"
            )
            logger.warning(
                f"[渲染] 模板 {name} 的 {label} 含 {{{{content}}}}，已忽略"
            )
    header = header.replace(_CONTENT, "")
    footer = footer.replace(_CONTENT, "")
    attach_img = attach_img.replace(_CONTENT, "")
    attach_file = attach_file.replace(_CONTENT, "")
    information = information.replace(_CONTENT, "")
    if "attach.src" not in _PLACEHOLDER.findall(attach_img):
        logger.warning(
            f"[渲染] 模板 {name} 的 {ATTACH_IMG_FILE} 没有 {{{{attach.src}}}}，"
            "图片不会显示"
        )
    # 主题自定义报告头区域：任一页框架文件里出现裸 {{information}} 即视为启用，
    # 此时报告头改为由该插槽渲染，不再插入「概览」内容块（避免重复）。
    information_slot = any(
        "information" in _PLACEHOLDER.findall(text)
        for text in (page, header, footer)
    )
    information_fields = any(
        _uses_information_fields(text) for text in (page, header, footer)
    )
    if information_slot:
        logger.debug(
            f"[渲染] 模板 {name} 使用 {{{{information}}}} 插槽渲染报告头"
        )
    elif information_fields:
        logger.debug(
            f"[渲染] 模板 {name} 使用 {{{{information.*}}}} 字段自行排版报告头，"
            "不注入整块报告头"
        )
    else:
        logger.warning(
            f"[渲染] 模板 {name} 没有 {{{{information}}}} 插槽，报告头不会显示"
        )
    _warn_unknown(page, name, "page.html", kind="page")
    _warn_unknown(header, name, "header.html", kind="header")
    _warn_unknown(footer, name, "footer.html", kind="footer")
    _warn_unknown(attach_img, name, ATTACH_IMG_FILE, kind="attach_img")
    _warn_unknown(attach_file, name, ATTACH_FILE_FILE, kind="attach_file")
    if information:
        _warn_unknown(information, name, INFORMATION_FILE, kind="information")
    return TemplateSet(
        name=name,
        page=page,
        header=header,
        footer=footer,
        style=_clean_template(_read_cached(directory / "style.css")),
        attach_img=attach_img,
        attach_file=attach_file,
        information=information,
        information_slot=information_slot,
        warnings=warnings,
    )


def _warn_unknown(text: str, name: str, label: str = "", kind: str = "page") -> None:
    """未识别或超出作用域的占位符原样保留，仅记 debug 日志。"""
    unknown = {
        key for key in _PLACEHOLDER.findall(text or "")
        if not _known_for_scope(kind, key)
    }
    if unknown:
        where = f"{name}/{label}" if label else name
        logger.debug(
            f"[渲染] 模板 {where} 含未识别或超出作用域的占位符（原样保留）："
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
