# codex-agy-sync（Codex 与 Antigravity 会话同频桥）

在同一项目中按需交接 Codex 与 Antigravity Desktop 的本地会话历史，优先用于某端额度中断后的极速抢救与接力续聊。提供原生 macOS 菜单栏应用 `CodexAgyHandoff.app` 与按需 CLI `codex-agy-sync handoff`，零后台轮询、不考虑远端会话、不向运行中数据库裸写。Stage 1 浏览器工作台（`serve`）与旧版离线同步（`sync-session` / `sync-workspace`）保留为冻结维护入口。

## 背景与核心问题

在同一个代码仓库中交替使用 Codex 与 Antigravity 时，任意一端做到一半的工作往往卡在私有轨迹格式与桌面侧栏隔离上。
- Codex 将会话存为 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（及 `.jsonl.zst`）与 `~/.codex/state_5.sqlite`（`threads`、`projects`、`project_roots`）。
- Antigravity 2.30+ Desktop（Hub 架构）将会话存为 `~/.gemini/antigravity/conversations/<cascade_id>.db`（内含 `steps.step_payload` Protobuf 二进制与 `trajectory_metadata_blob`）、`conversation_summaries.db`、`agyhub_summaries_proto.pb`、`annotations/<id>.pbtxt` 与 `brain/<id>/.system_generated/logs/transcript.jsonl`，并通过 `~/.gemini/config/projects/<uuid>.json` 绑定左侧项目栏。

本项目复用现有账本与编码器，参考开源 Desktop 导入项目的做法，并以本机真实 Desktop 验证兼容性。存储转换成功、侧栏可见和模型能连续续聊是三项不同的验收，不能互相替代。

## macOS 菜单栏极速接力（CodexAgyHandoff.app）

针对额度中断后的即时切端场景，仓库内 `macos/` 提供原生 Swift 6 菜单栏应用 `CodexAgyHandoff.app`（`LSUIElement` 常驻、零后台定时轮询）。

```bash
./scripts/build_menubar_app.sh --install
open "$HOME/Applications/CodexAgyHandoff.app"
```

- **零轮询按需刷新**：仅在点击菜单栏图标或按下全局快捷键 `⌃⌥H` 展开面板（以及点击刷新）时触发一次只读 `./bin/codex-agy-sync menubar-feed --limit 6`；日常待机 0% 轮询占用。
- **中断急救首卡与一键交接**：`menubar-feed` 按工作区边界快速扫描并结合在线回环 `IDLE` 状态识别已停止的半途中断会话（`interrupted_stopped`）与已完成会话（`completed_ready`）；主按钮直接执行 `handoff --workspace <path> --source-id <id> --auto-pair --include-interrupted`（若处于 `pending_reconcile` 则执行 `--reconcile`），并在交接完成后自动拉起目标端 `/Applications/ChatGPT.app`（`com.openai.codex`）或 `/Applications/Antigravity.app`（`com.google.antigravity`）。
- **CLI 可执行文件发现顺序**：应用按 `CODEX_AGY_SYNC_BIN` 环境变量、`UserDefaults`（`cliExecutablePath`，可在界面通过 `NSOpenPanel` “选择 CLI…” 指定）、`$PATH`、`~/.local/bin/codex-agy-sync`、`/opt/homebrew/bin/codex-agy-sync` 与 `/usr/local/bin/codex-agy-sync` 顺序解析 `codex-agy-sync`。
- **范围收敛说明**：不考虑远端会话与 Stage 2 `POST /api/send` 自动投递；日常切端以 `CodexAgyHandoff.app` 与 `codex-agy-sync handoff` 为主入口，`serve` 与 `sync-session` / `sync-workspace` 仅作为冻结维护入口保留。

## Desktop 按需交接

两端在 Desktop 中注册同一个项目目录；任一端缺失注册时，可执行 `codex-agy-sync pair`（优先在线 RPC 注册运行中宿主，离线端回退受锁保护写盘），或在 `handoff` 中直接传入 `--auto-pair`（优先在线 RPC，离线安全回退）或 `--pair-if-offline`（严格要求缺失端离线无锁）。完成当前回合后执行：

```bash
./bin/codex-agy-sync handoff --workspace "$PWD" --chain
./bin/codex-agy-sync handoff --workspace "$PWD" --latest --auto-pair
./bin/codex-agy-sync handoff --workspace "$PWD" --source-id <codex-or-agy-uuid>
./bin/codex-agy-sync handoff --workspace "$PWD" --pick
./bin/codex-agy-sync handoff --workspace "$PWD" --include-interrupted --latest
./bin/codex-agy-sync handoff --workspace "$PWD" --reconcile [--source-id <id>] [--dry-run]
./bin/codex-agy-sync menubar-feed [-w <path>] [--limit 6]
```

