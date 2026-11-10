"""Day stage: merge processed scenes per UTC day and lake on the NASA CyAN grid.

For each UTC date:
1. Checklist. The catalogue lists every S3A/S3B OLCI EFR scene of the date.
   A lake's checklist is every scene whose footprint touches the lake.
   Several catalogue versions of one acquisition (NR/NT reprocessing) count
   as one item; the best processed version is used.
2. Tick. The ledger says which items are processed (or permanently failed).
3. Decide. All items processed -> merge, "complete". No item still to come
   but one or more permanently failed -> merge what exists, "partial".
   Any item still to come -> no row; the lake waits (listed in the manifest).
4. Merge. The lake's cells are the NASA grid cells whose centres lie inside
   the HydroLAKES polygon (the rule NASA counts use). Each cell keeps the
   highest CI_cyano among the scenes with a valid view of it; a cell is
   "no valid data" only when no scene had a valid view.
5. Write daily_lakes.csv, day_manifest.json and daily_grid.nc under
   <output>/daily/USA/<year>/<month>/<date>/. Each run rebuilds the date's
   files from the saved scene grid files; rows are replaced, never appended.

This module only reads processed results. It never processes or queues scenes,
so running it again with nothing new gives the same files.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import uuid

import numpy as np

from ..common import atomic_json, digest, file_lock, sync_dir, utcnow
from ..ledger import Ledger
from . import grid as nasa_grid

SCHEMA = "cyanotrend-daily-1"
DONE = {"done", "skipped_no_lakes"}
WAITING = {"queued", "running", "retry", "publishing"}
TIMELINESS_RANK = {"NT": 0, "ST": 1, "NR": 2}
PRODUCT_NAME = re.compile(
    r"^(?P<sat>S3[AB])_OL_1_EFR_+(?P<start>\d{8}T\d{6})_(?P<stop>\d{8}T\d{6})_"
    r"(?P<created>\d{8}T\d{6})_.*?_(?P<timeliness>NR|NT|ST)_(?P<baseline>\d{3})(?:\.SEN3)?$"
)
DN_SLOPE = 0.011714
DN_INTERCEPT = -4.1870866  # CIcyano = 10**(DN * 0.011714 - 4.1870866), CyAN V6
CSV_COLUMNS = [
    "date_utc", "Hylak_id", "Lake_name", "Lake_area_km2", "states", "state_ids",
    "latitude", "longitude", "day_status", "within_pass_method", "satellites",
    "scenes_used", "scene_ids", "scene_names", "failed_or_unavailable_scenes",
    "Lake_cells", "Non_detectable_cells", "CI_cyano_detectable_cells",
    "No_valid_data_cells", "Cells_valid_in_two_or_more_scenes",
    "CI_cyano_max", "CI_cyano_mean_detectable",
]


def detection_limit():
    from ..core.settings import CI_DETECTION_LIMIT
    return float(CI_DETECTION_LIMIT)


def dates(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("Start date must not exceed end date")
    while first <= last:
        yield first
        first += timedelta(days=1)


def day_folder(workspace, day):
    return workspace.output / "daily" / "USA" / f"{day.year:04d}" / f"{day.month:02d}" / day.isoformat()


def acquisition(scene):
    """(identity of the acquisition, preference among its catalogue versions)."""
    match = PRODUCT_NAME.match(str(scene.get("name", "")))
    if match:
        key = (match["sat"], match["start"], match["stop"])
        preference = (TIMELINESS_RANK[match["timeliness"]], -int(match["created"].replace("T", "")))
        return key, preference
    start = str(scene.get("start", ""))[:19]
    stop = str(scene.get("end", ""))[:19]
    return (str(scene.get("satellite", "")), start, stop), (9, 0)


def ci_to_dn(value, valid):
    """NASA V6 DN: 0 valid below detection, 1..253 detections, 255 no valid data."""
    dn = np.full(np.shape(value), 255, dtype=np.uint8)
    dn[valid] = 0
    detected = valid & (value >= detection_limit())
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.rint((np.log10(value[detected]) - DN_INTERCEPT) / DN_SLOPE)
    dn[detected] = np.clip(raw, 1, 253).astype(np.uint8)
    return dn


def merge_cells(lake_keys, contributions, method):
    """Per cell: highest valid CI_cyano over scenes; invalid only if no scene was valid."""
    if method not in nasa_grid.METHODS:
        raise ValueError(f"Unknown within-pass method: {method}")
    n = lake_keys.size
    best = np.full(n, -np.inf)
    seen = np.zeros(n, dtype=np.uint8)
    for data in contributions:
        keys = data["key"]
        if keys.size == 0 or n == 0:
            continue
        pos = np.minimum(np.searchsorted(keys, lake_keys), keys.size - 1)
        hit = keys[pos] == lake_keys
        values = np.where(hit, data[f"{method}_value"][pos], np.nan)
        ok = hit & data[f"{method}_valid"][pos] & np.isfinite(values)
        seen += ok.astype(np.uint8)
        best = np.where(ok & (values > best), values, best)
    valid = seen > 0
    return valid, np.where(valid, best, np.nan), seen


def conus_lakes(workspace, lake_region):
    """Lakes touching a US state whose cells fall on the CONUS grid, with all their states."""
    import geopandas as gpd
    from .. import references

    lakes = references.load_lakes(workspace, region=lake_region)
    admin = references.boundaries(workspace, "ADM1")
    usa = admin[admin.country_iso3 == "USA"][["shapeName", "shapeID", "geometry"]]
    hits = gpd.sjoin(lakes[["Hylak_id", "geometry"]], usa, how="inner", predicate="intersects")
    states = hits.groupby("Hylak_id").agg(
        states=("shapeName", lambda s: "; ".join(sorted(set(map(str, s))))),
        state_ids=("shapeID", lambda s: "; ".join(sorted(set(map(str, s))))),
    )
    lakes = lakes.merge(states, left_on="Hylak_id", right_index=True, how="inner")
    point = lakes.geometry.representative_point()
    x, y = nasa_grid.project(point.x.to_numpy(), point.y.to_numpy())
    row, col = nasa_grid.cell_of(x, y)
    return lakes[nasa_grid.inside_grid(row, col)].reset_index(drop=True)


class LakeCells:
    """Lake cell keys, cached on disk per reference dataset (they never change)."""

    def __init__(self, workspace, reference_id):
        self.folder = workspace.output / "daily" / ".cache" / "lake_cells_v1" / str(reference_id)[:16]
        self.memory = {}

    def get(self, hylak_id, geometry):
        hylak_id = int(hylak_id)
        if hylak_id in self.memory:
            return self.memory[hylak_id]
        path = self.folder / f"{hylak_id}.npy"
        if path.exists():
            keys = np.load(path)
        else:
            keys = nasa_grid.lake_cells(geometry)
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp.npy")
            np.save(tmp, keys)
            os.replace(tmp, path)
        self.memory[hylak_id] = keys
        return keys


def ensure_scene_grid(workspace, scene):
    """Path of the scene's NASA-grid file; builds it from the native folder if missing."""
    folder = workspace.archive(scene).parent
    natives = nasa_grid.native_dirs(folder, scene["id"])
    if not natives:
        return None, f"no native lake-pixel folder beside {workspace.archive(scene).name}"
    native = natives[-1]
    path = nasa_grid.grid_path(native)
    if path.exists():
        try:
            nasa_grid.read_scene_grid(path)
            return path, None
        except (ValueError, KeyError, OSError):
            pass  # outdated or damaged: rebuild below
    nasa_grid.write_scene_grid(native, scene, path)
    return path, None


