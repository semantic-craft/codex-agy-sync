"""Dual-mode `HostTransportPort`, offline store projection writers, and companion store self-heal."""

from __future__ import annotations

import contextlib
import dataclasses
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any, Protocol, Sequence
import uuid

from .._wire import encode_string_field
from ..codecs import (
    AgyRawStepRow, TrajectoryWireCodec, decode_cascade_summary_proto, decode_summaries_state_proto,
    encode_trajectory_metadata_proto, parse_annotation_pbtxt_title, read_codex_rollout_lines,
)
from ..codecs.steps import bind_agy_step
from ..ledger import (
    AgyGuiSafeSource, CanonicalEvent, CascadeId, CascadeSummaryProjection, CodexThreadId,
    HostKind, RolloutLineNo, StepIndex, SubagentLineage, SummariesStateProjection,
    TrajectoryId, TrajectoryLedger, validate_session_id,
)
from .paths_and_locks import (
    HostEnvironmentPaths, HostLockSnapshot, PresenceWalGuard, TransportMode,
    _atomic_write_bytes, _atomic_write_text, _deterministic_uuid, _now_iso,
    _open_readonly_sqlite, _validated_child_path,
)
from .registry import (
    ProjectRegistry, WorkspaceProjectBinding, _ensure_agy_conversation_db_schema,
    _ensure_agy_summaries_db_schema, _ensure_codex_state_db_schema,
)
from .rescue_and_fork import ensure_companion_stores_complete_guarded
from .rpc import read_agy_ledger_dual_mode, read_codex_ledger_dual_mode
from .state_machine import generate_uuid7


def _atomic_write_rollout(path: Path, text: str) -> None:
    if path.name.endswith(".zst"):
        payload = text.encode("utf-8")
        try:
            from compression import zstd  # type: ignore[attr-defined]

            compressed = zstd.compress(payload)
        except ImportError:
            try:
                import zstandard  # type: ignore[import-not-found]

                compressed = zstandard.ZstdCompressor().compress(payload)
            except ImportError:
                compressed = subprocess.run(["zstd", "-q", "-c"], input=payload, capture_output=True, check=True).stdout
        _atomic_write_bytes(path, compressed)
        return
    _atomic_write_text(path, text)


