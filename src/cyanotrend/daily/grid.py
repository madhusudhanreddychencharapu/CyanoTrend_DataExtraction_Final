"""NASA CyAN CONUS 300 m Albers grid, and one NASA-grid file per processed scene.

The lattice is NASA's own, read from a NASA CyAN V6 daily tile
(L2026179.L3m_DAY_CYAN_CI_cyano_CYAN_CONUS_300m_7_5.tif): EPSG:5070
(NAD83 / Conus Albers Equal Area), square 300 m cells, 2000 x 2000 cells per
tile, tile 7_5 upper-left corner at x = 1,034,402.95 m, y = 914,826.27 m.
Every scene is placed on this single fixed lattice, so a cell means the same
piece of ground in every scene and in NASA's tiles.

Cell indices count from the upper-left corner of the 9 x 6 tile layout
(54 CONUS tiles; Schaeffer et al. 2022), inferred from tile 7_5 assuming
1-based <column>_<row> tile names. The inference only affects tile labels;
cell edges come from NASA's tile itself.

Within one scene, each native lake pixel is spread over its footprint (the
parallelogram spanned by its neighbouring pixels) and mapped to cells:
- "area": area-weighted mean of the valid pixels overlapping the cell
  (CyAN maps to Albers "with an area-weighted interpolation",
  Urquhart & Schaeffer 2019, Data in Brief 28, 104826);
- "nearest": the value of the overlapping pixel whose centre is closest
  to the cell centre (invalid if that pixel is invalid).
These two CI_cyano fields remain stored for reference. The daily stage now uses
the independently stored maximum valid overlapping value for each of CI,
CI_cyano, NDCI, MPH and FAI, as explicitly selected for the daily extension.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import uuid

import numpy as np

SCHEMA = "cyanotrend-nasa-grid-2"
CRS_EPSG = 5070
CELL_M = 300.0
TILE_CELLS = 2000
TILE_M = CELL_M * TILE_CELLS
REFERENCE_TILE = {
    "file": "L2026179.L3m_DAY_CYAN_CI_cyano_CYAN_CONUS_300m_7_5.tif",
    "tile": "7_5",
    "upper_left_x_m": 1034402.95,
    "upper_left_y_m": 914826.27,
}
TILE_COLUMNS = 9
TILE_ROWS = 6
ORIGIN_X = REFERENCE_TILE["upper_left_x_m"] - 6 * TILE_M  # -2,565,597.05 m
ORIGIN_Y = REFERENCE_TILE["upper_left_y_m"] + 4 * TILE_M  # 3,314,826.27 m
N_ROWS = TILE_ROWS * TILE_CELLS
N_COLS = TILE_COLUMNS * TILE_CELLS
SUBSAMPLES = 6  # 6 x 6 footprint points per pixel: 50 m spacing for a 300 m pixel
CHUNK_PIXELS = 40000
VECTOR_RANGE_M = (100.0, 2500.0)  # plausible OLCI FR pixel spacing
METHODS = ("area", "nearest", "max")
VALUE_DEFINITION = (
    "CI_cyano before the detection threshold: CI where CI is finite (SS681 < 0, "
    "not clear water) and SS665 > 0, else 0 for CI-valid water"
)
VALID_DEFINITION = "ci_valid_mask (bloom-aware valid water, finite CI inputs, not adjacency-flagged)"


def grid_definition():
    return {
        "crs": f"EPSG:{CRS_EPSG}",
        "cell_m": CELL_M,
        "origin_x_m": ORIGIN_X,
        "origin_y_m": ORIGIN_Y,
        "rows": N_ROWS,
        "cols": N_COLS,
        "tile_cells": TILE_CELLS,
        "reference_tile": REFERENCE_TILE,
        "row_rule": "row = floor((origin_y - y) / 300)",
        "col_rule": "col = floor((x - origin_x) / 300)",
    }


@lru_cache(maxsize=1)
def _forward():
    from pyproj import Transformer
    return Transformer.from_crs(4326, CRS_EPSG, always_xy=True)


@lru_cache(maxsize=1)
def _inverse():
    from pyproj import Transformer
    return Transformer.from_crs(CRS_EPSG, 4326, always_xy=True)


def project(lon, lat):
    x, y = _forward().transform(np.asarray(lon, dtype=float), np.asarray(lat, dtype=float))
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def unproject(x, y):
    lon, lat = _inverse().transform(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)


def cell_of(x, y):
    col = np.floor((np.asarray(x, dtype=float) - ORIGIN_X) / CELL_M).astype(np.int64)
    row = np.floor((ORIGIN_Y - np.asarray(y, dtype=float)) / CELL_M).astype(np.int64)
    return row, col


def cell_centre(row, col):
    row = np.asarray(row, dtype=float)
    col = np.asarray(col, dtype=float)
    return ORIGIN_X + (col + 0.5) * CELL_M, ORIGIN_Y - (row + 0.5) * CELL_M


def inside_grid(row, col):
    return (row >= 0) & (row < N_ROWS) & (col >= 0) & (col < N_COLS)


def cell_key(row, col):
    return np.asarray(row, dtype=np.int64) * N_COLS + np.asarray(col, dtype=np.int64)


def key_cell(key):
    key = np.asarray(key, dtype=np.int64)
    return key // N_COLS, key % N_COLS


def tile_name(row, col):
    return f"{int(col) // TILE_CELLS + 1}_{int(row) // TILE_CELLS + 1}"


def tile_offset(transform):
    """Integer (row, col) of a NASA tile's first cell; refuses grids off the lattice."""
    a, b, c, d, e, f = (float(v) for v in list(transform)[:6])
    if not (np.isclose(a, CELL_M) and np.isclose(e, -CELL_M) and b == 0 and d == 0):
        raise ValueError("Raster is not a north-up 300 m grid")
    col = (c - ORIGIN_X) / CELL_M
    row = (ORIGIN_Y - f) / CELL_M
    if not np.allclose([row, col], np.rint([row, col]), rtol=0, atol=1e-4):
        raise ValueError("Raster cells are not on the NASA CyAN lattice")
    return int(round(row)), int(round(col))