- **默认与精简参数体验**：传入 `--source-id` 且省略 `--direction` 时自动扫描两侧宿主存储推断方向（仅在两侧同时存在相同 UUID 或均未找到时报错）；`--latest` 跳过未继续对话的刚接力目标（`is_untouched_baton_target`），选取最新非子代理已完成会话；`--chain` 沿着工作区活动交接链（`source_id -> target_id -> ...`）自动推进或重交接已更新的源端，未继续对话时直接返回 `no_op`；`--pick`（以及 TTY 下省略所有选择参数时）仅向 `stderr` 输出候选菜单并从 `stdin` 读选择，保持 `stdout` 为纯 JSON（非 TTY 快速报错并提示 `--latest` / `--chain` / `--source-id` / `--pick`）。
- **自动配对与未配对诊断**：任一端未注册且未指定自动配对时，在创建 `handoffs/` 目录前即拒绝并给出精确宿主诊断及修复命令；传入 `--auto-pair` 时优先通过 `LanguageServerService/CreateProject` 与 `codex app-server` `project/list` / `project/create` 在线注册运行中宿主，并在缺失端离线时回退到 `guarded_pair_workspace`（返回 `pairing_mode`：`already_paired` / `online_rpc` / `hybrid` / `offline_locked`）；传入 `--pair-if-offline` 时仅允许在缺失宿主离线无 WAL 锁时补齐配对（若该端正在运行则拒绝并返回 `sync_busy_lock`）。二者互斥，且两端均已注册时零锁零写入直接返回。
- **半途回合抢救、CLI 转录镜像与滞留收据核对**：
  - `--include-interrupted` 允许在源会话已停止、非活跃流式输出时闭合抢救尾部中断回合，连同可见历史一并交接（活跃运行回合仍拒绝）。
  - 导入 Antigravity 目标时会同步镜像 `brain/<id>/.system_generated/logs/transcript.jsonl` 至 `--agy-cli-data-dir`（或 `$ANTIGRAVITY_CLI_DATA_DIR` / 相邻 `~/.gemini/antigravity-cli`，目录存在时生效）。
  - `--reconcile`（可配 `--source-id <id>` 定向过滤与 `--dry-run` 只读预览）按工作区核对滞留 `pending` 收据：结合目标端 RPC 与只读存储校验目标会话存在性及种子回合数，确认已落盘则晋升为 `status: "ready"` 并清理暂存目录（`promoted`），确认目标从未落盘或不存在则安全删除 `pending` 收据与暂存目录（`cleared`），仅部分落盘或受锁阻塞无法确认时保留原收据等待后续核对（`unresolved`）。
- **状态总览与结构化 CLI 错误**：
  - `codex-agy-sync status` 输出 `"workspace"`、`"sessions"`、`"pairing"`（含 `paired`、`agy_registered`、`codex_registered`、`agy_pid`、`codex_pid`、`agy_project_id`、`codex_project_id`、`agy_daemon_active`、`codex_daemon_active`、`remediation_steps`）、`"handoff_chains"` 与 `"pending_handoffs"`。
  - CLI 业务与运行时校验失败（如未配对、活跃锁、滞留 `pending` 收据、目标宿主 RPC 不可用或参数值校验错误）统一以退出码 `2` 向 `stderr` 输出结构化 JSON（含 `"error"` 与 `"message"`，并按场景附带 `"diagnosis"` / `"remediation"` / `"host"` / `"receipt"` / `"cause"`），不打印裸 traceback；`argparse` 命令行参数语法冲突（如未传 `--reconcile` 却传 `--dry-run`、`--auto-pair` 与 `--pair-if-offline` 互斥等）则以退出码 `2` 输出标准用法错误提示。
- **兼容性**：仍支持显式 `--direction codex_to_agy|agy_to_codex --source-id <id> --title <title>`。旧版 `handoffs/*.json` 收据无新字段（`created_at_iso`、`chain_id`、`chained_from_source_id`、`chain_depth`、`target_seed_turn_count`、`target_seed_text_digest`、`pairing_mode`）也可直接读取。

