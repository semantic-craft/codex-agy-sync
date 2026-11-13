# codex-agy-sync（Codex 与 Antigravity 会话同频桥）

在同一项目中按需交接 Codex 与 Antigravity Desktop 的会话历史，并在目标 Desktop 中继续聊天。没有后台同步进程，也不做实时镜像。旧版 `sync-session` 保留受控离线增量同步。

## 背景与核心问题

在同一个代码仓库中交替使用 Codex 与 Antigravity 时，任意一端做到一半的工作往往卡在私有轨迹格式与桌面侧栏隔离上。
- Codex 将会话存为 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（及 `.jsonl.zst`）与 `~/.codex/state_5.sqlite`（`threads`、`projects`、`project_roots`）。
- Antigravity 2.30+ Desktop（Hub 架构）将会话存为 `~/.gemini/antigravity/conversations/<cascade_id>.db`（内含 `steps.step_payload` Protobuf 二进制与 `trajectory_metadata_blob`）、`conversation_summaries.db`、`agyhub_summaries_proto.pb`、`annotations/<id>.pbtxt` 与 `brain/<id>/.system_generated/logs/transcript.jsonl`，并通过 `~/.gemini/config/projects/<uuid>.json` 绑定左侧项目栏。

本项目复用现有账本与编码器，参考开源 Desktop 导入项目的做法，并以本机真实 Desktop 验证兼容性。存储转换成功、侧栏可见和模型能连续续聊是三项不同的验收，不能互相替代。

## Desktop 按需交接

两端先在 Desktop 中注册同一个项目目录，完成当前回合后执行：

```bash
./bin/codex-agy-sync handoff --workspace "$PWD" \
  --direction codex_to_agy --source-id <codex-thread-id>
./bin/codex-agy-sync handoff --workspace "$PWD" \
  --direction agy_to_codex --source-id <agy-cascade-id>
```

收据返回目标 `target_id` 和 `status`。在目标 Desktop 打开对应标题的会话继续工作；下次换端时，将正在使用的会话 ID 作为 `--source-id`。可用 `--title` 指定新标题。

- 每次交接生成带累计历史的新会话，旧会话保留。相同源历史再次执行返回 `no_op`，复用已有目标。
- AGY 源端先用 RPC 确认空闲，再以只读 SQLite 提取完整工具历史；目标端在隔离目录生成完整新数据库与专属文件，以不覆盖方式发布新 ID，再由原生 `GetCascadeTrajectory` 加载和注册。桥不会直接写运行中的摘要数据库。
- Codex 由原生 `thread/fork(path)` 接管暂存 rollout，设置项目和标题，退订并等待线程卸载后交给 Desktop。宿主默认卸载等待可能约一分钟。
- 交接保留可见消息。工具名称、参数、结果以及思考与检查点的可读摘要，作为带说明的普通历史文本保留；模型私有推理状态、原生检查点、签名、运行环境注入、正在运行的进程和权限不跨宿主移植。
- 活跃或未完成回合拒绝交接。导入结果不确定时保留 `pending` 收据和已知目标 ID，不自动重建第二份会话。

`handoff` 与 `sync-session` 的语义不同。后者仍要求目标离线，按原配对会话增量写入；前者面向保持 Desktop 运行的按需新会话交接。

本次复用的直接依据：

