//! RFC 9562 monotonic UUIDv7 generator and pure `CursorForkStateMachine`.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::Utc;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::error::CoreError;
use crate::events::{CanonicalEvent, CompletedTurnWatermark};
use crate::id::{
    CascadeId, CodexThreadId, ContentDigest, CsrfSecret, HostKind, RolloutLineNo, StepIndex,
    TurnId, WorkspaceRootUri,
};
use crate::ledger::TrajectoryLedger;

pub const NULL_CASCADE_ID_STR: &str = "00000000-0000-0000-0000-000000000000";
pub const NULL_CODEX_THREAD_ID_STR: &str = "00000000-0000-0000-0000-000000000000";

static UUID7_STATE: Mutex<(u64, u16)> = Mutex::new((0, 0));

/// Generate a monotonic RFC 9562 UUIDv7 lowercase `8-4-4-4-12` string.
#[must_use]
pub fn generate_uuid7() -> String {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64);

    let rand_bytes = *Uuid::new_v4().as_bytes();
    let rand_11 = u16::from_be_bytes([rand_bytes[0], rand_bytes[1]]) & 0x07FF;

    let (ts_ms, seq_12) = {
        let mut guard = UUID7_STATE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if now_ms > guard.0 {
            guard.0 = now_ms;
            guard.1 = rand_11;
        } else {
            guard.1 += 1;
            if guard.1 > 0x0FFF {
                guard.0 += 1;
                guard.1 = rand_11;
            }
        }
        (guard.0 & 0xFFFF_FFFF_FFFF, guard.1 & 0x0FFF)
    };

    let rand_62 = u64::from_be_bytes([
        rand_bytes[2],
        rand_bytes[3],
        rand_bytes[4],
        rand_bytes[5],
        rand_bytes[6],
        rand_bytes[7],
        rand_bytes[8],
        rand_bytes[9],
    ]) & ((1_u64 << 62) - 1);

    let uuid_u128: u128 = (u128::from(ts_ms) << 80)
        | (0x7_u128 << 76)
        | (u128::from(seq_12) << 64)
        | (0b10_u128 << 62)
        | u128::from(rand_62);
    Uuid::from_u128(uuid_u128).to_string()
}

fn allocate_host_session_uuid(target_host: HostKind) -> String {
    match target_host {
        HostKind::Codex => generate_uuid7(),
        HostKind::Antigravity => Uuid::new_v4().to_string(),
    }
}

fn now_utc_iso() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// Selected execution mode for reading or writing a host session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum TransportMode {
    #[default]
    Auto,
    OnlineRpc,
    OfflineStorage,
}

impl TransportMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::OnlineRpc => "online_rpc",
            Self::OfflineStorage => "offline_storage",
        }
    }
}

/// Explicit operator policy when both hosts have advanced past the last sync cursor.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ForkResolutionPolicy {
    #[default]
    HaltOnDivergence,
    ForkNewSession,
    BackupAndOverwriteTarget,
}

/// Direction of synchronization for a paired session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SyncDirection {
    #[default]
    AutoIncremental,
    AgyToCodex,
    CodexToAgy,
}

impl SyncDirection {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AutoIncremental => "auto_incremental",
            Self::AgyToCodex => "agy_to_codex",
            Self::CodexToAgy => "codex_to_agy",
        }
    }
}

/// Pairing policy replacing `(pair_if_offline: bool, auto_pair: bool)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum PairingPolicy {
    #[default]
    RequirePaired,
    AutoPair,
    OfflinePairOnly,
}

/// Exclusive CLI selector for `sync`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SyncSelector {
    SourceId(String),
    Latest,
    Chain {
        source_id: Option<String>,
    },
    Pick,
    Reconcile {
        source_id: Option<String>,
        dry_run: bool,
    },
}

/// Live Antigravity `LanguageServerService` endpoint discovered from `hub_<port>.json`.
#[derive(Debug)]
pub struct AgyDaemonEndpoint {
    pub pid: u32,
    pub http_port: u16,
    pub https_port: u16,
    pub ls_version: String,
    pub csrf_secret: CsrfSecret,
}

/// Live `codex app-server` JSON-RPC v2 Unix socket endpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexDaemonEndpoint {
    pub socket_path: PathBuf,
    pub pid: Option<u32>,
}

