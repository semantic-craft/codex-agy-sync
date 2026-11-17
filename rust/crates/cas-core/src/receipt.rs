//! Sync receipt model with `#[serde(try_from, into)]` JSON parity and chain-tip DAG traversal.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::CoreError;
use crate::events::{CanonicalEvent, is_host_injected_user_event};
use crate::ledger::TrajectoryLedger;

/// Raw JSON wire layout matching Python `HandoffReceipt` (`handoffs/*.json` and CLI stdout).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RawSyncReceipt {
    pub workspace: String,
    pub source_host: String,
    pub source_id: String,
    pub source_digest: String,
    pub target_host: String,
    #[serde(default)]
    pub target_id: Option<String>,
    pub title: String,
    pub status: String,
    pub staging_path: String,
    #[serde(default = "default_true")]
    pub runtime_state_not_transferred: bool,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub created_at_iso: String,
    #[serde(default)]
    pub chain_id: Option<String>,
    #[serde(default)]
    pub chained_from_source_id: Option<String>,
    #[serde(default)]
    pub chain_depth: i64,
    #[serde(default)]
    pub target_seed_turn_count: Option<i64>,
    #[serde(default)]
    pub target_seed_text_digest: Option<String>,
    #[serde(default)]
    pub pairing_mode: Option<String>,
}

const fn default_true() -> bool {
    true
}

/// Type-safe receipt status enforcing valid state invariants.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReceiptState {
    Pending {
        target_id: Option<String>,
        staging_path: String,
        error: Option<String>,
    },
    Ready {
        target_id: String,
        staging_path: String,
    },
    NoOp {
        target_id: String,
        staging_path: String,
    },
}

impl ReceiptState {
    #[must_use]
    pub const fn status_str(&self) -> &'static str {
        match self {
            Self::Pending { .. } => "pending",
            Self::Ready { .. } => "ready",
            Self::NoOp { .. } => "no_op",
        }
    }

    #[must_use]
    pub fn target_id(&self) -> Option<&str> {
        match self {
            Self::Pending { target_id, .. } => target_id.as_deref(),
            Self::Ready { target_id, .. } | Self::NoOp { target_id, .. } => Some(target_id),
        }
    }

    #[must_use]
    pub fn staging_path(&self) -> &str {
        match self {
            Self::Pending { staging_path, .. }
            | Self::Ready { staging_path, .. }
            | Self::NoOp { staging_path, .. } => staging_path,
        }
    }

    #[must_use]
    pub fn error(&self) -> Option<&str> {
        match self {
            Self::Pending { error, .. } => error.as_deref(),
            Self::Ready { .. } | Self::NoOp { .. } => None,
        }
    }
}

/// Domain sync receipt persisted under `handoffs/*.json` and emitted by `codex-agy-sync sync`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "RawSyncReceipt", into = "RawSyncReceipt")]
pub struct SyncReceipt {
    pub workspace: String,
    pub source_host: String,
    pub source_id: String,
    pub source_digest: String,
    pub target_host: String,
    pub title: String,
    pub state: ReceiptState,
    pub runtime_state_not_transferred: bool,
    pub created_at_iso: String,
    pub chain_id: Option<String>,
    pub chained_from_source_id: Option<String>,
    pub chain_depth: i64,
    pub target_seed_turn_count: Option<i64>,
    pub target_seed_text_digest: Option<String>,
    pub pairing_mode: Option<String>,
}

impl SyncReceipt {
    #[must_use]
    pub fn status_str(&self) -> &'static str {
        self.state.status_str()
    }

    #[must_use]
    pub fn target_id(&self) -> Option<&str> {
        self.state.target_id()
    }

    #[must_use]
    pub fn staging_path(&self) -> &str {
        self.state.staging_path()
    }

    #[must_use]
    pub fn error(&self) -> Option<&str> {
        self.state.error()
    }

    #[must_use]
    pub fn to_raw(&self) -> RawSyncReceipt {
        RawSyncReceipt {
            workspace: self.workspace.clone(),
            source_host: self.source_host.clone(),
            source_id: self.source_id.clone(),
            source_digest: self.source_digest.clone(),
            target_host: self.target_host.clone(),
            target_id: self.target_id().map(str::to_owned),
            title: self.title.clone(),
            status: self.status_str().to_owned(),
            staging_path: self.staging_path().to_owned(),
            runtime_state_not_transferred: self.runtime_state_not_transferred,
            error: self.error().map(str::to_owned),
            created_at_iso: self.created_at_iso.clone(),
            chain_id: self.chain_id.clone(),
            chained_from_source_id: self.chained_from_source_id.clone(),
            chain_depth: self.chain_depth,
            target_seed_turn_count: self.target_seed_turn_count,
            target_seed_text_digest: self.target_seed_text_digest.clone(),
            pairing_mode: self.pairing_mode.clone(),
        }
    }
}

