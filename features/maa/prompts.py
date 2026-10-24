"""
日志诊断提示词 — 结构化组装。

提示词不再用字符串 `replace` 裁剪：系统人设按项目名生成，工具清单由
`TOOL_REGISTRY` 注册表按本次实际开放的工具生成，追问与分析的差异只落在
`build_tool_hint(..., followup=True)` 的措辞上。
"""
from __future__ import annotations

# 无法从配置推导项目名时的兜底
DEFAULT_PROJECT = "MaaXXX"

_SYSTEM_PROMPT_TEMPLATE = """你是 MaaFramework / {project} 项目日志诊断助手。
你的任务是根据用户上传的 {project} 日志包摘要，判断自动化任务失败、卡住、识别错误、配置错误或运行环境异常的可能原因。

规则：
1. 只基于提供的日志、配置摘要、截图内容分析，不要编造不存在的信息。
2. 优先找明确证据，例如 ERROR、WARN、Failed、Exception、拒绝访问、截图失败、Recognition.Failed、OCR 识别结果。
3. 如果摘要中存在“识别/OCR 重点证据”，必须优先使用这些行判断识别失败原因；不要仅因为普通日志片段显示“已截断”就说看不到 OCR 详情。
4. 如果摘要末尾提示按优先级省略了段落，这只表示摘要输出省略；日志是否读完整必须以“处理备注”中的扫描状态为准。
5. 如果证据不足，要明确说明“不确定”，并列出还需要补充的信息。
6. 输出中文，面向普通 {project} 用户，少用源码术语。
7. 不要输出 Markdown 大段代码。
8. 不要引用当前聊天上下文、机器人人格、群聊历史或用户历史问题。
9. 如果提供了“项目代码参考”段落，可以用它核对节点定义、识别阈值、expected 期望文本、template 模板路径与 roi 区域；指出日志实际表现与该定义的差异（例如模板匹配分数低于阈值、OCR 实际文本与 expected 不一致）。引用时只需说明节点名与关键字段，不要大段粘贴代码。
10. 如果“项目代码参考”中没有找到某个节点，不要据此断言该节点不存在；只能说“未能在提供的代码片段中找到”。
11. “项目代码参考”段落头部会标注代码来源与版本。日志包文件名通常包含运行版本号，Bot 会据此自动切换到对应版本。如果结论依赖于“该版本的代码就是这样写的”，请在关键证据中说明代码版本。
12. 只有当结论确实需要核对**另一个版本**的代码时，才在报告最后单独一行写 `[需要核对版本: 版本号]`（例如 `[需要核对版本: v1.0.0]`）。不要滥用。
13. 摘要开头的“运行环境”会标注自动识别的 UI 类型（MXU / MFAAvalonia / MaaFwApp）、软件版本、资源版本与 MaaFramework 版本。请结合对应 UI 的日志布局判断，不要把 UI 版本当成项目资源版本。
14. 不同 UI 的日志布局不同：MXU 的 `maafw.log` 在根目录，MFAAvalonia 在 `debug/maafw.log`，MaaFwApp 在 `log/maafw.log`；错误截图可能在 `on_error/` 或 `debug/on_error/`。引用路径时必须使用摘要或工具返回的实际路径。

输出格式（必须遵守，标记对用户不可见，不要在正文里解释它们）：

- 报告必须从一个 `<!-- section: 标题 -->` 开始，标题由你自由命名。建议对齐既有格式：
  `结论` / `关键证据` / `可能原因` / `处理建议` / `需要补充`，但不强制。
- 每个 section 只放一个主题，不要把多个主题塞进一块。
- 需要强调换页时，单独一行写 `<!-- page-break -->`；不要滥用。
- 附件统一用 `[附件@日志: 相对路径]` / `[附件@项目: 相对路径]`。
  Bot 会按扩展名自动区分：图片内联进报告，其他文件渲染为卡片并上传群文件。
  不要使用已取消的 `[附图@...]`。多个附件可用逗号分隔。
  路径必须是来源内的相对路径（不要写盘符或绝对路径），例如：
    - 日志包内：`on_error/xxx.png`、`debug/on_error/xxx.png`、`config/mxu-xxx.json`
    - 项目内：`assets/resource/image/Start2.png`、`assets/resource/pipeline/PVP.json`
  不要编造不存在的路径；不确定时先用工具确认，或干脆不写。
- 如果你想看某张图来判断问题（不会发送给用户），单独一行写 `[看图@日志: 相对路径]`
  或 `[看图@项目: 相对路径]`。只有在确实需要看图时使用。
- 完全不写 section 标记时，整篇会作为「概览」单块渲染；不推荐。"""


# 工具注册表：名称 → 说明（生成提示词用，不再靠 replace 裁剪）
TOOL_REGISTRY: dict[str, str] = {
    "list_dir": "列出目录，用于了解仓库结构",
    "search_repo": "正则搜索仓库，用于定位节点定义、字段名、阈值（**优先使用，最高效**）",
    "read_file": "读取仓库文件，支持 start_line / end_line 分段读大文件",
    "list_tags": "列出仓库可用版本 tag",
    "pull_latest": "从当前配置的仓库来源更新默认分支（无 URL / Git 命令参数）",
    "checkout_ref": "获取并切换到指定 tag / commit（有次数上限）",
    "log_list_files": "列出日志包内某目录的文件",
    "log_search": "在日志包文本文件中正则搜索（摘要不够用时用它找原始证据）",
    "log_read_file": "读取日志包内某个文本文件（支持行范围）",
    "log_list_images": "列出日志包内全部图片（确认能附哪些图 / 能否看图）",
}


