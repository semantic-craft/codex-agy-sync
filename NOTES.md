状态: active

# codex-agy-sync

## 目标

创建正式代码项目 `codex-agy-sync`（Codex与AGY会话同频桥），实现新版 Antigravity 2.30+（Hub 桌面端与 `agy` CLI）与 OpenAI Codex 0.166+（Codex Desktop 与 CLI）在同一项目文件夹下对话与工具步骤的最大颗粒度对齐、双向导入与增量同步。

交付形式：
- 原生 CLI 与守护同步工具（支持按项目目录自动配对 Codex `project_id` 与 Antigravity `project_id`，双向同步或单向接续未完成会话）。
- 基于 REA 提取的编译态 `.proto` 契约（从 `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 与 `~/.local/libexec/agy/agy` 提取真实 descriptor，替代手写 wire tag）。
- 在线 RPC（`LanguageServerService` Connect RPC / `codex app-server` JSON-RPC v2）与离线 SQLite/Protobuf/Rollout 双模读写引擎。

明确不做：
- 不直接裸写正在持有活跃 WAL 锁且正处于运行回合中的宿主数据库（运行中读取走 RPC 或只读快照，离线或空闲时才做受控落盘与缓存同步）。
- 不上传任何本地会话、凭据或 `csrfToken` 到外部网络，不在日志或收据中回显 `csrfToken`。

## 完成标准

1. **REA/REAP 契约提取与验证**：使用 `reverse-engineer-anything` 与 `reap-prompts` 从本机 Antigravity 2.30+（`language_server`、`agy`、`app.asar`）及 Codex 0.166+（`state_5.sqlite`、`rollout-*.jsonl`）提取完整 Protobuf 与 SQLite schema，生成可自动校验的契约定义与证据档。
2. **同文件夹项目自动配对**：自动关联 `~/.gemini/config/projects/<uuid>.json`（`folderUri`）与 `~/.codex/state_5.sqlite`（`projects` + `project_roots`）及 `.codex-global-state.json`，确保导入或同步后的会话直接挂载在两侧桌面端左侧对应项目下（Antigravity `source = 0` 且带合法 `project_id`，不落入 `outside-of-project` 或 `default-cli-project`；Codex `preview <> ''` 且 `archived = 0`）。
3. **细粒度步骤双向对齐**：支持 User 输入（`step_type = 14`）、Planner 回复与思考摘要（`step_type = 15` / field 94）、工具调用与返回结果（`step_type = 15/132` 内嵌 `ToolCall`：`exec_command`/`run_command`、`apply_patch`/`edit_file`、`view_file`/`read_file`）、阶段边界（`TaskBoundaryResponse` field 93 / `CHECKPOINT` `step_type = 23`）以及伴生日志 `brain/<id>/.system_generated/logs/transcript.jsonl` 与 `annotations/<id>.pbtxt` 的双向映射。
4. **增量接续与分叉保护**：支持在 Codex 工作到一半时一键同步到同目录 Antigravity 会话继续执行（反之亦然），记录已同步游标并具备分叉检测与原子备份回滚机制。
5. **端到端实测通过**：项目 `scripts/check` 全量测试通过，且在本机真实 Codex 与 Antigravity Desktop 2.30+ 上完成同目录双向导入与续聊实测验收。

## 当前状态

- 2026-11-10：通过 Kotone（判断 ID：`a607d12864a349a18ddb0d63345bd3d6`）完成正式项目立项，已挂载至 Antigravity Desktop（项目 ID：`c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）、Codex、DSH 与 Cindy 侧栏；已完成六大参考项目调研与三阶段架构设计，等待在本项目内开启新会话进入第一阶段实施。

## 下一步

1. 在 `~/Projects/codex-agy-sync` 开启新对话，执行第一阶段 REA 取证：用 `inspect_go_binary` / `open_binary` 提取 `language_server` 与 `agy` 内嵌的 `exa.language_server_pb` Protobuf descriptor，并用 `analyze_javascript_application` 核对 `app.asar` 侧栏过滤与刷新逻辑。
2. 用 `/architect` 设计核心领域模型（中间统一轨迹 IR、双向工具映射表、游标与分叉状态机）及 `scripts/check` 验证入口。
3. 实现并验证同目录 `Codex <-> Antigravity 2.30+` 双向高保真转换与在线/离线同步命令。

## 记录

- Kotone 判断：a607d12864a349a18ddb0d63345bd3d6；来源：antigravity。
- 2026-11-10：完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md` / `AGENTS.md` / `NOTES.md` 方案初始化。

## 坑

- Antigravity Desktop 2.30+（Hub 架构）使用 `~/.gemini/antigravity/conversation_summaries.db`、`agyhub_summaries_proto.pb` 与 `~/.gemini/config/projects/<uuid>.json`，不再使用旧版 IDE 2.5.5 的 `User/globalStorage/state.vscdb`。
- Antigravity Hub 前端仅展示 `source` 为 `0`、`18`、`20` 的会话，`source = 17`（CLI）会被隐藏到 `externalSummaries`；且标题必须同时写入 `TrajectorySummary.annotations.title`（field 15 -> 1）与 `annotations/<id>.pbtxt`。
- `agy --conversation=<id>` 强依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/` 目录存在，缺失会导致 CLI 挂起或报错。
