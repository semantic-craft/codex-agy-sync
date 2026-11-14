"""Stage-1 Local Workbench package: read-only indexing, search, draft copy, and loopback HTTP server."""

from __future__ import annotations

from .discovery import UnionFind, discover_workspaces
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
from .indexer import WorkbenchIndexer
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
    "stop_workbench_server",
    "validate_host_header",
    "validate_origin_header",
]
