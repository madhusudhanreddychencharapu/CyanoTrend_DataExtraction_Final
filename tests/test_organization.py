"""Retry batches and stable scene ownership without satellite services."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import uuid

import pytest

from cyanotrend.cli import dispatch, parser
from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger, SCHEMA
from cyanotrend.planning import create_plan


def scene(state="Michigan", **extra):
    return {"id": str(uuid.uuid4()), "start": "2024-08-14T12:00:00Z", "name": "S3A_TEST",
            "output_region": {"country": "USA", "state_name": state, "state_id": state}, **extra}


def enqueue(ledger, scenes, pid="p"):
    ledger.enqueue({"id": pid, "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": scenes}, ledger.path.parent / f"{pid}.json")


def test_retry_all_failed_preserves_attempts_logs_and_other_statuses(tmp_path):
    ledger = Ledger(tmp_path / "operations.sqlite3")
    failed, waiting, running, done = [scene() for _ in range(4)]
    enqueue(ledger, [failed])
    with patch("cyanotrend.ledger.time.time", return_value=1000):
        for number in range(1, 4):
            job = ledger.claim()
            assert job["attempts"] == number
            ledger.fail(failed["id"], f"failure {number}")
            ledger.update(failed["id"], next_attempt=0)
    log = tmp_path / f"{failed['id']}_attempt1.log"
    log.write_text("previous failure evidence")
    enqueue(ledger, [waiting, running, done], "others")
    ledger.update(running["id"], status="running")
    ledger.update(done["id"], status="done")
    result = dispatch(parser().parse_args(["--output", str(tmp_path), "retry-failed"]))
    assert result["requeued"] == 1 and result["scene_ids"] == [failed["id"]]
    assert log.read_text() == "previous failure evidence"
    record = ledger.scene(failed["id"])
    assert record["attempts"] == record["retry_start_attempt"] == 3
    assert json.loads(record["timings"][-1]["details"])["previous_error"] == "failure 3"
    assert ledger.scene(waiting["id"])["status"] == "queued"
    assert ledger.scene(running["id"])["status"] == "running"
    assert ledger.scene(done["id"])["status"] == "done"
    assert ledger.requeue_failed()["requeued"] == 0
    # Leave the unrelated queued item ineligible while checking the retry batch.
    ledger.update(waiting["id"], next_attempt=9999999999)
    with patch("cyanotrend.ledger.time.time", return_value=1000):
        for number in (4, 5, 6):
            assert ledger.claim()["attempts"] == number
            ledger.fail(failed["id"], f"failure {number}")
            record = ledger.scene(failed["id"])
            if number < 6:
                assert record["status"] == "retry"
                assert record["next_attempt"] == 1000 + (60 if number == 4 else 120)
            ledger.update(failed["id"], next_attempt=0)
        assert ledger.claim() is None
    assert ledger.scene(failed["id"])["status"] == "failed"


def test_requeue_is_atomic_across_simultaneous_commands(tmp_path):
    ledger = Ledger(tmp_path / "ops.db")
    s = scene()
    enqueue(ledger, [s])
    ledger.update(s["id"], status="failed")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ledger.requeue_failed(), range(4)))
    assert sum(r["requeued"] for r in results) == 1


def test_old_queue_gains_retry_budget_without_resetting_history(tmp_path):
    path = tmp_path / "ops.db"
    old_schema = SCHEMA.replace(",\n retry_start_attempt INTEGER NOT NULL DEFAULT 0", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(old_schema)
    ledger = Ledger(path)
    s = scene()
    enqueue(ledger, [s])
    assert ledger.claim()["retry_start_attempt"] == 0
    Ledger(path)  # Reopening is idempotent.
    assert ledger.scene(s["id"])["attempts"] == 1


def test_first_plan_keeps_scene_location_when_another_state_reuses_it(tmp_path):
    ws = Workspace.create(tmp_path / "out", tmp_path / "ref", tmp_path / "scratch", tmp_path / "ocssw")
    ledger = Ledger(ws.db)
    first = scene()
    enqueue(ledger, [first], "michigan")
    ledger.update(first["id"], status="done")
    second = {**first, "output_region": {"country": "USA", "state_name": "Wisconsin", "state_id": "WI"}}
    enqueue(ledger, [second], "wisconsin")
    saved = ledger.scene(first["id"])
    assert saved["status"] == "done"
    owner = json.loads(saved["scene_json"])
    assert ws.archive(owner).relative_to(ws.output).parts == (
        "world", "USA", "Michigan", "2024", "08", "14", f"{first['id']}.zip")
    assert ledger.summary()["counts"] == {"done": 1}
    assert ledger.summary()["plans"] == 2


def test_scene_path_uses_utc_day_and_rejects_missing_or_unsafe_state(tmp_path):
    ws = Workspace.create(tmp_path / "out")
    s = scene(start="2024-08-15T01:00:00+02:00")
    assert ws.archive(s).parent.name == "14"
    with pytest.raises(ValueError, match="state name"):
        ws.archive({**s, "output_region": {"country": "USA", "state_name": "../Michigan"}})
    with pytest.raises(ValueError, match="country"):
        ws.archive({k: v for k, v in s.items() if k != "output_region"})


def test_state_selection_is_required_before_discovery(tmp_path):
    ws = Workspace.create(tmp_path / "out")
    with pytest.raises(ValueError, match="--state-id"):
        create_plan(ws, "2024-08-14", "2024-08-14", country="USA")
    with pytest.raises(SystemExit):
        parser().parse_args(["plan", "--start", "2024-08-14", "--end", "2024-08-14", "--country", "USA"])