def lake_cells(geometry):
    """Cell keys whose centres lie strictly inside the lake polygon (NASA count rule).

    The same rule as nasa_cyan.count_lake_cells: cell centre, converted to
    longitude/latitude, inside the original HydroLAKES polygon.
    """
    from shapely import contains_xy, prepare
    from ..nasa_cyan import projected_lake

    if geometry is None or geometry.is_empty:
        return np.zeros(0, dtype=np.int64)
    polygon = projected_lake(geometry, f"EPSG:{CRS_EPSG}")
    if polygon.is_empty:
        return np.zeros(0, dtype=np.int64)
    minx, miny, maxx, maxy = polygon.bounds
    r0, c0 = (int(v) - 1 for v in cell_of(minx, maxy))
    r1, c1 = (int(v) + 1 for v in cell_of(maxx, miny))
    r0, c0 = max(r0, 0), max(c0, 0)
    r1, c1 = min(r1, N_ROWS - 1), min(c1, N_COLS - 1)
    if r1 < r0 or c1 < c0:
        return np.zeros(0, dtype=np.int64)
    prepare(geometry)
    cols = np.arange(c0, c1 + 1, dtype=np.int64)
    keys = []
    step = max(1, 2_000_000 // max(1, cols.size))
    for start in range(r0, r1 + 1, step):
        rows = np.arange(start, min(start + step, r1 + 1), dtype=np.int64)
        rr, cc = np.meshgrid(rows, cols, indexing="ij")
        x, y = cell_centre(rr, cc)
        lon, lat = unproject(x, y)
        inside = contains_xy(geometry, lon, lat)
        if inside.any():
            keys.append(cell_key(rr[inside], cc[inside]))
    if not keys:
        return np.zeros(0, dtype=np.int64)
    return np.sort(np.concatenate(keys))


# ---------------------------------------------------------------------------
# Native lake pixels -> cells of one scene
# ---------------------------------------------------------------------------

def native_pixels(path):
    """Read one native compact lake-pixel file (one processing window)."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        names = set(ds.variables)

        def read(name, fill=np.nan):
            values = ds.variables[name][:]
            return np.ma.filled(np.ma.asarray(values).astype(float), fill)

        lat = read("latitude")
        lon = read("longitude")
        srow = np.ma.filled(ds.variables["source_row"][:], -1).astype(np.int64)
        scol = np.ma.filled(ds.variables["source_col"][:], -1).astype(np.int64)
        ci = read("CI")
        ss665 = read("SS_665")
        if "ci_valid_mask" in names:
            valid = read("ci_valid_mask", 0) == 1
            valid_source = "ci_valid_mask"
        else:  # compact files written before the CI corrections existed
            valid = (read("valid_water_mask", 0) == 1) & np.isfinite(read("SS_681"))
            valid_source = "valid_water_mask & finite SS_681 (no ci_valid_mask in file)"
        indices = {"CI": np.where((read("ci_candidate_mask", 0) == 1) & valid, ci, np.nan)}
        for name in ("NDCI", "MPH", "FAI"):
            indices[name] = np.where(read(f"{name.lower()}_valid_mask", 0) == 1, read(name), np.nan)
        flags = {"primary_retained": read("valid_water_mask", 0) == 1,
                 "cloud_excluded": read("cloud_excluded_mask", 0) == 1}
    located = np.isfinite(lat) & np.isfinite(lon) & (srow >= 0) & (scol >= 0)
    valid &= located
    value = np.where(valid & np.isfinite(ci) & (ss665 > 0), ci, 0.0)
    indices["CI_cyano"] = np.where(valid, value, np.nan)
    return {
        "lat": lat[located], "lon": lon[located],
        "row": srow[located], "col": scol[located],
        "valid": valid[located], "value": value[located],
        "valid_source": valid_source,
        "indices": {k: v[located] for k, v in indices.items()},
        "flags": {k: v[located] for k, v in flags.items()},
    }


def _neighbour_vectors(x, y, srow, scol):
    """Along-track (row+1) and across-track (col+1) displacement of each pixel, metres."""
    n = x.size
    width = int(scol.max()) + 2 if n else 1
    keys = srow * width + scol
    order = np.argsort(keys, kind="stable")
    sorted_keys = keys[order]

    def find(target):
        pos = np.searchsorted(sorted_keys, target)
        pos = np.minimum(pos, max(n - 1, 0))
        hit = sorted_keys[pos] == target if n else np.zeros(0, dtype=bool)
        index = np.full(n, -1, dtype=np.int64)
        index[hit] = order[pos[hit]]
        return index

    lo, hi = VECTOR_RANGE_M
    result = []
    for step in (width, 1):  # next row, next column
        vx = np.full(n, np.nan)
        vy = np.full(n, np.nan)
        for sign in (1, -1):
            j = find(keys + sign * step)
            need = (j >= 0) & np.isnan(vx)
            dx = sign * (x[j[need]] - x[need])
            dy = sign * (y[j[need]] - y[need])
            length = np.hypot(dx, dy)
            ok = (length >= lo) & (length <= hi)
            idx = np.flatnonzero(need)[ok]
            vx[idx] = dx[ok]
            vy[idx] = dy[ok]
        # Pixel size depends on viewing angle, i.e. on the column: fill gaps per column.
        missing = np.isnan(vx)
        if missing.any():
            import pandas as pd
            frame = pd.DataFrame({"col": scol, "vx": vx, "vy": vy})
            per_col = frame.groupby("col")[["vx", "vy"]].median()
            fill = per_col.reindex(scol[missing])
            vx[missing] = fill["vx"].to_numpy()
            vy[missing] = fill["vy"].to_numpy()
            missing = np.isnan(vx)
            if missing.any():
                good = ~np.isnan(vx)
                if good.any():
                    vx[missing] = np.median(vx[good])
                    vy[missing] = np.median(vy[good])
                else:  # isolated pixels: assume a nominal 300 m north-up footprint
                    vx[missing], vy[missing] = (0.0, -CELL_M) if step == width else (CELL_M, 0.0)
        result.append((vx, vy))
    return result


def _reduce(parts):
    """Combine per-chunk cell records: sums add, nearest keeps the closest pixel."""
    key = np.concatenate([p["key"] for p in parts]) if parts else np.zeros(0, dtype=np.int64)
    if key.size == 0:
        return {name: np.zeros(0, dtype=dtype) for name, dtype in
                (("key", np.int64), ("weight", float), ("valid_weight", float), ("value_sum", float),
                 ("near_d2", float), ("near_valid", bool), ("near_value", float))}
    weight = np.concatenate([p["weight"] for p in parts])
    valid_weight = np.concatenate([p["valid_weight"] for p in parts])
    value_sum = np.concatenate([p["value_sum"] for p in parts])
    near_d2 = np.concatenate([p["near_d2"] for p in parts])
    near_valid = np.concatenate([p["near_valid"] for p in parts])
    near_value = np.concatenate([p["near_value"] for p in parts])
    unique, inverse = np.unique(key, return_inverse=True)
    m = unique.size
    out = {
        "key": unique,
        "weight": np.bincount(inverse, weight, m),
        "valid_weight": np.bincount(inverse, valid_weight, m),
        "value_sum": np.bincount(inverse, value_sum, m),
    }
    order = np.lexsort((near_d2, inverse))
    first = order[np.r_[True, inverse[order][1:] != inverse[order][:-1]]]
    out["near_d2"] = near_d2[first]
    out["near_valid"] = near_valid[first]
    out["near_value"] = near_value[first]
    return out


def pixels_to_cells(pixels, subsamples=SUBSAMPLES, chunk=CHUNK_PIXELS):
    """Spread each pixel over its footprint and accumulate per NASA cell."""
    x, y = project(pixels["lon"], pixels["lat"])
    (ax, ay), (bx, by) = _neighbour_vectors(x, y, pixels["row"], pixels["col"])
    area = np.abs(ax * by - ay * bx)
    offsets = (np.arange(subsamples) + 0.5) / subsamples - 0.5
    u, v = (g.ravel() for g in np.meshgrid(offsets, offsets, indexing="ij"))
    valid = np.asarray(pixels["valid"], dtype=bool)
    value = np.asarray(pixels["value"], dtype=float)
    parts = []
    for s in range(0, x.size, chunk):
        e = min(s + chunk, x.size)
        px, py = x[s:e, None], y[s:e, None]
        sx = px + u[None, :] * ax[s:e, None] + v[None, :] * bx[s:e, None]
        sy = py + u[None, :] * ay[s:e, None] + v[None, :] * by[s:e, None]
        row, col = cell_of(sx, sy)
        keep = inside_grid(row, col)
        if not keep.any():
            continue
        pix = np.broadcast_to(np.arange(s, e)[:, None], sx.shape)[keep]
        row, col = row[keep], col[keep]
        cx, cy = cell_centre(row, col)
        w = (area[pix] / subsamples**2)
        ok = valid[pix]
        parts.append(_reduce([{
            "key": cell_key(row, col),
            "weight": w,
            "valid_weight": np.where(ok, w, 0.0),
            "value_sum": np.where(ok, w * value[pix], 0.0),
            "near_d2": (x[pix] - cx) ** 2 + (y[pix] - cy) ** 2,
            "near_valid": ok,
            "near_value": np.where(ok, value[pix], np.nan),
        }]))
    return _reduce(parts) if parts else _reduce([])


# ---------------------------------------------------------------------------
# Scene grid files
# ---------------------------------------------------------------------------

def native_dirs(folder, scene_id):
    sid = str(uuid.UUID(str(scene_id)))
    return sorted(p for p in Path(folder).glob(f"{sid}_*_native") if p.is_dir())


def grid_path(native_dir):
    native_dir = Path(native_dir)
    if not native_dir.name.endswith("_native"):
        raise ValueError(f"Not a native folder: {native_dir}")
    return native_dir.with_name(native_dir.name[: -len("_native")] + "_nasa_grid.nc")


def scene_native_files(folder, scene_id):
    """Select only this scene's native windows from a shared date folder."""
    sid = str(uuid.UUID(str(scene_id)))
    return sorted(Path(folder).glob(f"{sid}_*_lakepixels.nc"))


def scene_grid_path(folder, scene_id, config_hash):
    from ..common import path_component
    sid = str(uuid.UUID(str(scene_id)))
    config = path_component(config_hash, "processing configuration")
    return Path(folder) / f"{sid}_{config}_nasa_grid.nc"


def utc_date(start):
    stamp = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Scene start time requires a timezone")
    return stamp.astimezone(timezone.utc).date()


def write_scene_grid(native_dir, scene, out_path=None, *, native_files=None):
    """Write one scene grid; explicit native_files isolates scenes sharing a date folder."""
    import netCDF4
    from ..common import digest, sync_dir
    from .features import INDICES, FLAGS, maximum_pixels_to_cells, reduce_maximum

    native_dir = Path(native_dir)
    files = sorted(map(Path, native_files)) if native_files is not None else sorted(native_dir.glob("*.nc"))
    if not files:
        raise ValueError(f"No native lake-pixel files in {native_dir}")
    out_path = Path(out_path) if out_path else grid_path(native_dir)
    parts, maximum_parts, sources, n_pixels, valid_sources = [], [], [], 0, set()
    for path in files:  # one file per processing window; pixel neighbours never cross files
        pixels = native_pixels(path)
        n_pixels += int(pixels["lat"].size)
        valid_sources.add(pixels["valid_source"])
        if pixels["lat"].size:
            parts.append(pixels_to_cells(pixels))
            maximum_parts.append(maximum_pixels_to_cells(pixels))
        sources.append({"file": path.name, "sha256": digest(path)})
    cells = _reduce(parts) if parts else _reduce([])
    maximum = reduce_maximum(maximum_parts)
    if not np.array_equal(cells["key"], maximum["key"]):
        raise ValueError("Maximum and reference mappings use different cell footprints")
    row, col = key_cell(cells["key"])
    with np.errstate(invalid="ignore", divide="ignore"):
        area_value = np.where(cells["valid_weight"] > 0, cells["value_sum"] / cells["valid_weight"], np.nan)
    attrs = {
        "Conventions": "CF-1.8",
        "title": "Sentinel-3 OLCI lake pixels placed on the NASA CyAN CONUS 300 m grid (one scene)",
        "schema": SCHEMA,
        "scene_id": str(scene["id"]),
        "scene_name": str(scene.get("name", "")),
        "satellite": str(scene.get("satellite", str(scene.get("name", ""))[:3])),
        "acquisition_start": str(scene.get("start", "")),
        "date_utc": utc_date(scene["start"]).isoformat(),
        "native_folder": native_dir.name,
        "source_native_files": json.dumps(sources),
        "native_pixels": n_pixels,
        "grid_definition": json.dumps(grid_definition()),
        "subsamples_per_axis": SUBSAMPLES,
        "value_definition": VALUE_DEFINITION,
        "valid_definition": "; ".join(sorted(valid_sources)) or VALID_DEFINITION,
        "methods": "max: highest overlapping valid value independently per index (daily default); area/nearest retained for reference",
        "cloud_diagnostic": "cloud_excluded_any: at least one overlapping native observation has cloud_excluded_mask=1; not a whole-cell cloud classification",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "software_version": __import__("cyanotrend").__version__,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
            ds.setncatts(attrs)
            ds.createDimension("cell", int(row.size))
            spec = [
                ("row", "i4", row, "NASA CyAN CONUS grid row (from the upper-left of tile 1_1)", "1"),
                ("col", "i4", col, "NASA CyAN CONUS grid column (from the upper-left of tile 1_1)", "1"),
                ("footprint_area", "f4", cells["weight"], "pixel footprint area overlapping the cell", "m2"),
                ("valid_area", "f4", cells["valid_weight"], "valid pixel footprint area overlapping the cell", "m2"),
                ("CI_cyano_area", "f4", area_value, "area-weighted mean CI_cyano of valid overlapping pixels", "1"),
                ("valid_nearest", "u1", cells["near_valid"].astype(np.uint8), "nearest overlapping pixel is valid", "1"),
                ("CI_cyano_nearest", "f4", cells["near_value"], "CI_cyano of the nearest overlapping pixel", "1"),
                ("nearest_distance", "f4", np.sqrt(cells["near_d2"]), "distance from nearest pixel centre to cell centre", "m"),
            ]
            spec += [(f"{name}_maximum", "f4", maximum[name],
                      f"highest valid overlapping {name} value within this scene", "1") for name in INDICES]
            spec += [(f"{name}_any", "u1", maximum[name].astype(np.uint8),
                      f"at least one overlapping native observation is {name}", "1") for name in FLAGS]
            for name, dtype, values, long_name, units in spec:
                var = ds.createVariable(name, dtype, ("cell",), zlib=True, complevel=4, shuffle=True, fill_value=False)
                var.setncatts({"long_name": long_name, "units": units})
                var[:] = values
        os.replace(tmp, out_path)
        sync_dir(out_path.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "status": "written",
        "path": out_path.name,
        "sha256": digest(out_path),
        "cells": int(row.size),
        "native_pixels": n_pixels,
    }


def read_scene_grid(path):
    """Arrays sorted by cell key, plus attributes; refuses other grids or schemas."""
    import netCDF4
    from .features import INDICES, FLAGS

    with netCDF4.Dataset(path) as ds:
        attrs = {k: ds.getncattr(k) for k in ds.ncattrs()}
        if attrs.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported NASA-grid file schema in {Path(path).name}")
        definition = json.loads(attrs["grid_definition"])
        if (definition["crs"] != f"EPSG:{CRS_EPSG}" or definition["cell_m"] != CELL_M
                or not np.isclose(definition["origin_x_m"], ORIGIN_X)
                or not np.isclose(definition["origin_y_m"], ORIGIN_Y)):
            raise ValueError(f"{Path(path).name} uses a different grid")

        def get(name):
            return np.ma.filled(ds.variables[name][:], np.nan if name.startswith("CI_") else 0)

        data = {name: np.asarray(get(name)) for name in
                ("row", "col", "valid_area", "CI_cyano_area", "valid_nearest", "CI_cyano_nearest")}
        maximum = {name: np.asarray(get(f"{name}_maximum")) for name in INDICES}
        flags = {name: np.asarray(get(f"{name}_any")) == 1 for name in FLAGS}
    key = cell_key(data["row"], data["col"])
    order = np.argsort(key, kind="stable")
    return {
        "attrs": attrs,
        "key": key[order],
        "area_valid": (data["valid_area"] > 0)[order],
        "area_value": data["CI_cyano_area"].astype(float)[order],
        "nearest_valid": (data["valid_nearest"] == 1)[order],
        "nearest_value": data["CI_cyano_nearest"].astype(float)[order],
        "max_valid": np.isfinite(maximum["CI_cyano"])[order],
        "max_value": maximum["CI_cyano"].astype(float)[order],
        "maximum": {k: v.astype(float)[order] for k, v in maximum.items()},
        "flags": {k: v[order] for k, v in flags.items()},
    }
