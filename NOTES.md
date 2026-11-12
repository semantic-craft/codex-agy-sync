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
2. **同文件夹项目自动配对** 自动关联 `~/.gemini/config/projects/<uuid>.json`（`folderUri`）与 `~/.codex/state_5.sqlite`（`projects` + `project_roots`）及 `.codex-global-state.json`，确保导入或同步后的会话直接挂载在两侧桌面端左侧对应项目下（Antigravity `source = 0` 且带合法 `project_id`，不落入 `outside-of-project` 或 `default-cli-project`；Codex `preview <> ''` 且 `archived = 0`）。
3. **细粒度步骤双向对齐** 支持 User 输入（`step_type = 14`）、Planner 回复与思考摘要（`step_type = 15`）、错误消息（`step_type = 17`）、16 类工具调用与返回结果（`step_type = 15/132` 内嵌 `ToolCall`）、阶段边界（`step_type = 23/81/82`）以及伴生日志 `brain/<id>/.system_generated/logs/transcript.jsonl`（含 `antigravity-cli` 镜像）与 `annotations/<id>.pbtxt` 的无损双向映射。
4. **增量接续与分叉保护** 支持在已完成回合边界增量同步，支持 `--include-interrupted` 显式闭合抢救已停止的半途回合（`B2`），记录已同步游标并具备对称分叉派生、持锁写盘与 `BackupManifest.restore()` 自动回滚机制。
5. **端到端实测通过** 项目 `scripts/check` 全量门禁通过，并在隔离沙盒与本机空闲窗口完成端到端验收。

## 当前状态

- 2026-11-10 完成正式项目立项（判断 ID `a607d12864a349a18ddb0d63345bd3d6`，Antigravity 项目 ID `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）、Phase 1 二进制 schema 提取以及初版双向同步原型与首次多模型审查。
- 2026-11-11 完成子智能体血缘隔离、工具路径 `file://` 规范化、二次 `/interrogate` 对抗审查修复（提交 `e56b226`、`f46aac1`），并落地项目本地隔离沙盒验收技能 `skills/verify-codex-agy-sync/SKILL.md` 与全仓代码质量专项评审，制定 6 个模块化重构与功能收口单元。
- 2026-11-12 按 `A2 + B2 + C1` 架构方案依次完成 Unit 1 至 Unit 5 落地并通过 `./scripts/check` 门禁：
  - **Unit 1** 抽离唯一安全 Wire 原语 `src/codex_agy_sync/_wire.py` 与工具载荷模块 `payloads.py`，消除重复 varint 解析器与摘要缓存失步隐患。
  - **Unit 2** 将编解码器拆分为 `src/codex_agy_sync/codecs/` 下 7 个子模块（`schema_tags.py`、`tool_payloads.py`、`tools.py`、`steps.py`、`rollout.py`、`projections.py`、`__init__.py`），挂载真函数表修复列表重复翻倍、补丁块头污染、`scope_items` 与 `thinking_signature` 保真、`step_type = 17` 错误步骤与 `.jsonl.zst` 解码。
  - **Unit 3** 收紧 `SubagentLineage.is_subagent` 正交判定（保护带 `agent_name` 的顶层会话与 Codex 根线程不被误判隐藏），收紧未完结回合水位线过滤，并实现 `B2` `--include-interrupted` 半途回合原生错误闭合、幂等重跑与续聊回传自动替换。
  - **Unit 4** 将桥接层拆分为 `src/codex_agy_sync/bridge/` 子包，实现 RFC 9562 单调 UUIDv7、`acquire_offline_write_locks` 全程持锁临界区、写盘异常自动 `BackupManifest.restore()` 回滚、`NoOpTransition` 伴生索引自愈（含 `antigravity-cli/brain` 镜像）及单次 `sync_session(FORK_NEW_SESSION)` 对称双分支派生。
  - **Unit 5** 新增 `src/codex_agy_sync/bridge/rpc.py`，兑现 `A2` 在线回环只读 RPC（Antigravity `LanguageServerService/GetCascadeTrajectory` Protobuf 客户端与 Codex `app-server` Unix Socket WebSocket `thread/read` 客户端，支持 macOS 软链套接字解析与超长扩展帧），确保活跃运行回合严格返回 `deferred_busy` 且 `SyncReceipt.transport_used` 如实报告实际传输通道，并同步对齐 `C1` 纯按需 CLI 文档。
  - **Unit 6** 将单体测试文件拆解为 11 个 `< 800` 行的专题模块（覆盖率与断言完全保留），新增 `skills/verify-codex-agy-sync/scripts/_suite.py` 7 场景隔离沙盒验证套件，完成三模型对抗审阅（`pro`、`flash`、`inherit`）9 项缺陷修复（`27d1ffd`）与跨平台 32 字节 Linux flock 对齐及 `zstandard` 回退（`9a87882`），全仓 60 项测试与沙盒验收全部通过。
  - **发布与分发** 完成标准 `pyproject.toml`、MIT `LICENSE`、`0.1.0` 版本号声明与控制台入口，推送到 GitHub 远程仓库 `semantic-craft/codex-agy-sync` 并发布首个 Release 标签 `v0.1.0`。

## 下一步

1. **桌面空闲窗口真实侧栏目视抽验** 在本机 Codex Desktop 与 Antigravity Desktop 2.30+ 空闲窗口执行一次真实项目的同步与侧栏目视确认。

## 记录

- Kotone 判断 `a607d12864a349a18ddb0d63345bd3d6`，来源 `antigravity`。
- 2026-11-10 完成立项、四端侧栏挂载（含 Antigravity Hub `CreateProject` 注册 `c13d254c-e8dc-4b56-b9a9-2a8ed562b3a1`）及 `README.md`、`AGENTS.md`、`NOTES.md` 初始化。

## 坑

- Antigravity Desktop 2.30+（Hub 架构）使用 `~/.gemini/antigravity/conversation_summaries.db`、`agyhub_summaries_proto.pb` 与 `~/.gemini/config/projects/<uuid>.json`，不再使用旧版 IDE 2.5.5 的 `User/globalStorage/state.vscdb`。
- Antigravity Hub 前端仅展示 `source` 为 `0`、`18`、`20` 的会话，`source = 17`（CLI）会被隐藏到 `externalSummaries`，且标题必须同时写入 `TrajectorySummary.annotations.title`（field 15 -> 1）与 `annotations/<id>.pbtxt`。
- `agy --conversation=<id>` 强依赖 `~/.gemini/{antigravity,antigravity-cli}/brain/<id>/.system_generated/logs/` 目录存在，缺失会导致 CLI 挂起或报错。
- Antigravity 2.30+ 的 `steps.step_payload` 二进制是 `gemini_coder.Step`，其中 `step_type = 14` 对应 field 19，`step_type = 15` 对应 field 20，`step_type = 17` 对应 field 24，`step_type = 23` 对应 field 30，`step_type = 81/82` 对应 field 93/94，`step_type = 101` 对应 field 114，`step_type = 132` 对应 field 140（内嵌 `exa.cortex.core.tools.*` `google.protobuf.Any` 载荷）。
- macOS 上 Codex Desktop 的 `~/.codex/app-server-control/app-server-control.sock` 是指向 `/private/tmp/codex-...` 的符号链接，探测时必须用 `Path.stat()` 而非 `lstat()`，且连接时优先取解析后更短的目标路径以避开 `AF_UNIX` 104 字节 `sun_path` 上限。
- 本机独立 CLI `agy` 安装在 `~/.local/bin/agy`，脚本须通过 `ANTIGRAVITY_CLI_BIN` 或 `shutil.which("agy")` 动态发现。
