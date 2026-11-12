# codex-agy-sync（Codex 与 Antigravity 会话同频桥）

实现 OpenAI Codex 0.166+（Desktop 与 CLI）与 Google Antigravity 2.30+（Hub Desktop 与 `agy` CLI）在同一项目文件夹下对话、思考摘要、工具调用与执行结果的最大颗粒度对齐、双向导入与无缝接续。

## 背景与核心问题

在同一个代码仓库中交替使用 Codex 与 Antigravity 时，任意一端做到一半的工作往往卡在私有轨迹格式与桌面侧栏隔离上。
- Codex 将会话存为 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（及 `.jsonl.zst`）与 `~/.codex/state_5.sqlite`（`threads`、`projects`、`project_roots`）。
- Antigravity 2.30+ Desktop（Hub 架构）将会话存为 `~/.gemini/antigravity/conversations/<cascade_id>.db`（内含 `steps.step_payload` Protobuf 二进制与 `trajectory_metadata_blob`）、`conversation_summaries.db`、`agyhub_summaries_proto.pb`、`annotations/<id>.pbtxt` 与 `brain/<id>/.system_generated/logs/transcript.jsonl`，并通过 `~/.gemini/config/projects/<uuid>.json` 绑定左侧项目栏。

现有开源工具或仅支持 `antigravity-cli` 目录，或过滤掉了全部工具调用与输出，或仍停留在旧版 IDE 2.5.5 的 `state.vscdb` 结构。本项目通过 REA（`reverse-engineer-anything`）与 REAP（`reap-prompts`）直接从本机新版客户端提取真实 Protobuf 描述符与 RPC 契约，实现新版本 100% 兼容的同目录细粒度对齐。

## 可借鉴的六大参考项目

| 项目 | 位置 / 仓库 | 借鉴模块与核心价值 |
| --- | --- | --- |
| **1. Lagrange** | `https://github.com/varungupta3009/Lagrange` (`lagrange.py`) | 破解 Antigravity Hub 2.30+ 架构的 `source = 0` 可见性规则、`TrajectoryMetadata.workspace_uris` (field 7) + `project_id` (field 18) 与 `~/.gemini/config/projects/<uuid>.json` 绑定、`annotations/<id>.pbtxt` 标题优先级、`agyhub_summaries_proto.pb` 离线重建与子代理 (field 17) 嵌套保持 |
| **2. Antigravity Trajectory Extractor** | `https://github.com/jijiamoer/antigravity-trajectory-extractor` | 在线发现运行中的 `language_server` 并调用 `GetCascadeTrajectory` RPC 直接获取最新 `steps` 与运行状态，作为 `Antigravity -> Codex` 在线无锁读取通道 |
| **3. Antigravity Database Manager** | `https://github.com/ag-donald/Antigravity-Database-Manager` (`docs/schema.proto`, `src/core/protobuf.py`) | 详尽的 `StepWrapper`、`TrajectoryStep`、`ToolCall` (`call_id`, `tool_name`, `arguments_json`, `response_blob`)、`AssistantResponse` (field 94) 与 `TaskBoundaryResponse` (field 93) 字段定义 |
| **4. txcript** | `https://github.com/skillsynchq/txcript` (`src/harness/codex.rs`, `src/harness/antigravity.rs`) | Codex `rollout-*.jsonl` 与 Antigravity `steps` 表及 `brain/<id>/.system_generated/logs/transcript.jsonl` 工具级双向转换，以及半途中断回合的工具错误闭合思路 |
| **5. agent-session-bridge & session-migrate** | `https://github.com/Amiyadesi/agent-session-bridge` (`src/agent_session_bridge/antigravity.py`) | `PRAGMA user_version=1` 防客户端旧版迁移清空元数据、`presence/*.lock` 与 `daemon/*.json` PID 存活门禁、`manifest.json` 备份审计以及 Codex `state_5.sqlite` 安全清理 |
| **6. 本机自有项目** | `~/Projects/dsh-conversation-import`、`~/Projects/dsh-antigravity-bridge`、`~/Projects/agent-fleet` | 增量水位线与分叉保护状态机，以及 `agent-fleet` 的 Hub `LanguageServerService` Connect RPC 与 `codex app-server` Unix Socket WebSocket JSON-RPC v2 客户端协议实现 |

