# codex-agy-sync（Codex 与 Antigravity 会话同频桥）

在同一项目中按需交接 Codex 与 Antigravity Desktop 的会话历史，并在目标 Desktop 中继续聊天。没有后台同步进程，也不做实时镜像。旧版 `sync-session` 保留受控离线增量同步。

## 背景与核心问题

在同一个代码仓库中交替使用 Codex 与 Antigravity 时，任意一端做到一半的工作往往卡在私有轨迹格式与桌面侧栏隔离上。
- Codex 将会话存为 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（及 `.jsonl.zst`）与 `~/.codex/state_5.sqlite`（`threads`、`projects`、`project_roots`）。
- Antigravity 2.30+ Desktop（Hub 架构）将会话存为 `~/.gemini/antigravity/conversations/<cascade_id>.db`（内含 `steps.step_payload` Protobuf 二进制与 `trajectory_metadata_blob`）、`conversation_summaries.db`、`agyhub_summaries_proto.pb`、`annotations/<id>.pbtxt` 与 `brain/<id>/.system_generated/logs/transcript.jsonl`，并通过 `~/.gemini/config/projects/<uuid>.json` 绑定左侧项目栏。

本项目复用现有账本与编码器，参考开源 Desktop 导入项目的做法，并以本机真实 Desktop 验证兼容性。存储转换成功、侧栏可见和模型能连续续聊是三项不同的验收，不能互相替代。

## Desktop 按需交接

两端在 Desktop 中注册同一个项目目录（或仅缺离线宿主注册时加 `--pair-if-offline` 自动补齐），完成当前回合后执行：

```bash
./bin/codex-agy-sync handoff --workspace "$PWD" --chain
./bin/codex-agy-sync handoff --workspace "$PWD" --latest
./bin/codex-agy-sync handoff --workspace "$PWD" --source-id <codex-or-agy-uuid>
./bin/codex-agy-sync handoff --workspace "$PWD" --pick
```

- **默认与零参数/精简参数体验**：传入 `--source-id` 且省略 `--direction` 时自动扫描两侧宿主存储推断方向（仅在两侧同时存在相同 UUID 或均未找到时报错）；`--latest` 跳过未继续对话的刚接力目标（`is_untouched_baton_target`），选取最新非子代理已完成会话；`--chain` 沿着工作区活动交接链（`source_id -> target_id -> ...`）自动推进或重交接已更新的源端，未继续对话时直接返回 `no_op`；`--pick`（以及 TTY 下省略所有选择参数时）仅向 `stderr` 输出候选菜单并从 `stdin` 读选择，保持 `stdout` 为纯 JSON（非 TTY 快速报错并提示 `--latest` / `--chain` / `--source-id` / `--pick`）。
- **未配对诊断与离线安全配对**：任一端未注册时在创建 `handoffs/` 目录前即拒绝并给出精确宿主诊断及修复命令（CLI `handoff` 与 `pair` 在未配对或被活跃锁阻塞时均以退出码 `2` 向 `stderr` 输出结构化 JSON 负载）；加 `--pair-if-offline` 时 `guarded_pair_workspace` 仅在缺失宿主离线且无 WAL 锁时持排他锁并备份后补齐配对，若两端已配对则零锁零写入直接返回。
- **兼容性**：仍支持显式 `--direction codex_to_agy|agy_to_codex --source-id <id> --title <title>`。旧版 `handoffs/*.json` 收据无新字段（`created_at_iso`、`chain_id`、`chained_from_source_id`、`chain_depth`、`target_seed_turn_count`、`target_seed_text_digest`）也可直接读取。

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

## Stage 1 本地工作台（`serve`）

通过按需启停的本地工作台在浏览器汇总检索与交接 Antigravity / Codex 会话，不驻留后台服务、不向运行中数据库写盘、不向远端会话自动发送消息：

- **启停与租约管理**：
  - 启动：`codex-agy-sync serve [-w <path>] [--ui <Main.html>] [--port 8765] [--idle-timeout 1800] [--max-lifetime 14400] [--open]`，仅绑定 `127.0.0.1` 并输出启动 JSON（`--port` 默认 `8765`，传 `0` 自动选择空闲端口；省略 `--ui` 时依次查找随包 `workbench/ui/index.html` / `workbench/workbench.html`，若均不存在则 `GET /` 返回 `404 ui_not_found`，本地搭配独立原型 HTML 使用需显式传入 `--ui <Main.html>`）。
  - 状态与停止：`codex-agy-sync serve --status` 查询当前租约与 `/api/health`；`codex-agy-sync serve --stop` 发送 `SIGTERM` 干净退出并清理 `<sync-state-dir>/workbench/server.json`（文件权限 `0o600`，不含任何鉴权 token）。
- **安全边界与敏感路径隔离**：
  - 所有 `/api/*`（除 `/api/health`）均要求 `X-Workbench-Token` 与回环 `Host` / `Origin`（`127.0.0.1`、`localhost`、`[::1]`）/ `Sec-Fetch-Site` 校验，未授权返回 `401` / `403`。
  - 工作区自动发现严格跳过 `$HOME/Archive`、`$HOME/.private_keys`（含软链逃逸）及 `outside-of-project`。
- **Stage 1 能力范围**：
  - 支持多工作区并查集聚合、跨端正文检索、引用片段组织只复制 Markdown 草稿（`stage_1_copy_only`，`delivery_executed: false`，`POST /api/send` 返回 `404`），以及一键触发真实 `handoff` 并在交接失败时返回 `422 unpaired_workspace` / `409 sync_busy_lock` 结构化诊断。

