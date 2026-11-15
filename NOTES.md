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

- 2026-11-15 **Day-1 可用性收敛完成**（分支 `feat/day1-usability-convergence`，待合并至 `main`）：
  - **Unit 1（在线项目注册 RPC 与自动配对，commit `1a96450`）**：新增 `bridge/project_rpc.py`，实现 Antigravity 在线项目注册（`POST /exa.language_server_pb.LanguageServerService/CreateProject` 二进制 Protobuf）与 Codex 在线项目查询/创建（`codex app-server` `project/list` + `project/create`），`pair` 与 `handoff --auto-pair` 默认在线优先注册运行中宿主并在缺失端离线时回退到 `guarded_pair_workspace`，按实际路径返回 `pairing_mode`（`already_paired` / `online_rpc` / `hybrid` / `offline_locked`）；补齐 `tests/test_online_pairing.py`。
  - **Unit 2（`handoff` 主线收敛与滞留核对，commit `b62a407`）**：`handoff` 新增 `--include-interrupted`（仅抢救已停止会话的尾部中断回合）、`agy_cli_data_dir` 转录镜像（导入 AGY 时镜像 `brain/<id>/.system_generated/logs/transcript.jsonl` 至 `antigravity-cli`）及 `--reconcile` / `--dry-run`（`bridge/handoff_reconcile.py` 与 `POST /api/handoff/reconcile`，验证滞留 `pending` 目标后晋升 `ready`、安全清理 absent 条目或保留 `unresolved`）；扩展 `status` 输出 `pairing` / `handoff_chains` / `pending_handoffs`，统一 CLI 业务与参数异常以退出码 `2` 向 `stderr` 输出结构化 JSON（`error` / `message` 等），并将 `sync-session` 与 `sync-workspace` 明确标为 `[Advanced/Offline]`；补齐 `tests/test_handoff_convergence.py`。
  - **Unit 3（内置工作台 UI 随包交付，commit `f26ea64`）**：将工作台页面内置至 `src/codex_agy_sync/workbench/ui/index.html`（通过 `pyproject.toml` `package-data` 随包分发），`codex-agy-sync serve --open` 开箱可用，保留 `--ui <path>` 覆盖入口；升级 `skills/verify-codex-agy-sync/scripts/workbench_verify.py`（默认验收随包 UI，`--prototype` 改为可选，且在 `repo-unpaired` 上实测 `#retryPairHandoff` 经 `CreateProject` RPC 返回 `pairing_mode == "online_rpc"`）；补齐 `tests/test_workbench_packaging.py`。
  - **Unit 4（宿主机规模单次扫描与按需水合，commit `89b6280`）**：新增 `workbench/catalog.py`，配合 `workbench/discovery.py` 单次只读预加载 `state_5.sqlite`（`project_roots` / `threads`）与 `conversation_summaries.db` 并按工作区桶归并，在扫描阶段过滤非本工作区 DB、子代理与已索引 rollout 避免重复解压缩；`workbench/indexer.py` 落地分层水合（默认 `eager_turn_budget=24` 优先解码聚簇、含收据、种子工作区及最新 24 条独立会话，其余通过 `get_work`、`get_snapshot(workspace_id=...)` 与 `get_snapshot_for_search` 按需并行水合并按 `(mtime_ns, size)` 缓存）。本机实测（109 个工作区 / 409 个工作项 / `threads` 5334 行 / `conversation_summaries` 1466 行）冷启动 `get_snapshot()` 耗时约 `3.10s`；补齐 `tests/test_indexer_scalability.py`。
  - **真实双端 Desktop E2E 验收通过**：`$HOME/Projects/_artifacts/codex-agy-sync/verify-live-desktop-20261115T034055Z-live/summary.json` 记录 `status == "PASS"`、`native_desktop == "VERIFIED"`、`pairing_mode == "online_rpc"`、`rpc_step_count == 4`、`cli_inspect_event_count == 4`、`agy_cli_transcript_mirrored == true`、`browser_errors == ""` 及 `serve --stop` 干净退出。