收据返回目标 `target_id` 和 `status`。在目标 Desktop 打开对应标题的会话继续工作；下次换端时，将正在使用的会话 ID 作为 `--source-id`。可用 `--title` 指定新标题。

- 每次交接生成带累计历史的新会话，旧会话保留。相同源历史再次执行返回 `no_op`，复用已有目标。
- AGY 源端先用 RPC 确认空闲，再以只读 SQLite 提取完整工具历史；目标端在隔离目录生成完整新数据库与专属文件，以不覆盖方式发布新 ID，再由原生 `GetCascadeTrajectory` 加载和注册。桥不会直接写运行中的摘要数据库。
- Codex 由原生 `thread/fork(path)` 接管暂存 rollout，设置项目和标题，退订并等待线程卸载后交给 Desktop。宿主默认卸载等待可能约一分钟。
- 交接保留可见消息。工具名称、参数、结果以及思考与检查点的可读摘要，作为带说明的普通历史文本保留；模型私有推理状态、原生检查点、签名、运行环境注入、正在运行的进程和权限不跨宿主移植。
- 活跃或未完成回合默认拒绝交接（非活跃已停止会话可加 `--include-interrupted` 抢救）。导入结果不确定时保留 `pending` 收据和已知目标 ID，后续可通过 `handoff --reconcile` 核对恢复，不自动重建第二份会话。

**`[Advanced/Offline]` 维护命令定位**：`sync-session` 与 `sync-workspace` 属于 `[Advanced/Offline]` 维护命令，要求目标宿主离线无锁并按原配对会话增量写入（受目标端次轮原生推理重放边界约束）；日常 Desktop 运行态交替续聊请优先使用 `handoff`。

本次复用的直接依据：

