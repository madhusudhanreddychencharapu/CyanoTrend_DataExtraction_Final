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
   highest valid value for each index independently, after maximum footprint
   mapping within each scene. An index has no valid data only when no scene
   supplied a valid value for it.
5. Publish date-named lakes CSV, manifest JSON and grid NC under
   <output>/daily/<country>/<pour-point state>/<year>/<month>/<day>/.
   Replace this date in the daily master and preserve its previous version.

This module reads processed native/grid results and the original NASA daily
product. It never processes or queues scenes. Publication metadata timestamps
can change on reruns even when numerical inputs and outputs are unchanged.
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

from ..common import dated_region_folder, digest, file_lock, utcnow
from ..ledger import Ledger
from . import grid as nasa_grid
from .features import INDICES, FLAGS, summary as index_summary
from ..schema import NASA_DETECTION_COLUMN

SCHEMA = "cyanotrend-daily-2"
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
    "Lake_cells",
    "primary_retained_cells",
    "Cloud_excluded_any_scene_cells", "Cloud_excluded_with_valid_daily_CI_cells",
    "Cloud_excluded_without_valid_daily_CI_cells",
    "ci_input_valid_cells", "ci_candidate_cells",
    "CI_cyano_detectable_cells", "Non_detectable_cells", "No_valid_data_cells",
    "Cells_valid_in_two_or_more_scenes", NASA_DETECTION_COLUMN,
]
for _index in INDICES:
    CSV_COLUMNS.extend(f"{_index}_{stat}" for stat in ("valid_cells", "mean", "median", "p10", "p90", "max"))
    if _index == "CI_cyano":
        CSV_COLUMNS.append("CI_cyano_mean_detectable")


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


def day_folder(workspace, day, country, state):
    return dated_region_folder(workspace.output / "daily", country, state, day)


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


def merge_cells(lake_keys, contributions, method, index="CI_cyano"):
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
        if method == "max":
            values = np.where(hit, data["maximum"][index][pos], np.nan)
            ok = hit & np.isfinite(values)
        else:
            if index != "CI_cyano":
                raise ValueError("All-index daily statistics require maximum mapping")
            values = np.where(hit, data[f"{method}_value"][pos], np.nan)
            ok = hit & data[f"{method}_valid"][pos] & np.isfinite(values)
        seen += ok.astype(np.uint8)
        best = np.where(ok & (values > best), values, best)
    valid = seen > 0
    return valid, np.where(valid, best, np.nan), seen


