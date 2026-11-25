# AUTOMATION_SPEC.md（Automation：CLI 软链与 Agent Skill 安装规格）

日期：2026-11-25
前置依赖：[INTEGRATION.md](./INTEGRATION.md)
范围：Phase 1（`CodexAgySync.app` 内置 CLI 软链安装器、干净室 Agent Skill 模板与覆盖前 Diff 审阅、外部托管防覆写保护、`自动化 / Automation` 设置分栏）。Phase 2（可选本地 `AF_UNIX` NDJSON JSON-RPC 2.0 控制桥）按需延后。

---

## 1. 目标与边界

让 Claude Code、Codex、Cursor、Antigravity 与任意脚本直接通过 `~/.local/bin/codex-agy-sync` 和 `/codex-agy-sync` Agent Skill 调用随 `CodexAgySync.app` 内嵌的 Rust CLI，同时遵守以下工程边界：

1. **零新增常驻进程**：核心同步子命令（`sync`、`status`、`pair`、`inspect`、`menubar-feed`、`serve`、`sync-session`、`sync-workspace`）由内嵌 Rust CLI 直接执行，不依赖 GUI 进程常驻或后台轮询（保持 `C1`）。
2. **符号链接而非二进制拷贝**：`~/.local/bin/codex-agy-sync` 软链到 `<AppBundle>/Contents/Helpers/codex-agy-sync`，整包升级替换后 CLI 版本自动对齐。
3. **非破坏性冲突防护**：
   - 若 `~/.local/bin/codex-agy-sync` 已存在且不是指向 `/Contents/Helpers/codex-agy-sync` 的符号链接，安装与卸载均拒绝覆写/删除并返回 `.conflictNonManaged`。
   - 若 `~/.agents/skills/codex-agy-sync` 或 `~/.claude/skills/codex-agy-sync` 为指向外部目录的符号链接（例如由 `skills.toml` / `skills sync` 托管），标记为 `.externallyManaged` 并只读提示，拒绝覆写或删除。
4. **覆盖前 Diff 审阅**：当规范目录下的技能文件与当前模板不一致（本地自定义修改或版本升级）时，先展示逐文件行级 Diff（`AgentSkillDiffReviewSheet`），用户确认后才原子写回。

---

## 2. 模块与代码接缝

| 文件 | 职责 |
| --- | --- |
| `macos/Sources/CodexAgySyncCore/Automation/CLIInstallService.swift` | 管理 `~/.local/bin/codex-agy-sync` 软链探测（`lstat(2)` 识别悬空链）、原子安装/更新（临时链接 + `rename(2)`）、安全卸载与 `~` 展开 `PATH` 检测 |
| `macos/Sources/CodexAgySyncCore/Automation/AgentSkillTemplateRenderer.swift` | 渲染干净室 3 文件技能包：`SKILL.md`（含 YAML frontmatter 与 `jq` 用法）、`references/contract.md`、`references/troubleshooting.md` |
| `macos/Sources/CodexAgySyncCore/Automation/AgentSkillInstallService.swift` | 管理 `~/.agents/skills/codex-agy-sync/` 正本与 `~/.claude/skills/codex-agy-sync` 软链、外部托管保护、LCS 行级 Diff、断链修复与仅删托管文件的选择性卸载 |
| `macos/Sources/CodexAgySyncCore/Automation/AutomationViewModel.swift` | `@MainActor @Observable` 状态编排、安装前/安装后双态切换、Diff 审阅闭环与 `Copy Path` |
| `macos/Sources/CodexAgySyncCore/Automation/AutomationSettingsPane.swift` | 设置页 `自动化 / Automation` 分栏、路径展示、冲突提示与 `AgentSkillDiffReviewSheet` |
| `macos/Sources/CodexAgySyncCore/SettingsView.swift` / `SettingsWindowController.swift` / `StatusBarController.swift` | 扩展 `SettingsTab.automation`、窗宽调整为 `600pt` 锁定单行 4 栏标签、打开与聚焦时刷新状态 |
| `macos/Tests/CodexAgySyncTests/AutomationTests.swift` | 在 `out/test-automation-sandboxes/<UUID>` 中全隔离验证 10 项核心行为，零触碰宿主机用户目录 |

---

## 3. Phase 2（延后决策说明）

当前 8 个核心 CLI 子命令已可独立运行并输出纯 JSON `stdout` 与退出码 `2` 结构化 `stderr`。在 Clean Paste 与 App Hotkeys 尚未合入、且尚无明确通过 CLI 操控运行中 GUI 状态的刚性需求前，暂不在 `CodexAgySync.app` 内增加常驻 `AF_UNIX` Socket 监听服务，以避免过早引入多实例监听归属与沙盒 `EPERM` 维护面。待有明确 GUI 控制需求时，再按 NDJSON JSON-RPC 2.0 + `0600` 权限 + macOS `104` 字节 `sun_path` 约束独立开 PR 落地。
