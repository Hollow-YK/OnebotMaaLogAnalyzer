# 分析设置

设置支持**全局默认 + 配置级覆盖**：全局位于 `data/settings.json`，各配置位于 `data/<配置名>/settings.json`，未覆盖项自动继承全局。也可直接在 `config.json` 的 `bot.configs.<名称>.settings` 中声明。

> 所有设置项都可通过 `/maa get` 查看、`/maa set <项> <值>` 修改（需写权限），见 [QQ 指令](commands.md)。

## 优先级与删除语义

生效值按以下顺序叠加（后者覆盖前者）：

1. `data/settings.json`（全局默认）
2. `config.json` → `bot.configs.<名称>.settings`（声明式基线，每次启动/重载重新读取）
3. `/maa set`（显式运行时覆盖，持久化到 `data/<名称>/settings.json`）

因此：

- 从 `config.json` **删掉**某个键 → 回退到 `/maa set` 的值；没有则回退全局
- `/maa set` 的值始终生效，并在重启 / `/maa reload` 后保留
- 旧版本留下的 `data/<名称>/settings.json`（全量 dump）会在首次启动时自动转换为增量格式（`{"version": 2, "overrides": {...}}`）

> **项目相关设置已全部搬到 `project` 段**：`name`、日志包识别
> （`archive_match_mode` / `file_prefix` / `require_log_layout`）、
> `ui.<名称>.enabled`、`repo` 都只写在
> `bot.configs.<名称>.project`，不再出现在 `settings` 中，
> 也不与 `settings` 叠加。见 [配置 · project](configuration.md#project项目配置)。

## 触发与反馈

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 该配置是否启用 |
| `file_list_delay_seconds` | `0` | 上传后等待群文件列表刷新的秒数 |
| `send_progress_message` | `true` | 是否发送进度提示 |
| `send_summary` | `true` | 是否发送分析结果 |
| `reply_chunk_chars` | `3500` | 回复分段字符数 |
| `duplicate_window_seconds` | `120` | 同一文件 / 同一组归档的去重与冷却窗口；`0` = 关闭去重 |

> **去重窗口**：部分协议端（如 SnowLuma）对一次上传会同时发出 `notice.group_upload`
> 与带 file 段的消息事件，两条入口各自触发一次分析导致重复回复。窗口内的同一文件只处理一次。
>
> 分析耗时可能超过窗口：同一归档**分析进行中**时到达的分卷 / 重复事件会直接忽略，
> 且窗口在**分析结束时重新计时**，因此延迟到达的重复通知不会触发第二轮分析。

### 分卷日志包的处理顺序

- **非分卷**（`xxx.zip`）：匹配到后立即下载分析
- **分卷**（`xxx-part01.zip`、`xxx-part02.zip`…）：先登记并**重新计时**，
  连续 `archive_part_settle_seconds` 秒没有新分卷后才开始分析一次
- 分卷静默期内到达的新分卷会取消上一次计时，因此上传顺序、间隔都不影响合并结果
- 同一分卷经 `notice.group_upload` 与 `message.file` 重复上报不会重置静默计时
- 分析进行中到达的分卷会被忽略；分析结束后的 `duplicate_window_seconds` 内不再重复触发

## 日志包命名与 UI 自动识别

日志包是否分析由 `project.archive_match_mode` 与 `project.ui.<名称>.enabled` 决定，
详见 [配置 · 识别方式](configuration.md#识别方式archive_match_mode)。已知命名如下：

| UI | 文件名示例 | 压缩包布局 |
| --- | --- | --- |
| MXU | `MaaAssistantKedrgame-logs-1.0.0-alpha.4-20260814-135755.zip` | 根目录 `maafw.log`、`on_error/`、`config/mxu-*.json` |
| MFAAvalonia | `log_20261019_110324.zip`、`log_20261019_141709-part01.zip` | `debug/maafw.log`、`logs/log-*.log`、`debug/on_error/` |
| MaaFwApp | `maafw_logs_20261022_195657.zip` | `log/maafw.log`、`log/app.log`、`device_info.txt`、`pi/debug/maafw.log`、`log/run/*.jsonl` |

Bot 会从文件名、`maafw.log` 头部、MFAA 的 `logs/log-*.log`、MaaFwApp 的 `device_info.txt`
自动解析 **UI 类型、软件版本、资源版本、MaaFramework 版本、设备与系统**，写入摘要和报告头。

- MXU 的版本号来自文件名（`-logs-<版本>-<时间戳>`），属于项目/资源版本
- MFAAvalonia 从 `程序版本`、`资源版本`、`MaaFramework 版本` 解析
- MaaFwApp 从 `device_info.txt` 的 `Version` / `MaaFwApp` / `Framework` 解析
- MFAA 的 `资源版本` 也会用于自动对齐项目仓库 tag

### 内容特征识别

下载后会检查压缩包内容：

| 命中条件 | 结果 |
| --- | --- |
| 已知 UI 布局（如 `debug/maafw.log`、`device_info.txt`） | 正常分析 |
| 含 `.log` / `.jsonl` 文件 | 正常分析 |
| 含 `on_error/` 或 `debug/custom/` 截图 | 正常分析 |
| 以上都没有（即使文件名像日志） | 跳过，不调用 AI |

该检查在 `auto` / `known` / `any` 模式下**始终执行**；`prefix` 模式下由
`project.require_log_layout` 控制（默认 `true`，设为 `false` 则只信前缀）。

> 仅靠**文件名**推断出的 UI **不算**内容命中。因此 `server-logs-20260101.zip`
> 这类同名但内容是普通资源的压缩包不会被误分析。
>
> 该检查只影响「是否分析」，不影响已确认日志包的 UI/版本解析。

## 结果附件

见 [结果附件](attachments.md)。

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `send_images` | `true` | 是否允许模型附图 |
| `max_report_images` | `4` | 最多附带图片数 |
| `send_repo_images` | `true` | 是否允许从仓库取模板图 |
| `send_files` | `true` | 是否允许附带任意文件 |
| `max_report_files` | `2` | 最多附带文件数 |
| `max_file_attachment_mb` | `20` | 单个**非图片**附件大小上限 MB |
| `inline_attachments` | `true` | 附件是否插在文字中间（旧配置兼容项） |

> 图片没有单张体积上限：渲染时按正文宽度等比缩放。呈现方式（内联 / 单独发送、
> 是否额外上传 `.md`）由 `bot.configs.<名称>.report` 控制，见
> [图片化报告与模板](render.md)。

## 追问答疑

见 [追问答疑](followup.md)。

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `followup_enabled` | `true` | 是否允许追问答疑 |
| `followup_window_minutes` | `0` | 追问会话有效分钟数（`0` = 跟随历史保留期） |
| `followup_max_turns` | `10` | 单会话最多追问轮数 |
| `followup_max_sessions` | `50` | 最多保留会话数 |

## 分析历史

见 [分析历史](history.md)。

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `history_enabled` | `true` | 是否归档分析历史（日志包 + 追问上下文） |
| `history_period_hours` | `24` | 历史保留周期（小时） |
| `history_keep_periods` | `2` | 保留周期数，超出即删除 |
| `history_max_records` | `200` | 历史记录条数上限 |
| `history_max_total_mb` | `4096` | 历史目录占用上限 MB |

## 摘要提取

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `max_zip_size_mb` | `100` | zip 下载大小上限 MB |
| `max_total_uncompressed_mb` | `120` | 解压总大小上限 MB |
| `max_zip_members` | `300` | 压缩包内成员数上限 |
| `max_prompt_chars` | `200000` | 摘要字符上限（作为 token 预算的兜底；按优先级裁剪，不会先丢关键证据） |
| `max_log_files` | `6` | 最多处理日志文件数（按 UI 布局优先选择框架日志 / UI 日志 / 运行记录） |
| `max_maafw_bak_files` | `1` | 最多纳入摘要的 `maafw.bak.*` 轮转备份日志数；`0` = 不纳入 |
| `max_maafw_bak_mb` | `64` | 单个 `maafw.bak` 大小上限 MB；`0` = 不限制 |
| `merge_archive_parts` | `true` | 自动合并 `*-partNN.zip` 分卷日志包 |
| `archive_part_settle_seconds` | `30` | 分卷静默期：每来一个新分卷重新计时，连续该秒数无新分卷后才开始分析；`0` = 收到首个分卷立即分析 |
| `max_log_chars` | `120000` | 单个日志摘要字符上限 |
| `max_recognition_evidence_chars` | `35000` | OCR 重点证据块字符上限 |
| `max_recognition_evidence_lines` | `60` | OCR 重点证据行数上限 |
| `log_head_lines` | `16` | 每个日志小节额外保留开头行数（版本、路径、控制器等环境信息） |
| `context_before_lines` | `80` | 错误命中前保留行数（按折叠后行数计） |
| `context_after_lines` | `60` | 错误命中后保留行数 |
| `max_sections_per_log` | `60` | 单个日志最多提取的小节数 |

### `maafw.bak` 是什么

MaaFramework 的日志按大小轮转：写满当前 `maafw.log` 后把它改名成
`maafw.bak.<时间戳>.log`，再新开一个 `maafw.log`。所以**最近的错误往往落在
备份文件里**（当前 `maafw.log` 只保留了崩溃之后的那一小段）。

- `max_maafw_bak_files`：把最新的几个备份纳入摘要，默认 `1`（只取最近一个）
- `max_maafw_bak_mb`：单个备份的大小上限，默认 `64` MB；`0` 表示不限制
- 备份按时间戳倒序挑选，超过上限的直接跳过，避免为了几百 MB 的备份拖慢分析

> 若日志包里的 `maafw.bak` 很多且单个体积很大，调小 `max_maafw_bak_mb`
> 比调小 `max_maafw_bak_files` 更划算：前者只丢掉过大的那个文件，
> 后者会丢掉「最近的备份」这一最有价值的证据。

## 大日志流式扫描

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `small_log_full_read_mb` | `5` | 小于该值完整读取 MB |
| `max_log_file_read_kb` | `512` | 普通模式单文件读取上限 KB |
| `stream_medium_logs` | `true` | 流式扫描中等日志 |
| `large_log_threshold_mb` | `50` | 巨型日志判定阈值 MB |
| `max_large_log_read_kb` | `256` | 仅前缀模式读取上限 KB |
| `stream_large_logs` | `true` | 流式深扫巨型日志 |
| `max_stream_log_mb` | `0` | 单个巨型日志流式扫描上限 MB，`0` = 不限制 |
| `compress_context_noise` | `true` | 折叠低价值 TRACE/DEBUG 行 |

> **流式扫描默认开启**：关闭后巨型日志仅读前缀，会**完全丢失**后段的错误证据。
> 实测 11.81 MB `maafw.log`：小日志完整读 8.4s / 流式中等 5.1s / 流式巨型 4.9s / 仅前缀 0.3s。

## 模型调用

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `system_prompt` | 空 | 自定义系统提示词；空 = 按项目名生成默认人设 |
| `ai_stage_timeout_seconds` | `300` | 整个 AI 阶段（含工具循环与收敛）的总时长上限 |
| `max_context_tokens` | `0` | 上下文 token 预算；`0` = 用 `llm.defaults.max_context_tokens` |
| `digest_timeout_seconds` | `0` | 摘要提取超时，`0` = 不限制 |
| `analysis_cache_hours` | `0` | 分析结果缓存小时数（进程内，按 file_id 命中，重载即清空）；`0` = 关闭 |
| `circuit_breaker_enabled` | `true` | 连续失败时熔断端点并降级为模板报告 |

> 模型选择与温度已下移到 `bot.configs.<名称>.llm`，见
> [配置 · LLM](configuration.md#llm端点与候选模型)。

## 并发

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `analysis_concurrency` | `2` | 同时进行的 AI 分析数（仓库状态相关的配置仍按配置串行） |
| `download_concurrency` | `2` | 同时进行的下载数 |
| `download_timeout_seconds` | `120` | 下载超时秒数 |

## 日志包工具

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `log_tools_enabled` | `true` | 是否允许 AI 用工具自主读日志包原始文件（**不依赖 repo 是否启用**） |

> 项目代码仓库不在这里配置：见 [项目代码参考](repo.md) 的 `project.repo`。

## 其他

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `save_digest_debug` | `false` | 保存实际发送给 AI 的摘要到 `data/debug_digests` |
| `cleanup_after_done` | `true` | 分析完成后清理临时文件 |
