"""Lake finder and NASA CyAN vs our CI_cyano scatter plot for one state, one date and chosen lakes.

NASA CyAN is used only to check our results. One dot is one original NASA daily cell (about 300 m) whose
centre lies inside the lake. x is the NASA CI_cyano of that cell (DN 1..253 converted back with NASA's
formula, DN 0 = below detection = 0). y is our daily CI_cyano of the ISIN bin that contains the cell
centre (below the detection limit = 0). Cells that are land or have no data in NASA, or no valid value
in ours, are left out; the counts of every kind of leftover are reported.
"""
from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path

import numpy as np

from ..common import dated_region_folder
from ..schema import NASA_DETECTION_COLUMN
from . import grid as isin
from .merge import DN_INTERCEPT, DN_SLOPE, detection_limit

LAKE_COLUMNS = ("Hylak_id", "Lake_name", "Lake_area_km2", "states", "day_status", "scenes_used", "Lake_cells",
                "ci_input_valid_cells", "CI_cyano_detectable_cells")


# ---------------------------------------------------------------------------
# Where the daily result of a state lives
# ---------------------------------------------------------------------------

def _state_name(workspace, country, state_id):
    """Folder name of a state: from the saved plans when the state was planned, else from the boundaries."""
    from ..ledger import Ledger

    for plan in Ledger(workspace.db).plan_rows():
        selection = json.loads(plan["metadata"] or "{}")
        if (str(selection.get("country", "")).upper() == country and str(selection.get("state_id", "")) == state_id
                and selection.get("state_name")):
            return selection["state_name"]
    from .. import references

    admin = references.boundaries(workspace, "ADM1")
    chosen = admin[(admin.country_iso3 == country) & (admin.shapeID.astype(str) == state_id)]
    if chosen.empty:
        raise ValueError("Country/state not found; inspect `cyanotrend regions` first")
    return str(chosen.shapeName.dropna().astype(str).iloc[0])


def _day_files(workspace, country, state_id, day):
    country = str(country).upper()
    state_id = str(state_id)
    state = _state_name(workspace, country, state_id)
    folder = dated_region_folder(workspace.output / "daily", country, state, day)
    files = {"state": state, "folder": folder, "lakes": folder / f"{day.isoformat()}_lakes.csv",
             "grid": folder / f"{day.isoformat()}_grid.nc"}
    if not files["lakes"].is_file() or not files["grid"].is_file():
        raise ValueError(
            f"No daily result for {state} on {day.isoformat()}. Run it first: cyanotrend --output "
            f"\"{workspace.output}\" daily --start {day} --end {day} --country {country} --state-id {state_id}"
        )
    return files


def _rows(path):
    with Path(path).open(newline="") as f:
        return list(csv.DictReader(f))


def _area(row):
    try:
        return float(row.get("Lake_area_km2") or 0.0)
    except ValueError:
        return 0.0


def _label(row):
    return (row.get("Lake_name") or "").strip() or f"(unnamed, Hylak_id {row['Hylak_id']})"


# ---------------------------------------------------------------------------
# Lake finder
# ---------------------------------------------------------------------------

def find_lakes(workspace, country, state_id, day, name=None, limit=30):
    """Lakes filed under the state with a merged daily row AND a NASA CyAN value on the date, biggest first."""
    day = date.fromisoformat(str(day))
    files = _day_files(workspace, country, state_id, day)
    rows = _rows(files["lakes"])
    comparable = [r for r in rows if (r.get(NASA_DETECTION_COLUMN) or "") != ""]
    needle = (name or "").strip().lower()
    matching = [r for r in comparable if needle in (r.get("Lake_name") or "").lower()] if needle else comparable
    matching.sort(key=lambda r: -_area(r))
    listed = [{**{k: (_label(r) if k == "Lake_name" else r.get(k, "")) for k in LAKE_COLUMNS},
               "NASA_detectable_cells": r[NASA_DETECTION_COLUMN]} for r in matching[:limit]]
    notes = []
    if not comparable:
        notes.append("None of this state's lakes has a NASA CyAN value for this date. NASA's daily product covers the "
                     "US by UTC date; a lake is compared only when all its passes are on that same UTC date.")
    elif not matching:
        notes.append(f"No NASA-comparable lake has a name containing {name!r}. Lakes without a NASA value are not listed.")
    if len(matching) > len(listed):
        notes.append(f"Showing the {len(listed)} biggest of {len(matching)}; use --name or --limit.")
    if listed:
        notes.append(f"Plot with: cyanotrend --output \"{workspace.output}\" scatter --country {str(country).upper()} "
                     f"--state-id {state_id} --date {day} --lake <Hylak_id or name>  (repeat --lake for several lakes)")
    return {"state": files["state"], "date": day.isoformat(), "lakes_with_daily_row": len(rows),
            "lakes_with_nasa_value": len(comparable), "lakes_matching": len(matching),
            "lakes": listed, "notes": notes}


