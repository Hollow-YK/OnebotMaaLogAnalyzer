# OnebotMaaLogAnalyzer

基于 **[OneBot v11 标准协议](https://github.com/botuniverse/onebot-11)** 的 MaaXXX 日志分析 Bot。纯 Python 实现，不依赖任何 Bot 框架。

监听群内上传的日志压缩包，自动下载并提取错误片段、配置摘要与错误截图列表，再用专用的日志诊断提示词调用 AI 分析，把结论与相关图片发回群内。

支持 **MXU**（`*-logs-<版本>-<时间戳>.zip`）、**MFAAvalonia**（`log_<时间戳>.zip`）、**MaaFwApp**（`maafw_logs_<时间戳>.zip`）等 UI 的日志包，并自动解析 UI 类型、软件版本、资源版本与 MaaFramework 版本；分卷包 `*-partNN.zip` 会自动合并分析。

分析完成后，群内可**引用分析结果继续追问**，也可用 **`/maa` 指令**查看与修改配置。

## 功能特性

- **自动分析** — 匹配到日志包立即下载分析，无需任何人操作
- **多 UI 适配** — 自动识别 MXU / MFAAvalonia / MaaFwApp 日志包布局与命名，解析 UI 类型、软件版本、资源版本、MaaFramework 版本；**无需配置文件名前缀**，下载后校验布局避免误判；自动合并分卷包
- **智能摘要** — 按 UI 布局选择框架日志 / UI 日志 / 运行记录，提取错误/警告/失败/异常/OCR 识别失败片段与配置摘要；大日志流式扫描，按签名去重只保留关键片段
- **AI 可读原始文件** — 摘要之外，AI 可用工具自主检索**日志压缩包**与**项目代码仓库**内的原始内容（只读、不落盘）
- **图片化报告** — 报告正文、截图与文件卡片由无头浏览器渲染成图片发送，取消单条 3500 字限制；浏览器缺失时自动回退纯文本
- **多模态看图** — 错误截图可自动注入视觉模型；AI 也可用 `[看图@日志: xxx.png]` 主动请求查看（不发送给用户）
- **模型自主附件** — AI 用 `[附件@日志: xxx.png]` / `[附件@项目: yyy.json]` 声明，Bot 按来源与相对路径取出；图片内联、文件上传群文件
- **引用式追问答疑** — **引用 Bot 的分析结果消息**即可继续提问，普通发言不会触发；分析历史归档使追问可**跨越数天**
- **项目代码参考** — 可选配置 MaaFW 项目仓库，按日志包文件名中的版本号自动切到对应 tag，让 AI 对照源码确认问题；支持 **inject**（预检索注入，快）与 **agent**（AI 自主检索，深）两种模式
- **QQ 指令** — `/maa` 系列指令查看/修改配置，带三级权限控制，可整体关闭
- **多群配置** — 每个配置独立管理监听群、通知群、分析设置与任务记录
- **调试模式** — 无需真实 OneBot 服务端即可注入模拟事件，CLI REPL + HTTP 端点 + JSON 批量测试

## 快速开始

**前置要求**：Python 3.10+、任意 OneBot v11 实现（NapCat / LLOneBot / Lagrange / OpenShamrock 等）、一个 OpenAI 兼容的 Chat Completions 接口。

```bash
python -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows
pip install -r requirements.txt

python main.py               # 首次运行生成 config.json
```

编辑生成的 `config.json`，至少填好这两处：

```jsonc
{
  "bot": {
    "configs": {
      "默认": {
        "listen_groups": ["你的群号"],
        "notify_group": "你的群号"
      }
    }
  },
  "llm": {
    "providers": [
      {
        "name": "default",                 // 端点名（配置里引用它）
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-...",
        "models": [
          { "model": "gpt-4o", "supports": { "tools": true, "vision": true } }
        ]
      }
    ]
  }
}
```

每个配置还要在 `bot.configs.<名称>.llm.models[]` 里按优先级引用：
`[{"name": "default", "model": "gpt-4o"}]`。

项目相关内容（项目名 / 各 UI 的日志包命名 / 代码仓库）统一写在 `project` 段：

```jsonc
"configs": {
  "MAK": {
    "listen_groups": ["你的群号"],
    "project": {
      "name": "MAK",
      // 不需要前缀：文件名特征初筛 + 下载后内容特征识别
      "archive_match_mode": "auto",
      // 按 UI 开关：键名 mxu / mfaa / maafwapp / auto
      "ui": { "mfaa": { "enabled": true } },
      "repo": { "enabled": true, "mode": "agent", "url": "https://github.com/..." }
    },
    "settings": { "max_log_files": 6 }
  }
}
```

再运行 `python main.py` 即可。之后在监听群上传日志压缩包（MXU / MFAAvalonia / MaaFwApp 命名均可，**无需配置前缀**），Bot 会自动分析并把结论发回群内：

```text
[群员] 上传 MaaXXX-logs-20260907-033044.zip
[Bot]  检测到日志包：MaaXXX-logs-20260907-033044.zip
       正在下载并分析，请稍等。
[Bot]  摘要完成，提取到 5 个日志文件、2 张错误截图，正在调用 AI。
[Bot]  MaaXXX 日志分析结果
       运行环境：MFAAvalonia · 软件版本：v2.16.2 · 资源版本：v1.4.7 · MaaFramework：v5.13.0
       文件：log_20260907_033044.zip
       大小：3.44 MB
       日志文件：5 个
       错误截图：2 张

       结论：
       本次任务失败的主要原因是游戏画面识别持续失败...
```

> 也可使用 [uv](https://github.com/astral-sh/uv)：`uv venv && uv pip install -r requirements.txt`

## 触发条件

Bot 只在**同时满足**以下条件时才会分析：

| 条件 | 说明 |
| --- | --- |
| 事件来源 | `notice.group_upload`（群文件上传）或消息内的 `file` 段 |
| 群被监听 | 群号在某个配置的 `listen_groups` 中 |
| 群唯一归属 | 该群只属于**一个**配置（多归属时忽略，避免设置歧义） |
| 配置启用 | 该配置的 `settings.enabled` 为 `true` |
| 文件名初筛 | `project.archive_match_mode`：`auto`（已知 UI 命名 / 名字含 log·logs·日志）、`known`、`prefix`（用 `project.file_prefix`）、`any` |
| 内容特征识别 | 下载后检查包内是否有日志或已知 UI 布局，非日志包直接跳过、不调用 AI（`prefix` 模式可用 `project.require_log_layout: false` 关闭） |
| UI 开关 | `project.ui.<名称>.enabled` 为 `false` 时跳过该 UI 的日志包 |
| 大小合规 | 不超过 `max_zip_size_mb`（超限会提示并记录跳过原因） |

非 `/maa` 开头的聊天文本不会触发指令；只有**引用了 Bot 分析结果消息**的发言才会被当作追问。

## 支持的日志包

| UI | 文件名示例 | 主要布局 |
| --- | --- | --- |
| MXU | `MaaAssistantKedrgame-logs-1.0.0-alpha.4-20260814-135755.zip` | 根目录 `maafw.log`、`on_error/`、`config/mxu-*.json` |
| MFAAvalonia | `log_20261019_110324.zip`、`log_20261019_141709-part01.zip` | `debug/maafw.log`、`logs/log-*.log`、`debug/on_error/` |
| MaaFwApp | `maafw_logs_20261022_195657.zip` | `log/maafw.log`、`log/app.log`、`device_info.txt`、`pi/debug/maafw.log`、`log/run/*.jsonl` |

版本来源：MXU 取文件名中的 `-logs-<版本>-<时间戳>`；MFAAvalonia 取 `程序版本` / `资源版本` / `MaaFramework 版本`；
MaaFwApp 取 `device_info.txt` 的 `Version` / `MaaFwApp` / `Framework`。未知布局会回退到通用日志选择。

## 文档

| 文档 | 内容 |
| --- | --- |
| [配置](docs/configuration.md) | `config.json` 全部字段、多群配置、通信模式、模型选择建议 |
| [分析设置](docs/settings.md) | 全部设置项（触发 / 附件 / 追问 / 历史 / 摘要 / 并发 / 模型） |
| [QQ 指令](docs/commands.md) | `/maa` 指令清单与三级权限控制 |
| [结果附件](docs/attachments.md) | 来源限定、相对路径沙箱、敏感文件保护、图文混排 |
| [追问答疑](docs/followup.md) | 引用式追问的触发规则与会话上下文 |
| [分析历史](docs/history.md) | 日志包归档、周期保留策略、手动管理 |
| [项目代码参考](docs/repo.md) | inject / agent 两种模式与全部设置项 |
| [AI 工具](docs/tools.md) | 日志包工具与仓库工具、沙箱安全、调用限制 |
| [调试](docs/debugging.md) | CLI REPL、HTTP 端点、JSON 批量测试与断言 |
| [架构](docs/architecture.md) | 分层设计、目录结构、事件订阅 |
| [发布](docs/releasing.md) | 自动发布流程、发布包白名单、提交信息规范 |

## 安全设计

- **附件路径沙箱** — 只接受相对路径，拒绝绝对路径与 `..` 上跳；必须指定来源（`@日志` / `@项目`），不跨来源回退
- **凭据保护** — `.git` / `.env` / `id_rsa` / `.pem` 等凭据密钥类文件一律禁止外发（详见[结果附件](docs/attachments.md#敏感文件保护)）
- **安全读取** — 通过 `get_group_file_url` 下载，zip 只在临时目录解析，不直接解压到外部路径
- **仓库只读** — 只读取仓库文本文件，**绝不执行任何仓库代码**；路径先 `resolve` 再校验位于仓库根内
- **保护本地开发** — 工作区有未提交改动时拒绝 `checkout`（`refuse_dirty_worktree`），分析后精确还原到分析前的 ref
- **提示词隔离** — 使用专用 `system_prompt`，不复用聊天人设或历史上下文；`system_prompt` 不可通过指令修改

## 鸣谢

### 开源项目

- [OneBot v11](https://github.com/botuniverse/onebot-11)
- [MaaFramework](https://github.com/MaaXYZ/MaaFramework)
  ![license](https://img.shields.io/github/license/MaaXYZ/MaaFramework?style=flat-square) 基于图像识别的自动化黑盒测试框架

### 贡献/参与者

感谢全部参与到测试与开发中的开发者ヾ(≧▽≦*)o

## 许可证

本项目采用 **GNU Affero General Public License v3.0 (AGPLv3)** 开源许可证。详见 [LICENSE](LICENSE) 文件。
