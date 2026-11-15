# 项目教训

实现、评审或验收前阅读相关条目，在用户纠正、返工或漏验暴露可复用教训后及时补记。

## 已核实教训

### 跨宿主轨迹同步的回合游标必须先归一化为顺序回合号再持久化

**双端同步游标（`SessionSyncCursor`）中的 `turn_id` 必须统一归一化为顺序回合标识（`turn-1`, `turn-2`, ...），禁止把单端特有的 UUID 直接写入跨端水位线。**

- 标签：#状态机 #幂等 #契约
- 起因与后果：Codex `rollout-*.jsonl` 在 `task_started` 与 `turn_context` 中携带 UUID 格式的 `turn_id`，而 Antigravity `steps` 表在 Protobuf wire 层不存储 Codex 回合 UUID，重解码后会按顺序生成 `turn-1`, `turn-2`。首版解码器直接保留了 Codex UUID 作为 `turn_id`，导致 `CODEX_TO_AGY` 初次导入后立即重跑 `sync_session` 时在 Antigravity 账本中找不到该 UUID 水位线，误判为历史重写分叉（`diverged_halted`）。
- 修复与证据：提交 `837d642` 在 `src/codex_agy_sync/codecs/rollout.py` 中将原始 `turn_id` 映射为确定性的顺序 `TurnId("turn-N")`，并在 `src/codex_agy_sync/codecs/steps.py` 中统一 `task_started` 与 `CHECKPOINT` 的回合切分边界，由 `tests/test_roundtrip_and_sync.py` 的 `test_real_session_isolated_roundtrip_codex_to_agy_and_back` 实测首次导入后立即重跑返回 `no_op`。
- 正确做法（Done right）：中间账本（`TrajectoryLedger`）的分组键与水位线锚点统一使用与宿主 wire 格式无关的规范化顺序回合号，宿主特有 UUID 放入 `HostExtensionEnvelope` 保真。
- 验证点：运行 `./scripts/check`（核对 `test_real_session_isolated_roundtrip_codex_to_agy_and_back` 与 `test_interrogate_adversarial_regressions`）。
- 复发记录：Seen 1x (2026-11-10)，由 `/interrogate` 对抗审查在 commit `dc32f09` 捕获并于 `837d642` 修复。

### 顶层会话分叉溯源与子智能体血缘必须正交建模并退役旧游标

**顶层会话的冲突分叉（`FORK_NEW_SESSION`）必须保持 `nesting_depth = 0` 并将分叉来源写入 `parent_references`，严禁复用子智能体 `parent_conversation_id` 导致顶层分叉会话从桌面侧栏消失，且写入新配对游标时必须自动退役冲突旧游标。**

- 标签：#状态机 #侧栏可见性 #幂等
- 起因与后果：提交 `9392c16` 引入 `SubagentLineage` 时将任何非空 `parent_cascade_id` 或 `parent_codex_thread_id` 一律提升为 `nesting_depth = 1`（`is_subagent = True`），而 `FORK_NEW_SESSION` 在派生顶层分叉会话时直接传入原会话 ID 作为父 ID，且未移除旧游标文件，导致派生出的顶层会话被隐藏进子代理抽屉，并在后续 `sync_workspace` 中触发重复分叉死循环。
- 修复与证据：提交 `e56b226` 与 `f46aac1` 将顶层分叉改写 `conversations/<id>.db` 的 `parent_references` 表，在 `SyncBridge._save_cursor` 中将同会话旧游标移入 `cursors/superseded/`，并在 Unit 4 实现单次 `sync_session` 对称双分支 UUIDv7 派生，已由 `test_top_level_fork_preserves_sidebar_visibility_and_converges_once` 与 `test_4e_single_sync_session_symmetric_fork_new_session` 验证通过。
- 正确做法（Done right）：区分并发冲突产生的顶层历史分叉与工具派发的子代理父子层级，保存 1 对 1 同步游标时强制清理同工作区占用任一会话 ID 的旧活跃游标。
- 验证点：运行 `./scripts/check`（核对 `test_top_level_fork_preserves_sidebar_visibility_and_converges_once` 与 `test_4e_single_sync_session_symmetric_fork_new_session`）。
- 复发记录：Seen 1x (2026-11-11)，由全分支二次 `/interrogate` 三模型共识（`R2`/`R3`）在 commit `9392c16` 捕获并于 `e56b226`、`f46aac1` 修复。

