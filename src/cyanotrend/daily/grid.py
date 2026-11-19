"""ISIN bins (NASA Level-3 integerized sinusoidal grid) and one bin file per processed scene.

This is the only grid CyanoTrend uses, for every country.

The grid is the NASA ocean-colour Level-3 binning scheme (Campbell, Blaisdell &
Darzi 1995, NASA TM 104566 Vol. 32), at 69120 rows (about 290 m; NASA's "250 m"
setting). Rows are strips of equal latitude height; row r has
int(2 * 69120 * cos(latitude of the row centre) + 0.5) bins of equal longitude
width, counted east from 180 W. Bins are numbered from 1, starting at the
south-west corner (NASA's numbering). Every bin is about 290 x 290 m everywhere
and has the same area to within 0.002% between 80 S and 80 N.

Within one scene, each native lake pixel is spread over its footprint (the
parallelogram spanned by its neighbouring pixels) and shared among the bins it
overlaps in proportion to the exact overlap area (no sampling points). A bin's value
for each index is the area-weighted mean of the valid pixels overlapping it. The nine rhos bands are
binned the same way (valid: primary valid water with all nine bands finite), and ten more native masks are
kept as "some overlapping pixel has the mask set" flags, so the daily stage can rebuild every layer of
the single-scene science product.
Pixels never decide a bin by their centre alone: a pixel and a bin are about
the same size, so centre-point binning would mis-assign roughly half the area.
Scenes are combined later, by the daily stage, with the per-bin maximum.

Bins are not a regular raster: neighbouring rows do not line up. A scene file
therefore lists the bins it touches (bin number, row, column, bin-centre
latitude and longitude, bin area and the values) instead of a 2-D array.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import uuid

import numpy as np

from .features import INDICES, FLAGS, BANDS, MASKS, NATIVE_BANDS, NATIVE_MASKS

SCHEMA = "cyanotrend-isin-grid-2"  # 2: adds rhos bands and masks; schema-1 files are rebuilt from the native files
N_ROWS = 69120
# Authalic radius of the WGS84 ellipsoid: a sphere with the same surface area, so bin areas are
# true ground areas. Bin numbering depends only on angles, not on this radius (NASA quotes
# its own bin areas for a 6378.145 km sphere).
EARTH_RADIUS_M = 6371007.181
BIN_HEIGHT_M = EARTH_RADIUS_M * np.pi / N_ROWS  # about 289.6 m
NOMINAL_PIXEL_M = 300.0  # OLCI full resolution at the sub-satellite point
CHUNK_PIXELS = 5000
MIN_OVERLAP_M2 = 1e-6  # overlaps below one square millimetre are floating-point noise
VECTOR_RANGE_M = (100.0, 2500.0)  # plausible OLCI FR pixel spacing
METHODS = ("area",)
VALUE_DEFINITION = (
    "per index: area-weighted mean over the valid pixels overlapping the bin; CI_cyano is "
    "CI where CI is finite (SS681 < 0, not clear water) and SS665 > 0, else 0 for CI-valid "
    "water, before the detection threshold"
)
VALID_DEFINITION = "ci_valid_mask (bloom-aware valid water, finite CI inputs, not adjacency-flagged)"


# ---------------------------------------------------------------------------
# ISIN geometry
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _rows():
    """Bins per row, and the number of bins before each row (0-based)."""
    row = np.arange(N_ROWS)
    lat = (row + 0.5) * 180.0 / N_ROWS - 90.0
    bins = np.floor(2.0 * N_ROWS * np.cos(np.radians(lat)) + 0.5).astype(np.int64)
    before = np.concatenate(([0], np.cumsum(bins)[:-1]))
    return bins, before


def bins_per_row():
    return _rows()[0]


def total_bins():
    bins, before = _rows()
    return int(before[-1] + bins[-1])


def grid_definition():
    return {
        "name": "NASA Level-3 integerized sinusoidal (ISIN) bins",
        "rows": N_ROWS,
        "total_bins": total_bins(),
        "earth_radius_m": EARTH_RADIUS_M,
        "bin_height_m": BIN_HEIGHT_M,
        "bins_in_equator_row": int(bins_per_row().max()),
        "bins_per_row_rule": "int(2 * rows * cos(row_centre_latitude) + 0.5)",
        "row_rule": "row = floor((latitude + 90) * rows / 180), counted from 90 S",
        "col_rule": "col = floor(((longitude + 180) mod 360) / 360 * bins_in_row), counted east from 180 W",
        "bin_number": "1-based: bins before the row + col + 1 (NASA numbering)",
        "binning": "area-weighted, exact overlap of each pixel footprint with each bin",
    }


def wrap_degrees(value):
    """Longitude in [-180, 180)."""
    return (np.asarray(value, dtype=float) + 180.0) % 360.0 - 180.0


def bin_number(lon, lat):
    """ISIN bin number of each point; 0 where the point is not on Earth."""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    bins, before = _rows()
    on_earth = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lat) <= 90.0)
    lat = np.where(on_earth, lat, 0.0)
    lon = np.where(on_earth, lon, 0.0)
    row = np.clip(np.floor((lat + 90.0) * (N_ROWS / 180.0)).astype(np.int64), 0, N_ROWS - 1)
    unit = ((lon + 180.0) % 360.0) / 360.0
    col = np.minimum(np.floor(unit * bins[row]).astype(np.int64), bins[row] - 1)
    return np.where(on_earth, before[row] + col + 1, 0)


def bin_row_col(key):
    """(row, col) of bin numbers; the numbers must be 1 or more."""
    _, before = _rows()
    index = np.asarray(key, dtype=np.int64) - 1
    row = np.searchsorted(before, index, side="right") - 1
    return row, index - before[row]


def bin_key(row, col):
    _, before = _rows()
    return before[np.asarray(row, dtype=np.int64)] + np.asarray(col, dtype=np.int64) + 1


def bin_centre_row_col(row, col):
    """Bin-centre (longitude, latitude) in degrees."""
    bins, _ = _rows()
    row = np.asarray(row, dtype=np.int64)
    lat = (row + 0.5) * 180.0 / N_ROWS - 90.0
    lon = 360.0 * (np.asarray(col, dtype=float) + 0.5) / bins[row] - 180.0
    return lon, lat


def bin_centre(key):
    return bin_centre_row_col(*bin_row_col(key))


def bin_area_m2(row):
    """Exact spherical area of the bins of each row."""
    bins, _ = _rows()
    row = np.asarray(row, dtype=np.int64)
    south = np.radians(row * 180.0 / N_ROWS - 90.0)
    north = np.radians((row + 1) * 180.0 / N_ROWS - 90.0)
    return EARTH_RADIUS_M ** 2 * (2.0 * np.pi / bins[row]) * (np.sin(north) - np.sin(south))


def lake_bins(geometry):
    """Bin numbers whose centres lie strictly inside the lake polygon.

    The polygon is the original HydroLAKES geometry in longitude/latitude.
    Parts of a lake split at the date line are tested separately.
    """
    from shapely import contains_xy, get_parts, prepare

    empty = np.zeros(0, dtype=np.int64)
    if geometry is None or geometry.is_empty:
        return empty
    bins, before = _rows()
    prepare(geometry)
    found = []
    for part in get_parts(geometry):
        minx, miny, maxx, maxy = part.bounds
        first = int(np.clip(np.floor((miny + 90.0) * N_ROWS / 180.0), 0, N_ROWS - 1))
        last = int(np.clip(np.floor((maxy + 90.0) * N_ROWS / 180.0), 0, N_ROWS - 1))
        rows = np.arange(first, last + 1, dtype=np.int64)
        width = bins[rows]
        c0 = np.clip(np.floor((minx + 180.0) / 360.0 * width).astype(np.int64) - 1, 0, width - 1)
        c1 = np.clip(np.floor((maxx + 180.0) / 360.0 * width).astype(np.int64) + 1, 0, width - 1)
        counts = c1 - c0 + 1
        ends = np.cumsum(counts)
        start = 0
        while start < rows.size:  # batches of about two million candidate bin centres
            allowed = (int(ends[start - 1]) if start else 0) + 2_000_000
            stop = max(start + 1, int(np.searchsorted(ends, allowed, side="right")))
            n = counts[start:stop]
            total = int(n.sum())
            rr = np.repeat(rows[start:stop], n)
            cc = np.repeat(c0[start:stop], n) + np.arange(total) - np.repeat(np.cumsum(n) - n, n)
            lon, lat = bin_centre_row_col(rr, cc)
            inside = contains_xy(geometry, lon, lat)
            if inside.any():
                found.append(before[rr[inside]] + cc[inside] + 1)
            start = stop
    return np.unique(np.concatenate(found)) if found else empty


# ---------------------------------------------------------------------------
# Local metric plane for pixel footprints
# ---------------------------------------------------------------------------
# The sinusoidal projection is the planar form of the ISIN grid: it preserves
# area, so footprint areas (and shares of them) are in true square metres.

def central_meridian(lon):
    """Mean longitude of a pixel set (circular), so one scene window never straddles the date line."""
    radians = np.radians(np.asarray(lon, dtype=float))
    return float(np.degrees(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())))


def project(lon, lat, lon0):
    lam = np.radians(wrap_degrees(np.asarray(lon, dtype=float) - lon0))
    phi = np.radians(np.asarray(lat, dtype=float))
    return EARTH_RADIUS_M * lam * np.cos(phi), EARTH_RADIUS_M * phi


def unproject(x, y, lon0):
    phi = np.asarray(y, dtype=float) / EARTH_RADIUS_M
    cos_phi = np.cos(phi)
    with np.errstate(divide="ignore", invalid="ignore"):
        lam = np.asarray(x, dtype=float) / (EARTH_RADIUS_M * cos_phi)
    lon = wrap_degrees(lon0 + np.degrees(lam))
    return np.where(np.abs(cos_phi) > 1e-9, lon, np.nan), np.degrees(phi)


# ---------------------------------------------------------------------------
# Native lake pixels -> bins of one scene
# ---------------------------------------------------------------------------

def native_pixels(path):
    """Read one native compact lake-pixel file (one processing window)."""
    import netCDF4

    absent = set()
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

        def optional(name, fill):  # older compact files may lack a layer: recorded, never invented
            if name in names:
                return read(name, fill)
            absent.add(name)
            return np.full(lat.shape, fill)

        rhos = {out: optional(native, np.nan) for out, native in NATIVE_BANDS.items()}
        rhos_ok = flags["primary_retained"].copy()
        for values in rhos.values():
            rhos_ok &= np.isfinite(values)
        bands = {out: np.where(rhos_ok, values, np.nan) for out, values in rhos.items()}
        masks = {out: optional(native, 0) == 1 for out, native in NATIVE_MASKS.items()}
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
        "bands": {k: v[located] for k, v in bands.items()},
        "masks": {k: v[located] for k, v in masks.items()},
        "absent": sorted(absent),
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
                    vx[missing], vy[missing] = (0.0, -NOMINAL_PIXEL_M) if step == width else (NOMINAL_PIXEL_M, 0.0)
        result.append((vx, vy))
    return result


def _empty_bins():
    out = {"key": np.zeros(0, dtype=np.int64), "weight": np.zeros(0)}
    for name in INDICES:
        out[f"{name}_weight"] = np.zeros(0)
        out[f"{name}_sum"] = np.zeros(0)
    out["bands_weight"] = np.zeros(0)
    for name in BANDS:
        out[f"{name}_sum"] = np.zeros(0)
    for name in FLAGS + MASKS:
        out[name] = np.zeros(0, dtype=bool)
    return out


def _reduce(parts):
    """Combine per-chunk bin records: footprint areas and area-weighted sums add; flags OR."""
    parts = [p for p in parts if p["key"].size]
    if not parts:
        return _empty_bins()
    key = np.concatenate([p["key"] for p in parts])
    unique, inverse = np.unique(key, return_inverse=True)
    m = unique.size
    out = {"key": unique, "weight": np.bincount(inverse, np.concatenate([p["weight"] for p in parts]), m)}
    for name in INDICES:
        for suffix in ("weight", "sum"):
            field = f"{name}_{suffix}"
            out[field] = np.bincount(inverse, np.concatenate([p[field] for p in parts]), m)
    for field in ["bands_weight"] + [f"{name}_sum" for name in BANDS]:
        out[field] = np.bincount(inverse, np.concatenate([p[field] for p in parts]), m)
    for name in FLAGS + MASKS:
        seen = np.concatenate([p[name] for p in parts]).astype(float)
        out[name] = np.bincount(inverse, seen, m) > 0
    return out


_CORNER_A = np.array([-0.5, 0.5, 0.5, -0.5])
_CORNER_B = np.array([-0.5, -0.5, 0.5, 0.5])


def _footprint_corners(x, y, ax, ay, bx, by):
    """Corners of each pixel footprint, counter-clockwise, in the sinusoidal plane: (n, 4) x and y."""
    cx = x[:, None] + _CORNER_A * ax[:, None] + _CORNER_B * bx[:, None]
    cy = y[:, None] + _CORNER_A * ay[:, None] + _CORNER_B * by[:, None]
    flip = (ax * by - ay * bx) < 0
    order = np.where(flip[:, None], np.array([0, 3, 2, 1]), np.array([0, 1, 2, 3]))
    return np.take_along_axis(cx, order, 1), np.take_along_axis(cy, order, 1)


def cumulative_area(px, py, X, Y):
    """Area of each convex counter-clockwise polygon that lies left of X and below Y.

    px, py: (n, 4) corners; X: (n, e) vertical cut lines; Y: (n,) one horizontal cut line.
    Green's theorem: the area is the sum over the edges of the integral of min(x, X) dy, over the
    part of the edge below Y. Edges that go up bound the polygon on the right, edges that go
    down on the left, so the sum is (right boundary - left boundary) integrated over height.
    The overlap with a rectangle is then four of these (inclusion - exclusion): exact, no sampling.
    """
    x0, y0 = px[:, None, :], py[:, None, :]
    x1, y1 = np.roll(px, -1, axis=1)[:, None, :], np.roll(py, -1, axis=1)[:, None, :]
    dx, dy = x1 - x0, y1 - y0
    top = Y[:, None, None]
    cut = X[:, :, None]
    flat = dy == 0
    t_below = np.clip((top - y0) / np.where(flat, 1.0, dy), 0.0, 1.0)
    t_a = np.where(dy > 0, 0.0, t_below)  # edge part below Y: t in [t_a, t_b]
    t_b = np.where(dy > 0, t_below, 1.0)
    vertical = dx == 0
    t_cut = np.clip((cut - x0) / np.where(vertical, 1.0, dx), t_a, t_b)
    up = dx > 0
    # Rising in x: left of the cut for t <= t_cut. Falling in x: left of the cut for t >= t_cut.
    left_lo, left_hi = np.where(up, t_a, t_cut), np.where(up, t_cut, t_b)
    right_lo, right_hi = np.where(up, t_cut, t_a), np.where(up, t_b, t_cut)
    inside = x0 * (left_hi - left_lo) + dx * (left_hi ** 2 - left_lo ** 2) / 2.0
    outside = cut * (right_hi - right_lo)
    constant = np.minimum(x0, cut) * (t_b - t_a)
    total = np.where(vertical, constant, inside + outside) * dy
    return np.where(flat, 0.0, total).sum(axis=2)


def pixel_bin_overlaps(x, y, ax, ay, bx, by, lon0):
    """Exact overlap of pixel footprints with ISIN bins.

    x, y: pixel centres in the sinusoidal plane (central meridian lon0); (ax, ay), (bx, by):
    footprint edge vectors. Returns (pixel index, bin number, weight): weight is the share of
    the pixel's ground area that lies in the bin, times that area in square metres, so the
    weights of one pixel add up to its area.

    Each footprint is placed in a flat plane of (longitude, latitude), with longitude stretched
    by cos(latitude of the pixel centre). There the bins are exact rectangles, and the
    footprint (a few hundred metres) is straight to well under a millimetre.
    """
    n = x.size
    if n == 0:
        return (np.zeros(0, dtype=np.int64),) * 2 + (np.zeros(0),)
    radius = EARTH_RADIUS_M
    n_bins, before = _rows()
    cos_c = np.cos(y / radius)
    lon_c = wrap_degrees(lon0 + np.degrees(x / (radius * cos_c)))
    cx, cy = _footprint_corners(x, y, ax, ay, bx, by)
    px = cx * (cos_c[:, None] / np.cos(cy / radius)) - x[:, None]
    py = cy - y[:, None]
    plane_area = np.abs(ax * by - ay * bx)
    qx, qy = np.roll(px, -1, axis=1), np.roll(py, -1, axis=1)
    shape_area = 0.5 * (px * qy - qx * py).sum(axis=1)
    row_of = lambda lat_m: np.clip(np.floor((np.degrees((y + lat_m) / radius) + 90.0) * N_ROWS / 180.0),
                                   0, N_ROWS - 1).astype(np.int64)
    first = row_of(py.min(axis=1))
    last = row_of(py.max(axis=1))
    a_min, a_max = px.min(axis=1), px.max(axis=1)
    out_pix, out_key, out_weight = [], [], []
    for j in range(int((last - first).max()) + 1):
        row = np.minimum(first + j, last)
        active = (first + j) <= last
        nb = n_bins[row]
        step = radius * cos_c * 2.0 * np.pi / nb  # width of one bin in the stretched plane, metres
        kappa = (lon_c + 180.0) / 360.0 * nb      # fractional column of the pixel centre
        k_first = np.floor(a_min / step + kappa)
        k_last = np.floor(a_max / step + kappa)
        n_col = int((k_last - k_first).max()) + 1
        edges = (k_first[:, None] + np.arange(n_col + 1) - kappa[:, None]) * step[:, None]
        y_low = radius * np.pi * (row / N_ROWS - 0.5) - y
        y_high = radius * np.pi * ((row + 1) / N_ROWS - 0.5) - y
        left = cumulative_area(px, py, edges, y_high) - cumulative_area(px, py, edges, y_low)
        overlap = np.diff(left, axis=1)
        keep = active[:, None] & (overlap > MIN_OVERLAP_M2) & (shape_area > 0)[:, None]
        pix, offset = np.nonzero(keep)
        if not pix.size:
            continue
        col = np.mod(k_first[pix].astype(np.int64) + offset, nb[pix])
        out_pix.append(pix)
        out_key.append(before[row[pix]] + col + 1)
        out_weight.append(overlap[pix, offset] / shape_area[pix] * plane_area[pix])
    if not out_pix:
        return (np.zeros(0, dtype=np.int64),) * 2 + (np.zeros(0),)
    return np.concatenate(out_pix), np.concatenate(out_key), np.concatenate(out_weight)


def pixels_to_bins(pixels, chunk=CHUNK_PIXELS):
    """Spread each pixel over its footprint and accumulate per ISIN bin, by exact overlap area."""
    n = int(np.asarray(pixels["lon"]).size)
    if n == 0:
        return _empty_bins()
    lon0 = central_meridian(pixels["lon"])
    x, y = project(pixels["lon"], pixels["lat"], lon0)
    (ax, ay), (bx, by) = _neighbour_vectors(x, y, pixels["row"], pixels["col"])
    indices = pixels["indices"]
    flags = pixels["flags"]
    bands = pixels.get("bands") or {}
    masks = pixels.get("masks") or {}
    parts = []
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        pix, key, w = pixel_bin_overlaps(x[s:e], y[s:e], ax[s:e], ay[s:e], bx[s:e], by[s:e], lon0)
        if not pix.size:
            continue
        pix = pix + s
        record = {"key": key, "weight": w}
        for name in INDICES:
            value = indices[name][pix]
            ok = np.isfinite(value)
            record[f"{name}_weight"] = np.where(ok, w, 0.0)
            record[f"{name}_sum"] = np.where(ok, w * value, 0.0)
        for name in FLAGS:
            record[name] = flags[name][pix]
        ok = np.isfinite(bands[BANDS[0]][pix]) if bands else np.zeros(pix.size, dtype=bool)
        record["bands_weight"] = np.where(ok, w, 0.0)
        for name in BANDS:
            record[f"{name}_sum"] = np.where(ok, w * bands[name][pix], 0.0) if bands else np.zeros(pix.size)
        for name in MASKS:
            record[name] = masks[name][pix] if name in masks else np.zeros(pix.size, dtype=bool)
        parts.append(_reduce([record]))
    return _reduce(parts)


def area_means(bins):
    """Area-weighted mean per index (NaN where no valid pixel overlaps the bin)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return {name: np.where(bins[f"{name}_weight"] > 0, bins[f"{name}_sum"] / bins[f"{name}_weight"], np.nan)
                for name in INDICES}


