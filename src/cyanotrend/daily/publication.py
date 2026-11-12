"""State folders and recoverable publication of daily files and the daily master.

All numerical rows and cells arrive already calculated. This module only groups
them for storage and replaces a date in the master under the daily writer lock.
"""
from __future__ import annotations

import csv
from datetime import date
import json
import os
from pathlib import Path
import shutil
import uuid

from ..common import atomic_json, digest, sync_dir


class DailyPublisher:
    """Caller holds .daily.lock. A journal can finish interrupted file replacements."""

    def __init__(self, workspace, columns):
        self.root = workspace.output / "daily"
        self.columns = list(columns)
        self.master = self.root / "master_merged_daily.csv"
        self.backup = self.root / "master_merged_daily_backup.csv"
        self.journal = self.root / ".publication.json"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.recover()
        if not self.master.exists():
            if self.backup.exists():
                raise ValueError("Daily master is missing while its backup exists")
            with self.master.open("x", newline="") as f:
                csv.writer(f).writerow(self.columns)
                f.flush()
                os.fsync(f.fileno())
            os.link(self.master, self.backup)
            sync_dir(self.root)
        with self.master.open(newline="") as f:
            if next(csv.reader(f), None) != self.columns:
                raise ValueError("Daily master schema mismatch")

    def stage(self):
        folder = self.root / ".pending" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        return folder

    def _path(self, relative):
        path = self.root / relative
        if Path(relative).is_absolute() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Daily publication path escapes the daily folder")
        return path

    def recover(self):
        if not self.journal.exists():
            return
        record = json.loads(self.journal.read_text())
        # Validate every recoverable file before changing any of them.
        for item in record["files"]:
            target, source = self._path(item["target"]), self._path(item["source"])
            if target.is_file() and digest(target) == item["sha256"]:
                continue
            if not source.is_file() or digest(source) != item["sha256"]:
                raise RuntimeError(f"Daily publication recovery needs intact staged file: {item['target']}")
        for item in record["files"]:
            target, source = self._path(item["target"]), self._path(item["source"])
            if target.is_file() and digest(target) == item["sha256"]:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            sync_dir(target.parent)
        for relative in record["remove"]:
            target = self._path(relative)
            target.unlink(missing_ok=True)
            if target.parent.exists():
                sync_dir(target.parent)
        # Remove the journal last. Replaying it preserves the same prior master.
        self.journal.unlink()
        sync_dir(self.root)
        shutil.rmtree(self._path(record["staging"]))

    def publish(self, day, rows, products, staging):
        """Commit all state products for one date plus its deduplicated master rows."""
        self.initialize()
        stamp = day.isoformat()
        keys = set()
        for row in rows:
            key = (str(row["date_utc"]), int(row["Hylak_id"]))
            if key[0] != stamp or key in keys:
                raise ValueError("Daily publication contains a wrong date or duplicate lake/date row")
            keys.add(key)
        new_master = staging / "master.next.csv"
        with self.master.open(newline="") as src, new_master.open("w", newline="") as dst:
            reader = csv.DictReader(src)
            if reader.fieldnames != self.columns:
                raise ValueError("Daily master schema mismatch")
            writer = csv.DictWriter(dst, fieldnames=self.columns)
            writer.writeheader()
            previous_keys = set()
            for row in reader:
                key = (row["date_utc"], int(row["Hylak_id"]))
                date.fromisoformat(key[0])
                if key in previous_keys:
                    raise ValueError("Duplicate lake/date key in existing daily master")
                previous_keys.add(key)
                if key[0] != stamp:
                    writer.writerow(row)
            writer.writerows(rows)
            dst.flush()
            os.fsync(dst.fileno())
        # Snapshot the previous master before any replacement. This also works
        # when a rerun produces identical bytes and when recovery runs twice.
        previous = staging / "master.previous.csv"
        os.link(self.master, previous)
        targets = {str(Path(target)) for target in products}
        remove = []
        pattern = f"*/*/{day.year:04d}/{day.month:02d}/{day.day:02d}/{stamp}_manifest.json"
        for old in self.root.glob(pattern):
            for suffix in ("lakes.csv", "grid.nc", "manifest.json"):
                relative = str(old.with_name(f"{stamp}_{suffix}").relative_to(self.root))
                if relative not in targets:
                    remove.append(relative)
        all_products = list(products.items()) + [
            (self.backup.name, previous), (self.master.name, new_master),
        ]
        files = []
        for relative, source in all_products:
            self._path(relative)
            with Path(source).open("rb") as f:
                os.fsync(f.fileno())
            files.append({"target": str(relative), "source": str(Path(source).relative_to(self.root)),
                          "sha256": digest(source)})
        sync_dir(staging)
        atomic_json(self.journal, {"date_utc": stamp, "files": files, "remove": remove,
                                   "staging": str(staging.relative_to(self.root))})
        self.recover()


