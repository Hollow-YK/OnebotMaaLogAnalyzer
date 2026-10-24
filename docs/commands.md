# QQ 指令

默认前缀 `/maa`（可在 `commands.prefix` 中修改，或设 `commands.enabled: false` 整体关闭）。

```text
/maa help              查看全部指令
/maa status            当前配置与运行状态
/maa get               查看当前分析设置
/maa project           查看项目配置（名称 / 各 UI / 仓库）
/maa repo              查看项目代码仓库设置
/maa jobs              最近分析记录
/maa history           分析历史归档（可跨天追问）
/maa ai [n]            最近 n 个会话（分析 + 追问）的用量（默认 5 个）
/maa followup          追问答疑会话状态

/maa set <项> <值>      修改分析设置（如 /maa set max_log_files 3）
/maa project set name MAK
/maa project set archive_match_mode prefix
/maa project set file_prefix MaaXXX-logs
/maa project set ui.mfaa.enabled false
/maa project set repo.mode agent
/maa repo mode agent   切换 inject / agent
/maa repo url <地址>   设置仓库主地址
/maa repo path <目录>  设置本地仓库目录
/maa repo mirror add <地址>    添加备用镜像（按顺序尝试）
/maa repo mirror remove <序号或地址>
/maa repo preload on|off       启用 / 关闭启动预下载
/maa history clean     清理超期历史记录
/maa history purge     清空全部分析历史
/maa enable|disable    启用 / 停用本配置
/maa reload            按 config.json 重建运行时（等同重启，日志另开新文件）

/maa followup close    结束当前追问会话
```

## 模型与报告

- `/maa get` 会先列出**候选模型顺序**（`name/model → …`）、看图开关与报告渲染模式，
  然后才是可改的分析设置
- 模型候选不在 `settings` 中，需要改 `config.json` 的
  `bot.configs.<名称>.llm.models[]` 后 `/maa reload`
- `/maa ai [n]` 读取 `data/ai_usage.jsonl`，按**会话**聚合：同一份日志的分析与它后续的
  追问（引用的任意消息）算作一个会话，显示该会话的调用次数、分析/追问拆分、
  输入/输出/合计 token、耗时与用到的 `provider/model`；不包含任何密钥

## 权限等级

| 等级 | 身份 | 说明 |
| --- | --- | --- |
| 2 | 超管 | `commands.owners` 中列出的 QQ 号 |
| 1 | 管理员 | `commands.admins` 中列出的 QQ 号，或协议端上报的群管理/群主 |
| 0 | 普通成员 | 其他群成员 |

## 设置项

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `commands.enabled` | `true` | 是否启用指令 |
| `commands.prefix` | `/maa` | 指令前缀 |
| `commands.owners` | 空 | 超管 QQ 号列表 |
| `commands.admins` | 空 | 管理员 QQ 号列表 |
| `commands.read_level` | `0` | 查看类指令所需等级 |
| `commands.write_level` | `2` | 修改类指令所需等级 |
| `commands.followup_level` | `0` | 追问答疑所需等级 |

> **默认 `write_level=2`（仅超管可改）是刻意的保守选择**：`repo.url` 一旦可改，等于允许让 Bot 去读取任意 git 仓库。若你信任群管理员，可设 `write_level: 1`。
>
> `system_prompt` 不允许通过指令修改（避免提示词注入）。
