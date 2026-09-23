# 项目代码参考

开启后，Bot 会在调用 AI 前完成三件事：

1. **对齐版本** — 从日志包文件名提取版本号（如 `MaaXXX-logs-1.0.0-alpha.9-20260907-033044.zip` → `1.0.0-alpha.9`），自动 checkout 对应 tag，使代码参考与日志实际运行的版本一致
2. **检索代码** — 见下方两种模式
3. **还原位置** — 分析结束（无论成败）**精确还原到分析前所在的 ref**（分支 / tag / 提交），不会把你的工作分支切走

来源二选一（`path` 优先）：本地已有仓库目录，或 git 地址（支持镜像，浅克隆到 `data/repos/<hash>` 并复用）。

## 两种检索模式

| 模式 | `mode` | 原理 | 耗时 | token 成本 | 适用 |
| --- | --- | --- | --- | --- | --- |
| 注入 | `inject`（默认） | Bot 从日志提取标识符 → 正则检索仓库 → 把命中片段拼进提示词 | 快（~1s 检索） | 低（单次调用） | 日常排查 |
| 自主 | `agent` | 把仓库读写工具交给 AI，由 AI 多轮自主检索 | 慢（多轮往返） | 高（每轮重发上下文） | 疑难问题、需追踪跨文件流程 |

> **inject 的局限**：只注入排序后的前 `max_files` 个文件；AI 无法在分析中途追加检索，也无法追踪 `next` 链跨文件跳转；“什么算相关”由 Bot 的正则决定。
>
> **agent 的代价**：每轮工具调用都要重发全部上下文（真实日志摘要约 170K 字符），token 与耗时成倍上升。受 `max_tool_rounds` / `agent_deadline_seconds` 双重限制。

## 配置示例

```jsonc
// config.json → bot.configs.<配置名>.settings.repo
"repo": {
  "enabled": true,
  "mode": "inject",                            // 或 "agent"
  "path": "E:/Code/MAA/MaaXXX",               // 本地仓库（推荐，无需网络与 git）
  "url": "https://github.com/MAAXYZ/MaaXXX",  // 或 git 地址，支持镜像
  "branch": ""
}
```

## 全部设置项

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用项目代码参考 |
| `mode` | `inject` | 检索模式：`inject` 或 `agent` |
| `path` | 空 | 本地仓库目录，**优先于 `url`**，不执行任何仓库代码 |
| `url` | `https://github.com/MAAXYZ/MaaXXX` | git 地址，**支持镜像**；浅克隆到 `data/repos/<hash>` 并复用 |
| `branch` | 空 | 克隆时使用的分支 / tag，空 = 默认分支 |
| `default_branch` | 空 | 无法还原原位置时的兜底目标，空 = 自动探测 |
| `update_on_analyze` | `false` | 每次分析前 `git pull`（仅 `url` 模式） |
| `auto_checkout_version` | `true` | 按日志包文件名中的版本号自动切 tag |
| `allow_ai_switch_tag` | `true` | 允许 AI 请求切换其他 tag 复核 |
| `max_tag_switch_rounds` | `2` | AI 切换 tag 的最大轮次 |
| `restore_latest_after_analyze` | `true` | 分析结束后还原到分析前的位置 |
| `refuse_dirty_worktree` | `true` | 工作区有未提交改动时拒绝切换（保护本地开发） |
| `max_tool_rounds` | `8` | **agent** 工具调用最大轮次 |
| `max_tool_calls_per_round` | `6` | **agent** 单轮最多执行的工具数 |
| `max_tool_result_chars` | `30000` | **agent** 单次工具结果字符上限 |
| `agent_deadline_seconds` | `600` | **agent** 循环总时长上限 |
| `clone_depth` | `1` | 浅克隆深度，`0` = 完整克隆（含全部历史） |
| `fetch_tags_on_demand` | `true` | 需要某个 tag 时才拉取（避免首次克隆拉整个仓库） |
| `clone_timeout_seconds` | `300` | git 克隆 / 拉取 / 切换超时秒数 |
| `extensions` | `json/jsonc/yaml/yml/toml/md/txt` | 参与扫描的文件扩展名 |
| `max_file_kb` | `256` | 跳过大于该值的文件 KB |
| `max_scan_files` | `20000` | 单次扫描文件数上限 |
| `max_files` | `12` | **inject** 注入提示词的文件数上限 |
| `max_identifiers` | `24` | **inject** 从日志中提取的标识符上限 |
| `context_lines` | `2` | **inject** 普通命中行的上下文行数 |
| `max_block_lines` | `150` | **inject** 命中节点定义时展开的 JSON 对象最大行数 |
| `max_chars` | `60000` | **inject** 代码片段段落字符上限 |

## 注意事项

> **失败不影响分析**：仓库不存在、git 未安装、克隆失败、tag 不存在或检索异常时，只记录日志并跳过该段落，分析照常进行。
>
> **命中节点定义时会自动展开整个 JSON 对象**（inject 模式），因此 `threshold`、`template`、`roi`、`expected` 等关键字段会完整呈现。
>
> **版本号前缀自动兼容**：日志里是 `1.0.0-alpha.9`、仓库 tag 是 `v1.0.0-alpha.9` 也能正确匹配；若日志版本比 tag 更细（带构建号），会退化为前缀匹配。
>
> **镜像地址**：`url` 接受任意 git 可达地址（GitHub / Gitee / 自建镜像 / 本地 `file://`）。
>
> **本地开发安全**：若 `path` 指向你正在开发的仓库且存在未提交改动，Bot 会跳过版本切换（`refuse_dirty_worktree`），避免 `checkout` 破坏工作区；此时仍会检索当前分支的代码。
