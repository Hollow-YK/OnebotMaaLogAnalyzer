# 图片化报告与模板

报告默认渲染成图片发送：AI 输出 Markdown，Bot 用无头浏览器排版、分页并截图，
不再受单条消息 3500 字限制。浏览器缺失或渲染失败时自动回退为纯文本。

```
AI 输出 Markdown（含 section / page-break / 附件指令）
  → 预解析标记，按 section 切分内容块
  → mistune 转 HTML（escape + 标签白名单）
  → 附件指令转成 <figure> / 文件卡片
  → 模板逐页填充 {{content}}
  → 无头浏览器逐页测量、装箱、截图
  → PNG（单页过大时改拍 JPEG）
```

## 配置

`report` 位于 `bot.configs.<名称>.report`，与 `llm` / `vision` 同级：

```json
"report": {
  "render": "auto",
  "template": "default",
  "max_width_px": 1080,
  "max_height_px": 4096,
  "max_pages": 6,
  "device_scale_factor": 1,
  "font_family": "",
  "browser_path": "",
  "reuse_browser": true,
  "render_timeout_ms": 8000,
  "embed_attachments": true,
  "attach_markdown": false,
  "fallback_to_text": true,
  "render_followup": true
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `render` | `auto` | `image`（强制图片）/ `text`（纯文本）/ `auto`（浏览器可用则图片） |
| `template` | `default` | `templates/` 下的目录名；只允许 `[A-Za-z0-9_-]` |
| `max_width_px` | `1080` | 画布宽度（CSS 像素） |
| `max_height_px` | `4096` | 单页高度上限，超出按此高度分页 |
| `max_pages` | `6` | 最多几张；超出截断并在最后一页提示 |
| `device_scale_factor` | `1` | 输出像素倍率；`1` 时输出宽度 = `max_width_px` |
| `font_family` | `""` | CSS `font-family`；空 = 系统 CJK 字体栈 |
| `browser_path` | `""` | 浏览器可执行文件绝对路径；空 = 按平台探测 |
| `reuse_browser` | `true` | 常驻浏览器进程（冷启动一次，后续热渲染） |
| `render_timeout_ms` | `8000` | 单次渲染（导航 + 等字体 + 全部截图）超时 |
| `embed_attachments` | `true` | 图片是否内联进报告图；`false` 时单独发送图片消息 |
| `attach_markdown` | `false` | 是否在图片后额外上传一份 `.md` |
| `fallback_to_text` | `true` | 渲染异常 / 浏览器缺失时回退纯文本 |
| `render_followup` | `true` | 追问回答是否也渲染成图片 |

> `render=text` 时 `settings.reply_chunk_chars` 才生效（按段落切分纯文本）。

## 三种模式

| 模式 | 行为 |
| --- | --- |
| `image` | 始终渲染。浏览器不可用时仍按纯文本发送，并记录 warning |
| `text` | 保持旧的 3500 字符分段 + 图文混排（图片单独发送） |
| `auto` | 探测到浏览器就渲染，否则走 `text` |

> `text` 模式下图片按**原始字节**直接发送，协议端可能拒收超大图片；
> 想避免这种风险请使用 `image` / `auto`（渲染管线会按正文宽度缩放）。

浏览器缺失或渲染失败时：

1. 报告以纯文本发送（内部标记会被剥离，不会漏给用户）；
2. 向**通知群**发送一条提示，每个配置**每进程只发一次**；
3. 提示内容指路 `report.browser_path`。

渲染失败会自动重启浏览器并重试**一次**；仍失败才回退。

## 报告标记

标记必须先于 Markdown 解析，单独占一行：

```markdown
<!-- section: 结论 -->
模板匹配失败，界面停在活动入口。

<!-- section: 关键证据 -->
- `[ERR]` Recognition.Failed score=0.62 < 0.70