def _write_csv(path, rows):
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_daily_grid(path, day, method, cells):
    import netCDF4

    keys = np.concatenate([c[1] for c in cells]) if cells else np.zeros(0, dtype=np.int64)
    lake = np.concatenate([np.full(c[1].size, c[0], dtype=np.int64) for c in cells]) if cells else np.zeros(0, dtype=np.int64)
    dn = np.concatenate([c[2] for c in cells]) if cells else np.zeros(0, dtype=np.uint8)
    value = np.concatenate([c[3] for c in cells]) if cells else np.zeros(0)
    seen = np.concatenate([c[4] for c in cells]) if cells else np.zeros(0, dtype=np.uint8)
    row, col = nasa_grid.key_cell(keys)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
            ds.setncatts({
                "Conventions": "CF-1.8",
                "title": "Daily S3A/S3B maximum CI_cyano on the NASA CyAN CONUS 300 m grid (merged lakes only)",
                "schema": SCHEMA, "date_utc": day.isoformat(), "within_pass_method": method,
                "grid_definition": json.dumps(nasa_grid.grid_definition()),
                "dn_rule": "NASA V6: 0 below detection, 1..253 CIcyano = 10**(DN*0.011714-4.1870866), 255 no valid data; 254 (land) not produced",
                "created_utc": utcnow(),
            })
            ds.createDimension("cell", int(keys.size))
            for name, dtype, values, long_name in [
                ("row", "i4", row, "NASA CyAN CONUS grid row"),
                ("col", "i4", col, "NASA CyAN CONUS grid column"),
                ("Hylak_id", "i8", lake, "HydroLAKES lake whose polygon contains the cell centre"),
                ("DN", "u1", dn, "NASA V6 CI_cyano DN of the daily maximum"),
                ("CI_cyano", "f4", value, "daily maximum CI_cyano (NaN: no valid data)"),
                ("scenes_valid", "u1", seen, "number of scenes with a valid view of the cell"),
            ]:
                # No default fill value: DN 255 is NASA's "no valid data" code, not a missing value.
                var = ds.createVariable(name, dtype, ("cell",), zlib=True, complevel=4, shuffle=True, fill_value=False)
                var.long_name = long_name
                var[:] = values
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _text(value):
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except TypeError:
        pass
    return str(value)