### 离线伴生索引必须从已提交主表重解码投影且目标活跃锁必须显式延迟

**多存储宿主的伴生索引必须从主表提交后的真实记录重解码生成，且目标端持有活跃守护进程或 WAL 锁时严禁裸写 SQLite，必须显式返回 `deferred_busy` 并如实报告 `transport_used`。**

- 标签：#并发安全 #一致性 #边界校验
- 起因与后果：`status` 命令与 `sync_session` 预检阶段曾无条件调用 `pair_workspace` 写入 `state_5.sqlite`，且增量 `Codex -> AGY` 同步曾直接拿内存事件投影 `transcript.jsonl` 与 `conversation_summaries.db`，造成步骤号与 `conversations/<id>.db` 的 `steps.idx` 错位。
- 修复与证据：提交 `f46aac1` 及 Unit 4/5 在 `cli.py`（`status`）与 `SyncBridge` 预检入口统一改用纯只读 `resolve_readonly_binding`，在 `DefaultHostTransport` 强制校验 `OFFLINE_STORAGE` 与 `acquire_offline_write_locks`，在提交 `steps` 后按 `idx ASC` 重解码生成全部伴生文件，并在 `bridge/rpc.py` 实现 `A2` 在线回环读取与诚实 `transport_used` 归因，已由 `test_online_rpc_defers_and_status_is_strictly_readonly` 与 `test_incremental_codex_to_agy_step_indices_match_across_stores` 验证通过。
- 正确做法（Done right）：只读命令与锁探测阶段零写盘，离线写入全程持排他 `flock` 并备好 `BackupManifest.restore()` 自动回滚，一切派生视图以已提交主表为单一真相源重投影。
- 验证点：运行 `./scripts/check`（核对全部行为与回归测试通过）。
- 复发记录：Seen 1x (2026-11-11)，由全分支二次 `/interrogate`（`R1`/`R6`）捕获并于 `f46aac1` 修复。

### 根会话角色名与子代理血缘必须正交判定且 macOS 软链套接字必须跟随目标解析

**判定子代理（`SubagentLineage.is_subagent`）严禁仅凭非空 `agent_name` 或 `agent_role` 推断，且在 macOS 上探测与连接 Codex `app-server` Unix 域套接字时必须使用跟随符号链接的 `Path.stat()` 并选用解析后更短的目标路径。**

- 标签：#侧栏可见性 #跨平台边界 #契约
- 起因与后果：本机真实宿主库核查发现 Antigravity 有大量带 `agent_name`（如自定义模式名）但 `nesting_depth = 0` 的顶层会话，Codex `threads.source` 亦区分 `"desktop"`/`"cli"` 与 `{"subagent": {"thread_spawn": ...}}` JSON，若把非空角色名当作子代理会导致数百条顶层会话被静默过滤或写入 `nesting_depth = 1` 从侧栏消失。同时 macOS 上 Codex Desktop 的 `~/.codex/app-server-control/app-server-control.sock` 是指向 `/private/tmp/codex-...` 的符号链接，使用 `lstat()` 会漏检 `S_ISSOCK`，而在深层沙盒目录直接按原软链路径 `connect()` 会触发 `AF_UNIX` 104 字节 `sun_path` 长度越界。
- 修复与证据：Unit 3 在 `src/codex_agy_sync/ledger.py` 与 `src/codex_agy_sync/bridge/registry.py` 将 `SubagentLineage.is_subagent` 收紧为显式层级深度、sidecar 文件、`spawn_step_index` 或 `threads.source` 子代理结构；Unit 5 在 `src/codex_agy_sync/bridge/paths_and_locks.py` 与 `rpc.py` 实现 `has_live_unix_socket`（基于 `stat().st_mode`）与 `unix_socket_connect_path`（自动选取软链解析前后字节更短的路径），由 `test_3a_orthogonal_lineage_agy_root_agent_name_and_codex_source_shapes` 与 `test_5b_codex_unix_websocket_rpc_symlink_large_payload_and_a2_sync` 验证通过。
- 正确做法（Done right）：顶层会话自定义角色名与子代理父子层级分字段正交存储；Unix 套接字探测统一走 `has_live_unix_socket` 并在 `connect()` 前比较软链目标字节长度。
- 验证点：运行 `./scripts/check`（核对 `test_3a` 与 `test_5b` 测试通过，并在本机只读探测 `PresenceWalGuard.discover_codex_daemon()` 命中软链套接字）。
- 复发记录：Seen 1x (2026-11-12)，在全仓真实宿主库只读实测评审与 Unit 3/5 在线 RPC 联调时核实并固化。