<!-- page-break -->
<!-- section: 处理建议 -->
1. 重新截图更新模板。
```

| 标记 | 作用 |
| --- | --- |
| `<!-- section: 标题 -->` | 开启一个内容块，标题为该行文本 |
| `<!-- page-break -->` | 强制在此处换页（不产生新块） |
| 中文等价 | `<!-- 小节: 标题 -->`、`<!-- 分页 -->` |

规则：

1. 标记本身**不会**出现在最终图片里（纯文本模式下也会被剥离）。
2. 空标题的 `<!-- section: -->` 视为无效标记，内容并入上一块。
3. 只有标题、没有任何正文的内容块会被忽略，避免图片里出现空小节。
4. 第一个标记之前若已有内容，自动归入标题为「概览」的块（兜底，提示词要求
   AI 从第一个 `section` 开始，因此正常不会触发）。
5. `<!-- page-break -->` 一定换页；与自动装箱混用时以强制分页优先。

## 支持的 Markdown 子集

| 元素 | 支持 | 说明 |
| --- | --- | --- |
| 标题 `#` `##` `###` | ✅ | 块内标题，与 section 标题层级区分 |
| 段落 / 粗体 / 斜体 / 删除线 | ✅ | |
| 行内代码 / 代码块 | ✅ | 等宽 + 底色 + 自动换行；**不做语法高亮** |
| 无序 / 有序 / 嵌套列表 | ✅ | |
| 引用 `>` / 分隔线 `---` | ✅ | |
| 表格 | ✅ | `table-layout:auto`，单元格自动换行 |
| 链接 | ✅ | 渲染为文字 + 链接色，不可点击跳转（页面禁止外联） |
| `[附件@日志: ...]` / `[附件@项目: ...]` | ✅ | 图片内联 / 其他文件渲染为卡片 |
| `[看图@日志: ...]` / `[看图@项目: ...]` | — | 只给模型看，渲染前已被剥离 |
| emoji | ✅ | 交给浏览器字体回退链 |

## 附件的呈现

- **图片**：内联为 `<figure class="attach-image">`，按**正文宽度等比缩放**
  （左右两端对齐）；高度超过可用正文高度时整体缩小，不会被分页裁切。
  图片**不再有体积上限**（旧的 `max_image_mb` 已删除）——渲染时会缩放，
  体积只影响生成报告图的大小。
- **其他文件**：渲染为 `<div class="attach-file">` 卡片（文件名 + 来源 + 大小），
  同时照旧通过 `upload_group_file` 上传，保证可下载。
- 同一路径在一次报告里只内联一次；已内联的图片不会再单独发一条图片消息。
- `embed_attachments=false` 时图片不内联，改为报告后单独发送图片消息。
- `attach_markdown=true` 时额外上传 `<项目>-<时间>-report.md`，
  内容为剥离 `section` / `page-break` / 看图标记后的原始 Markdown。

## 分页

```
① 先渲染「空正文」页面，测出模板自身的页眉/页脚/内边距占用 → 每页可用正文高度
② 渲染全部内容块，测量每块高度
③ 先按 page-break 切段，段内按可用高度装箱
④ 逐页填充模板，{{page}} / {{pages}} 每页不同
⑤ 逐页 DOM.getBoxModel 取包围盒 → Page.captureScreenshot(clip=包围盒)
```

- `.block` / `table` / `pre` / `blockquote` / `figure` 设置了 `break-inside: avoid`，
  不会被从中间切开。
- 单块高于整页时单独成页并允许内部溢出，同时记 warning。
- 一段以强制分页结尾时，本页到此为止，剩余空间留白。
- 页数超过 `max_pages` 时截断，并在最后一页插入「报告过长，已按 max_pages 截断」。

> 不能用「一个超长文档 + 按 y 坐标切片」：`position: fixed` 的页脚只绘制一次，
> 页码也无法按页变化。因此必须逐页构建 DOM。

## 浏览器

只支持 **Chromium 系**（Edge / Chrome / Chromium）；Firefox 不支持 CDP。

启动参数（固定）：

```
--headless=new --disable-gpu --hide-scrollbars --no-first-run
--no-default-browser-check --disable-extensions --disable-sync
--disable-background-networking --disable-component-update --disable-dev-shm-usage
--user-data-dir=<data/_browser_profile>   # 独立配置目录，不碰用户配置
--remote-debugging-port=0                 # 端口从 DevToolsActivePort 读取
--host-resolver-rules="MAP * 0.0.0.0"     # 阻断一切外联
```

探测顺序（`browser_path` 为空时）：

| 平台 | 顺序 |
| --- | --- |
| Windows | `%ProgramFiles%\Microsoft\Edge\Application\msedge.exe` → `ProgramFiles(x86)` → `%ProgramFiles%\Google\Chrome\Application\chrome.exe` → `ProgramFiles(x86)` → `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe` → `where msedge / chrome / chromium` |
| Linux | PATH 中 `microsoft-edge` → `microsoft-edge-stable` → `google-chrome` → `google-chrome-stable` → `chromium` → `chromium-browser`；另查 `/usr/bin`、`/usr/local/bin`、`/snap/bin` |
| macOS | `/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge` → Chrome → Chromium |
| 其他 | 仅 `browser_path` |

- `browser_path` 指向存在的文件时**直接使用**，不做探测；无效则记 warning 并回退探测。
- 配置目录被占用时自动改用 `_browser_profile_<pid>_<时间戳>`，启动前清理历史遗留目录。
- 渲染串行化；`reuse_browser=false` 时每次渲染后关闭进程。
- Bot 停止 / 重载时调用 `close_all()` 关闭全部常驻浏览器。
- 实测延迟：冷启动约 0.5–1 s，热态每页约 100 ms。

