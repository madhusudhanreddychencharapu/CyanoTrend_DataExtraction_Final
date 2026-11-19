"""Daily science NetCDF: every layer of the single-scene science product, merged per solar date, on ISIN bins.

One file per state and date, beside the lakes CSV: <date>_science.nc. It carries the nine rhos bands, the
five indices (CI, CI_cyano, NDCI, MPH, FAI), the masks, DN and scenes_valid, so the lakes CSV columns can
be rebuilt from it at any time (see reproduce.py).

Grid. The cells are exactly the ISIN bins (about 290 m), not a resampled raster. An ISIN row has a fixed
latitude and a whole number of equal-width bins, so in a sinusoidal (equal-area) projection every bin is
about 290 x 290 m. The file stores each ISIN row from the south to the north edge of the state's lakes as one
raster line (north at the top), as consecutive bins:

    cell (y, x)  =  ISIN row  rmax - y,   column  col0 + j0 + x   (wrapping around the globe)

Each cell is one bin with no resampling. Neighbouring lines are offset by less than one cell, so the x
coordinate is nominal (within half a cell); `row`, `col`, `bin`, `latitude` and `longitude` give the exact
bin. The `crs` variable is the sinusoidal projection on the ISIN sphere, central meridian in the file.

Layers hold values only on lake cells (bins whose centre is inside the HydroLAKES polygon, see
lake_support_mask); other cells are fill. Data is written tile by tile and only where lakes are, so a
state with a few lakes in a large bounding box stays small.
"""
from __future__ import annotations

import json
import os
import uuid

import numpy as np

from ..common import utcnow
from ..schema import NASA_DETECTION_COLUMN
from . import grid as isin
from .features import INDICES, BANDS, MASKS, FLAG_LAYER

SCHEMA = "cyanotrend-daily-science-1"
TILE = 128
FILL = np.float32(-9999.0)
MASK_ORDER = (["valid_water_mask"] + list(MASKS[:5]) + ["cloud_excluded_mask"] + list(MASKS[5:]))
MASK_TEXT = {
    "valid_water_mask": "primary bloom-aware valid water in at least one pass",
    "generic_flag_exclusion_sensitivity_mask": "generic flag exclusion (sensitivity) mask in at least one pass",
    "bloom_rescue_mask": "bloom rescue in at least one pass",
    "cldice_mask": "generic CLDICE flag (diagnostic) in at least one pass",
    "hisatzen_mask": "HISATZEN flag in at least one pass",
    "navfail_mask": "NAVFAIL flag in at least one pass",
    "cloud_excluded_mask": "cloud excluded in at least one pass",
    "ci_valid_mask": "valid CI input in at least one pass",
    "ci_candidate_mask": "CI candidate in at least one pass",
    "ci_detection_mask": "CI detection (native threshold) in at least one pass",
    "mph_valid_mask": "valid MPH in at least one pass",
    "fai_valid_mask": "valid FAI in at least one pass",
}
INDEX_TEXT = {
    "CI": "Cyanobacteria Index candidate magnitude, daily maximum over passes",
    "CI_cyano": "cyanobacteria index detections (valid non-detections are 0), daily maximum over passes",
    "NDCI": "Normalized Difference Chlorophyll Index, daily maximum over passes",
    "MPH": "Maximum Peak Height, daily maximum over passes",
    "FAI": "OLCI-adapted Floating Algae Index (AFAI form), daily maximum over passes",
}


def layout(row, col):
    """Rectangle of whole ISIN bins that covers the given bins: per ISIN row, the first column and the cell offsets."""
    n_all = isin.bins_per_row()
    rmin, rmax = int(row.min()), int(row.max())
    rows = np.arange(rmin, rmax + 1)
    n = n_all[rows]
    lat = (rows + 0.5) * 180.0 / isin.N_ROWS - 90.0
    lon, _ = isin.bin_centre_row_col(row, col)
    lon0 = isin.central_meridian(lon)
    a = isin.EARTH_RADIUS_M * np.cos(np.radians(lat)) * np.radians(360.0 / n)  # x width of a bin in each row
    col0 = np.minimum(np.floor(((lon0 + 180.0) % 360.0) / 360.0 * n).astype(np.int64), n - 1)
    b = n * (180.0 + lon0) / 360.0
    i = row - rmin
    half = n[i] // 2
    j = (col - col0[i] + half) % n[i] - half  # bins east (+) or west (-) of the central meridian, no wrap
    x = a[i] * (col0[i] + j + 0.5 - b[i])  # sinusoidal x of the bin centre, metres
    x_lo = float(x.min()) - 0.5 * isin.BIN_HEIGHT_M
    j0 = np.ceil(x_lo / a - (col0 + 0.5 - b)).astype(np.int64)  # first bin of each line
    np.minimum.at(j0, i, j)
    ix = j - j0[i]
    return {"rmin": rmin, "rmax": rmax, "n": n, "lat": lat, "col0": col0, "j0": j0, "lon0": lon0, "x_lo": x_lo,
            "height": rmax - rmin + 1, "width": int(ix.max()) + 1, "iy": rmax - row, "ix": ix}