def publish_day(workspace, day, rows, cells, scope, manifest, lake_scene_ids):
    """Partition finished results by the existing pour-point state assignment."""
    from .merge import CSV_COLUMNS, _write_csv, _write_daily_grid, day_folder

    groups = {}
    for lake in scope.itertuples(index=False):
        country = getattr(lake, "country_iso3", "")
        state = getattr(lake, "state_name", "")
        if getattr(lake, "admin_match_status", "matched") != "matched" or country != "USA":
            raise ValueError(
                f"Daily folder assignment is unresolved for Hylak_id={lake.Hylak_id} "
                f"(country={country!r}, state={state!r}). Boundary policy remains deferred in the project Word note."
            )
        relative = day_folder(workspace, day, country, state).relative_to(workspace.output / "daily")
        groups.setdefault(relative, {"country": country, "state_name": state, "ids": set()})["ids"].add(int(lake.Hylak_id))
    publisher = DailyPublisher(workspace, CSV_COLUMNS)
    publisher.initialize()
    staging = publisher.stage()
    products, folders = {}, []
    try:
        for relative, group in sorted(groups.items()):
            ids = group["ids"]
            state_rows = [r for r in rows if int(r["Hylak_id"]) in ids]
            state_cells = [c for c in cells if c[0] in ids]
            state_scenes = set().union(*(lake_scene_ids.get(i, set()) for i in ids))
            selected = lambda values: [v for v in values if int(v["Hylak_id"]) in ids]
            waiting = selected(manifest["waiting_lakes"])
            partial = selected(manifest["partial_lakes"])
            unavailable = selected(manifest["unavailable_lakes"])
            state_manifest = {
                **manifest,
                "output_region": {"country": group["country"], "state_name": group["state_name"],
                                  "assignment": "HydroLAKES pour point"},
                "date_counts": manifest["counts"],
                "counts": {
                    "lakes_touched_by_processed_scenes": len(ids),
                    "merged_complete": sum(r["day_status"] == "complete" for r in state_rows),
                    "merged_partial": len(partial), "waiting": len(waiting), "unavailable": len(unavailable),
                },
                "waiting_lakes": waiting, "partial_lakes": partial, "unavailable_lakes": unavailable,
                "acquisitions": [a for a in manifest["acquisitions"]
                                 if any(v["id"] in state_scenes for v in a["versions"])],
                "scene_grid_files": {k: v for k, v in manifest["scene_grid_files"].items() if k in state_scenes},
                "warnings_scope": "date-wide",
                "nasa_cyan_comparison": {
                    **manifest.get("nasa_cyan_comparison", {}),
                    "lake_results": selected(manifest.get("nasa_cyan_comparison", {}).get("lake_results", [])),
                    "coverage_status_counts_scope": "date-wide",
                },
            }
            folder = staging / relative
            folder.mkdir(parents=True)
            prefix = day.isoformat()
            _write_csv(folder / f"{prefix}_lakes.csv", state_rows)
            _write_daily_grid(folder / f"{prefix}_grid.nc", day, manifest["within_pass_method"], state_cells)
            atomic_json(folder / f"{prefix}_manifest.json", state_manifest)
            for path in folder.iterdir():
                products[str(relative / path.name)] = path
            folders.append(str(publisher.root / relative))
        publisher.publish(day, rows, products, staging)
    finally:
        # Once a journal exists, its staged files are required for recovery.
        if not publisher.journal.exists() and staging.exists():
            shutil.rmtree(staging)
    return folders
