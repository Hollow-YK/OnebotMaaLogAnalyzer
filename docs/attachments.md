# 结果附件

附图/附件**不是必须行为**。当 AI 认为某些内容有助于理解问题时，会在报告**任意位置**声明：

```text
[附图@日志: on_error/on_error_20260101_120002.png]
[附图@项目: assets/resource/image/Start2.png]
[附件@日志: config/mxu-MaaXXX.json]
[附件@项目: assets/resource/pipeline/PVP.json]
```

## 必须指定来源

| 标记 | 含义 |
| --- | --- |
| `@日志` | 本次上传的**日志压缩包**内 |
| `@项目` | **项目代码仓库**内 |

来源是**必需**的，Bot 只从指定来源查找，**不会跨来源回退**。这样可避免模型写了不存在的路径、却意外命中另一个来源的同名文件而发出非预期内容。

兼容写法：`[附图: 日志:on_error/a.png]`。

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

## 图片支持图文混排

图片会与**相邻文字合并为同一条消息**（QQ 图文混排，OneBot `image` 消息段），因此图片真正出现在两段文字中间：

```text
[Bot]  结论：模板不匹配。

       下面这张是日志包里的错误截图：
       🖼️ (图片与上面文字在同一条消息内)
       可以看到界面停在活动入口。
```

想控制图片出现的位置，把指令写在那个位置即可。非图片附件无法混排，会作为**群文件**单独上传。

- 该指令行**不会展示给用户**，Bot 发送前会剥离
- 未声明时**不附带任何内容**；声明了但找不到时会在报告末尾附一行说明

## 相关设置

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `send_images` | `true` | 是否允许模型附图（总开关） |
| `max_report_images` | `4` | 最多附带几张图 |
| `send_repo_images` | `true` | 是否允许从项目仓库取图（仅本地仓库可用） |
| `max_image_mb` | `5` | 单张图大小上限 MB |
| `send_files` | `true` | 是否允许附带任意文件（以群文件上传） |
| `max_report_files` | `2` | 最多附带几个文件 |
| `max_file_attachment_mb` | `20` | 单个附件文件大小上限 MB |
| `inline_attachments` | `true` | 附件是否插在文字中间（图文混排） |