impl TryFrom<RawSyncReceipt> for SyncReceipt {
    type Error = CoreError;

    fn try_from(raw: RawSyncReceipt) -> Result<Self, Self::Error> {
        let state = match raw.status.as_str() {
            "pending" => ReceiptState::Pending {
                target_id: raw.target_id,
                staging_path: raw.staging_path,
                error: raw.error,
            },
            "ready" => {
                let tid = raw.target_id.ok_or_else(|| {
                    CoreError::InvalidReceipt("ready receipt requires target_id".to_owned())
                })?;
                ReceiptState::Ready {
                    target_id: tid,
                    staging_path: raw.staging_path,
                }
            }
            "no_op" => {
                let tid = raw.target_id.ok_or_else(|| {
                    CoreError::InvalidReceipt("no_op receipt requires target_id".to_owned())
                })?;
                ReceiptState::NoOp {
                    target_id: tid,
                    staging_path: raw.staging_path,
                }
            }
            other => {
                return Err(CoreError::InvalidReceipt(format!(
                    "unsupported receipt status {other:?}"
                )));
            }
        };

        Ok(Self {
            workspace: raw.workspace,
            source_host: raw.source_host,
            source_id: raw.source_id,
            source_digest: raw.source_digest,
            target_host: raw.target_host,
            title: raw.title,
            state,
            runtime_state_not_transferred: raw.runtime_state_not_transferred,
            created_at_iso: raw.created_at_iso,
            chain_id: raw.chain_id,
            chained_from_source_id: raw.chained_from_source_id,
            chain_depth: raw.chain_depth,
            target_seed_turn_count: raw.target_seed_turn_count,
            target_seed_text_digest: raw.target_seed_text_digest,
            pairing_mode: raw.pairing_mode,
        })
    }
}

impl From<SyncReceipt> for RawSyncReceipt {
    fn from(value: SyncReceipt) -> Self {
        value.to_raw()
    }
}

/// Compute `(completed_turn_count, sha256_digest)` for completed user/assistant texts without extra RPCs.
#[must_use]
pub fn ledger_seed_fingerprint(ledger: &TrajectoryLedger) -> (i64, String) {
    let mut pairs: Vec<[&str; 2]> = Vec::new();
    let completed = ledger.slice_after_watermark(None).unwrap_or(&[]);
    for event in completed {
        match event {
            CanonicalEvent::UserInput { query_text, .. } => {
                if !is_host_injected_user_event(event) {
                    pairs.push(["user_input", query_text.trim()]);
                }
            }
            CanonicalEvent::AssistantText { text, .. } => {
                pairs.push(["assistant_text", text.trim()]);
            }
            _ => {}
        }
    }
    let json_str = serde_json::to_string(&pairs).unwrap_or_else(|_| "[]".to_owned());
    let mut hasher = Sha256::new();
    hasher.update(json_str.as_bytes());
    let digest = hex::encode(hasher.finalize());
    (ledger.completed_turn_watermarks.len() as i64, digest)
}

/// Loaded receipt entry `(file_path, receipt, mtime_sec_f64)`.
pub type ReceiptEntry = (PathBuf, SyncReceipt, f64);

/// Pending receipt hit returned when chain traversal encounters a blocking pending hop.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingReceiptErrorData {
    pub receipt: SyncReceipt,
    pub receipt_path: PathBuf,
}

type PendingLookup<'a> = HashMap<(String, String), &'a ReceiptEntry>;
type DagTerminal<'a> = (&'a SyncReceipt, f64, usize);
type DagWalkResult<'a> = (
    Vec<DagTerminal<'a>>,
    Vec<(String, String)>,
    HashSet<PathBuf>,
);