# ---------------------------------------------------------------------------
# NASA cells of a lake: original cells whose centres are strictly inside the polygon
# ---------------------------------------------------------------------------

def _collect(geometry, paths):
    """(lon, lat, DN) of the unique NASA cells with centres inside the lake. Same rule as nasa_cyan.count_lake_cells."""
    from affine import Affine
    from pyproj import CRS, Transformer
    import rasterio
    from rasterio.windows import Window, from_bounds
    from shapely import contains_xy, union_all
    from shapely.geometry import box

    from ..nasa_cyan import CyanUnavailable, projected_lake, raster_info

    paths = list(dict.fromkeys(Path(p) for p in paths))
    infos = [raster_info(p) for p in paths]
    crs = CRS.from_wkt(infos[0]["crs"])
    anchor = Affine(*infos[0]["transform"])
    offsets = []
    for info in infos:
        t = Affine(*info["transform"])
        if CRS.from_wkt(info["crs"]) != crs or not np.allclose([t.a, t.e], [anchor.a, anchor.e], rtol=0, atol=1e-8):
            raise CyanUnavailable("incompatible_grid", "NASA tiles have incompatible CRS or pixel size")
        col, row = (~anchor) * (t.c, t.f)
        if not np.allclose([col, row], np.rint([col, row]), rtol=0, atol=1e-6):
            raise CyanUnavailable("incompatible_grid", "NASA tiles are not on the same original pixel grid")
        offsets.append((int(round(row)), int(round(col))))
    lake = projected_lake(geometry, crs)
    if not union_all([box(*i["bounds"]) for i in infos]).covers(lake):
        raise CyanUnavailable("incomplete_coverage", "Available NASA tiles do not cover the whole lake")
    to_geo = Transformer.from_crs(crs, 4326, always_xy=True)
    parts = []
    for path, info, (row_offset, col_offset) in zip(paths, infos, offsets):
        intersection = lake.intersection(box(*info["bounds"]))
        if intersection.is_empty:
            continue
        with rasterio.open(path) as source:
            w = from_bounds(*intersection.bounds, transform=source.transform)
            c0 = max(0, int(np.floor(w.col_off)) - 1)
            r0 = max(0, int(np.floor(w.row_off)) - 1)
            c1 = min(source.width, int(np.ceil(w.col_off + w.width)) + 1)
            r1 = min(source.height, int(np.ceil(w.row_off + w.height)) + 1)
            for start in range(r0, r1, 256):
                end = min(start + 256, r1)
                if c1 <= c0:
                    continue
                raw = source.read(1, window=Window(c0, start, c1 - c0, end - start), masked=False)
                rows, cols = np.indices(raw.shape)
                rows += start
                cols += c0
                lon, lat = to_geo.transform(source.transform.c + (cols + 0.5) * source.transform.a,
                                            source.transform.f + (rows + 0.5) * source.transform.e)
                inside = contains_xy(geometry, lon, lat)
                if inside.any():
                    parts.append(np.column_stack((rows[inside] + row_offset, cols[inside] + col_offset,
                                                  lon[inside], lat[inside], raw[inside])))
    if not parts:
        return np.zeros(0), np.zeros(0), np.zeros(0, dtype=np.uint8)
    cells = np.concatenate(parts)
    cells = cells[np.lexsort((cells[:, 1], cells[:, 0]))]
    same = np.all(cells[1:, :2] == cells[:-1, :2], axis=1)
    if np.any(same & (cells[1:, 4] != cells[:-1, 4])):
        raise CyanUnavailable("overlap_conflict", "Overlapping NASA cells have different DN values")
    cells = cells[np.r_[True, ~same]]
    return cells[:, 2], cells[:, 3], cells[:, 4].astype(np.uint8)


