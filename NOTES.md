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
2. **同文件夹项目自动配对** 自动关联 `~/.gemini/config/projects/<uuid>.json`（`folderUri`）与 `~/.codex/state_5.sqlite`（`projects` + `project_roots`）及 `.codex-global-state.json`（含 `electron-saved-workspace-roots`），确保导入或同步后的会话直接挂载在两侧桌面端左侧对应项目下（Antigravity `source = 0` 且带合法 `project_id`，不落入 `outside-of-project` 或 `default-cli-project`；Codex `preview <> ''`、`cwd <> ''` 且 `archived = 0`）。
3. **细粒度步骤双向对齐** 支持 User 输入（`step_type = 14`）、Planner 回复与思考摘要（`step_type = 15`）、错误消息（`step_type = 17`）、16 类工具调用与返回结果（`step_type = 15/132` 内嵌 `ToolCall`）、阶段边界（`step_type = 23/81/82`）以及伴生日志 `brain/<id>/.system_generated/logs/transcript.jsonl`（含 `antigravity-cli` 镜像）与 `annotations/<id>.pbtxt` 的无损双向映射。
4. **增量接续与分叉保护** 支持在已完成回合边界增量同步，支持 `--include-interrupted` 显式闭合抢救已停止的半途回合（`B2`），记录已同步游标并具备对称分叉派生、持锁写盘与 `BackupManifest.restore()` 自动回滚机制。
5. **端到端实测通过** 项目 `scripts/check` 全量门禁通过，并在隔离沙盒与本机空闲窗口完成端到端验收。

## 当前状态

