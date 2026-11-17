# Codex与AGY会话同频桥 (`codex-agy-sync`)

先读本目录 `NOTES.md` 的“当前状态”和“下一步”，再读 `README.md` 与 `lessons.md`。

## 项目约定

- **严禁裸写活跃 WAL 数据库**：Antigravity Desktop 的 `language_server` 与 Codex Desktop 的 `codex app-server` 均以 WAL 模式持有 SQLite 连接。读取运行中数据库必须使用 `SQLITE_OPEN_READONLY`（`?mode=ro`）或调用宿主本地 RPC（`LanguageServerService` Connect RPC / `codex app-server` JSON-RPC v2）；离线或确认无活跃锁（`~/.gemini/antigravity/presence/<id>.lock` 与 `-wal`）时才执行受控写入并先做备份。
- **凭据与 Token 隔离**：从 `~/.gemini/antigravity/daemon/hub_<port>.json` 读取的 `csrfToken` 仅限内存中用于本地 loopback 请求头 `x-codeium-csrf-token`，严禁写入日志、测试快照、文档或 Git 仓库。
- **环境路径不写死**：家目录、数据目录（`~/.gemini/antigravity`、`~/.gemini/antigravity-cli`、`~/.codex`）与客户端二进制路径均通过环境变量、配置或运行时发现解析，不硬编码机器用户名。
- **验证入口**：所有代码改动通过本仓 `scripts/check` 验证；CLI 与多存储端到端行为验收走项目本地技能 `skills/verify-codex-agy-sync/SKILL.md`（证据输出至 `$HOME/Projects/_artifacts/codex-agy-sync/verify-<run-id>/`，严禁使用 `/tmp`）；一次性逆向快照放 `out/`（已加入 `.gitignore`），不提交入库。
- **签名与公证发布**：macOS 签名与公证构建遵循 `agent-home` `WORKING_STANDARDS.md` 统一约定，通过 `./scripts/release_macos.sh`（或 `./scripts/build_menubar_app.sh --release`）经 `NOTARY_PROFILE` / `macos-notary-profile` 与 `macos-notary` 完成 Developer ID 签名与公证，禁止硬编码本机 profile 名称或路径。
