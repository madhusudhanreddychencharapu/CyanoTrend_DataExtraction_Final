"""`run --auto-daily` and `progress`: when a date may merge, what a merge records, and the coordinator."""
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import uuid

import pytest

from cyanotrend.cli import main, parser
from cyanotrend.common import Workspace, file_lock
from cyanotrend.daily import auto
from cyanotrend.daily.merge import run_daily
from cyanotrend.ledger import Ledger
from cyanotrend.service import serve

FAKE_DAILY = Path(__file__).with_name("fake_daily.py")
FAKE_WORKER = Path(__file__).with_name("fake_worker.py")
MI = ("USA", "MI", "Michigan")
WI = ("USA", "WI", "Wisconsin")


def scene(day, hour=10, regions=(MI,), owner=MI):
    return {"id": str(uuid.uuid4()), "name": f"S3A_{day}_{hour}", "satellite": "S3A",
            "start": f"{day}T{hour:02d}:15:18Z", "end": f"{day}T{hour:02d}:18:18Z",
            "regions": [{"country": c, "state_id": s, "state_name": n} for c, s, n in regions],
            "output_region": {"country": owner[0], "state_id": owner[1], "state_name": owner[2]}}


def enqueue(ws, ledger, scenes, plan_id="mi", state=MI, start="2024-08-14", end="2024-08-16"):
    selection = {"country": state[0], "state_id": state[1], "state_name": state[2], "start": start, "end": end}
    ledger.enqueue({"id": plan_id, "created": f"2024-09-0{len(plan_id)}T00:00:00Z", "reference_id": "ref",
                    "selection": selection, "scenes": scenes}, ws.output / "plans" / f"{plan_id}.json")


def finish(ledger, *scenes, status="done"):
    for s in scenes:
        ledger.update(s["id"], status=status, stage="complete" if status == "done" else status)


def states(ledger, state=MI):
    plans = ledger.plan_regions()[(state[0], state[1])]["plans"]
    return {d.isoformat(): v for d, v in auto.date_states(ledger, state[0], state[1], plans, {}).items()}


@pytest.fixture
def ws(tmp_path):
    w = Workspace.create(tmp_path / "out", tmp_path / "refs", tmp_path / "scratch", tmp_path / "ocssw")
    w.ensure()
    return w


# ---------------------------------------------------------------------------
# When may a date merge?
# ---------------------------------------------------------------------------

def test_a_date_waits_for_every_scene_from_the_day_before_to_the_day_after(ws):
    ledger = Ledger(ws.db)
    before, a, b, c = scene("2024-08-13"), scene("2024-08-14"), scene("2024-08-15"), scene("2024-08-16")
    enqueue(ws, ledger, [before, a, b, c])
    finish(ledger, a, b, c)  # the scene of 08-13 is still queued
    found = states(ledger)
    assert found["2024-08-14"]["state"] == "waiting" and found["2024-08-14"]["open"] == 1  # 08-13 is inside its window
    assert found["2024-08-15"]["state"] == "ready" and found["2024-08-16"]["state"] == "ready"  # 08-13 is two days away
    finish(ledger, before)
    assert states(ledger)["2024-08-14"]["state"] == "ready"


def test_a_failed_scene_does_not_hold_a_date_back_but_a_retry_changes_it(ws):
    ledger = Ledger(ws.db)
    ok, bad = scene("2024-08-15"), scene("2024-08-15", hour=12)
    enqueue(ws, ledger, [ok, bad])
    finish(ledger, ok)
    finish(ledger, bad, status="failed")
    first = states(ledger)["2024-08-15"]
    assert first["state"] == "ready" and first["done"] == 1 and first["open"] == 0  # partial lakes, flagged by the merge

    run = ledger.daily_start("USA", "MI", "2024-08-15", "auto", first["signature"])
    ledger.daily_finish(run, "merged", {"complete": 1})
    assert states(ledger)["2024-08-15"]["state"] == "merged"

    ledger.requeue_failed()  # the failed scene is open again ...
    assert states(ledger)["2024-08-15"]["state"] == "waiting"
    finish(ledger, bad)  # ... and done: the merge no longer matches the scenes
    again = states(ledger)["2024-08-15"]
    assert again["state"] == "changed" and again["signature"] != first["signature"]


