"""Stage-1 Local Workbench package: read-only indexing, search, draft copy, and loopback HTTP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..workspace import build_lazy_package_exports

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
__all__, __getattr__, __dir__ = build_lazy_package_exports(
    __name__,
    globals(),
    _EXPORT_GROUPS,
    _LAZY_SUBMODULES,
)