### Desktop 续聊必须在多轮真实上下文中验收

- 2026-11-14 实测发现，Codex rollout 缺少 `session_meta.cli_version` 时原生 fork 拒绝导入；仅有 `response_item` 时模型历史存在，但 Desktop 的 `thread/read` 消息列表为空。编码时补版本和对应 `event_msg`，并以真实 app-server 回读验证。
- Codex 原生 `event_msg` 可能没有 `turn_id`。必须先配对同轮 `response_item` 副本，再推断新回合；页面注入的工具输出会使旧的 `turn_has_response` 启发式错误推进回合，造成消息重复。
- AGY 原生 Desktop 样本使用 trajectory type 4、step format 0 和每步 metadata field 20 的会话引用。不能把 SQLite 可解码当成 Desktop 可续聊。
- 跨宿主思考摘要作为普通文本交接，不伪造原生 reasoning。实测含原生 reasoning 的导入首轮成功、次轮报 Rustresponses 无法重放；相同输入去掉该记录后连续两轮及工具调用通过。
- `turn_context.summary = "none"` 是 Codex 配置，不是 AGY 会话摘要。误写为 AGY checkpoint 后，模型丢失检查点之前的口令；改为不移植原生检查点后，模型重新正确回答完整历史。可读摘要保留为普通文本。
- 原生 `thread/fork` 后，同连接取消订阅并等待卸载，再把会话交给 Desktop。只关闭客户端连接可能导致 Desktop 提示会话仍在其他应用打开。
- 回归入口为 `tests/test_desktop_rollout.py`、`tests/test_desktop_handoff.py`、`tests/test_native_codex_import.py`。这些测试不替代 Desktop 多轮验收。

- AGY 会把导入的 generic 工具结果（磁盘 step type 132）在 RPC 中投影成原生 RUN_COMMAND（type 21）。当前七类步骤 decoder 未覆盖该投影，导致回传误补中断结果。Desktop handoff 将工具调用和原始输出平文化，避开运行态重放；旧 codec 不因此宣称支持全部宿主投影。Codex custom `exec` 的非 JSON 脚本输入也必须保留原文，不能按空命令解析。

- AGY 新执行的原生工具也存在相同 RPC 投影差异：真实 Python 结果在磁盘 type 132 中完整，在 RPC type 21 中未被现有解码器读出。handoff 先用 RPC 检查空闲，再用现有只读 SQLite 读取器提取历史；实测原始结果 `1263` 与 call ID 配对保留，数据库指纹不变。

### Codex 侧栏可见性谓词以非空 `cwd` 为准且模块分层必须靠 AST 门禁固化

**Codex Desktop 0.166+ 侧栏筛选使用 `archived = 0 AND preview <> '' AND cwd <> ''`（`threads.project_id` 可为 `NULL`），且包内模块边界必须由自动化 AST 检查强制约束。**

