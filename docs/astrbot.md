# AstrBot 部署

OnebotMaaLogAnalyzer 的 AstrBot 版本是 AIOCQHTTP 插件。它与裸 OneBot 版本共用
`maa_analyzer/` 中的分析、LLM、配置、历史、追问、仓库检索和图片报告实现，
只是事件来源与消息/文件发送改由 AstrBot 适配器提供。

## 适用范围

- AstrBot `>=4.29.0,<5`
- AstrBot 的 `aiocqhttp` 平台适配器
- 协议端仍可为 NapCat、Lagrange 等 OneBot v11 实现
- 不支持 QQ 官方机器人或其他 AstrBot 平台适配器

插件安装后不需要运行 `python main.py`。AstrBot 的 AIOCQHTTP 适配器负责连接协议端，
插件只处理已唤醒的群消息和群文件上传通知。

插件日志会转发到 AstrBot 日志，并可按配置同时写入插件数据目录的 `logs/`。排查消息无响应时，将插件面板的 `log_level` 和 AstrBot 的日志级别设为 `DEBUG`，可以查看群号未匹配或管理指令未命中的原因。调试完成后可恢复为 `INFO`。

## 安装

1. 从 GitHub Release 下载 `OnebotMaaLogAnalyzer-<版本>-astrbot.zip`。
2. 把压缩包解压到 AstrBot 的 `data/plugins/`。
3. 确认目录结构为：

```text
AstrBot/data/plugins/OnebotMaaLogAnalyzer/
├── main.py
├── README.md
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── maa_analyzer/
└── templates/
```

4. 在 AstrBot 插件管理中启用插件。首次启动会创建
   `data/plugin_data/OnebotMaaLogAnalyzer/config.json` 和默认运行数据。

插件重复初始化不会重复启动后台任务；停用、AstrBot 重载或插件重载时会取消分卷计时与
后台任务、保存配置与历史、关闭 LLM/HTTP 会话和渲染浏览器。

## 配置

完整配置格式与裸 OneBot 模式相同，详见[配置](configuration.md)。AstrBot 面板映射常用项和大多数高级项；配置档案增删改排序、未被选择的 LLM Provider/Model 数组项等复杂结构仍需高级 JSON：

| 面板项 | 行为 |
| --- | --- |
| `enabled_mode` | `inherit` 继承 JSON 的 `settings.enabled`；`enabled` / `disabled` 显式覆盖 |
| `config_file` | 完整配置文件路径；相对路径从插件数据目录解析，留空使用插件数据目录下的 `config.json` |
| `data_dir` | 运行数据目录；留空继承 JSON 的 `bot.data_dir` |
| `default_config` | 面板字段写入哪个配置档案；留空使用第一个档案 |
| `listen_groups` / `notify_group` | 监听群与额外通知群 |
| `command_prefix` / `owners` / `admins` | `/maa` 前缀与权限名单 |
| `report_*` | 报告渲染模式、模板、尺寸、浏览器与渲染回退等高级项 |
| `project_name` / `archive_match_mode` / `file_prefix` | 项目显示名、日志包识别模式与前缀 |
| `require_log_layout_mode` / `ui_*_mode` | 日志布局校验与各 UI 日志包开关 |
| `repo_*` | 项目代码仓库来源、分支、检索模式、版本切换与扫描限制 |
| `llm_*` | 首选/备用候选、调用参数、端点、密钥、模型能力、全局兜底与熔断配置 |
| `vision_*` / `send_*_mode` | 看图开关、数量、细节、索图与报告图片、附件发送开关 |
| `followup_mode` / `followup_window_minutes` | 追问开关与窗口 |
| `history_mode` / `history_period_hours` / `history_keep_periods` | 历史开关、周期与保留数量 |
| `settings_*` | 分析触发、反馈、上下文、超时、压缩包与日志摘要高级设置 |
| `commands_*` | 管理指令开关、权限等级、回复分段与列表条数 |
| `log_to_file_mode` / `log_dir` / `log_level` | 文件日志开关、目录与等级 |