/// Point-in-time concurrency and liveness probe for a session on both hosts.
#[derive(Debug, Default)]
pub struct HostLockSnapshot {
    pub agy_daemon: Option<AgyDaemonEndpoint>,
    pub agy_presence_locked: bool,
    pub agy_wal_active: bool,
    pub agy_turn_inflight: bool,
    pub codex_daemon: Option<CodexDaemonEndpoint>,
    pub codex_wal_active: bool,
    pub codex_turn_inflight: bool,
}

impl HostLockSnapshot {
    #[must_use]
    pub fn can_write_agy_offline(&self) -> bool {
        self.agy_daemon.is_none()
            && !self.agy_presence_locked
            && !self.agy_wal_active
            && !self.agy_turn_inflight
    }

    #[must_use]
    pub fn can_write_codex_offline(&self) -> bool {
        self.codex_daemon.is_none() && !self.codex_wal_active && !self.codex_turn_inflight
    }
}

/// Persisted watermark state for a paired `(CascadeId, CodexThreadId)` session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionSyncCursor {
    pub workspace_uri: WorkspaceRootUri,
    pub cascade_id: CascadeId,
    pub codex_thread_id: CodexThreadId,
    pub last_synced_turn_id: TurnId,
    pub last_synced_agy_step_idx: StepIndex,
    pub last_synced_codex_line_no: RolloutLineNo,
    pub agy_boundary_digest: ContentDigest,
    pub codex_boundary_digest: ContentDigest,
    pub updated_at_iso: String,
    #[serde(default)]
    pub rescued_inflight_turn: bool,
    #[serde(default)]
    pub pre_rescue_source_host: String,
}

/// Pure transition computed by [`CursorForkStateMachine::evaluate`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CursorTransition {
    NoOp {
        cursor: SessionSyncCursor,
        deferred_inflight_host: Option<HostKind>,
    },
    InitialImport {
        source_host: HostKind,
        target_host: HostKind,
        events_to_apply: Vec<CanonicalEvent>,
        new_watermark: CompletedTurnWatermark,
        selected_transport: TransportMode,
    },
    IncrementalAppend {
        source_host: HostKind,
        target_host: HostKind,
        cursor: SessionSyncCursor,
        delta_events: Vec<CanonicalEvent>,
        new_watermark: CompletedTurnWatermark,
        selected_transport: TransportMode,
    },
    Diverged {
        source_host: HostKind,
        target_host: HostKind,
        cursor: SessionSyncCursor,
        common_ancestor_watermark: Option<CompletedTurnWatermark>,
        agy_head: Option<CompletedTurnWatermark>,
        codex_head: Option<CompletedTurnWatermark>,
        reason: String,
        resolution_policy: ForkResolutionPolicy,
        preallocated_fork_session_id: String,
        fork_events_to_apply: Vec<CanonicalEvent>,
        selected_transport: TransportMode,
    },
    DeferredBusy {
        blocking_host: HostKind,
        reason: String,
        retry_after_ms: u32,
        agy_busy: bool,
        codex_busy: bool,
    },
}

fn deferred_busy(blocking_host: HostKind, reason: impl Into<String>) -> CursorTransition {
    CursorTransition::DeferredBusy {
        blocking_host,
        reason: reason.into(),
        retry_after_ms: 1500,
        agy_busy: blocking_host == HostKind::Antigravity,
        codex_busy: blocking_host == HostKind::Codex,
    }
}

struct DivergenceInput<'a> {
    source_host: HostKind,
    source_ledger: Option<&'a TrajectoryLedger>,
    cursor: SessionSyncCursor,
    ancestor_wm: Option<CompletedTurnWatermark>,
    agy_head: Option<CompletedTurnWatermark>,
    codex_head: Option<CompletedTurnWatermark>,
    reason: &'a str,
}

/// Pure state machine computing sync transitions without I/O.
pub struct CursorForkStateMachine;

struct EvalContext<'a> {
    lock_snapshot: &'a HostLockSnapshot,
    transport_mode: TransportMode,
    fork_policy: ForkResolutionPolicy,
    online_rpc_write_supported: bool,
}