def test_dates_without_usable_scenes_are_not_merged(ws):
    ledger = Ledger(ws.db)
    failed, empty = scene("2024-08-14"), scene("2024-08-14", hour=12)
    far = scene("2024-08-20")  # more than a day from every plan date
    enqueue(ws, ledger, [failed, empty, far])
    finish(ledger, failed, status="failed")
    finish(ledger, empty, status="skipped_no_lakes")
    finish(ledger, far)
    found = states(ledger)
    assert found["2024-08-14"]["state"] == "nothing to merge"
    assert found["2024-08-16"]["state"] == "no scenes"
    assert [k for k, v in found.items() if v["state"] in ("ready", "changed")] == []


def test_scenes_of_other_plans_count_only_when_they_touch_the_state(ws):
    ledger = Ledger(ws.db)
    mine, theirs_touching, theirs_far = scene("2024-08-15"), scene("2024-08-15", hour=12, regions=(WI, MI), owner=WI), \
        scene("2024-08-15", hour=13, regions=(WI,), owner=WI)
    enqueue(ws, ledger, [mine])
    enqueue(ws, ledger, [theirs_touching, theirs_far], plan_id="wi!", state=WI)
    finish(ledger, mine)
    assert states(ledger)["2024-08-15"]["state"] == "waiting"  # Wisconsin's scene also touches Michigan lakes
    finish(ledger, theirs_touching)
    assert states(ledger)["2024-08-15"]["state"] == "ready"  # its other scene touches no Michigan lake


# ---------------------------------------------------------------------------
# A manual `daily` is recorded like an automatic one
# ---------------------------------------------------------------------------

def _run_daily(ws, day, **kwargs):
    result = {"date": day, "processed_scenes": 2, "merged_complete": 3, "merged_partial": 1, "waiting": 2,
              "unavailable": 0, "missing_scenes": 1, "not_planned_scenes": 1, "folders": ["f"],
              "_missing": [{"name": "S3B_gap", "satellite": "S3B", "sensing_start": "20240815T101500",
                            "ledger_status": "not planned"}]}
    region = {"country": "USA", "state_id": "MI", "state_name": "Michigan"}
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}), \
            patch("cyanotrend.daily.merge.state_scope", return_value=("lakes", region)), \
            patch("cyanotrend.daily.merge.merge_day", **kwargs) as merge_day:
        merge_day.return_value = kwargs.pop("return_value", result) if "side_effect" not in kwargs else None
        return run_daily(ws, day, day, catalogue=lambda *a, **k: [], lakes="lakes", admin=object(),
                         country="USA", state_id="MI")


def test_manual_daily_run_is_recorded_with_the_same_signature_the_coordinator_expects(ws):
    ledger = Ledger(ws.db)
    a, b = scene("2024-08-14"), scene("2024-08-15")
    enqueue(ws, ledger, [a, b])
    finish(ledger, a, b)
    result = _run_daily(ws, "2024-08-15")
    assert result["ok"] and "_missing" not in result["days"][0]  # the printed summary stays clean
    assert result["days"][0]["merged_complete"] == 3
    row, = ledger.daily_rows("USA", "MI")
    assert (row["state"], row["source"], row["date_solar"]) == ("merged", "manual", "2024-08-15")
    assert (row["complete_lakes"], row["partial_lakes"], row["waiting_lakes"], row["not_planned_scenes"]) == (3, 1, 2, 1)
    assert json.loads(row["details"])["missing"][0]["name"] == "S3B_gap"
    assert states(ledger)["2024-08-15"]["state"] == "merged"  # same fingerprint as the coordinator's


def test_failed_manual_merge_is_recorded_and_not_counted_as_merged(ws):
    ledger = Ledger(ws.db)
    a = scene("2024-08-15")
    enqueue(ws, ledger, [a])
    finish(ledger, a)
    result = _run_daily(ws, "2024-08-15", side_effect=RuntimeError("disk full"))
    assert not result["ok"]
    row, = ledger.daily_rows("USA", "MI")
    assert row["state"] == "failed" and "disk full" in row["error"]
    assert states(ledger)["2024-08-15"]["state"] == "failed"