空字符串、空列表、数值 `-1` 和各 `*_mode: inherit` 表示继承，不会清空高级 JSON。面板值只覆盖
所选配置档案中的对应字段；未被选择的端点/模型、Provider/Model 数组顺序和其他档案保持原样；面板不会回写修改 `config.json`。

### 复用裸模式配置与数据

最稳妥的迁移方式是在面板中同时填写绝对路径：

- `config_file`: 原裸模式 `config.json` 的绝对路径
- `data_dir`: 原 `bot.data_dir` 对应的绝对路径

这样设置、历史、追问、任务记录和仓库缓存继续使用原目录，不需要搬迁。若 `config_file`
留在插件数据目录下而只写相对 `bot.data_dir`，相对路径会按插件数据目录解析，可能与裸模式
启动目录不同。

插件模式不读取 `onebot` 连接段；该段只为裸模式保留。现有 `debug` 段也不会启用插件内的
离线 HTTP/CLI 调试入口。

## AIOCQHTTP 事件

插件把以下事件转换为共享 DTO：

- 群文本消息与 `/maa` 指令
- 消息中的 `file` 段
- 引用 Bot 消息的继续追问
- `notice/group_upload` 群文件上传

发送者角色优先读取 `raw_message.sender.role`。字段缺失时调用
`get_group_member_info` 获取角色，并缓存 300 秒；插件重载时清空缓存。所有消息 ID
统一转换为字符串，用于历史归档和引用追问。

普通群聊、私聊、未监听群和未知引用不会唤醒插件。群文件通知会先交给共享服务，再按监听群、
配置启用状态、文件名和压缩包内容规则决定是否分析。

## 消息与文件限制

主动消息优先通过 `bot.send_group_msg` 发送，并从返回值提取真实 `message_id`；不可用时
回退到 `call_action`。图片以 base64 消息段发送。

群文件列表、下载 URL 和上传通过协议端 API 执行。上传优先使用临时文件路径，失败时回退
base64。不同协议端对 `upload_group_file`、异步返回值和群文件 URL 的支持并不完全一致；
若某个实现不支持对应接口，日志会记录适配器错误，分析结果仍会尽量以文本或图片发送。

## 重载与回滚

- `/maa reload` 重新读取配置并重建共享运行时。
- 新配置或新运行时启动失败时，插件尝试恢复旧运行时、旧数据目录和旧日志设置。
- 成功重载后清空群成员角色缓存，重新绑定 `/maa` 重载回调。
- 插件退出时进行幂等清理，避免旧计时器或后台任务在下次启用后继续运行。

## 故障排查

**日志提示无法导入 `maa_analyzer`**

检查插件目录是否直接包含 `maa_analyzer/`，并确认压缩包没有多套一层目录。正确路径应为
`data/plugins/OnebotMaaLogAnalyzer/maa_analyzer/__init__.py`。

**上传日志后没有触发分析**

依次检查：消息是否来自群聊；群号是否在当前启用档案的 `listen_groups`；协议端是否发送
`notice/group_upload` 或 `file` 消息段；文件名和压缩包内容是否符合项目匹配规则；日志中是否
出现“未监听”“配置未启用”或“不是日志包”等跳过原因。

**图片报告或附件发送失败**

协议端可能不支持对应的图片、群文件或 URL 接口。查看 `Maa.AstrBot` 与 `Maa.API` 日志；
文本报告通常会继续发送，文件上传失败不会回滚已经完成的分析。

**面板改了值但未生效**

确认 `default_config` 指向现有档案，面板非空值才会覆盖；配置档案管理、未被选择的 Provider/Model 数组项和数组顺序仍需编辑高级 JSON。
修改后执行 `/maa reload` 或重载插件。

**插件停用后仍有旧任务**

这是不应出现的状态。收集 AstrBot 与 `Maa.AstrBot` 日志，确认插件版本包含最新生命周期
实现，再反馈复现步骤。

## 验证边界

仓库测试覆盖 AIOCQHTTP 事件转换、角色回退、消息 ID、发送失败、文件上传回退、面板优先级、
插件重复初始化、退出清理和重载回滚。真实 AstrBot 4.29.4 与 NapCat / Lagrange 的端到端
联调尚未在本仓库中执行，因此部署验收仍需在实际 AstrBot 环境完成。
