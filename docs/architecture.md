# 架构

## 分层

```text
功能模块（features/）    ← 只依赖 service，不直接引用 bot/
        │
        ▼
core/service.py         ← Bot 能力 API + 配置管理 + 事件分发
        │
        ▼
bot/ (api.py, client.py) ← OneBot v11 协议封装（对功能模块透明）
```

分层严格：`core` 不 import `features`；`features` 不 import `bot`。

功能模块通过 `service.register_event()` 订阅事件，无需修改框架代码。`MaaService` 提供统一的 `send_message()` / `send_image()` / 群文件查询 / `download_url()` 等语义化 API 封装底层协议细节。

## 目录结构

```text
OnebotMaaLogAnalyzer/
├── main.py              # 入口（asyncio）+ 模块装配注册
├── config.json          # 配置文件（首次运行自动生成，含密钥不提交）
├── requirements.txt     # Python 依赖
├── README.md            # 项目说明
├── docs/                # 详细文档（本目录）
├── .github/             # CI 配置
│   ├── cliff.toml       #   git-cliff 变更日志配置
│   └── workflows/
│       └── release.yml  #   打 tag 自动发布
├── bot/                 # [框架] 通信层 — 协议实现，功能模块不可见
│   ├── api.py           #   HTTP/WS API 封装（含文件下载）
│   ├── client.py        #   WebSocket 客户端/服务端
│   └── handler.py       #   原始事件 → service 桥接
├── core/                # [框架] 基础设施 — 功能模块的唯一依赖入口
│   ├── models.py        #   Pydantic 数据模型（配置 / 设置 / 任务记录）
│   ├── data_manager.py  #   JSON 持久化（多配置 + .tmp 原子写）
│   ├── llm.py           #   OpenAI 兼容 LLM 客户端
│   └── service.py       #   Bot 能力 API + 配置管理 + 事件分发
├── features/            # [功能] 所有业务功能
│   └── maa/             #   [日志分析]
│       ├── watcher.py          # 日志包监听（主要触发入口）
│       ├── analyzer.py         # 下载 → 摘要 → 仓库检索 → LLM → 报告 流程编排
│       ├── log_digest.py       # 日志摘要提取（核心算法）
│       ├── repo.py             # 项目代码检索（仓库/版本/tag 管理）
│       ├── repo_tools.py       # agent 模式的仓库工具集（含路径沙箱）
│       ├── log_tools.py        # 日志包工具（AI 自主读压缩包内原始文件）
│       ├── attachments.py      # 结果附图/附件（来源限定 + 相对路径沙箱 + 图文混排）
│       ├── safety.py           # 路径安全策略（凭据/密钥类文件禁止外发）
│       ├── followup.py         # 追问答疑会话管理（内存 + 历史回退）
│       ├── history.py          # 分析历史归档（日志包 / 消息 ID / 周期清理）
│       ├── commands.py         # QQ 指令解析与执行（含权限）
│       ├── message_handler.py  # 文本消息路由（指令 / 追问）
│       ├── onebot_files.py     # 群文件递归列出 / 查找 / 下载
│       ├── prompts.py          # 日志诊断提示词
│       └── text_utils.py       # 字节格式化、文本分段
├── debug/               # 调试工具（模拟事件注入，不随发布包分发）
│   ├── __init__.py      #   DebugAPI + DebugManager 核心引擎
│   ├── cli.py           #   CLI 交互式 REPL
│   ├── http_server.py   #   HTTP 调试端点
│   ├── runner.py        #   JSON 批量测试运行器
│   └── examples/        #   测试套件
├── data/                # 运行时数据
│   ├── settings.json    # 全局分析设置（默认值）
│   ├── repos/           # 克隆的仓库缓存（按 url+branch 哈希命名，**永不清理**）
│   └── <配置名>/         # 各配置独立目录
│       ├── groups.json       # 监听群 + 通知群
│       ├── settings.json     # 分析设置覆盖
│       ├── commands.json     # 指令与权限设置
│       ├── history/          # 分析历史（日志包 + 追问上下文，按周期自动清理）
│       └── jobs/records.json # 分析任务记录
└── logs/                # 日志文件（时间命名）
```

## 事件订阅

| 事件 | 监听器 | 用途 |
| --- | --- | --- |
| `notice.group_upload` | `LogWatcher.on_group_upload` | 群文件上传自动分析（主要触发路径） |
| `message.file` | `LogWatcher.on_message_file` | 消息中的文件段（部分实现以此上报） |
| `message.text` | `MessageHandler.on_message_text` | 文本消息 → `/maa` 指令 / 引用式追问 |

## 数据兼容

采用 `.tmp` 原子写入防止数据损坏，加载失败时自动尝试 `.tmp` 恢复。
