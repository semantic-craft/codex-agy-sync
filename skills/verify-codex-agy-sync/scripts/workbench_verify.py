#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _workbench_sandbox import (
    CLI_BIN,
    REPO_ROOT,
    AgyLoopbackRpcStub,
    build_isolated_env,
    capture_host_fingerprints,
    cleanup_sandbox,
    http_call,
    now_iso,
    record_git_receipts,
    run_doctor_checks,
    sandbox_layout,
    seed_workbench_sandbox,
    validate_run_dir,
    write_json,
)


DOM_VIEW_JS = (
    "({title:document.title,url:location.href,theme:document.documentElement.dataset.theme,"
    "viewport:{w:innerWidth,h:innerHeight,sw:document.documentElement.scrollWidth},"
    "activeView:(document.getElementById('panelBackdrop')&&!document.getElementById('panelBackdrop').classList.contains('hidden'))?'panel':'main',"
    "activeWork:document.getElementById('workTitle')?.textContent?.trim(),"
    "workStatus:document.getElementById('workStatus')?.textContent?.trim(),"
    "protoLabel:document.getElementById('protoLabelText')?.textContent?.trim(),"
    "localStatus:document.getElementById('localStatus')?.textContent?.trim(),"
    "workspaces:[...document.querySelectorAll('#sidebarProjects button[data-live-workspace]')].map(el=>el.textContent.trim()),"
    "works:[...document.querySelectorAll('#workItems [data-live-work]')].map(el=>el.textContent.trim()),"
    "chips:[...document.querySelectorAll('#sessionChips [data-live-session]')].map(el=>el.textContent.trim()),"
    "chainHops:document.querySelectorAll('#liveHandoffChain .chain-hop').length,"
    "highlighted:document.querySelectorAll('#timeline .message.highlight').length,"
    "drawerOpen:!document.getElementById('drawer')?.classList.contains('hidden')})"
)


class SuiteBlocked(RuntimeError):
    pass


