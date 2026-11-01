# 项目代码参考

开启后，Bot 会在调用 AI 前完成三件事：

1. **对齐版本** — 优先从压缩包内解析资源版本（如 MFAAvalonia 的 `资源版本：v1.4.7`），否则从文件名提取（如 `MaaXXX-logs-1.0.0-alpha.9-20260907-033044.zip` → `1.0.0-alpha.9`），自动 checkout 对应 tag，使代码参考与日志实际运行的版本一致
2. **检索代码** — 见下方两种模式
3. **还原位置** — 分析结束（无论成败）**精确还原到分析前所在的 ref**（分支 / tag / 提交），不会把你的工作分支切走

来源二选一（`path` 优先）：本地已有仓库目录，或 git 主地址与备用镜像。备用镜像按配置顺序依次尝试；克隆到 `data/repo/<安全配置名>` 并复用。

启用预下载时，Bot 在启动后后台**完整克隆**仓库（全部历史与 tag，并初始化子模块；最多重试 `preload_retries` 次），此后切换任意日志版本都不需要再联网。**分析现场始终使用临时浅克隆**，不等待启动下载；完整克隆成功后 canonical 生效，等当前分析结束（持配置锁）后自动清理临时浅克隆。若完整克隆一直失败，临时浅克隆会保留复用，并按 300 秒冷却重试后台完整克隆。

关闭预下载时**不做完整克隆**，分析现场照常用临时浅克隆（`--depth 1 --single-branch`），tag / commit 按需拉取。若此前是浅克隆、之后打开预下载，后台会自动重新完整克隆。

> 进程被中断可能留下 `.clone-*` / `.stale-*` 暂存目录；下次启动会按配置自动清理。克隆超时或取消时会连同 git 子进程（`index-pack` / `remote-https`）一起终止，避免残留文件被占用导致清理失败。