def test_a_merge_row_left_open_by_a_dead_process_is_closed_by_the_next_merge(ws):
    ledger = Ledger(ws.db)
    a = scene("2024-08-15")
    enqueue(ws, ledger, [a])
    finish(ledger, a)
    dead = ledger.daily_start("USA", "MI", "2024-08-15", "auto", "x")
    _run_daily(ws, "2024-08-15")
    assert ledger.daily_row(dead)["state"] == "interrupted"
    assert [r["state"] for r in ledger.daily_rows("USA", "MI")] == ["interrupted", "merged"]


# ---------------------------------------------------------------------------
# The coordinator: one merge at a time, in its own process
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_merge(monkeypatch):
    real = subprocess.Popen

    def launch(command, **kw):
        if "cyanotrend.worker" in command:
            return real([sys.executable, str(FAKE_WORKER), *command[3:]], **kw)
        assert command[1:3] == ["-m", "cyanotrend"] and command[command.index("daily") + 1] == "--start"
        assert kw.get("start_new_session") is True  # Ctrl-C must not reach the merge
        return real([sys.executable, str(FAKE_DAILY), *command[3:]], **kw)

    with patch("cyanotrend.daily.auto.subprocess.Popen", side_effect=launch) as popen:
        yield popen


def drain(coordinator, **kw):
    for _ in range(400):
        coordinator.tick(**kw)
        if not coordinator.running():
            return
        time.sleep(0.05)
    raise AssertionError("merge did not finish")


def test_only_one_merge_runs_at_a_time_oldest_date_first(ws, fake_merge, monkeypatch):
    monkeypatch.setenv("FAKE_DAILY_SLEEP", "0.4")
    ledger = Ledger(ws.db)
    scenes = [scene("2024-08-14"), scene("2024-08-16")]  # 08-15 sees both of them, so three dates are ready
    enqueue(ws, ledger, scenes)
    finish(ledger, *scenes)
    said = []
    coordinator = auto.AutoDaily(ws, ledger, say=said.append)
    coordinator.start()
    coordinator.tick()
    assert coordinator.running() and fake_merge.call_count == 1
    coordinator.tick()  # still merging: nothing new starts
    coordinator.tick()
    assert fake_merge.call_count == 1
    drain(coordinator, allow_new=False)  # stopping: the running merge finishes, no new one starts
    assert not coordinator.running() and fake_merge.call_count == 1
    assert [r["date_solar"] for r in ledger.daily_rows("USA", "MI")] == ["2024-08-14"]
    drain(coordinator)
    coordinator.tick()
    drain(coordinator)
    assert (ws.output / "merges.log").read_text().splitlines() == [f"USA MI 2024-08-{d}" for d in (14, 15, 16)]
    assert [r["state"] for r in ledger.daily_rows("USA", "MI")] == ["merged"] * 3
    assert not coordinator.has_work()
    text = "\n".join(said)
    assert "merging USA/MI 2024-08-14" in text and "merged USA/MI 2024-08-14: 2 complete, 1 partial, 1 lake(s) waiting" in text
    assert "1 scene(s) this date needs are in no plan" in text
    assert "plan --start 2024-08-15 --end 2024-08-15 --country USA --state-id MI" in text  # the hint; nothing was queued
    assert len(ledger.rows()) == 2


def test_a_date_is_merged_again_when_the_scenes_change_and_not_otherwise(ws, fake_merge):
    ledger = Ledger(ws.db)
    first = scene("2024-08-15")
    one_day = {"start": "2024-08-15", "end": "2024-08-15"}
    enqueue(ws, ledger, [first], **one_day)
    finish(ledger, first)
    coordinator = auto.AutoDaily(ws, ledger, say=lambda text: None)
    coordinator.start()
    drain(coordinator)
    drain(coordinator)
    assert coordinator.has_work() is False  # merged; asking again changes nothing
    assert fake_merge.call_count == 1
    late = scene("2024-08-15", hour=12)  # a later plan adds a scene of the same date
    enqueue(ws, ledger, [late], plan_id="mi2", **one_day)
    assert coordinator.has_work() is False  # it is still queued: the date waits
    finish(ledger, late)
    coordinator.tick()
    assert coordinator.running()
    drain(coordinator)
    assert [r["state"] for r in ledger.daily_rows("USA", "MI")] == ["merged", "merged"]