@dataclass
class WorkbenchRunner:
    layout: dict[str, Path]
    run_id: str
    prototype: Path
    ab_bin: str
    timeout_s: int
    env: dict[str, str] = field(default_factory=build_isolated_env)
    browser_session: str = ""
    browser_started: bool = False
    server_proc: subprocess.Popen[str] | None = None
    rpc_stub: AgyLoopbackRpcStub | None = None
    cmd_seq: int = 0
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    base_url: str = ""
    port: int = 0
    server_pid: int = 0

    def __post_init__(self) -> None:
        self.browser_session = f"wb-{self.run_id}"

    def cli_args(self, *extra: str) -> list[str]:
        return [
            str(CLI_BIN),
            "--gemini-config-dir", str(self.layout["gemini_config_dir"]),
            "--agy-data-dir", str(self.layout["agy_data_dir"]),
            "--codex-home-dir", str(self.layout["codex_home_dir"]),
            "--sync-state-dir", str(self.layout["sync_state_dir"]),
            *extra,
        ]

    def run_cmd(self, argv: list[str], slug: str, *, allow_fail: bool = False) -> dict[str, Any]:
        self.cmd_seq += 1
        started = time.monotonic()
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=self.env, capture_output=True, text=True, timeout=self.timeout_s, check=False,
        )
        receipt = {
            "seq": self.cmd_seq, "slug": slug, "argv": argv, "exit_code": proc.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000), "stdout": proc.stdout, "stderr": proc.stderr,
        }
        write_json(self.layout["commands_dir"] / f"{self.cmd_seq:03d}-{slug}.json", receipt)
        if not allow_fail and proc.returncode != 0:
            raise AssertionError(f"Command failed ({slug}): {proc.stderr or proc.stdout}")
        return receipt

    def ab(self, *args: str, allow_fail: bool = False) -> dict[str, Any]:
        slug = "-".join(re.sub(r"[^a-zA-Z0-9]+", "-", a).strip("-") for a in args[:3])[:48] or "ab"
        return self.run_cmd(
            [self.ab_bin, "--session", self.browser_session, "--idle-timeout", "120000", *args],
            f"ab-{slug}", allow_fail=allow_fail,
        )

    def ab_eval(self, expr: str) -> Any:
        payload = json.loads(self.ab("eval", expr, "--json")["stdout"])
        if not payload.get("success"):
            raise AssertionError(f"agent-browser eval failed ({expr}): {payload}")
        return payload.get("data", {}).get("result")

    def wait_until(self, desc: str, expr: str, *, predicate: Any = bool, timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.ab_eval(expr)
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"Timed out waiting for {desc}; last={last!r}")

    def capture_evidence(self, name: str, scenario: dict[str, Any]) -> None:
        view_path = self.layout["views_dir"] / f"{name}.json"
        shot_path = self.layout["screenshots_dir"] / f"{name}.png"
        write_json(view_path, self.ab_eval(DOM_VIEW_JS))
        self.ab("screenshot", str(shot_path))
        scenario["evidence"].extend([
            str(view_path.relative_to(self.layout["run_dir"])),
            str(shot_path.relative_to(self.layout["run_dir"])),
        ])

    def run_scenario(self, name: str, fn: Any) -> None:
        rec: dict[str, Any] = {"name": name, "started_at": now_iso(), "status": "PASS", "checks": [], "evidence": []}
        try:
            fn(rec)
        except Exception as exc:
            rec["status"] = "FAIL"
            rec["error"] = f"{type(exc).__name__}: {exc}"
            if self.browser_started:
                try:
                    self.ab("screenshot", str(self.layout["screenshots_dir"] / f"fail-{name}.png"), allow_fail=True)
                except Exception:
                    pass
            raise
        finally:
            rec["ended_at"] = now_iso()
            self.scenarios.append(rec)

    def _spawn_server(self) -> dict[str, Any]:
        argv = self.cli_args("serve", "--ui", str(self.prototype), "--port", "0", "--idle-timeout", "120")
        self.server_proc = subprocess.Popen(
            argv, cwd=str(REPO_ROOT), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert self.server_proc.stdout is not None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.server_proc.stdout], [], [], 0.2)
            if ready:
                line = self.server_proc.stdout.readline().strip()
                if line:
                    return json.loads(line)
            if self.server_proc.poll() is not None:
                err = self.server_proc.stderr.read() if self.server_proc.stderr else ""
                raise AssertionError(f"serve exited prematurely ({self.server_proc.returncode}): {err}")
        raise AssertionError("Timed out reading serve startup JSON.")

    def _select_workspace(self, name: str) -> None:
        self.ab_eval(
            f"[...document.querySelectorAll('#sidebarProjects button[data-live-workspace]')]"
            f".find(b=>b.textContent.includes({json.dumps(name)})).click()"
        )

    def scenario_01_server_and_security(self, rec: dict[str, Any]) -> None:
        startup = self._spawn_server()
        self.base_url = str(startup["url"]).rstrip("/")
        self.port = int(startup["port"])
        self.server_pid = int(startup["pid"])
        assert self.base_url.startswith("http://127.0.0.1:") and startup.get("stage") == "stage_1", startup
        rec["checks"].append({"startup": {k: startup[k] for k in ("url", "pid", "port", "stage")}})

        st_health, body_health = http_call(f"{self.base_url}/api/health")
        health = json.loads(body_health)
        assert st_health == 200 and health.get("stage") == "stage_1" and health.get("remote_message_send") is False, health
        rec["checks"].append({"health_200": health})

        st_unauth, _ = http_call(f"{self.base_url}/api/snapshot")
        assert st_unauth == 401, f"Expected 401 on unauthenticated /api/snapshot, got {st_unauth}"
        rec["checks"].append({"unauth_snapshot_status": st_unauth})

        st_root, root_html = http_call(f"{self.base_url}/")
        assert st_root == 200, st_root
        m = re.search(r'<meta\s+name="workbench-token"\s+content="([^"]+)"', root_html)
        assert m, "Injected workbench token meta tag missing from /"
        token = m.group(1)

        st_send, send_body = http_call(
            f"{self.base_url}/api/send", method="POST",
            headers={"X-Workbench-Token": token, "Content-Type": "application/json"}, body=b"{}",
        )
        assert st_send == 404, f"Expected 404 on POST /api/send, got {st_send}: {send_body}"
        rec["checks"].append({"post_send_status": st_send})

        status_json = json.loads(self.run_cmd(self.cli_args("serve", "--status"), "serve-status")["stdout"])
        assert status_json.get("running") is True and status_json.get("pid") == self.server_pid and status_json.get("port") == self.port
        rec["checks"].append({"cli_status": status_json})

        lease_path = self.layout["sync_state_dir"] / "workbench" / "server.json"
        lease_mode = stat.S_IMODE(lease_path.stat().st_mode)
        lease_raw = lease_path.read_text(encoding="utf-8")
        lease_json = json.loads(lease_raw)
        assert lease_mode == 0o600 and all("token" not in k.lower() for k in lease_json) and token not in lease_raw
        rec["checks"].append({"lease_mode": oct(lease_mode), "lease_keys": sorted(lease_json.keys())})

        st_snap, body_snap = http_call(f"{self.base_url}/api/snapshot", headers={"X-Workbench-Token": token})
        assert st_snap == 200, st_snap
        rec["checks"].append({"snapshot_workspace_count": len(json.loads(body_snap).get("workspaces", []))})

    def scenario_02_live_hydration_and_exclusions(self, rec: dict[str, Any], seed_info: dict[str, Any]) -> None:
        self.browser_started = True
        self.ab("open", f"{self.base_url}/")
        self.ab("set", "viewport", "1440", "900")
        self.ab("set", "media", "light", "reduced-motion")
        self.wait_until(
            "Stage 1 hydration",
            "({lbl:document.getElementById('protoLabelText')?.textContent||'',"
            "cnt:document.querySelectorAll('#sidebarProjects button[data-live-workspace]').length})",
            predicate=lambda v: isinstance(v, dict) and "Stage 1 本地工作台" in v.get("lbl", "") and v.get("cnt") == 2,
        )
        state = self.ab_eval(
            "({text:document.body.innerText,"
            "ws:[...document.querySelectorAll('#sidebarProjects button[data-live-workspace]')].map(b=>b.textContent.trim())})"
        )
        assert len(state["ws"]) == 2 and any("repo-paired" in w for w in state["ws"]) and any("repo-unpaired" in w for w in state["ws"])
        for marker in (*seed_info["excluded_markers"].values(), "Archive", ".private_keys", "outside-of-project"):
            assert marker not in state["text"], f"Excluded token leaked into UI: {marker}"

        self._select_workspace("repo-unpaired")
        banner = self.wait_until(
            "unpaired banner",
            "({cls:document.getElementById('livePairingBanner')?.className||'',"
            "pill:document.querySelector('.pair-pill.unpaired')?.textContent||''})",
            predicate=lambda v: isinstance(v, dict) and "unpaired" in v.get("cls", "") and "hidden" not in v.get("cls", ""),
        )
        rec["checks"].append({"workspaces": state["ws"], "unpaired_banner": banner["pill"]})
        self.capture_evidence("live-02-hydrated", rec)

    def scenario_03_cross_host_search_and_locate(self, rec: dict[str, Any], seed_info: dict[str, Any]) -> None:
        self.ab("click", "#allWorkButton")
        self.ab("fill", "#mainSearch", seed_info["search_query"])
        hits_all = self.wait_until(
            "cross-host search hits",
            "[...document.querySelectorAll('#workItems button[data-live-hit]')].map(b=>b.textContent.trim())",
            predicate=lambda items: isinstance(items, list) and len(items) >= 2,
        )
        assert any("AGY" in h for h in hits_all) and any("Codex" in h for h in hits_all), hits_all

        self.ab("click", '#searchFilters [data-filter="Codex"]')
        hits_codex = self.wait_until(
            "Codex scoped hits",
            "[...document.querySelectorAll('#workItems button[data-live-hit]')].map(b=>b.textContent.trim())",
            predicate=lambda items: isinstance(items, list) and len(items) >= 1 and all("Codex" in h for h in items),
        )
        self.ab("click", '#searchFilters [data-filter="AGY"]')
        hits_agy = self.wait_until(
            "AGY scoped hits",
            "[...document.querySelectorAll('#workItems button[data-live-hit]')].map(b=>b.textContent.trim())",
            predicate=lambda items: isinstance(items, list) and len(items) >= 1 and all("AGY" in h for h in items),
        )
        self.ab("click", '#searchFilters [data-filter="all"]')
        self.wait_until("all hits restored", "document.querySelectorAll('#workItems button[data-live-hit]').length>=2")
        self.ab("click", '#workItems button[data-live-hit="0"]')
        located = self.wait_until(
            "located turn banner",
            "({bannerHidden:document.getElementById('sourceBanner')?.classList.contains('hidden'),"
            "bannerText:document.getElementById('sourceBannerText')?.textContent?.trim(),"
            "highlights:document.querySelectorAll('#timeline .message.highlight').length})",
            predicate=lambda v: isinstance(v, dict) and v.get("bannerHidden") is False and v.get("highlights", 0) >= 1,
        )

        self.ab("click", "#panelViewButton")
        self.wait_until("panel opened", "!document.getElementById('panelBackdrop')?.classList.contains('hidden')")
        self.ab("fill", "#panelSearch", seed_info["search_query"])
        panel_hits = self.wait_until(
            "panel hits",
            "[...document.querySelectorAll('#paletteBody [data-live-panel-preview]')].map(b=>b.textContent.trim())",
            predicate=lambda items: isinstance(items, list) and len(items) >= 1,
        )
        self.ab("click", '#paletteBody [data-live-panel-preview="0"]')
        self.wait_until(
            "panel closed after navigate",
            "document.getElementById('panelBackdrop')?.classList.contains('hidden') && document.querySelectorAll('#timeline .message.highlight').length>=1",
        )
        rec["checks"].append({
            "hits_all_count": len(hits_all), "hits_codex_count": len(hits_codex),
            "hits_agy_count": len(hits_agy), "located": located, "panel_hits_count": len(panel_hits),
        })
        self.capture_evidence("live-03-search-locate", rec)

    def scenario_04_stage1_citation_draft_and_ime(self, rec: dict[str, Any]) -> None:
        self.ab("fill", "#mainSearch", "")
        self.wait_until("search cleared", "document.querySelectorAll('#workItems [data-live-work]').length >= 1")
        self._select_workspace("repo-paired")
        self.wait_until("repo-paired cite button", "document.querySelectorAll('#timeline button[data-live-cite]').length >= 1")
        self.ab("click", '#timeline button[data-live-cite="0"]')
        self.wait_until(
            "ask drawer opened with citation",
            "!document.getElementById('drawer')?.classList.contains('hidden') && Number((document.getElementById('fragmentCount')?.textContent||'').match(/\\d+/)?.[0]||0)>=1",
        )
        note_text = "Please review PKCE verifier flow"
        self.ab("fill", "#draftNote", note_text)
        ime_state = self.ab_eval(
            "(() => { const el = document.getElementById('draftNote'); el.focus();"
            "el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '中文' }));"
            "const notCanceled = el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, keyCode: 229, bubbles: true, cancelable: true }));"
            "el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '中文' }));"
            "return { defaultPrevented: !notCanceled, respHidden: document.getElementById('draftResponse')?.classList.contains('hidden') }; })()"
        )
        assert ime_state.get("defaultPrevented") is False, f"IME composition Enter called preventDefault(): {ime_state}"
        assert ime_state.get("respHidden") is True, f"IME composition Enter submitted prematurely: {ime_state}"

        self.ab("click", "#submitDraft")
        draft_res = self.wait_until(
            "Stage 1 markdown draft preview",
            "(() => { const p = document.getElementById('draftMarkdownPreview');"
            "const m = document.querySelector('#draftResponse .draft-result-meta');"
            "const btn = document.getElementById('copyDraftMarkdown');"
            "if (!p || !m || !btn) return null;"
            "return { md: p.textContent, meta: m.textContent, hasCopy: !btn.disabled }; })()",
        )
        assert "pkce verifier" in draft_res["md"].lower() and note_text in draft_res["md"]
        assert "delivery_executed: false" in draft_res["meta"] and draft_res["hasCopy"] is True
        self.ab("click", "#copyDraftMarkdown")
        rec["checks"].append({"ime_guard_kept_hidden": ime_state["respHidden"], "draft_meta": draft_res["meta"]})
        self.capture_evidence("live-04-draft-copy", rec)

    def scenario_05_handoff_and_refresh(self, rec: dict[str, Any]) -> None:
        self.ab("click", "#closeDrawer")
        self._select_workspace("repo-unpaired")
        self.wait_until("repo-unpaired selected", "document.getElementById('workStatus')?.textContent?.includes('未配对')")
        self.ab("click", "#handoffButton")
        self.wait_until("handoff drawer open", "!document.getElementById('drawer')?.classList.contains('hidden')")
        self.ab("click", "#submitDraft")
        unpaired_err = self.wait_until(
            "unpaired_workspace 422 error card",
            "(() => { const c = document.querySelector('#draftResponse .error-card'); const b = document.getElementById('retryPairHandoff');"
            "return (c && b) ? { txt: c.textContent, hasRetry: !b.disabled } : null; })()",
        )
        assert "unpaired_workspace" in unpaired_err["txt"], unpaired_err
        self.ab("click", "#retryPairHandoff")
        busy_err = self.wait_until(
            "pair_if_offline retry hits active daemon lock guard",
            "(() => { const c = document.querySelector('#draftResponse .error-card');"
            "return (c && c.textContent.includes('sync_busy_lock')) ? { txt: c.textContent } : null; })()",
        )

        self.ab("click", "#closeDrawer")
        self._select_workspace("repo-paired")
        self.wait_until("repo-paired selected", "document.getElementById('workStatus')?.textContent?.includes('双端已配对')")
        self.ab("click", "#handoffButton")
        self.wait_until("handoff drawer open on paired", "!document.getElementById('drawer')?.classList.contains('hidden')")
        if self.ab_eval("document.getElementById('handoffPairIfOffline')?.checked === true"):
            self.ab("click", "#handoffPairIfOffline")
        self.ab("fill", "#handoffTitle", "PKCE Stage1 Verified Handoff")
        self.ab("scrollintoview", "#submitDraft")
        self.ab("click", "#submitDraft")
        receipt_view = self.wait_until(
            "200 HandoffReceipt card in UI",
            "(() => { const err = document.querySelector('#draftResponse .error-card');"
            "if (err) return { error: err.textContent };"
            "const card = document.querySelector('#draftResponse .receipt-card'); if (!card) return null;"
            "const kvs = [...card.querySelectorAll('.receipt-key')].map((k,i)=>({k:k.textContent.trim(),v:card.querySelectorAll('.receipt-val')[i]?.textContent?.trim()}));"
            "return { kvs, chips: document.querySelectorAll('#sessionChips [data-live-session]').length, hops: document.querySelectorAll('#liveHandoffChain .chain-hop').length }; })()",
            timeout=20.0,
        )
        assert "error" not in receipt_view, f"Handoff returned UI error: {receipt_view}"
        self.ab("scrollintoview", "#draftResponse")
        target_id = next((item["v"] for item in receipt_view["kvs"] if item["k"] == "target_id"), "")
        assert target_id and target_id != "—" and receipt_view["hops"] >= 2 and receipt_view["chips"] >= 3, receipt_view
        assert (self.layout["agy_data_dir"] / "conversations" / f"{target_id}.db").is_file()
        receipts = [json.loads(hf.read_text(encoding="utf-8")) for hf in sorted((self.layout["sync_state_dir"] / "handoffs").glob("*.json"))]
        new_rec = next((r for r in receipts if r.get("target_id") == target_id and r.get("status") == "ready"), None)
        assert new_rec is not None and int(new_rec.get("chain_depth", 0)) >= 1, receipts
        rec["checks"].append({
            "unpaired_422_retry_present": unpaired_err["hasRetry"],
            "retry_busy_lock_guard": "sync_busy_lock" in busy_err["txt"],
            "handoff_target_id": target_id,
            "chain_depth": new_rec["chain_depth"],
            "ui_session_chips": receipt_view["chips"],
            "ui_chain_hops": receipt_view["hops"],
        })
        self.capture_evidence("live-05-handoff", rec)

    def scenario_06_responsive_themes_and_errors(self, rec: dict[str, Any]) -> None:
        self.ab("click", "#closeDrawer")
        self.ab("set", "viewport", "1440", "900")
        self.ab("set", "media", "light", "reduced-motion")
        if self.ab_eval("document.documentElement.dataset.theme") != "light":
            self.ab("click", "#themeButton")
        self.wait_until("light 1440 layout", "document.documentElement.dataset.theme==='light' && innerWidth===1440 && document.documentElement.scrollWidth<=innerWidth")
        self.capture_evidence("live-06-light-1440", rec)

        self.ab("set", "viewport", "390", "844")
        self.wait_until("light 390 layout", "document.documentElement.dataset.theme==='light' && innerWidth===390 && document.documentElement.scrollWidth<=innerWidth")
        self.capture_evidence("live-06-light-390", rec)

        self.ab("click", "#themeButton")
        self.wait_until("dark 390 layout", "document.documentElement.dataset.theme==='dark' && innerWidth===390 && document.documentElement.scrollWidth<=innerWidth")
        self.capture_evidence("live-06-dark-390", rec)

        err_payload = json.loads(self.ab("errors", "--json")["stdout"])
        assert err_payload.get("success") and err_payload.get("data", {}).get("errors") == [], err_payload
        reqs = json.loads(self.ab("network", "requests", "--json")["stdout"]).get("data", {}).get("requests", [])
        bad_urls = [r.get("url", "") for r in reqs if not str(r.get("url", "")).startswith(f"{self.base_url}/")]
        assert not bad_urls, f"Non-loopback requests observed: {bad_urls}"
        rec["checks"].append({"errors": [], "network_request_count": len(reqs), "all_requests_loopback": True})

    def scenario_07_stop_and_cleanup(self, rec: dict[str, Any]) -> None:
        stop_json = json.loads(self.run_cmd(self.cli_args("serve", "--stop"), "serve-stop")["stdout"])
        assert stop_json.get("stopped") is True and self.server_proc is not None, stop_json
        rc = self.server_proc.wait(timeout=10)
        assert rc == 0, f"Expected clean server exit code 0 after --stop, got {rc}"
        assert not (self.layout["sync_state_dir"] / "workbench" / "server.json").exists()
        status_after = json.loads(self.run_cmd(self.cli_args("serve", "--status"), "serve-status-after-stop")["stdout"])
        assert status_after.get("running") is False, status_after
        rec["checks"].append({"stop_json": stop_json, "server_exit_code": rc, "lease_removed": True, "running_after_stop": False})


