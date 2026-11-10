# codex-agy-sync（Codex 与 Antigravity 会话同频桥）

实现 OpenAI Codex 0.166+（Desktop / CLI）与 Google Antigravity 2.30+（Hub Desktop / `agy` CLI）在同一项目文件夹下对话、思考摘要、工具调用与执行结果的最大颗粒度对齐、双向导入与无缝接续。

## 背景与核心问题

在同一个代码仓库中交替使用 Codex 与 Antigravity 时，任意一端做到一半的工作往往卡在私有轨迹格式与桌面侧栏隔离上：
- Codex 将会话存为 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` 与 `~/.codex/state_5.sqlite`（`threads`、`projects`、`project_roots`）。
- Antigravity 2.30+ Desktop（Hub 架构）将会话存为 `~/.gemini/antigravity/conversations/<cascade_id>.db`（内含 `steps.step_payload` Protobuf 二进制与 `trajectory_metadata_blob`）、`conversation_summaries.db`、`agyhub_summaries_proto.pb`、`annotations/<id>.pbtxt` 与 `brain/<id>/.system_generated/logs/transcript.jsonl`，并通过 `~/.gemini/config/projects/<uuid>.json` 绑定左侧项目栏。

现有开源工具或仅支持 `antigravity-cli` 目录，或过滤掉了全部工具调用与输出，或仍停留在旧版 IDE 2.5.5 的 `state.vscdb` 结构。本项目通过 REA（`reverse-engineer-anything`）与 REAP（`reap-prompts`）直接从本机新版客户端提取真实 Protobuf 描述符与 RPC 契约，实现新版本 100% 兼容的同目录细粒度对齐。

## 可借鉴的六大参考项目

| 项目 | 位置 / 仓库 | 借鉴模块与核心价值 |
| --- | --- | --- |
| **1. Lagrange** | `https://github.com/varungupta3009/Lagrange` (`lagrange.py`) | 唯一破解 Antigravity Hub 2.30+ 架构的实现：`source = 0` 可见性规则、`TrajectoryMetadata.workspace_uris` (field 7) + `project_id` (field 18) 与 `~/.gemini/config/projects/<uuid>.json` 绑定、`annotations/<id>.pbtxt` 标题优先级、`agyhub_summaries_proto.pb` 重建与子代理 (field 17) 嵌套保持 |
| **2. Antigravity Trajectory Extractor** | `https://github.com/jijiamoer/antigravity-trajectory-extractor` | 在线发现运行中的 `language_server` 并调用 `GetCascadeTrajectory` RPC 直接获取解码后的 `steps` 与 `generator_metadata`，作为 `Antigravity -> Codex` 在线无损读取通道 |
| **3. Antigravity Database Manager** | `https://github.com/ag-donald/Antigravity-Database-Manager` (`docs/schema.proto`, `src/core/protobuf.py`) | 详尽的 `StepWrapper`、`TrajectoryStep`、`ToolCall` (`call_id`, `tool_name`, `arguments_json`, `response_blob`)、`AssistantResponse` (field 94) 与 `TaskBoundaryResponse` (field 93) 字段定义 |
| **4. txcript** | `https://github.com/skillsynchq/txcript` (`src/harness/codex.rs`, `src/harness/antigravity.rs`) | Rust 实现的 Codex `rollout-*.jsonl` 与 Antigravity `steps` 表（`CortexStepUserInput` type 14、`CortexStepPlannerResponse` type 15）及 `brain/<id>/.system_generated/logs/transcript.jsonl` 工具级双向转换 |
| **5. agent-session-bridge & session-migrate** | `https://github.com/Amiyadesi/agent-session-bridge` (`src/agent_session_bridge/antigravity.py`) | `PRAGMA user_version=1` 防客户端旧版迁移清空元数据、`presence/*.lock` 与 `daemon/*.json` PID 存活门禁、`.manifest.json` 审计以及 Codex `state_5.sqlite` 安全清理 |
| **6. 本机自有项目** | `~/Projects/dsh-conversation-import`、`~/Projects/dsh-antigravity-bridge`、`~/Projects/agent-fleet` | `dsh-conversation-import/index.js` 的 60s 增量轮询与分叉保护；`agent-fleet/crates/fleet-daemon/src/antigravity.rs` 与 `docs/desktop-rea-dossier.md` 的 Hub `LanguageServerService` Connect RPC 及 `codex app-server` JSON-RPC v2 驱动实现 |