## 模板

模板固定放在工程根目录 `templates/`，每个子目录是一套完整模板：

```
templates/
  default/            # 内置（随发布包分发）
    page.html         # 必需
    header.html       # 可选
    footer.html       # 可选
    style.css         # 可选
  dark/               # 你自己放的深色模板
    page.html
    style.css
```

```json
"report": { "template": "dark" }
```

- `template` 为空 → `default`；指向不存在的目录 → 记 warning 并回退 `default`。
- 目录名只允许 `[A-Za-z0-9_-]`；`..`、路径分隔符、绝对路径一律拒绝。
- **没有主题配置、没有 `style.dark.css`**：深浅色由模板自己决定，需要深色就整套复制一份。

### page.html

必须包含 `{{content}}`，否则模板无效并回退 `default`：

```html
<div class="page">
  {{header}}
  <main class="content">{{content}}</main>
  <footer class="page-footer">{{footer}}</footer>
</div>
```

### header.html / footer.html

两者都是**每页重复出现**的固定框架，可用的占位符与 `page.html` **完全一致**
（详见下一节）。默认模板的分工只是排版选择，不是限制：

```html
<!-- templates/default/header.html -->
<div class="page-header">
  <h1 class="report-title">{{title}}</h1>
  <div class="report-meta">{{project}} · {{file}} · {{generated_at}}</div>
</div>
```

```html
<!-- templates/default/footer.html -->
<div class="footer-line">
  <span class="footer-note">Powered By OnebotMaaLogAnalyzer</span>
  <span class="pager">{{page}} / {{pages}}</span>
</div>
<div class="footer-usage">{{usage}}</div>
```

想在页首也显示用量，把占位符直接写进 `header.html` 即可，例如：

```html
<div class="page-header">
  <h1 class="report-title">{{title}}</h1>
  <div class="report-meta">{{project}} · {{file}} · {{generated_at}}</div>
  <div class="report-meta">{{usage}}（{{usage_calls}} 次调用）</div>
</div>
```

`{{usage}}` 为空时 `.footer-usage:empty { display: none }` 会让整行不占位，
因此没有调用记录时页脚不会多出空白（`header.html` 里也可以套用同样的 CSS）。

### 占位符

替换是**单趟**的：插入的内容（包括正文 HTML）不会被再次解析。

**`page.html` / `header.html` / `footer.html` 使用同一份取值**，下表除标注
「仅 page.html」的项外，三者都可以用、写法完全一样：

| 占位符 | 含义 | 转义 | 可用于 |
| --- | --- | --- | --- |
| `{{content}}` | **AI 内容块**，按顺序拼接（每块自带 `<h2>` 标题） | 否 | 仅 `page.html` |
| `{{header}}` / `{{footer}}` | 该页的页眉 / 页脚内容（来自 `header.html` / `footer.html`） | 否 | 仅 `page.html` |
| `{{page}}` / `{{pages}}` | 当前页 / 总页数 | 是 | 三个文件 |
| `{{title}}` / `{{project}}` / `{{file}}` / `{{generated_at}}` | 报告元信息 | 是 | 三个文件 |
| `{{tokens_input}}` | 本次会话输入 token（如 `12.3k`） | 是 | 三个文件 |
| `{{tokens_output}}` | 本次会话输出 token | 是 | 三个文件 |
| `{{tokens_total}}` | 本次会话合计 token | 是 | 三个文件 |
| `{{usage_calls}}` | 本次会话的模型调用次数 | 是 | 三个文件 |
| `{{provider}}` | 本次会话用到的端点（多个用「、」分隔） | 是 | 三个文件 |
| `{{model}}` | 本次会话用到的模型（多个用「、」分隔） | 是 | 三个文件 |
| `{{usage}}` | 一行摘要：输入 / 输出 / 合计 + `provider/model ×次数` | 是 | 三个文件 |

每个占位符在每页可以出现任意多次。

> 同一份日志的分析与它后续的全部追问计为**同一个会话**，因此 token 数
> 包含追问消耗；追问回答渲染成图片时展示的是该会话当时的累计值。
> 页眉 / 页脚高度会随用量行是否出现而变化，渲染时先测模板骨架，不会挤掉正文。

中文别名：`<内容>` / `{{内容}}` → `{{content}}`，`<页眉>` → `{{header}}`，`<页脚>` → `{{footer}}`。

规则：

1. 多个 `{{content}}` → 只填充第一处，其余留空并记 warning。
2. `{{content}}` 写在 `header.html` / `footer.html` 中无效：会被忽略并记 warning
   （正文只能出现在 `page.html`）。
