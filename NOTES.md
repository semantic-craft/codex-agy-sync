状态: active

# codex-agy-sync

## 目标

创建正式代码项目 `codex-agy-sync`（Codex与AGY会话同频桥），实现新版 Antigravity 2.30+（Hub 桌面端与 `agy` CLI）与 OpenAI Codex 0.166+（Codex Desktop 与 CLI）在同一项目文件夹下对话与工具步骤的最大颗粒度对齐、双向导入与增量同步。

交付形式：
- 原生按需 CLI 同步工具（`C1` 显式子命令触发，无后台轮询守护进程，支持按项目目录自动配对 Codex `project_id` 与 Antigravity `project_id`，双向增量同步或通过 `--include-interrupted` 接续已停止的半途会话）。
- 基于 REA 提取的编译态 `.proto` 契约（从 `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 与 `agy` CLI 提取真实 descriptor，替代手写裸 wire tag）。
- `A2` 在线回环只读 RPC（`LanguageServerService/GetCascadeTrajectory` Connect RPC 与 `codex app-server` `thread/read` Unix Socket WebSocket JSON-RPC v2）加受控离线 SQLite/Protobuf/Rollout 落盘引擎。

明确不做：
- 不直接裸写正在持有活跃 WAL 锁或处于运行回合中的宿主数据库（活跃源端走回环 RPC 或 `?mode=ro` 只读读取，目标端仅在离线无锁时持排他 `flock` 先备份再落盘）。
- 不驻留后台轮询守护进程，不上传任何本地会话、凭据或 `csrfToken` 到外部网络，不在日志、异常或收据中回显 `csrfToken`。

## 完成标准

1. **REA/REAP 契约提取与验证** 使用 `reverse-engineer-anything` 与 `reap-prompts` 从本机 Antigravity 2.30+（`language_server`、`agy`、`app.asar`）及 Codex 0.166+（`state_5.sqlite`、`rollout-*.jsonl`）提取完整 Protobuf 与 SQLite schema，生成可自动校验的契约定义与证据档。
2. **同文件夹项目自动配对** 自动关联 `~/.gemini/config/projects/<uuid>.json`（`folderUri`）与 `~/.codex/state_5.sqlite`（`projects` + `project_roots`）及 `.codex-global-state.json`，确保导入或同步后的会话直接挂载在两侧桌面端左侧对应项目下（Antigravity `source = 0` 且带合法 `project_id`，不落入 `outside-of-project` 或 `default-cli-project`；Codex `preview <> ''` 且 `archived = 0`）。
3. **细粒度步骤双向对齐** 支持 User 输入（`step_type = 14`）、Planner 回复与思考摘要（`step_type = 15`）、错误消息（`step_type = 17`）、16 类工具调用与返回结果（`step_type = 15/132` 内嵌 `ToolCall`）、阶段边界（`step_type = 23/81/82`）以及伴生日志 `brain/<id>/.system_generated/logs/transcript.jsonl`（含 `antigravity-cli` 镜像）与 `annotations/<id>.pbtxt` 的无损双向映射。
4. **增量接续与分叉保护** 支持在已完成回合边界增量同步，支持 `--include-interrupted` 显式闭合抢救已停止的半途回合（`B2`），记录已同步游标并具备对称分叉派生、持锁写盘与 `BackupManifest.restore()` 自动回滚机制。
5. **端到端实测通过** 项目 `scripts/check` 全量门禁通过，并在隔离沙盒与本机空闲窗口完成端到端验收。

## 当前状态

- 2026-11-14 完成 `handoff` CLI/UX 增强（分支 `feat/handoff-cli-ux`）：新增 `bridge/handoff_resolve.py`（配对诊断、候选会话扫描、`--source-id` 方向自动推断、`--latest` 跳过未动过的接力目标、`--chain` 接力链游标/幂等 `no_op`、`--pick` 仅写 `stderr` 的 TTY 交互菜单及 `--pair-if-offline`），扩展 `HandoffReceipt` 向后兼容加载（`load_handoff_receipt`）与种子指纹（`ledger_seed_fingerprint`），使 `guarded_pair_workspace` 双端已注册时零锁零写入幂等，并在 `cli.py` 为未配对/忙锁/挂起返回退出码 `2` 与结构化 `stderr` JSON。单元测试补齐 `tests/test_handoff_cli_ux.py`。

- 2026-11-14 为外置工作台原型新增独立浏览器验收入口，见 `skills/verify-codex-agy-sync/features/ui-prototype.md`。覆盖搜索、共享草稿、模拟请教、完整历史接续与重复接续；证据与原型留在仓库外。截图由 Agent 单独复核，真实 Desktop 与消息投递须另行验收。

- 2026-11-14 `0.2.0` 已完成实现与发布包准备。GitHub 仓库已公开。Desktop handoff 的交付范围是可见历史文本与新会话续聊，不宣称跨宿主运行态无损迁移；同会话在线追加和其余工具的全面实机验收不在本次完成范围。

- 2026-11-14 新增 Desktop 专用 `handoff`。复用现有账本和编码器，AGY 以全新 ID 发布后由原生 RPC 加载，Codex 走原生 fork 并等待卸载。每次切换生成新会话，相同源历史重跑 `no_op`；不做实时镜像，不覆盖活跃会话。
- 真实 Desktop 验收发现并修复格式、重复消息和跨宿主运行态问题。思考及检查点摘要保留为普通文本，不重放私有推理状态、原生检查点和明确宿主注入。当前验收对象为 AGY 2.31.2 与 ChatGPT 内嵌 Codex；普通文本、多轮上下文和 Python 工具历史文本已实际双向往返；工具名称、参数与原始输出平文化，不仿制宿主运行态。验收收据与截图位于 `$HOME/Projects/_artifacts/codex-agy-sync/verify-desktop-20261113T180734Z/acceptance-*`。
- 发布复验 `scripts/check` 通过，91 项测试、5 项既有实机夹具跳过。此前真实测试会话已归档，本轮 Codex 映射使用其隔离数据库副本并只在副本恢复可见状态；AGY 仍为只读实机映射。隔离 CLI 七场景通过，宿主指纹未变。wheel 已在独立环境安装，版本和 `handoff --help` 通过。本轮未重新执行 Desktop 多轮实机续聊，沿用上列已验收证据。


## 下一步

1. 使用 `handoff` 在 Desktop 间按需交接；同一会话 ID 的在线增量追加仍不在此路径内。
2. 其他工具类型的 Desktop 运行态兼容性按实际使用反馈验证；现有编码器回归不等于这些工具的全部实机验收。

## 记录

- Kotone 判断 `a607d12864a349a18ddb0d63345bd3d6`，来源 `antigravity`。
- 2026-11-10 完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md`、`AGENTS.md`、`NOTES.md` 初始化。

