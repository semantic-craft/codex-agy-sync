"""Loopback HTTP request handler and security header gates for Stage-1 Workbench."""

from __future__ import annotations

import contextlib
import dataclasses
import html
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import json
import re
import secrets
import time
from typing import Any, Mapping
import urllib.parse

from ..bridge.handoff_store import HandoffPendingError
from ..bridge.paths_and_locks import SyncBusyLockError
from ..bridge.registry import UnpairedWorkspaceError

_MAX_BODY_BYTES = 1_048_576
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_ORIGINS = frozenset({"127.0.0.1", "localhost", "::1"})
_BAD_HOST_CHARS = frozenset("@/?#\\")


def validate_host_header(raw_host: str | None, bound_port: int) -> bool:
    candidate = (raw_host or "").strip()
    if not candidate or candidate.endswith(":") or any(ch.isspace() or ch in _BAD_HOST_CHARS for ch in candidate):
        return False
    try:
        parsed = urllib.parse.urlsplit(f"http://{candidate}")
        return (
            parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and parsed.hostname in _ALLOWED_HOSTS
            and (parsed.port is None or parsed.port == bound_port)
        )
    except ValueError:
        return False


def validate_origin_header(raw_origin: str | None, bound_port: int) -> bool:
    if raw_origin is None:
        return True
    cleaned = raw_origin.strip()
    if not cleaned or cleaned.lower() == "null" or any(ch.isspace() or ch in "@?#\\" for ch in cleaned):
        return False
    try:
        p = urllib.parse.urlsplit(cleaned)
        return (
            p.scheme == "http"
            and p.hostname in _ALLOWED_ORIGINS
            and p.port == bound_port
            and p.username is None
            and p.password is None
            and p.path in {"", "/"}
            and not p.query
            and not p.fragment
        )
    except ValueError:
        return False


class _HeadCloseLocator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.close_pos: tuple[int, int] | None = None

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "head" and self.close_pos is None:
            self.close_pos = self.getpos()


def _find_head_close_offset(html_text: str) -> int:
    locator = _HeadCloseLocator()
    locator.feed(html_text)
    locator.close()
    if locator.close_pos is None:
        return -1
    lineno, col = locator.close_pos
    pos = 0
    for _ in range(lineno - 1):
        nl = html_text.find("\n", pos)
        if nl < 0:
            return -1
        pos = nl + 1
    return pos + col


def inject_html_metas(html_text: str, token: str) -> str:
    escaped = html.escape(token, quote=True)
    metas = f'<meta name="workbench-token" content="{escaped}">\n<meta name="workbench-mode" content="stage1-live">\n'
    idx = _find_head_close_offset(html_text)
    return (html_text[:idx] + metas + html_text[idx:]) if idx >= 0 else (metas + html_text)


