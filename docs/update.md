# 检查更新与自动更新

程序会从固定的 GitHub 仓库
[`Hollow-YK/OnebotMaaLogAnalyzer`](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer)
检查新版本。更新分为**正式版**和**测试版**两个通道，默认只检查正式版。

## 默认行为

- 检查更新：**开启**
- 通道：**正式版 `stable`**
- 自动更新：**关闭**
- 启动约 10 秒后检查一次，之后每 24 小时检查一次
- 发现新版本后私聊 `update.notify_qq` 中的 QQ 号
- 检查失败也会私聊通知，但**不会自动重试**

自动检查关闭后，仍可手动执行 `/maa update check`。

## 配置

```json
"update": {
  "check_enabled": true,
  "channel": "stable",
  "auto_update": false,
  "download_mode": "auto",
  "notify_qq": ["123456789"],
  "check_on_start": true,
  "check_interval_hours": 24,
  "api_bases": ["https://api.github.com"],
  "download_mirrors": [],
  "github_token": ""
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `check_enabled` | `true` | 自动检查总开关；关闭后仍可手动检查 |
| `channel` | `stable` | `stable` 正式版 / `prerelease` 测试版（含正式版与预发布，取最新） |
| `auto_update` | `false` | 发现新版本后自动下载安装 |
| `download_mode` | `auto` | `auto` / `full` / `incremental` |
| `notify_qq` | `[]` | 接收更新与失败通知的 QQ 号列表；为空则只写日志 |
| `check_on_start` | `true` | 启动后检查一次 |
| `check_interval_hours` | `24` | 自动检查间隔，最小 1 小时 |
| `api_bases` | GitHub API | 依次查询全部地址，取结果中最新的版本 |
| `download_mirrors` | `[]` | 下载镜像，支持 `{url}` 占位或直接前缀拼接 |
| `github_token` | `""` | 可选，提高 GitHub API 限额；日志中脱敏 |

`notify_qq` 中的账号必须是机器人好友，否则协议端会拒绝私聊。通知失败只记录日志。

AstrBot 面板的「检查更新」分组提供上述大部分字段；`github_token` 只放在高级
`config.json`，避免在面板中暴露。

## 通道

- `stable`：只接受 `prerelease=false` 的 Release。
- `prerelease`：正式版和预发布一起比较，取版本号最高者。例如同时存在
  `v0.2.1` 与 `v0.3.0-beta.2` 时选择后者。

版本号使用 SemVer 比较：正式版高于同版本预发布，`beta.10` 高于 `beta.2`。

## 多个 GitHub API

`api_bases` 按顺序逐个查询，每个地址只请求一次：

- 某个地址失败时记录错误并继续下一个；
- 全部成功后取所有结果中版本最高的 Release；
- 全部失败时私聊 `notify_qq` 报告失败，**不自动重试**，等待下一次周期检查；
- 部分地址失败但至少一个成功时，不单独发送失败通知。

## 通知

发现新版本时发送：

```text
【OnebotMaaLogAnalyzer 更新】
发现新版本：v0.3.0（正式版）
当前版本：0.2.2
发布页：https://github.com/Hollow-YK/OnebotMaaLogAnalyzer/releases/tag/v0.3.0
自动更新：已关闭
更新内容摘要：
...
发送 /maa update apply 可手动更新。
```

同一通道的同一版本只通知一次。更新成功后下次启动会再发送一次
“已更新到 x 并成功启动”；如果启动失败并回退，会通知回退结果。

## 自动更新

`auto_update=true` 时，发现新版本后自动执行：

1. 下载 Release manifest，并用 `SHA256SUMS.txt` 校验；
2. 对比本地 `build-manifest.json`，计算差异；
3. 按 `download_mode` 下载整包或仅差异文件；
4. 在 `versions/.tmp-*` 构建完整的新版本快照；
5. 校验全部文件哈希并做启动自检；
6. 原子改名为 `versions/<版本>`；
7. 备份配置等关键小文件；
8. 原子切换 `current.json`；
9. 裸模式通过稳定引导器重启，AstrBot 模式提示重载插件。

### 下载模式

- `full`：下载整包 zip。
- `incremental`：只下载哈希变化的文件；失败直接报错，不回退整包。
- `auto`：优先增量；manifest 缺失、差异过大或增量失败时回退整包一次。

增量文件从对应 Git tag 的 `raw.githubusercontent.com` 路径下载；CI 生成的文件
（`build-info.json`、`maa_analyzer/version.py`、AstrBot 的 `metadata.yaml` 和根
`main.py`）直接内嵌在 manifest 中。

## 崩溃安全设计

更新不会修改正在运行的版本。安装目录大致为：

```text
OnebotMaaLogAnalyzer/
├── main.py                 # 稳定引导器 / AstrBot shim
├── maa_bootstrap.py        # 引导器逻辑
├── current.json            # 当前版本指针（原子替换）
├── last-good.json          # 最后一次确认启动成功的版本
├── config.json
├── data/
├── logs/
├── versions/
│   ├── 0.2.1/
│   └── 0.3.0/
├── main.py.legacy          # 首次迁移前的旧入口
└── .updates/               # 临时目录、备份与日志
```

首次自动更新会把原来的扁平安装迁移为上述版本目录布局，同时保留 `main.py.legacy`
和根目录旧文件；迁移中途失败时旧扁平程序仍然可用。

保证点：

- 新版本在独立目录完整构建并校验，失败时只清理临时目录；
- 指针使用临时文件 + `fsync` + `os.replace` 原子切换；
- 裸模式由稳定引导器监督启动，新版本未在超时内确认启动会自动切回旧版本；
- 同一新版本连续两次启动失败会被标记为坏版本，不再自动尝试；
- 回退时恢复 `config.json`、AstrBot 的 `metadata.yaml` / `_conf_schema.json`
  以及关键小数据文件；
- 旧版本目录与 `main.py.legacy` 保留，作为最终回退；
- 根目录 `templates/` 按旧 manifest 哈希做三方判断：用户改过的模板保留，
  未修改的默认模板随新版本更新，新增模板会补入。

即使下载中断、磁盘写入失败、进程被杀或断电，下次启动也会选择可用版本。
该保证以磁盘本身未损坏为前提。

## 源码运行

从 git clone 直接运行、且没有 CI 写入的 `build-info.json` 时：

- 允许检查更新、接收新版本通知；
- **不允许自动更新**，`/maa update apply` 也会被拒绝；
- 请使用 `git pull` 自行更新。

## 指令

| 指令 | 权限 | 行为 |
| --- | --- | --- |
| `/maa update` / `/maa update status` | 查看权限 | 当前版本、安装类型、通道、自动更新、上次检查 |
| `/maa update check` | 查看权限 | 立即检查配置中的通道 |
| `/maa update check stable` | 查看权限 | 立即检查正式版 |
| `/maa update check pre` | 查看权限 | 立即检查测试版 |
| `/maa update apply` | 写权限 | 下载并安装最新版本 |

## 发布产物

CI 在每个压缩包中写入：

- `build-info.json`：版本、包类型（`standard` / `light` / `astrbot`）、仓库与 tag；
- `build-manifest.json`：包内每个文件的 SHA256。

Release 额外上传三个 `*.manifest.json` 和 `SHA256SUMS.txt`。增量更新依赖这些文件。

## 故障排查

- **没有收到通知**：检查 `update.notify_qq` 是否配置、机器人是否为目标账号好友、
  日志中是否有私聊发送失败记录。
- **检查失败**：查看 `data/updates/last-result.json` 和日志中的 API 错误；
  程序不会自动重试，可手动 `/maa update check`。
- **更新后回退**：查看 `current.json` 的 `bad` 列表与 `.updates/backups/`；
  修复后删除 `current.json` 中的 `bad` 项或重新安装发布包。
- **AstrBot 更新后未生效**：在 AstrBot 面板重载插件；根目录 `main.py` 是稳定 shim，
  重载后会读取 `current.json` 的新版本。