impl CursorForkStateMachine {
    fn select_transport_or_defer(
        target_host: HostKind,
        lock_snapshot: &HostLockSnapshot,
        requested_mode: TransportMode,
        online_rpc_write_supported: bool,
    ) -> Result<TransportMode, Box<CursorTransition>> {
        if target_host == HostKind::Antigravity {
            if lock_snapshot.agy_turn_inflight {
                return Err(Box::new(deferred_busy(
                    HostKind::Antigravity,
                    "Antigravity target session has an in-flight turn.",
                )));
            }
            if requested_mode == TransportMode::OnlineRpc && online_rpc_write_supported {
                if lock_snapshot.agy_daemon.is_none() {
                    return Err(Box::new(deferred_busy(
                        HostKind::Antigravity,
                        "Online RPC requested for Antigravity, but no live LanguageServer daemon is available.",
                    )));
                }
                return Ok(TransportMode::OnlineRpc);
            }
            if lock_snapshot.can_write_agy_offline() {
                return Ok(TransportMode::OfflineStorage);
            }
            if requested_mode == TransportMode::OnlineRpc {
                return Err(Box::new(deferred_busy(
                    target_host,
                    "Online write RPC for antigravity is not enabled; deferring to avoid raw-writing active WAL storage.",
                )));
            }
            return Err(Box::new(deferred_busy(
                HostKind::Antigravity,
                "Antigravity session is locked by an active presence flock or WAL daemon.",
            )));
        }

        if lock_snapshot.codex_turn_inflight {
            return Err(Box::new(deferred_busy(
                HostKind::Codex,
                "Codex target thread has an in-flight turn.",
            )));
        }
        if requested_mode == TransportMode::OnlineRpc && online_rpc_write_supported {
            if lock_snapshot.codex_daemon.is_none() {
                return Err(Box::new(deferred_busy(
                    HostKind::Codex,
                    "Online RPC requested for Codex, but no live app-server socket is available.",
                )));
            }
            return Ok(TransportMode::OnlineRpc);
        }
        if lock_snapshot.can_write_codex_offline() {
            return Ok(TransportMode::OfflineStorage);
        }
        if requested_mode == TransportMode::OnlineRpc {
            return Err(Box::new(deferred_busy(
                target_host,
                "Online write RPC for codex is not enabled; deferring to avoid raw-writing active WAL storage.",
            )));
        }
        Err(Box::new(deferred_busy(
            HostKind::Codex,
            "Codex thread cannot be written safely right now due to active WAL lock.",
        )))
    }

    fn plan_divergence(
        div: DivergenceInput<'_>,
        ctx: &EvalContext<'_>,
    ) -> Result<CursorTransition, CoreError> {
        let target_host = if div.source_host == HostKind::Codex {
            HostKind::Antigravity
        } else {
            HostKind::Codex
        };
        let sel = match Self::select_transport_or_defer(
            target_host,
            ctx.lock_snapshot,
            ctx.transport_mode,
            ctx.online_rpc_write_supported,
        ) {
            Ok(m) => m,
            Err(defer) => return Ok(*defer),
        };
        let fork_events = match div.source_ledger {
            Some(l) => l.slice_after_watermark(None)?.to_vec(),
            None => Vec::new(),
        };
        Ok(CursorTransition::Diverged {
            source_host: div.source_host,
            target_host,
            cursor: div.cursor,
            common_ancestor_watermark: div.ancestor_wm,
            agy_head: div.agy_head,
            codex_head: div.codex_head,
            reason: div.reason.to_owned(),
            resolution_policy: ctx.fork_policy,
            preallocated_fork_session_id: allocate_host_session_uuid(target_host),
            fork_events_to_apply: fork_events,
            selected_transport: sel,
        })
    }

    fn build_initial_import(
        source_host: HostKind,
        target_host: HostKind,
        source_ledger: Option<&TrajectoryLedger>,
        source_head: Option<&CompletedTurnWatermark>,
        ctx: &EvalContext<'_>,
    ) -> Result<CursorTransition, CoreError> {
        let host_label = match source_host {
            HostKind::Antigravity => "Antigravity session",
            HostKind::Codex => "Codex thread",
        };
        let (Some(ledger), Some(head)) = (source_ledger, source_head) else {
            if source_ledger.is_some_and(|l| l.has_inflight_turn) {
                return Ok(deferred_busy(
                    source_host,
                    format!("Source {host_label} only has an uncompleted in-flight turn."),
                ));
            }
            return Err(CoreError::EmptySession(format!(
                "Source {host_label} has no completed turns to sync."
            )));
        };
        let sel = match Self::select_transport_or_defer(
            target_host,
            ctx.lock_snapshot,
            ctx.transport_mode,
            ctx.online_rpc_write_supported,
        ) {
            Ok(m) => m,
            Err(defer) => return Ok(*defer),
        };
        Ok(CursorTransition::InitialImport {
            source_host,
            target_host,
            events_to_apply: ledger.slice_after_watermark(None)?.to_vec(),
            new_watermark: head.clone(),
            selected_transport: sel,
        })
    }