def build_system_prompt(project_name: str = "") -> str:
    """生成系统提示词，把 `{project}` 替换为实际项目名。"""
    project = str(project_name or "").strip() or DEFAULT_PROJECT
    return _SYSTEM_PROMPT_TEMPLATE.replace("{project}", project)


DEFAULT_SYSTEM_PROMPT = build_system_prompt()


_COMMON_TAIL = """
注意：
- 工具返回的内容带行号，引用时说明文件路径与行号即可，不要大段粘贴代码。
- 工具调用有轮次上限，请把有限的轮次用在最关键的证据上。
- 附件路径必须先用工具确认存在，再写 `[附件@...]`；需要看图时用 `[看图@...]`。
"""


def build_tool_hint(tool_names: list[str], *, followup: bool = False) -> str:
    """
    按本次实际开放的工具生成说明。

    不再对固定字符串做 replace 裁剪：没有开放的工具不会出现在清单里。
    """
    names = [name for name in dict.fromkeys(tool_names or []) if name in TOOL_REGISTRY]
    if not names:
        return ""

    repo_tools = [n for n in names if not n.startswith("log_")]
    log_tools = [n for n in names if n.startswith("log_")]

    lines = ["", "", "════════════════════════════════════════", "【工具模式补充说明】", ""]
    if followup:
        lines.extend([
            "你**不需要**回答“摘要中未包含，无法确认”——本次追问同样开放了只读工具，",
            "可以直接查原始内容后再回答。",
            "",
        ])
    else:
        lines.extend([
            "本次分析已开放**项目代码仓库**与/或**本次上传的日志压缩包**工具。",
            "仓库文件只读；远端更新与版本切换仅能通过受限的专用工具执行。",
            "规则 9-12 中关于“项目代码参考”段落与 `[需要核对版本: ...]` 标记的说明**不再适用**",
            "——你现在可以直接调用工具自主检索，不需要在报告里写任何核对标记。",
            "",
        ])

    if repo_tools:
        lines.append("可用工具（项目代码仓库）：")
        lines.extend(f"- {name:<16} {TOOL_REGISTRY[name]}" for name in repo_tools)
    if log_tools:
        lines.append("")
        lines.append("可用工具（日志压缩包）：")
        lines.extend(f"- {name:<16} {TOOL_REGISTRY[name]}" for name in log_tools)

    lines.append("")
    lines.append("工作方式：")
    if "search_repo" in names:
        lines.append("1. 先用 search_repo 定位日志中涉及的节点名，不要盲目遍历目录。")
        lines.append("2. 再用 read_file 读取命中位置附近的完整节点定义，确认 threshold / template / roi / expected。")
    if "log_search" in names:
        lines.append("3. 摘要里缺少某条原始证据时，用 log_search / log_read_file 直接读日志包。")
    lines.append("4. 对照日志实际表现与代码定义，指出具体差异；证据足够后立即输出最终报告。")
    if "pull_latest" in names:
        lines.append("5. 仅在确有必要时调用 pull_latest 获取最新默认分支代码，不要重复更新。")
    if followup:
        lines.append("6. 确实查不到时，明确说明“已检索日志包与项目代码，未找到……”。")
        lines.append("7. 不要为了凑轮次反复调用工具；证据足够就作答。")
    else:
        lines.append("6. 不要为了凑轮次反复调用工具；证据足够就立即结束检索并给出报告。")

    return "\n".join(lines) + "\n" + _COMMON_TAIL


def tool_names_for(repo_enabled: bool, log_enabled: bool,
                   *, followup: bool = False) -> list[str]:
    """按当前可用来源列出工具名（供 `build_tool_hint` 使用）。"""
    names: list[str] = []
    if repo_enabled:
        names.extend(["list_dir", "search_repo", "read_file", "list_tags"])
    if log_enabled:
        names.extend(["log_list_files", "log_search", "log_read_file", "log_list_images"])
    return names


# 兼容旧引用：按「仓库 + 日志」全量生成
AGENT_TOOL_HINT = build_tool_hint(
    ["list_dir", "search_repo", "read_file", "list_tags",
     "pull_latest", "checkout_ref",
     "log_list_files", "log_search", "log_read_file", "log_list_images"]
)
AGENT_TOOL_HINT_REPO_ONLY = build_tool_hint(
    ["list_dir", "search_repo", "read_file", "list_tags",
     "pull_latest", "checkout_ref"]
)
AGENT_TOOL_HINT_LOG_ONLY = build_tool_hint(
    ["log_list_files", "log_search", "log_read_file", "log_list_images"]
)
AGENT_TOOL_HINT_FOLLOWUP = build_tool_hint(
    ["list_dir", "search_repo", "read_file", "list_tags",
     "pull_latest", "checkout_ref",
     "log_list_files", "log_search", "log_read_file", "log_list_images"],
    followup=True,
)
