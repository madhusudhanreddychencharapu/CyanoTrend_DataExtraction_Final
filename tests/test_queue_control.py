"""Plan and queue control: plans, cancel-plan, cancel-queued, and the status report."""
import json
import uuid

import pytest

from cyanotrend import queue_control as qc
from cyanotrend.cli import main
from cyanotrend.common import Workspace, file_lock
from cyanotrend.ledger import Ledger


def scene(day="2024-08-14"):
    return {"id": str(uuid.uuid4()), "name": "S3A_x", "satellite": "S3A", "start": f"{day}T16:15:18Z",
            "end": f"{day}T16:18:18Z",
            "regions": [{"country": "USA", "state_id": "MI", "state_name": "Michigan"}]}


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace.create(tmp_path / "out", tmp_path / "refs", tmp_path / "scratch", tmp_path / "ocssw")
    workspace.ensure()
    return workspace


def plan(ws, ledger, plan_id, scenes, file=True, created="2024-09-01T00:00:00Z"):
    selection = {"start": "2024-08-14", "end": "2024-08-15", "country": "USA", "state_id": "MI",
                 "state_name": "Michigan"}
    path = ws.output / "plans" / f"{plan_id}.json"
    ledger.enqueue({"id": plan_id, "created": created, "reference_id": "ref", "selection": selection,
                    "scenes": scenes}, path)
    if file:
        path.write_text(json.dumps({"schema": "cyanotrend-plan-1", "id": plan_id, "selection": selection,
                                    "scenes": scenes}))
    return path


def tables(ledger):
    with ledger.connect() as c:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("scenes", "plans", "plan_scenes", "scene_regions", "timings")}


def test_plan_with_deleted_file_is_flagged_and_cancel_plan_clears_its_queue(ws):
    ledger = Ledger(ws.db)
    scenes = [scene() for _ in range(3)]
    plan(ws, ledger, "aaaa1111", scenes, file=False)  # the plan file was deleted by hand
    row, = qc.list_plans(ws, ledger)
    assert row["plan_file"] == "MISSING" and row["queued"] == 3 and row["scenes"] == 3
    report = qc.status_report(ws, ledger)
    assert report["plans_without_file"] == 1
    assert any("no plan file" in message for message in report["attention"])

    dry = qc.cancel_plan(ws, ledger, "aaaa", dry_run=True)  # a unique start of the ID is enough
    assert dry["removed_scenes"] == 3 and dry["dry_run"] and tables(ledger)["scenes"] == 3

    done = qc.cancel_plan(ws, ledger, "aaaa1111")
    assert done["removed_scenes"] == 3 and done["removed_by_status"] == {"queued": 3} and done["plan_removed"]
    assert set(tables(ledger).values()) == {0}  # no leftovers in any table


def test_cancel_plan_keeps_running_finished_and_shared_scenes(ws):
    ledger = Ledger(ws.db)
    queued, retrying, failed, running, finished, shared = (scene() for _ in range(6))
    path = plan(ws, ledger, "aaaa1111", [queued, retrying, failed, running, finished, shared])
    plan(ws, ledger, "bbbb2222", [shared], created="2024-09-02T00:00:00Z")
    ledger.update(retrying["id"], status="retry")
    ledger.update(failed["id"], status="failed")
    ledger.update(running["id"], status="running")
    ledger.update(finished["id"], status="done", stage="complete")

    result = qc.cancel_plan(ws, ledger, "aaaa1111")
    assert result["removed_by_status"] == {"queued": 1, "retry": 1, "failed": 1}
    assert result["kept_because_another_plan_lists_them"] == 1
    assert result["left_in_plan"] == {"running": 1, "done": 1}
    assert not result["plan_removed"] and path.exists()
    assert {r["id"] for r in ledger.rows()} == {running["id"], finished["id"], shared["id"]}
    assert ledger.scene(shared["id"])["status"] == "queued"
    rows = {r["plan_id"]: r for r in qc.list_plans(ws, ledger)}
    assert rows["bbbb2222"]["queued"] == 1 and rows["aaaa1111"]["scenes"] == 2
    assert any("running" in note for note in result["notes"])


def test_cancel_plan_moves_the_file_so_run_cannot_import_it_again(ws):
    ledger = Ledger(ws.db)
    path = plan(ws, ledger, "aaaa1111", [scene()])
    result = qc.cancel_plan(ws, ledger, "aaaa1111")
    assert result["plan_removed"] and not path.exists()
    assert (ws.output / "plans" / "cancelled" / "aaaa1111.json").exists()
    assert list((ws.output / "plans").glob("*.json")) == []  # import_plans only looks here


def test_unregistered_plan_file_is_listed_and_can_be_set_aside(ws):
    ledger = Ledger(ws.db)
    (ws.output / "plans" / "cccc3333.json").write_text(json.dumps({"id": "cccc3333", "selection": {"start": "2025-01-01"}}))
    row, = qc.list_plans(ws, ledger)
    assert row["plan_file"] == "not registered" and row["start"] == "2025-01-01"
    assert qc.status_report(ws, ledger)["unregistered_plan_files"] == 1
    assert qc.cancel_plan(ws, ledger, "cccc")["plan_removed"]
    assert qc.list_plans(ws, ledger) == []


