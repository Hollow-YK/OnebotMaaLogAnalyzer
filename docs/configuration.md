# 配置

## config.json

首次运行 `python main.py` 会自动生成 `config.json`：

```json
{
  "onebot": {
    "mode": "http_ws",
    "http_url": "http://127.0.0.1:3000",
    "ws_url": "ws://127.0.0.1:3001",
    "ws_reverse_port": 8080,
    "access_token": "",
    "api_timeout_seconds": 30
  },
  "bot": {
    "data_dir": "data",
    "configs": {
      "默认": {
        "listen_groups": ["123456789"],
        "notify_group": "123456789",
        "project": {
          "name": "MaaXXX",
          "archive_match_mode": "auto",
          "ui": { "mxu": { "enabled": true }, "mfaa": { "enabled": true } },
          "repo": { "enabled": false, "mode": "inject", "url": "https://github.com/MAAXYZ/MaaXXX" }
        },
        "llm": {
          "models": [
            { "name": "default", "model": "gpt-4o-mini" }
          ]
        },
        "vision": {
          "enabled": true,
          "max_images": 4,
          "max_image_kb": 800,
          "detail": "high",
          "attach_error_screenshots": true,
          "attach_repo_templates": true,
          "allow_model_request": true
        },
        "report": {
          "render": "auto",
          "template": "default",
          "max_width_px": 1080,
          "max_height_px": 4096,
          "max_pages": 6,
          "device_scale_factor": 1,
          "attach_markdown": false
        },
        "settings": {
          "followup_enabled": true,
          "followup_window_minutes": 0,
          "history_enabled": true,
          "history_period_hours": 24,
          "history_keep_periods": 2
        },
        "commands": { "enabled": true, "prefix": "/maa", "owners": [], "admins": [] }
      }
    }
  },
  "llm": {
    "providers": [
      {
        "name": "default",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-...",
        "max_concurrency": 2,
        "timeout_seconds": 180,
        "connect_timeout_seconds": 10,
        "max_retries": 2,
        "models": [
          {
            "model": "gpt-4o-mini",
            "supports": { "tools": true, "vision": true, "json_mode": true },
            "max_tokens": 8192,
            "max_context_tokens": 48000
          }
        ]
      }
    ],
    "defaults": {
      "temperature": 0.2,
      "top_p": 1.0,
      "max_tokens": 8192,
      "max_context_tokens": 48000
    },
    "circuit_breaker": { "failure_threshold": 3, "cooldown_seconds": 300 }
  },
  "debug": { "enabled": false, "http_port": 8765, "data_dir": "data/test" },
  "log": { "log_to_file": true, "log_level": "INFO", "log_dir": "logs" }
}
```

## 顶层字段