fn pending_maps(receipts: &[ReceiptEntry]) -> (PendingLookup<'_>, PendingLookup<'_>) {
    let mut by_src: PendingLookup<'_> = HashMap::new();
    let mut by_tgt: PendingLookup<'_> = HashMap::new();
    for entry in receipts {
        let (_, rec, mtime) = entry;
        if rec.status_str() != "pending" {
            continue;
        }
        let sk = (rec.source_host.clone(), rec.source_id.clone());
        let replace_src = by_src.get(&sk).is_none_or(|cur| {
            (rec.created_at_iso.as_str(), *mtime) > (cur.1.created_at_iso.as_str(), cur.2)
        });
        if replace_src {
            by_src.insert(sk, entry);
        }
        if let Some(tid) = rec.target_id() {
            let tk = (rec.target_host.clone(), tid.to_owned());
            let replace_tgt = by_tgt.get(&tk).is_none_or(|cur| {
                (rec.created_at_iso.as_str(), *mtime) > (cur.1.created_at_iso.as_str(), cur.2)
            });
            if replace_tgt {
                by_tgt.insert(tk, entry);
            }
        }
    }
    (by_src, by_tgt)
}

fn raise_if_nodes_pending(
    nodes: &[(String, String)],
    by_src: &PendingLookup<'_>,
    by_tgt: &PendingLookup<'_>,
) -> Result<(), Box<PendingReceiptErrorData>> {
    for key in nodes {
        if let Some(hit) = by_src.get(key).or_else(|| by_tgt.get(key)) {
            return Err(Box::new(PendingReceiptErrorData {
                receipt: hit.1.clone(),
                receipt_path: hit.0.clone(),
            }));
        }
    }
    Ok(())
}

fn walk_ready_dag<'a>(
    ready_items: &[&'a ReceiptEntry],
    start_items: &[&'a ReceiptEntry],
) -> DagWalkResult<'a> {
    type QueueItem<'q> = (&'q ReceiptEntry, usize, HashSet<(String, String)>);
    let mut queue: VecDeque<QueueItem<'a>> = start_items
        .iter()
        .map(|it| {
            let mut set = HashSet::new();
            set.insert((it.1.source_host.clone(), it.1.source_id.clone()));
            (*it, 1, set)
        })
        .collect();
    let mut enqueued: HashSet<PathBuf> = start_items.iter().map(|it| it.0.clone()).collect();
    let mut terminals: Vec<DagTerminal<'a>> = Vec::new();
    let mut visited: HashSet<PathBuf> = HashSet::new();
    let mut nodes: Vec<(String, String)> = Vec::new();

    while let Some(((p, cur, mtime), hops, path_nodes)) = queue.pop_front() {
        visited.insert(p.clone());
        nodes.push((cur.source_host.clone(), cur.source_id.clone()));
        if let Some(tid) = cur.target_id() {
            let tgt_key = (cur.target_host.clone(), tid.to_owned());
            nodes.push(tgt_key.clone());
            let all_nxt: Vec<&'a ReceiptEntry> = ready_items
                .iter()
                .copied()
                .filter(|it| {
                    it.1.source_host == cur.target_host && it.1.source_id == tid && &it.0 != p
                })
                .collect();
            if !all_nxt.is_empty() && !path_nodes.contains(&tgt_key) {
                for it in all_nxt {
                    if !enqueued.contains(&it.0) {
                        enqueued.insert(it.0.clone());
                        let mut next_path = path_nodes.clone();
                        next_path.insert(tgt_key.clone());
                        queue.push_back((it, hops + 1, next_path));
                    }
                }
                continue;
            }
        }
        terminals.push((cur, *mtime, hops));
    }

    (terminals, nodes, visited)
}

fn cmp_f64_total(a: f64, b: f64) -> std::cmp::Ordering {
    a.total_cmp(&b)
}

