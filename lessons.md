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

## 记录模板

复制下列模板新增条目，已有同类条目则更新原条。未知的提交、原因或验收结果明确写“待核实”。
同一事件的多次修复、测试重跑不增加复发次数，只统计有独立证据的再次发生。
记录保留可复用的原因与证据定位，凭据、私人资料及完整日志留在各自获准的位置。

```markdown
### <教训标题>

**<一句话说明下次应遵守的规则。>**

- 标签：<#验收 #凭据 #交接 这类跨项目归类词，1 至 3 个>
- 起因与后果：<触发条件、原因、用户可感知的影响，未知则写待核实。>
- 修复与证据：<修复提交或 PR、回归检查与实际验收证据。>
- 正确做法（Done right）：<下次的具体做法，关联防复发代码或检查。>
- 验证点：<写明之后怎么核对这条已改到位。>
- 复发记录：Seen <N>x (<YYYY-MM-DD>)，<每次独立发生的证据。>
```

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
