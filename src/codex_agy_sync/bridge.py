"""Project pairing registry, WAL/presence lock guard, cursor/fork state machine, and `SyncBridge` facade.

Invariants encoded in this module:
1. `CsrfSecret` wraps the loopback `x-codeium-csrf-token` read from
   `~/.gemini/antigravity/daemon/hub_<port>.json` with `__repr__` and `__str__`
   redacted so it can never leak into logs, tracebacks, or serialized state.
2. `HostEnvironmentPaths` resolves all directories dynamically from environment
   variables or `Path.home()`, never hardcoding a machine username.
3. `PresenceWalGuard` enforces `SQLITE_OPEN_READONLY` (`?mode=ro` + `PRAGMA query_only=ON`)
   on all SQLite reads, blocks offline writes whenever `presence/<cascade_id>.lock`
   is held (`flock(LOCK_EX | LOCK_NB)` raises `EWOULDBLOCK`), a live daemon holds an
   active `-wal` file, or a turn is in-flight, and produces a self-describing
   `BackupManifest` (grafted from Candidate 1) before any offline mutation.
4. `CursorForkStateMachine` is a pure state machine that compares source and target
   `CompletedTurnWatermark` heads against `SessionSyncCursor`, advancing only on
   completed turns and pre-planning fork/overwrite targets (`DivergedConflictTransition`,
   grafted from Candidate 1) when both hosts advanced independently (per
   `principle-model-the-domain` and `principle-make-operations-idempotent`).
5. `SyncBridge` is a deep facade hiding multi-store project registration, dual-mode
   RPC/SQLite selection, companion file projection, and atomic cursor persistence
   behind four high-level entrypoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence, Tuple, TypeAlias

from .ledger import (
    AgyGuiSafeSource,
    AgyProjectId,
    CanonicalEvent,
    CascadeId,
    CodexLegacyProjectId,
    CodexProjectId,
    CodexThreadId,
    CompletedTurnWatermark,
    ContentDigest,
    HostKind,
    RolloutLineNo,
    StepIndex,
    TrajectoryLedger,
    TurnId,
    WorkspacePath,
    WorkspaceRootUri,
)

# ---------------------------------------------------------------------------
# Environment & Secret Boundary Types (per principle-boundary-discipline)
# ---------------------------------------------------------------------------


class CsrfSecret:
    """In-memory-only wrapper for `x-codeium-csrf-token` from `hub_<port>.json`.

    Invariant: `__repr__`, `__str__`, and `__format__` always return `"[REDACTED_CSRF]"`
    so the secret never appears in logs, exception messages, or snapshots.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        # TODO: Validate non-empty token and store in self._token.
        raise NotImplementedError("CsrfSecret.__init__ stub")

    def as_loopback_header(self) -> Mapping[str, str]:
        """Return `{"x-codeium-csrf-token": self._token}` for 127.0.0.1 loopback RPCs only."""
        # TODO: return {"x-codeium-csrf-token": self._token}
        raise NotImplementedError("CsrfSecret.as_loopback_header stub")

    def __repr__(self) -> str:
        return "CsrfSecret('[REDACTED_CSRF]')"

    def __str__(self) -> str:
        return "[REDACTED_CSRF]"

    def __format__(self, format_spec: str) -> str:
        return "[REDACTED_CSRF]"


