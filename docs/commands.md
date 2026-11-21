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
/maa ai all            全部历史用量汇总及按群统计
/maa followup          追问答疑会话状态

/maa set <项> <值>      修改分析设置（如 /maa set max_report_images 3）
/maa project set name MAK
/maa project set archive_match_mode prefix
/maa project set file_prefix MaaXXX-logs
/maa project set ui.mfaa.enabled false
/maa repo url https://github.com/you/repo.git
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

/maa followup close    结束当前追问会话（需管理员及以上）
```

## 模型与报告

- 日志分析结果末尾显示“本次分析 Token 用量”：输入、输出、合计 token 及累计缓存命中率，
  数量使用完整整数；没有缓存读写记录时显示“暂无缓存数据”。统计累计本次分析各轮模型调用，
  纯文本、图片报告及渲染回退均包含此信息，追问回复不追加此行。
- `/maa ai` 的整体统计及会话明细、`/maa ai all` 的全部历史及各群统计、
  内置图片模板页脚和模型调用日志统一展示输入、输出、合计 token 与缓存命中率。
  命中率按累计 token 计算，保留一位小数；无缓存读写记录时显示“暂无缓存数据”。
- `/maa get` 会先列出**候选模型顺序**（`name/model → …`）、看图开关与报告渲染模式，
  然后才是可改的分析设置
- 模型候选不在 `settings` 中，需要改 `config.json` 的
  `bot.configs.<名称>.llm.models[]` 后 `/maa reload`
- `/maa ai [n]` 读取 `data/ai_usage.jsonl`，按**会话**聚合：同一份日志的分析与它后续的
  追问（引用的任意消息）算作一个会话，显示该会话的调用次数、分析/追问拆分、
  输入/输出/合计 token、缓存命中率、耗时与用到的 `provider/model`；不包含任何密钥
- `/maa ai all` 读取完整用量文件，汇总全部历史的会话数、调用次数、失败次数、
  输入/输出/合计 token、整体缓存命中率和累计耗时，不逐条列出会话。
  整体命中率按累计缓存读取 token 占累计输入 token 的比例计算。
  普通 `/maa ai [n]` 最多显示 20 个会话，只读取文件末尾 4 MiB 的记录。
  `all` 同时按群列出会话数、调用次数、失败次数、输入/输出/合计 token、
  缓存命中率及累计耗时，按总 token 从高到低排序。新调用记录 `group_id`，
  分析和后续追问均归入对应群；无群号且无法从同一会话补全的旧记录列为“未知群”。

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
| `commands.read_level` | `1` | 查看类指令所需等级（`status` / `get` / `project` / `repo` / `jobs` / `history` / `ai`；默认仅管理员及以上） |
| `commands.write_level` | `2` | 修改类指令所需等级 |
| `commands.followup_level` | `0` | 追问答疑所需等级 |

> **默认 `write_level=2`（仅超管可改）是刻意的保守选择**：`repo.url` 一旦可改，等于允许让 Bot 去读取任意 git 仓库。若你信任群管理员，可设 `write_level: 1`。
>
> `system_prompt` 不允许通过指令修改（避免提示词注入）。