- 标签：#契约 #侧栏可见性 #架构
- 起因与后果：早期对照 `idx_threads_project_recency` 假设 `threads.project_id IS NOT NULL` 为必需条件，但新版本 Codex Desktop 会将根路径保存在 `threads.cwd` 与 `.codex-global-state.json` 的 `electron-saved-workspace-roots` 中并将 `project_id` 置空，导致校验脚本误筛；同时多轮扩展后跨模块引用了 `_private` 符号与延迟包内导入。
- 修复与证据：在 `schema/sidebar_visibility_rules.json` 与 `scripts/verify_session_mapping.py` 对齐 `cwd_non_empty` 规则并校验空 `cwd` 反例；重组 `src/codex_agy_sync/` 为 6 层严格向下 DAG（`scripts/check_architecture.py` 集成于 `./scripts/check`），抽离 `workspace.py`、`events.py`、`bridge/store_catalog.py`、`bridge/handoff_store.py` 并提升 `workbench/{discovery,lease,handler}.py` 为公开模块，由 `tests/test_architecture_coherence.py` 回归覆盖。
- 正确做法（Done right）：所有宿主机侧栏谓词改动同步更新 schema 规则、验证脚本与反例测试；任何新增包内模块须归属清晰层级并通过 `./scripts/check` AST 检查。
- 验证点：运行 `./scripts/check`（确认 Step `[2/5]`、`[3/5]` AST coherence gate 与 `tests/test_architecture_coherence.py` 全部通过）。
- 复发记录：Seen 1x (2026-11-14)，在阶段架构一致性重构中发现并固化。

### 只读宿主数据库探针必须优先选静默会话并将文件指纹窗口收紧到连接期

**Antigravity 或 Codex 只读探针挑选默认会话时应优先跳过仍在活跃写入 `.db-wal` 的会话，并将只读指纹前后快照严格收紧到 SQLite 只读连接期而非跨过后续 CPU 解码阶段。**

- 标签：#只读探针 #WAL并发 #验收
- 起因与后果：`select_default_agy_conversation` 按 `last_modified_time DESC` 直接挑选活跃会话，且 `decode_antigravity_session` 将 `before_*_stat` 与 `after_*_stat` 跨越了约 1.5s 的 Protobuf 全量解码窗口；当活跃会话在解码期间持续追加 `.db-wal` 时，探针触发 `Read-only violation detected on Antigravity SQLite files!` 误报并中断验收。
- 修复与证据：在 `scripts/verify_session_mapping.py` 新增 `run_readonly_sqlite_query`（校验 `PRAGMA query_only == 1`，仅在 `open_sqlite_readonly` 到 `conn.close()` 之间对比 `snapshot_file_stat`，遇并发写入抖动重试最多 5 次），并在 `select_default_agy_conversation` 按主库与 `-wal` 最近修改时间划分 120s 静默 `idle_candidates` 优先扫描、无命中再回退 `active_candidates`，收紧 `decode_antigravity_session` 与 `decode_codex_session` 的快照范围；连续两次运行 `./scripts/check` 全量通过验证。
- 正确做法（Done right）：所有只读 SQLite 探针通过 `run_readonly_sqlite_query` 在纯连接查询窗口内校验文件指纹，默认样本挑选以主库与 `-wal` 静默超过 120 秒为优先，禁止让只读完整性快照跨越后续 CPU 解码或磁盘伴生文件读取。
- 验证点：连续两次运行 `./scripts/check`（核对 `[2/5]` 只读无损解码与 `readonly_integrity` 字段均为 `True` 且全量 5 步检查通过）。
- 复发记录：Seen 2x (2026-11-14, 2026-11-15)，在活跃宿主会话并发写入期间执行只读探针及 `./scripts/check` 时暴露竞态误报。

### 运行中宿主的跨端项目配对应优先走在线注册 RPC 并区分配对模式

**当 Antigravity 或 Codex Desktop 处于运行态且持有活跃 WAL 锁时，项目配对必须通过宿主在线注册 RPC（`LanguageServerService/CreateProject` 与 `codex app-server` `project/list` + `project/create`）完成，并区分 `already_paired` / `online_rpc` / `hybrid` / `offline_locked` 四类模式。**