def _geometry(L, y0, y1, x0, x1):
    """Exact ISIN row, column, bin number, centre and area of every cell of a tile (lake or not)."""
    iy = np.arange(y0, y1)
    rows = L["rmax"] - iy
    ri = rows - L["rmin"]
    n = L["n"][ri][:, None]
    col = (L["col0"][ri][:, None] + L["j0"][ri][:, None] + np.arange(x0, x1)[None, :]) % n
    rr = np.broadcast_to(rows[:, None], col.shape)
    lon, lat = isin.bin_centre_row_col(rr, col)
    area = np.broadcast_to(isin.bin_area_m2(rows)[:, None], col.shape)
    return {"row": rr, "col": col, "bin": isin.bin_key(rr, col), "bin_area": area, "latitude": lat, "longitude": lon}


def _wkt(lon0):
    return ('PROJCS["ISIN sinusoidal (authalic sphere)",GEOGCS["Authalic sphere",DATUM["Authalic_sphere",'
            f'SPHEROID["Authalic sphere",{isin.EARTH_RADIUS_M},0]],PRIMEM["Greenwich",0],'
            'UNIT["degree",0.0174532925199433]],PROJECTION["Sinusoidal"],'
            f'PARAMETER["longitude_of_center",{lon0!r}],PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
            'UNIT["metre",1]]')