/// Resolve the active tip receipt across workspace sync chains using recency-first DAG traversal.
///
/// # Errors
/// Returns [`PendingReceiptErrorData`] if any node along the resolved chain or requested `source_id`
/// has a `pending` receipt.
pub fn resolve_chain_tip(
    receipts: &[ReceiptEntry],
    source_id: Option<&str>,
) -> Result<Option<SyncReceipt>, Box<PendingReceiptErrorData>> {
    let (by_src, by_tgt) = pending_maps(receipts);
    let ready_items: Vec<&ReceiptEntry> = receipts
        .iter()
        .filter(|it| it.1.status_str() == "ready")
        .collect();

    if let Some(sid) = source_id {
        if let Some(best) = receipts
            .iter()
            .filter(|it| {
                it.1.status_str() == "pending"
                    && (it.1.source_id == sid || it.1.target_id() == Some(sid))
            })
            .max_by(|a, b| {
                a.1.created_at_iso
                    .cmp(&b.1.created_at_iso)
                    .then_with(|| cmp_f64_total(a.2, b.2))
            })
        {
            return Err(Box::new(PendingReceiptErrorData {
                receipt: best.1.clone(),
                receipt_path: best.0.clone(),
            }));
        }
        if ready_items.is_empty() {
            return Ok(None);
        }
        let starts: Vec<&ReceiptEntry> = ready_items
            .iter()
            .copied()
            .filter(|it| it.1.source_id == sid)
            .collect();
        if !starts.is_empty() {
            let (terminals, nodes, _) = walk_ready_dag(&ready_items, &starts);
            if let Some((win_rec, _, _)) = terminals.into_iter().max_by(|a, b| {
                a.0.chain_depth
                    .cmp(&b.0.chain_depth)
                    .then_with(|| a.2.cmp(&b.2))
                    .then_with(|| a.0.created_at_iso.cmp(&b.0.created_at_iso))
                    .then_with(|| cmp_f64_total(a.1, b.1))
            }) {
                raise_if_nodes_pending(&nodes, &by_src, &by_tgt)?;
                return Ok(Some(win_rec.clone()));
            }
            return Ok(None);
        }
        let Some(win_entry) = ready_items
            .iter()
            .copied()
            .filter(|it| it.1.target_id() == Some(sid))
            .max_by(|a, b| {
                a.1.chain_depth
                    .cmp(&b.1.chain_depth)
                    .then_with(|| a.1.created_at_iso.cmp(&b.1.created_at_iso))
                    .then_with(|| cmp_f64_total(a.2, b.2))
            })
        else {
            return Ok(None);
        };
        let win_rec = &win_entry.1;
        let check_nodes = vec![
            (win_rec.source_host.clone(), win_rec.source_id.clone()),
            (
                win_rec.target_host.clone(),
                win_rec.target_id().unwrap_or("").to_owned(),
            ),
        ];
        raise_if_nodes_pending(&check_nodes, &by_src, &by_tgt)?;
        return Ok(Some(win_rec.clone()));
    }

    if ready_items.is_empty() {
        return Ok(None);
    }
    let target_keys: HashSet<(String, String)> = ready_items
        .iter()
        .filter_map(|it| {
            it.1.target_id()
                .map(|tid| (it.1.target_host.clone(), tid.to_owned()))
        })
        .collect();
    let roots: Vec<&ReceiptEntry> = ready_items
        .iter()
        .copied()
        .filter(|it| !target_keys.contains(&(it.1.source_host.clone(), it.1.source_id.clone())))
        .collect();

    let mut seen_paths: HashSet<PathBuf> = HashSet::new();
    let mut components: Vec<(DagTerminal<'_>, Vec<(String, String)>)> = Vec::new();
    for &seed in roots.iter().chain(ready_items.iter()) {
        let is_root = roots.iter().any(|r| r.0 == seed.0);
        if is_root || !seen_paths.contains(&seed.0) {
            let (terminals, nodes, visited) = walk_ready_dag(&ready_items, &[seed]);
            seen_paths.extend(visited);
            if let Some(comp_tip) = terminals.into_iter().max_by(|a, b| {
                a.0.created_at_iso
                    .cmp(&b.0.created_at_iso)
                    .then_with(|| cmp_f64_total(a.1, b.1))
                    .then_with(|| a.0.chain_depth.cmp(&b.0.chain_depth))
                    .then_with(|| a.2.cmp(&b.2))
            }) {
                components.push((comp_tip, nodes));
            }
        }
    }

    let Some((win_tip, win_nodes)) = components.into_iter().max_by(|a, b| {
        a.0.0
            .created_at_iso
            .cmp(&b.0.0.created_at_iso)
            .then_with(|| cmp_f64_total(a.0.1, b.0.1))
            .then_with(|| a.0.0.chain_depth.cmp(&b.0.0.chain_depth))
            .then_with(|| a.0.2.cmp(&b.0.2))
    }) else {
        return Ok(None);
    };
    raise_if_nodes_pending(&win_nodes, &by_src, &by_tgt)?;
    Ok(Some(win_tip.0.clone()))
}
