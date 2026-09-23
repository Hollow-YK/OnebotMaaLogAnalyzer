# 分析设置

设置支持**全局默认 + 配置级覆盖**：全局位于 `data/settings.json`，各配置位于 `data/<配置名>/settings.json`，未覆盖项自动继承全局。也可直接在 `config.json` 的 `bot.configs.<名称>.settings` 中声明。

> 所有设置项都可通过 `/maa get` 查看、`/maa set <项> <值>` 修改（需写权限），见 [QQ 指令](commands.md)。

## 触发与反馈

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 该配置是否启用 |
| `file_prefix` | `MaaXXX-logs` | 日志包文件名前缀 |
| `send_progress_message` | `true` | 是否发送进度提示 |
| `send_summary` | `true` | 是否发送分析结果 |
| `reply_chunk_chars` | `3500` | 回复分段字符数 |
| `duplicate_window_seconds` | `120` | 同一文件去重窗口；`0` = 关闭去重 |

> **去重窗口**：部分协议端（如 SnowLuma）对一次上传会同时发出 `notice.group_upload`
> 与带 file 段的消息事件，两条入口各自触发一次分析导致重复回复。窗口内的同一文件只处理一次。

## 结果附件

见 [结果附件](attachments.md)。

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `send_images` | `true` | 是否允许模型附图 |
| `max_report_images` | `4` | 最多附带图片数 |
| `send_repo_images` | `true` | 是否允许从仓库取模板图 |
| `max_image_mb` | `5` | 单张图大小上限 MB |
| `send_files` | `true` | 是否允许附带任意文件 |
| `max_report_files` | `2` | 最多附带文件数 |
| `max_file_attachment_mb` | `20` | 单个附件文件大小上限 MB |
| `inline_attachments` | `true` | 附件是否插在文字中间（图文混排） |

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
| `max_prompt_chars` | `600000` | 发送给 AI 的摘要最大字符数 |
| `max_log_files` | `6` | 最多处理日志文件数（保底选 maafw.log / 最新日期 / 最新 agent 各 1 个） |
| `max_maafw_bak_files` | `1` | 最多处理 `maafw.bak` 日志数 |
| `max_log_chars` | `120000` | 单个日志摘要字符上限 |
| `max_recognition_evidence_chars` | `35000` | OCR 重点证据块字符上限 |
| `max_recognition_evidence_lines` | `60` | OCR 重点证据行数上限 |
| `log_head_lines` | `16` | 每个日志小节额外保留开头行数（版本、路径、控制器等环境信息） |
| `context_before_lines` | `80` | 错误命中前保留行数（按折叠后行数计） |
| `context_after_lines` | `60` | 错误命中后保留行数 |
| `max_sections_per_log` | `60` | 单个日志最多提取的小节数 |

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
| `temperature` | 空 | 覆盖全局温度，空 = 用全局 |
| `model` | 空 | 覆盖全局模型名，空 = 用全局 |
| `llm_timeout_seconds` | `900` | 单次 AI 调用超时 |
| `digest_timeout_seconds` | `0` | 摘要提取超时，`0` = 不限制 |

## 并发

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `analysis_concurrency` | `1` | 同时进行的分析数 |
| `download_concurrency` | `2` | 同时进行的下载数 |
| `download_timeout_seconds` | `120` | 下载超时秒数 |
| `file_list_delay_seconds` | `0` | 上传后等待群文件列表刷新 |

## 工具与仓库

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `log_tools_enabled` | `true` | 是否允许 AI 用工具自主读日志包原始文件（**不依赖 repo 是否启用**） |
| `repo` | 见 [项目代码参考](repo.md) | 项目代码仓库设置 |

## 其他

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `save_digest_debug` | `false` | 保存实际发送给 AI 的摘要到 `data/debug_digests` |
| `cleanup_after_done` | `true` | 分析完成后清理临时文件 |
