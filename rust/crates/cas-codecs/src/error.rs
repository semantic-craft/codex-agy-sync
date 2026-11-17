//! Error types for `cas-codecs`.

use thiserror::Error;

/// Errors raised while decoding or encoding protobuf wire, rollout, or projection streams.
#[derive(Debug, Error)]
pub enum CodecError {
    #[error("{0}")]
    Wire(String),

    #[error("Invalid or truncated zstd rollout stream: {0}")]
    ZstdStream(String),

    #[error("Invalid JSON in rollout stream: {0}")]
    Json(#[from] serde_json::Error),

    #[error("I/O error reading codec stream: {0}")]
    Io(#[from] std::io::Error),

    #[error(transparent)]
    Core(#[from] cas_core::CoreError),
}
