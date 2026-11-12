"""Date replacement and interrupted publication preserve the prior daily master."""
import csv
from datetime import date
import os
from unittest.mock import patch

import pytest

from cyanotrend.common import Workspace
from cyanotrend.daily.merge import CSV_COLUMNS, _write_csv
from cyanotrend.daily.publication import DailyPublisher


def row(day, lid, value=1):
    return {"date_utc": day.isoformat(), "Hylak_id": lid,
            "CI_cyano_detectable_cells": value, "day_status": "complete"}


def read(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def publish(publisher, day, rows, state="Michigan"):
    staging = publisher.stage()
    relative = f"USA/{state}/{day.year:04d}/{day.month:02d}/{day.day:02d}"
    files = {}
    for suffix in ("lakes.csv", "grid.nc", "manifest.json"):
        name = f"{day.isoformat()}_{suffix}"
        path = staging / name
        if suffix == "lakes.csv":
            _write_csv(path, rows)
        else:
            path.write_text(repr(rows))  # Publication fixtures; no numerical processing here.
        files[f"{relative}/{name}"] = path
    publisher.publish(day, rows, files, staging)
    return publisher.root / relative


@pytest.fixture
def publisher(tmp_path):
    ws = Workspace.create(tmp_path / "out")
    p = DailyPublisher(ws, CSV_COLUMNS)
    p.initialize()
    return p


def test_daily_master_replaces_date_preserves_other_dates_and_rotates_backup(publisher):
    a, b = date(2024, 8, 14), date(2024, 8, 15)
    publish(publisher, a, [row(a, 1)])
    assert read(publisher.backup) == []
    publish(publisher, b, [row(b, 2)])
    before = publisher.master.read_bytes()
    publish(publisher, a, [row(a, 1, 8), row(a, 3, 2)])
    result = read(publisher.master)
    assert len(result) == 3
    assert {(r["date_utc"], r["Hylak_id"]) for r in result} == {
        (a.isoformat(), "1"), (a.isoformat(), "3"), (b.isoformat(), "2")}
    assert publisher.backup.read_bytes() == before
    before = publisher.master.read_bytes()
    publish(publisher, a, [row(a, 1, 8), row(a, 3, 2)])
    assert publisher.master.read_bytes() == publisher.backup.read_bytes() == before
    publish(publisher, a, [])
    assert [(r["date_utc"], r["Hylak_id"]) for r in read(publisher.master)] == [(b.isoformat(), "2")]


@pytest.mark.parametrize("when", ["grid", "before_master", "after_master"])
def test_crash_recovery_finishes_date_without_losing_prior_backup(publisher, when):
    day = date(2024, 8, 14)
    folder = publish(publisher, day, [row(day, 1)])
    previous_master = publisher.master.read_bytes()
    original = os.replace

    def crash(source, target):
        target = str(target)
        if when == "grid" and target == str(folder / f"{day}_grid.nc"):
            raise OSError("interrupted state grid publication")
        if target == str(publisher.master):
            if when == "before_master":
                raise OSError("interrupted before master replacement")
            if when == "after_master":
                original(source, target)
                raise OSError("interrupted after master replacement")
        return original(source, target)

    with patch("cyanotrend.daily.publication.os.replace", side_effect=crash):
        with pytest.raises(OSError, match="interrupted"):
            publish(publisher, day, [row(day, 1, 9)])
    assert publisher.journal.exists()
    publisher.recover()
    assert publisher.backup.read_bytes() == previous_master
    assert read(publisher.master)[0]["CI_cyano_detectable_cells"] == "9"
    assert read(folder / f"{day}_lakes.csv") == read(publisher.master)
    assert not publisher.journal.exists()
    publisher.recover()
    assert publisher.backup.read_bytes() == previous_master


def test_legacy_daily_files_are_untouched_and_stale_new_state_files_are_removed(publisher):
    day = date(2024, 8, 14)
    legacy = publisher.root / "USA/2024/08/2024-08-14/daily_lakes.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy output must stay intact")
    first = publish(publisher, day, [row(day, 1)])
    publish(publisher, day, [row(day, 2)], state="Wisconsin")
    assert legacy.read_text() == "legacy output must stay intact"
    assert not list(first.glob(f"{day}_*"))
    assert [r["Hylak_id"] for r in read(publisher.master)] == ["2"]


def test_duplicate_or_wrong_date_rows_cannot_replace_master(publisher):
    day = date(2024, 8, 14)
    publish(publisher, day, [row(day, 1)])
    previous = publisher.master.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        publish(publisher, day, [row(day, 1), row(day, 1)])
    with pytest.raises(ValueError, match="wrong date"):
        publish(publisher, day, [row(date(2024, 8, 15), 1)])
    assert publisher.master.read_bytes() == previous
    assert not publisher.journal.exists()