- `main`（`v0.4.3`，`CodexAgySync` / `sync` 统一命名、实时双语切换、自动更新策略、旧版残余无重脏清理与沙盒路径 `UserDefaults` 测试隔离）：
  - **原生 macOS 菜单栏应用（`CodexAgySync.app`）默认内嵌 Rust CLI 与统一整包更新**：`macos/` 提供 Swift 6（macOS 14+）原生状态栏应用，`./scripts/build_menubar_app.sh` 与 `./scripts/release_macos.sh` 从 `pyproject.toml` / `rust/Cargo.toml` 同步版本、编译 `cas-cli` 并由内而外签名打入 `CodexAgySync.app/Contents/Helpers/codex-agy-sync`（Rust `serve` 透传回 Python CLI，独立 `./bin/codex-agy-sync` 控制台入口保留）；Swift 默认优先调用随包内嵌 Rust 二进制（`内置引擎 / Bundled`），已全面移除 Swift 侧 Python/pip wheel 探测与升级胶水，统一通过 `CodexAgySync-macos.zip`（SHA-256 + `codesign --verify --deep --strict` 校验）或本地源码仓编译同步完成整包原子替换；零后台定时轮询，按需驱动 `392pt` 毛玻璃命令流面板、双语搜索过滤、全键盘导航与独立 `CodexAgySync 设置 / CodexAgySync Settings` 窗口。
  - **在线回环批量空闲探测与持续 `-wal` 覆盖**：`workbench/menubar_feed.py` 在线优先调用单次批量 `LanguageServerService/GetAllCascadeTrajectories`（将结果探测与本地磁盘 `.db` 只读解码并行重叠，回退时复用 HTTP/1.1 Keep-Alive `GetCascadeTrajectory(include_steps=False)`，并借助 `discover_agy_subagent_sidecar_ids` 与 `include_raw_summary=False` 跳过全量 JSON 与 BLOB 读取），配对 PEP 562 延迟包导出（`__init__.py`、`bridge/__init__.py`、`workbench/__init__.py` 与 `cli.py`）；`sync --include-interrupted` 与 `menubar-feed` 在线确认 Antigravity `cascade_run_status == 1` 或 Codex `thread/read` 非活跃空闲时，允许抢救并交接已停止的中断会话，同时保持 `sync-session` / `sync-workspace` 原有离线锁保护不变。
  - **Stage 1 内置本地工作台（`serve`，冻结维护）**：默认随包分发 `src/codex_agy_sync/workbench/ui/index.html`（`codex-agy-sync serve --open` 开箱即用，保留 `--ui <path>` 覆盖），缺少本地令牌时显式报错引导；支持 `127.0.0.1` 回环绑定的 `0o600` 无 token `server.json`、`X-Workbench-Token` 与 `Host`/`Origin`/`Sec-Fetch-Site` 鉴权、敏感路径隔离、跨端全文检索、只复制 Markdown 草稿（`POST /api/send` 返回 `404`）、一键桌面交接与 `POST /api/handoff/reconcile`。
  - **在线优先项目配对与清理 RPC**：`bridge/project_rpc.py` 实现 Antigravity `LanguageServerService/CreateProject`（含重名工作区 `HTTP 409` 自动消歧与并发回查）及 `codex app-server` `project/list` + `project/create`；`pair` 与 `sync --auto-pair` 在线优先、离线安全回退，按实际路径记录 `pairing_mode`（`already_paired` / `online_rpc` / `hybrid` / `offline_locked`）。同时提供 `delete_agy_cascade_via_rpc`（`DeleteCascadeTrajectory`、尽最大努力 `JetboxDeleteSummary` 与 `agy_cli_data_dir/brain/<cid>` 清理）与 `delete_agy_project_via_rpc`（`DeleteProject`）。
  - **`sync` 主线收口与暂存自动清理**：支持 `--latest`、`--chain`、`--pick`、`--source-id` 方向推断、`--include-interrupted` 已停止半途回合抢救、`agy_cli_data_dir` 转录镜像、`--reconcile` / `--dry-run` 滞留收据核对、`status` 扩展输出（`pairing` / `handoff_chains` / `pending_handoffs`）及退出码 `2` 的结构化 `stderr` JSON 错误。交接达成 `status == "ready"` 后自动清理 `handoffs/staging/<key>`，仅在 `status == "pending"` 时保留暂存目录供 `sync --reconcile` 核对；`sync-session` 与 `sync-workspace` 仅保留作 `[Advanced/Offline]` 冻结维护工具。
  - **单次预加载目录扫描与分层按需水合**：`workbench/catalog.py`、`workbench/discovery.py`（含 `discover_workspaces` 原始路径去重与敏感根目录预归一化）与 `codecs/rollout.py`（`compression.zstd.open` / `stream_reader(read_across_frames=True)` 增量行流，并将截断或损坏帧统一归一化为 `ValueError`）配合 `_wire.py` 仅在提供 `known_tags` 时分配 `UnknownWireField` 及热点值对象 `slots=True`；`WorkbenchIndexer` 按 `eager_turn_budget=24` 优先水合多端聚簇、含交接收据及最新会话，其余卡片按含 `-wal` 侧车文件指纹缓存（本机真实库冷启动中位数 `1.38s`，`discover_workspaces` 提速至 `57.90ms`，`menubar-feed` 子进程稳定在 `~0.77s`）。
  - **门禁与真实宿主验证**：`./scripts/check`（含 `scripts/check_architecture.py` 6 层无环 DAG 与边界 AST 门禁、`[4/6]` 测试/QA 反向 DNS `suiteName` 及 Swift Python/pip 胶水静态门禁、`[5b/6]` Swift XCTest 跨越 `cfprefsd` 7.5s 延迟落盘窗口的 `~/Library/Preferences` 零泄漏与沙盒回收回归，以及 `[6/6]` Rust `cargo fmt/clippy/test` 与 `scripts/verify_rust_parity.py` 8 组差分门禁）、`skills/verify-codex-agy-sync/scripts/menubar_verify.py suite`（`cua-driver` 隔离沙盒 GUI 搜索筛选、内置引擎显示与对话接力端到端验证）、`control_sync_verify.py suite` 与 `workbench_verify.py suite` 覆盖全链路回归；真实双端 Desktop E2E 验收已确认双端在线配对与续聊互通（`status == "PASS"`，`native_desktop == "VERIFIED"`，`pairing_mode == "online_rpc"`）。

## 下一步

