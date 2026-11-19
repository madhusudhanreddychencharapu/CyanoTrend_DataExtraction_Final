"""Rebuild a day's lakes CSV columns from its <date>_science.nc and compare them with the CSV.

Only the science file's layers are used (Hylak_id, DN, CI, CI_cyano, NDCI, MPH, FAI, valid_water_mask,
cloud_excluded_mask, scenes_valid, the lake table and the stored detection limit). Nothing is recomputed from
scenes, so this works at any time, long after the scene files are gone. The check runs in blocks of raster
lines, so a large state does not need much memory.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from ..schema import NASA_DETECTION_COLUMN
from .features import INDICES, summary

BLOCK = 256
FILL = -9999.0
COUNTS = ["Lake_cells", "primary_retained_cells", "Cloud_excluded_any_scene_cells",
          "Cloud_excluded_with_valid_daily_CI_cells", "Cloud_excluded_without_valid_daily_CI_cells",
          "ci_input_valid_cells", "ci_candidate_cells", "CI_cyano_detectable_cells", "Non_detectable_cells",
          "No_valid_data_cells", "Cells_valid_in_two_or_more_scenes"]


def rebuild(science_path):
    """{Hylak_id: {csv column: value}} computed from the science file alone."""
    import netCDF4

    with netCDF4.Dataset(science_path) as ds:
        ds.set_auto_maskandscale(False)  # raw values: 255 is a DN, -9999 is a fill
        limit = float(ds.getncattr("CI_cyano_detection_limit"))
        ids = np.asarray(ds.variables["lake_Hylak_id"][:], dtype=np.int64)
        nasa = np.asarray(ds.variables[f"lake_{NASA_DETECTION_COLUMN}"][:], dtype=np.int64)
        status = np.asarray(ds.variables["lake_day_status"][:], dtype=np.int64)
        order = np.argsort(ids)
        sorted_ids = ids[order]
        n = ids.size
        counts = {name: np.zeros(n, dtype=np.int64) for name in COUNTS}
        values = {index: [[] for _ in range(n)] for index in INDICES}
        detected_values = [[] for _ in range(n)]
        height = ds.dimensions["y"].size
        for y0 in range(0, height, BLOCK):
            y1 = min(y0 + BLOCK, height)
            hylak = np.asarray(ds.variables["Hylak_id"][y0:y1, :])
            cell = hylak >= 0
            if not cell.any():
                continue
            lake = order[np.searchsorted(sorted_ids, hylak[cell])]
            dn = np.asarray(ds.variables["DN"][y0:y1, :])[cell]
            ci_cyano = np.asarray(ds.variables["CI_cyano"][y0:y1, :])[cell].astype(np.float64)
            ci = np.asarray(ds.variables["CI"][y0:y1, :])[cell]
            retained = np.asarray(ds.variables["valid_water_mask"][y0:y1, :])[cell] == 1
            cloudy = np.asarray(ds.variables["cloud_excluded_mask"][y0:y1, :])[cell] == 1
            scenes = np.asarray(ds.variables["scenes_valid"][y0:y1, :])[cell]
            valid = dn != 255
            detected = valid & (ci_cyano >= limit)
            flags = {
                "Lake_cells": np.ones(lake.size, dtype=bool), "primary_retained_cells": retained,
                "Cloud_excluded_any_scene_cells": cloudy,
                "Cloud_excluded_with_valid_daily_CI_cells": cloudy & valid,
                "Cloud_excluded_without_valid_daily_CI_cells": cloudy & ~valid,
                "ci_input_valid_cells": valid, "ci_candidate_cells": ci != np.float32(FILL),
                "CI_cyano_detectable_cells": detected, "Non_detectable_cells": valid & ~detected,
                "No_valid_data_cells": ~valid, "Cells_valid_in_two_or_more_scenes": (scenes >= 2) & (scenes != 255),
            }
            for name, flag in flags.items():
                counts[name] += np.bincount(lake, weights=flag, minlength=n).astype(np.int64)
            layers = {"CI": ci.astype(np.float64), "CI_cyano": ci_cyano}
            for index in ("NDCI", "MPH", "FAI"):
                layers[index] = np.asarray(ds.variables[index][y0:y1, :])[cell].astype(np.float64)
            by_lake = np.argsort(lake, kind="stable")
            cuts = np.flatnonzero(np.diff(lake[by_lake])) + 1
            for part in np.split(by_lake, cuts):
                if not part.size:
                    continue
                k = int(lake[part[0]])
                for index in INDICES:
                    v = layers[index][part]
                    values[index][k].append(v[v != FILL])
                detected_values[k].append(ci_cyano[part][detected[part]])
    result = {}
    for k in range(n):
        row = {name: int(counts[name][k]) for name in COUNTS}
        row["ci_candidate_cells"] = int(counts["ci_candidate_cells"][k])
        for index in INDICES:
            joined = np.concatenate(values[index][k]) if values[index][k] else np.zeros(0)
            for stat, v in summary(joined).items():
                row[f"{index}_{stat}"] = v
        det = np.concatenate(detected_values[k]) if detected_values[k] else np.zeros(0)
        row["CI_cyano_mean_detectable"] = float(det.mean()) if det.size else ""
        row[NASA_DETECTION_COLUMN] = "" if nasa[k] < 0 else int(nasa[k])
        row["day_status"] = "complete" if status[k] == 0 else "partial"
        result[int(ids[k])] = row
    return result


def _same(expected, found):
    if expected == "" or found == "":
        return expected == "" and found == ""
    return math.isclose(float(expected), float(found), rel_tol=1e-9, abs_tol=1e-12)


def reproduce(science_path, csv_path=None):
    """Compare the rebuilt columns with the lakes CSV beside the science file. Never writes anything."""
    science_path = Path(science_path)
    if not science_path.is_file():
        raise ValueError(f"Science file not found: {science_path}")
    if csv_path is None:
        if not science_path.name.endswith("_science.nc"):
            raise ValueError("Give the lakes CSV with --csv, or use a file named <date>_science.nc")
        csv_path = science_path.with_name(science_path.name[: -len("_science.nc")] + "_lakes.csv")
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise ValueError(f"Lakes CSV not found: {csv_path}")
    rebuilt = rebuild(science_path)
    with csv_path.open(newline="") as f:
        table = {int(r["Hylak_id"]): r for r in csv.DictReader(f)}
    mismatches, checked = [], 0
    for lake in sorted(set(rebuilt) | set(table)):
        if lake not in table:
            mismatches.append(f"Hylak_id {lake}: in the science file but not in the CSV")
            continue
        if lake not in rebuilt:
            mismatches.append(f"Hylak_id {lake}: in the CSV but not in the science file")
            continue
        for column, value in rebuilt[lake].items():
            if column == "day_status":
                same = table[lake][column] == value
            else:
                same = _same(value, table[lake][column])
            checked += 1
            if not same:
                mismatches.append(f"Hylak_id {lake} {column}: science file gives {value!r}, CSV has {table[lake][column]!r}")
    return {"ok": not mismatches, "science_file": str(science_path), "csv_file": str(csv_path),
            "lakes": len(rebuilt), "values_compared": checked, "mismatches": mismatches[:50],
            "mismatch_count": len(mismatches)}
