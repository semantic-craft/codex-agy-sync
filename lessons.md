# 项目教训

实现、评审或验收前阅读相关条目；用户纠正、返工或漏验暴露可复用教训后补记。
当前仅保留模板，尚未整理经核实的教训；这不表示项目从未出现问题。

## 已核实教训

### 跨宿主轨迹同步的回合游标必须先归一化为顺序回合号再持久化

**双端同步游标（`SessionSyncCursor`）中的 `turn_id` 必须统一归一化为顺序回合标识（`turn-1`, `turn-2`, ...），禁止把单端特有的 UUID 直接写入跨端水位线。**

- 标签：#状态机 #幂等 #契约
- 起因与后果：Codex `rollout-*.jsonl` 在 `task_started` / `turn_context` 中携带 UUID 格式的 `turn_id`，而 Antigravity `steps` 表在 Protobuf wire 层不存储 Codex 回合 UUID，重解码后会按顺序生成 `turn-1`, `turn-2`。首版 `decode_codex_rollout_to_ledger` 直接保留了 Codex UUID 作为 `turn_id`，导致 `CODEX_TO_AGY` 初次导入后立即重跑 `sync_session` 时在 Antigravity 账本中找不到该 UUID 水位线，误判为历史重写分叉（`diverged_halted`）。
- 修复与证据：提交 `837d642` 在 `TrajectoryWireCodec.decode_codex_rollout_to_ledger`（`src/codex_agy_sync/codecs.py`）中将原始 `turn_id` 映射为确定性的顺序 `TurnId("turn-N")`，并在 `decode_agy_steps_to_ledger` 中统一 `task_started` 与 `CHECKPOINT` 的回合切分边界；`tests/test_roundtrip_and_sync.py` 的 `test_real_session_isolated_roundtrip_codex_to_agy_and_back` 实测首次导入后立即重跑返回 `no_op`。
- 正确做法（Done right）：中间账本（`TrajectoryLedger`）的分组键与水位线锚点统一使用与宿主 wire 格式无关的规范化顺序回合号，宿主特有 UUID 放入 `HostExtensionEnvelope` 保真。
- 验证点：运行 `./scripts/check`（含 `test_real_session_isolated_roundtrip_codex_to_agy_and_back` 与 `test_interrogate_adversarial_regressions`）。
- 复发记录：Seen 1x (2026-11-10)；由 `/interrogate` 对抗审查在 commit `dc32f09` 捕获并于 `837d642` 修复。

## 记录模板

复制下列模板新增条目，已有同类条目则更新原条。未知的提交、原因或验收结果明确写“待核实”。
同一事件的多次修复、测试重跑不增加复发次数；只统计有独立证据的再次发生。
记录保留可复用的原因与证据定位；凭据、私人资料及完整日志留在各自获准的位置。

```markdown
### <教训标题>

**<一句话说明下次应遵守的规则。>**

- 标签：<#验收 #凭据 #交接 这类跨项目归类词，1–3 个；`skills candidates` 按标签找跨项目同类教训。>
- 起因与后果：<触发条件、原因、用户可感知的影响；关联 issue 或引入提交，未知则写待核实。>
- 修复与证据：<修复提交或 PR、回归检查与实际验收证据；区分已验证和未验证的部分。>
- 正确做法（Done right）：<下次的具体做法；链接到防复发代码、检查或所属规范。>
- 验证点：<写明之后怎么核对这条已改到位（看哪个文件、跑什么命令、查什么结果）。>
- 复发记录：Seen <N>x (<YYYY-MM-DD, ...>)；<每次独立发生的证据；修复提交数与重试次数不计入。>
```