def _run_suite(args: argparse.Namespace) -> int:
    ok_doc, doc_report = run_doctor_checks(args.prototype)
    if not ok_doc:
        print(json.dumps(doc_report, indent=2, ensure_ascii=False))
        return 2
    try:
        run_id, run_dir = validate_run_dir(Path(args.run_dir) if args.run_dir else None)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, indent=2, ensure_ascii=False))
        return 2

    layout = sandbox_layout(run_dir)
    before_fp = capture_host_fingerprints()
    seed_info = seed_workbench_sandbox(layout, run_id)
    git_head = record_git_receipts(layout)
    write_json(
        layout["env_receipt"],
        {
            "run_id": run_id, "run_dir": str(run_dir), "created_at": now_iso(),
            "prototype": doc_report["prototype"], "git_head": git_head,
            "target_daemon_mode": "sandbox_loopback_rpc_stub", "native_desktop": "NOT_RUN",
            "host_fingerprints_before": before_fp,
        },
    )

    runner = WorkbenchRunner(
        layout=layout, run_id=run_id, prototype=Path(doc_report["prototype"]["path"]),
        ab_bin=str(doc_report["agent_browser"]["path"]), timeout_s=max(15, int(args.timeout)),
    )

    def _handle_sig(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("Signal interrupted workbench verification.")

    prev_int, prev_term = signal.signal(signal.SIGINT, _handle_sig), signal.signal(signal.SIGTERM, _handle_sig)
    suite_status, failure_detail = "PASS", None
    try:
        runner.rpc_stub = AgyLoopbackRpcStub(layout["agy_data_dir"])
        assert runner.rpc_stub.start() > 0
        runner.run_scenario("01-server-launch-status-and-security", runner.scenario_01_server_and_security)
        runner.run_scenario("02-live-hydration-and-exclusions", lambda r: runner.scenario_02_live_hydration_and_exclusions(r, seed_info))
        runner.run_scenario("03-cross-host-search-and-locate", lambda r: runner.scenario_03_cross_host_search_and_locate(r, seed_info))
        runner.run_scenario("04-stage1-citation-draft-and-ime", runner.scenario_04_stage1_citation_draft_and_ime)
        runner.run_scenario("05-one-click-handoff-and-refresh", runner.scenario_05_handoff_and_refresh)
        runner.run_scenario("06-responsive-themes-and-errors", runner.scenario_06_responsive_themes_and_errors)
        runner.run_scenario("07-server-stop-and-cleanup", runner.scenario_07_stop_and_cleanup)
    except BaseException as exc:
        suite_status, failure_detail = "FAIL", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if runner.browser_started:
                try:
                    runner.ab("close", allow_fail=True)
                except Exception:
                    pass
            if runner.server_proc is not None and runner.server_proc.poll() is None:
                runner.server_proc.terminate()
                try:
                    runner.server_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runner.server_proc.kill()
                    runner.server_proc.wait(timeout=5)
            if runner.rpc_stub is not None:
                runner.rpc_stub.stop()
            cleanup_ok, cleanup_receipt = cleanup_sandbox(layout)
        finally:
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)

    if not cleanup_ok:
        suite_status = "FAIL"
        failure_detail = failure_detail or "Sandbox cleanup or host fingerprint invariant failed."

    summary = {
        "status": suite_status, "run_id": run_id, "run_dir": str(run_dir), "evidence_dir": str(layout["evidence_dir"]),
        "target_daemon_mode": "sandbox_loopback_rpc_stub", "visual_semantics": "REVIEW_REQUIRED",
        "native_desktop": "NOT_RUN", "host_fingerprints_unchanged": bool(cleanup_receipt.get("host_fingerprints_unchanged")),
        "scenarios": runner.scenarios, "cleanup": cleanup_receipt, "failure": failure_detail, "finished_at": now_iso(),
    }
    write_json(layout["suite_summary"], summary)
    print(json.dumps({
        "status": suite_status, "run_id": run_id, "run_dir": str(run_dir), "summary": str(layout["suite_summary"]),
        "host_fingerprints_unchanged": summary["host_fingerprints_unchanged"],
        "visual_semantics": summary["visual_semantics"], "native_desktop": summary["native_desktop"], "failure": failure_detail,
    }, indent=2, ensure_ascii=False))
    return 0 if suite_status == "PASS" else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Stage-1 Local Workbench end-to-end.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_doc = sub.add_parser("doctor", help="Read-only preflight check for live workbench verification.")
    p_doc.add_argument("--prototype", required=True, help="External Main.html prototype artifact path.")
    p_suite = sub.add_parser("suite", help="Run isolated end-to-end Stage-1 workbench suite.")
    p_suite.add_argument("--prototype", required=True, help="External Main.html prototype artifact path.")
    p_suite.add_argument("--run-dir", default=None, help="Non-existing run directory outside /tmp.")
    p_suite.add_argument("--timeout", type=int, default=60, help="Per-command timeout in seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        ok, report = run_doctor_checks(args.prototype)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if ok else 2
    if args.command == "suite":
        return _run_suite(args)
    raise SuiteBlocked(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
