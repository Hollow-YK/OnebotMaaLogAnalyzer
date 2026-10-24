"""
分页装箱 — 强制分页优先，段内再按可用高度装箱。

不按 y 坐标切图：`position: fixed` 的页脚在 `captureBeyondViewport` 下
只绘制一次，页码也无法按页变化。因此必须逐页构建 DOM。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("Maa.Render.Paginate")


@dataclass
class BlockMetrics:
    """单个内容块的尺寸与分页意图。"""

    index: int
    height: float
    force_break_after: bool = False


def pack_blocks(blocks: list[BlockMetrics],
                available_height: float) -> tuple[list[list[int]], list[str]]:
    """
    把内容块装箱为每页的块索引列表。

    - `force_break_after` 一定在块后换页
    - 单块高于整页时单独成页并允许内部溢出（记录警告）
    """
    warnings: list[str] = []
    pages: list[list[int]] = []
    current: list[int] = []
    used = 0.0
    available = max(120.0, float(available_height or 0))

    for block in blocks:
        height = max(0.0, float(block.height or 0))
        if current and used + height > available:
            pages.append(current)
            current = []
            used = 0.0
        if height > available and not current:
            warnings.append(
                f"内容块 #{block.index} 高于单页可用高度（{int(height)}px），已单独成页"
            )
        current.append(block.index)
        used += height
        if block.force_break_after:
            pages.append(current)
            current = []
            used = 0.0

    if current:
        pages.append(current)
    if not pages and blocks:
        pages = [[block.index for block in blocks]]
    return pages, warnings


def truncate_pages(pages: list[list[int]], max_pages: int) -> tuple[list[list[int]], bool]:
    """按 max_pages 截断，返回 (页列表, 是否截断)。"""
    limit = max(1, int(max_pages or 1))
    if len(pages) <= limit:
        return pages, False
    return pages[:limit], True


# ── 页面内测量脚本 ──

MEASURE_CHROME_JS = """
(() => {
  const page = document.querySelector('.page');
  if (!page) { return {error: 'missing-page'}; }
  const content = document.querySelector('.content')
    || (document.querySelector('.block') ? document.querySelector('.block').parentElement : null);
  const cs = getComputedStyle(page);
  const padTop = parseFloat(cs.paddingTop) || 0;
  const padBottom = parseFloat(cs.paddingBottom) || 0;
  const header = document.querySelector('.page-header, header');
  const footer = document.querySelector('.page-footer, footer');
  const headerStyle = header ? getComputedStyle(header) : null;
  const footerStyle = footer ? getComputedStyle(footer) : null;
  const headerH = header ? header.getBoundingClientRect().height
      + (parseFloat(headerStyle.marginTop) || 0)
      + (parseFloat(headerStyle.marginBottom) || 0) : 0;
  const footerH = footer ? footer.getBoundingClientRect().height
      + (parseFloat(footerStyle.marginTop) || 0)
      + (parseFloat(footerStyle.marginBottom) || 0) : 0;
  let contentPad = 0;
  let contentMargin = 0;
  if (content) {
    const ccs = getComputedStyle(content);
    contentPad = (parseFloat(ccs.paddingTop) || 0)
      + (parseFloat(ccs.paddingBottom) || 0);
    contentMargin = (parseFloat(ccs.marginTop) || 0)
      + (parseFloat(ccs.marginBottom) || 0);
  }
  const maxHeight = window.__MAA_MAX_PAGE_HEIGHT__ || 4096;
  const chrome = padTop + padBottom + headerH + footerH + contentPad + contentMargin;
  const available = Math.max(120, maxHeight - chrome);
  return {available: available, chrome: chrome};
})()
"""

MEASURE_BLOCKS_JS = """
(() => {
  const content = document.querySelector('.content')
    || (document.querySelector('.block') ? document.querySelector('.block').parentElement : null);
  const blocks = content
    ? Array.from(content.querySelectorAll(':scope > .block'))
    : Array.from(document.querySelectorAll('.block'));
  const heights = blocks.map(el => {
    const cs = getComputedStyle(el);
    return el.getBoundingClientRect().height
      + (parseFloat(cs.marginTop) || 0)
      + (parseFloat(cs.marginBottom) || 0);
  });
  return {heights: heights};
})()
"""

MEASURE_PAGE_BOX_JS = """
(() => {
  const page = document.querySelector('.page');
  if (!page) { return {error: 'missing-page'}; }
  const rect = page.getBoundingClientRect();
  return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
})()
"""

MEASURE_DOCUMENT_HEIGHT_JS = """
(() => {
  const page = document.querySelector('.page');
  const height = page ? page.getBoundingClientRect().height : 0;
  return {height: height};
})()
"""