3. 未识别的占位符**原样保留**并记 debug 日志（不报错，便于逐步调试）。
4. 模板按 `mtime` 缓存，避免每份报告重复读盘。
5. 模板内的 `<script>` / `<iframe>` 与外部 URL 引用会被剥离。
6. 模板只描述「单页长什么样」；分页发生在正文内部，**模板里不要写分页逻辑**。
7. 模板作者可以使用任意 CSS（渲染环境是真实浏览器）。
8. 内置 `default` 只用块级流式布局，不依赖 flex / grid / JS 框架。

### 可用的结构类名

插件生成的 HTML 带固定类名，模板可以完全覆盖其外观：

| 类名 | 元素 |
| --- | --- |
| `.page` | 单页容器（由模板定义宽度/内边距） |
| `.page-header` | 页眉容器（默认模板的 `header.html`） |
| `.report-title` / `.report-meta` | 标题 / 元信息 |
| `.content` | 正文插槽（**可选**：没有该元素时按 `{{content}}` 所在元素的子 `.block` 测量） |
| `.block` | 一个内容块（`<section>`） |
| `.block-title` | 内容块标题（`<h2>`） |
| `.block-lead` | 「概览」块的额外类（模板可单独定制） |
| `.attach-image` / `.attach-image img` / `figcaption` | 内联图片 |
| `.attach-file` / `.attach-name` / `.attach-meta` | 文件卡片 |
| `.page-footer` | 页脚容器（默认模板的 `footer.html` 放这里） |
| `.footer-line` / `.footer-note` / `.pager` | 页脚第一行：左署名、右页码 |
| `.footer-usage` | 页脚第二行：会话 token 与模型用量（为空时整行隐藏） |

CSS 变量 `--maa-content-max-height` 由渲染器按 `max_height_px` 计算后注入，
用于限制超高图片；模板可直接使用：

```css
.attach-image img {
  width: 100%;
  height: auto;
  max-height: var(--maa-content-max-height, 3200px);
  object-fit: contain;
}
```

### 自定义模板示例

`templates/dark/page.html`：

```html
<div class="page">
  <header class="page-header">{{header}}</header>
  <main class="content">{{content}}</main>
  <footer class="page-footer">{{footer}}</footer>
</div>
```

`templates/dark/style.css`：

```css
.page { background: #14181d; color: #d7dee7; padding: 28px 32px; }
.block-title { border-left: 4px solid #58a6ff; color: #e6edf3; }
.block code, .block pre { background: #1c2229; border-color: #2b323b; }
.attach-file { background: #1c2229; border-color: #2b323b; }
```

然后把配置改成 `"template": "dark"` 即可，无需重启（模板按 mtime 重新加载）。

## 安全约束

AI 输出与模板都属于不可信输入：

1. mistune 使用 `escape=True`，原始 HTML 被转义，不透传任何脚本。
2. 白名单只作用于 AI 文本产出的标签：`h1-h3` / `p` / `ul` / `ol` / `li` /
   `strong` / `em` / `del` / `code` / `pre` / `blockquote` / `hr` / `table` /
   `thead` / `tbody` / `tr` / `th` / `td` / `a` / `img` / `br` / `span`。
   `section` / `figure` / `div.attach-file` 等结构标签由插件自身生成。
3. `img` 只接受 `data:image/...`；`a` 只保留 `http(s)` 链接；其余属性丢弃。
4. 页面 CSP：`default-src 'none'; img-src data:; style-src 'unsafe-inline'`。
5. 浏览器加 `--host-resolver-rules="MAP * 0.0.0.0"`，即使出现远程 `img` 也无法外联。
6. 模板中的 `<script>` / `<iframe>` / 外链一律剥离。

## 排错

| 现象 | 原因与处理 |
| --- | --- |
| 报告变成纯文本 | 浏览器未找到 / 渲染失败。查看日志中的 `[渲染]` 行与通知群提示；装 Edge/Chrome/Chromium 或设 `browser_path` |
| 日志出现「模板 X 缺少 page.html」 | 模板目录缺文件，已回退 `default` |
| 图片里出现 `{{content}}` 字样 | `page.html` 用了不支持的写法（例如全角括号），改用 `{{content}}` 或 `<内容>` |
| 中文显示为方块 | 系统缺 CJK 字体；在 `report.font_family` 指定已安装字体 |
| 报告被截断 | 页数超过 `max_pages`，调大该值或让 AI 用 `<!-- page-break -->` 主动分页 |
| 页面空白 / 高度 0 | 渲染超时或模板 CSS 把 `.page` 撑成 0 宽高；检查模板 |
| 图片被裁切 | 模板覆盖了 `--maa-content-max-height` 或未使用 `object-fit: contain` |
| 每页都显示同一个页码 | 模板里硬编码了页码，改用 `{{page}}` / `{{pages}}` |