- 标签：#在线RPC #项目配对 #并发安全
- 起因与后果：早期 `guarded_pair_workspace` 仅支持离线持锁写 `projects/<uuid>.json` 与 `state_5.sqlite` / `.codex-global-state.json`，任意一端 Desktop 打开时触发 `--pair-if-offline` 会因活跃守护进程锁直接返回 `409 sync_busy_lock`，导致工作台一键交接与日常开箱配对受阻。
- 修复与证据：提交 `1a96450` 新增 `bridge/project_rpc.py`，对 Antigravity 编码 `exa.language_server_pb.CreateProjectRequest`（含 `exa.project_pb.Project` 与 `backend`）投递 `POST /exa.language_server_pb.LanguageServerService/CreateProject` 并轮询等待 `~/.gemini/config/projects/<uuid>.json` 落地（最长 0.25s）；对 Codex 通过 Unix Socket WebSocket 先分页调用 `project/list` 去重，未命中再调用 `project/create`（传 `idempotencyKey`、`name`、`roots: [{"path": ...}]`、`metadata: None`）。`registry.py` 引入 `pair_workspace_with_mode`（`pair` 与 `handoff --auto-pair` 默认在线优先，离线端再回退安全写盘），同时保留 `--pair-if-offline` 的严格离线语义（互斥且在线端被阻塞时仍报 `sync_busy_lock`）。由 `tests/test_online_pairing.py` 与真实双端 Desktop E2E（`$HOME/Projects/_artifacts/codex-agy-sync/verify-live-desktop-20261115T043314Z-live/summary.json`，`pairing_mode: "online_rpc"`）验证通过。同时已核实并补充双方官方清理 RPC 约定：Antigravity 通过 `LanguageServerService/DeleteCascadeTrajectory`（辅助尽最大努力 `JetboxDeleteSummary` 与 `agy_cli_data_dir/brain/<cid>` 清理，`delete_agy_cascade_via_rpc`）与 `LanguageServerService/DeleteProject`（`delete_agy_project_via_rpc`）回收；Codex 通过 `codex app-server` `thread/archive` 与 `project/delete` 回收。
- 正确做法（Done right）：默认使用 `pair` 或 `handoff --auto-pair` 让活跃端走 RPC、离线端走持锁写盘，在 `HandoffReceipt.pairing_mode` 如实记录路径；严禁在桌面进程存活时退回裸写数据库。
- 验证点：运行 `./scripts/check`（核对 `tests/test_online_pairing.py` 与 `tests/test_workbench_packaging.py`），并在 `workbench_verify.py` 场景 `05-one-click-handoff-and-refresh` 验证 `pair_if_offline: true` 返回 `409 sync_busy_lock` 而 `#retryPairHandoff` 通过在线 `CreateProject` RPC 返回 `200`（`pairing_mode == "online_rpc"`）。
- 补充说明（重名工作区 409 自动消歧）：实测 Antigravity 在线 `LanguageServerService/CreateProject` 会对 `~/.gemini/config/projects/*.json` 中的 `project.name` 执行全局唯一校验；当 git worktree、同目录名克隆或临时 E2E 目录出现重名时服务端返回 `HTTP 409 already_exists`。`register_agy_project_via_rpc` 通过预扫描现有名称、按需追加父目录名或短 UUID 后缀自动消歧，并在收到 `409` 时优先复查并发写入的同工作区注册记录后再回退次选名称。
- 复发记录：Seen 1x (2026-11-15)，在 Day-1 可用性收敛与工作台一键配对重试联调中补齐。

### 滞留 `pending` 交接收据须经目标存活性与种子核对后晋升或清理

**当交接在导入或等待确认阶段中断并遗留 `status: "pending"` 收据时，不得盲目覆写或忽略，而应通过 `handoff --reconcile` / `POST /api/handoff/reconcile` 核对目标宿主 RPC 与磁盘状态后再决定晋升、清理或保留。**