@dataclass(frozen=True)
class HostEnvironmentPaths:
    """Runtime-resolved host storage roots with zero hardcoded machine usernames."""

    gemini_config_dir: Path
    agy_data_dir: Path
    codex_home_dir: Path
    sync_state_dir: Path

    @classmethod
    def discover(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> HostEnvironmentPaths:
        """Resolve paths from `ANTIGRAVITY_DATA_DIR`, `GEMINI_CONFIG_DIR`, `CODEX_HOME`, or `Path.home()`."""
        # TODO:
        # Read environment overrides if provided, falling back to Path.home() / ".gemini" / ...
        # and Path.home() / ".codex". Never hardcode OS user home prefixes.
        raise NotImplementedError("HostEnvironmentPaths.discover stub")


# ---------------------------------------------------------------------------
# Bidirectional Project Pairing Registry & Sidebar Invariants
# ---------------------------------------------------------------------------

FORBIDDEN_AGY_PROJECT_IDS: frozenset[str] = frozenset(
    {"", "default-cli-project", "outside-of-project", "null", "none"}
)


@dataclass(frozen=True)
class WorkspaceProjectBinding:
    """Verified bidirectional project identity for a single workspace folder.

    Invariants:
    - `agy_project_id` is never in `FORBIDDEN_AGY_PROJECT_IDS` and corresponds to
      `~/.gemini/config/projects/<agy_project_id>.json` with `folderUri == workspace_uri`.
    - `codex_project_id` exists in `state_5.sqlite` (`projects` + `project_roots`
      with `path == str(workspace_path)`) and is linked in `.codex-global-state.json`
      via `codex_legacy_project_id`.
    """

    workspace_path: WorkspacePath
    workspace_uri: WorkspaceRootUri
    workspace_name: str
    agy_project_id: AgyProjectId
    codex_project_id: CodexProjectId
    codex_legacy_project_id: CodexLegacyProjectId


@dataclass(frozen=True)
class PairedSessionDescriptor:
    """Summary of a discovered or paired conversation across Antigravity and Codex."""

    workspace: WorkspaceProjectBinding
    cascade_id: CascadeId | None
    codex_thread_id: CodexThreadId | None
    title: str
    agy_step_count: int
    codex_turn_count: int
    parent_cascade_id: CascadeId | None = None
    parent_codex_thread_id: CodexThreadId | None = None


class ProjectRegistry:
    """Owns bidirectional workspace-to-project resolution and sidebar-visibility repair.

    Hides the 4 Antigravity stores (`projects/<id>.json`, `trajectory_metadata_blob`,
    `conversation_summaries.db`, `agyhub_summaries_proto.pb` + `annotations/<id>.pbtxt`)
    and the 2 Codex stores (`state_5.sqlite` + `.codex-global-state.json`).
    """

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        # TODO: Store `paths` and initialize read-only discovery caches.
        raise NotImplementedError("ProjectRegistry.__init__ stub")

    def ensure_workspace_binding(
        self,
        workspace: str | Path,
        prefer_online_rpc: bool = True,
    ) -> WorkspaceProjectBinding:
        """Resolve or idempotently register both hosts' project records for `workspace`.

        Invariants (per `principle-make-operations-idempotent`):
        - Calling this method repeatedly for the same folder converges to the exact
          same `(AgyProjectId, CodexProjectId, CodexLegacyProjectId)` triple without
          creating duplicate projects.
        - Uses `LanguageServerService/CreateProject` and `codex app-server` `project/create`
          (with deterministic `idempotencyKey`) when online, or atomic file/SQLite updates
          when offline under `PresenceWalGuard`.
        """
        # TODO:
        # 1. Normalize via `parse_workspace_identity(workspace)`.
        # 2. Scan `~/.gemini/config/projects/*.json` for matching `folderUri`; if absent or in
        #    `FORBIDDEN_AGY_PROJECT_IDS`, create a fresh UUID project record.
        # 3. Query `state_5.sqlite` (`mode=ro`) `project_roots` for `str(workspace_path)` and
        #    `.codex-global-state.json` for `local-projects` / `app-server-project-id-by-legacy-project-id-by-host`.
        # 4. Return validated `WorkspaceProjectBinding`.
        raise NotImplementedError("ProjectRegistry.ensure_workspace_binding stub")

    def list_workspace_sessions(
        self,
        binding: WorkspaceProjectBinding,
    ) -> Tuple[PairedSessionDescriptor, ...]:
        """List all sessions belonging to `binding` across both hosts."""
        # TODO:
        # Read Antigravity `conversation_summaries.db` (`mode=ro`) + Codex `state_5.sqlite` (`mode=ro`),
        # join against existing `SessionSyncCursor` records in `sync_state_dir`, and return descriptors.
        raise NotImplementedError("ProjectRegistry.list_workspace_sessions stub")


# ---------------------------------------------------------------------------
# Presence, WAL Concurrency Guard & Self-Describing `BackupManifest`
# ---------------------------------------------------------------------------


class TransportMode(str, Enum):
    """Selected execution mode for reading or writing a host session."""

    AUTO = "auto"
    ONLINE_RPC = "online_rpc"
    OFFLINE_STORAGE = "offline_storage"


@dataclass(frozen=True)
class AgyDaemonEndpoint:
    """Live Antigravity `LanguageServerService` endpoint discovered from `hub_<port>.json`."""

    pid: int
    http_port: int
    https_port: int
    ls_version: str
    csrf_secret: CsrfSecret


@dataclass(frozen=True)
class CodexDaemonEndpoint:
    """Live `codex app-server` JSON-RPC v2 Unix socket endpoint."""

    socket_path: Path
    pid: int | None = None


@dataclass(frozen=True)
class HostLockSnapshot:
    """Point-in-time concurrency and liveness probe for a session on both hosts."""

    agy_daemon: AgyDaemonEndpoint | None
    agy_presence_locked: bool
    agy_wal_active: bool
    agy_turn_inflight: bool
    codex_daemon: CodexDaemonEndpoint | None
    codex_wal_active: bool
    codex_turn_inflight: bool

    def can_write_agy_offline(self) -> bool:
        """True iff no live daemon, presence flock, or in-flight turn holds the cascade."""
        # TODO: return (self.agy_daemon is None) and (not self.agy_presence_locked) and (not self.agy_wal_active) and (not self.agy_turn_inflight)
        raise NotImplementedError("HostLockSnapshot.can_write_agy_offline stub")

    def can_write_codex_offline(self) -> bool:
        """True iff no live `codex app-server` or active WAL/in-flight turn holds the thread."""
        # TODO: return (self.codex_daemon is None) and (not self.codex_wal_active) and (not self.codex_turn_inflight)
        raise NotImplementedError("HostLockSnapshot.can_write_codex_offline stub")


@dataclass(frozen=True)
class BackupManifest:
    """Self-describing atomic backup manifest created before any offline mutation (grafted from Candidate 1)."""

    manifest_id: str
    created_at_iso: str
    target_host: HostKind
    target_session_id: str
    backup_dir: Path
    backed_up_files: Tuple[Path, ...]
    file_sha256_map: Mapping[str, str]
    restoration_command: str


class PresenceWalGuard:
    """Enforces read-only SQLite access, `flock` presence probing, and pre-write `BackupManifest` creation."""

    def __init__(self, paths: HostEnvironmentPaths) -> None:
        # TODO: Store `paths`.
        raise NotImplementedError("PresenceWalGuard.__init__ stub")

    def probe_locks(
        self,
        cascade_id: CascadeId | None,
        codex_thread_id: CodexThreadId | None,
    ) -> HostLockSnapshot:
        """Probe `~/.gemini/antigravity/presence/<cascade_id>.lock` via non-blocking `flock`,
        check `-wal` files and daemon liveness (`hub_<port>.json` PID + loopback ping,
        Codex Unix domain socket).
        """
        # TODO:
        # 1. Open `presence/<cascade_id>.lock` and test `fcntl.flock(fd, LOCK_EX | LOCK_NB)`;
        #    if `BlockingIOError` / `EWOULDBLOCK`, `agy_presence_locked = True`; otherwise release immediately.
        # 2. Inspect `daemon/hub_*.json`, verify PID liveness via `os.kill(pid, 0)`, wrap token in `CsrfSecret`.
        # 3. Check `-wal` existence/size for `conversations/<cascade_id>.db-wal`,
        #    `conversation_summaries.db-wal`, and `state_5.sqlite-wal`.
        raise NotImplementedError("PresenceWalGuard.probe_locks stub")

    def sqlite_readonly_uri(self, db_path: Path) -> str:
        """Build a `file:<path>?mode=ro` SQLite URI for safe concurrent WAL reading."""
        # TODO: return f"{db_path.resolve().as_uri()}?mode=ro"
        raise NotImplementedError("PresenceWalGuard.sqlite_readonly_uri stub")

    def create_offline_backup(
        self,
        target_host: HostKind,
        target_session_id: str,
        target_files: Sequence[Path],
    ) -> BackupManifest:
        """Create an atomic timestamped backup under `sync_state_dir/backups/` and write `manifest.json`."""
        # TODO:
        # 1. Create `sync_state_dir/backups/<target_session_id>_<timestamp>/`.
        # 2. Copy existing files with `shutil.copy2` and compute SHA-256 hashes.
        # 3. Write `manifest.json` and return `BackupManifest` with executable `restoration_command`.
        raise NotImplementedError("PresenceWalGuard.create_offline_backup stub")


# ---------------------------------------------------------------------------
# Incremental Cursor & Fork State Machine (`CursorForkStateMachine`)
# ---------------------------------------------------------------------------


class ForkResolutionPolicy(str, Enum):
    """Explicit operator policy when both hosts have advanced past the last sync cursor."""

    HALT_ON_DIVERGENCE = "halt_on_divergence"
    FORK_NEW_SESSION = "fork_new_session"
    BACKUP_AND_OVERWRITE_TARGET = "backup_and_overwrite_target"


class SyncDirection(str, Enum):
    """Direction of synchronization for a paired session."""

    AUTO_INCREMENTAL = "auto_incremental"
    AGY_TO_CODEX = "agy_to_codex"
    CODEX_TO_AGY = "codex_to_agy"


@dataclass(frozen=True)
class SessionSyncCursor:
    """Persisted watermark state for a paired `(CascadeId, CodexThreadId)` session.

    Invariants:
    - Stored atomically via temp-file + `os.replace` under `sync_state_dir/cursors/`.
    - Only updated after target host write + companion projections succeed.
    """

    workspace_uri: WorkspaceRootUri
    cascade_id: CascadeId
    codex_thread_id: CodexThreadId
    last_synced_turn_id: TurnId
    last_synced_agy_step_idx: StepIndex
    last_synced_codex_line_no: RolloutLineNo
    agy_boundary_digest: ContentDigest
    codex_boundary_digest: ContentDigest
    updated_at_iso: str


@dataclass(frozen=True)
class NoOpTransition:
    """Neither host has completed a new turn since `cursor`."""

    kind: Literal["no_op"]
    cursor: SessionSyncCursor
    deferred_inflight_host: HostKind | None = None


@dataclass(frozen=True)
class InitialImportTransition:
    """One host has completed turns and the peer session does not yet exist or is empty."""

    kind: Literal["initial_import"]
    source_host: HostKind
    target_host: HostKind
    events_to_apply: Tuple[CanonicalEvent, ...]
    new_watermark: CompletedTurnWatermark
    selected_transport: TransportMode


@dataclass(frozen=True)
class IncrementalAppendTransition:
    """Exactly one host has advanced past `cursor` while the target still matches `cursor`."""

    kind: Literal["incremental_append"]
    source_host: HostKind
    target_host: HostKind
    cursor: SessionSyncCursor
    delta_events: Tuple[CanonicalEvent, ...]
    new_watermark: CompletedTurnWatermark
    selected_transport: TransportMode


@dataclass(frozen=True)
class DivergedConflictTransition:
    """Both hosts advanced independently past `cursor` or boundary digests mismatched.

    Grafted from Candidate 1 (`ForkSessionPlan`): pre-computes the common ancestor
    watermark, pre-allocates `preallocated_fork_session_id`, and slices
    `fork_events_to_apply` during pure state-machine planning so execution is
    purely mechanical.
    """

    kind: Literal["diverged"]
    source_host: HostKind
    target_host: HostKind
    cursor: SessionSyncCursor
    common_ancestor_watermark: CompletedTurnWatermark | None
    agy_head: CompletedTurnWatermark | None
    codex_head: CompletedTurnWatermark | None
    reason: str
    resolution_policy: ForkResolutionPolicy
    preallocated_fork_session_id: str | None = None
    fork_events_to_apply: Tuple[CanonicalEvent, ...] = ()
    selected_transport: TransportMode = TransportMode.AUTO


@dataclass(frozen=True)
class DeferredBusyTransition:
    """Target host has an in-flight turn or holds a lock incompatible with the requested mode."""

    kind: Literal["deferred_busy"]
    blocking_host: HostKind
    reason: str
    retry_after_ms: int = 1500


CursorTransition: TypeAlias = (
    NoOpTransition
    | InitialImportTransition
    | IncrementalAppendTransition
    | DivergedConflictTransition
    | DeferredBusyTransition
)


class CursorForkStateMachine:
    """Pure state machine computing the sync transition from source/target ledgers and cursor."""

    @staticmethod
    def evaluate(
        agy_ledger: TrajectoryLedger | None,
        codex_ledger: TrajectoryLedger | None,
        cursor: SessionSyncCursor | None,
        direction: SyncDirection,
        lock_snapshot: HostLockSnapshot,
        transport_mode: TransportMode,
        fork_policy: ForkResolutionPolicy,
    ) -> CursorTransition:
        """Compute the deterministic `CursorTransition` without performing I/O.

        Transition table (from Phase A grounding):
        1. If target is busy (`agy_turn_inflight` / `codex_turn_inflight`) or offline mode
           was requested while target WAL/presence lock is held -> `DeferredBusyTransition`.
        2. If `cursor is None` and exactly one side has a `latest_watermark()` ->
           `InitialImportTransition` carrying `slice_after_watermark(None)`.
        3. If `cursor is not None`:
           - Verify that `cursor.last_synced_turn_id` still exists in both ledgers with
             matching `boundary_digest`. If either digest changed (history rewritten),
             emit `DivergedConflictTransition` with pre-computed common ancestor and fork slice.
           - Let `agy_advanced = agy_head.last_agy_step_index > cursor.last_synced_agy_step_idx`
             and `codex_advanced = codex_head.last_codex_line_no > cursor.last_synced_codex_line_no`.
           - Neither advanced -> `NoOpTransition`.
           - Only one advanced (compatible with `direction`) -> `IncrementalAppendTransition`
             carrying `source_ledger.slice_after_watermark(cursor.last_synced_turn_id)`.
           - Both advanced -> `DivergedConflictTransition` with `preallocated_fork_session_id`
             and `fork_events_to_apply`.
        """
        # TODO:
        # Implement the pure 5-branch decision table above over `CompletedTurnWatermark`
        # and `HostLockSnapshot`.
        raise NotImplementedError("CursorForkStateMachine.evaluate stub")


# ---------------------------------------------------------------------------
# Dual-Mode Transport Port (`HostTransportPort`) & Deep Facade (`SyncBridge`)
# ---------------------------------------------------------------------------


class HostTransportPort(Protocol):
    """Port abstracting Online RPC (`LanguageServerService` / `codex app-server`) and Offline Storage."""

    def read_agy_ledger(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        """Read an Antigravity cascade via `GetCascadeTrajectorySteps` RPC or `?mode=ro` SQLite."""
        ...

    def read_codex_ledger(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
    ) -> TrajectoryLedger:
        """Read a Codex thread via `thread/read` JSON-RPC v2 or `rollout-*.jsonl`."""
        ...

    def apply_to_agy(
        self,
        binding: WorkspaceProjectBinding,
        cascade_id: CascadeId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_cascade_id: CascadeId | None = None,
    ) -> CascadeId:
        """Write delta events and companion projections (`transcript.jsonl`, `annotations/<id>.pbtxt`,
        `conversation_summaries.db`, `agyhub_summaries_proto.pb`) to Antigravity via Online Connect RPC
        (`StartCascade`, `SendUserCascadeMessage`, `UpdateConversationAnnotations`) or Offline WAL-safe write.
        """
        ...

    def apply_to_codex(
        self,
        binding: WorkspaceProjectBinding,
        thread_id: CodexThreadId | None,
        events: Sequence[CanonicalEvent],
        full_ledger: TrajectoryLedger,
        lock_snapshot: HostLockSnapshot,
        mode: TransportMode,
        parent_thread_id: CodexThreadId | None = None,
    ) -> CodexThreadId:
        """Write delta events and thread index metadata (`state_5.sqlite` `threads` with `preview != ''`
        & `archived = 0`, `.codex-global-state.json` `thread-project-assignments`, `rollout-*.jsonl`)
        via Online `codex app-server` JSON-RPC v2 or Offline WAL-safe write.
        """
        ...


@dataclass(frozen=True)
class SyncReceipt:
    """Auditable outcome returned by `SyncBridge.sync_session`."""

    workspace: WorkspaceProjectBinding
    cascade_id: CascadeId
    codex_thread_id: CodexThreadId
    transition_kind: Literal[
        "no_op",
        "initial_import",
        "incremental_append",
        "diverged_halted",
        "diverged_forked",
        "diverged_overwritten",
        "deferred_busy",
    ]
    transport_used: TransportMode
    events_applied: int
    updated_cursor: SessionSyncCursor | None
    forked_from_cascade_id: CascadeId | None = None
    forked_from_codex_thread_id: CodexThreadId | None = None
    backup_manifest: BackupManifest | None = None


class SyncBridge:
    """Deep public facade for `codex-agy-sync`.

    Concentrates project pairing, lock probing, wire decoding, cursor/fork state
    machine evaluation, dual-mode write execution, and companion artifact projection
    behind a narrow, idempotent interface (per `architect` interface-depth rules).
    """

    def __init__(
        self,
        paths: HostEnvironmentPaths | None = None,
        transport: HostTransportPort | None = None,
    ) -> None:
        """Initialize the sync bridge with runtime-discovered paths and default dual-mode transport."""
        # TODO:
        # Resolve `paths = paths or HostEnvironmentPaths.discover()`, instantiate
        # `ProjectRegistry(paths)` and `PresenceWalGuard(paths)`.
        raise NotImplementedError("SyncBridge.__init__ stub")

    def pair_workspace(
        self,
        workspace: str | Path,
        *,
        prefer_online_rpc: bool = True,
    ) -> WorkspaceProjectBinding:
        """Ensure both Antigravity and Codex have paired, sidebar-visible project entries for `workspace`."""
        # TODO: Delegate to `self._registry.ensure_workspace_binding(workspace, prefer_online_rpc)`.
        raise NotImplementedError("SyncBridge.pair_workspace stub")

    def inspect_session_ledger(
        self,
        workspace: str | Path,
        *,
        cascade_id: CascadeId | None = None,
        codex_thread_id: CodexThreadId | None = None,
        mode: TransportMode = TransportMode.AUTO,
    ) -> TrajectoryLedger:
        """Read either host's trajectory into an indexed `TrajectoryLedger` without mutating state."""
        # TODO:
        # 1. Resolve `binding = self.pair_workspace(workspace)`.
        # 2. Probe locks via `PresenceWalGuard.probe_locks(cascade_id, codex_thread_id)`.
        # 3. Read and decode into `TrajectoryLedger` via `HostTransportPort`.
        raise NotImplementedError("SyncBridge.inspect_session_ledger stub")

    def sync_session(
        self,
        workspace: str | Path,
        *,
        cascade_id: CascadeId | None = None,
        codex_thread_id: CodexThreadId | None = None,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL,
        mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
        source_tag: AgyGuiSafeSource = AgyGuiSafeSource.CASCADE_CLIENT,
    ) -> SyncReceipt:
        """Synchronize one paired session (or import an unpaired session) idempotently.

        Invariants:
        - Re-running `sync_session` with identical host state produces `transition_kind="no_op"`
          and writes zero bytes (per `principle-make-operations-idempotent`).
        - Never writes to an active WAL database when `PresenceWalGuard` reports a live lock;
          automatically selects Online RPC when the target daemon is running and `mode=AUTO`,
          or returns `transition_kind="deferred_busy"`.
        - When both hosts have advanced past `SessionSyncCursor`, obeys `fork_policy`
          (`HALT_ON_DIVERGENCE` returns `diverged_halted`; `FORK_NEW_SESSION` creates a linked
          child session with `parent_conversation_id` / `attached_thread_parent_id`;
          `BACKUP_AND_OVERWRITE_TARGET` creates a `BackupManifest` via `create_offline_backup`
          before replacing).
        """
        # TODO:
        # 1. `binding = self.pair_workspace(workspace)`
        # 2. `locks = self._guard.probe_locks(cascade_id, codex_thread_id)`
        # 3. Load existing `SessionSyncCursor` and read `agy_ledger` / `codex_ledger`.
        # 4. `transition = CursorForkStateMachine.evaluate(...)`
        # 5. Execute transition via `HostTransportPort`, project companion artifacts,
        #    atomically persist updated `SessionSyncCursor`, and return `SyncReceipt`.
        raise NotImplementedError("SyncBridge.sync_session stub")

    def sync_workspace(
        self,
        workspace: str | Path,
        *,
        direction: SyncDirection = SyncDirection.AUTO_INCREMENTAL,
        mode: TransportMode = TransportMode.AUTO,
        fork_policy: ForkResolutionPolicy = ForkResolutionPolicy.HALT_ON_DIVERGENCE,
    ) -> Tuple[SyncReceipt, ...]:
        """Discover and synchronize all sessions in `workspace`."""
        # TODO:
        # List sessions via `self._registry.list_workspace_sessions(binding)` and invoke
        # `self.sync_session(...)` for each session in deterministic order.
        raise NotImplementedError("SyncBridge.sync_workspace stub")