    fn converged_noop(
        cursor: &SessionSyncCursor,
        agy_ledger: Option<&TrajectoryLedger>,
        codex_ledger: Option<&TrajectoryLedger>,
        agy_head: &CompletedTurnWatermark,
        codex_head: &CompletedTurnWatermark,
    ) -> CursorTransition {
        let inflight = if agy_ledger.is_some_and(|l| l.has_inflight_turn) {
            Some(HostKind::Antigravity)
        } else if codex_ledger.is_some_and(|l| l.has_inflight_turn) {
            Some(HostKind::Codex)
        } else {
            None
        };
        CursorTransition::NoOp {
            cursor: SessionSyncCursor {
                workspace_uri: cursor.workspace_uri.clone(),
                cascade_id: cursor.cascade_id.clone(),
                codex_thread_id: cursor.codex_thread_id.clone(),
                last_synced_turn_id: agy_head.turn_id.clone(),
                last_synced_agy_step_idx: agy_head.last_agy_step_index,
                last_synced_codex_line_no: codex_head.last_codex_line_no,
                agy_boundary_digest: agy_head.boundary_digest.clone(),
                codex_boundary_digest: codex_head.boundary_digest.clone(),
                updated_at_iso: now_utc_iso(),
                rescued_inflight_turn: false,
                pre_rescue_source_host: String::new(),
            },
            deferred_inflight_host: inflight,
        }
    }

