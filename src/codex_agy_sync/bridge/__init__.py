"""Public package surface for `codex_agy_sync.bridge`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..workspace import build_lazy_package_exports

if TYPE_CHECKING:
    from ..workspace import FORBIDDEN_AGY_PROJECT_IDS
    from .handoff import (
        HandoffPendingError,
        HandoffReceipt,
        handoff_ledger,
        handoff_session,
        ledger_seed_fingerprint,
        load_handoff_receipt,
        publish_new_file,
        read_handoff_source,
    )
    from .handoff_reconcile import (
        reconcile_workspace_handoffs,
        summarize_workspace_handoffs,
    )
    from .handoff_resolve import (
        HandoffCancelledError,
        HandoffCandidate,
        ResolvedHandoffRequest,
        UnpairedWorkspaceError,
        WorkspaceBindingDiagnosis,
        diagnose_workspace_binding,
        execute_handoff,
        list_handoff_candidates,
        resolve_handoff_request,
        validate_handoff_selector_flags,
    )
    from .handoff_store import (
        parse_ts_epoch_iso,
        receipt_matches_workspace,
    )
    from .native_codex import NativeCodexImportPending, import_codex_rollout_via_rpc
    from .paths_and_locks import (
        AgyDaemonEndpoint,
        BackupManifest,
        CodexDaemonEndpoint,
        CsrfSecret,
        HostEnvironmentPaths,
        HostLockSnapshot,
        PresenceWalGuard,
        SyncBusyLockError,
        TransportMode,
        atomic_write_bytes,
        atomic_write_text,
        deterministic_uuid,
        expand_existing_with_sidecars,
        has_live_unix_socket,
        is_pid_alive,
        is_sqlite_shm_wal_locked,
        now_iso,
        open_nonblocking_ex_flock,
        open_readonly_sqlite,
        validated_child_path,
    )
    from .project_rpc import (
        delete_agy_cascade_via_rpc,
        delete_agy_project_via_rpc,
        encode_agy_create_project_request,
        register_agy_project_via_rpc,
        register_codex_project_via_rpc,
    )
    from .registry import (
        PairedSessionDescriptor,
        ProjectRegistry,
        WorkspaceProjectBinding,
        build_workspace_binding_diagnosis,
        guarded_pair_workspace,
        pair_workspace_with_mode,
    )
    from .rescue_and_fork import is_source_actively_running
    from .rpc import (
        AgyRpcReadResult,
        CodexRpcReadResult,
        OnlineRpcUnavailableError,
        UnixWebSocketClient,
        extract_codex_rpc_thread,
        fetch_agy_run_statuses_via_rpc,
        fetch_agy_trajectory_via_rpc,
        fetch_codex_thread_via_rpc,
    )
    from .state_machine import (
        NULL_CASCADE_ID,
        NULL_CODEX_THREAD_ID,
        CursorForkStateMachine,
        CursorTransition,
        DeferredBusyTransition,
        DivergedConflictTransition,
        EmptySessionError,
        ForkResolutionPolicy,
        HostTransportPort,
        IncrementalAppendTransition,
        InitialImportTransition,
        NoOpTransition,
        SessionSyncCursor,
        SyncDirection,
        generate_uuid7,
    )
    from .store_catalog import (
        build_combined_lineage,
        compute_codex_nesting_depth,
        discover_agy_subagent_sidecar_ids,
        discover_agy_subagent_sidecars,
        ensure_agy_conversation_db_schema,
        ensure_agy_summaries_db_schema,
        ensure_codex_state_db_schema,
        extract_agy_project_folder_uris,
        extract_rollout_filename_uuid,
        optional_select_columns_sql,
        parse_codex_thread_source_lineage,
        parse_summary_workspace_uris,
        safe_valid_session_id,
        table_column_names,
    )
    from .sync import SyncBridge, SyncReceipt
    from .transport import DefaultHostTransport, atomic_write_rollout

_EXPORT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("..workspace", ("FORBIDDEN_AGY_PROJECT_IDS",)),
    (
        ".handoff",
        (
            "HandoffPendingError", "HandoffReceipt", "handoff_ledger", "handoff_session",
            "ledger_seed_fingerprint", "load_handoff_receipt", "publish_new_file", "read_handoff_source",
        ),
    ),
    (".handoff_reconcile", ("reconcile_workspace_handoffs", "summarize_workspace_handoffs")),
    (
        ".handoff_resolve",
        (
            "HandoffCancelledError", "HandoffCandidate", "ResolvedHandoffRequest",
            "UnpairedWorkspaceError", "WorkspaceBindingDiagnosis", "diagnose_workspace_binding",
            "execute_handoff", "list_handoff_candidates", "resolve_handoff_request",
            "validate_handoff_selector_flags",
        ),
    ),
    (".handoff_store", ("parse_ts_epoch_iso", "receipt_matches_workspace")),
    (".native_codex", ("NativeCodexImportPending", "import_codex_rollout_via_rpc")),
    (
        ".paths_and_locks",
        (
            "AgyDaemonEndpoint", "BackupManifest", "CodexDaemonEndpoint", "CsrfSecret",
            "HostEnvironmentPaths", "HostLockSnapshot", "PresenceWalGuard", "SyncBusyLockError",
            "TransportMode", "atomic_write_bytes", "atomic_write_text", "deterministic_uuid",
            "expand_existing_with_sidecars", "has_live_unix_socket", "is_pid_alive",
            "is_sqlite_shm_wal_locked", "now_iso", "open_nonblocking_ex_flock",
            "open_readonly_sqlite", "validated_child_path",
        ),
    ),
    (
        ".project_rpc",
        (
            "delete_agy_cascade_via_rpc", "delete_agy_project_via_rpc",
            "encode_agy_create_project_request", "register_agy_project_via_rpc",
            "register_codex_project_via_rpc",
        ),
    ),
    (
        ".registry",
        (
            "PairedSessionDescriptor", "ProjectRegistry", "WorkspaceProjectBinding",
            "build_workspace_binding_diagnosis", "guarded_pair_workspace", "pair_workspace_with_mode",
        ),
    ),
    (".rescue_and_fork", ("is_source_actively_running",)),
    (
        ".rpc",
        (
            "AgyRpcReadResult", "CodexRpcReadResult", "OnlineRpcUnavailableError",
            "UnixWebSocketClient", "extract_codex_rpc_thread", "fetch_agy_run_statuses_via_rpc",
            "fetch_agy_trajectory_via_rpc", "fetch_codex_thread_via_rpc",
        ),
    ),
    (
        ".state_machine",
        (
            "NULL_CASCADE_ID", "NULL_CODEX_THREAD_ID", "CursorForkStateMachine", "CursorTransition",
            "DeferredBusyTransition", "DivergedConflictTransition", "EmptySessionError",
            "ForkResolutionPolicy", "HostTransportPort", "IncrementalAppendTransition",
            "InitialImportTransition", "NoOpTransition", "SessionSyncCursor",
            "SyncDirection", "generate_uuid7",
        ),
    ),
    (
        ".store_catalog",
        (
            "build_combined_lineage", "compute_codex_nesting_depth",
            "discover_agy_subagent_sidecar_ids", "discover_agy_subagent_sidecars",
            "ensure_agy_conversation_db_schema", "ensure_agy_summaries_db_schema",
            "ensure_codex_state_db_schema", "extract_agy_project_folder_uris",
            "extract_rollout_filename_uuid", "optional_select_columns_sql",
            "parse_codex_thread_source_lineage", "parse_summary_workspace_uris",
            "safe_valid_session_id", "table_column_names",
        ),
    ),
    (".sync", ("SyncBridge", "SyncReceipt")),
    (".transport", ("DefaultHostTransport", "atomic_write_rollout")),
)
_LAZY_SUBMODULES: frozenset[str] = frozenset({
    "handoff", "handoff_reconcile", "handoff_resolve", "handoff_store",
    "native_codex", "paths_and_locks", "project_rpc", "registry",
    "rescue_and_fork", "rpc", "state_machine", "store_catalog", "sync", "transport",
})
__all__, __getattr__, __dir__ = build_lazy_package_exports(
    __name__,
    globals(),
    _EXPORT_GROUPS,
    _LAZY_SUBMODULES,
)
