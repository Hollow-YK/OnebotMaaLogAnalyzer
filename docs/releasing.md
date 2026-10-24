# 发布

推送 `v*` 标签即触发 GitHub Actions 自动发布：

```bash
git tag v0.3.1
git push origin v0.3.1
```

工作流会：

1. 用 [git-cliff](https://git-cliff.org/) 依据提交记录自动生成 Release Notes（分组规则见 `.github/cliff.toml`）
2. 打包**普通包**与**精简包**两个压缩包（**白名单式**，仅含运行必需文件）
3. 创建 GitHub Release；版本号含 `alpha` / `beta` / `rc` / `dev` / `preview` 时自动标记为预发布

## 两种发布包

| 压缩包 | 顶层目录 | 内容差异 |
| --- | --- | --- |
| `OnebotMaaLogAnalyzer-<版本>.zip` | `OnebotMaaLogAnalyzer/` | **普通包**：含 `tools/`（模板编辑器、config.json 编辑器）与 `templates/` 下全部主题 |
| `OnebotMaaLogAnalyzer-<版本>-light.zip` | `OnebotMaaLogAnalyzer-light/` | **精简包**：不含 `tools/`，`templates/` 只保留 `default` |

> Bot 本体运行不需要 `tools/`，两个包都能直接 `pip install -r requirements.txt` 后启动。
> 精简包体积更小、暴露面更少，普通包额外附带可视化工具与可选主题。
>
> 精简包只带 `default` 主题。若 `report.template` 指向其它主题（如 `default-dark`），
> 运行时记一条 warning 并回退 `default`，功能不受影响；需要其它主题就换普通包，
> 或把自己的主题目录放进 `templates/`。

## 发布包内容（白名单）

```text
OnebotMaaLogAnalyzer/        # 精简包顶层目录名为 OnebotMaaLogAnalyzer-light，且没有 tools/、templates/ 只有 default/
├── main.py
├── requirements.txt
├── README.md
├── LICENSE
├── docs/         # 详细文档
├── bot/          # OneBot v11 协议层
├── core/         # 基础设施（models / service / llm / data_manager）
├── features/     # 业务功能（maa/）
├── templates/    # 报告图片模板：普通包含全部主题，精简包只有 default/
└── tools/        # 【仅普通包】templates/editor.py、config/editor.py
```

**不打包**（开发期专用，发布包中会被校验拦截）：

| 排除项 | 原因 |
| --- | --- |
| `debug/`（含 `examples/*.json`） | 调试工具与测试用例 |
| `.github/` | CI 配置 |
| `.gitignore` / `config.json` | 开发与本地配置（`config.json` 含密钥） |
| `__pycache__/`、`*.pyc` | 字节码缓存 |
| `test_*.py`、`tests/` | 单元测试 |
| `*.log` | 运行日志 |
| `tools/` | 仅在精简包中排除；普通包保留 |
| `templates/<其它主题>/` | 仅在精简包中排除，只保留 `default/` |

打包脚本在生成压缩包前会**双重校验**（两个包分别校验）：既检查禁止文件不存在，也检查必需文件齐全，任一不满足即让构建失败，避免把测试文件误发到发布包。此外还有包归属校验：

- 普通包缺 `tools/templates/editor.py` 或 `tools/config/editor.py` → 构建失败
- 精简包里出现 `tools/` → 构建失败
- 精简包的 `templates/` 除了 `default/` 还有别的主题 → 构建失败

（`templates/default/page.html` 属于通用必需文件，两个包都缺则构建失败。）

## 提交信息规范

Release Notes 由提交记录自动生成，无需手工维护版本历史文件。

推荐使用[约定式提交](https://www.conventionalcommits.org/)（`feat:` / `fix:` / `docs:` / `refactor:` 等），也支持中文前缀（`新增` / `修复` / `优化` 等）兜底分组。提交信息若含 `[skip changelog]` 则不会出现在 Release Notes 中。
