# QQ 指令

默认前缀 `/maa`（可在 `commands.prefix` 中修改，或设 `commands.enabled: false` 整体关闭）。

```text
/maa help              查看全部指令
/maa status            当前配置与运行状态
/maa get               查看当前分析设置
/maa repo              查看项目代码仓库设置
/maa jobs              最近分析记录
/maa history           分析历史归档（可跨天追问）
/maa followup          追问答疑会话状态

/maa set <项> <值>      修改分析设置（如 /maa set max_log_files 3）
/maa repo mode agent   切换 inject / agent
/maa repo url <地址>   设置仓库地址（支持镜像）
/maa repo path <目录>  设置本地仓库目录
/maa history clean     清理超期历史记录
/maa history purge     清空全部分析历史
/maa enable|disable    启用 / 停用本配置
/maa reload            重新加载 config.json（不重启进程）

/maa followup close    结束当前追问会话
```

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