def test_a_crashed_merge_is_recorded_and_not_retried_in_the_same_run(ws, fake_merge, monkeypatch):
    monkeypatch.setenv("FAKE_DAILY_MODE", "crash")
    ledger = Ledger(ws.db)
    a = scene("2024-08-15")
    enqueue(ws, ledger, [a], start="2024-08-15", end="2024-08-15")
    finish(ledger, a)
    said = []
    coordinator = auto.AutoDaily(ws, ledger, say=said.append)
    drain(coordinator)
    for _ in range(3):
        coordinator.tick()
    assert fake_merge.call_count == 1 and not coordinator.has_work()
    row, = ledger.daily_rows("USA", "MI")
    assert row["state"] == "failed" and "exit code 3" in row["error"] and "boom" in row["error"]
    assert "FAILED" in said[-1] and Path(row["log"]).name in said[-1]
    assert states(ledger)["2024-08-15"]["state"] == "failed"
    # A new `run` tries it once more.
    assert auto.AutoDaily(ws, ledger, say=lambda text: None).has_work()


def test_starting_run_closes_rows_of_merges_that_died_but_not_a_live_manual_merge(ws):
    ledger = Ledger(ws.db)
    dead = ledger.daily_start("USA", "MI", "2024-08-15", "auto", "x")
    auto.AutoDaily(ws, ledger).start()
    assert ledger.daily_row(dead)["state"] == "interrupted"
    live = ledger.daily_start("USA", "MI", "2024-08-16", "manual", "y")
    with file_lock(ws.output / "daily" / ".daily.lock"):  # a manual `daily` is merging
        auto.AutoDaily(ws, ledger).start()
    assert ledger.daily_row(live)["state"] == "merging"


def test_run_with_auto_daily_merges_each_date_after_its_scenes_finish_and_then_exits(ws, fake_merge, monkeypatch):
    ledger = Ledger(ws.db)
    scenes = [scene("2024-01-01"), scene("2024-01-01", hour=12), scene("2024-01-02")]
    enqueue(ws, ledger, scenes, start="2024-01-01", end="2024-01-02")
    monkeypatch.setenv("CDSE_USERNAME", "fixture")
    monkeypatch.setenv("CDSE_PASSWORD", "fixture")
    with patch("cyanotrend.preflight.check", return_value={"ok": True}), patch("cyanotrend.service.import_plans"):
        result = serve(ws, workers=2, once=True, poll_seconds=0.02, auto_daily=True)
    assert result["counts"] == {"done": 3}
    assert (ws.output / "merges.log").read_text().splitlines() == ["USA MI 2024-01-01", "USA MI 2024-01-02"]
    assert [r["state"] for r in ledger.daily_rows("USA", "MI")] == ["merged", "merged"]
    assert all(r["source"] == "auto" for r in ledger.daily_rows("USA", "MI"))


def test_run_without_auto_daily_never_merges(ws, fake_merge, monkeypatch):
    ledger = Ledger(ws.db)
    enqueue(ws, ledger, [scene("2024-01-01")], start="2024-01-01", end="2024-01-01")
    monkeypatch.setenv("CDSE_USERNAME", "fixture")
    monkeypatch.setenv("CDSE_PASSWORD", "fixture")
    with patch("cyanotrend.preflight.check", return_value={"ok": True}), patch("cyanotrend.service.import_plans"):
        assert serve(ws, workers=1, once=True, poll_seconds=0.02)["counts"] == {"done": 1}
    assert ledger.daily_rows() == [] and not (ws.output / "merges.log").exists()
    assert parser().parse_args(["run"]).auto_daily is False
    assert parser().parse_args(["run", "--auto-daily"]).auto_daily is True


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

