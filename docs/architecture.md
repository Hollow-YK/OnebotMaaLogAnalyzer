# 架构

## 双入口

项目保留两种部署入口，但分析链路只有一套：

```text
裸 OneBot                          AstrBot
python main.py                     astrbot_plugin/main.py
    │                                   │
    ▼                                   ▼
OneBotPlatformAdapter              AstrBotPlatformAdapter
    │                                   │
    └──────────── PlatformPort ─────────┘
                         │
                         ▼
                 MaaWorkflowRuntime
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
maa_analyzer/core  maa_analyzer/features/maa  LLM / history / render
```

- **裸 OneBot**：根 `main.py` 是稳定引导器，负责选择 `current.json` 指向的不可变版本并监督启动；
  实际应用入口 `maa_analyzer/bare_entry.py` 读取 `config.json`、建立 OneBot HTTP/WS 连接并处理四种通信模式。
- **AstrBot**：`astrbot_plugin/main.py` 是 AIOCQHTTP 插件壳，由 AstrBot 提供事件与协议 API；
  CI 包的根 `main.py` 是稳定 shim，真实插件壳为 `plugin_shell.py`。

两个入口都通过 `MaaWorkflowRuntime` 装配相同的服务、LLM、历史、追问、仓库、附件和报告组件。
插件模式不需要另起裸 OneBot 进程。

## 分层与导入边界

```text
入口 / 平台适配器
        │  PlatformEvent + PlatformPort
        ▼
maa_analyzer/runtime.py       共享运行时装配
        │
        ├── maa_analyzer/core/       配置、数据、模型、LLM、token、视觉
        └── maa_analyzer/features/   日志分析、指令、追问、历史、渲染、仓库
```

约束：

- 共享内核只能依赖 `maa_analyzer.platform` 中的 DTO 与 `PlatformPort`，不得 import
  `OneBotPlatformAdapter`、`AstrBotPlatformAdapter`、AstrBot `event/Context` 或 `aiocqhttp`。
- 平台适配器负责把原生事件转换为 `GroupMessageEvent` / `GroupUploadNotice`，并把发送、
  群文件和下载操作转换为 `PlatformPort` 方法。
- `MaaService` 通过注入的端口发送消息和访问群文件，不持有具体 Bot API。
- 所有消息发送结果统一为 `message_id: str | None`；文件上传结果为 `bool`。

## 目录结构

```text
OnebotMaaLogAnalyzer/
├── main.py                         # 稳定引导器（选择 current.json 指向的版本）
├── maa_bootstrap.py                # 引导器/AstrBot shim 的选择与回退逻辑
├── astrbot_plugin/                 # AstrBot 插件壳
│   ├── main.py                     # Star 生命周期、事件过滤器、AIOCQHTTP 监听
│   ├── _launcher.py                # CI 包中作为根 main.py 的稳定 shim
│   ├── metadata.yaml
│   ├── _conf_schema.json           # 面板配置模式
│   └── requirements.txt
├── maa_analyzer/                   # 两个入口共享的唯一内核命名空间
│   ├── platform.py                 # PlatformPort、事件 DTO、发送者与文件元数据
│   ├── runtime.py                  # 共享运行时生命周期
│   ├── config/                     # 配置加载、校验、面板适配、日志
│   │   ├── panel_schema.py         # Pydantic 面板分组与扁平化
│   │   ├── panel_fields.py         # 面板字段到 config.json 的映射
│   │   ├── panel.py                # 面板覆盖应用
│   │   ├── defaults.py             # 首次运行默认配置
│   │   ├── loader.py               # 加载、校验与生效配置
│   │   └── logging_setup.py        # 两种入口共用的日志初始化
│   ├── astrbot/
│   │   └── plugin_runtime.py       # 与 AstrBot 框架解耦的插件生命周期桥
│   ├── platforms/
│   │   ├── onebot/                 # HTTP/WS、适配器、事件桥
│   │   └── astrbot/                # AIOCQHTTP 事件与 API 适配器
│   ├── core/                       # models、service、data、LLM 等基础设施
│   ├── features/maa/               # 分析、指令、历史、追问、渲染、仓库等业务
│   └── update/                     # GitHub 检查、manifest 差异、增量安装与启动确认
├── templates/                      # 图片报告模板，两个发布形态共用
├── debug/                          # 源码调试工具，不进入发布包
├── docs/
└── tools/                          # 编辑器工具，只进入裸模式普通包
```

`config.json` 不在源码仓库中提交，首次运行自动生成。运行时数据默认位于 `data/`；
AstrBot 默认使用 `data/plugin_data/OnebotMaaLogAnalyzer/`。

## 平台端口

`PlatformPort` 提供：

- 文本、消息段、图片和文件发送
- 群文件根目录/子目录列表、下载 URL 和上传
- 任意 URL 下载
- 协议端版本与群成员信息查询
- 运行时关闭/会话清理

共享 DTO 覆盖群上传通知、群消息、文件段、引用目标、发送者群角色和文件元数据。DTO 的
`to_service_payload()` 生成现有业务层可读的兼容结构，因此迁移不需要改写历史文件格式。

## 事件流转