- 标签：#交接恢复 #幂等 #安全清理
- 起因与后果：`handoff` 为防重复生成第二份目标会话，在确认目标已就绪前先落盘 `pending` 收据。若传输或卸载等待期间进程中断、超时，后续重跑会在未创建新会话前拒绝并抛出 `handoff_pending`，用户需要一个既能认领已成功落盘的目标、又能安全清理失败残余的收敛入口。
- 修复与证据：提交 `b62a407` 新增 `bridge/handoff_reconcile.py` 与 `handoff --reconcile`（支持 `--source-id` 过滤与 `--dry-run` 预览）及 `POST /api/handoff/reconcile` / UI `#reconcilePendingHandoff`。核对全程持 `open_handoff_lock`：对 AGY 目标比对 `GetCascadeTrajectory` RPC 步数与暂存 `.db` / `ledger_seed_fingerprint` 回合数（离线回退 `conversations/<id>.db` + `annotations/<id>.pbtxt`）；对 Codex 目标比对 `thread/read` RPC 或 rollout + `state_5.sqlite` `threads` 记录（无 `target_id` 时用 `_codex_orphan_matches_receipt` 排查匿名候选）。按结果分别执行 `"promoted"`（更新为 `status: "ready"` 并通过 `handoff_store.cleanup_handoff_staging_dir` 防越界清理暂存目录）、`"cleared"`（确认目标未生成后安全删除收据与暂存目录）或 `"unresolved"`（部分落盘、活跃 WAL 锁阻塞 RPC 或存在匿名候选时保留原 `pending` 收据，不引入伪造 `failed` 状态）。另外 `handoff.py` 在交接正常达成 `status == "ready"` 时也会自动调用 `cleanup_handoff_staging_dir` 清理 `handoffs/staging/<key>`，仅在 `status == "pending"` 失败恢复场景保留暂存目录供 `--reconcile` 核对。由 `tests/test_handoff_convergence.py` 与 `tests/test_desktop_handoff.py` 验证通过。
- 正确做法（Done right）：先用 `--reconcile --dry-run` 或 UI `#reconcilePendingHandoff` 核对目标端真实状态；只有确认完整种子回合已就绪才晋升 `ready`，只有确认目标完全不存在才清理收据并做路径逃逸校验。
- 验证点：运行 `./scripts/check`（核对 `tests/test_handoff_convergence.py` 与 `POST /api/handoff/reconcile` 回归）。
- 复发记录：Seen 1x (2026-11-15)，在 `handoff` 失败恢复收敛中落地验证。

### 宿主机规模多工作区扫描必须单次预加载表并实行分层按需水合

**工作台快照构建在 100+ 工作区规模下严禁按工作区 `O(W * N)` 重复扫描 `state_5.sqlite` / `conversation_summaries.db` 或全量解压 rollout / PB，必须采用单次预加载分桶与 `eager_turn_budget` 分层按需水合。**

- 标签：#性能 #只读索引 #架构
- 起因与后果：早期 `WorkbenchIndexer` 为每个工作区逐一重复查询全库并全量解析所有会话回合（含 Codex `.jsonl.zst` 解压与 Antigravity Protobuf 解码）。在本机真实规模（实测 109 个工作区、409 个工作项、`state_5.sqlite` `threads` 5334 行、`conversation_summaries.db` `conversation_summaries` 1466 行）下，按工作区乘以全库会话数的重复扫描与全量解码会造成工作台冷启动严重卡顿甚至数十分钟级阻塞。
- 修复与证据：提交 `89b6280` 新增 `workbench/catalog.py` 并重构 `workbench/discovery.py` 与 `workbench/indexer.py`：`load_preloaded_host_tables` 在单次快照内仅以 `?mode=ro` 各打开一次 `state_5.sqlite` 与 `conversation_summaries.db`；`scan_host_catalog` 单次扫描按工作区桶归并，跳过非当前工作区 DB、子代理与已索引 rollout 避免重复解压缩，并以 `(mtime_ns, size)` 缓存 Antigravity 元数据；`WorkbenchIndexer` 默认 `eager_turn_budget=24` 优先水合多端聚簇、含交接收据、种子工作区及最新 24 条独立工作区卡片，其余卡片生成 `_build_unhydrated_work_card` 元数据骨架，待调用 `get_work(work_id)`、`get_snapshot(workspace_id=...)` 或 `get_snapshot_for_search(workspace_id=..., scope=...)` 时再并行按需水合并缓存。本机实测冷启动 `WorkbenchIndexer(paths).get_snapshot()` 降至约 `3.10s`，并由 `tests/test_indexer_scalability.py` 锁定 SQLite 打开次数与懒水合全覆盖语义。
- 正确做法（Done right）：全局扫描逻辑集中在 `catalog.py` 与 `discovery.py` 的单次 `O(1)` 表预加载 + `O(N)` 分桶中，列表冷启动只解码预算内最新回合，详情/单工作区/全文搜索再按需水合，禁止把工作区循环写回全量表读或全量 rollout 解码路径。
- 验证点：运行 `python3 -m unittest tests.test_indexer_scalability -v` 及 `./scripts/check`，并可在真实宿主执行只读 `WorkbenchIndexer(HostEnvironmentPaths.discover()).get_snapshot()` 核对秒级冷启动。
- 复发记录：Seen 1x (2026-11-15)，在真实宿主机大规模工作区工作台冷启动实测中发现并重构根治。