## 运行机制与架构选型（`A2 + B2 + C1`）

1. **`A2` 在线回环 RPC 无锁读取与受控离线落盘写入**
   - 读取端支持在线回环 RPC 与 `?mode=ro` 只读磁盘双模路由。Antigravity 在线通道调用 `LanguageServerService/GetCascadeTrajectory` 二进制 Protobuf 接口（仅在内存请求头携带 `x-codeium-csrf-token`），Codex 在线通道连接 Unix Socket WebSocket 执行 `codex app-server` 的 `initialize` 与 `thread/read(includeTurns=true)`。
   - `sync-session` 写入端采用受控离线存储落盘。写盘全程持有 `sync.lock` 与 `presence/<id>.lock` 排他 `flock`，确认目标宿主无活跃守护进程、无活跃 `-wal` 且无未完结回合后先生成 SHA-256 `BackupManifest`，任何中途异常自动调用 `BackupManifest.restore()` 原子回滚，并在 `SyncReceipt.transport_used` 如实报告实际使用的读取传输模式（`online_rpc` 或 `offline_storage`）。
2. **`B2` 严格完结回合水位线与显式半途回合抢救**
   - 默认仅在工具调用全部闭合的已完成回合边界推进同步游标，自动跳过流式未完结回合。
   - 显式传入 `--include-interrupted` 时，若源宿主已停止运行（非活跃流式状态），按 `txcript` 风格为悬空工具调用补齐原生取消错误结果并闭合半途回合，保持重跑幂等并在对端续聊回传时平滑替换抢救锚点。
3. **`C1` 纯按需显式 CLI 触发（零后台轮询守护进程）**
   - 支持通过 `pip install .` 或 `pipx install .` 安装标准控制台命令 `codex-agy-sync`，亦可直接执行仓库内 `bin/codex-agy-sync`。
   - 提供按需子命令（`pair`、`status`、`inspect`、`sync-session`、`handoff`、`sync-workspace`、`serve`），按需显式执行，不驻留任何后台轮询守护进程。

## 模块化代码结构

`src/codex_agy_sync/` 采用职责单一的分层包结构，所有单文件行数与函数长度均受 `./scripts/check` AST 门禁约束：
- `_wire.py` 提供全仓唯一的 64 位 Protobuf varint、wire 字段解析与时间戳编解码原语。
- `payloads.py` 定义 16 类结构化工具载荷（`CanonicalToolPayload`）与未知工具透传信封（`OpaqueToolPayload`）。
- `ledger.py` 定义强类型标识、`SubagentLineage`、索引化事件账本 `TrajectoryLedger`、5 类伴生存储纯投影以及 `B2` 半途回合闭合纯函数。
- `codecs/`（核心子模块 `schema_tags.py`、`tool_payloads.py`、`tools.py`、`steps.py`、`rollout.py`、`projections.py`、`__init__.py`）实现表驱动的 `gemini_coder.Step` Protobuf 与 Codex `.jsonl` / `.jsonl.zst` / RPC 线程编解码。
- `bridge/`（核心子模块 `paths_and_locks.py`、`registry.py`、`state_machine.py`、`rpc.py`、`native_codex.py`、`handoff.py`、`handoff_resolve.py`、`_handoff_scan.py`、`rescue_and_fork.py`、`transport.py`、`sync.py`、`__init__.py`）实现双端项目配对、锁守卫与备份回滚、纯状态机、`A2` 在线回环 RPC 读取客户端、按需 `handoff` 解析/接力链诊断、多存储落盘自愈与对称分叉门面 `SyncBridge`。
- `workbench/`（核心子模块 `models.py`、`_discovery.py`、`indexer.py`、`service.py`、`_lease.py`、`_handler.py`、`server.py`、`__init__.py`）实现 Stage 1 本地工作台的工作区发现、时间线聚类、Markdown 草稿渲染、`0o600` 租约管理与回环 HTTP/UI 托管。
- `cli.py` 与 `bin/codex-agy-sync` 暴露命令行入口。

## 契约提取与验证入口

- `python3 scripts/extract_schema.py` 从本机 `/Applications/Antigravity.app/Contents/Resources/bin/language_server`、`agy` CLI 与 `app.asar` 提取编译态 `FileDescriptorProto` 与侧栏可见性规则，生成 `schema/antigravity_2_30_schema.json`、`schema/antigravity_2_30.proto` 与 `schema/sidebar_visibility_rules.json`。
- `python3 scripts/verify_session_mapping.py` 以 `SQLITE_OPEN_READONLY`（`?mode=ro` + `PRAGMA query_only=ON`）只读解码本机真实 Antigravity `.db` 会话与 Codex `rollout-*.jsonl`，校验 `unknown_wire_fields_count == 0` 且宿主数据库零变动。
- `python3 skills/verify-codex-agy-sync/scripts/workbench_verify.py doctor --prototype "<Main.html>"` 与 `suite --prototype "<Main.html>"` 在隔离沙盒 + 回环 AGY RPC 桩下通过 `agent-browser` 端到端验收 `serve` 启停、安全门禁、跨端检索、引用草稿 IME 保护及一键交接。
- `./scripts/check` 一键运行描述符完整性校验、真实会话只读无损解码、包结构与函数规模 AST 检查、路径与 `csrfToken` 卫生扫描及全量行为回归测试。