def band_means(bins):
    """Area-weighted mean per rhos band over pixels valid for all bands (NaN where none)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return {name: np.where(bins["bands_weight"] > 0, bins[f"{name}_sum"] / bins["bands_weight"], np.nan)
                for name in BANDS}


# ---------------------------------------------------------------------------
# Scene bin files
# ---------------------------------------------------------------------------

def native_dirs(folder, scene_id):
    sid = str(uuid.UUID(str(scene_id)))
    return sorted(p for p in Path(folder).glob(f"{sid}_*_native") if p.is_dir())


def grid_path(native_dir):
    native_dir = Path(native_dir)
    if not native_dir.name.endswith("_native"):
        raise ValueError(f"Not a native folder: {native_dir}")
    return native_dir.with_name(native_dir.name[: -len("_native")] + "_isin_grid.nc")


def scene_native_files(folder, scene_id):
    """Select only this scene's native windows from a shared date folder."""
    sid = str(uuid.UUID(str(scene_id)))
    return sorted(Path(folder).glob(f"{sid}_*_lakepixels.nc"))


def scene_grid_path(folder, scene_id, config_hash):
    from ..common import path_component
    sid = str(uuid.UUID(str(scene_id)))
    config = path_component(config_hash, "processing configuration")
    return Path(folder) / f"{sid}_{config}_isin_grid.nc"