def merge_day(workspace, ledger, lakes, day, method, catalogue, rows_by_day, status_of,
              lake_cells, capped_plans):
    from ..core.catalogue import scene_geometry
    from shapely import union_all

    day_rows = rows_by_day.get(day, [])
    processed = [r for r in day_rows if r["status"] in DONE]
    if not processed:
        return {"date": day.isoformat(), "result": "no processed scenes for this date"}
    touched = set()
    for r in processed:
        geometry = scene_geometry(json.loads(r["scene_json"]))
        if geometry is not None:
            touched.update(int(i) for i in lakes.sindex.query(geometry, predicate="intersects"))
    if not touched:
        return {"date": day.isoformat(), "result": "processed scenes touch no CONUS lake"}
    scope = lakes.iloc[sorted(touched)].reset_index(drop=True)
    west, south, east, north = (float(v) for v in scope.total_bounds)
    bbox = (max(-180.0, west - 0.01), max(-90.0, south - 0.01), min(180.0, east + 0.01), min(90.0, north + 0.01))
    listed = catalogue(day.isoformat(), day.isoformat(), bbox=bbox)

    scenes = {}
    for scene in listed:
        if scene.get("satellite") in ("S3A", "S3B") and nasa_grid.utc_date(scene["start"]) == day:
            scenes[str(scene["id"])] = scene
    for r in day_rows:
        scenes.setdefault(r["id"], json.loads(r["scene_json"]))

    versions = {}
    for sid, scene in scenes.items():
        key, preference = acquisition(scene)
        versions.setdefault(key, []).append((preference, sid))
    acquisitions = {}
    for key, items in versions.items():
        items.sort()
        ids = [sid for _, sid in items]
        done = [sid for sid in ids if status_of.get(sid) in DONE]
        if done:
            state, used = "done", done[0]
        elif any(status_of.get(sid) in WAITING or status_of.get(sid) is None for sid in ids):
            state, used = "waiting", None
        else:
            state, used = "failed", None
        geometry = [g for g in (scene_geometry(scenes[sid]) for sid in ids) if g is not None]
        acquisitions[key] = {
            "status": state, "used": used, "ids": ids,
            "geometry": union_all(geometry) if geometry else None,
        }
    cover = [set() for _ in range(len(scope))]
    for key, info in acquisitions.items():
        if info["geometry"] is None:
            continue
        for i in scope.sindex.query(info["geometry"], predicate="intersects"):
            cover[int(i)].add(key)

    grids, grid_files, warnings = {}, {}, []

    def scene_grid(sid):
        if sid in grids:
            return grids[sid]
        row = next((r for r in processed if r["id"] == sid), None)
        scene = scenes[sid]
        if row is not None and (row["status"] == "skipped_no_lakes" or row["stage"] == "complete_no_native"):
            grids[sid] = ("empty", None)
            grid_files[sid] = {"status": "no lake pixels in scene"}
            return grids[sid]
        try:
            path, reason = ensure_scene_grid(workspace, scene)
        except Exception as exc:  # recorded; the lake becomes partial
            path, reason = None, f"{type(exc).__name__}: {exc}"
        if path is None:
            grids[sid] = ("missing", reason)
            grid_files[sid] = {"status": "unavailable", "reason": reason}
            warnings.append(f"Scene {scenes[sid].get('name', sid)}: grid unavailable ({reason})")
        else:
            grids[sid] = ("ok", nasa_grid.read_scene_grid(path))
            grid_files[sid] = {"file": path.name, "sha256": digest(path)}
        return grids[sid]

    names = {sid: scenes[sid].get("name", sid) for sid in scenes}
    rows, cells, waiting, unavailable, partial = [], [], [], [], []
    limit = detection_limit()
    for i, lake in enumerate(scope.itertuples(index=False)):
        keys = sorted(cover[i])
        if not keys:
            continue
        describe = {"Hylak_id": int(lake.Hylak_id), "Lake_name": _text(getattr(lake, "Lake_name", "")),
                    "states": lake.states}
        still = [names[acquisitions[k]["ids"][0]] for k in keys if acquisitions[k]["status"] == "waiting"]
        if still:
            waiting.append({**describe, "missing_scenes": still})
            continue
        used = [acquisitions[k]["used"] for k in keys if acquisitions[k]["status"] == "done"]
        failed = [names[acquisitions[k]["ids"][0]] for k in keys if acquisitions[k]["status"] == "failed"]
        contributions = []
        for sid in used:
            kind, data = scene_grid(sid)
            if kind == "ok":
                contributions.append(data)
            elif kind == "missing":
                failed.append(names[sid])
        if not used:
            unavailable.append({**describe, "failed_scenes": failed})
            continue
        status = "partial" if failed else "complete"
        if failed:
            partial.append({**describe, "failed_or_unavailable_scenes": failed})
        lake_keys = lake_cells.get(lake.Hylak_id, lake.geometry)
        valid, value, seen = merge_cells(lake_keys, contributions, method)
        detected = valid & (value >= limit)
        dn = ci_to_dn(value, valid)
        cells.append((int(lake.Hylak_id), lake_keys, dn, value.astype(np.float32), seen))
        satellites = sorted({str(scenes[s].get("satellite", "")) for s in used})
        rows.append({
            "date_utc": day.isoformat(),
            "Hylak_id": int(lake.Hylak_id),
            "Lake_name": _text(getattr(lake, "Lake_name", "")),
            "Lake_area_km2": _text(getattr(lake, "Lake_area", "")),
            "states": lake.states,
            "state_ids": lake.state_ids,
            "latitude": _text(getattr(lake, "latitude", "")),
            "longitude": _text(getattr(lake, "longitude", "")),
            "day_status": status,
            "within_pass_method": method,
            "satellites": ";".join(satellites),
            "scenes_used": len(used),
            "scene_ids": ";".join(sorted(used)),
            "scene_names": ";".join(sorted(names[s] for s in used)),
            "failed_or_unavailable_scenes": ";".join(sorted(failed)),
            "Lake_cells": int(lake_keys.size),
            "Non_detectable_cells": int((valid & ~detected).sum()),
            "CI_cyano_detectable_cells": int(detected.sum()),
            "No_valid_data_cells": int((~valid).sum()),
            "Cells_valid_in_two_or_more_scenes": int((seen >= 2).sum()),
            "CI_cyano_max": float(np.nanmax(value)) if valid.any() else "",
            "CI_cyano_mean_detectable": float(value[detected].mean()) if detected.any() else "",
        })
    rows.sort(key=lambda r: r["Hylak_id"])
    caps = sorted(p for p, ids in capped_plans.items() if ids & {r["id"] for r in day_rows})
    if caps:
        warnings.append("Plans " + ", ".join(caps) + " used --max-scenes; capped catalogue results can leave lakes waiting")

    folder = day_folder(workspace, day)
    folder.mkdir(parents=True, exist_ok=True)
    _write_csv(folder / "daily_lakes.csv", rows)
    _write_daily_grid(folder / "daily_grid.nc", day, method, cells)
    acquisition_list = []
    for key in sorted(acquisitions):
        info = acquisitions[key]
        acquisition_list.append({
            "satellite": key[0], "sensing_start": key[1], "sensing_stop": key[2],
            "status": info["status"], "used_scene_id": info["used"],
            "versions": [{"id": sid, "name": names[sid], "ledger_status": status_of.get(sid) or "not planned"}
                         for sid in info["ids"]],
        })
    counts = {
        "lakes_touched_by_processed_scenes": len(scope),
        "merged_complete": sum(r["day_status"] == "complete" for r in rows),
        "merged_partial": sum(r["day_status"] == "partial" for r in rows),
        "waiting": len(waiting),
        "unavailable": len(unavailable),
    }
    atomic_json(folder / "day_manifest.json", {
        "schema": SCHEMA,
        "date_utc": day.isoformat(),
        "created_utc": utcnow(),
        "within_pass_method": method,
        "rules": {
            "checklist": "every catalogue S3A/S3B OLCI EFR scene of the UTC date whose footprint touches the lake",
            "complete": "every checklist scene processed",
            "partial": "nothing still to come; one or more checklist scenes permanently failed or without a grid file",
            "waiting": "a checklist scene is not processed yet; no row is written",
            "lake_cells": "NASA grid cells whose centres lie inside the HydroLAKES polygon",
            "daily_value": "per cell, highest valid CI_cyano over the lake's scenes; no valid data only if no scene was valid",
            "detection": f"CI_cyano >= {limit:.6g} (NASA V6 DN 1)",
        },
        "grid": nasa_grid.grid_definition(),
        "catalogue": {"bbox": list(bbox), "products_listed": len(listed)},
        "counts": counts,
        "acquisitions": acquisition_list,
        "scene_grid_files": grid_files,
        "partial_lakes": partial,
        "waiting_lakes": waiting,
        "unavailable_lakes": unavailable,
        "warnings": warnings,
    })
    sync_dir(folder)
    return {"date": day.isoformat(), "processed_scenes": len(processed), **counts,
            "folder": str(folder), "warnings": len(warnings)}


