"""Stage-1 Local Workbench package: read-only indexing, search, draft copy, and loopback HTTP server."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import UnionFind, discover_workspaces
    from .indexer import WorkbenchIndexer
    from .menubar_feed import (
        MenubarCandidateState,
        MenubarFeedSnapshot,
        MenubarSessionItem,
        build_menubar_feed,
    )
    from .models import (
        STAGE_1_CAPABILITIES,
        CitationRef,
        DraftRenderResult,
        HandoffHopSummary,
        SearchHit,
        TimelineTurn,
        WorkCard,
        WorkbenchSnapshot,
        WorkSessionLink,
        WorkspaceCard,
    )
    from .server import (
        WorkbenchLease,
        WorkbenchServer,
        default_workbench_ui_path,
        inject_html_metas,
        load_workbench_lease,
        query_workbench_status,
        run_serve_server,
        stop_workbench_server,
        validate_host_header,
        validate_origin_header,
    )
    from .service import WorkbenchService

_EXPORT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".discovery", ("UnionFind", "discover_workspaces")),
    (".indexer", ("WorkbenchIndexer",)),
    (".menubar_feed", ("MenubarCandidateState", "MenubarFeedSnapshot", "MenubarSessionItem", "build_menubar_feed")),
    (
        ".models",
        (
            "STAGE_1_CAPABILITIES", "CitationRef", "DraftRenderResult", "HandoffHopSummary",
            "SearchHit", "TimelineTurn", "WorkCard", "WorkSessionLink", "WorkbenchSnapshot", "WorkspaceCard",
        ),
    ),
    (
        ".server",
        (
            "WorkbenchLease", "WorkbenchServer", "default_workbench_ui_path", "inject_html_metas",
            "load_workbench_lease", "query_workbench_status", "run_serve_server",
            "stop_workbench_server", "validate_host_header", "validate_origin_header",
        ),
    ),
    (".service", ("WorkbenchService",)),
)
_LAZY_SUBMODULES: frozenset[str] = frozenset({
    "catalog", "discovery", "handler", "indexer", "lease",
    "menubar_feed", "models", "server", "service",
})
_LAZY_EXPORTS: dict[str, str] = {sym: mod for mod, syms in _EXPORT_GROUPS for sym in syms}


def __getattr__(name: str) -> Any:
    if name in _LAZY_SUBMODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if (mod_path := _LAZY_EXPORTS.get(name)) is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = getattr(importlib.import_module(mod_path, __name__), name)
    globals()[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "STAGE_1_CAPABILITIES",
    "CitationRef",
    "DraftRenderResult",
    "HandoffHopSummary",
    "SearchHit",
    "TimelineTurn",
    "UnionFind",
    "WorkCard",
    "WorkSessionLink",
    "WorkbenchIndexer",
    "WorkbenchLease",
    "WorkbenchServer",
    "WorkbenchService",
    "WorkbenchSnapshot",
    "WorkspaceCard",
    "default_workbench_ui_path",
    "discover_workspaces",
    "inject_html_metas",
    "load_workbench_lease",
    "query_workbench_status",
    "run_serve_server",
    "MenubarCandidateState",
    "MenubarFeedSnapshot",
    "MenubarSessionItem",
    "build_menubar_feed",
    "stop_workbench_server",
    "validate_host_header",
    "validate_origin_header",
]