def _cached_records(cache, day):
    """Tiles already downloaded for the date, from their sidecar files (no network needed)."""
    records = []
    for sidecar in sorted(Path(cache).glob(f"L{day.strftime('%Y%j')}.*.tif.json")):
        data = json.loads(sidecar.read_text())
        if all(k in data for k in ("filename", "region", "tile", "selection_bounds")) and (Path(cache) / data["filename"]).is_file():
            records.append({k: data[k] for k in ("filename", "region", "tile", "selection_bounds", "url") if k in data})
    return records


def _cells_from_records(geometry, records, cache):
    from shapely.geometry import box

    from ..nasa_cyan import CyanUnavailable, fetch_tile, lake_sources, projected_lake, raster_info
    from ..telemetry import redact

    options = lake_sources(geometry, records)
    if not options:
        raise CyanUnavailable("unavailable", "No daily NASA tile covers this lake")
    failures = []
    for _, candidates in options:
        paths = []
        for tile in (r for r in candidates if r["tile"] != "ALL"):
            try:
                path = fetch_tile(tile, cache)
                paths.append(path)
                info = raster_info(path)
                if box(*info["bounds"]).covers(projected_lake(geometry, info["crs"])):
                    paths = [path]  # one tile that covers the whole lake is enough
                    break
            except Exception as exc:
                failures.append(redact(f"{tile['filename']}: {type(exc).__name__}: {exc}")[:300])
        groups = [lambda p=paths: p] + [(lambda t=t: [fetch_tile(t, cache)]) for t in candidates if t["tile"] == "ALL"]
        for make in groups:
            try:
                group = make()
                if group:
                    return _collect(geometry, group)
            except Exception as exc:
                failures.append(redact(f"{type(exc).__name__}: {exc}")[:300])
    raise CyanUnavailable("incomplete_coverage", "; ".join(failures) or "Whole-lake NASA coverage unavailable")


def nasa_cells(geometry, day, cache):
    """NASA daily cells of one lake: tiles already in the cache first, then the public NASA catalogue."""
    from ..nasa_cyan import discover_tiles
    from ..telemetry import redact

    reasons = []
    for source in ("cache", "NASA catalogue"):
        try:
            records = _cached_records(cache, day) if source == "cache" else discover_tiles(day)
            if not records:
                raise ValueError("no tiles")
            return _cells_from_records(geometry, records, cache)
        except Exception as exc:
            reasons.append(f"{source}: {redact(str(exc))[:300]}")
    raise ValueError(f"No NASA CyAN cells for this lake on {day.isoformat()} ({'; '.join(reasons)})")


def _polygons(workspace, ids):
    """Original HydroLAKES polygons of the chosen lakes only."""
    import zipfile

    import geopandas as gpd

    from .. import references

    archive = workspace.references / references.manifest(workspace)["hydrolakes"]["path"]
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if len(names) != 1:
        raise ValueError("Expected one HydroLAKES polygon shapefile")
    lakes = gpd.read_file(f"/vsizip/{archive}/{names[0]}", engine="pyogrio",
                          where=f"Hylak_id IN ({','.join(str(int(i)) for i in ids)})").to_crs(4326)
    lakes.geometry = lakes.geometry.make_valid()
    found = {int(i): g for i, g in zip(lakes.Hylak_id, lakes.geometry)}
    if set(found) != {int(i) for i in ids}:
        raise ValueError(f"HydroLAKES polygon not found for Hylak_id {sorted(set(map(int, ids)) - set(found))}")
    return found


# ---------------------------------------------------------------------------
# Pairing, statistics, plot
# ---------------------------------------------------------------------------