def _parse_bool_param(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: Any

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status: int, payload: Any) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._send_bytes(status, raw, "application/json; charset=utf-8")

    def _check_security_headers(self) -> bool:
        port = self.server.owner.port
        host_headers = self.headers.get_all("Host") or []
        if len(host_headers) != 1 or not validate_host_header(host_headers[0], port):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return False
        origin_headers = self.headers.get_all("Origin") or []
        if len(origin_headers) > 1 or (origin_headers and not validate_origin_header(origin_headers[0], port)):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_origin"})
            return False
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_fetch_site"})
            return False
        return True

    def _check_api_token(self) -> bool:
        provided = (self.headers.get("X-Workbench-Token") or "").strip()
        if not provided or not secrets.compare_digest(provided, self.server.owner.token):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _read_json_body(self) -> dict[str, Any] | None:
        te_headers = [v for v in (self.headers.get_all("Transfer-Encoding") or []) if v.strip()]
        if te_headers:
            self.close_connection = True
            self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "unsupported_transfer_encoding"})
            return None
        cl_headers = self.headers.get_all("Content-Length") or []
        if not cl_headers:
            self.close_connection = True
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
            return None
        if len(cl_headers) > 1 or not re.fullmatch(r"[0-9]+", cl_headers[0].strip()):
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return None
        content_len = int(cl_headers[0].strip())
        if content_len > _MAX_BODY_BYTES:
            if content_len <= _MAX_BODY_BYTES * 2:
                self._drain_request_body(content_len)
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload_too_large"})
            return None
        body = self.rfile.read(content_len) if content_len > 0 else b""
        if len(body) != content_len:
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete_body"})
            return None
        try:
            payload = json.loads(body.decode("utf-8") if body else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "json_object_required"})
            return None
        return payload

    def _drain_request_body(self, total_bytes: int) -> None:
        orig_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 0.4
        remaining = total_bytes
        try:
            self.connection.settimeout(0.05)
            while remaining > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read(min(remaining, 65_536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(orig_timeout)

    def do_GET(self) -> None:
        if not self._check_security_headers():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self.server.owner.health_payload())
            return
        if path == "/":
            self.server.owner.touch_activity()
            ui_path = self.server.owner.ui_path
            if not ui_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "ui_not_found", "ui_path": str(ui_path)})
                return
            try:
                ui_text = ui_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "ui_not_found", "ui_path": str(ui_path)})
                return
            except (OSError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ui_read_failed", "message": str(exc)})
                return
            rendered = inject_html_metas(ui_text, self.server.owner.token)
            self._send_bytes(HTTPStatus.OK, rendered.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._check_api_token():
            return
        self.server.owner.touch_activity()
        self._handle_api_get(path, urllib.parse.parse_qs(parsed.query))

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        svc = self.server.owner.service
        refresh = _parse_bool_param(query.get("refresh", [""])[0])
        ws_id = (query.get("workspace_id", [""])[0] or "").strip() or None
        if path == "/api/snapshot":
            self._send_json(HTTPStatus.OK, svc.get_snapshot(workspace_id=ws_id, refresh=refresh).to_dict())
            return
        if path.startswith("/api/works/"):
            work_id = urllib.parse.unquote(path[len("/api/works/") :]).strip()
            try:
                self._send_json(HTTPStatus.OK, svc.get_work(work_id, refresh=refresh).to_dict())
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "work_not_found", "message": str(exc)})
            return
        if path == "/api/search":
            q, scope, raw_limit = query.get("q", [""])[0], query.get("scope", ["all"])[0], query.get("limit", ["50"])[0]
            try:
                hits = svc.search(q, workspace_id=ws_id, scope=scope, limit=int(raw_limit))
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search", "message": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"query": q, "scope": scope, "count": len(hits), "hits": [h.to_dict() for h in hits]})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._check_security_headers():
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/") or path == "/api/send":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._check_api_token():
            return
        self.server.owner.touch_activity()
        payload = self._read_json_body()
        if payload is None:
            return
        if path == "/api/draft/render":
            self._handle_draft_render(payload)
        elif path == "/api/handoff":
            self._handle_handoff(payload)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _validate_str_fields(self, payload: Mapping[str, Any], names: tuple[str, ...], *, prefix: str = "invalid_") -> bool:
        for name in names:
            val = payload.get(name)
            if val is not None and not isinstance(val, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"{prefix}{name}"})
                return False
        return True

    def _validate_bool_fields(self, payload: Mapping[str, Any], names: tuple[str, ...]) -> bool:
        for name in names:
            val = payload.get(name)
            if val is not None and not isinstance(val, bool):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid_{name}"})
                return False
        return True

    def _handle_draft_render(self, payload: dict[str, Any]) -> None:
        raw_citations = () if payload.get("citations") is None else payload.get("citations")
        if not isinstance(raw_citations, (list, tuple)):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_citations"})
            return
        if not self._validate_str_fields(payload, ("work_id", "note", "target_hint", "target_session")):
            return
        for item in raw_citations:
            if not isinstance(item, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_citations"})
                return
            if not self._validate_str_fields(
                item,
                ("workspace_id", "work_id", "host", "session_id", "turn_id", "excerpt", "label"),
                prefix="invalid_citation_",
            ):
                return
        try:
            result = self.server.owner.service.render_draft(
                payload.get("work_id") or "",
                payload.get("note") or "",
                citations=raw_citations,
                target_hint=payload.get("target_hint") or "codex",
            )
            self._send_json(HTTPStatus.OK, result.to_dict())
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "work_not_found", "message": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_draft", "message": str(exc)})

    def _handle_handoff(self, payload: dict[str, Any]) -> None:
        if not self._validate_str_fields(
            payload, ("workspace", "workspace_id", "source_id", "direction", "title", "target_session"),
        ):
            return
        if not self._validate_bool_fields(payload, ("latest", "chain", "pair_if_offline")):
            return
        ws = payload.get("workspace") or payload.get("workspace_id") or ""
        try:
            receipt = self.server.owner.service.trigger_handoff(
                workspace=ws,
                source_id=payload.get("source_id"),
                direction=payload.get("direction"),
                latest=bool(payload.get("latest", False)),
                chain=bool(payload.get("chain", False)),
                title=payload.get("title"),
                pair_if_offline=bool(payload.get("pair_if_offline", False)),
            )
            self._send_json(HTTPStatus.OK, receipt)
        except HandoffPendingError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": "handoff_pending", "message": str(exc), "receipt": dataclasses.asdict(exc.receipt)})
        except SyncBusyLockError as exc:
            self._send_json(HTTPStatus.CONFLICT, exc.to_error_payload())
        except UnpairedWorkspaceError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, exc.to_error_payload())
        except (ValueError, RuntimeError) as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "handoff_rejected", "message": str(exc)})