## 坑

- Antigravity Desktop 2.30+（Hub 架构）使用 `~/.gemini/antigravity/conversation_summaries.db`、`agyhub_summaries_proto.pb` 与 `~/.gemini/config/projects/<uuid>.json`，不再使用旧版 IDE 2.5.5 的 `User/globalStorage/state.vscdb`。
- Antigravity Hub 的 source 可见性以 `schema/sidebar_visibility_rules.json` 为准；`source = 17`（CLI）会进入外部会话。2.31.2 原生样本 trajectory source 为1、summary source为0，两者不能混用。离线投影的标题同时写入 `TrajectorySummary.annotations.title`（field 15 -> 1）与 `annotations/<id>.pbtxt`。
- `agy --conversation=<id>` 强依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/` 目录存在，缺失会导致 CLI 挂起或报错。
- Antigravity 2.30+ 的 `steps.step_payload` 二进制是 `gemini_coder.Step`，其中 `step_type = 14` 对应 field 19，`step_type = 15` 对应 field 20，`step_type = 17` 对应 field 24，`step_type = 23` 对应 field 30，`step_type = 81/82` 对应 field 93/94，`step_type = 101` 对应 field 114，`step_type = 132` 对应 field 140（内嵌 `exa.cortex.core.tools.*` `google.protobuf.Any` 载荷）。
- macOS 上 Codex Desktop 的 `~/.codex/app-server-control/app-server-control.sock` 是指向 `/private/tmp/codex-...` 的符号链接，探测时必须用 `Path.stat()` 而非 `lstat()`，且连接时优先取解析后更短的目标路径以避开 `AF_UNIX` 104 字节 `sun_path` 上限。
- 本机独立 CLI `agy` 安装在 `~/.local/bin/agy`，脚本须通过 `ANTIGRAVITY_CLI_BIN` 或 `shutil.which("agy")` 动态发现。
