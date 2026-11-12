"""End-to-end isolated sandbox verification suite for codex-agy-sync."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from _fixtures import seed_fixture


def run_verification_suite(
    *,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    execute_init: Callable[[str, Path], dict[str, Any]],
    execute_doctor: Callable[[Path], tuple[bool, dict[str, Any]]],
    execute_cli: Callable[..., tuple[int, dict[str, Any]]],
    execute_hold_lock: Callable[..., tuple[int, dict[str, Any]]],
    execute_inspect_stores: Callable[..., dict[str, Any]],
    execute_cleanup: Callable[[dict[str, Path]], tuple[bool, dict[str, Any]]],
    write_json: Callable[[Path, Any], None],
) -> tuple[int, dict[str, Any]]:
    execute_init(run_id, run_dir)

    scenarios: dict[str, dict[str, Any]] = {}
    failure_error: str | None = None
    c2a_cid = "00000000-0000-4000-8000-000000000011"
    c2a_tid = "01960000-0000-7000-8000-000000000011"
    int_cid = "00000000-0000-4000-8000-000000000051"
    int_tid = "01960000-0000-7000-8000-000000000051"
    lock_cid = "00000000-0000-4000-8000-000000000099"

    try:
        doc_ok, doc_out = execute_doctor(run_dir)
        assert doc_ok, f"doctor failed: {doc_out}"
        scenarios["init_and_doctor"] = {"verdict": "PASS"}

        rc, rec = execute_cli(
            layout, "status-empty", ["status", "--workspace", "@WORKSPACE"], echo_output=False
        )
        assert rc == 0 and json.loads(rec["stdout"])["sessions"] == []
        v_empty = execute_inspect_stores(layout, "after-empty-status")
        assert v_empty["codex_state_db_exists"] is False and v_empty["agy_project_files_count"] == 0
        scenarios["status_readonly_unpaired"] = {"verdict": "PASS"}

        rc_pair, rec_pair = execute_cli(
            layout, "pair-ws", ["pair", "--workspace", "@WORKSPACE"], echo_output=False
        )
        assert rc_pair == 0 and json.loads(rec_pair["stdout"])["agy_project_id"]
        seed_fixture(layout, "codex-basic")
        rc_c2a, rec_c2a = execute_cli(
            layout,
            "sync-c2a-initial",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--codex-thread-id",
                c2a_tid,
                "--cascade-id",
                c2a_cid,
                "--direction",
                "codex_to_agy",
                "--mode",
                "auto",
            ],
            echo_output=False,
        )
        c2a_json = json.loads(rec_c2a["stdout"])
        assert rc_c2a == 0
        assert c2a_json["transition_kind"] == "initial_import"
        assert c2a_json["transport_used"] == "offline_storage"
        v_c2a = execute_inspect_stores(
            layout, "after-c2a-initial", cascade_id=c2a_cid, codex_thread_id=c2a_tid
        )
        assert v_c2a["agy_conv_db"]["exists"] is True and v_c2a["agy_conv_db"]["user_version"] >= 1
        assert v_c2a["agy_summary_row"] is not None and v_c2a["agy_summary_row"]["source"] != 17
        assert v_c2a["agy_summary_row"]["nesting_depth"] == 0
        assert v_c2a["agy_annotation_pbtxt"]["exists"] is True
        assert v_c2a["agy_transcript"]["exists"] is True and v_c2a["agy_transcript"]["line_count"] >= 1
        assert v_c2a["agyhub_summaries_pb_size"] > 0
        scenarios["codex_to_agy_initial_and_companions"] = {"verdict": "PASS"}

        seed_fixture(layout, "codex-append-turn")
        rc_inc, rec_inc = execute_cli(
            layout,
            "sync-c2a-append",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--codex-thread-id",
                c2a_tid,
                "--cascade-id",
                c2a_cid,
                "--direction",
                "auto_incremental",
            ],
            echo_output=False,
        )
        inc_json = json.loads(rec_inc["stdout"])
        assert rc_inc == 0 and inc_json["transition_kind"] == "incremental_append"
        assert inc_json["updated_cursor"]["last_synced_turn_id"] == "turn-3"
        v_inc = execute_inspect_stores(
            layout, "after-c2a-append", cascade_id=c2a_cid, codex_thread_id=c2a_tid
        )
        assert v_inc["agy_conv_db"]["step_count"] == v_inc["agy_summary_row"]["step_count"]
        scenarios["incremental_append"] = {"verdict": "PASS"}

        seed_fixture(layout, "agy-interrupted")
        rc_def, rec_def = execute_cli(
            layout,
            "sync-a2c-exclude-interrupted",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--cascade-id",
                int_cid,
                "--codex-thread-id",
                int_tid,
                "--direction",
                "agy_to_codex",
            ],
            echo_output=False,
        )
        def_json = json.loads(rec_def["stdout"])
        assert rc_def == 0 and def_json["transition_kind"] == "initial_import"
        assert def_json["events_applied"] == 2
        assert def_json["updated_cursor"]["last_synced_turn_id"] == "turn-1"
        assert def_json["updated_cursor"]["rescued_inflight_turn"] is False
        _, insp_def = execute_cli(
            layout,
            "inspect-codex-before-rescue",
            ["inspect", "--workspace", "@WORKSPACE", "--codex-thread-id", int_tid],
            echo_output=False,
        )
        assert json.loads(insp_def["stdout"])["completed_turns"] == 1

        rc_res, rec_res = execute_cli(
            layout,
            "sync-a2c-rescue-interrupted",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--cascade-id",
                int_cid,
                "--codex-thread-id",
                int_tid,
                "--direction",
                "agy_to_codex",
                "--include-interrupted",
            ],
            echo_output=False,
        )
        res_json = json.loads(rec_res["stdout"])
        assert rc_res == 0 and res_json["transition_kind"] == "incremental_append"
        assert res_json["updated_cursor"]["last_synced_turn_id"] == "turn-2"
        assert res_json["updated_cursor"]["rescued_inflight_turn"] is True
        _, insp_res = execute_cli(
            layout,
            "inspect-codex-after-rescue",
            ["inspect", "--workspace", "@WORKSPACE", "--codex-thread-id", int_tid],
            echo_output=False,
        )
        insp_res_json = json.loads(insp_res["stdout"])
        assert insp_res_json["completed_turns"] == 2 and insp_res_json["has_inflight_turn"] is False

        rc_noop, rec_noop = execute_cli(
            layout,
            "sync-a2c-rescue-noop",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--cascade-id",
                int_cid,
                "--codex-thread-id",
                int_tid,
                "--direction",
                "agy_to_codex",
                "--include-interrupted",
            ],
            echo_output=False,
        )
        noop_json = json.loads(rec_noop["stdout"])
        assert rc_noop == 0 and noop_json["transition_kind"] == "no_op"
        assert noop_json["events_applied"] == 0
        scenarios["b2_interrupted_rescue_and_noop"] = {"verdict": "PASS"}

        rc_lock, rec_lock = execute_hold_lock(
            layout,
            lock_cid,
            "sync-locked-defer",
            [
                "sync-session",
                "--workspace",
                "@WORKSPACE",
                "--codex-thread-id",
                c2a_tid,
                "--cascade-id",
                lock_cid,
                "--direction",
                "codex_to_agy",
            ],
            echo_output=False,
        )
        lock_json = json.loads(rec_lock["stdout"])
        assert rc_lock == 0 and lock_json["transition_kind"] == "deferred_busy"
        assert lock_json["events_applied"] == 0
        v_lock = execute_inspect_stores(layout, "after-locked-defer", cascade_id=lock_cid)
        assert v_lock["agy_conv_db"]["exists"] is False
        scenarios["presence_lock_deferral"] = {"verdict": "PASS"}
    except Exception as exc:
        failure_error = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_ok, cleanup_receipt = execute_cleanup(layout)
        scenarios["cleanup_and_host_isolation"] = {
            "verdict": "PASS" if cleanup_ok else "FAIL",
            "sandbox_exists_after": cleanup_receipt["sandbox_exists_after"],
            "host_fingerprints_unchanged": cleanup_receipt["host_fingerprints_unchanged"],
        }

    passed = [name for name, item in scenarios.items() if item.get("verdict") == "PASS"]
    all_ok = (
        failure_error is None
        and cleanup_ok
        and len(passed) == len(scenarios)
        and bool(cleanup_receipt["host_fingerprints_unchanged"])
    )
    summary = {
        "overall_verdict": "PASS" if all_ok else "FAIL",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "scenarios_passed": len(passed),
        "scenarios_total": len(scenarios),
        "scenarios": scenarios,
        "host_fingerprints_unchanged": bool(cleanup_receipt["host_fingerprints_unchanged"]),
        "error": failure_error,
    }
    write_json(layout["suite_summary"], summary)
    return (0 if all_ok else 1), summary
