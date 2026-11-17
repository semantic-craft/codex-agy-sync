//! Host OS adapters: read-only SQLite/catalog scanners, native `libc` locks, loopback RPC, and sync engine.

pub mod backup;
pub mod catalog;
pub mod error;
pub mod locks;
pub mod menubar_feed;
pub mod paths;
pub mod registry;
pub mod rpc_agy;
pub mod rpc_codex;
pub mod sqlite;
pub mod sync_engine;
pub mod transport;

pub use backup::{BackupManifest, create_offline_backup_manifest};
pub use catalog::{
    AgyCatalogEntry, AgySummaryRow, CodexCatalogEntry, CodexProjectRow, CodexThreadRow,
    CombinedSessionDescriptor, ParsedCodexSourceLineage, PreloadedHostTables,
    assert_source_belongs_to_binding, build_combined_lineage, compute_codex_nesting_depth,
    count_codex_rollout_user_turns, discover_agy_entries, discover_agy_subagent_sidecar_ids,
    discover_agy_subagent_sidecars, discover_codex_entries, enrich_agy_session_from_conv_db,
    extract_agy_project_folder_uris, find_readonly_agy_project_id, infer_direction_from_source_id,
    load_preloaded_host_tables, parse_codex_thread_source_lineage, parse_summary_workspace_uris,
    probe_agy_ownership, probe_codex_ownership, scan_agy_summaries_with_foreign_ids,
    uris_match_workspace,
};
pub use error::HostError;
pub use locks::{
    FlockGuard, OfflineWriteLockGuard, PresenceWalGuard, has_live_unix_socket, is_pid_alive,
    is_sqlite_shm_wal_locked, open_handoff_lock, open_nonblocking_ex_flock,
    unix_socket_connect_path,
};
pub use menubar_feed::{
    MenubarCandidateState, MenubarFeedSnapshot, MenubarSessionItem, build_menubar_feed,
};
pub use paths::{
    HostEnvironmentPaths, atomic_copy_file, atomic_write_bytes, atomic_write_text,
    candidate_home_dirs, deterministic_uuid, expand_existing_with_sidecars,
    format_epoch_precise_iso, format_epoch_utc_iso, is_excluded_for_homes, now_iso,
    parse_ts_epoch_iso, parse_ws_ident, validated_child_path,
};
pub use registry::{
    PairedSessionDescriptor, ProjectRegistry, WorkspaceBindingDiagnosis, WorkspaceProjectBinding,
    build_workspace_binding_diagnosis, diagnose_workspace_binding, guarded_pair_workspace,
    pair_workspace_with_mode,
};
pub use rpc_agy::{
    AgyHttpConn, AgyRpcReadResult, encode_agy_create_project_request,
    fetch_agy_run_statuses_via_rpc, fetch_agy_trajectory_via_rpc, register_agy_project_via_rpc,
};
pub use rpc_codex::{
    CodexRpcReadResult, extract_codex_rpc_thread, fetch_codex_thread_via_rpc,
    import_codex_rollout_via_rpc, register_codex_project_via_rpc,
};
pub use sqlite::{
    ensure_agy_conversation_db_schema, ensure_agy_summaries_db_schema,
    ensure_codex_state_db_schema, open_locked_rw_sqlite, open_readonly_sqlite,
    optional_select_columns_sql, table_column_names,
};
pub use sync_engine::{
    HandoffCandidate, InspectCounts, InspectReport, InspectWatermark, ReconcileEntry,
    ReconcileReport, ResolvedHandoffRequest, StatusChainItem, StatusPendingItem, StatusReceiptItem,
    WorkspaceHandoffsSummary, execute_handoff, handoff_ledger, handoff_session,
    inspect_session_ledger, is_source_actively_running, list_handoff_candidates, load_all_receipts,
    load_workspace_receipts, read_handoff_source, reconcile_workspace_handoffs,
    resolve_handoff_request, summarize_workspace_handoffs, validate_handoff_selector_flags,
};
pub use transport::{DefaultHostTransport, atomic_write_rollout, resolve_effective_agy_lineage};
