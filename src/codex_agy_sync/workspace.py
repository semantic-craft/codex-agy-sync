"""Tier 0 workspace identity, branded domain IDs, host kinds, and sensitive path exclusions."""

from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import re
import sys
from typing import Any, NewType, Sequence
from urllib.parse import unquote, urlsplit

PACKAGE_VERSION: str = "0.3.0"

SessionId = NewType("SessionId", str)
CascadeId = NewType("CascadeId", str)
TrajectoryId = NewType("TrajectoryId", str)
CodexThreadId = NewType("CodexThreadId", str)
StepIndex = NewType("StepIndex", int)
RolloutLineNo = NewType("RolloutLineNo", int)
TurnId = NewType("TurnId", str)
ToolCallId = NewType("ToolCallId", str)
LedgerSeqNo = NewType("LedgerSeqNo", int)
ContentDigest = NewType("ContentDigest", str)
WorkspaceRootUri = NewType("WorkspaceRootUri", str)
WorkspacePath = NewType("WorkspacePath", Path)
AgyProjectId = NewType("AgyProjectId", str)
CodexProjectId = NewType("CodexProjectId", str)
CodexLegacyProjectId = NewType("CodexLegacyProjectId", str)

FORBIDDEN_AGY_PROJECT_IDS: frozenset[str] = frozenset(
    {"", "default-cli-project", "outside-of-project", "null", "none"}
)

_LINE_ANNOT_RE = re.compile(r"\s+\((?:lines?|L)\b[^)]*\)\s*$", flags=re.IGNORECASE)
_MD_FILE_LINK_RE = re.compile(r"^\[[^\]]*\]\(\s*(file://.+?|/[^)]+?)\s*\)$", flags=re.IGNORECASE)


class HostKind(str, Enum):
    ANTIGRAVITY = "antigravity"
    CODEX = "codex"


class AgyGuiSafeSource(Enum):
    """Antigravity trajectory sources passing the Desktop sidebar filter (`17` CLI excluded)."""

    UNSPECIFIED = (0, "")
    CASCADE_CLIENT = (1, "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT")
    JETBOX = (18, "CORTEX_TRAJECTORY_SOURCE_JETBOX")
    AGENT_API = (19, "CORTEX_TRAJECTORY_SOURCE_AGENT_API")
    CIDER = (20, "CORTEX_TRAJECTORY_SOURCE_CIDER")

    @property
    def proto_enum_value(self) -> int:
        return self.value[0]

    @property
    def summary_column_name(self) -> str:
        return self.value[1]


def parse_workspace_identity(raw_path_or_uri: str | Path) -> tuple[WorkspacePath, WorkspaceRootUri]:
    """Parse and canonicalize an untrusted path or `file://` URI at the boundary."""
    if raw_path_or_uri is None:
        raise ValueError("Workspace path cannot be None.")
    raw_text = str(raw_path_or_uri).strip()
    if not raw_text:
        raise ValueError("Workspace path cannot be empty.")
    if raw_text.startswith("file://"):
        parsed = urlsplit(raw_text)
        if parsed.netloc.lower() not in ("", "localhost"):
            raise ValueError(f"Unsupported non-local file URI authority: {parsed.netloc!r}")
        unquoted = unquote(parsed.path)
        if parsed.scheme != "file" or not unquoted.startswith("/"):
            raise ValueError(f"Invalid file URI {raw_path_or_uri!r}.")
        candidate = Path(unquoted)
    else:
        if "://" in raw_text:
            raise ValueError(f"Unsupported URI scheme in {raw_path_or_uri!r}.")
        expanded = os.path.expanduser(os.path.expandvars(raw_text))
        if expanded.startswith("~") or "$" in expanded or raw_text in (".", ".."):
            raise ValueError(f"Invalid or unexpanded workspace specifier {raw_path_or_uri!r}.")
        candidate = Path(expanded)
    resolved = candidate.resolve(strict=False)
    if str(resolved) == "/" or len(resolved.parts) <= 1:
        raise ValueError(f"Workspace path cannot resolve to filesystem root {raw_path_or_uri!r}.")
    return WorkspacePath(resolved), WorkspaceRootUri(resolved.as_uri().rstrip("/"))


def validate_session_id(raw_id: str, label: str = "session_id") -> str:
    """Validate an untrusted session or thread identifier at the boundary."""
    if (
        not isinstance(raw_id, str)
        or not raw_id
        or raw_id != raw_id.strip()
        or len(raw_id) > 128
        or any(sep in raw_id for sep in ("..", "/", "\\"))
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_id)
    ):
        raise ValueError(f"Invalid {label}: {raw_id!r}")
    return raw_id