- 2026-11-15 `0.3.0`（`v0.3.0`）发布就绪：整合 `v0.2.0` 之后合并的 Stage 1 本地工作台（`codex-agy-sync serve` 回环 HTTP/UI 服务、工作区并查集发现与跨端检索/引用草稿）、`handoff` CLI/UX 增强（`--latest`、`--chain`、`--pick`、`--source-id` 方向推断、`--pair-if-offline` 与结构化诊断）、UI 原型浏览验收入口，以及 PR `#6` 落地的 6 层无环架构重构与 Codex 0.166+ `cwd_non_empty` 侧栏规则；同步 `pyproject.toml` / `PACKAGE_VERSION`（`0.3.0`）、`scripts/check` 版本一致性门禁与 `README.md` 六层结构说明。
- 2026-11-14 完成架构一致性重构（分支 `refactor/architectural-coherence`）：收拢 `src/codex_agy_sync/` 为严格 6 层向下 DAG（Tier 0 `_wire`/`workspace`/`payloads`；Tier 1 `events`/`ledger`；Tier 2 `codecs/` 含统一 `compress_zstd_rollout_bytes`；Tier 3 `bridge/` 拆出 `store_catalog.py`、`handoff_store.py` 并统合配对诊断与 `execute_handoff`；Tier 4 `workbench/` 提升 `discovery.py`/`lease.py`/`handler.py`；Tier 5 `cli`/`__init__`/`__main__`），对齐 `schema/sidebar_visibility_rules.json` 与 `scripts/verify_session_mapping.py` 为 `cwd_non_empty`，并在 `./scripts/check`（`scripts/check_architecture.py`）及 `tests/test_architecture_coherence.py` 落地 AST 门禁（零循环、零跨模块 `_private` 导入、零非 `self`/`cls` 私有属性访问、零函数内内部导入）。
- 2026-11-14 完成 Stage 1 本地工作台 PR 2（分支 `feat/workbench-stage1`）：新增 `src/codex_agy_sync/workbench/`（`models.py`、`_discovery.py`、`indexer.py`、`service.py`、`_lease.py`、`_handler.py`、`server.py`；已于 `refactor/architectural-coherence` 公开化为 `discovery.py`、`lease.py`、`handler.py`）与 `codex-agy-sync serve`（`--ui` / `--port` / `--idle-timeout` / `--max-lifetime` / `--open` / `--status` / `--stop`）；严格限定仅监听 `127.0.0.1`、注入单次 `X-Workbench-Token` 并校验 `Host` / `Origin` / `Sec-Fetch-Site`、`server.json` 以 `0o600` 无 token 落盘、过滤 `$HOME/Archive`、`$HOME/.private_keys`、软链逃逸与 `outside-of-project`，提供跨端检索、只复制 Markdown 草稿（`POST /api/send` 返回 `404`）与一键真实桌面交接；单元测试 `tests/test_workbench_service_and_server.py`、`tests/test_workbench_interrogate_fixes.py` 与浏览器端到端脚本 `skills/verify-codex-agy-sync/scripts/workbench_verify.py`（含 `_workbench_sandbox.py`，使用隔离沙盒 + 回环 AGY RPC 桩）全部通过，宿主数据库零变动。

- 2026-11-14 完成 `handoff` CLI/UX 增强（分支 `feat/handoff-cli-ux`）：新增 `bridge/handoff_resolve.py`（配对诊断、候选会话扫描、`--source-id` 方向自动推断、`--latest` 跳过未动过的接力目标、`--chain` 接力链游标/幂等 `no_op`、`--pick` 仅写 `stderr` 的 TTY 交互菜单及 `--pair-if-offline`），扩展 `HandoffReceipt` 向后兼容加载（`load_handoff_receipt`）与种子指纹（`ledger_seed_fingerprint`），使 `guarded_pair_workspace` 双端已注册时零锁零写入幂等，并在 `cli.py` 为未配对/忙锁/挂起返回退出码 `2` 与结构化 `stderr` JSON。单元测试补齐 `tests/test_handoff_cli_ux.py`。

- 2026-11-14 为外置工作台原型新增独立浏览器验收入口，见 `skills/verify-codex-agy-sync/features/ui-prototype.md`。覆盖搜索、共享草稿、模拟请教、完整历史接续与重复接续；证据与原型留在仓库外。截图由 Agent 单独复核，真实 Desktop 与消息投递须另行验收。

- 2026-11-14 `0.2.0` 已完成实现与发布包准备。GitHub 仓库已公开。Desktop handoff 的交付范围是可见历史文本与新会话续聊，不宣称跨宿主运行态无损迁移；同会话在线追加和其余工具的全面实机验收不在本次完成范围。

- 2026-11-14 新增 Desktop 专用 `handoff`。复用现有账本和编码器，AGY 以全新 ID 发布后由原生 RPC 加载，Codex 走原生 fork 并等待卸载。每次切换生成新会话，相同源历史重跑 `no_op`；不做实时镜像，不覆盖活跃会话。
- 真实 Desktop 验收发现并修复格式、重复消息和跨宿主运行态问题。思考及检查点摘要保留为普通文本，不重放私有推理状态、原生检查点和明确宿主注入。当前验收对象为 AGY 2.31.2 与 ChatGPT 内嵌 Codex；普通文本、多轮上下文和 Python 工具历史文本已实际双向往返；工具名称、参数与原始输出平文化，不仿制宿主运行态。验收收据与截图位于 `$HOME/Projects/_artifacts/codex-agy-sync/verify-desktop-20261113T180734Z/acceptance-*`。
- 发布复验 `scripts/check` 通过，91 项测试、5 项既有实机夹具跳过。此前真实测试会话已归档，本轮 Codex 映射使用其隔离数据库副本并只在副本恢复可见状态；AGY 仍为只读实机映射。隔离 CLI 七场景通过，宿主指纹未变。wheel 已在独立环境安装，版本和 `handoff --help` 通过。本轮未重新执行 Desktop 多轮实机续聊，沿用上列已验收证据。


## 下一步

1. 日常使用以 `codex-agy-sync serve --open`（直接启用随包内置 UI，无需外挂 `Main.html`，仅需定制界面时传 `--ui`）和 `codex-agy-sync handoff`（支持 `--auto-pair`、`--include-interrupted`、`--reconcile`）为主入口；`sync-session` 与 `sync-workspace` 仅保留作 `[Advanced/Offline]` 维护工具。
2. 推进分支 `feat/day1-usability-convergence` 收尾并回归验收 Stage 2 远端消息投递（当前 `POST /api/send` 仍固定返回 `404`）及更多原生工具类型的 Desktop 运行态兼容性。

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