def _pick(rows, spec):
    text = str(spec).strip()
    if text.isdigit():
        hit = [r for r in rows if int(r["Hylak_id"]) == int(text)]
        if not hit:
            raise ValueError(f"Hylak_id {text} has no daily row for this state and date; list lakes with `cyanotrend lakes`")
        return hit[0]
    lowered = text.lower()
    found = [r for r in rows if (r.get("Lake_name") or "").strip().lower() == lowered] or \
            [r for r in rows if lowered in (r.get("Lake_name") or "").lower()]
    if not found:
        raise ValueError(f"No lake named like {text!r} in this state's daily result; find names with `cyanotrend lakes --name ...`")
    if len(found) > 1:
        options = "; ".join(f"{r['Hylak_id']} {_label(r)} ({_area(r):g} km2)" for r in sorted(found, key=lambda r: -_area(r))[:10])
        raise ValueError(f"{text!r} matches several lakes: {options}. Use the Hylak_id")
    return found[0]


def _our_values(grid_path, lake_id):
    import netCDF4

    with netCDF4.Dataset(grid_path) as ds:
        ds.set_auto_mask(False)
        mine = np.asarray(ds["Hylak_id"][:]) == lake_id
        keys = np.asarray(ds["bin"][:])[mine]
        values = np.asarray(ds["CI_cyano"][:], dtype=float)[mine]
    order = np.argsort(keys)
    return keys[order], values[order]


def pair_cells(lon, lat, dn, keys, values):
    """x = NASA CI_cyano, y = our CI_cyano at the same place; plus the count of every kind of leftover."""
    dn = np.asarray(dn, dtype=np.int64)
    nasa_valid = dn <= 253
    nasa_ci = np.where(dn >= 1, 10.0 ** (dn * DN_SLOPE + DN_INTERCEPT), 0.0)
    cell_bin = isin.bin_number(lon, lat)
    if keys.size:
        pos = np.minimum(np.searchsorted(keys, cell_bin), keys.size - 1)
        has_bin = (cell_bin > 0) & (keys[pos] == cell_bin)
        ours = np.where(has_bin, values[pos], np.nan)
    else:
        pos = np.zeros(cell_bin.size, dtype=np.int64)
        has_bin = np.zeros(cell_bin.size, dtype=bool)
        ours = np.full(cell_bin.size, np.nan)
    ours_valid = has_bin & np.isfinite(ours)
    keep = nasa_valid & ours_valid
    detected = ours_valid & (ours >= detection_limit())
    our_ci = np.where(detected, ours, 0.0)
    counts = {
        "nasa_cells_in_lake": int(dn.size),
        "left_out_nasa_land_or_no_data": int((~nasa_valid).sum()),
        "left_out_no_bin_of_ours_at_cell_centre": int((nasa_valid & ~has_bin).sum()),
        "left_out_our_bin_without_valid_data": int((nasa_valid & has_bin & ~ours_valid).sum()),
        "cells_plotted": int(keep.sum()),
    }
    return keep, nasa_ci, our_ci, cell_bin, detected, counts


