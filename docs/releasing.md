# 发布

推送 `v*` 标签即触发 GitHub Actions 自动发布，例如：

```bash
git tag v0.3.1
git push origin v0.3.1
```

压缩包版本名会去掉开头的 `v`，并将标签中的 `/` 替换为 `-`。
Release 名称保留原始标签名。

工作流会：

1. 用 [git-cliff](https://git-cliff.org/) 依据提交记录自动生成 Release Notes
   （分组规则见 `.github/cliff.toml`）。
2. 生成裸 OneBot 普通包、裸 OneBot 精简包和 AstrBot 插件包三个压缩包。
3. 在压缩前执行白名单、必需文件和禁止文件校验，任一失败即中止发布。
4. 创建 GitHub Release；版本号含 `alpha` / `beta` / `rc` / `dev` / `preview`
   时自动标记为预发布。

## 持续集成

每次 push 和 Pull Request 都会运行类型检查，并使用与正式发布相同的脚本构建上述三个包。
ZIP 可从对应 GitHub Actions 运行的 `packages-<提交 SHA>` artifact 下载；CI 不会创建 Release。

类型检查使用 mypy，将当前结果与 PR 目标提交比较；push 默认分支时与推送前的提交比较，
其他分支 push 时与默认分支比较；新增类型问题会使
检查失败，行号变化不会影响比较。本地可安装 `mypy==2.4.1`，并将 `MYPY_BASELINE_DIR`
设置为比较用的代码目录后运行 `python .github/scripts/check_types.py`。

## 三种发布包

| 压缩包 | 顶层目录 | 用途 |
| --- | --- | --- |
| `OnebotMaaLogAnalyzer-<版本>.zip` | `OnebotMaaLogAnalyzer/` | 裸 OneBot 普通包，含编辑工具和全部报告主题 |
| `OnebotMaaLogAnalyzer-<版本>-light.zip` | `OnebotMaaLogAnalyzer-light/` | 裸 OneBot 精简包，不含编辑工具，只含 `default` 主题 |
| `OnebotMaaLogAnalyzer-<版本>-astrbot.zip` | `OnebotMaaLogAnalyzer/` | AstrBot AIOCQHTTP 插件包 |

两个裸模式包都从 `main.py` 启动，并包含完整的 `maa_analyzer/` 共享内核。AstrBot 包中的
`main.py` 是插件入口，安装到 AstrBot 插件目录后不需要再启动裸 OneBot 进程。

## 普通包

```text
OnebotMaaLogAnalyzer/
├── main.py
├── requirements.txt
├── README.md
├── LICENSE
├── docs/
├── maa_analyzer/
├── templates/
└── tools/
```

`tools/` 包含模板编辑器和 `config.json` 编辑器，`templates/` 包含全部内置报告主题。
该包运行时不依赖 `tools/`，但保留它供用户编辑模板与配置。

## 精简包

```text
OnebotMaaLogAnalyzer-light/
├── main.py
├── requirements.txt
├── README.md
├── LICENSE
├── docs/
├── maa_analyzer/
└── templates/
    └── default/
```

精简包不含 `tools/`，并删除 `templates/` 下除 `default/` 外的所有主题。若配置中的
`report.template` 指向其它主题，运行时会记录 warning 并回退到 `default/`；日志分析和
纯文本报告不受影响。

## AstrBot 插件包

```text
OnebotMaaLogAnalyzer/
├── main.py
├── README.md
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── maa_analyzer/
└── templates/
```

解压后必须直接得到 `OnebotMaaLogAnalyzer/main.py`，不能再多套一层同名目录。插件包包含
与裸模式相同的 `maa_analyzer/` 和报告模板；配置、数据目录及安装步骤见
[AstrBot 部署](astrbot.md)。

## 白名单与禁止项

三个包在压缩前分别检查必需文件。裸模式包必须包含启动器、共享内核和
`templates/default/page.html`（以及同样必需的 `attach-img.html` /
`attach-file.html`）；普通包还必须包含两个编辑器：

- `tools/templates/editor.py`
- `tools/config/editor.py`

AstrBot 包必须包含插件壳、面板 schema、共享内核和
`maa_analyzer/platforms/astrbot/adapter.py`。插件包不得只在顶层放 `maa_analyzer/` 而
遗漏插件入口。

以下内容不得进入任何发布包：

| 排除项 | 原因 |
| --- | --- |
| `debug/` | 开发调试工具和录音事件 |
| `tests/`、`test_*.py` | 单元测试 |
| `.github/` | CI 配置 |
| `config.json`、`config.local.json`、`.env` | 本地配置和密钥 |
| `.gitignore` | 开发元数据 |
| `__pycache__/`、`*.pyc`、`*.pyo`、`*.pyd` | 字节码缓存 |
| `*.log` | 运行日志 |
| `astrbot_plugin/` 作为裸包内目录 | AstrBot 源码目录不进入裸模式包 |
| 顶层 `bot/`、`core/`、`features/` | 旧命名空间，迁移后不允许回归 |

精简包额外要求 `tools/` 不存在，且 `templates/` 顶层只能有 `default/`。工作流在
ZIP 生成前打印每个包的完整文件列表，便于在 Release 日志中审计内容。

## 发布前验收

自动构建不能替代部署联调。发布前至少执行仓库全量测试、导入边界检查和裸模式配置模板
启动检查；AstrBot 侧还需要在 AstrBot 4.29.x + AIOCQHTTP 环境分别验证 NapCat 与
Lagrange 的上传触发、分卷汇总、引用追问、附件、历史清理、多配置以及停用/启用/热重载。

真实 AstrBot 与 NapCat / Lagrange 联调不由当前仓库 CI 执行，未在部署环境完成验证时不得
把该组合写成“已联调通过”。详细验证边界见[AstrBot 部署](astrbot.md#验证边界)。

## 提交信息规范

Release Notes 由提交记录自动生成，无需手工维护版本历史文件。推荐使用
[约定式提交](https://www.conventionalcommits.org/)（`feat:` / `fix:` / `docs:` /
`refactor:` 等），也支持中文前缀（`新增` / `修复` / `优化` 等）兜底分组。提交信息若含
`[skip changelog]` 则不会出现在 Release Notes 中。