def write_daily_science(path, day, method, cells, rows, limit):
    """Write <date>_science.nc for the lakes in `cells` (see merge.merge_day for the tuple).

    rows: the CSV rows of the same lakes (lake table: day status and the NASA count).
    Returns False, writing nothing, when there are no cells.
    """
    import netCDF4
    from pathlib import Path
    from .merge import DN_SLOPE, DN_INTERCEPT

    path = Path(path)
    if not cells:
        return False

    def cat(get):
        return np.concatenate([get(c) for c in cells])

    keys = cat(lambda c: c[1])
    if not keys.size:
        return False
    row, col = isin.bin_row_col(keys)
    L = layout(row, col)
    height, width, iy, ix = L["height"], L["width"], L["iy"], L["ix"]
    lake = np.concatenate([np.full(c[1].size, c[0], dtype=np.int32) for c in cells])
    observed = cat(lambda c: c[7]["observed"])

    def f4(values):
        values = np.asarray(values, dtype=np.float64)
        return np.where(np.isfinite(values), values, float(FILL)).astype(np.float32)

    # (name, dtype, fill, tile base, per-bin values, attributes)
    layers = []
    for name in BANDS:
        layers.append((name, "f4", FILL, FILL, f4(cat(lambda c, n=name: c[7]["bands"][n])),
                       {"units": "1", "long_name": f"Rayleigh-corrected OLCI reflectance {name}, daily maximum over passes",
                        "comment": "area-weighted mean over valid pixels per pass, then highest pass value per band"}))
    for name in INDICES:
        layers.append((name, "f4", FILL, FILL, f4(cat(lambda c, n=name: c[5][n])),
                       {"units": "1", "long_name": INDEX_TEXT[name]}))
    flag_values = {FLAG_LAYER["primary_retained"]: cat(lambda c: c[6]["primary_retained"]),
                   FLAG_LAYER["cloud_excluded"]: cat(lambda c: c[6]["cloud_excluded"])}
    for name in MASKS:
        flag_values[name] = cat(lambda c, n=name: c[7]["masks"][n])
    for name in MASK_ORDER:
        layers.append((name, "i1", np.int8(-1), np.int8(-1),
                       np.where(observed, flag_values[name].astype(np.int8), np.int8(-1)).astype(np.int8),
                       {"long_name": name.replace("_", " "), "comment": MASK_TEXT[name] + "; -1: no pass observed the bin",
                        "flag_values": np.array([0, 1], dtype=np.int8), "flag_meanings": "false true",
                        "valid_range": np.array([0, 1], dtype=np.int8)}))
    layers.append(("lake_support_mask", "i1", np.int8(-1), np.int8(0), np.ones(keys.size, dtype=np.int8),
                   {"long_name": "cell is an ISIN bin whose centre lies inside a HydroLAKES polygon",
                    "flag_values": np.array([0, 1], dtype=np.int8), "flag_meanings": "false true"}))
    layers.append(("native_source_match_mask", "i1", np.int8(-1), np.int8(0), observed.astype(np.int8),
                   {"long_name": "lake cell overlapped by at least one native OLCI pixel footprint in some pass",
                    "flag_values": np.array([0, 1], dtype=np.int8), "flag_meanings": "false true"}))
    layers.append(("footprint_area", "f4", FILL, FILL, f4(cat(lambda c: c[7]["footprint"])),
                   {"units": "m2", "long_name": "largest native pixel footprint area overlapping the bin in any one pass",
                    "comment": "replaces native_match_distance_m of the 300 m product: ISIN binning shares each pixel by overlap area"}))
    layers.append(("Hylak_id", "i4", np.int32(-1), np.int32(-1), lake,
                   {"long_name": "HydroLAKES identifier of the lake whose polygon contains the bin centre"}))
    layers.append(("DN", "u1", np.uint8(255), np.uint8(255), cat(lambda c: c[2]).astype(np.uint8),
                   {"long_name": "NASA V6 CI_cyano DN of the daily maximum",
                    "comment": "0 valid below detection, 1..253 detections, 255 no valid data (also the fill outside lakes)"}))
    layers.append(("scenes_valid", "u1", np.uint8(255), np.uint8(255), cat(lambda c: c[4]).astype(np.uint8),
                   {"long_name": "number of passes with a valid CI_cyano view of the bin", "comment": "255 outside lake cells"}))
    geometry = [("bin_area", "f4", FILL, "m2", "spherical area of the bin"),
                ("row", "i4", np.int32(-1), "1", "ISIN row, counted from 90 S"),
                ("col", "i4", np.int32(-1), "1", "bin position in its ISIN row, counted east from 180 W"),
                ("bin", "i8", np.int64(0), "1", "ISIN bin number (1-based, NASA Level-3 numbering)"),
                ("latitude", "f8", np.nan, "degrees_north", "bin-centre latitude"),
                ("longitude", "f8", np.nan, "degrees_east", "bin-centre longitude")]

    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    scene_ids = sorted({s for r in rows for s in str(r.get("scene_ids", "")).split(";") if s})
    scene_names = sorted({s for r in rows for s in str(r.get("scene_names", "")).split(";") if s})
    satellites = sorted({s for r in rows for s in str(r.get("satellites", "")).split(";") if s})
    lon0 = L["lon0"]
    cell = isin.BIN_HEIGHT_M
    try:
        with netCDF4.Dataset(tmp, "w", format="NETCDF4") as ds:
            ds.setncatts({
                "Conventions": "CF-1.8",
                "title": "Sentinel-3 OLCI daily merged science product on ISIN bins (one state, lake cells)",
                "schema": SCHEMA, "date_solar": day.isoformat(), "within_pass_method": method,
                "grid_definition": json.dumps(isin.grid_definition()),
                "grid_layout": ("one cell = one ISIN bin; each ISIN row is one raster line (north at the top) of consecutive "
                                "bins; x is nominal within half a cell, row/col/bin/latitude/longitude are exact"),
                "cell_size_m": float(cell), "central_meridian_degrees": float(lon0),
                "raster_rows": int(height), "raster_columns": int(width),
                "lake_cells": int(keys.size), "lakes": int(len(cells)),
                "CI_cyano_detection_limit": float(limit),
                "dn_rule": f"NASA V6: 0 below detection, 1..253 CIcyano = 10**(DN*{DN_SLOPE}{DN_INTERCEPT:+}), 255 no valid data; 254 (land) not produced",
                "daily_rule": ("per bin and layer: each pass is the area-weighted mean of its valid pixels overlapping the bin; "
                               "the daily value is the highest pass value, independently per layer"),
                "rhos_daily_rule": "each rhos band is combined independently like an index, so a bin seen by two passes can mix passes",
                "mask_daily_rule": "masks are 1 when at least one pass set them, 0 when observed and none did, -1 when no pass observed the bin",
                "satellites": ";".join(satellites), "scene_ids": ";".join(scene_ids), "scene_names": ";".join(scene_names),
                "reproduce": "python -m cyanotrend reproduce <this file> rebuilds the lakes CSV columns from these layers and compares them",
                "created_utc": utcnow(),
            })
            ds.createDimension("y", height)
            ds.createDimension("x", width)
            chunks = (min(TILE, height), min(TILE, width))
            crs = ds.createVariable("crs", "i4", ())
            wkt = _wkt(lon0)
            crs.setncatts({"grid_mapping_name": "sinusoidal", "longitude_of_central_meridian": float(lon0),
                           "false_easting": 0.0, "false_northing": 0.0, "earth_radius": float(isin.EARTH_RADIUS_M),
                           "crs_wkt": wkt, "spatial_ref": wkt})
            yv = ds.createVariable("y", "f8", ("y",))
            yv.setncatts({"standard_name": "projection_y_coordinate", "units": "m", "long_name": "sinusoidal y of the ISIN row centre"})
            yv[:] = isin.EARTH_RADIUS_M * np.radians(L["lat"][::-1])
            xv = ds.createVariable("x", "f8", ("x",))
            xv.setncatts({"standard_name": "projection_x_coordinate", "units": "m", "long_name": "nominal sinusoidal x of the cell centre (exact within half a cell)"})
            xv[:] = L["x_lo"] + (np.arange(width) + 0.5) * cell

            handles = []
            for name, dtype, fill, base, values, attrs in layers:
                var = ds.createVariable(name, dtype, ("y", "x"), zlib=True, complevel=4, shuffle=True,
                                        chunksizes=chunks, fill_value=fill)
                var.setncatts({"coordinates": "latitude longitude", "grid_mapping": "crs", **attrs})
                handles.append((var, base, values))
            geo_handles = []
            for name, dtype, fill, units, long_name in geometry:
                var = ds.createVariable(name, dtype, ("y", "x"), zlib=True, complevel=4, shuffle=True,
                                        chunksizes=chunks, fill_value=fill)
                attrs = {"long_name": long_name, "units": units}
                if name in ("latitude", "longitude"):
                    attrs["standard_name"] = name
                else:
                    attrs["coordinates"] = "latitude longitude"
                    attrs["grid_mapping"] = "crs"
                var.setncatts(attrs)
                geo_handles.append((name, var, fill))

            tiles_x = -(-width // TILE)
            tile = (iy // TILE) * tiles_x + ix // TILE
            order = np.argsort(tile, kind="stable")
            sorted_tile = tile[order]
            starts = np.concatenate(([0], np.flatnonzero(np.diff(sorted_tile)) + 1))
            ends = np.concatenate((starts[1:], [tile.size]))
            for s, e in zip(starts, ends):
                idx = order[s:e]
                ty, tx = divmod(int(sorted_tile[s]), tiles_x)
                y0, y1 = ty * TILE, min((ty + 1) * TILE, height)
                x0, x1 = tx * TILE, min((tx + 1) * TILE, width)
                ly, lx = iy[idx] - y0, ix[idx] - x0
                for var, base, values in handles:
                    block = np.full((y1 - y0, x1 - x0), base, dtype=var.dtype)
                    block[ly, lx] = values[idx]
                    var[y0:y1, x0:x1] = block
                geo = _geometry(L, y0, y1, x0, x1)
                for name, var, fill in geo_handles:
                    var[y0:y1, x0:x1] = geo[name].astype(var.dtype)

            ordered = sorted(rows, key=lambda r: int(r["Hylak_id"]))
            ds.createDimension("lake", len(ordered))
            table = [("lake_Hylak_id", "i4", np.int32(-1), [int(r["Hylak_id"]) for r in ordered],
                      {"long_name": "HydroLAKES identifier"}),
                     ("lake_day_status", "i1", np.int8(-1), [0 if r["day_status"] == "complete" else 1 for r in ordered],
                      {"long_name": "day status of the lake", "flag_values": np.array([0, 1], dtype=np.int8),
                       "flag_meanings": "complete partial"}),
                     (f"lake_{NASA_DETECTION_COLUMN}", "i4", np.int32(-1),
                      [-1 if r.get(NASA_DETECTION_COLUMN, "") in ("", None) else int(r[NASA_DETECTION_COLUMN]) for r in ordered],
                      {"long_name": "NASA CyAN DN 1..253 cells inside the lake (verification only); -1: blank in the CSV (no NASA coverage or passes on other UTC days)"})]
            for name, dtype, fill, values, attrs in table:
                var = ds.createVariable(name, dtype, ("lake",), fill_value=fill)
                var.setncatts(attrs)
                var[:] = np.asarray(values)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True
