# 结果附件与看图

附件**不是必须行为**。当 AI 认为某些内容有助于理解问题时，会在报告**任意位置**声明：

```text
[附件@日志: on_error/on_error_20260101_120002.png]
[附件@项目: assets/resource/image/Start2.png]
[附件@日志: config/mxu-MaaXXX.json]
[附件@日志: device_info.txt]
[附件@项目: assets/resource/pipeline/PVP.json]
```

图片与文件**统一使用「附件」指令**，Bot 按扩展名自动区分：

- 图片（png/jpg/webp/bmp）→ 内联进图片化报告；`report.embed_attachments=false`
  时改为单独发送图片消息
- 其他文件 → 报告里渲染为卡片，并照旧上传为**群文件**（保证可下载）

旧的 `[附图@...]` 已取消：只作为普通文本被剥离，不会再发送图片。
`inline_attachments` 设置保留用于兼容旧配置，但图片化报告下不再需要。

## 看图（只给模型，不发送）

如果模型只是**想看看某张图**来判断问题，可以声明：

```text
[看图@日志: on_error/on_error_20260101_120002.png]
[看图@项目: assets/resource/image/Start2.png]
```

- 图片会加入模型上下文，用于分析；
- **不会**发送给用户，也不会出现在图片化报告里；
- 受 `bot.configs.<名称>.vision` 控制（数量 / 体积 / 是否允许主动请求）；
- 只有 `supports.vision=true` 的候选模型才会收到图片。

不同 UI 的日志包布局不同：MXU 的错误截图通常在根目录 `on_error/`，
MFAAvalonia 在 `debug/on_error/`，MaaFwApp 可能没有错误截图。路径必须与摘要/工具返回的实际路径一致。

## 必须指定来源

| 标记 | 含义 |
| --- | --- |
| `@日志` | 本次上传的**日志压缩包**内 |
| `@项目` | **项目代码仓库**内 |

来源是**必需**的，Bot 只从指定来源查找，**不会跨来源回退**。这样可避免模型写了不存在的路径、却意外命中另一个来源的同名文件而发出非预期内容。

兼容写法：`[附件: 日志:on_error/a.png]`。

## 必须是相对路径

路径一律为**来源内的相对路径**，Bot 会做沙箱校验：

| 输入 | 结果 |
| --- | --- |
| `on_error/a.png` | ✅ 接受 |
| `./config/x.json` | ✅ 接受（去掉 `./` 前缀） |
| `assets\resource\pipeline\PVP.json` | ✅ 接受（分隔符统一） |
| `C:/Windows/system32/config.json` | ❌ 拒绝（绝对路径） |
| `/etc/passwd` | ❌ 拒绝（绝对路径） |
| `../../secret.txt` | ❌ 拒绝（上跳越界） |
| `.env` / `sub/.env` | ❌ 拒绝（凭据文件） |
| `sub/.git/config` | ❌ 拒绝（版本控制内部） |
| `server.pem` / `id_rsa` | ❌ 拒绝（私钥） |

缺少来源标记或路径非法的指令会被直接丢弃（只记日志，不发送任何内容）。

## 敏感文件保护

附件功能会把文件发到群聊，因此**凭据与密钥类文件一律拒绝外发**（对日志包与项目仓库同样生效）：

| 类别 | 示例 |
| --- | --- |
| 版本控制内部 | `.git/`、`.hg/`、`.svn/`（remote URL 常含 token） |
| 云凭据目录 | `.ssh/`、`.aws/`、`.gnupg/`、`.docker/`、`.kube/` |
| 环境变量文件 | `.env`、`.env.local`、`.env.production` |
| 凭据清单 | `credentials`、`.netrc`、`.npmrc`、`.pgpass`、`.git-credentials` |
| 私钥与证书 | `.pem`、`.key`、`.pfx`、`.p12`、`id_rsa*`、`id_ed25519*` |

> 被拒绝的附件只记日志，不会发送到群里。`agent` 模式的 `read_file` 工具同样受此限制。

## 图片化报告中的呈现

默认 `report.render=auto`：浏览器可用时，报告正文、图片与非图片文件卡片会一起
渲染成一张或多张图片发送，不再受单条消息 3500 字限制。

- 图片指令行**不会展示给用户**，渲染前已被解析为 `<figure>`
- 同一路径在一次报告里只内联一次；已内联的图片不会再单独发一条图片消息
- 非图片附件：图片里只是卡片提示，实际文件仍会 `upload_group_file`
- 未声明时不附带任何内容；声明了但找不到时会在报告末尾附一行说明
- `render=text` 时退化为旧的图文混排：图片与相邻文字合并为同一条消息，文件单独上传

## 相关设置

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `send_images` | `true` | 是否允许模型附图（总开关，同时受 `max_report_images` 限制） |
| `max_report_images` | `4` | 最多附带几张图 |
| `send_repo_images` | `true` | 是否允许从项目仓库取图（仅本地仓库可用） |
| `send_files` | `true` | 是否允许附带任意文件（以群文件上传） |
| `max_report_files` | `2` | 最多附带几个文件 |
| `max_file_attachment_mb` | `20` | 单个**非图片**附件大小上限 MB |
| `inline_attachments` | `true` | 旧配置兼容项；图片化报告下由 `report.embed_attachments` 控制 |
| `report.embed_attachments` | `true` | 图片是否内联进报告图（`false` = 单独发送图片消息） |
| `report.attach_markdown` | `false` | 是否在图片后额外上传一份 `.md` |

> 图片**没有单张体积上限**（旧的 `max_image_mb` 已删除）：渲染时按正文宽度等比缩放，
> 大图不会导致失败或跳过，详见 [图片化报告与模板](render.md)。