| 字段 | 说明 |
| --- | --- |
| `onebot.mode` | 通信模式（见下方） |
| `onebot.http_url` | HTTP API 地址，`""` 禁用 |
| `onebot.ws_url` | 正向 WS 地址，`""` 禁用 |
| `onebot.ws_reverse_port` | 反向 WS 端口，`0` 禁用 |
| `onebot.access_token` | 鉴权令牌，需与 OneBot 实现一致 |
| `onebot.api_timeout_seconds` | API 调用超时（秒），默认 30 |
| `bot.data_dir` | 数据目录 |
| `bot.configs` | **多群配置**：配置名 → 监听群 / 通知群 / 设置覆盖 / 指令设置 |
| `llm.providers[]` | **端点与密钥**（OpenAI 兼容），每个端点声明自己的 `models[]` 与能力 |
| `llm.defaults` | 全局参数兜底：`temperature` / `top_p` / `max_tokens` / `max_context_tokens` |
| `llm.circuit_breaker` | 端点级熔断：`failure_threshold` / `cooldown_seconds` |
| `bot.configs.<名称>.llm.models[]` | 该配置按优先级尝试的候选 `{name, model}` 列表 |
| `bot.configs.<名称>.vision` | 多模态（看图）设置 |
| `bot.configs.<名称>.report` | 图片化报告设置 |
| `log.log_to_file` | 是否输出日志到文件 |
| `log.log_level` | 日志等级：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log.log_dir` | 日志目录，每次启动生成 `YYYY-MM-DD_HH-MM-SS.log` |
| `debug.enabled` | 启用调试模式（默认 `false`），见 [调试](debugging.md) |
| `debug.http_port` | 调试 HTTP 端口（默认 `8765`，仅 `127.0.0.1`） |
| `debug.data_dir` | 调试模式使用的测试数据目录（默认 `data/test`） |

> `config.json` 含 API 密钥与 `access_token`，已被 `.gitignore` 排除，**不要提交到仓库**。

## LLM：端点与候选模型

全局 `llm.providers[]` 只描述**端点与端点上的模型**（含能力与参数）；
每个配置在 `bot.configs.<名称>.llm.models[]` 里按优先级引用 `{name, model}`，
**严格按数组顺序失败转移**，不做权重或轮询。

```jsonc
"bot": {
  "configs": {
    "MAN": {
      "llm": {
        "models": [
          { "name": "deepseek", "model": "deepseek-v4.1-flash" },
          { "name": "deepseek", "model": "deepseek-v4-pro" },
          { "name": "backup",   "model": "qwen-vl-max" }
        ],
        "temperature": 0.2,
        "max_tokens": 8192
      }
    }
  }
}
```

- 参数优先级（低 → 高）：`llm.defaults` → `providers[].models[]` → `bot.configs.<名称>.llm`
- `supports.tools` / `supports.vision` 决定候选能否满足带工具 / 带图调用：
  过滤在**原列表顺序**上进行；过滤后为空则该能力降级（不提供工具 / 不注入图片），
  并在报告中注明，而不是整体失败
- 引用不存在的端点或模型会在启动时被拒绝（打印具体项并以非 0 退出）
- 旧版 `llm.base_url` / `api_key` / `model` 结构不再支持：
  启动会打印迁移提示；`python main.py --print-config-template` 可输出新模板
- 每个端点一把并发信号量（`max_concurrency`），跨配置共享；`settings.analysis_concurrency`
  控制任务级并发，两者互不替代

## vision：多模态

```json
"vision": {
  "enabled": true,
  "max_images": 4,
  "max_image_kb": 800,
  "detail": "high",
  "attach_error_screenshots": true,
  "attach_repo_templates": true,
  "allow_model_request": true
}
```

- 视觉候选从同一份 `llm.models[]` 中按序筛选 `supports.vision=true` 的模型
- 默认**不缩放图片**，只按原始字节大小过滤（超过 `max_image_kb` 跳过该图）
- `attach_error_screenshots`：分析开始时自动注入 `on_error/` 截图
- `allow_model_request`：模型可在工具循环中用 `[看图@日志: 路径]` / `[看图@项目: 路径]`
  主动请求看图；该指令**不会发送给用户**
- 连续 2 次读图失败会停止本次看图，并在报告末尾注明

## report：图片化报告

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

- `render`：`image`（强制图片）/ `text`（保持纯文本分段）/ `auto`（浏览器可用则图片）
- 渲染引擎为**无头 Edge / Chrome / Chromium + CDP**，`browser_path` 为空时按平台自动探测
- 模板固定放在工程根目录 `templates/<目录名>/`（`page.html` 必需，可含
  `header.html` / `footer.html` / `style.css`）；目录名只允许 `[A-Za-z0-9_-]`
- 报告正文按 `<!-- section: 标题 -->` 分块顺序拼接；`<!-- page-break -->` 强制换页
- 内联图片按正文宽度等比缩放，**不再有单张体积上限**
  （旧的 `settings.max_image_mb` 已删除）

完整的渲染管线、分页算法、浏览器探测顺序与模板编写指南见
[图片化报告与模板](render.md)。

## 多群配置

`bot.configs` 是声明式的群组关系来源，一个群**只能属于一个配置**（否则无法确定使用哪套设置，Bot 会忽略该群）：

```json
"bot": {
  "configs": {
    "官服": {
      "listen_groups": ["111111111", "222222222"],
      "notify_group": "111111111",
      "project": {
        "name": "MaaXXX",
        "archive_match_mode": "auto"
      },
      "settings": {
        "max_log_files": 8
      }
    },
    "测试服": {
      "listen_groups": ["333333333"]
    }
  }
}
```

| 配置字段 | 说明 |
| --- | --- |
| `listen_groups` | 要监听的群号列表 |
| `notify_group` | 分析结果额外推送的群（可选，留空只发回原群） |
| `project` | **项目相关**：项目名、日志包识别方式、UI 开关、项目代码仓库 |
| `llm` | **候选模型**：按优先级引用全局 `llm.providers[]` 中的 `{name, model}` |
| `vision` | **多模态（看图）**：开关、图片数量与体积上限 |
| `report` | **图片化报告**：渲染模式、模板、分页与浏览器 |
| `settings` | 该配置的分析设置覆盖（可选，未设置项继承全局 `data/settings.json`），见 [分析设置](settings.md) |
| `commands` | 该配置的指令与权限设置（可选），见 [QQ 指令](commands.md) |

## project：项目配置

`project` 把「项目名 / 日志包识别 / 各 UI 开关 / 代码仓库」从 `settings` 中拆出来，
避免项目相关配置混在通用分析设置里。

```jsonc
"project": {
  // 项目名：同时作为报告头与提示词中的显示名
  "name": "MAK",

  // 日志包识别方式：auto（默认）/ known / prefix / any
  "archive_match_mode": "auto",

  // 仅 prefix 模式使用：自定义文件名前缀
  // "file_prefix": "MaaAssistantKedrgame-logs",
  // 仅 prefix 模式使用：是否额外做内容特征识别（默认 true）
  "require_log_layout": true,

  // 按 UI 开关：键名 mxu / mfaa / maafwapp / auto
  "ui": {
    "mfaa":     { "enabled": true },
    "maafwapp": { "enabled": false }   // 不分析 MaaFwApp 的日志包
  },

  // 项目代码仓库
  "repo": {
    "enabled": true,
    "mode": "agent",
    "url": "https://github.com/Hollow-YK/MaaAssistantKedrgame",
    "auto_checkout_version": true
  }
}
```

### 字段说明

| 字段 | 说明 |
| --- | --- |
| `name` | 项目名，同时作为报告头与提示词中的显示名 |
| `archive_match_mode` | 日志包识别方式：`auto` / `known` / `prefix` / `any`，见下节 |
| `file_prefix` | **仅 `prefix` 模式**：文件名前缀；留空则该模式不会命中任何包 |
| `require_log_layout` | **仅 `prefix` 模式**：是否额外做内容特征识别（默认 `true`） |
| `ui.<名称>.enabled` | 是否分析该 UI 的日志；未配置回退 `ui.auto`，再回退 `true` |
| `repo` | 项目代码仓库，见 [项目代码参考](repo.md) |

> 分卷合并、`maafw.bak` 数量 / 大小等属于**分析设置**，仍在
> `settings` 中（`merge_archive_parts`、`archive_part_settle_seconds`、
> `max_maafw_bak_files`、`max_maafw_bak_mb`），见
> [分析设置 · 摘要提取](settings.md#摘要提取)。

### 识别方式：archive_match_mode

日志包识别统一为「**文件名初筛（下载前）+ 内容特征识别（下载后）**」，由
`archive_match_mode` 决定初筛策略：

| 模式 | 文件名初筛 | 内容特征识别 |
| --- | --- | --- |
| `auto`（默认） | 已知 UI 命名，或名字含 `log` / `logs` / `日志` | 强制 |
| `known` | 仅已知 UI 命名（MXU / MFAAvalonia / MaaFwApp） | 强制 |
| `prefix` | 仅 `file_prefix` 开头 | 由 `require_log_layout` 决定（默认 `true`） |
| `any` | 任意 `.zip` | 强制 |

```text
上传 .zip
  ├─ 文件名初筛（archive_match_mode，见上表）
  ├─ 下载（分卷则合并）
  └─ 内容特征识别
       ├─ 识别出的 UI 被 project.ui.<名称>.enabled 关闭 → 跳过
       ├─ 命中已知 UI 布局（maafw.log / device_info.txt / logs/log-* 等）→ 分析
       ├─ 有 .log / .jsonl / on_error 截图 → 分析
       └─ 都没有 → 跳过，不调用 AI，群里回一行「已跳过」