def test_plan_reference_must_be_known_and_unambiguous(ws):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene()])
    plan(ws, ledger, "aaaa2222", [scene()])
    with pytest.raises(ValueError, match="ambiguous"):
        qc.cancel_plan(ws, ledger, "aaaa")
    with pytest.raises(ValueError, match="Unknown plan"):
        qc.cancel_plan(ws, ledger, "zzzz")


def test_cancel_queued_by_plan_dates_and_failed_flag(ws):
    ledger = Ledger(ws.db)
    d14, d15, failed_scene, other = scene("2024-08-14"), scene("2024-08-15"), scene("2024-08-15"), scene("2024-08-15")
    plan(ws, ledger, "aaaa1111", [d14, d15, failed_scene])
    plan(ws, ledger, "bbbb2222", [other], created="2024-09-02T00:00:00Z")
    ledger.update(failed_scene["id"], status="failed")

    assert qc.cancel_queued(ws, ledger, plan="aaaa", start="2024-08-15", dry_run=True)["removed_scenes"] == 1
    assert len(ledger.rows()) == 4
    result = qc.cancel_queued(ws, ledger, plan="aaaa", start="2024-08-15")
    assert result["removed_scenes"] == 1 and ledger.scene(d14["id"])["status"] == "queued"
    assert ledger.scene(failed_scene["id"])["status"] == "failed"  # failed stays unless asked
    assert ledger.scene(other["id"])["status"] == "queued"          # another plan's scene

    result = qc.cancel_queued(ws, ledger, include_failed=True)
    assert result["removed_scenes"] == 3 and ledger.rows() == []
    note = next(n for n in result["notes"] if n.startswith("Plans left with no scenes"))
    assert "aaaa1111" in note and "bbbb2222" in note  # both plans lost every scene


def test_cancel_queued_reports_plans_it_emptied(ws):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene()])
    result = qc.cancel_queued(ws, ledger)
    assert result["removed_scenes"] == 1
    assert any("aaaa1111" in note and "cancel-plan" in note for note in result["notes"])
    assert len(ledger.plan_rows()) == 1  # kept: its plan file would otherwise be imported again


def test_a_scene_a_worker_has_claimed_is_never_removed(ws):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene(), scene()])
    claimed = ledger.claim()
    result = qc.cancel_queued(ws, ledger)
    assert result["removed_scenes"] == 1 and claimed["id"] not in result["scene_ids"]
    assert ledger.scene(claimed["id"])["status"] == "running"
    assert qc.cancel_plan(ws, ledger, "aaaa1111")["left_in_plan"] == {"running": 1}


def test_status_tells_whether_run_is_active_and_flags_abandoned_scenes(ws):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene(), scene()])
    claimed = ledger.claim()
    report = qc.status_report(ws, ledger)
    assert report["run_active"] == "no" and report["scenes_total"] == 2
    assert report["counts"] == {"queued": 1, "running": 1} and report["waiting_to_start"] == 1
    assert report["active"][0]["scene_id"] == claimed["id"]
    assert report["active"][0]["state"].startswith("ABANDONED")
    assert any("`run` is not active" in message for message in report["attention"])
    with file_lock(ws.output / ".service.lock"):  # what a live `cyanotrend run` holds
        live = qc.status_report(ws, ledger)
    assert live["run_active"] == "yes" and live["active"][0]["state"] == "active"
    assert live["attention"] == ["nothing needs attention"]


def test_status_counts_scenes_that_belong_to_no_plan(ws):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene()])
    with ledger.connect() as c:
        c.execute("DELETE FROM plan_scenes")
    report = qc.status_report(ws, ledger)
    assert report["scenes_without_plan"] == 1
    assert any("no plan" in message for message in report["attention"])
    assert qc.cancel_queued(ws, ledger)["removed_scenes"] == 1


def test_commands_refuse_a_mistyped_output_instead_of_creating_a_workspace(tmp_path, capsys):
    out = tmp_path / "typo"
    for command in ("status", "plans", "scenes", "daily --start 2024-08-14 --end 2024-08-14"):
        assert main(["--output", str(out), *command.split()]) == 1
        assert "No cyanotrend workspace" in capsys.readouterr().out
    assert not out.exists()


def test_cli_plans_cancel_and_status_round_trip(ws, capsys):
    ledger = Ledger(ws.db)
    plan(ws, ledger, "aaaa1111", [scene(), scene()])
    base = ["--output", str(ws.output)]
    assert main([*base, "--json", "plans"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["queued"] == 2
    assert main([*base, "cancel-plan", "aaaa", "--dry-run"]) == 0
    assert "removed_scenes: 2" in capsys.readouterr().out and len(ledger.rows()) == 2
    assert main([*base, "cancel-queued", "--plan", "aaaa", "--start", "2024-08-14"]) == 0
    capsys.readouterr()
    assert ledger.rows() == []
    assert main([*base, "status"]) == 0
    text = capsys.readouterr().out
    assert "run_active: no" in text and "scenes_total: 0" in text
    assert main([*base, "cancel-plan", "aaaa"]) == 0 and ledger.plan_rows() == []
    assert main([*base, "cancel-plan", "aaaa"]) == 1  # already gone: an error, not a crash