def test_progress_reports_dates_waiting_ready_merged_and_lakes_still_waiting(ws, capsys):
    ledger = Ledger(ws.db)
    a, b, c, queued = scene("2024-08-14"), scene("2024-08-15"), scene("2024-08-16"), scene("2024-08-17")
    enqueue(ws, ledger, [a, b, c, queued])
    finish(ledger, a, b, c)
    # 08-14 merged earlier, with one lake still waiting for a scene that is in no plan.
    # 08-15 is ready; 08-16 waits for the queued scene of 08-17 (inside its window); the rest is merged.
    sig = states(ledger)["2024-08-14"]["signature"]
    run = ledger.daily_start("USA", "MI", "2024-08-14", "auto", sig)
    ledger.daily_finish(run, "merged", {"complete": 4, "partial": 0, "waiting": 1, "unavailable": 0, "missing": 1, "not_planned": 1},
                        {"missing": [{"name": "S3B_gap", "satellite": "S3B", "sensing_start": "20240814T101500",
                                      "ledger_status": "not planned"}]})
    assert main(["--output", str(ws.output), "--json", "progress"]) == 0
    report = json.loads(capsys.readouterr().out)
    row, = report["states"]
    assert row["state"] == "Michigan (USA)" and row["dates_in_plans"] == 3
    assert (row["merged"], row["ready_to_merge"], row["waiting_for_scenes"]) == (1, 1, 1)
    assert report["waiting_for_scenes"] == [{"state": "Michigan (USA)", "date": "2024-08-16", "scenes_not_finished": 1,
                                             "of_scenes": 3, "merged_before": ""}]
    assert report["ready_to_merge"][0]["date"] == "2024-08-15"
    waiting, = report["merged_but_lakes_waiting"]
    assert (waiting["date"], waiting["lakes_waiting"], waiting["scenes_in_no_plan"]) == ("2024-08-14", 1, 1)
    assert any("plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id MI" in h for h in report["add_missing_scenes_with"])
    assert report["merge_running"] == "no" and report["recent_merges"][0]["result"] == "merged"
    assert any("ready to merge" in line for line in report["attention"])
    assert main(["--output", str(ws.output), "progress"]) == 0  # the plain-text table form works too
    assert "waiting_for_scenes" in capsys.readouterr().out


def test_progress_shows_an_orphaned_merge_as_interrupted_and_rejects_a_mistyped_output(ws, tmp_path, capsys):
    ledger = Ledger(ws.db)
    enqueue(ws, ledger, [scene("2024-08-15")])
    ledger.daily_start("USA", "MI", "2024-08-15", "auto", "x")
    assert main(["--output", str(ws.output), "--json", "progress"]) == 0
    assert json.loads(capsys.readouterr().out)["recent_merges"][0]["result"] == "interrupted"
    assert main(["--output", str(tmp_path / "typo"), "progress"]) == 1
    assert "No cyanotrend workspace" in capsys.readouterr().out and not (tmp_path / "typo").exists()


def test_progress_on_an_empty_workspace(ws, capsys):
    Ledger(ws.db)
    assert main(["--output", str(ws.output), "--json", "progress"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["states"] == [] and report["attention"] == ["nothing needs attention"]


def test_the_command_the_coordinator_starts_is_a_valid_daily_command_and_the_run_id_stays_hidden(ws, fake_merge, capsys):
    ledger = Ledger(ws.db)
    a = scene("2024-08-15")
    enqueue(ws, ledger, [a], start="2024-08-15", end="2024-08-15")
    finish(ledger, a)
    coordinator = auto.AutoDaily(ws, ledger, say=lambda text: None)
    drain(coordinator)
    command = fake_merge.call_args.args[0]
    args = parser().parse_args(command[3:])  # everything after `python -m cyanotrend`
    assert (args.command, args.start, args.end, args.country, args.state_id) == \
        ("daily", "2024-08-15", "2024-08-15", "USA", "MI")
    assert args.run_id == ledger.daily_rows("USA", "MI")[0]["id"] and args.output == str(ws.output)
    with pytest.raises(SystemExit):
        parser().parse_args(["daily", "--help"])
    assert "--run-id" not in capsys.readouterr().out
    assert parser().parse_args(["daily", "--start", "2024-08-15", "--end", "2024-08-15"]).run_id is None
