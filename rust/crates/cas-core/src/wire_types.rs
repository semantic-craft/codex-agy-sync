//! Protobuf wire metadata types and ISO-8601 timestamp helpers shared across crates.

use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};

/// Opaque protobuf wire field preserved for forward-compatible round-tripping.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownWireField {
    pub field_tag: u32,
    pub wire_type: u8,
    pub raw_bytes: Vec<u8>,
}

const DEFAULT_FALLBACK_SECONDS: i64 = 1_700_000_000;

/// Convert an ISO-8601 timestamp string into `(unix_seconds, nanos)`.
#[must_use]
pub fn parse_iso_to_seconds_nanos(iso_str: &str) -> (i64, i32) {
    let cleaned = iso_str.trim();
    if cleaned.is_empty() {
        return (DEFAULT_FALLBACK_SECONDS, 0);
    }
    let normalized = if let Some(prefix) = cleaned.strip_suffix('Z') {
        format!("{prefix}+00:00")
    } else {
        cleaned.to_owned()
    };

    if let Ok(dt) = DateTime::parse_from_rfc3339(&normalized) {
        let utc = dt.with_timezone(&Utc);
        return (utc.timestamp(), utc.timestamp_subsec_nanos() as i32);
    }

    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%:z",
        "%Y-%m-%d %H:%M:%S%:z",
    ] {
        if let Ok(dt) = DateTime::parse_from_str(&normalized, fmt) {
            let utc = dt.with_timezone(&Utc);
            return (utc.timestamp(), utc.timestamp_subsec_nanos() as i32);
        }
    }

    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ] {
        if let Ok(ndt) = NaiveDateTime::parse_from_str(&normalized, fmt) {
            let utc = Utc.from_utc_datetime(&ndt);
            return (utc.timestamp(), utc.timestamp_subsec_nanos() as i32);
        }
    }

    (DEFAULT_FALLBACK_SECONDS, 0)
}

/// Format `(seconds, nanos)` into an ISO-8601 UTC timestamp string matching Python `_wire.py`.
#[must_use]
pub fn format_timestamp_seconds_nanos(seconds: i64, nanos: i32, default_iso: &str) -> String {
    if seconds <= 0 {
        return default_iso.to_owned();
    }
    let Some(dt) = DateTime::<Utc>::from_timestamp(seconds, 0) else {
        return default_iso.to_owned();
    };
    let base = dt.format("%Y-%m-%dT%H:%M:%S").to_string();
    if nanos <= 0 {
        return format!("{base}Z");
    }
    if nanos % 1000 != 0 {
        let padded = format!("{nanos:09}");
        let trimmed = padded.trim_end_matches('0');
        format!("{base}.{trimmed}Z")
    } else {
        format!("{base}.{:06}Z", nanos / 1000)
    }
}
