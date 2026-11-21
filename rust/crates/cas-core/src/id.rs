//! Validated domain IDs, workspace identity canonicalization, host kinds, and CSRF secret redaction.

use std::borrow::Cow;
use std::env;
use std::fmt;
use std::path::{Component, Path, PathBuf};
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use url::Url;
use zeroize::{Zeroize, ZeroizeOnDrop};

use crate::error::CoreError;

/// Package version pinned to `pyproject.toml`.
pub const PACKAGE_VERSION: &str = "0.4.3";

/// Antigravity project IDs reserved for ungrouped/CLI buckets and excluded from Desktop sidebar sync.
pub const FORBIDDEN_AGY_PROJECT_IDS: &[&str] = &[
    "",
    "default-cli-project",
    "outside-of-project",
    "null",
    "none",
];

/// Validate an untrusted session, thread, or project identifier at the boundary.
///
/// # Errors
/// Returns [`CoreError::InvalidId`] if empty, untrimmed, longer than 128 bytes,
/// contains path traversal/separators (`..`, `/`, `\`), or control characters.
pub fn validate_session_id<'a>(raw_id: &'a str, label: &'static str) -> Result<&'a str, CoreError> {
    if raw_id.is_empty()
        || raw_id != raw_id.trim()
        || raw_id.len() > 128
        || raw_id.contains("..")
        || raw_id.contains('/')
        || raw_id.contains('\\')
        || raw_id
            .chars()
            .any(|ch| (ch as u32) < 32 || (ch as u32) == 127)
    {
        return Err(CoreError::InvalidId {
            label,
            value: raw_id.to_owned(),
        });
    }
    Ok(raw_id)
}

macro_rules! validated_id_newtype {
    ($name:ident, $label:literal, $doc:literal) => {
        #[doc = $doc]
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        pub struct $name(String);

        impl $name {
            /// Parse and validate at the boundary.
            ///
            /// # Errors
            /// Returns [`CoreError::InvalidId`] if the identifier violates boundary rules.
            pub fn parse(raw: &str) -> Result<Self, CoreError> {
                validate_session_id(raw, $label)?;
                Ok(Self(raw.to_owned()))
            }

            /// Borrow the validated identifier string.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl TryFrom<&str> for $name {
            type Error = CoreError;
            fn try_from(value: &str) -> Result<Self, Self::Error> {
                Self::parse(value)
            }
        }

        impl TryFrom<String> for $name {
            type Error = CoreError;
            fn try_from(value: String) -> Result<Self, Self::Error> {
                validate_session_id(&value, $label)?;
                Ok(Self(value))
            }
        }

        impl FromStr for $name {
            type Err = CoreError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                Self::parse(s)
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> Self {
                value.0
            }
        }

        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

validated_id_newtype!(
    SessionId,
    "session_id",
    "Generic validated session identifier."
);
validated_id_newtype!(
    CascadeId,
    "cascade_id",
    "Validated Antigravity cascade UUID."
);
validated_id_newtype!(
    CodexThreadId,
    "codex_thread_id",
    "Validated Codex thread UUID."
);
validated_id_newtype!(
    AgyProjectId,
    "agy_project_id",
    "Validated Antigravity project UUID."
);

impl AgyProjectId {
    /// Construct from wire bytes where an ungrouped trajectory may carry an empty project ID.
    #[must_use]
    pub fn from_wire_lossy(raw: &str) -> Self {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            Self(String::new())
        } else {
            Self::parse(trimmed).unwrap_or_else(|_| Self(String::new()))
        }
    }
}
validated_id_newtype!(
    CodexProjectId,
    "codex_project_id",
    "Validated Codex project UUID."
);
validated_id_newtype!(
    CodexLegacyProjectId,
    "codex_legacy_project_id",
    "Validated legacy Codex local-project UUID."
);
validated_id_newtype!(
    TrajectoryId,
    "trajectory_id",
    "Validated Antigravity trajectory UUID."
);

macro_rules! nonempty_id_newtype {
    ($name:ident, $label:literal, $doc:literal) => {
        #[doc = $doc]
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        pub struct $name(String);

        impl $name {
            /// Construct a non-empty identifier.
            ///
            /// # Errors
            /// Returns [`CoreError::InvalidId`] if empty or control-character tainted.
            pub fn parse(raw: &str) -> Result<Self, CoreError> {
                if raw.is_empty() || raw.chars().any(|ch| (ch as u32) < 32 || (ch as u32) == 127) {
                    return Err(CoreError::InvalidId {
                        label: $label,
                        value: raw.to_owned(),
                    });
                }
                Ok(Self(raw.to_owned()))
            }

            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl TryFrom<&str> for $name {
            type Error = CoreError;
            fn try_from(value: &str) -> Result<Self, Self::Error> {
                Self::parse(value)
            }
        }

        impl TryFrom<String> for $name {
            type Error = CoreError;
            fn try_from(value: String) -> Result<Self, Self::Error> {
                Self::parse(&value)
            }
        }

        impl FromStr for $name {
            type Err = CoreError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                Self::parse(s)
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> Self {
                value.0
            }
        }

        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

nonempty_id_newtype!(ToolCallId, "tool_call_id", "Tool call identifier.");
nonempty_id_newtype!(
    TurnId,
    "turn_id",
    "Canonical sequential turn identifier (e.g. turn-1)."
);
nonempty_id_newtype!(
    ContentDigest,
    "content_digest",
    "SHA-256 hex digest at a turn watermark."
);
nonempty_id_newtype!(
    WorkspaceRootUri,
    "workspace_root_uri",
    "Canonical file:// workspace root URI."
);

/// Zero-indexed Antigravity step index.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StepIndex(pub u32);

impl fmt::Display for StepIndex {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// One-indexed Codex rollout JSONL line number (`0` permitted only as synthetic pre-cursor sentinel).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct RolloutLineNo(pub u64);

impl fmt::Display for RolloutLineNo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Zero-indexed canonical ledger event sequence number.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct LedgerSeqNo(pub usize);

impl fmt::Display for LedgerSeqNo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Canonicalized workspace filesystem path.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct WorkspacePath(PathBuf);

impl WorkspacePath {
    #[must_use]
    pub fn as_path(&self) -> &Path {
        &self.0
    }
}

impl AsRef<Path> for WorkspacePath {
    fn as_ref(&self) -> &Path {
        &self.0
    }
}

impl fmt::Display for WorkspacePath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0.display())
    }
}

/// Host identifier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HostKind {
    Antigravity,
    Codex,
}

impl HostKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Antigravity => "antigravity",
            Self::Codex => "codex",
        }
    }
}