def run_daily(workspace, start, end, method="area", catalogue=None, lakes=None):
    """Merge every date in [start, end]; returns one summary per date."""
    from .. import references

    if method not in nasa_grid.METHODS:
        raise ValueError(f"--method must be one of {', '.join(nasa_grid.METHODS)}")
    days = list(dates(start, end))
    if catalogue is None:
        from ..core.catalogue import search_olci_l1_catalog as catalogue
    ledger = Ledger(workspace.db)
    with ledger.connect() as c:
        meta = {r[0]: r[1] for r in c.execute("SELECT key, value FROM workspace_meta")}
        plans = [dict(r) for r in c.execute("SELECT id, metadata FROM plans")]
        membership = {}
        for r in c.execute("SELECT plan_id, scene_id FROM plan_scenes"):
            membership.setdefault(r[0], set()).add(r[1])
    capped = {p["id"]: membership.get(p["id"], set()) for p in plans
              if json.loads(p["metadata"] or "{}").get("max_scenes")}
    all_rows = ledger.rows()
    status_of = {r["id"]: r["status"] for r in all_rows}
    rows_by_day = {}
    wanted = set(days)
    for r in all_rows:
        day = nasa_grid.utc_date(json.loads(r["scene_json"])["start"])
        if day in wanted:
            rows_by_day.setdefault(day, []).append(r)
    results, ok = [], True
    if not rows_by_day:
        return {"ok": True, "days": [{"date": d.isoformat(), "result": "no scenes in the ledger"} for d in days]}
    with file_lock(workspace.output / "daily" / ".daily.lock"):
        if lakes is None:
            lakes = conus_lakes(workspace, meta.get("lake_region", "WORLD"))
        cache = LakeCells(workspace, references.manifest(workspace)["reference_id"])
        for day in days:
            try:
                results.append(merge_day(workspace, ledger, lakes, day, method, catalogue,
                                         rows_by_day, status_of, cache, capped))
            except Exception as exc:  # this date's files stay as they were
                from ..telemetry import redact
                ok = False
                results.append({"date": day.isoformat(), "error": redact(f"{type(exc).__name__}: {exc}")[:1000]})
    return {"ok": ok, "within_pass_method": method, "days": results}
