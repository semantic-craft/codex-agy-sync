//! Structured domain/host error types matching `codex-agy-sync` CLI exit-code-2 JSON diagnostics.

use std::path::PathBuf;

use cas_core::{CoreError, SyncReceipt};
use serde_json::{Map, Value, json};
use thiserror::Error;

/// Host/bridge error with deterministic CLI exit-code-2 JSON serialization.
#[derive(Debug, Error)]
pub enum HostError {
    #[error("{message}")]
    SyncBusyLock {
        message: String,
        host: Option<String>,
        remediation: Vec<String>,
    },

    #[error("{0}")]
    FileNotFound(String),

    #[error("{message}")]
    UnpairedWorkspace {
        message: String,
        diagnosis: Value,
        remediation: Vec<String>,
    },

    #[error(
        "Sync remains pending; target={}. Run `codex-agy-sync sync --reconcile` or inspect {} before retrying; no new session was allocated on retry. Cause: {}",
        receipt.state.target_id().unwrap_or("unknown"),
        receipt_path.display(),
        receipt.state.error().unwrap_or("Previous operation did not finish.")
    )]
    HandoffPending {
        receipt: Box<SyncReceipt>,
        receipt_path: PathBuf,
    },

    #[error("{0}")]
    HandoffCancelled(String),

    #[error("{0}")]
    EmptySession(String),

    #[error("{0}")]
    OnlineRpcUnavailable(String),

    #[error("Codex import requires reconciliation before retry: {message}")]
    NativeCodexImportPending {
        message: String,
        thread_id: Option<String>,
    },

    #[error("{0}")]
    InvalidArgument(String),

    #[error("{0}")]
    Runtime(String),

    #[error(transparent)]
    Core(#[from] CoreError),

    #[error(transparent)]
    Codec(#[from] cas_codecs::CodecError),

    #[error("SQLite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

impl HostError {
    /// Convert into the exact CLI stderr JSON error payload expected by Swift and differential parity tests.
    #[must_use]
    pub fn to_error_payload(&self, subcommand: &str) -> Value {
        match self {
            Self::SyncBusyLock {
                message,
                host,
                remediation,
            } => {
                let mut m = Map::new();
                m.insert("error".into(), json!("sync_busy_lock"));
                m.insert("message".into(), json!(message));
                if let Some(h) = host {
                    m.insert("host".into(), json!(h));
                }
                if !remediation.is_empty() {
                    m.insert("remediation".into(), json!(remediation));
                }
                Value::Object(m)
            }
            Self::FileNotFound(msg) => json!({"error": "file_not_found", "message": msg}),
            Self::UnpairedWorkspace {
                message,
                diagnosis,
                remediation,
            } => json!({
                "error": "unpaired_workspace",
                "message": message,
                "diagnosis": diagnosis,
                "remediation": remediation,
            }),
            Self::HandoffPending { receipt, .. } => {
                let receipt_val =
                    serde_json::to_value(receipt.as_ref()).unwrap_or_else(|_| json!({}));
                let mut m = receipt_val.as_object().cloned().unwrap_or_default();
                m.insert("error".into(), json!("handoff_pending"));
                m.insert("message".into(), json!(self.to_string()));
                m.insert("cause".into(), json!(receipt.state.error()));
                m.insert("receipt".into(), receipt_val);
                Value::Object(m)
            }
            Self::HandoffCancelled(msg) => json!({"error": "handoff_cancelled", "message": msg}),
            Self::EmptySession(msg) | Self::Core(CoreError::EmptySession(msg)) => {
                json!({"error": "empty_session", "message": msg})
            }
            Self::OnlineRpcUnavailable(msg)
            | Self::NativeCodexImportPending { message: msg, .. } => {
                json!({"error": "online_rpc_unavailable", "message": msg})
            }
            Self::InvalidArgument(msg) => {
                let code = if subcommand == "sync" {
                    "invalid_handoff_request"
                } else {
                    "invalid_argument"
                };
                json!({"error": code, "message": msg})
            }
            Self::Core(
                CoreError::InvalidId { .. }
                | CoreError::InvalidWorkspace(_)
                | CoreError::InvalidSummary(_)
                | CoreError::InvalidReceipt(_)
                | CoreError::InvalidData(_),
            )
            | Self::Json(_) => {
                let code = if subcommand == "sync" {
                    "invalid_handoff_request"
                } else {
                    "invalid_argument"
                };
                json!({"error": code, "message": self.to_string()})
            }
            Self::Io(io_err) if io_err.kind() == std::io::ErrorKind::NotFound => {
                json!({"error": "file_not_found", "message": io_err.to_string()})
            }
            other => json!({"error": "runtime_error", "message": other.to_string()}),
        }
    }
}