impl fmt::Display for HostKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Antigravity trajectory sources passing the Desktop sidebar filter (`17` CLI excluded).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(u32)]
pub enum AgyGuiSafeSource {
    Unspecified = 0,
    CascadeClient = 1,
    Jetbox = 18,
    AgentApi = 19,
    Cider = 20,
}

impl AgyGuiSafeSource {
    #[must_use]
    pub const fn proto_enum_value(self) -> u32 {
        self as u32
    }

    #[must_use]
    pub const fn summary_column_name(self) -> &'static str {
        match self {
            Self::Unspecified => "",
            Self::CascadeClient => "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
            Self::Jetbox => "CORTEX_TRAJECTORY_SOURCE_JETBOX",
            Self::AgentApi => "CORTEX_TRAJECTORY_SOURCE_AGENT_API",
            Self::Cider => "CORTEX_TRAJECTORY_SOURCE_CIDER",
        }
    }

    #[must_use]
    pub const fn from_proto_enum(val: u32) -> Option<Self> {
        match val {
            0 => Some(Self::Unspecified),
            1 => Some(Self::CascadeClient),
            18 => Some(Self::Jetbox),
            19 => Some(Self::AgentApi),
            20 => Some(Self::Cider),
            _ => None,
        }
    }
}

/// In-memory-only wrapper for `x-codeium-csrf-token` from `hub_<port>.json`.
///
/// Never implements `Clone`, redacts `Debug` and `Display`, and zeroizes on drop.
#[derive(Zeroize, ZeroizeOnDrop)]
pub struct CsrfSecret {
    token: String,
}

impl CsrfSecret {
    /// Construct a `CsrfSecret` after rejecting empty or whitespace-only tokens.
    ///
    /// # Errors
    /// Returns [`CoreError::EmptyCsrfToken`] when `token.trim()` is empty.
    pub fn try_new(token: &str) -> Result<Self, CoreError> {
        let cleaned = token.trim();
        if cleaned.is_empty() {
            return Err(CoreError::EmptyCsrfToken);
        }
        Ok(Self {
            token: cleaned.to_owned(),
        })
    }

    /// Return the `(header_name, header_value)` pair strictly for loopback requests.
    #[must_use]
    pub fn as_loopback_header(&self) -> (&'static str, &str) {
        ("x-codeium-csrf-token", &self.token)
    }
}

impl fmt::Debug for CsrfSecret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("CsrfSecret('[REDACTED_CSRF]')")
    }
}

impl fmt::Display for CsrfSecret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("[REDACTED_CSRF]")
    }
}