### 桌面守护进程运行期间必须以在线 RPC 空闲态覆写全局 WAL 误判

**`menubar-feed` 与 `handoff --include-interrupted` 在 Antigravity / Codex 守护进程在线时必须以 `GetCascadeTrajectory`（`cascade_run_status == 1`）与 `thread/read`（非活跃且无 `in_progress` 边界）作为空闲态依据，严禁因宿主级 `conversation_summaries.db-wal` / `state_5.sqlite-wal` 非空、历史 `presence/<cid>.lock` 或残余 `steps.status = 2` 将已停止会话误判为 `ACTIVE_RUNNING`。**
离线 `sync-session` / `sync-workspace` 仍保留 `is_source_actively_running` 的 WAL 锁避让规则以防裸写冲突；该边界由 `tests/test_menubar_feed.py` 的 `test_online_rpc_idle_overrides_host_wal_for_feed_and_handoff` 回归锁定。

### macOS `LSUIElement` 附属窗口与双端原子更新必须在替换与聚焦边界做显式保护

**菜单栏应用（`LSUIElement = true`）打开独立 `NSWindow`（`isReleasedWhenClosed = false`）时必须临时切换 `.regular` 激活策略并由窗口关闭/失焦通知回收本地 `NSEvent` 监听器，且 App+CLI 同频更新必须先在隔离暂存区完成签名校验与目标写权限检查后再安装 Python 包并原子替换 `.app`。**

- 标签：#macOS原生 #原子更新 #生命周期
- 起因与后果：在 `4a5a6a9` 对抗审查（`/interrogate`）中发现：（1）`build_menubar_app.sh` 重写 `Info.plist` 时漏掉 `CFBundleIdentifier` 且在编译前执行 `rm -rf "$APP_BUNDLE"`；（2）`SettingsView` 快捷键录制使用 `NSEvent.addLocalMonitorForEvents` 依赖 SwiftUI `.onDisappear` 释放，但复用窗口 `isReleasedWhenClosed = false` 关闭时不触发 `onDisappear` 造成监听残留，且 `.accessory` 激活策略下独立设置窗难以成为前台 Key Window；（3）`UpdateService` 先跑 `pip install` 后检查 `.app` 签名与目录写权限，目录只读或 `.app` 构建失败会导致双端版本撕裂。
- 修复与证据：在 `scripts/build_menubar_app.sh` 恢复 `CFBundleIdentifier` 并在 `$SCRATCH_PATH/bundle-stage.*` 完成构建签名后再原子替换目标 `.app`；在 `SettingsWindowController` 窗口打开期间切换 `.regular` 并在 `windowWillClose` 恢复 `.accessory`，由 `NSWindow.willCloseNotification` / `didResignKeyNotification` 清理录制监听器并暂停全局热键；在 `UpdateService` 增加资源文件名路径逃逸防护、非临时源码仓校验、先校验目标目录可写与暂存 `.app` `codesign --verify` 再安装 CLI 包，并记录 `pendingRelaunchVersion` 防止重探测误报失配。由 `PreferencesAndUpdateTests.swift`、`MenubarViewModelTests.swift` 与 `menubar_verify.py suite` 验证通过。
- 正确做法（Done right）：所有 `.app` 产物先在沙盒/暂存目录完成 `Info.plist` 键完整性与 `codesign` 校验再做原子交换；复用型 `NSWindow` 的事件监听器绑定 AppKit 窗口通知而非仅靠 SwiftUI `onDisappear`。
- 验证点：运行 `./scripts/check` 与 `python3 skills/verify-codex-agy-sync/scripts/menubar_verify.py suite`（核对 `bundle_id`、`AppIcon.icns` 及活跃 Key Window 设置页截图）。
- 复发记录：Seen 1x (2026-11-16)，由三模型 `/interrogate` 对抗审查在 `4a5a6a9` 捕获并修复。