> 每个来源在克隆前会先用 `git ls-remote` 探测（60 秒，可用预算收紧），无响应的镜像会直接跳过，而不是等到克隆超时。
> 单次上限由配置决定：浅克隆 `shallow_clone_timeout_seconds`（默认 600s）、后台完整克隆 `preload_timeout_seconds`（默认 1800s，含子模块）、更新默认分支 `update_timeout_seconds`（默认 600s）；`0` 表示沿用内置常量。

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
// config.json → bot.configs.<配置名>.project.repo
"project": {
  "name": "MAK",
  "repo": {
    "enabled": true,
    "mode": "inject",                            // 或 "agent"
    "path": "E:/Code/MAA/MaaXXX",               // 本地仓库（推荐，无需网络与 git）
    "url": "https://github.com/MAAXYZ/MaaXXX",  // 主地址
    "mirrors": ["https://mirror.example/MaaXXX.git"], // 按顺序备用
    "preload_enabled": true,
    "preload_retries": 3,
    "branch": ""
  }
}
```

> 仓库配置只写在 `project.repo`（`config.json` 或 `/maa repo` / `/maa project set repo.*`），
> `settings` 中已不再有 `repo` 字段。

## 全部设置项

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用项目代码参考 |
| `mode` | `inject` | 检索模式：`inject` 或 `agent` |
| `path` | 空 | 本地仓库目录，**优先于 `url`**，不执行任何仓库代码 |
| `url` | `https://github.com/MAAXYZ/MaaXXX` | 主 git 地址；始终浅克隆 |
| `mirrors` | `[]` | 备用 git 地址，按顺序依次尝试，直到一个来源成功 |
| `branch` | 空 | 克隆时使用的分支 / tag，空 = 默认分支 |
| `default_branch` | 空 | 无法还原原位置时的兜底目标，空 = 自动探测 |
| `preload_enabled` | `true` | 启动后后台完整克隆一次（全部历史 / tag + 子模块）；关闭时在首次日志到达时浅克隆 |
| `preload_retries` | `3` | 启动预下载失败时的尝试次数（最大 5 次，间隔递增） |
| `update_on_analyze` | `false` | 每份日志分析前更新默认分支，然后再对齐日志版本 tag |
| `allow_ai_update` | `true` | agent 模式允许 AI 用 `pull_latest` 更新当前配置仓库的默认分支；不接受 URL 或 Git 命令 |
| `auto_checkout_version` | `true` | 按压缩包内解析的资源版本（无则用文件名版本）自动切 tag |
| `allow_ai_switch_tag` | `true` | 允许 AI 获取并切换到其他 tag / commit 复核，与预下载开关无关 |
| `max_tag_switch_rounds` | `2` | AI 切换 tag 的最大轮次（`0` = 不开放该工具） |
| `restore_latest_after_analyze` | `true` | 分析结束后还原到分析前的位置 |
| `refuse_dirty_worktree` | `true` | 工作区有未提交改动时拒绝切换（保护本地开发） |
| `max_tool_rounds` | `8` | **agent** 工具调用最大轮次 |
| `max_tool_calls_per_round` | `6` | **agent** 单轮最多执行的工具数 |
| `max_tool_result_chars` | `30000` | **agent** 单次工具结果字符上限 |
| `max_tool_result_tokens` | `0` | **agent** 单次工具结果 token 上限；`0` = 由字符上限估算 |
| `max_tool_context_tokens` | `0` | 工具结果累计 token 预算；`0` = 不超过上下文预算的 50% |
| `agent_deadline_seconds` | `600` | **agent** 循环总时长上限 |
| `shallow_clone_timeout_seconds` | `600` | 分析/追问现场**浅克隆**的单次上限（秒）；`0` = 内置 |
| `preload_timeout_seconds` | `1800` | 后台预下载一次**完整克隆**（含子模块）的总上限（秒）；`0` = 内置 |
| `update_timeout_seconds` | `600` | 分析/追问里「更新默认分支」的总预算（探测 + fetch，秒）；`0` = 内置 |
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
> **子模块**：预下载模式下每次更新 / 切换版本后会执行 `submodule update --init --recursive`；子模块失败只告警，主仓库仍可用。`path` 模式不会改动你的子模块。
>
> **失败原因可见**：克隆 / 更新失败时，日志与 `/maa repo` 的预下载状态会给出 git 报错原文（只剥掉 URL 里的凭据与 query token，公开地址照常显示），便于判断是网络、403 还是地址错误。只有本身带凭据的地址会被替换成 `<来源#N>`。
>
> **AI 切版本不阻塞**：预下载（完整克隆）已把历史与 tag 拉到本地，AI 请求的版本只要本地已有就直接切换、**完全不联网**；只有本地确实没有的目标才按需拉取，且先并发探测各来源（单个 15s），只对可达来源做短超时拉取（单个 90s、总计 180s）。全部失败时立即返回原因并保持当前版本，让模型基于当前代码给结论，而不是挂到分析阶段超时。
>
> **AI 远端操作受限**：AI 只能调用不带参数的 `pull_latest` 更新此配置的仓库；不得指定 URL、执行任意 Git 命令。获取 tag / commit 的工具由 `allow_ai_switch_tag` 控制（与预下载是否开启无关），且受 `max_tag_switch_rounds` 限制。
>
> **本地开发安全**：若 `path` 指向你正在开发的仓库且存在未提交改动，Bot 会跳过版本切换（`refuse_dirty_worktree`），避免 `checkout` 破坏工作区；此时仍会检索当前分支的代码。
>
> **并发与仓库状态**：启用仓库（`repo.enabled=true`）的配置**按配置串行分析**——切换 tag、还原位置、追问取图都会改动同一个工作区。现在**所有配置都按配置排队**（不只开仓库的）：同一配置内分析、追问、仓库操作共用一把工作锁，永远不并行；不同配置各有一份仓库与信号量，互不影响。需要更高吞吐时请拆成多个配置，并给每个配置独立的仓库来源或 `path`。

## 获取路径（谁在什么时候克隆）

**分析现场只做浅克隆，完整克隆只由后台预下载负责。**

```
分析 / 追问 需要项目代码
  └─ canonical 已是完整克隆？ ── 是 ──► 直接用它（0 等待）
        │
        否（缺失 / 仍是浅克隆 / 正在升级）
        ├─► 用「每配置一份」的临时浅克隆：<data>/repo/.temporary/<配置>-<随机>
        │     单次上限 shallow_clone_timeout_seconds（默认 600s）
        └─► 同时确保后台有一个完整克隆任务（失败后会按 300s 冷却重试）
              单次上限 preload_timeout_seconds（默认 1800s，含子模块）

后台完整克隆成功
  └─► canonical 变为完整克隆
       └─► 等当前分析结束（持配置锁）后删掉那份临时浅克隆
```

由此得到的时长上界（各段独立、不互相侵占）：

- 分析现场等待仓库 ≈ 来源数 ×（探测 60s + 浅克隆）——最坏可乘镜像数量；
- `update_on_analyze: true` 时另加 `update_timeout_seconds`（默认 600s）；
- 后台完整克隆不阻塞分析，但会与分析的浅克隆**并行**占用同一远端带宽；
- 三个数字都是 `project.repo` 字段，`0` 表示沿用内置常量（600 / 1800 / 600）。

> 浅克隆只有 `--depth 1 --single-branch`：没有历史与 tag。需要核对旧版本 tag 时由
> `align_version` 按需拉取（探测 15s / 拉取 90s / 总计 180s，见上文「AI 切版本不阻塞」），
> 或等后台完整预下载完成后再分析。
