# 配置

`config.json` 是两个入口共用的完整配置格式。裸模式默认读取工程根目录的 `config.json`；AstrBot 插件默认读取 `data/plugin_data/OnebotMaaLogAnalyzer/config.json`，也可在面板中指定现有文件。首次运行 `python main.py` 会自动生成一份可直接启动的默认配置；`python main.py --print-config-template` 可随时把当前版本的模板打印到标准输出。

本页**按 `config.json` 的字段顺序**（`onebot` → `bot` → `llm` → `debug` → `log`）逐字段说明：每个字段都给出类型 / 默认值、作用，以及可直接粘贴的示例。

- 省略的字段一律按默认值生效；**无法识别的字段会被静默忽略**（拼错不报错，只是不生效），改完没效果时先对照本页拼写。
- 字段名同时接受 `snake_case` 与 `camelCase`（如 `listen_groups` / `listenGroups`），本页统一用 `snake_case`。
- 字段顺序与生成顺序一致，可对照[完整示例](#完整示例默认-configjson)。

| 段 | 作用 | 跳转 |
| --- | --- | --- |
| `onebot` | 连接 OneBot 协议端：模式、地址、令牌、超时 | [onebot](#onebotonebot-连接) |
| `bot` | 数据目录与多群配置（项目 / 模型 / 看图 / 报告 / 设置 / 指令） | [bot](#bot数据目录与多群配置) |
| `llm` | 全局端点、密钥、模型能力与兜底参数 | [LLM](#llm端点与候选模型) |
| `debug` | 离线调试模式（不连接协议端） | [debug](#debug离线调试) |
| `log` | 控制台 / 文件日志 | [log](#log日志) |

## 部署入口与面板覆盖

同一个文件可以继续供裸 OneBot 使用，也可以在 AstrBot 中复用。两种入口的差异只在事件和
发送通道：

- **裸 OneBot**：读取并使用 `onebot` 段，按 `ws` / `http_ws` / `ws_reverse` /
  `http_ws_reverse` 建立连接。
- **AstrBot AIOCQHTTP**：忽略 `onebot` 段，由 AstrBot 接收事件和调用协议 API；`bot`、`llm`、
  `log` 及配置档案语义保持不变。

AstrBot 面板映射常用字段和大多数高级字段；复杂结构仍需编辑 JSON。新建面板配置时，有固定程序默认值的字段直接显示这些默认值，面板值会覆盖所选档案或端点的对应配置。下表说明各字段的生效范围和空值行为：

| 面板字段 | 生效范围 | 空值或自动选择的含义 |
| --- | --- | --- |
| `config_file` | 配置文件路径 | 使用插件数据目录下的 `config.json` |
| `data_dir` | 运行数据目录 | 恢复面板默认 `data`，相对插件数据目录解析 |
| `default_config` | 选择面板编辑的档案 | 选择第一个档案 |
| `listen_groups` / `notify_group` | 所选档案的监听群、额外通知群 | 空列表停止监听；空通知群关闭额外转发 |
| `owners` / `admins` | 所选档案的指令权限名单 | 空列表清空对应名单 |
| `command_prefix` / `commands_*` | 指令前缀、开关、权限、回复上限 | 固定默认值以面板为准，清空固定值恢复默认 |
| `project_name` / `file_prefix` | 项目显示名、日志包文件名前缀 | 清空配置；空项目名允许从日志识别，prefix 模式需填写前缀 |
| `archive_match_mode` / `require_log_layout_mode` / `ui_*_mode` | 日志包识别、布局校验、UI 自动识别开关 | 使用面板固定默认值 |
| `report_*` | 报告模式、模板、尺寸、浏览器与渲染选项 | 固定值恢复面板默认；空字体和浏览器路径使用程序自动选择 |
| `repo_search_mode` / `repo_*` | 所选档案的仓库设置 | 可选文本留空清空；镜像、扩展名空列表清空；固定值恢复面板默认 |
| `llm_provider` / `llm_model` | 所选档案的首选模型引用 | 自动选择面板选中的端点、该端点选中的模型或第一个模型 |
| `llm_fallback_models` | 备用候选链，格式 `端点名:模型名` | 空列表清空备用候选 |
| `llm_profile_max_tokens` | 所选档案的输出 token 上限 | `-1` 清除档案参数，使用模型或全局参数 |
| `llm_endpoint_selector` / `llm_endpoint_model` | 选择要编辑的全局端点、该端点的模型 | 选择第一个端点、该端点的第一个模型 |
| `llm_endpoint_name` | 修改所选端点名称并更新引用 | 保留所选端点名称 |
| `llm_endpoint_base_url` / `llm_endpoint_api_key` | 所选端点的地址与密钥 | 地址恢复面板默认；空密钥清除该端点密钥 |
| `llm_endpoint_kind` / `llm_endpoint_max_*` / `llm_endpoint_*timeout_seconds` | 所选端点协议、并发、重试与超时 | 使用面板固定默认值 |
| `llm_model_*` | 所选模型能力、输出与上下文参数 | 使用面板固定默认值，token 为 `0` 时使用全局值 |
| `llm_defaults_*` / `llm_circuit_*` | 全局 LLM 参数、熔断设置 | 使用面板固定默认值 |
| `vision_*` / `send_*_mode` / `followup_*` / `history_*` / `settings_*` | 所选档案的分析、看图、发送、追问、历史等设置 | 使用面板值；空系统提示词恢复程序默认提示词 |
| `log_to_file_mode` / `log_dir` / `log_level` | 插件日志 | 使用面板固定默认值 |

**面板中提供的字段以面板为准，空字符串和空列表不再静默继承 JSON 中的同级旧值。**
插件启动时会把清空的固定默认字段，以及旧的 `inherit` / `-1` 标记，恢复成对应的面板默认值并保存，
使 WebUI 展示与实际采用的值一致。档案 token 上限为 `-1` 时，
清除同级参数并使用模型或全局参数；这些下层参数也可在 WebUI 编辑。
选择器的自动选择规则见上表。对于直接调用配置加载 API 的调用方，未提供的字段保留 JSON 值。

面板覆盖不会回写 `config.json`。未选中的配置档案、未选中的端点和模型、未映射到面板的高级字段保持原值。
JSON 中的端点和模型声明仍用于选择器定位；所选档案的备用候选以面板列表为准。
如果此前通过面板留空继承 JSON 中的监听群、权限名单、密钥、仓库或其他值，升级后需在 WebUI 填写实际需要的值。
`/maa set`、`/maa project set` 等独立的运行时覆盖层仍按各自的配置优先级生效。

复用裸模式配置时，建议在 AstrBot 面板中同时填写绝对 `config_file` 与绝对 `data_dir`。
若只写相对 `bot.data_dir`，插件会按自己的数据目录解析，不一定等于裸进程的启动目录。
完整安装、迁移和故障排查见 [AstrBot 部署](astrbot.md)。

## 完整示例：默认 config.json

首次运行生成的完整内容（`api_key`、`access_token` 默认为空，需要自己填）。监听群默认为空，需填写实际群号才会接收日志；通知群默认为空，分析结果只发回原群：

```json
{
  "onebot": {
    "mode": "http_ws",
    "http_url": "http://127.0.0.1:3000",
    "ws_url": "ws://127.0.0.1:3001",
    "ws_reverse_port": 8080,
    "ws_reverse_host": "127.0.0.1",
    "access_token": "",
    "api_timeout_seconds": 30
  },
  "bot": {
    "data_dir": "data",
    "configs": {
      "默认": {
        "listen_groups": [],
        "notify_group": "",
        "project": {
          "name": "MaaXXX",
          "archive_match_mode": "auto",
          "require_log_layout": true,
          "repo": {
            "mode": "inject",
            "path": "",
            "url": "https://github.com/MAAXYZ/MaaXXX",
            "mirrors": [],
            "branch": "",
            "preload_enabled": true,
            "preload_retries": 3,
            "update_on_analyze": false,
            "allow_ai_update": true,
            "auto_checkout_version": true,
            "allow_ai_switch_tag": true,
            "restore_latest_after_analyze": true
          }
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
          "font_family": "",
          "browser_path": "",
          "reuse_browser": true,
          "render_timeout_ms": 8000,
          "embed_attachments": true,
          "attach_markdown": false,
          "fallback_to_text": true,
          "render_followup": true
        },
        "settings": {
          "send_images": true,
          "max_report_images": 4,
          "send_repo_images": true,
          "send_files": true,
          "max_report_files": 2,
          "max_file_attachment_mb": 20,
          "followup_enabled": true,
          "followup_window_minutes": 0,
          "history_enabled": true,
          "history_period_hours": 24,
          "history_keep_periods": 2,
          "ai_stage_timeout_seconds": 300,
          "max_context_tokens": 0
        },
        "commands": {
          "enabled": true,
          "prefix": "/maa",
          "owners": [],
          "admins": [],
          "read_level": 0,
          "write_level": 2,
          "followup_level": 0
        }
      }
    }
  },
  "llm": {
    "providers": [
      {
        "name": "default",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "max_concurrency": 2,
        "timeout_seconds": 180,
        "connect_timeout_seconds": 10,
        "max_retries": 2,
        "models": [
          {
            "model": "gpt-4o-mini",
            "supports": { "vision": true, "json_mode": true },
            "max_tokens": 8192,
            "max_context_tokens": 48000
          }
        ]
      }
    ],
    "defaults": {
      "max_tokens": 8192,
      "max_context_tokens": 48000
    },
    "circuit_breaker": {
      "failure_threshold": 3,
      "cooldown_seconds": 300,
      "rate_cooldown_seconds": 60,
      "auth_cooldown_seconds": 1800
    }
  },
  "debug": {
    "enabled": false,
    "http_port": 8765,
    "data_dir": "data/test"
  },
  "log": {
    "log_to_file": true,
    "log_level": "INFO",
    "log_dir": "logs"
  }
}
```

> `config.json` 含 API 密钥与 `access_token`，已被 `.gitignore` 排除，**不要提交到仓库**。

## onebot：OneBot 连接（仅裸模式）

本段只由 `python main.py` 使用。AstrBot AIOCQHTTP 插件不读取此段。

### onebot 示例

```json
"onebot": {
  "mode": "http_ws",
  "http_url": "http://127.0.0.1:3000",
  "ws_url": "ws://127.0.0.1:3001",
  "ws_reverse_port": 8080,
  "ws_reverse_host": "127.0.0.1",
  "access_token": "",
  "api_timeout_seconds": 30
}
```

### onebot 字段

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `mode` | `string` / `"http_ws"` | 连接模式，取值见[通信模式](#通信模式)；非法值启动即报错 | `"mode": "http_ws"` |
| `http_url` | `string` / `"http://127.0.0.1:3000"` | HTTP API 根地址。`http_ws` / `http_ws_reverse` 必填，且必须是 `http://` 或 `https://` | `"http_url": "http://127.0.0.1:3000"` |
| `ws_url` | `string` / `"ws://127.0.0.1:3001"` | 正向 WS 地址（Bot 主动连协议端）。`ws` / `http_ws` 必填，必须是 `ws://` 或 `wss://` | `"ws_url": "ws://127.0.0.1:3001"` |
| `ws_reverse_port` | `int` / `8080` | 反向 WS 监听端口。`ws_reverse` / `http_ws_reverse` 必填，取值 `1`–`65535` | `"ws_reverse_port": 8080` |
| `ws_reverse_host` | `string` / `"127.0.0.1"` | 反向 WS 监听地址。只允许本机连接就用默认；容器、内网穿透、Bot 有公网 IP 时才改 `0.0.0.0` | `"ws_reverse_host": "0.0.0.0"` |
| `access_token` | `string` / `""` | OneBot 鉴权令牌，必须与协议端配置一致；空 = 不鉴权 | `"access_token": "my-token"` |
| `api_timeout_seconds` | `int` / `30` | 单次 OneBot API 调用超时（秒） | `"api_timeout_seconds": 60` |

### 通信模式

| 模式 | `mode` 值 | API 通道 | 事件通道 | 适用场景 |
| --- | --- | --- | --- | --- |
| HTTP + 正向 WS | `http_ws` | HTTP POST | 正向 WS（Bot 连 OneBot） | 本地开发（默认） |
| 正向 WS Universal | `ws` | WS | WS（同一连接） | 单端口部署 |
| 反向 WS Universal | `ws_reverse` | WS | 反向 WS（OneBot 连 Bot） | 内网穿透 |
| HTTP + 反向 WS | `http_ws_reverse` | HTTP POST | 反向 WS | Bot 有公网 IP |

四种模式任选其一，与协议端的连接配置必须成对出现（例如协议端配了反向 WS，Bot 这边就不能用 `http_ws`）。

### 启动校验

`onebot` 段在启动与 `/maa reload` 时都会校验，**错误在启动期暴露**，而不是表现为无尽的重连循环：

- `mode` 必须是上表四种之一，否则报 `未知模式: xxx，可选: ...`
- 当前模式需要的地址项必须非空，且 scheme / 端口合法
- 反向模式要求 `ws_reverse_port` 为 1–65535 的整数、`ws_reverse_host` 非空
- 任一项不合法 → 打印具体字段后以非 0 退出

### 反向 WS：监听与鉴权

- 默认只监听 `127.0.0.1`，协议端需与本 Bot 同机（或同网络命名空间）。
- 改成 `0.0.0.0` 等于对网络开放监听端口，**此时必须设置 `access_token`**。
- 令牌来源（任一满足即可）：握手头 `Authorization: Bearer <token>`，或 URL 查询参数 `?access_token=<token>`；比较使用常量时间比较。
- `access_token` 为空时反向 WS 不校验来源，启动日志会打印一条警告；正向 WS 与 HTTP 则干脆不带 `Authorization`。

## bot：数据目录与多群配置

### bot 示例

```jsonc
"bot": {
  "data_dir": "data",
  "configs": {
    "官服": {
      // 该配置监听的群；一个群只能属于一个配置
      "listen_groups": ["111111111", "222222222"],
      // 分析结果额外推送的群（可选）
      "notify_group": "111111111",

      "project": { "name": "MaaXXX" },

      "llm": {
        "models": [{ "name": "default", "model": "gpt-4o-mini" }],
        "max_tokens": 8192
      },

      "vision": { "enabled": true },

      "report": { "render": "auto" },

      "settings": { "max_report_images": 4 },

      "commands": { "enabled": true, "prefix": "/maa" }
    },
    "测试服": {
      "listen_groups": ["333333333"]
    }
  }
}
```

### bot 字段

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `data_dir` | `string` / `"data"` | 数据根目录。裸模式相对启动工作目录，AstrBot 相对插件数据目录。设置、历史、仓库缓存、用量都写在下面；调试模式下不生效 | `"data_dir": "data"` |
| `configs` | `object` / — | 多群配置：键是**配置名**，值是下面这组字段。配置名同时用于日志前缀 `[配置名]` 与数据子目录 `data/<配置名>/`，改名等于换一套数据；目录名会被清洗（只留字母数字与 `._-`，其余替换为 `_`），升级前已有的老目录名继续沿用 | `"configs": { "官服": { "listen_groups": ["111111111"] } }` |
| `configs.<名称>.listen_groups` | `string[]` / `[]` | 要监听的群号（字符串）。为空则该配置不接收任何群 | `"listen_groups": ["111111111"]` |
| `configs.<名称>.notify_group` | `string` / `null` | 分析结果额外推送的群号；`null` / 空串 = 只发回原群 | `"notify_group": "111111111"` |
| `configs.<名称>.project` | `object` / — | 项目名、日志包识别方式、UI 开关、代码仓库，见 [project](#project项目配置) | `"project": { "name": "MaaXXX" }` |
| `configs.<名称>.llm` | `object` / — | 本配置按优先级尝试的候选模型与参数，见 [llm](#llm候选模型) | `"llm": { "models": [] }` |
| `configs.<名称>.vision` | `object` / — | 多模态（看图）设置，见 [vision](#vision多模态) | `"vision": { "enabled": true }` |
| `configs.<名称>.report` | `object` / — | 图片化报告设置，见 [report](#report图片化报告) | `"report": { "render": "auto" }` |
| `configs.<名称>.settings` | `object` / — | 分析设置覆盖（未写项继承全局），见 [settings](#settings分析设置覆盖) | `"settings": { "max_report_images": 4 }` |
| `configs.<名称>.commands` | `object` / — | 指令与权限，见 [commands](#commands指令与权限) | `"commands": { "enabled": true }` |

### 数据目录布局

```text
data/
├─ settings.json                 # 全局分析设置（/maa set 的全局覆盖）
├─ ai_usage.jsonl                # 模型用量与缓存命中记录
├─ repo/<配置名>/                # 项目代码仓库缓存（不被历史清理）
└─ <配置名>/
   ├─ groups.json                # 监听群 / 通知群
   ├─ settings.json              # 该配置的 /maa set 覆盖
   ├─ project.json               # 该配置的 /maa project set 覆盖
   ├─ commands.json              # 该配置的指令设置
   ├─ jobs/records.json          # 任务记录（/maa jobs）
   └─ history/                   # 分析历史（归档包 + 追问上下文）
```

### project：项目配置

`project` 把「项目名 / 日志包识别 / 各 UI 开关 / 代码仓库」从 `settings` 中拆出来，避免项目相关配置混在通用分析设置里。

```jsonc
"project": {
  // 项目名：同时作为报告头与提示词中的显示名
  "name": "MAK",

  // 日志包识别方式：auto（默认）/ known / prefix / any
  "archive_match_mode": "auto",

  // 仅 prefix 模式使用：自定义文件名前缀
  "file_prefix": "MaaAssistantKedrgame-logs",
  // 仅 prefix 模式使用：是否额外做内容特征识别（默认 true）
  "require_log_layout": true,

  // 未知 UI 的日志包开关；已知 UI 始终启用
  "ui": { "auto": { "enabled": true } },

  // 项目代码仓库
  "repo": {
    "enabled": true,
    "mode": "agent",
    "url": "https://github.com/Hollow-YK/MaaAssistantKedrgame",
    "auto_checkout_version": true
  }
}
```

#### project 字段

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `name` | `string` / `""` | 项目名，同时作为报告头与提示词中的显示名；空 = 用压缩包解析出的项目名，再回退 `MaaXXX` | `"name": "MAK"` |
| `archive_match_mode` | `string` / `"auto"` | 下载前的文件名初筛方式：`auto` / `known` / `prefix` / `any`，见[识别方式](#识别方式archive_match_mode) | `"archive_match_mode": "auto"` |
| `file_prefix` | `string` / `""` | **仅 `prefix` 模式**：文件名前缀；留空则该模式不会命中任何包 | `"file_prefix": "MaaAssistantKedrgame-logs"` |
| `require_log_layout` | `bool` / `true` | **仅 `prefix` 模式**：是否额外做内容特征识别；`false` = 只信前缀 | `"require_log_layout": true` |
| `ui` | `object` / `{}` | 未知 UI 的开关；已知 UI 始终启用，旧配置中的对应开关不再生效 | `"ui": { "auto": { "enabled": false } }` |
| `ui.<名称>.enabled` | `bool` / `true` | 控制未知 UI 的日志包；`false` 会跳过，不调用 AI | `"enabled": false` |
| `repo` | `object` / — | 项目代码仓库，见 [repo](#repo项目代码仓库) | `"repo": { "mode": "inject" }` |

#### repo：项目代码仓库

让 AI 在分析时对照源码核对节点定义、阈值与 `expected` 文本。来源二选一（`path` 优先）；失败只记日志并跳过该段落，不影响分析。

```jsonc
"repo": {
  "mode": "inject",
  "path": "E:/Code/MAA/MaaXXX",
  "url": "https://github.com/MAAXYZ/MaaXXX",
  "mirrors": ["https://mirror.example/MaaXXX.git"],
  "preload_enabled": true,
  "preload_retries": 3,
  "auto_checkout_version": true
}
```

项目代码参考始终开启；旧配置中的 `repo.enabled` 不再生效。

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `mode` | `string` / `"inject"` | 检索模式：`inject`（预检索后把命中片段拼进提示词，快、省 token）/ `agent`（把仓库读写工具交给 AI，深、耗 token）；其它值按 `inject` | `"mode": "agent"` |
| `path` | `string` / `""` | 本地仓库目录，**优先于 `url`**，不需要网络与 git，也不会执行仓库代码 | `"path": "E:/Code/MAA/MaaXXX"` |
| `url` | `string` / `"https://github.com/MAAXYZ/MaaXXX"` | 主 git 地址（GitHub / Gitee / 自建镜像 / `file://` 均可） | `"url": "https://github.com/MAAXYZ/MaaXXX"` |
| `mirrors` | `string[]` / `[]` | 备用地址，按顺序依次尝试，直到一个来源成功 | `"mirrors": ["https://gitee.com/xx/MaaXXX.git"]` |
| `branch` | `string` / `""` | 克隆时使用的分支 / tag；空 = 仓库默认分支 | `"branch": "dev"` |
| `default_branch` | `string` / `""` | 分析结束还原时的兜底目标；空 = 自动探测 | `"default_branch": "main"` |
| `preload_enabled` | `bool` / `true` | 启动后后台完整克隆（含全部历史 / tag）并初始化子模块；**分析现场始终浅克隆**，关闭则不做完整克隆、tag 按需拉取 | `"preload_enabled": false` |
| `preload_retries` | `int` / `3` | 启动预下载失败的重试次数（最大 5，间隔递增） | `"preload_retries": 5` |
| `update_on_analyze` | `bool` / `false` | 每份日志分析前先更新默认分支，再对齐日志版本 tag | `"update_on_analyze": true` |
| `allow_ai_update` | `bool` / `true` | 允许 AI 用 `pull_latest` 更新当前仓库的默认分支（不接受 URL 或 Git 命令） | `"allow_ai_update": false` |
| `auto_checkout_version` | `bool` / `true` | 按日志包解析出的资源版本自动 checkout 对应 tag | `"auto_checkout_version": true` |
| `allow_ai_switch_tag` | `bool` / `true` | 允许 AI 获取并切换到其它 tag / commit 复核（与 `preload_enabled` 无关） | `"allow_ai_switch_tag": false` |
| `max_tag_switch_rounds` | `int` / `2` | AI 切换 tag 的最大轮次；`0` = 不开放该工具 | `"max_tag_switch_rounds": 0` |
| `restore_latest_after_analyze` | `bool` / `true` | 分析结束后还原到分析前的位置；关闭后仓库停在分析用的 tag（detached HEAD） | `"restore_latest_after_analyze": false` |
| `refuse_dirty_worktree` | `bool` / `true` | 工作区有未提交改动时拒绝切换（保护本地开发） | `"refuse_dirty_worktree": true` |
| `max_tool_rounds` | `int` / `8` | **agent** 工具调用最大轮次 | `"max_tool_rounds": 6` |
| `max_tool_calls_per_round` | `int` / `6` | **agent** 单轮最多执行的工具数 | `"max_tool_calls_per_round": 4` |
| `max_tool_result_chars` | `int` / `30000` | **agent** 单次工具结果字符上限 | `"max_tool_result_chars": 12000` |
| `max_tool_result_tokens` | `int` / `0` | **agent** 单次工具结果 token 上限；`0` = 由 `max_tool_result_chars` 估算，非 0 时最小 64 | `"max_tool_result_tokens": 4000` |
| `max_tool_context_tokens` | `int` / `0` | 工具结果累计 token 预算；`0` = 不超过本次上下文预算的 50% | `"max_tool_context_tokens": 16000` |
| `agent_deadline_seconds` | `int` / `600` | **agent** 循环总时长上限 | `"agent_deadline_seconds": 900` |
| `shallow_clone_timeout_seconds` | `int` / `600` | 分析/追问现场**浅克隆**的单次上限（秒）；`0` = 内置 600 | `"shallow_clone_timeout_seconds": 300` |
| `preload_timeout_seconds` | `int` / `1800` | 后台预下载一次**完整克隆**（含子模块）的总上限（秒）；`0` = 内置 1800 | `"preload_timeout_seconds": 900` |
| `update_timeout_seconds` | `int` / `600` | 分析/追问里「更新默认分支」的总预算（探测 + fetch，秒）；`0` = 内置 | `"update_timeout_seconds": 120` |
| `extensions` | `string[]` / `json/jsonc/yaml/yml/toml/md/txt` | 参与扫描的文件扩展名 | `"extensions": [".json", ".yaml"]` |
| `max_file_kb` | `int` / `256` | 跳过大于该值的文件（KB） | `"max_file_kb": 512` |
| `max_scan_files` | `int` / `20000` | 单次扫描文件数上限 | `"max_scan_files": 5000` |
| `max_files` | `int` / `12` | **inject** 注入提示词的文件数上限 | `"max_files": 8` |
| `max_identifiers` | `int` / `24` | **inject** 从日志中提取的标识符上限 | `"max_identifiers": 12` |
| `context_lines` | `int` / `2` | **inject** 普通命中行的上下文行数 | `"context_lines": 4` |
| `max_block_lines` | `int` / `150` | **inject** 命中节点定义时展开的 JSON 对象最大行数 | `"max_block_lines": 80` |
| `max_chars` | `int` / `60000` | **inject** 代码片段段落字符上限 | `"max_chars": 30000` |

完整的检索行为、版本切换细节与镜像策略见 [项目代码参考](repo.md)。

#### 识别方式：archive_match_mode

日志包识别统一为「**文件名初筛（下载前）+ 内容特征识别（下载后）**」，由 `archive_match_mode` 决定初筛策略：

| 模式 | 文件名初筛 | 内容特征识别 |
| --- | --- | --- |
| `auto`（默认） | 已知 UI 命名，或名字含 `log` / `logs` / `日志` | 强制 |
| `known` | 仅已知 UI 命名（MXU / MFAAvalonia / MaaFwApp / MaaTauriAndroid / MWU / MFW-PyQt6） | 强制 |
| `prefix` | 仅 `file_prefix` 开头 | 由 `require_log_layout` 决定（默认 `true`） |
| `any` | 任意 `.zip` | 强制 |

```text
上传 .zip
  ├─ 文件名初筛（archive_match_mode，见上表）
  ├─ 下载（分卷则合并）
  └─ 内容特征识别
       ├─ 未知 UI 被 project.ui.auto.enabled 关闭 → 跳过
       ├─ 命中已知 UI 布局（maafw.log / device_info.txt / logs/log-* 等）→ 分析
       ├─ 有 .log / .jsonl / on_error 截图 → 分析
       └─ 都没有 → 跳过，不调用 AI，群里回一行「已跳过」
```

- `auto` / `known` / `any` **不需要任何前缀**；六种已知 UI 的文件名与布局都能自动识别
- `any` 适合完全自定义命名的日志包：所有 zip 都会下载后校验，代价是无谓下载
- `prefix` 是唯一使用 `file_prefix` 的模式，`require_log_layout: false` 时**只信前缀**，不再校验内容
- 内容特征识别只看包内容，仅靠文件名推断的 UI 不算命中，避免 `server-logs.zip` 这类误判
- `project.ui.auto.enabled: false` 会跳过未知 UI 的日志包（不调用 AI）

#### 解析优先级

`project` 与分析设置完全独立，两者没有任何字段重叠：

```text
settings：data/settings.json（全局默认）
          → config.json 的 settings
          → /maa set 覆盖

project： config.json 的 project
          → data/<配置名>/project.json（/maa project set 写入）
```

`project.repo` 由 `_prepare_repo` 直接读取，不经过 `settings`。

MXU、MFAAvalonia、MaaFwApp、MaaTauriAndroid、MWU 和 MFW-PyQt6 始终启用，旧配置中的对应开关及 `auto` 兜底均不能关闭它们。`auto` 控制未知 UI。

#### 日志包命名与 UI 自动识别

`auto` / `known` / `any` 模式下都会接受六种已知 UI 命名：

| UI | 文件名示例 | `project.ui` 键 |
| --- | --- | --- |
| MXU | `MaaAssistantKedrgame-logs-1.0.0-alpha.4-20260814-135755.zip` | `mxu` |
| MFAAvalonia | `log_20261019_110324.zip`、`log_20261019_141709-part01.zip` | `mfaa` |
| MaaFwApp | `maafw_logs_20261022_195657.zip` | `maafwapp` |
| MaaTauriAndroid | `maa_tauri_android-logs-1760000000000-<uuid>.zip` | `mta` |
| MWU | `mwu-logs.zip` | `mwu` |
| MFW-PyQt6 | `Demo-1.2.3-20261114_101010.zip` | `mfw` |

Bot 会自动解析 UI 类型、软件版本、资源版本与 MaaFramework 版本，并写入摘要和报告头；分卷包 `*-partNN.zip` 会先等待上传（连续 `archive_part_settle_seconds` 秒无新分卷），再自动下载并合并分析。合并后按「编号连续 + 内容特征」判断是否齐全：不完整时最多额外等 `archive_part_wait_extra_rounds` 轮，仍不完整则按 `archive_part_incomplete_action` 处理（照常分析并标注缺失，或取消并提示补齐后重传）。

> 旧字段 `settings.file_prefix` / `extra_file_prefixes` / `auto_detect_archive` / `display_name` / `archive_match_mode` / `require_log_layout` / `repo` 已移除，写在 `settings` 里会被静默忽略；日志包匹配、UI 开关、项目名、项目仓库只在 `project` 中生效。

### llm：候选模型

每个配置用 `llm.models[]` 按优先级引用全局端点上的模型，**严格按数组顺序失败转移**，不做权重或轮询。

```json
"llm": {
  "models": [
    { "name": "deepseek", "model": "deepseek-v4.1-flash" },
    { "name": "deepseek", "model": "deepseek-v4-pro" },
    { "name": "backup",   "model": "qwen-vl-max" }
  ],
  "max_tokens": 8192
}
```

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `models` | `object[]` / `[]` | 候选列表，元素为 `{name, model}`：`name` = `llm.providers[].name`，`model` = 该端点的 `models[].model`。为空则启动报错 | `"models": [{ "name": "default", "model": "gpt-4o-mini" }]` |
| `models[].name` | `string` / `""` | 端点名，必须与 `llm.providers[].name` 完全一致 | `"name": "deepseek"` |
| `models[].model` | `string` / `""` | 模型名，必须在该端点的 `models[]` 中声明过 | `"model": "deepseek-v4-pro"` |
| `max_tokens` | `int` / `null` | 本配置单次最大输出 token；`null` = 用模型级，未设置时由 API 服务端决定 | `"max_tokens": 4096` |

- 引用不存在的端点或模型会在启动时被拒绝（打印具体项并以非 0 退出）
- 重复的 `{name, model}` 会被去重（保留首次出现）

### vision：多模态

把错误截图、模板图交给视觉模型（只给模型看，不发给群）。

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

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `enabled` | `bool` / `true` | 看图总开关；关闭后所有图片都不注入 | `"enabled": false` |
| `max_images` | `int` / `4` | 单次请求最多附带的图片数量（含模型主动索图），影响上下文与费用 | `"max_images": 2` |
| `max_image_kb` | `int` / `800` | 单图大小上限 KB；超限直接跳过该图，**不做缩放** | `"max_image_kb": 400` |
| `detail` | `string` / `"high"` | 图片细节等级：`low` / `high` / `auto`，原样传给端点的 `image_url.detail` | `"detail": "low"` |
| `attach_error_screenshots` | `bool` / `true` | 分析开始时自动注入日志包内 `on_error/` 截图 | `"attach_error_screenshots": false` |
| `attach_repo_templates` | `bool` / `true` | 允许附带从项目仓库取到的模板图（需 `repo` 可用） | `"attach_repo_templates": false` |
| `allow_model_request` | `bool` / `true` | 允许模型在工具循环中用 `[看图@日志: 路径]` / `[看图@项目: 路径]` 主动索图；该指令不会发送给用户 | `"allow_model_request": false` |

- 视觉候选从同一份 `llm.models[]` 中按序筛选 `supports.vision=true` 的模型；过滤后为空则不注入图片并在报告里注明
- 连续 2 次读图失败会停止本次看图，并在报告末尾注明

### report：图片化报告

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

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `render` | `string` / `"auto"` | `image`（强制图片）/ `text`（纯文本分段）/ `auto`（浏览器可用则图片）；非法值按 `auto` | `"render": "image"` |
| `template` | `string` / `"default"` | `templates/` 下的目录名，只允许 `[A-Za-z0-9_-]`；不存在的目录记 warning 并回退 `default` | `"template": "dark"` |
| `max_width_px` | `int` / `1080` | 画布宽度（CSS 像素） | `"max_width_px": 900` |
| `max_height_px` | `int` / `4096` | 单页高度上限，超出按此高度分页 | `"max_height_px": 3000` |
| `max_pages` | `int` / `6` | 最多几张；超出截断并在最后一页提示 | `"max_pages": 10` |
| `device_scale_factor` | `float` / `1.0` | 输出像素倍率；`1.0` 时输出宽度 = `max_width_px` | `"device_scale_factor": 2` |
| `font_family` | `string` / `""` | CSS `font-family`；空 = 系统 CJK 字体栈 | `"font_family": "Microsoft YaHei"` |
| `browser_path` | `string` / `""` | 浏览器可执行文件绝对路径；空 = 按平台探测 Edge / Chrome / Chromium | `"browser_path": "C:/Program Files/Google/Chrome/Application/chrome.exe"` |
| `reuse_browser` | `bool` / `true` | 常驻浏览器进程（冷启动一次，后续热渲染）；`false` 时每次渲染后关闭 | `"reuse_browser": false` |
| `render_timeout_ms` | `int` / `8000` | 单次渲染（导航 + 等字体 + 全部截图）超时 | `"render_timeout_ms": 15000` |
| `embed_attachments` | `bool` / `true` | 图片是否内联进报告图；`false` 时报告后单独发送图片消息 | `"embed_attachments": false` |
| `attach_markdown` | `bool` / `false` | 是否在图片后额外上传一份 `.md` | `"attach_markdown": true` |
| `fallback_to_text` | `bool` / `true` | 渲染异常 / 浏览器缺失时回退纯文本 | `"fallback_to_text": false` |
| `render_followup` | `bool` / `true` | 追问回答是否也渲染成图片 | `"render_followup": false` |

- 渲染引擎为**无头 Edge / Chrome / Chromium + CDP**
- 模板固定放在工程根目录 `templates/<目录名>/`（`page.html` 必需，可含 `header.html` / `footer.html` / `style.css`）
- 报告正文按 `<!-- section: 标题 -->` 分块顺序拼接；`<!-- page-break -->` 强制换页
- 内联图片按正文宽度等比缩放，**没有单张体积上限**（旧的 `settings.max_image_mb` 已删除）

完整的渲染管线、分页算法、浏览器探测顺序与模板编写指南见 [图片化报告与模板](render.md)。

### settings：分析设置覆盖

`settings` 是**声明式基线**：写在这里的值每次启动 / 重载都会重新读取；从 `config.json` 删掉某个键，就会回退到 `/maa set` 的值，再回退到 `data/settings.json`。

```json
"settings": {
  "max_report_images": 6,
  "followup_enabled": true,
  "history_period_hours": 12,
  "context_compact_ratio": 0.75,
  "download_concurrency": 4
}
```

`settings` 接受下列全部 62 个分析设置项（顺序与代码一致），逐项行为与调优建议见 [分析设置](settings.md)：

| 设置项 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `enabled` | `bool` / `true` | 该配置是否启用；`false` 时即使群里有日志包也不分析 | `"enabled": false` |
| `file_list_delay_seconds` | `float` / `0` | 上传后等待群文件列表刷新的秒数 | `"file_list_delay_seconds": 2` |
| `duplicate_window_seconds` | `int` / `120` | 同一文件 / 同一归档的去重与冷却窗口；`0` = 关闭去重 | `"duplicate_window_seconds": 60` |
| `send_progress_message` | `bool` / `true` | 是否发送进度提示 | `"send_progress_message": false` |
| `send_summary` | `bool` / `true` | 是否发送分析结果 | `"send_summary": true` |
| `merge_messages` | `bool` / `true` | 将相邻文字和图片合并为普通消息；关闭后按片段顺序逐条发送。主报告和追问均生效，不是 QQ 合并转发。AstrBot 面板字段为 `settings_merge_messages_mode`（`enabled` / `disabled`） | `"merge_messages": false` |
| `reply_chunk_chars` | `int` / `3500` | 回复分段字符数（最小 1000） | `"reply_chunk_chars": 2000` |
| `send_images` | `bool` / `true` | 是否允许模型附图 | `"send_images": false` |
| `max_report_images` | `int` / `4` | 最多附带图片数；`0` = 不发图片（与文件上限分别计数） | `"max_report_images": 6` |
| `send_repo_images` | `bool` / `true` | 是否允许从仓库取模板图 | `"send_repo_images": false` |
| `send_files` | `bool` / `true` | 是否允许附带任意文件（如配置、pipeline JSON） | `"send_files": false` |
| `max_report_files` | `int` / `2` | 最多附带文件数；`0` = 不发文件（与图片上限分别计数） | `"max_report_files": 4` |
| `max_file_attachment_mb` | `int` / `20` | 单个非图片附件大小上限 MB；`0` = 关闭文件附件 | `"max_file_attachment_mb": 50` |
| `inline_attachments` | `bool` / `true` | 旧配置兼容项：附件位置由渲染流程决定，当前版本不读取该字段 | `"inline_attachments": false` |
| `followup_enabled` | `bool` / `true` | 是否允许分析后追问答疑 | `"followup_enabled": false` |
| `followup_window_minutes` | `int` / `0` | 追问会话有效分钟数；`0` = 跟随历史保留期 | `"followup_window_minutes": 120` |
| `followup_max_turns` | `int` / `10` | 单会话最多追问轮数 | `"followup_max_turns": 5` |
| `followup_max_sessions` | `int` / `50` | 最多保留多少个追问会话 | `"followup_max_sessions": 20` |
| `history_enabled` | `bool` / `true` | 是否归档分析历史（日志包 + 追问上下文） | `"history_enabled": false` |
| `history_period_hours` | `int` / `24` | 历史保留周期（小时） | `"history_period_hours": 12` |
| `history_keep_periods` | `int` / `2` | 保留周期数，超出即删除 | `"history_keep_periods": 3` |
| `history_max_records` | `int` / `200` | 每配置历史记录条数上限；`0` = 不限 | `"history_max_records": 100` |
| `history_max_total_mb` | `int` / `4096` | 历史目录总大小上限 MB；按记录目录实际占用现算（含追问阶段补解压的 `extracted/`）；`0` = 不限 | `"history_max_total_mb": 2048` |
| `download_concurrency` | `int` / `2` | 同一配置内同时进行的下载数（分卷并行下载） | `"download_concurrency": 4` |
| `download_timeout_seconds` | `int` / `120` | 下载超时秒数 | `"download_timeout_seconds": 300` |
| `digest_timeout_seconds` | `int` / `0` | 摘要提取超时；`0` = 不限制 | `"digest_timeout_seconds": 120` |
| `ai_stage_timeout_seconds` | `int` / `300` | 整个 AI 阶段（含工具循环与收敛）总时长上限 | `"ai_stage_timeout_seconds": 600` |
| `max_context_tokens` | `int` / `0` | 上下文窗口；`0` = 候选模型优先，再回退 `llm.defaults.max_context_tokens` | `"max_context_tokens": 128000` |
| `context_compact_enabled` | `bool` / `true` | 估算输入超阈值时压缩历史；端点返回超限时也会强制压缩重试 | `"context_compact_enabled": false` |
| `context_compact_ratio` | `float` / `0.8` | 触发压缩的预算占比（0.5–0.95） | `"context_compact_ratio": 0.75` |
| `context_retain_ratio` | `float` / `0.25` | 压缩后保留最近对话的 token 占比（0.1–0.5） | `"context_retain_ratio": 0.3` |
| `context_prune_tool_result_tokens` | `int` / `512` | 压缩时历史工具结果裁剪到的 token 上限；`0` = 不裁剪 | `"context_prune_tool_result_tokens": 1024` |
| `context_summary_tokens` | `int` / `1500` | 压缩摘要的目标 token 上限 | `"context_summary_tokens": 2000` |
| `analysis_cache_hours` | `int` / `0` | 分析结果进程内缓存小时数（按 file_id，重载即清空）；`0` = 关闭 | `"analysis_cache_hours": 6` |
| `circuit_breaker_enabled` | `bool` / `true` | 连续失败时熔断端点并降级为模板报告（冷却时长见 `llm.circuit_breaker`） | `"circuit_breaker_enabled": false` |
| `system_prompt` | `string` / `""` | 自定义系统提示词；空 = 按项目名生成默认人设。**不允许通过指令修改** | `"system_prompt": "你是日志分析助手"` |
| `log_tools_enabled` | `bool` / `true` | 允许 AI 用工具直接读日志包原始文件（不依赖 `repo`） | `"log_tools_enabled": false` |
| `cleanup_after_done` | `bool` / `true` | 分析完成后清理临时文件 | `"cleanup_after_done": false` |
| `save_digest_debug` | `bool` / `false` | 把实际发送给 AI 的摘要保存到 `data/debug_digests` | `"save_digest_debug": true` |
| `max_zip_size_mb` | `int` / `100` | zip 下载大小上限 MB | `"max_zip_size_mb": 200` |
| `max_total_uncompressed_mb` | `int` / `1024` | **解压后总大小上限 MB**：下载后只读 zip 目录求和，超限直接跳过整次分析；通过后下游不再比对总量 | `"max_total_uncompressed_mb": 2048` |
| `max_prompt_chars` | `int` / `200000` | 摘要字符上限（token 预算的兜底，按优先级裁剪） | `"max_prompt_chars": 150000` |
| `max_zip_members` | `int` / `300` | 压缩包内成员数上限 | `"max_zip_members": 500` |
| `max_log_files` | `int` / `6` | 最多处理日志文件数（按 UI 布局优先选框架日志 / UI 日志 / 运行记录） | `"max_log_files": 8` |
| `max_maafw_bak_files` | `int` / `1` | 纳入摘要的 `maafw.bak.*` 轮转备份数；`0` = 不纳入 | `"max_maafw_bak_files": 2` |
| `max_maafw_bak_mb` | `int` / `64` | 单个 `maafw.bak` 大小上限 MB；`0` = 不限制 | `"max_maafw_bak_mb": 32` |
| `merge_archive_parts` | `bool` / `true` | 自动合并 `*-partNN.zip` 分卷日志包 | `"merge_archive_parts": false` |
| `archive_part_settle_seconds` | `float` / `30` | 分卷静默期：连续该秒数无新分卷才开始分析；`0` = 首个分卷立即分析 | `"archive_part_settle_seconds": 60` |
| `archive_part_require_content` | `bool` / `true` | 分卷齐全性是否要求内容特征（至少一卷含日志正文/配置）；`false` = 只按编号连续判定 | `"archive_part_require_content": false` |
| `archive_part_wait_extra_rounds` | `int` / `1` | 判定不完整时额外等待的轮数（每轮再等 `archive_part_settle_seconds` 秒） | `"archive_part_wait_extra_rounds": 2` |
| `archive_part_incomplete_action` | `str` / `analyze` | 仍不完整时：`analyze`（照常分析并标注缺失）或 `cancel`（取消并提示重传） | `"archive_part_incomplete_action": "cancel"` |
| `max_log_file_read_kb` | `int` / `512` | 日志包单文件读取/工具返回窗口上限 KB（`log_search` 流式全量扫描，不受此限） | `"max_log_file_read_kb": 1024` |
| `small_log_full_read_mb` | `int` / `5` | 小于该值完整读取（MB） | `"small_log_full_read_mb": 3` |
| `stream_medium_logs` | `bool` / `true` | 流式扫描中等日志 | `"stream_medium_logs": false` |
| `large_log_threshold_mb` | `int` / `50` | 巨型日志判定阈值 MB | `"large_log_threshold_mb": 100` |
| `max_large_log_read_kb` | `int` / `256` | 仅前缀模式读取上限 KB | `"max_large_log_read_kb": 512` |
| `stream_large_logs` | `bool` / `true` | 流式深扫巨型日志 | `"stream_large_logs": false` |
| `max_stream_log_mb` | `int` / `0` | 单个巨型日志流式扫描上限 MB；`0` = 不限制 | `"max_stream_log_mb": 100` |
| `max_sections_per_log` | `int` / `60` | 单个日志最多提取的小节数 | `"max_sections_per_log": 40` |
| `max_log_chars` | `int` / `120000` | 单个日志摘要字符上限 | `"max_log_chars": 60000` |
| `compress_context_noise` | `bool` / `true` | 折叠低价值 TRACE / DEBUG 行 | `"compress_context_noise": false` |
| `max_recognition_evidence_lines` | `int` / `60` | OCR 重点证据行数上限 | `"max_recognition_evidence_lines": 30` |
| `max_recognition_evidence_chars` | `int` / `35000` | OCR 重点证据块字符上限 | `"max_recognition_evidence_chars": 20000` |
| `log_head_lines` | `int` / `16` | 每个日志小节额外保留的开头行数（版本、路径、控制器等环境信息） | `"log_head_lines": 24` |

> 模型选择与采样参数不在 `settings` 中，而在 `bot.configs.<名称>.llm`（见 [llm](#llm候选模型)）。
> 上下文压缩与提示词缓存的组织方式见 [上下文与缓存](context.md)。

### commands：指令与权限

```json
"commands": {
  "enabled": true,
  "prefix": "/maa",
  "owners": ["10001"],
  "admins": ["10002"],
  "read_level": 0,
  "write_level": 2,
  "followup_level": 0,
  "reply_chunk_chars": 3500,
  "max_list_items": 40
}
```

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `enabled` | `bool` / `true` | 是否启用指令系统；`false` 时 `/maa ...` 完全无响应 | `"enabled": false` |
| `prefix` | `string` / `"/maa"` | 指令前缀 | `"prefix": "!maa"` |
| `owners` | `string[]` / `[]` | 超管 QQ 号列表（等级 2，可修改任何配置） | `"owners": ["10001"]` |
| `admins` | `string[]` / `[]` | 管理员 QQ 号列表（等级 1，配合 `write_level: 1` 授予写权限） | `"admins": ["10002"]` |
| `read_level` | `int` / `1` | 查看类指令所需等级（`status` / `get` / `project` / `repo` / `jobs` / `history` / `ai`；默认仅管理员及以上，`0` = 群内任何人） | `"read_level": 0` |
| `write_level` | `int` / `2` | 修改类指令所需等级（`set` / `project set` / `repo` …） | `"write_level": 1` |
| `followup_level` | `int` / `0` | 追问答疑所需等级；`followup close`（结束会话）另需至少 `1` | `"followup_level": 1` |
| `reply_chunk_chars` | `int` / `3500` | 指令回复的分段字符数 | `"reply_chunk_chars": 2000` |
| `max_list_items` | `int` / `40` | `/maa jobs` 列表最多展示的任务条数 | `"max_list_items": 20` |

- 等级 2 = `owners` 中的 QQ；等级 1 = `admins` 中的 QQ，或协议端上报的群管理 / 群主；其余为 0
- 默认 `write_level: 2`（仅超管可改）是刻意的保守选择：`repo.url` 一旦可改，等于允许让 Bot 去读取任意 git 仓库
- 默认 `read_level: 1`：查看类指令会暴露群列表、群内上传记录、本地仓库路径与模型用量，因此默认只对管理员开放；想让群成员也能查，显式设 `"read_level": 0`
- 报告本身照常发到群里（主动推送不走指令），提级只影响手动查询
- `followup_level` 默认 `0` 表示**群成员仍可引用报告追问**；但 `followup close`（结束会话）是写操作，任何情况下都至少需要等级 `1`
- `system_prompt` 不允许通过指令修改（避免提示词注入）

指令全集与输出格式见 [QQ 指令](commands.md)。

### 群归属与多群示例

`bot.configs` 是声明式的群组关系来源：一个群**只能属于一个配置**，同时出现在两个配置的 `listen_groups` 里会导致无法确定使用哪套设置，Bot 会忽略该群。

```json
"bot": {
  "configs": {
    "官服": {
      "listen_groups": ["111111111", "222222222"],
      "notify_group": "111111111",
      "project": { "name": "MAK", "archive_match_mode": "auto" },
      "settings": { "max_log_files": 8 }
    },
    "测试服": {
      "listen_groups": ["333333333"],
      "project": { "name": "MAK-test" },
      "llm": { "models": [{ "name": "backup", "model": "qwen-vl-max" }] }
    }
  }
}
```

- `configs` 中删除一个配置 → 该配置被停用，`data/<配置名>/` 保留；重新加回即可恢复
- 每个配置独立拥有 `project` / `llm` / `vision` / `report` / `settings` / `commands`，互不影响

## LLM：端点与候选模型

全局 `llm.providers[]` 只描述**端点与端点上的模型**（含能力与参数）；每个配置在 `bot.configs.<名称>.llm.models[]` 里按优先级引用 `{name, model}`。

### LLM 示例

```jsonc
"llm": {
  "providers": [
    {
      // OpenAI 兼容端点
      "name": "main",
      "kind": "openai",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-...",
      "max_concurrency": 2,
      "models": [
        {
          "model": "gpt-4o-mini",
          "supports": { "vision": true, "json_mode": true },
          "max_tokens": 8192,
          "max_context_tokens": 128000
        }
      ]
    },
    {
      // Anthropic Messages 端点（官方 Anthropic 或 DeepSeek 的 anthropic 兼容端点）
      "name": "claude",
      "kind": "anthropic",
      "base_url": "https://api.deepseek.com/anthropic",
      "api_key": "sk-...",
      "models": [
        {
          "model": "deepseek-v4-flash",
          "supports": { "vision": false },
          "max_tokens": 8192,
          "max_context_tokens": 128000
        }
      ]
    }
  ],
  "defaults": { "max_tokens": 8192, "max_context_tokens": 48000 },
  "circuit_breaker": { "failure_threshold": 3, "cooldown_seconds": 300 }
}
```

### llm.providers[]

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `name` | `string` / `""` | 端点名，供 `bot.configs.<名称>.llm.models[].name` 引用；重复或为空启动报错 | `"name": "main"` |
| `kind` | `string` / `"openai"` | 协议类型：`openai`（`/v1/chat/completions`）或 `anthropic`（`/v1/messages`，也接受 `claude` / `messages` / `anthropic-messages`）；其它值启动报错 | `"kind": "anthropic"` |
| `base_url` | `string` / `""` | 端点根地址。自动补全路径：`openai` 补 `/v1/chat/completions`，`anthropic` 补 `/v1/messages`；已含 `/v1` 或完整路径时不重复拼接 | `"base_url": "https://api.deepseek.com"` |
| `api_key` | `string` / `""` | 密钥。`openai` 用 `Authorization: Bearer`，`anthropic` 同时发 `x-api-key` 与 `Authorization`；日志只打印 sha256 前 8 位指纹 | `"api_key": "sk-..."` |
| `max_concurrency` | `int` / `2` | 该端点同时在途的请求数上限（跨配置共享的信号量） | `"max_concurrency": 4` |
| `timeout_seconds` | `int` / `180` | 单次请求总超时（秒，最小 10） | `"timeout_seconds": 300` |
| `connect_timeout_seconds` | `int` / `10` | 建立连接超时（秒，最小 1） | `"connect_timeout_seconds": 5` |
| `max_retries` | `int` / `2` | **同一候选内**的重试次数（不含失败转移）。仅 `5xx` / 网络错误 / 非配额 `429` 会重试，退避 0.5s 起翻倍、上限 8s，优先用 `Retry-After` | `"max_retries": 1` |
| `models` | `object[]` / `[]` | 该端点上的模型清单；为空启动报错 | `"models": [ { "model": "gpt-4o-mini" } ]` |

### llm.providers[].models[]

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `model` | `string` / `""` | 端点接受的模型名，原样作为请求的 `model`；同一端点内不可重复 | `"model": "gpt-4o-mini"` |
| `supports.vision` | `bool` / `false` | 是否支持图片输入。带图调用只会在 `true` 的候选上尝试 | `"vision": true` |
| `supports.json_mode` | `bool` / `false` | 声明该模型支持 JSON 模式（当前版本不发送 `response_format`，仅作能力声明与后续扩展） | `"json_mode": true` |
| `max_tokens` | `int` / `0` | 该模型的单次最大输出；`0` = 用 `llm.defaults.max_tokens` | `"max_tokens": 8192` |
| `max_context_tokens` | `int` / `0` | 该模型的上下文窗口；`0` = 用 `llm.defaults.max_context_tokens`。输入预算 = 窗口 − 输出 − 安全余量 | `"max_context_tokens": 128000` |

### llm.defaults

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `max_tokens` | `int` / `8192` | 全局兜底最大输出 | `"max_tokens": 4096` |
| `max_context_tokens` | `int` / `48000` | 全局兜底上下文窗口（最小 1024） | `"max_context_tokens": 96000` |

### llm.circuit_breaker

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `failure_threshold` | `int` / `3` | 连续失败多少次后熔断该端点 | `"failure_threshold": 5` |
| `cooldown_seconds` | `int` / `300` | 通用冷却（`5xx` / 网络 / 空响应） | `"cooldown_seconds": 120` |
| `rate_cooldown_seconds` | `int` / `60` | 限流 / 配额冷却（对齐 TPM / RPM 分钟窗口；`Retry-After` 更长时以它为准） | `"rate_cooldown_seconds": 30` |
| `auth_cooldown_seconds` | `int` / `1800` | 鉴权失败冷却（401 / 403） | `"auth_cooldown_seconds": 600` |

### 协议：kind

`kind` 决定协议，同一配置可以混用两种端点，失败转移照常工作：

| `kind` | 请求地址 | 关键差异 |
| --- | --- | --- |
| `openai`（默认） | `POST {base_url}/v1/chat/completions` | `messages[].role` 含 `system` / `tool`；工具调用用 `tool_calls`；图片用 `image_url` |
| `anthropic` | `POST {base_url}/v1/messages` | `system` 提到顶层；工具用 `tools[].input_schema`；结果用 `tool_result` 内容块；自动合并相邻同角色消息 |

Anthropic 协议的自动适配：

- 强制要求 `max_tokens`，用 `models[].max_tokens` 或 `llm.defaults.max_tokens` 填充
- 端点要求显式 `thinking` 时，自动补 `{"type": "disabled"}` 后重试
- `stop_reason` 映射为统一的结束原因，`cache_read_input_tokens` / `cache_creation_input_tokens` 计入缓存统计

OpenAI 端点的参数降级：遇到新模型参数差异时自动重试一次，`max_tokens` → `max_completion_tokens`，并丢弃不支持的 `tool_choice`。

### 参数优先级

低 → 高：

```text
max_tokens：         llm.defaults → providers[].models[] → bot.configs.<名称>.llm
上下文窗口：          llm.defaults → providers[].models[] → settings.max_context_tokens
```

### 能力过滤与降级

所有模型均按支持工具调用处理，旧配置中的 `supports.tools` 不再生效。请确保所选模型和服务实际支持工具调用。`supports.vision` 决定候选能否满足带图调用：

- 过滤在**原列表顺序**上进行，完全相同的 `{name, model}` 会被去重
- 过滤后为空则该能力降级（不提供工具 / 不注入图片），并在报告中注明，而不是整体失败
- 每个端点一把并发信号量（`max_concurrency`），跨配置共享；同一配置内的分析恒为串行（见[并发模型](architecture.md#并发模型)），因此端点并发只在该配置内部生效

### 失败转移与重试

候选按 `models[]` 顺序尝试，遇到下列情况**立即换下一个候选**：

- 401 / 403（鉴权）
- 429（限流 / 配额）
- 5xx、网络错误、空响应、非 JSON 响应

细节：

- 配额类 429（`tpm exhausted` / `quota exceeded` / `insufficient quota` 等）**不做秒级重试**，直接冷却并换候选
- `max_retries` 只作用于 `5xx` / 网络 / 非配额 429，且在**同一候选内**重试
- 上下文超限（400 / 413 / 422 且错误体表明超限）由上层压缩后重试，**不切换候选**
- 全部候选都不可用时降级为模板报告，并写明失败原因（如 `main/x：鉴权失败（HTTP 401 …）；backup/y：配额耗尽（HTTP 429 …）`）

### 熔断与冷却

- 冷却按失败类型区分：`cooldown_seconds`（通用）、`rate_cooldown_seconds`（限流 / 配额）、`auth_cooldown_seconds`（鉴权）
- 冷却结束是**半开**：放行一次试探，成功即恢复；鉴权失败同理——改好 key 后不必重启，等冷却结束会自动重新启用该端点
- `settings.circuit_breaker_enabled: false` 可整体关闭端点熔断

### 启动校验与模板

- 端点名校验：`name` 不能为空、不能重复；`base_url` 必须非空；`kind` 必须在允许集合内；`models[]` 不能为空且模型名不可重复
- 配置级校验：每个 `bot.configs.<名称>.llm.models[]` 必须非空，引用的 `name` / `model` 必须存在；错误会逐条打印并以非 0 退出
- 旧版 `llm.base_url` / `api_key` / `model` 结构不再支持：启动会打印迁移提示；`python main.py --print-config-template` 可输出新模板
- 启动与重载时会输出 `[LLM] 当前配置：端点 … · 模型 … · 密钥指纹 …`，指纹是密钥的 sha256 前 8 位，可用于确认进程实际加载的是哪把密钥

### 提示词缓存与自动压缩

两种协议都支持提示词缓存，`/maa ai` 与日志里的「缓存命中 N%」对两者一视同仁。

- 系统提示词、工具定义、历史消息在会话内保持不变，新内容只追加在末尾
- 输入预算 = 上下文窗口 − 本次最大输出 − 安全余量，避免「刚好塞满」被 400 拒绝
- 估算输入超过 `输入预算 × context_compact_ratio` 或端点返回超限时自动压缩：
  裁剪历史工具结果 → 摘要中间区段 → 必要时整轮丢弃；首条日志摘要始终保留
- 用量文件 `data/ai_usage.jsonl` 记录 `uncached_prompt_tokens` / `cache_read_tokens` / `cache_write_tokens`

原理、阈值与调优见 [上下文与提示词缓存](context.md)。

## debug：离线调试

调试模式**不连接 OneBot**，通过模拟事件注入验证逻辑，且只读写 `debug.data_dir`，不碰生产数据。

```json
"debug": {
  "enabled": true,
  "http_port": 8765,
  "data_dir": "data/test"
}
```

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `enabled` | `bool` / `false` | 是否启用离线调试模式；启用后 Bot 不连接协议端、不探测协议端、不预加载仓库 | `"enabled": true` |
| `http_port` | `int` / `8765` | 调试 HTTP 端口，仅监听 `127.0.0.1` | `"http_port": 9000` |
| `data_dir` | `string` / `"data/test"` | 调试**唯一**的数据目录，不会读写 `bot.data_dir`；优先级：显式参数 > 本字段 > `data/test` | `"data_dir": "data/debug"` |

- `debug.enabled: false`（默认）时调试接口完全不启动，Bot 正常工作
- 调试模块只存在于源码仓库，不随发布包分发；发布包中启用会提示并退出

接口、断言与内置场景见 [调试](debugging.md)。

## log：日志

```json
"log": {
  "log_to_file": true,
  "log_level": "INFO",
  "log_dir": "logs"
}
```

| 字段 | 类型 / 默认 | 说明 | 示例 |
| --- | --- | --- | --- |
| `log_to_file` | `bool` / `true` | 是否输出日志到文件；`false` 时只打控制台 | `"log_to_file": false` |
| `log_level` | `string` / `"INFO"` | 日志等级：`DEBUG` / `INFO` / `WARNING` / `ERROR`；无法识别的值按 `INFO` | `"log_level": "DEBUG"` |
| `log_dir` | `string` / `"logs"` | 日志目录（相对工作目录），不存在会自动创建 | `"log_dir": "logs/2026"` |

- 每次启动生成 `YYYY-MM-DD_HH-MM-SS.log`；同一秒内重载会追加 `_2`、`_3` 后缀
- 行内 `[配置名]` 标注该行属于哪个配置

## 热重载

修改 `config.json` 后可用 `/maa reload` 热重载，**效果与重启进程一致**：

- 重新读取全部段落：`onebot`（模式 / 地址 / 令牌 / 超时）、`bot.data_dir`、`bot.configs`、`llm`、`log`、`debug`
- 日志**另开一个新文件**（同一秒内重载会追加 `_2`、`_3` 后缀），旧 handler 会先关闭
- `bot.configs` 中删除的配置会被停用；重新加回即可恢复（数据目录保留）
- `settings` / `project` / `commands` 是声明式的：删掉某个键即回退到下一层（见[分析设置](settings.md)）
- LLM 客户端、OneBot 连接、分析器与历史存储全部重建

> 重建期间连接会短暂中断（通常 1～2 秒），进行中的分析会被中止（与重启相同）。
> 新配置启动失败时会自动回退到重载前的配置，并在群里提示失败原因。
>
> **调试模式**：`debug.enabled` 的 HTTP 调试服务会随重载重建；但调试 CLI 的会话仍持有启动时的配置快照，需重启进程才能看到新配置。

## 安全提示

- `config.json` 含 API 密钥与 `access_token`，已在 `.gitignore` 中，**不要提交、不要贴进 issue**。
- `commands.write_level` 默认 `2`（仅超管可改）：`repo.url` 可被改写意味着可以让 Bot 去读取任意 git 仓库。
- 反向 WS 的 `ws_reverse_host` 默认只监听 `127.0.0.1`；改成 `0.0.0.0` 时务必配置 `access_token`。
- `repo.path` 指向的目录只被读取，不会执行其中任何代码；但仍建议指向可信仓库。