/// Normalize a path lexically (cleaning `.` and `..`) without touching the filesystem.
#[must_use]
pub fn lexical_abs_path(path: &Path) -> PathBuf {
    let joined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut out = PathBuf::new();
    for comp in joined.components() {
        match comp {
            Component::Prefix(p) => out.push(p.as_os_str()),
            Component::RootDir => out.push(comp.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(s) => out.push(s),
        }
    }
    out
}

/// Resolve a path like Python `Path.resolve(strict=False)` by canonicalizing the deepest existing ancestor.
#[must_use]
pub fn resolve_strict_false(path: &Path) -> PathBuf {
    let abs = lexical_abs_path(path);
    if let Ok(canon) = std::fs::canonicalize(&abs) {
        return canon;
    }
    let mut existing = abs.as_path();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    while !existing.exists() {
        if let Some(name) = existing.file_name() {
            tail.push(name.to_os_string());
        } else {
            break;
        }
        if let Some(parent) = existing.parent() {
            existing = parent;
        } else {
            break;
        }
    }
    let mut resolved = std::fs::canonicalize(existing).unwrap_or_else(|_| existing.to_path_buf());
    for seg in tail.into_iter().rev() {
        resolved.push(seg);
    }
    resolved
}

fn expand_tilde_and_vars(raw: &str) -> String {
    let mut out = raw.to_owned();
    if out == "~" || out.starts_with("~/") {
        if let Some(home) = env::var_os("HOME").filter(|v| !v.is_empty()) {
            let home_str = PathBuf::from(home).to_string_lossy().into_owned();
            if out == "~" {
                out = home_str;
            } else {
                out = format!("{home_str}{}", &out[1..]);
            }
        }
    }
    out
}

/// Parse and canonicalize an untrusted path or `file://` URI at the boundary.
///
/// # Errors
/// Returns [`CoreError::InvalidWorkspace`] on empty input, non-local URI authority,
/// unexpanded `~`/`$` tokens, or paths resolving to `/`.
pub fn parse_workspace_identity(
    raw_path_or_uri: &str,
) -> Result<(WorkspacePath, WorkspaceRootUri), CoreError> {
    let raw_text = raw_path_or_uri.trim();
    if raw_text.is_empty() {
        return Err(CoreError::InvalidWorkspace(
            "Workspace path cannot be empty.".to_owned(),
        ));
    }
    let candidate = if raw_text.starts_with("file://") {
        let parsed = Url::parse(raw_text).map_err(|e| {
            CoreError::InvalidWorkspace(format!("Invalid file URI {raw_path_or_uri:?}: {e}"))
        })?;
        if parsed.scheme() != "file" {
            return Err(CoreError::InvalidWorkspace(format!(
                "Invalid file URI {raw_path_or_uri:?}."
            )));
        }
        if let Some(host) = parsed.host_str() {
            if !host.is_empty() && !host.eq_ignore_ascii_case("localhost") {
                return Err(CoreError::InvalidWorkspace(format!(
                    "Unsupported non-local file URI authority: {host:?}"
                )));
            }
        }
        let decoded = percent_decode(parsed.path());
        if !decoded.starts_with('/') {
            return Err(CoreError::InvalidWorkspace(format!(
                "Invalid file URI {raw_path_or_uri:?}."
            )));
        }
        PathBuf::from(decoded)
    } else {
        if raw_text.contains("://") {
            return Err(CoreError::InvalidWorkspace(format!(
                "Unsupported URI scheme in {raw_path_or_uri:?}."
            )));
        }
        let expanded = expand_tilde_and_vars(raw_text);
        if expanded.starts_with('~')
            || expanded.contains('$')
            || raw_text == "."
            || raw_text == ".."
        {
            return Err(CoreError::InvalidWorkspace(format!(
                "Invalid or unexpanded workspace specifier {raw_path_or_uri:?}."
            )));
        }
        PathBuf::from(expanded)
    };

    let resolved = resolve_strict_false(&candidate);
    let comp_count = resolved.components().count();
    if resolved == Path::new("/") || comp_count <= 1 {
        return Err(CoreError::InvalidWorkspace(format!(
            "Workspace path cannot resolve to filesystem root {raw_path_or_uri:?}."
        )));
    }
    let uri_str = Url::from_file_path(&resolved)
        .map_err(|()| {
            CoreError::InvalidWorkspace(format!(
                "Cannot convert path to file URI: {}",
                resolved.display()
            ))
        })?
        .to_string()
        .trim_end_matches('/')
        .to_owned();
    Ok((WorkspacePath(resolved), WorkspaceRootUri::parse(&uri_str)?))
}

/// Normalize a CLI or API workspace specifier while preserving `file://` URIs and lexical symlink paths.
#[must_use]
pub fn normalize_workspace_spec(raw_spec: &str) -> String {
    let text = raw_spec.trim();
    if text.is_empty() {
        return String::new();
    }
    if text.starts_with("file://") || text.contains("://") {
        return text.to_owned();
    }
    let expanded = expand_tilde_and_vars(text);
    if expanded.starts_with('~') || expanded.contains('$') {
        return expanded;
    }
    lexical_abs_path(Path::new(&expanded))
        .to_string_lossy()
        .into_owned()
}

/// Convert a normalized workspace specifier into a candidate `PathBuf` if local.
#[must_use]
pub fn raw_spec_to_candidate_path(raw: &str) -> Option<PathBuf> {
    let text = normalize_workspace_spec(raw);
    if text.is_empty() {
        return None;
    }
    if text.starts_with("file://") {
        let parsed = Url::parse(&text).ok()?;
        if let Some(host) = parsed.host_str() {
            if !host.is_empty() && !host.eq_ignore_ascii_case("localhost") {
                return None;
            }
        }
        let decoded = percent_decode(parsed.path());
        return decoded.starts_with('/').then(|| PathBuf::from(decoded));
    }
    if text.contains("://") {
        return None;
    }
    Some(PathBuf::from(text))
}

/// Percent-decode a UTF-8 path string (invalid sequences replaced lossily).
#[must_use]
pub fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                out.push((hi << 4) | lo);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

const fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn strip_trailing_line_annotation(s: &str) -> &str {
    let trimmed = s.trim_end();
    if !trimmed.ends_with(')') {
        return s;
    }
    let Some(open_idx) = trimmed.rfind('(') else {
        return s;
    };
    let before = &trimmed[..open_idx];
    if before.is_empty() || !before.ends_with(char::is_whitespace) {
        return s;
    }
    let inner = &trimmed[open_idx + 1..trimmed.len() - 1];
    let lower = inner.to_ascii_lowercase();
    let matches_kw = if lower == "line" || lower == "lines" || lower == "l" {
        true
    } else if let Some(rest) = lower
        .strip_prefix("lines")
        .or_else(|| lower.strip_prefix("line"))
    {
        rest.chars()
            .next()
            .is_some_and(|c| !c.is_alphanumeric() && c != '_')
    } else if let Some(rest) = lower.strip_prefix('l') {
        rest.chars()
            .next()
            .is_some_and(|c| !c.is_alphanumeric() && c != '_')
    } else {
        false
    };
    if matches_kw { before.trim_end() } else { s }
}

fn strip_markdown_file_link(s: &str) -> Option<&str> {
    if !s.starts_with('[') || !s.ends_with(')') {
        return None;
    }
    let close_bracket = s.find("](")?;
    if s[1..close_bracket].contains(']') {
        return None;
    }
    let inner = s[close_bracket + 2..s.len() - 1].trim();
    if inner.to_ascii_lowercase().starts_with("file://") || inner.starts_with('/') {
        Some(inner)
    } else {
        None
    }
}

/// Normalize a tool file path or `file://` URI at the codec boundary, borrowing when already clean.
#[must_use]
pub fn normalize_tool_file_path(raw_path: &str) -> Cow<'_, str> {
    if raw_path.is_empty() {
        return Cow::Borrowed("");
    }
    let needs_clean = raw_path.contains(['\r', '\n', '\0']) || raw_path != raw_path.trim();
    let mut cleaned: Cow<'_, str> = if needs_clean {
        Cow::Owned(raw_path.replace(['\r', '\n', '\0'], "").trim().to_owned())
    } else {
        Cow::Borrowed(raw_path)
    };
    if cleaned.is_empty() {
        return Cow::Borrowed("");
    }
    if cleaned.starts_with('`') {
        let s = if let Some(end_bt) = cleaned[1..].find('`') {
            cleaned[1..1 + end_bt].trim().to_owned()
        } else {
            cleaned.trim_matches('`').trim().to_owned()
        };
        cleaned = Cow::Owned(s);
    }
    let after_annot = strip_trailing_line_annotation(&cleaned);
    if after_annot.len() != cleaned.len() {
        cleaned = Cow::Owned(after_annot.to_owned());
    }
    if let Some(link_inner) = strip_markdown_file_link(&cleaned) {
        cleaned = Cow::Owned(link_inner.to_owned());
    }
    if cleaned.len() >= 7 && cleaned[..7].eq_ignore_ascii_case("file://") {
        let stripped = strip_trailing_line_annotation(&cleaned);
        if let Ok(parsed) = Url::parse(stripped) {
            let netloc = parsed.host_str().unwrap_or("");
            let unquoted = percent_decode(parsed.path());
            let raw_out = if !netloc.is_empty() && !netloc.eq_ignore_ascii_case("localhost") {
                format!("//{netloc}{unquoted}")
            } else {
                let bytes = unquoted.as_bytes();
                if bytes.len() >= 3
                    && bytes[0] == b'/'
                    && bytes[1].is_ascii_alphabetic()
                    && bytes[2] == b':'
                {
                    unquoted[1..].to_owned()
                } else {
                    unquoted
                }
            };
            return Cow::Owned(raw_out.replace(['\r', '\n', '\0'], "").trim().to_owned());
        }
    }
    cleaned
}
