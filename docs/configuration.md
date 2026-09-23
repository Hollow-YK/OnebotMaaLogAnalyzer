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
        "settings": {
          "file_prefix": "MaaXXX-logs",
          "followup_enabled": true,
          "followup_window_minutes": 0,
          "history_enabled": true,
          "history_period_hours": 24,
          "history_keep_periods": 2,
          "repo": { "enabled": false, "mode": "inject", "url": "https://github.com/MAAXYZ/MaaXXX" }
        },
        "commands": { "enabled": true, "prefix": "/maa", "owners": [], "admins": [] }
      }
    }
  },
  "llm": {
    "base_url": "https://api.openai.com",
    "api_key": "sk-...",
    "model": "gpt-4o",
    "temperature": 0.2,
    "timeout_seconds": 900,
    "fallback_models": [],
    "max_tokens": 0
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
| `llm.base_url` | OpenAI 兼容接口地址（**必须配置**） |
| `llm.api_key` | API 密钥 |
| `llm.model` | 模型名（**必须配置**） |
| `llm.temperature` | 默认分析温度（可被配置级覆盖） |
| `llm.timeout_seconds` | 默认 AI 超时（可被配置级覆盖） |
| `llm.fallback_models` | 备用模型列表，主模型失败时按序尝试 |
| `llm.max_tokens` | 最大输出 token，`0` 表示不限制 |
| `log.log_to_file` | 是否输出日志到文件 |
| `log.log_level` | 日志等级：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log.log_dir` | 日志目录，每次启动生成 `YYYY-MM-DD_HH-MM-SS.log` |
| `debug.enabled` | 启用调试模式（默认 `false`），见 [调试](debugging.md) |
| `debug.http_port` | 调试 HTTP 端口（默认 `8765`，仅 `127.0.0.1`） |
| `debug.data_dir` | 调试模式使用的测试数据目录（默认 `data/test`） |

> `config.json` 含 API 密钥与 `access_token`，已被 `.gitignore` 排除，**不要提交到仓库**。

## 多群配置

`bot.configs` 是声明式的群组关系来源，一个群**只能属于一个配置**（否则无法确定使用哪套设置，Bot 会忽略该群）：

```json
"bot": {
  "configs": {
    "官服": {
      "listen_groups": ["111111111", "222222222"],
      "notify_group": "111111111",
      "settings": {
        "file_prefix": "MaaXXX-logs",
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
| `settings` | 该配置的分析设置覆盖（可选，未设置项继承全局 `data/settings.json`），见 [分析设置](settings.md) |
| `commands` | 该配置的指令与权限设置（可选），见 [QQ 指令](commands.md) |

修改 `config.json` 后可用 `/maa reload` 热重载，无需重启进程。

## 通信模式

通过 `onebot.mode` 选择：

| 模式 | `mode` 值 | API 通道 | 事件通道 | 适用场景 |
| --- | --- | --- | --- | --- |
| HTTP + 正向 WS | `http_ws` | HTTP POST | 正向 WS（Bot 连 OneBot） | 本地开发（默认） |
| 正向 WS Universal | `ws` | WS | WS（同一连接） | 单端口部署 |
| 反向 WS Universal | `ws_reverse` | WS | 反向 WS（OneBot 连 Bot） | 内网穿透 |
| HTTP + 反向 WS | `http_ws_reverse` | HTTP POST | 反向 WS | Bot 有公网 IP |

> 四种模式任选其一。