```

- `auto` / `known` / `any` **不需要任何前缀**；三种已知 UI 的文件名与布局都能自动识别
- `any` 适合完全自定义命名的日志包：所有 zip 都会下载后校验，代价是无谓下载
- `prefix` 是唯一使用 `file_prefix` 的模式，`require_log_layout: false` 时**只信前缀**，
  不再校验内容（适合前缀已足够独特的场景）
- 内容特征识别只看包内容，仅靠文件名推断的 UI 不算命中，避免 `server-logs.zip` 这类误判
- `project.ui.<名称>.enabled: false` 会在下载后直接跳过该 UI 的日志包（不调用 AI）

### 解析优先级

`project` 与分析设置完全独立，两者没有任何字段重叠：

```text
settings：data/settings.json（全局默认）
          → config.json 的 settings
          → /maa set 覆盖

project： config.json 的 project
          → data/<配置名>/project.json（/maa project set 写入）
```

`project` 中的 `repo` 由 `_prepare_repo` 直接读取，不再经过 `settings`。

`project.ui` 的键名会归一化：`mfaa` / `mfaaValonia` / `mfaa-avalonia` 等价，
`maafwapp` / `maafw` 等价；`auto` 作为未命中时的兜底。

### 日志包命名与 UI 自动识别

`auto` / `known` / `any` 模式下都会接受三种已知 UI 命名：

| UI | 文件名示例 | `project.ui` 键 |
| --- | --- | --- |
| MXU | `MaaAssistantKedrgame-logs-1.0.0-alpha.4-20260814-135755.zip` | `mxu` |
| MFAAvalonia | `log_20261019_110324.zip`、`log_20261019_141709-part01.zip` | `mfaa` |
| MaaFwApp | `maafw_logs_20261022_195657.zip` | `maafwapp` |

Bot 会自动解析 UI 类型、软件版本、资源版本与 MaaFramework 版本，并写入摘要和报告头；
分卷包 `*-partNN.zip` 会先等待上传（连续 `archive_part_settle_seconds` 秒无新分卷），
再自动下载并合并分析。

> **不兼容旧配置**：`settings` 中的 `file_prefix` / `extra_file_prefixes` /
> `auto_detect_archive` / `display_name` / `archive_match_mode` /
> `require_log_layout` / `repo` 已全部移除，请改写到 `project`。
> 日志包匹配、UI 开关、项目名、项目仓库只在 `project` 中生效。

修改 `config.json` 后可用 `/maa reload` 热重载，**效果与重启进程一致**：

- 重新读取全部段落：`onebot`（模式 / 地址 / 令牌 / 超时）、`bot.data_dir`、`bot.configs`、`llm`、`log`、`debug`
- 日志**另开一个新文件**（同一秒内重载会追加 `_2`、`_3` 后缀），旧 handler 会先关闭
- `bot.configs` 中删除的配置会被停用；重新加回即可恢复（数据目录保留）
- `settings` / `project` / `commands` 是声明式的：删掉某个键即回退到下一层（见[分析设置](settings.md)）
- LLM 客户端、OneBot 连接、分析器与历史存储全部重建

> 重建期间连接会短暂中断（通常 1～2 秒），进行中的分析会被中止（与重启相同）。
> 新配置启动失败时会自动回退到重载前的配置，并在群里提示失败原因。
> 启动与重载时会输出一行 `[LLM] 当前配置：端点 … · 模型 … · 密钥指纹 …`，指纹是密钥的 sha256 前 8 位，可用于确认进程实际加载的是哪把密钥。
>
> **调试模式**：`debug.enabled` 的 HTTP 调试服务会随重载重建；但调试 CLI 的会话仍持有启动时的配置快照，需重启进程才能看到新配置。

## 通信模式

通过 `onebot.mode` 选择：

| 模式 | `mode` 值 | API 通道 | 事件通道 | 适用场景 |
| --- | --- | --- | --- | --- |
| HTTP + 正向 WS | `http_ws` | HTTP POST | 正向 WS（Bot 连 OneBot） | 本地开发（默认） |
| 正向 WS Universal | `ws` | WS | WS（同一连接） | 单端口部署 |
| 反向 WS Universal | `ws_reverse` | WS | 反向 WS（OneBot 连 Bot） | 内网穿透 |
| HTTP + 反向 WS | `http_ws_reverse` | HTTP POST | 反向 WS | Bot 有公网 IP |

> 四种模式任选其一。
