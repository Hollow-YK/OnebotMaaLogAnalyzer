# 发布

推送 `v*` 标签即触发 GitHub Actions 自动发布：

```bash
git tag v0.3.1
git push origin v0.3.1
```

工作流会：

1. 用 [git-cliff](https://git-cliff.org/) 依据提交记录自动生成 Release Notes（分组规则见 `.github/cliff.toml`）
2. 打包可运行的项目压缩包（**白名单式**，仅含运行必需文件）
3. 创建 GitHub Release；版本号含 `alpha` / `beta` / `rc` / `dev` / `preview` 时自动标记为预发布

## 发布包内容（白名单）

```text
OnebotMaaLogAnalyzer/
├── main.py
├── requirements.txt
├── README.md
├── LICENSE
├── docs/         # 详细文档
├── bot/          # OneBot v11 协议层
├── core/         # 基础设施（models / service / llm / data_manager）
└── features/     # 业务功能（maa/）
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

打包脚本在生成压缩包前会**双重校验**：既检查禁止文件不存在，也检查必需文件齐全，任一不满足即让构建失败，避免把测试文件误发到发布包。

## 提交信息规范

Release Notes 由提交记录自动生成，无需手工维护版本历史文件。

推荐使用[约定式提交](https://www.conventionalcommits.org/)（`feat:` / `fix:` / `docs:` / `refactor:` 等），也支持中文前缀（`新增` / `修复` / `优化` 等）兜底分组。提交信息若含 `[skip changelog]` 则不会出现在 Release Notes 中。