def _r(x, y):
    if x.size < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def _fit(x, y):
    if x.size < 2 or np.std(x) == 0:
        return float("nan"), float("nan")
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def _plot(path, series, state, day, overall):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.8, 6.8))
    top = 0.0
    total = sum(item["x"].size for item in series)
    size, alpha = (24, 0.6) if total < 300 else (10, 0.45) if total < 3000 else (3, 0.35)
    for k, item in enumerate(series):
        colour = plt.get_cmap("tab10")(k % 10)
        ax.scatter(item["x"], item["y"], s=size, alpha=alpha, color=colour, linewidths=0, rasterized=True, clip_on=False,
                   label=f"{item['label']} (n={item['x'].size:,}, R={item['r']:.2f})")
        if item["x"].size:
            top = max(top, float(item["x"].max()), float(item["y"].max()))
    top = top * 1.05 if top > 0 else 1e-4
    margin = top * 0.02  # keep dots at exactly 0 (non-detects) fully visible instead of cut by the axis
    ax.set_xlim(-margin, top)
    ax.set_ylim(-margin, top)
    ax.set_aspect("equal", adjustable="box")
    if np.isfinite(overall["slope"]):
        line = np.array([-margin, top])
        ax.plot(line, overall["slope"] * line + overall["intercept"], color="black", linestyle="--", linewidth=1.2)
        ax.text(0.03, 0.97, f"R = {overall['r']:.3f}\ny = {overall['slope']:.3f}x {overall['intercept']:+.2e}\n"
                f"n = {overall['cells']:,}", transform=ax.transAxes, va="top", ha="left",
                bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85, "edgecolor": "0.7"})
    ax.set_xlabel("NASA CyAN CI_cyano (daily, 300 m cell)")
    ax.set_ylabel("Our CI_cyano (daily, ISIN bin at the cell centre)")
    ax.set_title(f"{state}, {day.isoformat()}")
    ax.legend(loc="lower right", fontsize=8, markerscale=4 if size < 10 else 1.2, framealpha=0.85)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def scatter(workspace, country, state_id, day, lakes, out_dir=None, polygons=None, cache=None):
    """Scatter of NASA CyAN CI_cyano (x) against our CI_cyano (y), one NASA cell per dot, for chosen lakes.

    lakes: names or Hylak_ids. polygons / cache can be supplied (tests); otherwise HydroLAKES and the
    workspace's NASA tile cache are used.
    """
    import pandas as pd

    day = date.fromisoformat(str(day))
    if not lakes:
        raise ValueError("Give at least one --lake")
    files = _day_files(workspace, country, state_id, day)
    rows = _rows(files["lakes"])
    chosen = []
    for spec in lakes:
        row = _pick(rows, spec)
        if (row.get(NASA_DETECTION_COLUMN) or "") == "":
            raise ValueError(f"{_label(row)} (Hylak_id {row['Hylak_id']}) has no NASA CyAN value for {day}: its passes are "
                             "on another UTC day or NASA does not cover the whole lake. List comparable lakes with `cyanotrend lakes`")
        if all(int(c["Hylak_id"]) != int(row["Hylak_id"]) for c in chosen):
            chosen.append(row)
    ids = [int(r["Hylak_id"]) for r in chosen]
    polygons = polygons if polygons is not None else _polygons(workspace, ids)
    cache = Path(cache) if cache is not None else workspace.references / "obpg_cyan"
    series, tables, per_lake = [], [], []
    for row in chosen:
        lake_id = int(row["Hylak_id"])
        lon, lat, dn = nasa_cells(polygons[lake_id], day, cache)
        keys, values = _our_values(files["grid"], lake_id)
        keep, nasa_ci, our_ci, cell_bin, detected, counts = pair_cells(lon, lat, dn, keys, values)
        x, y = nasa_ci[keep], our_ci[keep]
        series.append({"label": _label(row), "x": x, "y": y, "r": _r(x, y)})
        tables.append(pd.DataFrame({
            "Hylak_id": lake_id, "Lake_name": (row.get("Lake_name") or "").strip(),
            "longitude": lon[keep], "latitude": lat[keep], "NASA_DN": dn[keep], "NASA_CI_cyano": x,
            "our_bin": cell_bin[keep], "our_CI_cyano": y, "our_detected": detected[keep].astype(int),
        }))
        per_lake.append({"Hylak_id": lake_id, "Lake_name": _label(row), "R": round(_r(x, y), 4), **counts})
    all_x = np.concatenate([s["x"] for s in series])
    all_y = np.concatenate([s["y"] for s in series])
    slope, intercept = _fit(all_x, all_y)
    overall = {"cells": int(all_x.size), "r": _r(all_x, all_y), "slope": slope, "intercept": intercept}
    if overall["cells"] == 0:
        raise ValueError("No cell has a valid NASA value and a valid value of ours; nothing to plot. Per lake: "
                         + json.dumps(per_lake))
    folder = Path(out_dir) if out_dir else workspace.output / "compare" / files["folder"].relative_to(workspace.output / "daily")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{day.isoformat()}_lake" + ("s_" if len(ids) > 1 else "_") + "_".join(str(i) for i in ids)
    png, table = folder / f"{stem}_scatter.png", folder / f"{stem}_pairs.csv"
    _plot(png, series, files["state"], day, overall)
    pd.concat(tables, ignore_index=True).to_csv(table, index=False)
    return {
        "png": str(png), "pairs_csv": str(table), "state": files["state"], "date": day.isoformat(),
        "overall": {"cells": overall["cells"], "R": round(overall["r"], 4), "slope": round(slope, 4),
                    "intercept": float(f"{intercept:.4g}")},
        "lakes": per_lake,
        "rules": "x = NASA CI_cyano of the cell (DN 0 = 0); y = our daily CI_cyano of the bin containing the cell centre "
                 "(below detection = 0); cells with NASA land/no data or no valid value of ours are left out",
    }