    /// Compute the deterministic [`CursorTransition`] without performing I/O.
    ///
    /// # Errors
    /// Returns [`CoreError::EmptySession`] when neither side has completed turns and neither is in-flight.
    #[allow(clippy::too_many_arguments)]
    pub fn evaluate(
        agy_ledger: Option<&TrajectoryLedger>,
        codex_ledger: Option<&TrajectoryLedger>,
        cursor: Option<&SessionSyncCursor>,
        direction: SyncDirection,
        lock_snapshot: &HostLockSnapshot,
        transport_mode: TransportMode,
        fork_policy: ForkResolutionPolicy,
        online_rpc_write_supported: bool,
    ) -> Result<CursorTransition, CoreError> {
        let agy_head = agy_ledger.and_then(TrajectoryLedger::latest_watermark);
        let codex_head = codex_ledger.and_then(TrajectoryLedger::latest_watermark);
        let ctx = EvalContext {
            lock_snapshot,
            transport_mode,
            fork_policy,
            online_rpc_write_supported,
        };

        let Some(cur) = cursor else {
            return Self::evaluate_without_cursor(
                agy_ledger,
                codex_ledger,
                agy_head,
                codex_head,
                direction,
                &ctx,
            );
        };

        if agy_ledger.is_none() || codex_ledger.is_none() {
            return Self::evaluate_missing_peer_with_cursor(
                agy_ledger,
                codex_ledger,
                agy_head,
                codex_head,
                cur,
                direction,
                &ctx,
            );
        }

        let Some(agy_l) = agy_ledger else {
            return Err(CoreError::EmptySession(
                "Missing Antigravity ledger.".to_owned(),
            ));
        };
        let Some(codex_l) = codex_ledger else {
            return Err(CoreError::EmptySession("Missing Codex ledger.".to_owned()));
        };

        let agy_anchor_wm = agy_l.find_watermark(&cur.last_synced_turn_id);
        let codex_anchor_wm = codex_l.find_watermark(&cur.last_synced_turn_id);
        let agy_mismatch =
            agy_anchor_wm.is_none_or(|wm| wm.boundary_digest != cur.agy_boundary_digest);
        let codex_mismatch =
            codex_anchor_wm.is_none_or(|wm| wm.boundary_digest != cur.codex_boundary_digest);

        if agy_mismatch || codex_mismatch {
            if let (Some(ah), Some(ch)) = (agy_head, codex_head) {
                if ah.turn_id == ch.turn_id && ah.boundary_digest == ch.boundary_digest {
                    return Ok(Self::converged_noop(
                        cur,
                        Some(agy_l),
                        Some(codex_l),
                        ah,
                        ch,
                    ));
                }
            }
            let src_host = if direction == SyncDirection::CodexToAgy {
                HostKind::Codex
            } else {
                HostKind::Antigravity
            };
            let src_ledger = if src_host == HostKind::Codex {
                Some(codex_l)
            } else {
                Some(agy_l)
            };
            return Self::plan_divergence(
                DivergenceInput {
                    source_host: src_host,
                    source_ledger: src_ledger,
                    cursor: cur.clone(),
                    ancestor_wm: None,
                    agy_head: agy_head.cloned(),
                    codex_head: codex_head.cloned(),
                    reason: "Watermark turn missing or boundary digest mutated since last sync.",
                },
                &ctx,
            );
        }

        let (Some(ah), Some(ch)) = (agy_head, codex_head) else {
            return Err(CoreError::EmptySession(
                "Watermark matched without completed head.".to_owned(),
            ));
        };
        let agy_advanced = ah.turn_id != cur.last_synced_turn_id
            || ah.last_agy_step_index > cur.last_synced_agy_step_idx;
        let codex_advanced = ch.turn_id != cur.last_synced_turn_id
            || ch.last_codex_line_no > cur.last_synced_codex_line_no;

        if !agy_advanced && !codex_advanced {
            let inflight = if agy_l.has_inflight_turn {
                Some(HostKind::Antigravity)
            } else if codex_l.has_inflight_turn {
                Some(HostKind::Codex)
            } else {
                None
            };
            return Ok(CursorTransition::NoOp {
                cursor: cur.clone(),
                deferred_inflight_host: inflight,
            });
        }

        if agy_advanced && !codex_advanced {
            if direction == SyncDirection::CodexToAgy {
                return Self::plan_divergence(
                    DivergenceInput {
                        source_host: HostKind::Codex,
                        source_ledger: Some(codex_l),
                        cursor: cur.clone(),
                        ancestor_wm: agy_anchor_wm.cloned(),
                        agy_head: Some(ah.clone()),
                        codex_head: Some(ch.clone()),
                        reason: "Target Antigravity session has uncommitted turns ahead of Codex source.",
                    },
                    &ctx,
                );
            }
            let sel = match Self::select_transport_or_defer(
                HostKind::Codex,
                ctx.lock_snapshot,
                ctx.transport_mode,
                ctx.online_rpc_write_supported,
            ) {
                Ok(m) => m,
                Err(defer) => return Ok(*defer),
            };
            return Ok(CursorTransition::IncrementalAppend {
                source_host: HostKind::Antigravity,
                target_host: HostKind::Codex,
                cursor: cur.clone(),
                delta_events: agy_l
                    .slice_after_watermark(Some(&cur.last_synced_turn_id))?
                    .to_vec(),
                new_watermark: ah.clone(),
                selected_transport: sel,
            });
        }

        if codex_advanced && !agy_advanced {
            if direction == SyncDirection::AgyToCodex {
                return Self::plan_divergence(
                    DivergenceInput {
                        source_host: HostKind::Antigravity,
                        source_ledger: Some(agy_l),
                        cursor: cur.clone(),
                        ancestor_wm: codex_anchor_wm.cloned(),
                        agy_head: Some(ah.clone()),
                        codex_head: Some(ch.clone()),
                        reason: "Target Codex thread has uncommitted turns ahead of Antigravity source.",
                    },
                    &ctx,
                );
            }
            let sel = match Self::select_transport_or_defer(
                HostKind::Antigravity,
                ctx.lock_snapshot,
                ctx.transport_mode,
                ctx.online_rpc_write_supported,
            ) {
                Ok(m) => m,
                Err(defer) => return Ok(*defer),
            };
            return Ok(CursorTransition::IncrementalAppend {
                source_host: HostKind::Codex,
                target_host: HostKind::Antigravity,
                cursor: cur.clone(),
                delta_events: codex_l
                    .slice_after_watermark(Some(&cur.last_synced_turn_id))?
                    .to_vec(),
                new_watermark: ch.clone(),
                selected_transport: sel,
            });
        }

        if ah.turn_id == ch.turn_id && ah.boundary_digest == ch.boundary_digest {
            return Ok(Self::converged_noop(
                cur,
                Some(agy_l),
                Some(codex_l),
                ah,
                ch,
            ));
        }

        let src_host = if direction == SyncDirection::CodexToAgy {
            HostKind::Codex
        } else {
            HostKind::Antigravity
        };
        let src_ledger = if src_host == HostKind::Codex {
            Some(codex_l)
        } else {
            Some(agy_l)
        };
        Self::plan_divergence(
            DivergenceInput {
                source_host: src_host,
                source_ledger: src_ledger,
                cursor: cur.clone(),
                ancestor_wm: agy_anchor_wm.cloned(),
                agy_head: Some(ah.clone()),
                codex_head: Some(ch.clone()),
                reason: "Both Antigravity and Codex advanced independently past the last sync watermark.",
            },
            &ctx,
        )
    }

