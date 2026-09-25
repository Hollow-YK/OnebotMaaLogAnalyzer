# 调试模式

设置 `debug.enabled: true` 后启动 Bot，将进入**离线调试模式**——不连接真实 OneBot 服务端，通过模拟事件注入来验证代码逻辑。

> 调试模块仅存在于源码仓库，不随发布包分发。若发布包中启用了 `debug.enabled`，Bot 会提示并退出。

## CLI 交互式 REPL

当前终端直接操作：

```text
debug> upload group=123456 user=10001 name=MaaXXX-logs-20260101.zip size=2048
debug> msg group=123456 user=10001 text="/maa help"
debug> files group=123456 set MaaXXX-logs-a.zip:fa:2048
debug> configs                                        # 查看已加载配置
debug> run debug/examples/smoke_test.json             # 运行批量测试
debug> help                                           # 查看所有命令
debug> quit                                           # 退出
```

| 命令 | 说明 |
| --- | --- |
| `upload` | 模拟群文件上传通知 |
| `msgfile` | 模拟消息内的文件段 |
| `msg` | 模拟普通文本消息（指令 / 追问） |
| `notice` | 模拟任意通知事件 |
| `request` | 模拟请求事件 |
| `files` | 预置群文件列表 |
| `configs` | 查看已加载配置 |
| `run` | 运行 JSON 测试文件 |
| `history` | 查看最近事件 |
| `clear` / `quit` | 清屏 / 退出 |

> 调试模式下 `download_url` 会自动生成一个最小 MaaXXX 日志包，
> 因此无需准备真实 zip 即可跑通「下载 → 摘要 → AI」完整链路（AI 输出为模拟内容）。

## HTTP 调试端点

监听 `127.0.0.1:8765`，适合脚本与集成测试：

```bash
# 注入群文件上传
curl -X POST http://127.0.0.1:8765/debug/upload \
  -H "Content-Type: application/json" \
  -d '{"group_id":123456,"user_id":10001,"file_name":"MaaXXX-logs-a.zip","size":2048}'

# 预置群文件列表
curl -X POST http://127.0.0.1:8765/debug/files \
  -H "Content-Type: application/json" \
  -d '{"group_id":123456,"files":[{"file_name":"MaaXXX-logs-a.zip","file_id":"fa","size":2048}]}'

# 查看已加载配置
curl http://127.0.0.1:8765/debug/configs
```

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/debug/event` | POST | 注入原始 OneBot 事件 JSON |
| `/debug/upload` | POST | 便捷群文件上传注入 |
| `/debug/message` | POST | 便捷普通消息注入 |
| `/debug/files` | POST | 预置群文件列表（供群文件接口返回） |
| `/debug/configs` | GET | 查看已加载配置 |
| `/debug/run` | POST | 运行 JSON 测试文件 |
| `/debug/health` | GET | 健康检查 |

> `debug.enabled: false`（默认）时调试接口完全不启动，Bot 正常工作。

## JSON 批量测试

测试文件位于 `debug/examples/`，格式示例：

```json
{
  "name": "冒烟测试 — 只响应日志包",
  "setup": {
    "configs": {"测试组": {"listen_groups": ["123456"], "notify_group": "123456"}},
    "files": {"123456": [{"file_name": "MaaXXX-logs-a.zip", "file_id": "fid_a", "size": 2048}]}
  },
  "scenarios": [
    {
      "name": "上传日志包 — 触发自动分析",
      "event": {"post_type": "notice", "notice_type": "group_upload", "group_id": 123456,
                "user_id": 10001, "file": {"id": "fid_a", "name": "MaaXXX-logs-a.zip", "size": 2048}},
      "assert": {"reply_contains": "MaaXXX 日志分析结果", "no_error": true}
    },
    {
      "name": "发送无关文本 — 不响应",
      "event": {"post_type": "message", "message_type": "group", "group_id": 123456,
                "user_id": 10001, "raw_message": "/help"},
      "assert": {"api_count": 0, "no_error": true}
    }
  ]
}
```

### 运行全部套件

```bash
python -c "import asyncio;from debug.runner import run_test_file;from debug import DebugManager;cfg={'bot':{'data_dir':'data/test','configs':{}}};[print(asyncio.run(run_test_file(f'debug/examples/{n}.json',DebugManager.from_config(cfg)))) for n in ('smoke_test','analyze_flow','repo_lookup','commands_test','history_test','attachment_safety','followup_restart','followup_repo','followup_repo_attach','followup_tool_loop','followup_converge_fallback')]"
```

> 每次运行前先删除 `data/test`，否则残留配置会导致群多归属 → `resolve_config` 返回 `None` 而误判失败。

### 内置套件

| 文件 | 覆盖范围 |
| --- | --- |
| `smoke_test.json` | 日志包与文本消息边界 |
| `analyze_flow.json` | 端到端流程与边界情况 |
| `repo_lookup.json` | 项目代码参考（inject / agent） |
| `commands_test.json` | 指令权限 / 附图 / 追问答疑 |
| `history_test.json` | 历史归档 / 周期清理 / 跨天追问 |
| `attachment_safety.json` | 附件来源 / 相对路径 / 敏感文件防护 |
| `followup_restart.json` | **重启后继续追问**（真实子进程重启） |
| `followup_repo.json` | 重启后 agent 模式仓库工具仍可用 |
| `followup_repo_attach.json` | 重启后 `@项目` 附件仍可取到 |
| `followup_tool_loop.json` | 模型只检索不输出正文时仍须给出回答 |
| `followup_converge_fallback.json` | 模型连收敛都不配合时的兜底回复 |

### 真实重启测试

场景加 `"restart": true` 后，该场景会在**全新子进程**里执行：

```text
父进程 ──JSON任务──▶ python -m debug.worker ──重建整条管线──▶ 注入事件
                                                    │
                                              仅磁盘数据延续
