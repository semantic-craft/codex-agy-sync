//! Typed error definitions for `cas-core`.

use thiserror::Error;

/// Domain errors produced by `cas-core` validation, ledger, and state machine logic.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CoreError {
    /// Invalid session, thread, cascade, or project identifier.
    #[error("invalid {label}: {value:?}")]
    InvalidId { label: &'static str, value: String },

    /// Invalid workspace path or URI.
    #[error("{0}")]
    InvalidWorkspace(String),

    /// Empty or whitespace-only CSRF token.
    #[error("CSRF token cannot be empty.")]
    EmptyCsrfToken,

    /// Invalid Antigravity summary invariant.
    #[error("{0}")]
    InvalidSummary(String),

    /// Invalid sync receipt payload or state transition.
    #[error("{0}")]
    InvalidReceipt(String),

    /// Session has zero completed turns and no active in-flight turn.
    #[error("{0}")]
    EmptySession(String),

    /// Missing expected watermark or ledger lookup key.
    #[error("{0}")]
    LedgerLookup(String),

    /// Malformed or invalid tool/event wire data.
    #[error("{0}")]
    InvalidData(String),
}