| 上游 | 复用范围 |
| --- | --- |
| [agentsview](https://github.com/kenn-io/agentsview/blob/main/internal/parser/antigravity.go) | Desktop SQLite/Protobuf 读取结构；它是读取器，不承担原生写入 |
| [antigravity-chat-manger](https://github.com/M7mdJs/antigravity-chat-manger/blob/main/src/recovery.py) | 通过 `GetCascadeTrajectory` 让宿主载入导入会话 |
| [antigravity-move-chat-to-projects](https://github.com/Jabir-A-H/antigravity-move-chat-to-projects/blob/main/transplant_chat.py) | 区分会话内容与原生身份元数据；最终路径无需占位会话 |
| [Antigravity-Database-Manager](https://github.com/ag-donald/Antigravity-Database-Manager/blob/main/src/core/summaries_repair.py) | 宿主持久化摘要的接口参考；本机实测无需额外调用摘要写入 |
| [OpenAI app-server](https://github.com/openai/codex/tree/main/codex-rs/app-server) | 原生 rollout 分叉、项目归属、标题和线程卸载 |

## 可借鉴的六大参考项目

| 项目 | 位置 / 仓库 | 借鉴模块与核心价值 |
| --- | --- | --- |
| **1. Lagrange** | `https://github.com/varungupta3009/Lagrange` (`lagrange.py`) | 破解 Antigravity Hub 2.30+ 架构的 `source = 0` 可见性规则、`TrajectoryMetadata.workspace_uris` (field 7) + `project_id` (field 18) 与 `~/.gemini/config/projects/<uuid>.json` 绑定、`annotations/<id>.pbtxt` 标题优先级、`agyhub_summaries_proto.pb` 离线重建与子代理 (field 17) 嵌套保持 |
| **2. Antigravity Trajectory Extractor** | `https://github.com/jijiamoer/antigravity-trajectory-extractor` | 在线发现运行中的 `language_server` 并调用 `GetCascadeTrajectory` RPC 直接获取最新 `steps` 与运行状态，作为 `Antigravity -> Codex` 在线无锁读取通道 |
| **3. Antigravity Database Manager** | `https://github.com/ag-donald/Antigravity-Database-Manager` (`docs/schema.proto`, `src/core/protobuf.py`) | 详尽的 `StepWrapper`、`TrajectoryStep`、`ToolCall` (`call_id`, `tool_name`, `arguments_json`, `response_blob`)、`AssistantResponse` (field 94) 与 `TaskBoundaryResponse` (field 93) 字段定义 |
| **4. txcript** | `https://github.com/skillsynchq/txcript` (`src/harness/codex.rs`, `src/harness/antigravity.rs`) | Codex `rollout-*.jsonl` 与 Antigravity `steps` 表及 `brain/<id>/.system_generated/logs/transcript.jsonl` 工具级双向转换，以及半途中断回合的工具错误闭合思路 |
| **5. agent-session-bridge & session-migrate** | `https://github.com/Amiyadesi/agent-session-bridge` (`src/agent_session_bridge/antigravity.py`) | `PRAGMA user_version=1` 防客户端旧版迁移清空元数据、`presence/*.lock` 与 `daemon/*.json` PID 存活门禁、`manifest.json` 备份审计以及 Codex `state_5.sqlite` 安全清理 |
| **6. 本机自有项目** | `~/Projects/dsh-conversation-import`、`~/Projects/dsh-antigravity-bridge`、`~/Projects/agents-everywhere` | 增量水位线与分叉保护状态机，以及 `agents-everywhere` 的 Antigravity 原生运行时集成与 Codex app-server 接入；当前实现以本仓 `bridge/rpc.py` 为准 |

## 运行机制与架构选型（`A2 + B2 + C1`）

1. **`A2` 在线回环 RPC 无锁读取与受控离线落盘写入**
   - 读取端支持在线回环 RPC 与 `?mode=ro` 只读磁盘双模路由。Antigravity 在线通道调用 `LanguageServerService/GetCascadeTrajectory` 二进制 Protobuf 接口（仅在内存请求头携带 `x-codeium-csrf-token`），Codex 在线通道连接 Unix Socket WebSocket 执行 `codex app-server` 的 `initialize` 与 `thread/read(includeTurns=true)`。
   - `sync-session` 写入端采用受控离线存储落盘。写盘全程持有 `sync.lock` 与 `presence/<id>.lock` 排他 `flock`，确认目标宿主无活跃守护进程、无活跃 `-wal` 且无未完结回合后先生成 SHA-256 `BackupManifest`，任何中途异常自动调用 `BackupManifest.restore()` 原子回滚，并在 `SyncReceipt.transport_used` 如实报告实际使用的读取传输模式（`online_rpc` 或 `offline_storage`）。
2. **`B2` 严格完结回合水位线与显式半途回合抢救**
   - 默认仅在工具调用全部闭合的已完成回合边界推进同步游标，自动跳过流式未完结回合。
   - 显式传入 `--include-interrupted` 时，若源宿主已停止运行（非活跃流式状态），按 `txcript` 风格为悬空工具调用补齐原生取消错误结果并闭合半途回合，保持重跑幂等并在对端续聊回传时平滑替换抢救锚点。
3. **`C1` 纯按需显式 CLI 触发（零后台轮询守护进程）**
   - 支持通过 `pip install .` 或 `pipx install .` 安装标准控制台命令 `codex-agy-sync`，亦可直接执行仓库内 `bin/codex-agy-sync`。
   - 提供按需子命令（`pair`、`status`、`inspect`、`handoff`、`sync-session`、`sync-workspace`），按需显式执行，不驻留任何后台轮询守护进程。

## 模块化代码结构

`src/codex_agy_sync/` 采用职责单一的分层包结构，所有单文件行数与函数长度均受 `./scripts/check` AST 门禁约束：
- `_wire.py` 提供全仓唯一的 64 位 Protobuf varint、wire 字段解析与时间戳编解码原语。
- `payloads.py` 定义 16 类结构化工具载荷（`CanonicalToolPayload`）与未知工具透传信封（`OpaqueToolPayload`）。
- `ledger.py` 定义强类型标识、`SubagentLineage`、索引化事件账本 `TrajectoryLedger`、5 类伴生存储纯投影以及 `B2` 半途回合闭合纯函数。
- `codecs/`（核心子模块 `schema_tags.py`、`tool_payloads.py`、`tools.py`、`steps.py`、`rollout.py`、`projections.py`、`__init__.py`）实现表驱动的 `gemini_coder.Step` Protobuf 与 Codex `.jsonl` / `.jsonl.zst` / RPC 线程编解码。
- `bridge/`（核心子模块 `paths_and_locks.py`、`registry.py`、`state_machine.py`、`rpc.py`、`native_codex.py`、`handoff.py`、`transport.py`、`sync.py`、`__init__.py`）实现双端项目配对、锁守卫与备份回滚、纯状态机、`A2` 在线回环 RPC 读取客户端、多存储落盘自愈与对称分叉门面 `SyncBridge`。
- `cli.py` 与 `bin/codex-agy-sync` 暴露命令行入口。

## 契约提取与验证入口

- `python3 scripts/extract_schema.py` 从本机 `/Applications/Antigravity.app/Contents/Resources/bin/language_server`、`agy` CLI 与 `app.asar` 提取编译态 `FileDescriptorProto` 与侧栏可见性规则，生成 `schema/antigravity_2_30_schema.json`、`schema/antigravity_2_30.proto` 与 `schema/sidebar_visibility_rules.json`。
- `python3 scripts/verify_session_mapping.py` 以 `SQLITE_OPEN_READONLY`（`?mode=ro` + `PRAGMA query_only=ON`）只读解码本机真实 Antigravity `.db` 会话与 Codex `rollout-*.jsonl`，校验 `unknown_wire_fields_count == 0` 且宿主数据库零变动。
- `./scripts/check` 一键运行描述符完整性校验、真实会话只读无损解码、包结构与函数规模 AST 检查、路径与 `csrfToken` 卫生扫描及全量行为回归测试。