def utc_datetime(start):
    stamp = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Scene start time requires a timezone")
    return stamp.astimezone(timezone.utc)


def utc_date(start):
    return utc_datetime(start).date()


def solar_date(start, longitude):
    """Local mean solar date of an instant at a longitude: UTC plus longitude / 15 hours."""
    return (utc_datetime(start) + timedelta(hours=float(wrap_degrees(longitude)) / 15.0)).date()


def utc_dates_of_solar_date(day, west, east):
    """First and last UTC date of passes that have solar date `day` for lakes between two longitudes."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    first = midnight - timedelta(hours=east / 15.0)
    last = midnight + timedelta(days=1) - timedelta(hours=west / 15.0) - timedelta(microseconds=1)
    return first.date(), last.date()


def write_scene_grid(native_dir, scene, out_path=None, *, native_files=None):
    """Write one scene bin file; explicit native_files isolates scenes sharing a date folder."""
    import netCDF4
    from ..common import digest, sync_dir

    native_dir = Path(native_dir)
    files = sorted(map(Path, native_files)) if native_files is not None else sorted(native_dir.glob("*.nc"))
    if not files:
        raise ValueError(f"No native lake-pixel files in {native_dir}")
    out_path = Path(out_path) if out_path else grid_path(native_dir)
    parts, sources, n_pixels, valid_sources, absent = [], [], 0, set(), set()
    for path in files:  # one file per processing window; pixel neighbours never cross files
        pixels = native_pixels(path)
        n_pixels += int(pixels["lat"].size)
        valid_sources.add(pixels["valid_source"])
        absent.update(pixels.get("absent", ()))
        if pixels["lat"].size:
            parts.append(pixels_to_bins(pixels))
        sources.append({"file": path.name, "sha256": digest(path)})
    bins = _reduce(parts)
    means = area_means(bins)
    band_avg = band_means(bins)
    row, col = bin_row_col(bins["key"]) if bins["key"].size else (np.zeros(0, dtype=np.int64),) * 2
    lon, lat = bin_centre_row_col(row, col)
    attrs = {
        "Conventions": "CF-1.8",
        "title": "Sentinel-3 OLCI lake pixels placed on ISIN bins by area-weighted binning (one scene)",
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
        "value_definition": VALUE_DEFINITION,
        "valid_definition": "; ".join(sorted(valid_sources)) or VALID_DEFINITION,
        "method": "area: each pixel footprint is shared among the bins it overlaps in proportion to overlap area; bin value = area-weighted mean of the valid pixels, independently per index",
        "bands_definition": "rhos_*: area-weighted mean over pixels that are primary valid water with all nine rhos bands finite; rhos_valid_area is their footprint area",
        "masks_definition": "*_any: at least one overlapping native observation has the mask set (valid_water_mask is primary_retained_any)",
        "native_variables_missing": json.dumps(sorted(absent)),
        "cloud_diagnostic": "cloud_excluded_any: at least one overlapping native observation has cloud_excluded_mask=1; not a whole-bin cloud classification",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "software_version": __import__("cyanotrend").__version__,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
            ds.setncatts(attrs)
            ds.createDimension("bin", int(row.size))
            spec = [
                ("bin", "i8", bins["key"], "ISIN bin number (1-based, NASA Level-3 numbering)", "1"),
                ("row", "i4", row, "ISIN row, counted from 90 S", "1"),
                ("col", "i4", col, "bin position in its row, counted east from 180 W", "1"),
                ("latitude", "f8", lat, "bin-centre latitude", "degrees_north"),
                ("longitude", "f8", lon, "bin-centre longitude", "degrees_east"),
                ("bin_area", "f4", bin_area_m2(row), "spherical area of the bin", "m2"),
                ("footprint_area", "f4", bins["weight"], "pixel footprint area overlapping the bin", "m2"),
            ]
            for name in INDICES:
                spec.append((f"{name}_valid_area", "f4", bins[f"{name}_weight"],
                             f"footprint area of valid {name} pixels overlapping the bin", "m2"))
                spec.append((f"{name}_area_mean", "f4", means[name],
                             f"area-weighted mean of valid {name} pixels overlapping the bin (NaN: none)", "1"))
            spec.append(("rhos_valid_area", "f4", bins["bands_weight"],
                         "footprint area of pixels valid for all rhos bands overlapping the bin", "m2"))
            for name in BANDS:
                spec.append((f"{name}_area_mean", "f4", band_avg[name],
                             f"area-weighted mean of valid {name} pixels overlapping the bin (NaN: none)", "1"))
            for name in FLAGS:
                spec.append((f"{name}_any", "u1", bins[name].astype(np.uint8),
                             f"at least one overlapping native observation is {name}", "1"))
            for name in MASKS:
                spec.append((f"{name}_any", "u1", bins[name].astype(np.uint8),
                             f"at least one overlapping native observation has {name} set", "1"))
            for name, dtype, values, long_name, units in spec:
                var = ds.createVariable(name, dtype, ("bin",), zlib=True, complevel=4, shuffle=True, fill_value=False)
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
        "bins": int(row.size),
        "native_pixels": n_pixels,
    }


def read_scene_grid(path):
    """Arrays sorted by bin number, plus attributes; refuses other grids or schemas."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        attrs = {k: ds.getncattr(k) for k in ds.ncattrs()}
        if attrs.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported scene bin file schema in {Path(path).name}")
        definition = json.loads(attrs["grid_definition"])
        if (definition.get("rows") != N_ROWS
                or not np.isclose(definition.get("earth_radius_m", 0.0), EARTH_RADIUS_M)):
            raise ValueError(f"{Path(path).name} uses a different grid")
        key = np.ma.filled(ds.variables["bin"][:], 0).astype(np.int64)
        values = {name: np.ma.filled(ds.variables[f"{name}_area_mean"][:], np.nan).astype(float) for name in INDICES}
        flags = {name: np.ma.filled(ds.variables[f"{name}_any"][:], 0) == 1 for name in FLAGS}
        bands = {name: np.ma.filled(ds.variables[f"{name}_area_mean"][:], np.nan).astype(float) for name in BANDS}
        masks = {name: np.ma.filled(ds.variables[f"{name}_any"][:], 0) == 1 for name in MASKS}
        footprint = np.ma.filled(ds.variables["footprint_area"][:], 0.0).astype(float)
    order = np.argsort(key, kind="stable")
    return {
        "attrs": attrs,
        "key": key[order],
        "values": {k: v[order] for k, v in values.items()},
        "flags": {k: v[order] for k, v in flags.items()},
        "bands": {k: v[order] for k, v in bands.items()},
        "masks": {k: v[order] for k, v in masks.items()},
        "footprint": footprint[order],
    }