    fn evaluate_without_cursor(
        agy_ledger: Option<&TrajectoryLedger>,
        codex_ledger: Option<&TrajectoryLedger>,
        agy_head: Option<&CompletedTurnWatermark>,
        codex_head: Option<&CompletedTurnWatermark>,
        direction: SyncDirection,
        ctx: &EvalContext<'_>,
    ) -> Result<CursorTransition, CoreError> {
        if agy_head.is_none() && codex_head.is_none() {
            let agy_inf = agy_ledger.is_some_and(|l| l.has_inflight_turn);
            let codex_inf = codex_ledger.is_some_and(|l| l.has_inflight_turn);
            if agy_inf || codex_inf {
                let blocking = if agy_inf {
                    HostKind::Antigravity
                } else {
                    HostKind::Codex
                };
                return Ok(CursorTransition::DeferredBusy {
                    blocking_host: blocking,
                    reason: "deferred_busy".to_owned(),
                    retry_after_ms: 1500,
                    agy_busy: agy_inf,
                    codex_busy: codex_inf,
                });
            }
        }
        if codex_head.is_none()
            && (direction == SyncDirection::AgyToCodex
                || (direction == SyncDirection::AutoIncremental && agy_head.is_some()))
        {
            return Self::build_initial_import(
                HostKind::Antigravity,
                HostKind::Codex,
                agy_ledger,
                agy_head,
                ctx,
            );
        }
        if direction == SyncDirection::AgyToCodex && agy_head.is_none() {
            return Self::build_initial_import(
                HostKind::Antigravity,
                HostKind::Codex,
                agy_ledger,
                agy_head,
                ctx,
            );
        }
        if agy_head.is_none()
            && (direction == SyncDirection::CodexToAgy
                || (direction == SyncDirection::AutoIncremental && codex_head.is_some()))
        {
            return Self::build_initial_import(
                HostKind::Codex,
                HostKind::Antigravity,
                codex_ledger,
                codex_head,
                ctx,
            );
        }
        if direction == SyncDirection::CodexToAgy && codex_head.is_none() {
            return Self::build_initial_import(
                HostKind::Codex,
                HostKind::Antigravity,
                codex_ledger,
                codex_head,
                ctx,
            );
        }

        if let (Some(ah), Some(ch)) = (agy_head, codex_head) {
            let ws_uri = match agy_ledger {
                Some(l) => l.workspace_uri.clone(),
                None => WorkspaceRootUri::parse("file:///workspace")?,
            };
            let null_cid = CascadeId::parse(NULL_CASCADE_ID_STR)?;
            let null_tid = CodexThreadId::parse(NULL_CODEX_THREAD_ID_STR)?;
            if ah.turn_id == ch.turn_id && ah.boundary_digest == ch.boundary_digest {
                return Ok(CursorTransition::NoOp {
                    cursor: SessionSyncCursor {
                        workspace_uri: ws_uri,
                        cascade_id: null_cid,
                        codex_thread_id: null_tid,
                        last_synced_turn_id: ah.turn_id.clone(),
                        last_synced_agy_step_idx: ah.last_agy_step_index,
                        last_synced_codex_line_no: ch.last_codex_line_no,
                        agy_boundary_digest: ah.boundary_digest.clone(),
                        codex_boundary_digest: ch.boundary_digest.clone(),
                        updated_at_iso: now_utc_iso(),
                        rescued_inflight_turn: false,
                        pre_rescue_source_host: String::new(),
                    },
                    deferred_inflight_host: None,
                });
            }
            let src_host = if direction == SyncDirection::CodexToAgy {
                HostKind::Codex
            } else {
                HostKind::Antigravity
            };
            let src_ledger = if src_host == HostKind::Codex {
                codex_ledger
            } else {
                agy_ledger
            };
            let synth_ws_uri = src_ledger.map_or(ws_uri, |l| l.workspace_uri.clone());
            let synth_cursor = SessionSyncCursor {
                workspace_uri: synth_ws_uri,
                cascade_id: null_cid,
                codex_thread_id: null_tid,
                last_synced_turn_id: TurnId::parse("turn-0")?,
                last_synced_agy_step_idx: StepIndex(0),
                last_synced_codex_line_no: RolloutLineNo(0),
                agy_boundary_digest: ah.boundary_digest.clone(),
                codex_boundary_digest: ch.boundary_digest.clone(),
                updated_at_iso: now_utc_iso(),
                rescued_inflight_turn: false,
                pre_rescue_source_host: String::new(),
            };
            return Self::plan_divergence(
                DivergenceInput {
                    source_host: src_host,
                    source_ledger: src_ledger,
                    cursor: synth_cursor,
                    ancestor_wm: None,
                    agy_head: Some(ah.clone()),
                    codex_head: Some(ch.clone()),
                    reason: "Both sessions already contain independent non-empty histories without a prior sync cursor.",
                },
                ctx,
            );
        }

        Err(CoreError::EmptySession(
            "Neither Antigravity nor Codex has any completed turns to synchronize.".to_owned(),
        ))
    }