1. 聚焦本地额度中断极速交接主线：日常以 `CodexAgySync.app`（`⌃⌥H`）与 `codex-agy-sync sync` 为主入口；远端会话与 Stage 2 `POST /api/send` 不纳入维护范围，`serve` 与 `sync-session` / `sync-workspace` 保持冻结维护模式（仅修复回归缺陷，不再新增功能）。

## 记录

- 2026-11-10 完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md`、`AGENTS.md`、`NOTES.md` 初始化。

## 坑

- **活跃 WAL 与回环 RPC 边界**：读取运行中 `state_5.sqlite` / `conversations/<id>.db` / `conversation_summaries.db` 必须走只读连接（`?mode=ro`）或宿主本地 RPC；仅在确认无活跃守护进程、无 `-shm`/`-wal` 锁且持排他 `flock` 后才可执行离线写盘并先生成 `BackupManifest`。
- **`csrfToken` 内存隔离**：从 `~/.gemini/antigravity/daemon/hub_<port>.json` 读取的 `csrfToken` 仅限内存请求头 `x-codeium-csrf-token`，严禁写入日志、异常、收据、快照或文档，且回环请求须通过 `urllib.request.ProxyHandler({})` 绕过环境代理环境变量。
- **Antigravity 项目重名 409 与清理 RPC**：`LanguageServerService/CreateProject` 对 `~/.gemini/config/projects/*.json` 的 `project.name` 做全局唯一校验，同名分支或克隆返回 `HTTP 409`（`register_agy_project_via_rpc` 已做父目录/短 UUID 自动消歧并回查并发注册）；清理 Antigravity 测试会话与项目须使用 `DeleteCascadeTrajectory`（及 `JetboxDeleteSummary` + `agy_cli_data_dir/brain/<cid>`）与 `DeleteProject` RPC。
- **Antigravity Hub 可见性与转录镜像**：可见性遵循 `schema/sidebar_visibility_rules.json`（`source != 17` 且 `project_id` 不落入桶名）；`agy --conversation=<id>` 依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/transcript.jsonl` 同时存在；`steps.step_payload` 二进制为 `gemini_coder.Step`（`14->19`、`15->20`、`17->24`、`23->30`、`81/82->93/94`、`101->114`、`132->140`）。
- **Codex 次轮重放限制与侧栏规则**：Codex 0.166+ 侧栏要求 `archived = 0 AND preview <> '' AND cwd <> ''`（`threads.project_id` 可空）；跨宿主交接不移植私有 reasoning / 原生检查点，否则次轮会触发 Rustresponses 重放报错。
- **macOS 软链套接字探测与二进制路径发现**：`~/.codex/app-server-control/app-server-control.sock` 是指向 `/private/tmp/codex-...` 的符号链接，探测用 `Path.stat()` 且连接取解析后更短路径（避开 `AF_UNIX` 104 字节上限）；独立 CLI `agy` 通过 `ANTIGRAVITY_CLI_BIN` 或 `shutil.which("agy")` 动态发现，不写死用户家目录。
- **宿主持续 `-wal` 下的在线空闲覆盖边界**：Antigravity Desktop 打开期间会持续维持非空 `conversation_summaries.db-wal`；若仅凭宿主级 `-wal` 判定运行状态，会将已停止的中断会话误判为活跃。`menubar-feed` 与 `sync` 仅在在线回环 `GetCascadeTrajectory` 返回 `cascade_run_status == 1`（`IDLE`）且 Codex `thread/read` 为非活跃空闲时放行已停止中断会话抢救，不放松 `sync-session` 的离线写锁保护。
- **SwiftUI 无边框 `NSPanel` 交互与无障碍**：`.buttonStyle(.plain)` 在未聚焦无边框 `NSPanel` 上会吞掉后台 `AXPress` 且可能出现焦点环；菜单栏面板需使用自定义 `ButtonStyle`（`FlatSurfaceButtonStyle`）配合 `.focusEffectDisabled()`、`.accessibilityAction` 与重写 `canBecomeKey` 的 `NSPanel` 子类，才能兼具无蓝框视觉外观与稳定 `cua-driver` / VoiceOver 响应。
