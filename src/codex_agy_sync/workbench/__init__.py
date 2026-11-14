"""Stage-1 Local Workbench package: read-only indexing, search, draft copy, and loopback HTTP server."""

from __future__ import annotations

from .indexer import WorkbenchIndexer, discover_workspaces
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
    load_workbench_lease,
    query_workbench_status,
    run_serve_server,
    stop_workbench_server,
)
from .service import WorkbenchService

__all__ = [
    "STAGE_1_CAPABILITIES",
    "CitationRef",
    "DraftRenderResult",
    "HandoffHopSummary",
    "SearchHit",
    "TimelineTurn",
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
    "load_workbench_lease",
    "query_workbench_status",
    "run_serve_server",
    "stop_workbench_server",
]