def normalize_tool_file_path(raw_path: str) -> str:
    """Normalize a tool file path or `file://` URI at the codec boundary."""
    if not raw_path:
        return ""
    cleaned = str(raw_path).replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("`"):
        end_bt = cleaned.find("`", 1)
        cleaned = cleaned[1:end_bt].strip() if end_bt != -1 else cleaned.strip("`").strip()
    cleaned = _LINE_ANNOT_RE.sub("", cleaned)
    if md_match := _MD_FILE_LINK_RE.match(cleaned):
        cleaned = md_match.group(1).strip()
    if cleaned.lower().startswith("file://"):
        split = urlsplit(_LINE_ANNOT_RE.sub("", cleaned))
        netloc = split.netloc
        unquoted_path = unquote(split.path)
        if netloc.lower() not in ("", "localhost"):
            raw_out = f"//{netloc}{unquoted_path}"
        elif len(unquoted_path) >= 3 and unquoted_path[0] == "/" and unquoted_path[1].isalpha() and unquoted_path[2] == ":":
            raw_out = unquoted_path[1:]
        else:
            raw_out = unquoted_path
        return raw_out.replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    return cleaned


def _lexical_abs(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_path_equal_or_under(cand: Path, root: Path) -> bool:
    if cand == root or cand.is_relative_to(root):
        return True
    if sys.platform == "darwin":
        cand_parts = tuple(p.casefold() for p in cand.parts)
        root_parts = tuple(p.casefold() for p in root.parts)
        return len(cand_parts) >= len(root_parts) and cand_parts[: len(root_parts)] == root_parts
    return False


def is_sensitive_excluded_path(candidate: Path, home_dir: Path) -> bool:
    """Return `True` when `candidate` or its resolved target sits under `Archive` or `.private_keys`."""
    cand_variants = (_lexical_abs(candidate), candidate.resolve(strict=False))
    home_abs = _lexical_abs(home_dir)
    for sub in ("Archive", ".private_keys"):
        raw_root = home_abs / sub
        res_root = raw_root.resolve(strict=False)
        for root in (raw_root, res_root):
            for cand in cand_variants:
                if _is_path_equal_or_under(cand, root):
                    return True
    return False


def candidate_home_dirs(paths_or_codex_home: Any = None) -> tuple[Path, ...]:
    """Return unique lexical and resolved home directories anchored by optional Codex home and `$HOME`."""
    homes: list[Path] = []
    codex_home = getattr(paths_or_codex_home, "codex_home_dir", paths_or_codex_home)
    if isinstance(codex_home, Path):
        homes.append(codex_home.parent)
    elif isinstance(codex_home, str) and codex_home.strip():
        homes.append(Path(codex_home).parent)
    homes.append(Path.home())
    if env_home := (os.environ.get("HOME") or "").strip():
        homes.append(Path(env_home).expanduser())
    unique: list[Path] = []
    seen: set[Path] = set()
    for home in homes:
        for variant in (home, home.resolve(strict=False)):
            if variant not in seen:
                seen.add(variant)
                unique.append(variant)
    return tuple(unique)


def normalize_workspace_spec(raw_spec: str | Path) -> str:
    """Normalize a CLI or API workspace specifier while preserving `file://` URIs and lexical symlink paths."""
    text = str(raw_spec or "").strip()
    if not text:
        return ""
    if text.startswith("file://") or "://" in text:
        return text
    expanded = os.path.expanduser(os.path.expandvars(text))
    if expanded.startswith("~") or "$" in expanded:
        return expanded
    return os.path.abspath(expanded)


def raw_spec_to_candidate_path(raw: str | Path) -> Path | None:
    text = normalize_workspace_spec(raw)
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlsplit(text)
        if parsed.netloc.lower() not in ("", "localhost"):
            return None
        unquoted = unquote(parsed.path)
        return Path(unquoted) if unquoted.startswith("/") else None
    if "://" in text:
        return None
    return Path(text)


def is_excluded_for_homes(candidate: Path, homes: Sequence[Path]) -> bool:
    return any(is_sensitive_excluded_path(candidate, home) for home in homes)


__all__ = [
    "AgyGuiSafeSource",
    "AgyProjectId",
    "CascadeId",
    "CodexLegacyProjectId",
    "CodexProjectId",
    "CodexThreadId",
    "ContentDigest",
    "FORBIDDEN_AGY_PROJECT_IDS",
    "HostKind",
    "LedgerSeqNo",
    "PACKAGE_VERSION",
    "RolloutLineNo",
    "SessionId",
    "StepIndex",
    "ToolCallId",
    "TrajectoryId",
    "TurnId",
    "WorkspacePath",
    "WorkspaceRootUri",
    "candidate_home_dirs",
    "is_excluded_for_homes",
    "is_sensitive_excluded_path",
    "normalize_tool_file_path",
    "normalize_workspace_spec",
    "parse_workspace_identity",
    "raw_spec_to_candidate_path",
    "validate_session_id",
]
