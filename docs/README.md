# 文档

| 文档 | 内容 |
| --- | --- |
| [AstrBot 部署](astrbot.md) | AIOCQHTTP 插件安装、配置迁移、面板覆盖、重载回滚与故障排查 |
| [配置](configuration.md) | `config.json` 全部字段（按文件顺序逐项说明 + 示例）、`project` / `repo` 项目配置、多群配置、模型端点与协议、调试与日志 |
| [分析设置](settings.md) | 全部设置项（触发 / 附件 / 追问 / 历史 / 压缩包 / 并发 / 模型） |
| [上下文与提示词缓存](context.md) | 提示词前缀缓存、缓存命中率观测、上下文自动压缩的触发条件与代价 |
| [QQ 指令](commands.md) | `/maa` 指令清单与三级权限控制 |
| [结果附件与看图](attachments.md) | `[附件@...]` 统一图片与文件、`[看图@...]` 只给模型看、来源限定与敏感文件保护 |
| [图片化报告与模板](render.md) | 报告渲染成图片：`report` 设置、`section` / `page-break` 标记、分页、浏览器探测、`templates/` 编写指南与排错 |
| [追问答疑](followup.md) | 引用式追问的触发规则与会话上下文 |
| [分析历史](history.md) | 日志包归档、周期保留策略、手动管理 |
| [项目代码参考](repo.md) | 让 AI 用只读工具对照源码与全部设置项 |
| [AI 工具](tools.md) | 日志包工具与仓库工具清单、沙箱安全、调用限制 |
| [调试](debugging.md) | 离线调试模式：CLI REPL、HTTP 端点、JSON 批量测试与断言 |
| [架构](architecture.md) | 双入口分层设计、共享内核、平台适配器、目录结构与事件订阅 |
| [检查更新与自动更新](update.md) | GitHub 正式版/测试版检查、私聊通知、多 API、增量更新与崩溃安全回退 |
| [发布](releasing.md) | 自动发布流程、裸 OneBot 普通包 / 精简包、AstrBot 插件包与白名单 |