## 运行机制与架构选型（`A2 + B2 + C1`）

1. **`A2` 在线回环 RPC 无锁读取与受控离线落盘写入**
   - 读取端支持在线回环 RPC 与 `?mode=ro` 只读磁盘双模路由。Antigravity 在线通道调用 `LanguageServerService/GetCascadeTrajectory` 二进制 Protobuf 接口（仅在内存请求头携带 `x-codeium-csrf-token`），Codex 在线通道连接 Unix Socket WebSocket 执行 `codex app-server` 的 `initialize` 与 `thread/read(includeTurns=true)`。
   - 写入端严格采用 `Lagrange` 风格的受控离线存储落盘。写盘全程持有 `sync.lock` 与 `presence/<id>.lock` 排他 `flock`，确认目标宿主无活跃守护进程、无活跃 `-wal` 且无未完结回合后先生成 SHA-256 `BackupManifest`，任何中途异常自动调用 `BackupManifest.restore()` 原子回滚，并在 `SyncReceipt.transport_used` 如实报告实际使用的读取传输模式（`online_rpc` 或 `offline_storage`）。
2. **`B2` 严格完结回合水位线与显式半途回合抢救**
   - 默认仅在工具调用全部闭合的已完成回合边界推进同步游标，自动跳过流式未完结回合。
   - 显式传入 `--include-interrupted` 时，若源宿主已停止运行（非活跃流式状态），按 `txcript` 风格为悬空工具调用补齐原生取消错误结果并闭合半途回合，保持重跑幂等并在对端续聊回传时平滑替换抢救锚点。
3. **`C1` 纯按需显式 CLI 触发（零后台轮询守护进程）**
   - 通过 `bin/codex-agy-sync` 提供五个确定性子命令（`pair`、`status`、`inspect`、`sync-session`、`sync-workspace`），按需显式执行，不驻留任何后台轮询守护进程。

## 模块化代码结构

`src/codex_agy_sync/` 采用职责单一的分层包结构，所有单文件行数与函数长度均受 `./scripts/check` AST 门禁约束：
- `_wire.py` 提供全仓唯一的 64 位 Protobuf varint、wire 字段解析与时间戳编解码原语。
- `payloads.py` 定义 16 类结构化工具载荷（`CanonicalToolPayload`）与未知工具透传信封（`OpaqueToolPayload`）。
- `ledger.py` 定义强类型标识、`SubagentLineage`、索引化事件账本 `TrajectoryLedger`、5 类伴生存储纯投影以及 `B2` 半途回合闭合纯函数。
- `codecs/`（7 个子模块 `schema_tags.py`、`tool_payloads.py`、`tools.py`、`steps.py`、`rollout.py`、`projections.py`、`__init__.py`）实现表驱动的 `gemini_coder.Step` Protobuf 与 Codex `.jsonl` / `.jsonl.zst` / RPC 线程编解码。
- `bridge/`（7 个子模块 `paths_and_locks.py`、`registry.py`、`state_machine.py`、`rpc.py`、`transport.py`、`sync.py`、`__init__.py`）实现双端项目配对、锁守卫与备份回滚、纯状态机、`A2` 在线回环 RPC 读取客户端、多存储落盘自愈与对称分叉门面 `SyncBridge`。
- `cli.py` 与 `bin/codex-agy-sync` 暴露命令行入口。

## 契约提取与验证入口

- `python3 scripts/extract_schema.py` 从本机 `/Applications/Antigravity.app/Contents/Resources/bin/language_server`、`agy` CLI 与 `app.asar` 提取编译态 `FileDescriptorProto` 与侧栏可见性规则，生成 `schema/antigravity_2_30_schema.json`、`schema/antigravity_2_30.proto` 与 `schema/sidebar_visibility_rules.json`。
- `python3 scripts/verify_session_mapping.py` 以 `SQLITE_OPEN_READONLY`（`?mode=ro` + `PRAGMA query_only=ON`）只读解码本机真实 Antigravity `.db` 会话与 Codex `rollout-*.jsonl`，校验 `unknown_wire_fields_count == 0` 且宿主数据库零变动。
- `./scripts/check` 一键运行描述符完整性校验、真实会话只读无损解码、包结构与函数规模 AST 检查、路径与 `csrfToken` 卫生扫描及全量行为回归测试。