## 三阶段实施方案

### Phase 1：REA / REAP 真实二进制与 ASAR 契约提取
1. 按 `reap-22-go-and-rust-binary-source-recovery` 与 `inspect_go_binary` / `open_binary`，从 `/Applications/Antigravity.app/Contents/Resources/bin/language_server` 与 `~/.local/libexec/agy/agy` 提取内嵌 `exa.language_server_pb` 编译态 Protobuf descriptor，生成标准 `.proto` 定义。
2. 按 `reap-11-electron-and-javascript` 与 `analyze_javascript_application` 分析 `/Applications/Antigravity.app/Contents/Resources/app.asar`，锁定侧栏过滤函数、项目分组与 `StreamCascadeSummariesReactiveUpdates` 触发条件。
3. 按 `reap-15-version-comparison-and-crash-work` 建立版本升级比对脚本，客户端升级时自动检测 schema 漂移。

### Phase 2：同文件夹项目与细粒度步骤对齐模型
- **项目级对齐**：自动扫描 `~/.gemini/config/projects/<uuid>.json` 与 `~/.codex/state_5.sqlite` (`projects` + `project_roots`)，按归一化工作目录路径建立双向映射。
- **步骤级对齐**：
  - 用户输入与附件：`Codex response_item (user)` <-> `AGY step_type = 14 (CortexStepUserInput)`
  - 助手回复与思考摘要：`Codex assistant message + reasoning summary` <-> `AGY step_type = 15 (CortexStepPlannerResponse) + field 94 (AssistantResponse)`
  - 工具调用与输出：`Codex function_call + function_call_output` (`exec_command`, `apply_patch`, `view_file`) <-> `AGY step_type = 15/132 ToolCall` (`run_command`, `edit_file`, `read_file`)
  - 阶段检查点与伴生日志：同步生成 `brain/<id>/.system_generated/logs/transcript.jsonl`、`annotations/<id>.pbtxt` 与 `agyhub_summaries_proto.pb` 条目。

### Phase 3：在线 RPC 与离线落盘双模同步引擎
- **在线读取与热接续**：当 `language_server` 或 `codex app-server` 在线时，优先通过 `GetCascadeTrajectory` 与 `thread/read` 无锁读取最新轨迹。
- **安全落盘与增量游标**：维护 `codex_thread_id <-> agy_cascade_id` 的 `step_index / turn_id` 同步游标，支持把未完成会话的增量轮次单向或双向补齐，检测到目标端已分叉时自动停止覆盖并提示。

## 契约提取与自检入口（Phase 1 已落地）

- `python3 scripts/extract_schema.py`：从本机 `/Applications/Antigravity.app/Contents/Resources/bin/language_server`、`agy` CLI 与 `app.asar` 提取编译态 `FileDescriptorProto` 与侧栏可见性规则，生成：
  - `schema/antigravity_2_30_schema.json`（9 个核心 `.proto` 描述符闭包：704 个消息、136 个枚举、373 个 `LanguageServerService` RPC）
  - `schema/antigravity_2_30.proto`（人类可读的 Protobuf 3 定义）
  - `schema/sidebar_visibility_rules.json`（Antigravity 2.30+ 与 Codex 0.166+ 侧栏可见性与项目绑定契约）
- `python3 scripts/verify_session_mapping.py`：以 `SQLITE_OPEN_READONLY`（`?mode=ro` + `PRAGMA query_only=ON`）只读解码本机真实 Antigravity `.db` 会话与 Codex `rollout-*.jsonl`，校验 `unknown_wire_fields_count == 0` 与数据库 inode/mtime 零变动，并输出双方在项目绑定、用户输入、思考摘要、工具调用与结果上的精确字段映射报告。
- `./scripts/check`：一键运行描述提取确定性校验、真实会话只读无损解码、3 项负向控制测试与路径/凭据卫生检查。
