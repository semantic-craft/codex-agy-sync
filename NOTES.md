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

- 2026-11-10：通过 Kotone（判断 ID：`a607d12864a349a18ddb0d63345bd3d6`）完成正式项目立项，已挂载至 Antigravity Desktop（项目 ID：`c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）、Codex、DSH 与 Cindy 侧栏；已完成六大参考项目调研与三阶段架构设计。
- 2026-11-10：完成第一阶段 REA / REAP 二进制与数据库契约提取与自检杠杆落地：
  - 新增 `scripts/extract_schema.py`，直接从 `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 与 `agy`（支持运行时 PATH 发现 `~/.local/bin/agy`，合并两端描述符并记录版本漂移）提取 9 个编译态 `FileDescriptorProto`（覆盖 `gemini_coder`、`exa.jetski_cortex_pb`、`exa.cortex_pb`、`exa.codeium_common_pb`、`exa.cortex.core.tools`、`exa.project_pb`、`jetbox_summaries_pb`、`exa.language_server_pb`，共 704 个可达消息、136 个枚举、373 个 RPC 方法），生成 `schema/antigravity_2_30_schema.json`、`schema/antigravity_2_30.proto` 与 `schema/sidebar_visibility_rules.json`。
  - 实测校正了开源项目（如 Antigravity-Database-Manager）在 Antigravity 2.30+ 上的字段偏差：`conversations/<id>.db` 的 `steps.step_payload` 实际为 `gemini_coder.Step`（`user_input` 为 field 19、`planner_response` 为 field 20、`checkpoint` 为 field 30、`task_boundary` 为 field 93、`notify_user` 为 field 94、`system_message` 为 field 114、`generic` 为 field 140），而 `step_type = 132`（`CORTEX_STEP_TYPE_GENERIC`）在 `CortexStepMetadata.tool_call`（field 4）存 `ChatToolCall`，在 `Step.generic.result.payload`（`google.protobuf.Any`）内嵌 `exa.cortex.core.tools.{ReadFile,FileEdit,RunCommand,SearchWeb,...}` 强类型结构。
  - 新增只读验证脚本 `scripts/verify_session_mapping.py` 与自检门禁 `scripts/check`，在只读模式（`?mode=ro` + `PRAGMA query_only=ON` + inode/mtime 前后快照校验）下无损解码本机真实 Antigravity 2.30+ 会话（`eb95dc40-cb5e-4017-b4ff-f41223e1f701`，161 步，`unknown_wire_fields_count == 0`）与同项目目录 Codex 0.166+ 会话（`01a1ae7d-d56f-7650-8336-aa848b9f2a5a`，含 `exec_command` 等 19 次配对工具调用），并验证基于真实解码状态突变的 3 项负向控制全绿。

## 下一步

1. 用 `/architect` 设计核心领域模型（中间统一轨迹 IR、双向工具映射表、游标与分叉状态机）。
2. 实现同目录 `Codex <-> Antigravity 2.30+` 双向高保真转换器（覆盖 `gemini_coder.Step` 14/15/23/101/132 与 Codex `rollout-*.jsonl` `response_item` / `event_msg`）及伴生文件（`transcript.jsonl`、`annotations/<id>.pbtxt`、`agyhub_summaries_proto.pb`）。
3. 实现在线 RPC 与离线受控落盘双模同步命令并完成端到端续聊验收。

## 记录

- Kotone 判断：a607d12864a349a18ddb0d63345bd3d6；来源：antigravity。
- 2026-11-10：完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md` / `AGENTS.md` / `NOTES.md` 方案初始化。

## 坑

- Antigravity Desktop 2.30+（Hub 架构）使用 `~/.gemini/antigravity/conversation_summaries.db`、`agyhub_summaries_proto.pb` 与 `~/.gemini/config/projects/<uuid>.json`，不再使用旧版 IDE 2.5.5 的 `User/globalStorage/state.vscdb`。
- Antigravity Hub 前端仅展示 `source` 为 `0`、`18`、`20` 的会话，`source = 17`（CLI）会被隐藏到 `externalSummaries`；且标题必须同时写入 `TrajectorySummary.annotations.title`（field 15 -> 1）与 `annotations/<id>.pbtxt`。
- `agy --conversation=<id>` 强依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/` 目录存在，缺失会导致 CLI 挂起或报错。
- Antigravity 2.30+ 的 `steps.step_payload` 二进制并非旧版 `CortexTrajectoryStep`，而是 `third_party/gemini_coder/proto/trajectory.proto` 中的 `gemini_coder.Step`：其中 `step_type = 14` 对应 `Step.user_input`（field 19），`step_type = 15` 对应 `Step.planner_response`（field 20），`step_type = 23` 对应 `Step.checkpoint`（field 30），`step_type = 93/94` 在 `Step` oneof 中分别为 `task_boundary`（field 93）与 `notify_user`（field 94），`step_type = 101` 对应 `Step.system_message`（field 114），`step_type = 132` 对应 `Step.generic`（field 140，内嵌 `exa.cortex.core.tools.*` `google.protobuf.Any` 载荷）。
- 本机独立 CLI `agy` 安装在 `~/.local/bin/agy`（而非旧文档假设的 `~/.local/libexec/agy/agy`），脚本须通过 `ANTIGRAVITY_CLI_BIN` 或 `shutil.which("agy")` 动态发现。
