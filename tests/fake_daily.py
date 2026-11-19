"""Subprocess fixture: stands in for `cyanotrend daily` when testing `run --auto-daily`."""
import os
from pathlib import Path
import sys
import time

from cyanotrend.ledger import Ledger

args = sys.argv[1:]


def option(name):
    return args[args.index(name) + 1]


out, run_id = Path(option("--output")), int(option("--run-id"))
time.sleep(float(os.environ.get("FAKE_DAILY_SLEEP", "0")))
with (out / "merges.log").open("a") as f:  # order and number of merges
    f.write(f"{option('--country')} {option('--state-id')} {option('--start')}\n")
if os.environ.get("FAKE_DAILY_MODE") == "crash":
    print("boom: fixture crash", file=sys.stderr)
    sys.exit(3)
missing = [{"name": "S3B_gap", "satellite": "S3B", "sensing_start": "20240815T101500", "ledger_status": "not planned"}]
Ledger(out / "operations.sqlite3").daily_finish(
    run_id, "merged",
    {"complete": 2, "partial": 1, "waiting": 1, "unavailable": 0, "missing": 1, "not_planned": 1},
    {"missing": missing})