| 上游 | 复用范围 |
| --- | --- |
| [agentsview](https://github.com/kenn-io/agentsview/blob/main/internal/parser/antigravity.go) | Desktop SQLite/Protobuf 读取结构；它是读取器，不承担原生写入 |
| [antigravity-chat-manger](https://github.com/M7mdJs/antigravity-chat-manger/blob/main/src/recovery.py) | 通过 `GetCascadeTrajectory` 让宿主载入导入会话 |
| [antigravity-move-chat-to-projects](https://github.com/Jabir-A-H/antigravity-move-chat-to-projects/blob/main/transplant_chat.py) | 区分会话内容与原生身份元数据；最终路径无需占位会话 |
| [Antigravity-Database-Manager](https://github.com/ag-donald/Antigravity-Database-Manager/blob/main/src/core/summaries_repair.py) | 宿主持久化摘要的接口参考；本机实测无需额外调用摘要写入 |
| [OpenAI app-server](https://github.com/openai/codex/tree/main/codex-rs/app-server) | 原生 rollout 分叉、项目归属、标题和线程卸载 |

## Stage 1 本地工作台（`serve`，冻结维护）

作为 `CodexAgyHandoff.app` 与 `handoff` 之外的冻结维护浏览器工作台，支持按需启停汇总检索与交接，不驻留后台服务、不向运行中数据库写盘、不向远端会话自动发送消息：

- **启停与内置 UI**：
  - 启动：`codex-agy-sync serve [-w <path>] [--ui <path>] [--port 8765] [--idle-timeout 1800] [--max-lifetime 14400] [--open]`，仅绑定 `127.0.0.1` 并输出启动 JSON（`--port` 默认 `8765`，传 `0` 自动选择空闲端口）。`pyproject.toml` 通过 `package-data` 默认随包附带内置工作台页面 `src/codex_agy_sync/workbench/ui/index.html`，省略 `--ui` 时默认提供内置 UI（`codex-agy-sync serve --open` 开箱即用）；传入 `--ui <path>` 时仅使用该指定文件（不回退内置页面）：CLI `serve --ui <缺失路径>` 会在启动服务或打开浏览器前即以退出码 `2`（`file_not_found`）拦截报错，直接实例化 `WorkbenchServer` 并显式配置缺失的 `ui_path` 时 `GET /` 返回 `404 ui_not_found`。
  - 状态与停止：`codex-agy-sync serve --status` 查询当前租约与 `/api/health`；`codex-agy-sync serve --stop` 发送 `SIGTERM` 干净退出并清理 `<sync-state-dir>/workbench/server.json`（文件权限 `0o600`，不含任何鉴权 token）。
- **安全边界与敏感路径隔离**：
  - 所有 `/api/*`（除 `/api/health`）均要求 `X-Workbench-Token` 与回环 `Host` / `Origin`（`127.0.0.1`、`localhost`、`[::1]`）/ `Sec-Fetch-Site` 校验，未授权返回 `401` / `403`。
  - 工作区自动发现严格跳过 `$HOME/Archive`、`$HOME/.private_keys`（含软链逃逸）及 `outside-of-project`。
- **单次扫描与按需水合**：
  - `workbench/catalog.py` 与 `workbench/discovery.py` 单次只读预加载 `state_5.sqlite` 与 `conversation_summaries.db` 并按工作区桶归并，避免跨工作区重复全表/全量 rollout 扫描。
  - `workbench/indexer.py` 对独立工作区卡片采用分层水合（默认 `eager_turn_budget=24` 优先解码聚簇、有交接收据、种子工作区及最新 24 条独立会话，其余卡片保留元数据骨架，点击 `GET /api/works/<work_id>`、按工作区过滤 `GET /api/snapshot?workspace_id=...` 或触发 `GET /api/search` 时并行按需水合并按 `(mtime_ns, size)` 缓存）。
- **Stage 1 能力范围**：
  - 支持多工作区并查集聚合、跨端正文检索、引用片段组织只复制 Markdown 草稿（`stage_1_copy_only`，`delivery_executed: false`；Stage 2 远端投递暂未开启，`POST /api/send` 当前返回 `404`）。
  - 支持工作台一键触发真实 `handoff`（UI `#handoffPairIfOffline` 默认不勾选，手动勾选或点击 `#retryPairHandoff` 时发送 `auto_pair: true`；支持 `#handoffIncludeInterrupted`），未配对且未开启自动配对时返回 `422 unpaired_workspace` 与 `#retryPairHandoff`（自动在线配对重试），滞留挂起时返回 `409 handoff_pending` 与 `#reconcilePendingHandoff`（调用 `POST /api/handoff/reconcile` 核对恢复或清理），忙锁返回 `409 sync_busy_lock`。

## 运行机制与架构选型（`A2 + B2 + C1`）

1. **`A2` 在线回环 RPC 与受控离线存储**
   - 读取端支持在线回环 RPC 与 `?mode=ro` 只读磁盘双模路由。Antigravity 在线通道调用 `LanguageServerService/GetCascadeTrajectory` 二进制 Protobuf 接口（仅在内存请求头携带 `x-codeium-csrf-token`），Codex 在线通道连接 Unix Socket WebSocket 执行 `codex app-server` 的 `initialize` 与 `thread/read(includeTurns=true)`。
   - 项目配对通道通过 `bridge/project_rpc.py` 优先在线注册运行中宿主（`LanguageServerService/CreateProject` 与 `codex app-server` `project/list` / `project/create`），离线端回退到 `guarded_pair_workspace` 排他锁备份写入。
   - `[Advanced/Offline]` `sync-session` 写入端采用受控离线存储落盘。写盘全程持有 `sync.lock` 与 `presence/<id>.lock` 排他 `flock`，确认目标宿主无活跃守护进程、无活跃 `-wal` 且无未完结回合后先生成 SHA-256 `BackupManifest`，任何中途异常自动调用 `BackupManifest.restore()` 原子回滚，并在 `SyncReceipt.transport_used` 如实报告实际使用的读取传输模式（`online_rpc` 或 `offline_storage`）。
2. **`B2` 严格完结回合水位线与显式半途回合抢救**
   - 默认仅在工具调用全部闭合的已完成回合边界推进同步游标，自动跳过流式未完结回合。
   - 显式传入 `--include-interrupted` 时，若源宿主已停止运行（非活跃流式状态），按 `txcript` 风格为悬空工具调用补齐原生取消错误结果并闭合半途回合，保持重跑幂等并在对端续聊回传时平滑替换抢救锚点。
3. **`C1` 纯按需显式 CLI 触发（零后台轮询守护进程）**
   - 支持通过 `pip install .` 或 `pipx install .` 安装标准控制台命令 `codex-agy-sync`，亦可直接执行仓库内 `bin/codex-agy-sync`。
   - 提供日常主线子命令（`handoff`、`menubar-feed`、`status`、`pair`、`inspect`）与冻结维护命令（`serve`、`sync-session`、`sync-workspace`），按需显式执行，不驻留任何后台轮询守护进程。

## 模块化代码结构

`src/codex_agy_sync/` 采用严格的 6 层单向无环分层架构，单文件行数、函数规模与层间边界由 `./scripts/check` 与 `scripts/check_architecture.py` AST 门禁约束；`macos/` 承载独立原生菜单栏应用：
- Tier 0 基础原语与领域模型（`_wire.py`、`workspace.py`、`payloads.py`）提供全仓唯一的 64 位 Protobuf varint / wire 解析、工作区标识归一化与 16 类结构化工具载荷（`CanonicalToolPayload` / `OpaqueToolPayload`）。
- Tier 1 账本与规范事件（`events.py`、`ledger.py`）定义强类型标识、`SubagentLineage`、规范事件、5 类伴生存储纯投影、索引化事件账本 `TrajectoryLedger` 及 `B2` 半途回合闭合纯函数。
- Tier 2 编解码（`codecs/`，含 `schema_tags.py`、`tool_payloads.py`、`tools.py`、`projections.py`、`steps.py`、`rpc_thread.py`、`rollout.py`、`__init__.py`）实现表驱动的 `gemini_coder.Step` Protobuf 与 Codex `.jsonl` / `.jsonl.zst`（统一 `compress_zstd_rollout_bytes`）/ RPC 线程编解码。
- Tier 3 桥接编排（`bridge/`，含 `paths_and_locks.py`、`store_catalog.py`、`registry.py`、`project_rpc.py`、`state_machine.py`、`rpc.py`、`native_codex.py`、`transport.py`、`rescue_and_fork.py`、`sync.py`、`handoff_store.py`、`handoff.py`、`handoff_resolve.py`、`handoff_reconcile.py`、`__init__.py`）实现双端配对诊断、在线项目注册 RPC、锁守卫与备份回滚、`HostTransportPort` 与纯状态机、`A2` 在线回环 RPC、多存储目录扫描与落盘、按需 `handoff` 接力链、滞留收据核对及门面 `SyncBridge`。
- Tier 4 读端聚合与本地工作台（`workbench/`，含 `menubar_feed.py`、`models.py`、`discovery.py`、`catalog.py`、`lease.py`、`indexer.py`、`service.py`、`handler.py`、`server.py`、`ui/index.html`、`__init__.py`）提供零写盘 `menubar-feed` 有界快速候选扫描与 Stage 1 工作台只读发现、分层水合索引、`0o600` 租约管理及回环 HTTP 服务。
- Tier 5 公共入口（`cli.py`、`__init__.py`、`__main__.py` 与 `bin/codex-agy-sync`）暴露命令行与公开 API。
- 原生 macOS 菜单栏包（`macos/Package.swift`，Swift 6 / macOS 14+，`CodexAgyHandoffCore` + `CodexAgyHandoff` + `CodexAgyHandoffTests`）与构建安装脚本 `scripts/build_menubar_app.sh`。

## 契约提取与验证入口

- `python3 scripts/extract_schema.py` 从本机 `/Applications/Antigravity.app/Contents/Resources/bin/language_server`、`agy` CLI 与 `app.asar` 提取编译态 `FileDescriptorProto` 与侧栏可见性规则，生成 `schema/antigravity_2_30_schema.json`、`schema/antigravity_2_30.proto` 与 `schema/sidebar_visibility_rules.json`。
- `python3 scripts/verify_session_mapping.py` 以 `SQLITE_OPEN_READONLY`（`?mode=ro` + `PRAGMA query_only=ON`）只读解码本机真实 Antigravity `.db` 会话与 Codex `rollout-*.jsonl`，校验 `unknown_wire_fields_count == 0` 且宿主数据库零变动。
- `python3 skills/verify-codex-agy-sync/scripts/menubar_verify.py doctor` 与 `suite` 在隔离沙盒与双端回环 RPC 桩下通过 `cua-driver` 端到端验收 `CodexAgyHandoff.app` 中断抢救、在线自动配对、Codex rollout 生成与宿主库零污染。
- `python3 skills/verify-codex-agy-sync/scripts/workbench_verify.py doctor` 与 `suite`（可选传 `--prototype <path>` 覆盖内置 UI）在隔离沙盒 + 回环 AGY RPC 桩下通过 `agent-browser` 端到端验收 `serve` 启停、安全门禁、跨端检索与一键交接。
- `./scripts/check`（含 `scripts/check_architecture.py`）一键运行描述符完整性校验、真实会话只读无损解码、6 层 DAG 与边界 AST 门禁、路径与 `csrfToken` 卫生扫描、Python 回归测试以及 `[5b/5]` macOS Swift XCTest（`swift test --package-path macos --scratch-path out/swift-test-check`）。