```

子进程会完整走一遍启动流程（`service.load()`、配置重读、历史重载），
等价于 kill 后重新 `python main.py`，而不是在同一进程里清几个字典 ——
因此能捕获任何依赖进程全局状态的缺陷。随后父进程也就地重建，
使后续场景看到重启后的真实状态。

> 调试 API 的 `message_id` 是本地计数器，重启时父进程会把计数续给子进程，
> 避免新进程从 `100000` 重新发号、与重启前的 ID 撞号而**假通过**。
> 真实 QQ 的 `message_id` 由服务端全局发号，不存在这个问题。

### setup 字段

| 字段 | 说明 |
| --- | --- |
| `configs` | 声明配置与监听群（可直接含 `commands`） |
| `files` | 预置群文件列表 |
| `mock_repo` | 物化模拟仓库（`{相对路径: 内容}`），自动注入该配置的 `repo.path` |
| `create_files` | 创建 sentinel 文件（验证仓库缓存未被清理） |
| `history` | 播种历史记录（`reset` 清空、每条的 `age_hours` 构造过期记录） |
| `history_cleanup` | 播种后立即执行一次清理 |
| `model_report` | 覆盖模拟模型的报告（测试特定附件指令 / 敏感文件请求） |
| `llm_tool_behavior` | `normal`（首轮调工具后收敛）/ `always`（每轮都调直到上限） |
| `llm_converge_empty` | 收敛调用返回空正文（模拟模型不配合收敛） |

### 场景字段

| 字段 | 说明 |
| --- | --- |
| `event` | 要注入的事件 |
| `assert` | 断言集合 |
| `capture` | 捕获变量供后续场景引用（如 `{"analysis_msg": "LAST_BOT_MSG"}`） |
| `restart` | **在全新子进程中执行本场景**（真实重启，验证落盘状态） |
| `llm` | 本场景覆盖模型行为（`tool_behavior` / `converge_empty` / `report`） |
| `preserve_followup` | 跨场景保留追问会话 |
| `preserve_dedup` | 跨场景保留去重状态 |
| `history_cleanup` | 注入事件前先清理历史 |

### 占位符

- `$LAST_BOT_MSG` — 最近一次 Bot 消息 ID（**event 与 assert 中均可用**）
- `$name` — 由 `capture` 写入的具名变量

> **坑**：追问回复也会更新 `last_message_id`，导致后续场景的 `$LAST_BOT_MSG` 漂移（引用到追问回复而非分析消息）。
> 解决：场景加 `"capture": {"analysis_msg": "LAST_BOT_MSG"}` 固定 ID，后续用 `$analysis_msg` 引用。

### 支持的断言

| 断言 | 说明 |
| --- | --- |
| `reply_contains` / `reply_not_contains` | 回复包含 / 不包含指定文本（支持字符串或数组） |
| `api_count` | API 调用数量 `==` / `>=` / `<=` 指定值 |
| `api_actions_include` / `api_actions_exclude` | API 调用中包含 / 不包含指定 action |
| `api_segments_include` / `api_segments_exclude` | 消息段类型包含 / 不包含指定值（如 `image`） |
| `prompt_contains` / `prompt_not_contains` | **用户提示词**（注入内容）包含 / 不包含 |
| `system_prompt_contains` / `system_prompt_not_contains` | **系统提示词**（固定人设）包含 / 不包含 |
| `tools_include` / `tools_exclude` | 向模型提供的工具名包含 / 不包含（如 `search_repo`） |
| `tool_results_contains` | 工具**执行结果**中包含指定文本（证明工具真读到了内容） |
| `no_error` | 无异常（`true` / `false`） |
| `history_count` / `history_zips` / `history_no_zips` / `history_message_ids` | 历史归档状态 |
| `files_exist` / `files_missing` | 相对 `data_dir` 的文件存在性（验证 `repos/` 缓存未被清理） |

> `tools_include` 只能证明工具**被提供**，`tool_results_contains` 才能证明工具
> **真的读到了内容**（例如仓库工具确实读到了 `assets/.../PVP.json`）。