| 平台事件 | 适配器输出 | 共享服务路由 |
| --- | --- | --- |
| OneBot `message/group` | `GroupMessageEvent` | `message.file` 或 `message.text` |
| OneBot `notice/group_upload` | `GroupUploadNotice` | `notice.group_upload` |
| AIOCQHTTP 群文本 | `GroupMessageEvent` | 指令或引用追问 |
| AIOCQHTTP 文件段 | `GroupMessageEvent` + 文件元数据 | 日志包分析 |
| AIOCQHTTP 群上传通知 | `GroupUploadNotice` | 日志包分析 |

AstrBot 在监听器执行前会先判断事件是否值得唤醒。`MaaEventFilter` 同步解析群号、命令、
文件名和引用消息，只让当前插件可能消费的事件通过；实际的监听群、启用状态和日志内容校验
仍由共享服务完成。

## 生命周期

`MaaWorkflowRuntime.start()` 幂等创建数据目录、LLM、服务、历史、分析器、追问存储、
监听器、更新管理器和后台任务。`stop()` 会：

1. 取消更新检查、待处理的分卷计时和后台任务；
2. 保存服务配置、历史与任务状态；
3. 关闭运行时拥有的 LLM/HTTP 会话；
4. 关闭渲染浏览器；
5. 在入口拥有端口时关闭平台适配器。

AstrBot 插件额外负责隔离日志句柄、角色缓存和配置重载回滚。插件停用后再次启用会创建新的
运行时，不复用已取消的任务集合。

更新子系统与常规分析运行时不同：它面向整个进程和安装目录，而不是单个配置。检查循环由
`MaaWorkflowRuntime` 启停，但真正的文件安装使用不可变版本目录、原子 `current.json`
指针和独立引导器，避免更新过程中破坏正在运行的版本。

## 并发模型

一句话：**配置之间隔离，配置之内排队。**

| 维度 | 规则 | 实现 |
| --- | --- | --- |
| 同一配置 | 分析、追问、仓库操作**排队串行**，不并行 | 每个配置一把工作锁（`MaaAnalyzer.config_work_lock`），分析入口与 `message_handler` 的追问路径共用 |
| 不同配置 | **互不影响**，可同时跑 | 信号量、仓库 provider、分析缓存、追问策略都按配置名隔离；没有跨配置共享的可变运行时状态 |
| 渲染 | 按 `data_dir` 排队串行 | `maa_analyzer/features/maa/render`：进线程前先在渲染器队列排队，同步侧用 `data_dir` 键的线程锁包住渲染 / 重启 / 关闭 |
| 下载 | 同一次分析内按 `download_concurrency` 并行 | 按配置维护的下载信号量（分卷并行下载） |
| 阻塞 I/O | 下沉到线程，不占事件循环 | `maa_analyzer.core.concurrency.run_blocking`：日志包解压/扫描、仓库遍历/读文件、历史归档与清理、配置写盘 |

因此「同一配置下并发分析多份日志」不是受支持的行为，也不需要
`analysis_concurrency` 这类设置：需要更高吞吐就拆成多个配置，每个配置
各自排队。反过来，一个配置里解压大日志包或遍历大仓库时，也不会卡住
其它配置——这些操作在线程里跑。

进度与日志按配置分组（`config_log_context`），任务记录（`/maa jobs`）
也按配置隔离，便于区分哪个配置在排队、哪个在跑。

## 数据兼容

JSON 数据文件沿用原格式，并做以下保护：

```text
data/
├── settings.json
├── ai_usage.jsonl
├── repo/
├── updates/                    # 更新状态、结果与下载日志
└── <配置名>/
    ├── groups.json
    ├── settings.json
    ├── project.json
    ├── commands.json
    ├── jobs/records.json
    └── history/
```

- **原子写入**：先写 `.tmp`，再用 `os.replace` 覆盖（不再 `unlink` + `rename`，
  避免 Windows 上出现"文件不存在"的窗口）；`os.replace` 失败时原文件保持不动。
- **`.tmp` 恢复**：加载失败时自动尝试用残留的 `.tmp` 顶替。
- **损坏文件先备份**：`settings.json` / `<配置>/groups.json` / `commands.json` /
  `settings.json` 覆盖 / `project.json` / `jobs/records.json` 解析失败时，先备份成
  `<文件名>.corrupt-<时间戳>` 再按默认值重建；**备份也失败则进入写保护**，
  宁可用默认值跑也不覆盖唯一一份数据。
- **配置名清洗**：配置名只允许字母数字与 `._-`（中文等 Unicode 字母数字保留），
  路径分隔符、`..`、绝对路径都会被替换，目录恒定落在 `data/<清洗后的名字>/`；
  升级前已存在的老目录名会继续沿用，避免"历史不见了"。

`config.json` 的 `bot.configs`、`llm`、`log` 等高级字段在两种入口中保持同一语义；
`onebot` 段只由裸入口消费。AstrBot 面板覆盖只在内存中修改有效配置，不回写原始文件。

## 真实联调边界

自动化测试覆盖端口转换、配置优先级、生命周期、发送/上传回退和裸 OneBot 四种通信模式的
配置兼容。真实 AstrBot 4.29.4 与 NapCat / Lagrange 的端到端安装、上传、分卷汇总、追问、
附件、历史清理和热重载仍需在部署环境联调。
