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
- 2026-11-10：完成第二阶段 `/architect` + `/arena` 架构设计与第三阶段全量实现、双向真实副本往返测试及 `/interrogate` 多模型对抗审查：
  - 按可验证单元依次提交 [`5574006`](file:///Users/xianweizhang/Projects/codex-agy-sync/docs/architecture.md)（领域骨架）、[`de391e2`](file:///Users/xianweizhang/Projects/codex-agy-sync/src/codex_agy_sync/ledger.py)（统一事件账本与 5 项伴生投影）、[`d3a8816`](file:///Users/xianweizhang/Projects/codex-agy-sync/src/codex_agy_sync/codecs.py)（表驱动 7 类步骤与 16 类工具编解码器）、[`dc32f09`](file:///Users/xianweizhang/Projects/codex-agy-sync/src/codex_agy_sync/bridge.py)（双端项目配对、WAL/Presence 锁守卫、增量游标与分叉状态机、`bin/codex-agy-sync` CLI）以及 [`837d642`](file:///Users/xianweizhang/Projects/codex-agy-sync/tests/test_roundtrip_and_sync.py)（修复 `/interrogate` 三个模型审查发现的全部 8 项问题并锁定回归测试）。
  - 在隔离的真实会话副本上完成 `Antigravity 2.30+ (eb95dc40-cb5e-4017-b4ff-f41223e1f701, 161 步, 97 次工具调用) <-> Codex 0.166+ (01a1ae7d-d56f-7650-8336-aa848b9f2a5a, 18 条推理, 19 次工具调用)` 双向往返同步、增量追加、分叉派生、备份覆盖与 `flock` 锁延迟测试，`./scripts/check` 5 阶段全绿（15/15 行为与回归测试通过）。
- 2026-11-11：吸收 `vshulcz/deja-vu` 对比分析的第 2、3 点，在 Phase 2 转换器架构（`docs/architecture.md`）与实现（`ledger.py`、`codecs.py`、`bridge.py`、`cli.py`）中补齐：
  - **子智能体（Subagent）血缘识别与侧栏隔离**：新增 `SubagentLineage` 值对象，解析 `brain/<parent>/.system_generated/subagents/<child>.json`、`CortexTrajectoryMetadata`（tags 5/6/8/17）、`conversation_summaries.db`（`parent_conversation_id` / `nesting_depth` / `agent_name`）与 Codex `threads`（`attached_thread_parent_id` / `agent_role` / `agent_nickname`）；`list_workspace_sessions` 与 `sync_workspace` 默认 `include_subagents=False` 过滤子会话以防污染 Codex 顶层项目侧栏，显式同步时保留完整父子血缘与伴生 JSON。
  - **工具文件路径 `file://` URI 百分号解码与控制字符注入防护**：新增边界纯函数 `normalize_tool_file_path`，在 `codecs.py` 编解码边界对 `ReadFilePayload.path`、`FileEditPayload.path`（含 `apply_patch` 补丁头）、`RunCommandPayload.cwd`、`DefineSubagentPayload.agent_path`、`GenerateImagePayload.image_paths` 剥离反引号与 `file://` 前缀，仅对带 `file://` 前缀的 URI 执行 `urllib.parse.unquote`（保留普通路径中的字面量 `%`），并剥离 `\n`/`\r`/`\x00` 防止 `%0A` 伪造 `apply_patch` 头注入；`./scripts/check` 5 阶段全绿（17/17 行为与回归测试通过）。
- 2026-11-11：接管 `main` 分支完成全分支二次多模型 `/interrogate` 对抗审查（`pro`、`flash`、`inherit` 三模型独立审查 `aa6ebb9...9392c16`，复现并修复全部 9 项缺陷 `R1`–`R9`）：
  - 提交 [`e56b226`](file:///Users/xianweizhang/Projects/codex-agy-sync/src/codex_agy_sync/codecs.py)：修复含未编码空格 `file://` URI 截断、会话 ID 边界校验（`validate_session_id`）、跨端水位线工具参数摘要与框架事件排除、`DELETE` 文件补丁往返保真、Codex `apply_patch.input` / `exec_command.cmd` 及 Antigravity PascalCase 工具参数投影、多行标题空白折叠。
  - 提交 [`f46aac1`](file:///Users/xianweizhang/Projects/codex-agy-sync/src/codex_agy_sync/bridge.py)：强制 `ONLINE_RPC` 在未接入写 RPC 时返回 `deferred_busy`、`status` 与预检阶段纯只读零落盘、顶层分叉会话保持 `nesting_depth = 0`（血缘记入 `parent_references`）并自动退役旧游标至 `cursors/superseded/`、增量同步后从已提交 `steps` 重解码伴生视图确保 `step_index` 严格一致、游标落盘前崩溃重试自动收敛为 `no_op`、保留既有 `PRAGMA user_version = 1` 并通过 `AF_UNIX` `connect()` 区分死残留 `app-server.sock`；`./scripts/check` 5 阶段全绿（29/29 行为与回归测试通过）。
- 2026-11-11：完成全仓代码质量、架构一致性与打补丁痕迹专项评审（`pro`、`flash`、`inherit` 三模型独立审查 + 本机真实宿主库只读实测复现），并落地与端到端自证项目本地验收技能 [`skills/verify-codex-agy-sync/SKILL.md`](file:///Users/xianweizhang/Projects/codex-agy-sync/skills/verify-codex-agy-sync/SKILL.md)（含 `.agents/skills/{verify,verify-codex-agy-sync}` 软链、`control_sync_verify.py` 隔离沙盒驱动器与 5 份功能地图，实测保留 26 份证据文件且宿主数据库零变更）。评审确认当前 `src/`（9,120 行）虽通过 29 项既有测试，但存在明显迭代打补丁堆叠、4 套重复 wire 解析器、6 遍重复写管线及多项真实数据正确性缺口，详见下方“下一步”6 个待办可验证单元。

## 下一步

1. **Unit 1（抽离唯一安全 Wire 基元并净化 `ledger.py`）**：新建 `src/codex_agy_sync/_wire.py` 合并全仓 4 套重复 Protobuf varint/wire 解析器并补齐截断越界检查；将 242 行底层字节拼装移出 `ledger.py`，移除 `CascadeSummaryProjection.__post_init__` 缓存失步陷阱与 `decode_summaries_state_proto` 假占位值，清理遗留重复同义参数/方法别名及根目录一次性 `todo.md` / `decisions.tsv`。
2. **Unit 2（修复有损编解码与真表驱动拆解 `codecs.py`）**：将 `TOOL_CODEC_TABLE` / `STEP_WIRE_TABLE` 改为挂载真实 `(encode_fn, decode_fn)` 并消费 `schema/antigravity_2_30_schema.json` 字段常量，消除 523 行 `if kind == ...` 巨石与 181 处裸数字 tag；修复 6 类工具列表 $2^N$ 指数翻倍（恢复测试严格相等断言）、`UnifiedDiff` 块头前导空格污染（删除合成 `"unified_diff"` JSON 拐杖）、`UserInputEvent.scope_items` 与 `ChatToolCall.thinking_signature` 丢失、`_decode_step_metadata` `" preserved_fields"` 拼写及未知字段丢弃，并补齐本机真实存在的 Codex `response_item.agent_message` 与 Antigravity `step_type = 17`（`ERROR_MESSAGE`）无损保真。
3. **Unit 3（修正子代理血缘误判与半途回合完成判定）**：收紧 `SubagentLineage.is_subagent` 仅凭真实父子层级/sidecar 判定，防止本机带非空 `agent_name`/`agent_role` 的顶层会话（AGY 273/967 条、Codex 251/4,466 条）被静默过滤或强改 `nesting_depth = 1`；修正 `TrajectoryLedger.from_events` 避免将多步工具循环中段（末尾裸 `ToolResultEvent`）及 `steps.status in {1, 2, 8, 9}` 流式步骤误判为已完成回合；按需设计显式半途快照导入开关。
4. **Unit 4（持锁事务上下文、自动回滚、伴生自愈与对称分叉）**：将 `PresenceWalGuard` 改为写盘全程持有 `sync.lock` 与 `presence/<id>.lock` 的上下文管理器，消除探后即放 TOCTOU；用单一 `_execute_write_plan` 消除 `sync_session` 527 行 6 遍重复写管线并在写盘异常时自动按备份清单还原；在 `NoOpTransition` 校验并自愈缺失伴生索引（含 `antigravity-cli/brain` 镜像）；使 `sync_session(FORK_NEW_SESSION)` 单次调用内对称完成双分支 UUIDv7 派生，移除 `sync_workspace` 外层补调补丁。
5. **Unit 5（兑现或收口 Online RPC 与守护模式）**：实现 `GetCascadeTrajectory` Connect RPC 与 `codex app-server` `thread/read` 在线无锁读取客户端（未运行时对 `online_rpc` 如实延迟，不谎报 `transport_used`），或同步收敛文档与枚举声明；并在桌面空闲窗口完成真实 GUI 侧栏人工验收。
6. **Unit 6（拆分 3,413 行单文件测试并接入验收门禁）**：将 `tests/test_roundtrip_and_sync.py` 按领域模块拆分为 `< 800` 行测试文件，固化上述全部回归用例，并将 `skills/verify-codex-agy-sync` 冒验纳入常规回归。

## 记录

- Kotone 判断：a607d12864a349a18ddb0d63345bd3d6；来源：antigravity。
- 2026-11-10：完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md` / `AGENTS.md` / `NOTES.md` 方案初始化。

## 坑

- Antigravity Desktop 2.30+（Hub 架构）使用 `~/.gemini/antigravity/conversation_summaries.db`、`agyhub_summaries_proto.pb` 与 `~/.gemini/config/projects/<uuid>.json`，不再使用旧版 IDE 2.5.5 的 `User/globalStorage/state.vscdb`。
- Antigravity Hub 前端仅展示 `source` 为 `0`、`18`、`20` 的会话，`source = 17`（CLI）会被隐藏到 `externalSummaries`；且标题必须同时写入 `TrajectorySummary.annotations.title`（field 15 -> 1）与 `annotations/<id>.pbtxt`。
- `agy --conversation=<id>` 强依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/` 目录存在，缺失会导致 CLI 挂起或报错。
- Antigravity 2.30+ 的 `steps.step_payload` 二进制并非旧版 `CortexTrajectoryStep`，而是 `third_party/gemini_coder/proto/trajectory.proto` 中的 `gemini_coder.Step`：其中 `step_type = 14` 对应 `Step.user_input`（field 19），`step_type = 15` 对应 `Step.planner_response`（field 20），`step_type = 23` 对应 `Step.checkpoint`（field 30），`step_type = 93/94` 在 `Step` oneof 中分别为 `task_boundary`（field 93）与 `notify_user`（field 94），`step_type = 101` 对应 `Step.system_message`（field 114），`step_type = 132` 对应 `Step.generic`（field 140，内嵌 `exa.cortex.core.tools.*` `google.protobuf.Any` 载荷）。
- 本机独立 CLI `agy` 安装在 `~/.local/bin/agy`（而非旧文档假设的 `~/.local/libexec/agy/agy`），脚本须通过 `ANTIGRAVITY_CLI_BIN` 或 `shutil.which("agy")` 动态发现。