def merge_flags(lake_keys, contributions):
    result = {name: np.zeros(lake_keys.size, dtype=bool) for name in FLAGS}
    for data in contributions:
        keys = data["key"]
        if not keys.size or not lake_keys.size:
            continue
        pos = np.minimum(np.searchsorted(keys, lake_keys), keys.size - 1)
        hit = keys[pos] == lake_keys
        for name in FLAGS:
            result[name] |= hit & data["flags"][name][pos]
    return result


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
    """Find or rebuild one scene's grid from its native files in the owning state's folder."""
    folder = workspace.archive(scene).parent
    files = nasa_grid.scene_native_files(folder, scene["id"])
    if not files:
        return None, f"no native lake-pixel files beside {workspace.archive(scene).name}"
    configs = {p.stem.rsplit("_", 2)[1] for p in files}
    if len(configs) != 1:
        raise ValueError(f"Multiple native processing configurations for scene {scene['id']}")
    path = nasa_grid.scene_grid_path(folder, scene["id"], configs.pop())
    if path.exists():
        try:
            data = nasa_grid.read_scene_grid(path)
            if data["attrs"].get("scene_id") != scene["id"]:
                raise ValueError("Scene grid identity mismatch")
            return path, None
        except (ValueError, KeyError, OSError):
            pass  # outdated or damaged: rebuild below
    nasa_grid.write_scene_grid(folder, scene, path, native_files=files)
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
            for index in INDICES:
                if index == "CI_cyano":
                    continue  # Already written with its existing name above.
                values = np.concatenate([c[5][index] for c in cells]) if cells else np.zeros(0)
                var = ds.createVariable(index, "f4", ("cell",), zlib=True, complevel=4, fill_value=False)
                var.long_name = f"daily maximum valid {index}; independent of the scene selected for other indices"
                var[:] = values
            for name in FLAGS:
                values = np.concatenate([c[6][name] for c in cells]) if cells else np.zeros(0, dtype=bool)
                var = ds.createVariable(f"{name}_any", "u1", ("cell",), zlib=True, complevel=4, fill_value=False)
                var.long_name = f"at least one overlapping observation in the day is {name}"
                var[:] = values.astype(np.uint8)
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
        # Catalogue records do not carry the original plan's file ownership.
        scene = json.loads(row["scene_json"]) if row is not None else scenes[sid]
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
        merged = {index: merge_cells(lake_keys, contributions, method, index) for index in INDICES}
        valid, value, seen = merged["CI_cyano"]
        daily_indices = {index: data[1] for index, data in merged.items()}
        daily_flags = merge_flags(lake_keys, contributions)
        cloudy = daily_flags["cloud_excluded"]
        detected = valid & (value >= limit)
        dn = ci_to_dn(value, valid)
        cells.append((int(lake.Hylak_id), lake_keys, dn, value.astype(np.float32), seen,
                      daily_indices, daily_flags))
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
            "primary_retained_cells": int(daily_flags["primary_retained"].sum()),
            "Cloud_excluded_any_scene_cells": int(cloudy.sum()),
            "Cloud_excluded_with_valid_daily_CI_cells": int((cloudy & valid).sum()),
            "Cloud_excluded_without_valid_daily_CI_cells": int((cloudy & ~valid).sum()),
            "ci_input_valid_cells": int(valid.sum()),
            "ci_candidate_cells": int(merged["CI"][0].sum()),
            "Non_detectable_cells": int((valid & ~detected).sum()),
            "CI_cyano_detectable_cells": int(detected.sum()),
            "No_valid_data_cells": int((~valid).sum()),
            "Cells_valid_in_two_or_more_scenes": int((seen >= 2).sum()),
            **{f"{index}_{stat}": v for index, values in daily_indices.items()
               for stat, v in index_summary(values).items()},
            "CI_cyano_mean_detectable": float(value[detected].mean()) if detected.any() else "",
        })
    rows.sort(key=lambda r: r["Hylak_id"])
    nasa_metadata = {}
    if rows:
        from ..nasa_cyan import measure_daily
        measurements, nasa_metadata = measure_daily(
            scope, day.isoformat() + "T00:00:00Z", workspace.references / "obpg_cyan",
            [r["Hylak_id"] for r in rows],
        )
        lookup = measurements.set_index("Hylak_id")[NASA_DETECTION_COLUMN]
        import pandas as pd
        for row in rows:
            value = lookup.loc[row["Hylak_id"]]
            row[NASA_DETECTION_COLUMN] = "" if pd.isna(value) else int(value)
    caps = sorted(p for p, ids in capped_plans.items() if ids & {r["id"] for r in day_rows})
    if caps:
        warnings.append("Plans " + ", ".join(caps) + " used --max-scenes; capped catalogue results can leave lakes waiting")

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
    manifest = {
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
            "daily_value": "independent maximum valid value per index within each scene and across all selected acquisitions of the UTC day",
            "statistics": "per lake, finite daily cell maxima; CI uses candidate values, CI_cyano includes valid non-detections, other indices use their existing valid masks",
            "cloud_counts": "any cloud_excluded_mask observation overlapping a cell, split by availability of valid daily CI; not proof that cloud alone caused missing data",
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
        "nasa_cyan_comparison": nasa_metadata,
    }
    from .publication import publish_day
    lake_scene_ids = {
        int(lake.Hylak_id): {sid for key in cover[i] for sid in acquisitions[key]["ids"]}
        for i, lake in enumerate(scope.itertuples(index=False))
    }
    folders = publish_day(workspace, day, rows, cells, scope, manifest, lake_scene_ids)
    return {"date": day.isoformat(), "processed_scenes": len(processed), **counts,
            "folders": folders, "warnings": len(warnings)}


def run_daily(workspace, start, end, method="max", catalogue=None, lakes=None):
    """Merge every date in [start, end]; returns one summary per date."""
    from .. import references

    if method != "max":
        raise ValueError("Daily all-index statistics use --method max")
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
    with file_lock(workspace.output / "daily" / ".daily.lock"):
        from .publication import DailyPublisher
        publisher = DailyPublisher(workspace, CSV_COLUMNS)
        publisher.recover()
        if not rows_by_day:
            return {"ok": True, "days": [{"date": d.isoformat(), "result": "no scenes in the ledger"} for d in days]}
        if lakes is None:
            lakes = conus_lakes(workspace, meta.get("lake_region", "WORLD"))
        cache = LakeCells(workspace, references.manifest(workspace)["reference_id"])
        for day in days:
            try:
                results.append(merge_day(workspace, ledger, lakes, day, method, catalogue,
                                         rows_by_day, status_of, cache, capped))
            except Exception as exc:
                from ..telemetry import redact
                ok = False
                results.append({"date": day.isoformat(), "error": redact(f"{type(exc).__name__}: {exc}")[:1000]})
                if publisher.journal.exists():
                    # Finish this interrupted publication on restart before starting another date.
                    break
    return {"ok": ok, "within_pass_method": method, "days": results}