    fn evaluate_missing_peer_with_cursor(
        agy_ledger: Option<&TrajectoryLedger>,
        codex_ledger: Option<&TrajectoryLedger>,
        agy_head: Option<&CompletedTurnWatermark>,
        codex_head: Option<&CompletedTurnWatermark>,
        cursor: &SessionSyncCursor,
        direction: SyncDirection,
        ctx: &EvalContext<'_>,
    ) -> Result<CursorTransition, CoreError> {
        if agy_ledger.is_none() && codex_ledger.is_none() {
            return Ok(CursorTransition::NoOp {
                cursor: cursor.clone(),
                deferred_inflight_host: None,
            });
        }
        let (surviving_host, missing_host, surviving_ledger, surviving_head) =
            if agy_ledger.is_none() {
                (
                    HostKind::Codex,
                    HostKind::Antigravity,
                    codex_ledger,
                    codex_head,
                )
            } else {
                (HostKind::Antigravity, HostKind::Codex, agy_ledger, agy_head)
            };
        let Some(surv_l) = surviving_ledger else {
            return Ok(CursorTransition::NoOp {
                cursor: cursor.clone(),
                deferred_inflight_host: None,
            });
        };
        if surviving_head.is_none() {
            if surv_l.has_inflight_turn {
                return Ok(deferred_busy(surviving_host, "deferred_busy"));
            }
            return Ok(CursorTransition::NoOp {
                cursor: cursor.clone(),
                deferred_inflight_host: None,
            });
        }
        let from_missing = (agy_ledger.is_none() && direction == SyncDirection::AgyToCodex)
            || (codex_ledger.is_none() && direction == SyncDirection::CodexToAgy);
        if from_missing && ctx.fork_policy != ForkResolutionPolicy::ForkNewSession {
            if ctx.fork_policy == ForkResolutionPolicy::HaltOnDivergence {
                return Self::plan_divergence(
                    DivergenceInput {
                        source_host: surviving_host,
                        source_ledger: Some(surv_l),
                        cursor: cursor.clone(),
                        ancestor_wm: None,
                        agy_head: agy_head.cloned(),
                        codex_head: codex_head.cloned(),
                        reason: "Source peer session was deleted since last sync.",
                    },
                    ctx,
                );
            }
            return Err(CoreError::EmptySession(
                "Cannot overwrite surviving session from a deleted peer.".to_owned(),
            ));
        }
        if ctx.fork_policy == ForkResolutionPolicy::ForkNewSession {
            return Self::plan_divergence(
                DivergenceInput {
                    source_host: surviving_host,
                    source_ledger: Some(surv_l),
                    cursor: cursor.clone(),
                    ancestor_wm: None,
                    agy_head: agy_head.cloned(),
                    codex_head: codex_head.cloned(),
                    reason: "Peer session missing; re-materializing onto missing host.",
                },
                ctx,
            );
        }
        Self::build_initial_import(
            surviving_host,
            missing_host,
            Some(surv_l),
            surviving_head,
            ctx,
        )
    }
}