class HostTransportPort(Protocol):
    """Port abstracting Online RPC (`LanguageServerService` / `codex app-server`) and Offline Storage."""

    def read_agy_ledger(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> TrajectoryLedger: ...

    def read_codex_ledger(
        self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> TrajectoryLedger: ...

    def apply_to_agy(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId | None,
        events: Sequence[CanonicalEvent], full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
        parent_cascade_id: CascadeId | None = None,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
        incremental: bool = False, lineage: SubagentLineage | None = None,
        forked_from_cascade_id: CascadeId | None = None,
    ) -> CascadeId: ...

    def apply_to_codex(
        self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId | None,
        events: Sequence[CanonicalEvent], full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
        parent_thread_id: CodexThreadId | None = None,
        incremental: bool = False, lineage: SubagentLineage | None = None,
    ) -> CodexThreadId: ...


def _resolve_effective_agy_lineage(
    parent_cascade_id: CascadeId | None, lineage: SubagentLineage | None,
) -> SubagentLineage:
    eff_parent = parent_cascade_id or (lineage.parent_cascade_id if lineage is not None else None)
    if eff_parent is not None:
        validate_session_id(str(eff_parent), label="parent_cascade_id")
    eff_root = (lineage.root_cascade_id if lineage is not None else None) or eff_parent
    if eff_root is not None:
        validate_session_id(str(eff_root), label="root_cascade_id")
    return SubagentLineage(
        parent_cascade_id=eff_parent, root_cascade_id=eff_root,
        parent_codex_thread_id=lineage.parent_codex_thread_id if lineage is not None else None,
        nesting_depth=lineage.nesting_depth if lineage is not None else 0,
        agent_type_name=lineage.agent_type_name if lineage is not None else "",
        agent_role=lineage.agent_role if lineage is not None else "",
        spawn_step_index=lineage.spawn_step_index if lineage is not None else None,
    )


def _rows_to_raw_agy_steps(rows: Sequence[tuple[Any, ...]]) -> list[AgyRawStepRow]:
    return [
        AgyRawStepRow(
            StepIndex(int(r[0])), int(r[1]), int(r[2]), bool(r[3]),
            bytes(r[4] or b""), bytes(r[5] or b""), int(r[6] or 1),
        )
        for r in rows
    ]


class DefaultHostTransport:
    """Production dual-mode transport reading via `?mode=ro` SQLite/JSONL and writing all host stores."""

    supports_online_rpc_write: bool = False

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        self._paths = paths
        self._codec = TrajectoryWireCodec()

    def _read_agy_explicit_title(self, cascade_id: CascadeId) -> str | None:
        validate_session_id(str(cascade_id), label="cascade_id")
        annot_path = _validated_child_path(self._paths.agy_data_dir, "annotations", f"{cascade_id}.pbtxt", label="cascade_id")
        pbtxt_title = parse_annotation_pbtxt_title(annot_path.read_text(encoding="utf-8", errors="replace")) if annot_path.is_file() else None
        db_title = summary_annot_title = None
        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        if sum_db.is_file():
            try:
                with contextlib.closing(_open_readonly_sqlite(sum_db)) as conn:
                    row = conn.execute("SELECT title, raw_summary FROM conversation_summaries WHERE conversation_id = ?", (str(cascade_id),)).fetchone()
                if row:
                    db_title = str(row[0]).strip() if row[0] and str(row[0]).strip() else None
                    if row[1]:
                        with contextlib.suppress(ValueError):
                            summary_annot_title = decode_cascade_summary_proto(bytes(row[1]), fallback_cascade_id=cascade_id).annotations.title.strip() or None
            except sqlite3.OperationalError:
                pass
        return next((c for c in (pbtxt_title, summary_annot_title, db_title) if c), None)

    def _read_persisted_agy_steps(self, conv_db: Path) -> list[AgyRawStepRow]:
        with contextlib.closing(_open_readonly_sqlite(conv_db)) as conn:
            rows = conn.execute("SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC").fetchall()
        return _rows_to_raw_agy_steps(rows)

    def _read_agy_ledger_disk(self, binding: WorkspaceProjectBinding, cascade_id: CascadeId) -> TrajectoryLedger:
        conv_db = _validated_child_path(self._paths.agy_data_dir, "conversations", f"{cascade_id}.db", label="cascade_id")
        if not conv_db.is_file():
            raise FileNotFoundError(f"Antigravity conversation database not found: {conv_db}")
        return self._codec.decode_agy_steps_to_ledger(
            binding.workspace_path, binding.workspace_uri, self._read_persisted_agy_steps(conv_db),
            explicit_title_override=self._read_agy_explicit_title(cascade_id),
        )

    def read_agy_ledger_with_meta(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId, lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> tuple[TrajectoryLedger, TransportMode, bool]:
        return read_agy_ledger_dual_mode(
            self._codec, binding, cascade_id, lock_snapshot, mode,
            read_disk_fn=lambda: self._read_agy_ledger_disk(binding, cascade_id), read_title_fn=self._read_agy_explicit_title,
        )

    def read_agy_ledger(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId, lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> TrajectoryLedger:
        return self.read_agy_ledger_with_meta(binding, cascade_id, lock_snapshot, mode)[0]

    def _resolve_codex_rollout_candidate(self, raw_path: str, resolved_codex_home: Path) -> Path | None:
        for cand in (Path(raw_path), Path(f"{raw_path}.zst")):
            if cand.is_file() and cand.resolve(strict=False).is_relative_to(resolved_codex_home):
                return cand
        return None

    def _probe_codex_rollout_and_title(self, thread_id: CodexThreadId) -> tuple[Path | None, str | None]:
        validate_session_id(str(thread_id), label="codex_thread_id")
        resolved_codex_home = self._paths.codex_home_dir.resolve(strict=False)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        rollout_path = db_title = None
        if state_db.is_file():
            with contextlib.suppress(sqlite3.OperationalError):
                with contextlib.closing(_open_readonly_sqlite(state_db)) as conn:
                    row = conn.execute("SELECT rollout_path, title, name FROM threads WHERE id = ?", (str(thread_id),)).fetchone()
                if row:
                    rollout_path = self._resolve_codex_rollout_candidate(str(row[0]), resolved_codex_home) if row[0] else None
                    db_title = next((str(tc).strip() for tc in (row[2], row[1]) if tc and str(tc).strip()), None)
        session_idx = self._paths.codex_home_dir / "session_index.jsonl"
        if session_idx.is_file() and not db_title:
            with contextlib.suppress(Exception):
                for line in session_idx.read_text(encoding="utf-8", errors="replace").splitlines():
                    if str(thread_id) in line and (obj := json.loads(line)).get("id") == str(thread_id) and str(obj.get("thread_name") or "").strip():
                        db_title = str(obj["thread_name"]).strip()
        return rollout_path, db_title

    def _find_codex_rollout_and_title(self, thread_id: CodexThreadId) -> tuple[Path, str | None]:
        rollout_path, db_title = self._probe_codex_rollout_and_title(thread_id)
        if rollout_path is None:
            for sub in ("sessions", "archived_sessions"):
                if (sroot := self._paths.codex_home_dir / sub).is_dir():
                    rsroot = sroot.resolve(strict=False)
                    matches = [m for pat in (f"*{thread_id}.jsonl", f"*{thread_id}.jsonl.zst") for m in sorted(sroot.rglob(pat)) if m.is_file() and m.resolve(strict=False).is_relative_to(rsroot)]
                    if matches:
                        rollout_path = matches[-1]
                        break
        if rollout_path is None or not rollout_path.is_file():
            raise FileNotFoundError(f"Codex rollout JSONL file not found for thread {thread_id}")
        return rollout_path, db_title

    def read_codex_ledger_with_meta(
        self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId, lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> tuple[TrajectoryLedger, TransportMode, bool]:
        def _disk() -> TrajectoryLedger:
            rp, title = self._find_codex_rollout_and_title(thread_id)
            return self._codec.decode_codex_rollout_to_ledger(binding.workspace_path, binding.workspace_uri, read_codex_rollout_lines(rp), explicit_title_override=title)

        return read_codex_ledger_dual_mode(
            self._codec, self._paths.codex_home_dir, binding, thread_id, lock_snapshot, mode,
            read_disk_fn=_disk, read_optional_title_fn=lambda tid: self._probe_codex_rollout_and_title(tid)[1],
            resolve_rollout_fn=self._resolve_codex_rollout_candidate,
        )

    def read_codex_ledger(
        self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId, lock_snapshot: HostLockSnapshot, mode: TransportMode,
    ) -> TrajectoryLedger:
        return self.read_codex_ledger_with_meta(binding, thread_id, lock_snapshot, mode)[0]

    def _write_agy_conversation_db(
        self, binding: WorkspaceProjectBinding, target_cid: CascadeId,
        events: Sequence[CanonicalEvent], full_ledger: TrajectoryLedger,
        source_tag: AgyGuiSafeSource, incremental: bool,
        effective_lineage: SubagentLineage, forked_from_cascade_id: CascadeId | None,
    ) -> tuple[Path, list[AgyRawStepRow]]:
        conv_db = _validated_child_path(self._paths.agy_data_dir, "conversations", f"{target_cid}.db", label="cascade_id")
        conv_db.parent.mkdir(parents=True, exist_ok=True)
        traj_id = TrajectoryId(_deterministic_uuid("agy-trajectory", str(target_cid)))
        is_incremental = incremental and conv_db.is_file() and len(events) > 0

        conn = sqlite3.connect(str(conv_db), timeout=10.0)
        try:
            _ensure_agy_conversation_db_schema(conn)
            existing_meta = conn.execute("SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
            if existing_meta:
                traj_id = TrajectoryId(existing_meta[0])
            if is_incremental:
                max_row = conn.execute("SELECT COALESCE(MAX(idx), -1) FROM steps").fetchone()
                start_idx = StepIndex(int(max_row[0]) + 1 if max_row else 0)
                step_rows = self._codec.encode_events_to_agy_steps(events, starting_step_idx=start_idx)
            else:
                conn.execute("DELETE FROM steps")
                step_rows = self._codec.encode_ledger_to_agy_steps(full_ledger, starting_step_idx=StepIndex(0))

            for raw_row in step_rows:
                r = bind_agy_step(raw_row, str(traj_id), str(target_cid))
                conn.execute(
                    "INSERT OR REPLACE INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(r.idx), r.step_type, r.status, 1 if r.has_subtrajectory else 0, r.metadata_blob, r.step_payload_blob, r.step_format),
                )
            conn.execute("DELETE FROM trajectory_meta")
            conn.execute(
                "INSERT INTO trajectory_meta (trajectory_id, cascade_id, trajectory_type, source) VALUES (?, ?, 4, ?)",
                (str(traj_id), str(target_cid), source_tag.proto_enum_value),
            )
            created_iso = full_ledger.events[0].anchor.timestamp_iso if full_ledger.events else _now_iso()
            tmeta_blob = encode_trajectory_metadata_proto(
                project_id=binding.agy_project_id, workspace_uris=(binding.workspace_uri,), created_at_iso=created_iso,
                nesting_depth=effective_lineage.nesting_depth,
                parent_conversation_id=str(effective_lineage.parent_cascade_id or ""),
                root_conversation_id=str(effective_lineage.root_cascade_id or target_cid),
                agent_type_name=effective_lineage.agent_type_name, agent_role=effective_lineage.agent_role,
            )
            conn.execute("INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)", (bytes(tmeta_blob),))
            if forked_from_cascade_id is not None:
                conn.execute("DELETE FROM parent_references")
                conn.execute("INSERT INTO parent_references (idx, data) VALUES (0, ?)", (encode_string_field(1, str(forked_from_cascade_id)),))
            conn.commit()
            persisted_rows = conn.execute(
                "SELECT idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format FROM steps ORDER BY idx ASC"
            ).fetchall()
        finally:
            conn.close()
        return conv_db, _rows_to_raw_agy_steps(persisted_rows)

    def _load_or_seed_hub_summaries_state(self, hub_pb_path: Path, sum_db: Path) -> SummariesStateProjection | None:
        if hub_pb_path.is_file():
            try:
                return decode_summaries_state_proto(hub_pb_path.read_bytes())
            except ValueError:
                pass
        if not sum_db.is_file():
            return None
        seeded: dict[CascadeId, CascadeSummaryProjection] = {}
        try:
            conn = _open_readonly_sqlite(sum_db)
            try:
                rows = conn.execute("SELECT conversation_id, raw_summary FROM conversation_summaries").fetchall()
            finally:
                conn.close()
            for raw_cid, raw_blob in rows:
                if raw_cid and raw_blob:
                    try:
                        cid_obj = CascadeId(validate_session_id(str(raw_cid), label="cascade_id"))
                        seeded[cid_obj] = decode_cascade_summary_proto(bytes(raw_blob), fallback_cascade_id=cid_obj)
                    except ValueError:
                        continue
        except sqlite3.OperationalError:
            return None
        return SummariesStateProjection(summaries=seeded, file_mod_times={}) if seeded else None

    def _write_agy_brain_and_pbtxt(
        self, binding: WorkspaceProjectBinding, target_cid: CascadeId,
        persisted_ledger: TrajectoryLedger, summary_proj: CascadeSummaryProjection,
        viewed_iso: str, effective_lineage: SubagentLineage,
    ) -> None:
        annot_path = _validated_child_path(self._paths.agy_data_dir, "annotations", f"{target_cid}.pbtxt", label="cascade_id")
        _atomic_write_text(annot_path, persisted_ledger.project_annotation_pbtxt(target_cid, viewed_iso).render_pbtxt())

        transcript_rows = persisted_ledger.project_transcript_rows()
        transcript_text = "\n".join(row.to_json_line() for row in transcript_rows) + ("\n" if transcript_rows else "")
        for root_dir in (self._paths.agy_data_dir, self._paths.agy_cli_data_dir):
            if root_dir is not None:
                tx_path = _validated_child_path(
                    root_dir, "brain", str(target_cid), ".system_generated", "logs", "transcript.jsonl", label="cascade_id"
                )
                _atomic_write_text(tx_path, transcript_text)

        eff_parent = effective_lineage.parent_cascade_id
        if eff_parent is not None and effective_lineage.is_subagent:
            sidecar_path = _validated_child_path(
                self._paths.agy_data_dir, "brain", str(eff_parent), ".system_generated", "subagents", f"{target_cid}.json", label="cascade_id"
            )
            sidecar_doc: dict[str, Any] = {
                "conversationId": str(target_cid),
                "subagentDescriptor": {
                    "typeName": effective_lineage.agent_type_name or "self",
                    "role": effective_lineage.agent_role or summary_proj.title,
                },
                "state": "SUBAGENT_STATE_ALIVE",
                "workspaceUris": [str(binding.workspace_uri)],
            }
            if effective_lineage.spawn_step_index is not None:
                sidecar_doc["spawnStepIndex"] = int(effective_lineage.spawn_step_index)
            _atomic_write_text(sidecar_path, json.dumps(sidecar_doc, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _write_agy_companion_stores(
        self, binding: WorkspaceProjectBinding, target_cid: CascadeId,
        persisted_ledger: TrajectoryLedger, persisted_step_count: int,
        conv_db: Path, source_tag: AgyGuiSafeSource, effective_lineage: SubagentLineage,
    ) -> None:
        viewed_iso = _now_iso()
        summary_proj = persisted_ledger.project_agy_cascade_summary(
            cascade_id=target_cid, project_id=binding.agy_project_id, viewed_at_iso=viewed_iso,
            source=source_tag, lineage=effective_lineage,
        )
        if summary_proj.step_count != persisted_step_count:
            summary_proj = dataclasses.replace(summary_proj, step_count=persisted_step_count)
        self._write_agy_brain_and_pbtxt(binding, target_cid, persisted_ledger, summary_proj, viewed_iso, effective_lineage)

        last_user_time, last_user_idx = summary_proj.created_at_iso, -1
        for ev in persisted_ledger.events:
            if ev.kind == "user_input":
                last_user_time = ev.anchor.timestamp_iso or last_user_time
                if ev.anchor.agy_step_index is not None:
                    last_user_idx = int(ev.anchor.agy_step_index)

        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        sconn = sqlite3.connect(str(sum_db), timeout=10.0)
        try:
            _ensure_agy_summaries_db_schema(sconn)
            sconn.execute(
                """
                INSERT OR REPLACE INTO conversation_summaries (
                    conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source,
                    project_id, agent_name, parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                    not_fully_idle, killed, last_user_input_time, last_user_input_step_index, app_data_dir, raw_summary, group_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', ?, ?, ?, ?, ?, '', '', 0, 0, ?, ?, ?, ?, '')
                """,
                (
                    str(target_cid), summary_proj.title, summary_proj.preview, summary_proj.step_count,
                    summary_proj.updated_at_iso, json.dumps([str(u) for u in summary_proj.workspace_uris]),
                    summary_proj.source.summary_column_name, str(summary_proj.project_id), summary_proj.agent_name,
                    str(effective_lineage.parent_cascade_id or ""), summary_proj.nesting_depth, last_user_time,
                    last_user_idx, str(self._paths.agy_data_dir), summary_proj.summary_proto_bytes,
                ),
            )
            sconn.commit()
        finally:
            sconn.close()

        hub_pb_path = self._paths.agy_data_dir / "agyhub_summaries_proto.pb"
        existing_hub = self._load_or_seed_hub_summaries_state(hub_pb_path, sum_db)
        conv_mtime = int(conv_db.stat().st_mtime) if conv_db.is_file() else int(time.time())
        hub_proj = persisted_ledger.project_agy_hub_summaries_state(
            existing_state=existing_hub, updated_summary=summary_proj, conv_db_path=str(conv_db), conv_db_mtime=conv_mtime,
        )
        _atomic_write_bytes(hub_pb_path, hub_proj.proto_bytes)

    def apply_to_agy(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId | None,
        events: Sequence[CanonicalEvent], full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
        parent_cascade_id: CascadeId | None = None,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
        incremental: bool = False, lineage: SubagentLineage | None = None,
        forked_from_cascade_id: CascadeId | None = None,
    ) -> CascadeId:
        if mode != TransportMode.OFFLINE_STORAGE:
            raise RuntimeError(f"apply_to_agy requires TransportMode.OFFLINE_STORAGE (got {mode.value}); refusing to raw-write active WAL storage.")
        if not lock_snapshot.can_write_agy_offline():
            raise RuntimeError("Cannot raw-write Antigravity storage while presence lock, daemon WAL, or in-flight turn is active.")
        if cascade_id is not None:
            validate_session_id(str(cascade_id), label="cascade_id")
        if forked_from_cascade_id is not None:
            validate_session_id(str(forked_from_cascade_id), label="forked_from_cascade_id")

        target_cid = cascade_id or CascadeId(str(uuid.uuid4()))
        validate_session_id(str(target_cid), label="cascade_id")
        eff_lineage = _resolve_effective_agy_lineage(parent_cascade_id, lineage)
        conv_db, raw_persisted = self._write_agy_conversation_db(
            binding, target_cid, events, full_ledger, source_tag, incremental, eff_lineage, forked_from_cascade_id
        )
        persisted_ledger = self._codec.decode_agy_steps_to_ledger(
            workspace_path=binding.workspace_path, workspace_uri=binding.workspace_uri,
            raw_steps=raw_persisted, explicit_title_override=full_ledger.title,
        )
        self._write_agy_companion_stores(binding, target_cid, persisted_ledger, len(raw_persisted), conv_db, source_tag, eff_lineage)
        return target_cid

    def _resolve_or_allocate_codex_rollout_path(self, target_tid: CodexThreadId, *, had_explicit_tid: bool) -> Path:
        resolved_codex_home = self._paths.codex_home_dir.resolve(strict=False)
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        if state_db.is_file() and had_explicit_tid:
            try:
                rconn = _open_readonly_sqlite(state_db)
                try:
                    row = rconn.execute("SELECT rollout_path FROM threads WHERE id = ?", (str(target_tid),)).fetchone()
                finally:
                    rconn.close()
                if row and row[0]:
                    cand = Path(str(row[0]))
                    if not cand.is_file() and Path(f"{cand}.zst").is_file():
                        cand = Path(f"{cand}.zst")
                    if cand.resolve(strict=False).is_relative_to(resolved_codex_home):
                        return cand
            except sqlite3.OperationalError:
                pass

        now_dt = datetime.now(tz=timezone.utc)
        ts_str = now_dt.strftime("%Y-%m-%dT%H-%M-%S")
        rollout_path = _validated_child_path(
            self._paths.codex_home_dir, "sessions", now_dt.strftime("%Y"), now_dt.strftime("%m"),
            now_dt.strftime("%d"), f"rollout-{ts_str}-{target_tid}.jsonl", label="codex_thread_id",
        )
        rollout_path.parent.mkdir(parents=True, exist_ok=True)
        return rollout_path

    def _write_codex_index_and_global_state(
        self, binding: WorkspaceProjectBinding, target_tid: CodexThreadId, title: str,
    ) -> None:
        session_idx_path = self._paths.codex_home_dir / "session_index.jsonl"
        kept_lines: list[str] = []
        if session_idx_path.is_file():
            for ln in session_idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    if json.loads(ln).get("id") == str(target_tid):
                        continue
                except Exception:
                    pass
                kept_lines.append(ln)
        kept_lines.append(json.dumps({"id": str(target_tid), "thread_name": title, "updated_at": _now_iso()}, ensure_ascii=False))
        _atomic_write_text(session_idx_path, "\n".join(kept_lines) + "\n")

        global_state_path = self._paths.codex_home_dir / ".codex-global-state.json"
        gs_data: dict[str, Any] = {}
        if global_state_path.is_file():
            try:
                loaded = json.loads(global_state_path.read_text(encoding="utf-8"))
                gs_data = loaded if isinstance(loaded, dict) else {}
            except Exception:
                gs_data = {}
        assignments = gs_data.get("thread-project-assignments")
        if not isinstance(assignments, dict):
            assignments = {}
            gs_data["thread-project-assignments"] = assignments
        assignments[str(target_tid)] = {"projectKind": "local", "projectId": str(binding.codex_legacy_project_id)}
        _atomic_write_text(global_state_path, json.dumps(gs_data, ensure_ascii=False, indent=2) + "\n")

    def _write_codex_companion_stores(
        self, binding: WorkspaceProjectBinding, target_tid: CodexThreadId,
        rollout_path: Path, full_ledger: TrajectoryLedger,
        lineage: SubagentLineage | None, effective_parent_tid: CodexThreadId | None,
    ) -> None:
        thread_proj = full_ledger.project_codex_thread_summary(thread_id=target_tid, project_id=binding.codex_project_id)
        created_sec = max(1, thread_proj.created_at_ms // 1000)
        updated_sec = max(1, thread_proj.updated_at_ms // 1000)
        eff_agent_role = (lineage.agent_type_name or None) if lineage is not None else None
        eff_agent_nick = (lineage.agent_role or None) if lineage is not None else None
        thread_source_val = (
            json.dumps(
                {"subagent": {"thread_spawn": {
                    "parent_thread_id": str(effective_parent_tid) if effective_parent_tid else None,
                    "depth": max(1, lineage.nesting_depth),
                    "agent_nickname": lineage.agent_role or None,
                    "agent_role": lineage.agent_type_name or None,
                }}},
                ensure_ascii=False, separators=(",", ":"),
            )
            if (lineage is not None and lineage.is_subagent) else "desktop"
        )

        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        conn = sqlite3.connect(str(state_db), timeout=10.0)
        try:
            _ensure_codex_state_db_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, agent_nickname, agent_role, created_at_ms, updated_at_ms,
                    preview, recency_at, recency_at_ms, name, project_id, originator, attached_thread_parent_id
                ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, 'workspace-write', 'never', 0, 1, 0, '0.166.0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex_vscode', ?)
                """,
                (
                    str(target_tid), str(rollout_path), created_sec, updated_sec, thread_source_val,
                    str(thread_proj.cwd), thread_proj.title, thread_proj.preview, eff_agent_nick, eff_agent_role,
                    thread_proj.created_at_ms, thread_proj.updated_at_ms, thread_proj.preview, updated_sec,
                    thread_proj.updated_at_ms, thread_proj.title, str(binding.codex_project_id),
                    str(effective_parent_tid) if effective_parent_tid else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self._write_codex_index_and_global_state(binding, target_tid, thread_proj.title)

    def apply_to_codex(
        self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId | None,
        events: Sequence[CanonicalEvent], full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot, mode: TransportMode,
        parent_thread_id: CodexThreadId | None = None,
        incremental: bool = False, lineage: SubagentLineage | None = None,
    ) -> CodexThreadId:
        if mode != TransportMode.OFFLINE_STORAGE:
            raise RuntimeError(f"apply_to_codex requires TransportMode.OFFLINE_STORAGE (got {mode.value}); refusing to raw-write active WAL storage.")
        if not lock_snapshot.can_write_codex_offline():
            raise RuntimeError("Cannot raw-write Codex storage while app-server daemon, WAL lock, or in-flight turn is active.")
        if thread_id is not None:
            validate_session_id(str(thread_id), label="codex_thread_id")
        if parent_thread_id is not None:
            validate_session_id(str(parent_thread_id), label="parent_codex_thread_id")

        target_tid = thread_id or CodexThreadId(generate_uuid7())
        validate_session_id(str(target_tid), label="codex_thread_id")
        eff_parent_tid = parent_thread_id or (lineage.parent_codex_thread_id if lineage is not None else None)
        if eff_parent_tid is not None:
            validate_session_id(str(eff_parent_tid), label="parent_codex_thread_id")

        rollout_path = self._resolve_or_allocate_codex_rollout_path(target_tid, had_explicit_tid=(thread_id is not None))
        if incremental and rollout_path.is_file() and len(events) > 0:
            prior_lines = [r.raw_json for r in read_codex_rollout_lines(rollout_path)]
            delta_rollout = self._codec.encode_events_to_codex_rollout(
                events, starting_line_no=RolloutLineNo(len(prior_lines) + 1),
                session_id=str(target_tid), cwd=str(binding.workspace_path),
            )
            _atomic_write_rollout(rollout_path, "\n".join([*prior_lines, *(r.raw_json for r in delta_rollout)]) + "\n")
        else:
            full_rollout = self._codec.encode_ledger_to_codex_rollout(
                full_ledger, starting_line_no=RolloutLineNo(1),
                session_id=str(target_tid), cwd=str(binding.workspace_path),
            )
            _atomic_write_rollout(rollout_path, "\n".join(r.raw_json for r in full_rollout) + ("\n" if full_rollout else ""))

        self._write_codex_companion_stores(binding, target_tid, rollout_path, full_ledger, lineage, eff_parent_tid)
        return target_tid

    def _agy_companions_missing(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId, lineage: SubagentLineage,
    ) -> bool:
        if not (self._paths.gemini_config_dir / "projects" / f"{binding.agy_project_id}.json").is_file():
            return True
        if not _validated_child_path(self._paths.agy_data_dir, "annotations", f"{cascade_id}.pbtxt", label="cascade_id").is_file():
            return True
        for root_dir in (self._paths.agy_data_dir, self._paths.agy_cli_data_dir):
            if root_dir is not None and not _validated_child_path(
                root_dir, "brain", str(cascade_id), ".system_generated", "logs", "transcript.jsonl", label="cascade_id"
            ).is_file():
                return True
        if lineage.is_subagent and lineage.parent_cascade_id is not None and not _validated_child_path(
            self._paths.agy_data_dir, "brain", str(lineage.parent_cascade_id), ".system_generated", "subagents", f"{cascade_id}.json", label="cascade_id"
        ).is_file():
            return True

        sum_db = self._paths.agy_data_dir / "conversation_summaries.db"
        hub_pb = self._paths.agy_data_dir / "agyhub_summaries_proto.pb"
        if not sum_db.is_file() or not hub_pb.is_file():
            return True
        try:
            conn = _open_readonly_sqlite(sum_db)
            try:
                has_row = conn.execute("SELECT 1 FROM conversation_summaries WHERE conversation_id = ?", (str(cascade_id),)).fetchone() is not None
            finally:
                conn.close()
            if not has_row:
                return True
            return cascade_id not in decode_summaries_state_proto(hub_pb.read_bytes()).summaries
        except (sqlite3.OperationalError, ValueError):
            return True

    def _codex_companions_missing(self, binding: WorkspaceProjectBinding, thread_id: CodexThreadId) -> bool:
        state_db = self._paths.codex_home_dir / "state_5.sqlite"
        session_idx = self._paths.codex_home_dir / "session_index.jsonl"
        gs_path = self._paths.codex_home_dir / ".codex-global-state.json"
        if not (state_db.is_file() and session_idx.is_file() and gs_path.is_file()):
            return True
        try:
            conn = _open_readonly_sqlite(state_db)
            try:
                has_thread = conn.execute("SELECT 1 FROM threads WHERE id = ?", (str(thread_id),)).fetchone() is not None
                has_proj = conn.execute("SELECT 1 FROM project_roots WHERE project_id = ?", (str(binding.codex_project_id),)).fetchone() is not None
            finally:
                conn.close()
            if not (has_thread and has_proj):
                return True
        except sqlite3.OperationalError:
            return True

        found_idx = any(
            str(thread_id) in ln and json.loads(ln).get("id") == str(thread_id)
            for ln in session_idx.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip().startswith("{")
        )
        if not found_idx:
            return True
        try:
            gs = json.loads(gs_path.read_text(encoding="utf-8"))
        except Exception:
            return True
        if not isinstance(gs, dict):
            return True
        assignments, local_projects = gs.get("thread-project-assignments"), gs.get("local-projects")
        return not (
            isinstance(assignments, dict) and str(thread_id) in assignments
            and isinstance(local_projects, dict) and str(binding.codex_legacy_project_id) in local_projects
        )

    def ensure_companion_stores_complete(
        self, binding: WorkspaceProjectBinding, cascade_id: CascadeId, codex_thread_id: CodexThreadId,
        agy_ledger: TrajectoryLedger | None, codex_ledger: TrajectoryLedger | None,
        locks: HostLockSnapshot, mode: TransportMode, source_tag: AgyGuiSafeSource,
        lineage: SubagentLineage, guard: PresenceWalGuard, registry: ProjectRegistry,
    ) -> WorkspaceProjectBinding:
        return ensure_companion_stores_complete_guarded(
            self, binding, cascade_id, codex_thread_id, agy_ledger, codex_ledger,
            locks, mode, source_tag, lineage, guard, registry,
        )
