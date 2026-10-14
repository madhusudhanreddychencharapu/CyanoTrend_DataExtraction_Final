"""Whole-lake counts of original daily NASA CyAN DN cells; never resample.

CMR footprints select candidate files only. Actual raster extents establish full
coverage, and pixel centres in the original HydroLAKES polygon select counts.
NASA V6: DN 0 below detection, 1..253 detections, 254 land, 255 no data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import netrc
from pathlib import Path
import re
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
import rasterio
from rasterio.windows import Window, from_bounds
import requests
from shapely import contains_xy, union_all
from shapely.geometry import box
from shapely.ops import transform as transform_geometry

from .common import atomic_json, digest, file_lock, utcnow
from .schema import (COLUMNS, COMPARISON_COLUMNS, COMPARISON_LOCAL_COLUMNS,
                     NASA_DETECTION_COLUMN, NASA_MEASUREMENT_COLUMNS)
from .telemetry import redact

LOG = logging.getLogger(__name__)
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
COLLECTION = "C3416412382-OB_CLOUD"  # NASA Merged S3 OLCI mapped CI, V6.0
GETFILE = "https://oceandata.sci.gsfc.nasa.gov/getfile/"
REGION_CRS = {"CONUS": 5070, "AK": 3338}
PRODUCT_PATTERN = re.compile(
    r"^L(?P<date>\d{7})\.L3m_DAY_CYAN(?P<ak>AK)?_CI_cyano_CYAN_"
    r"(?P<region>CONUS|AK)_300m(?:_(?P<tile>\d+_\d+))?\.tif$"
)


class CyanUnavailable(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def utc_date(acquisition_start):
    stamp = datetime.fromisoformat(str(acquisition_start).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Scene acquisition_start requires a timezone")
    return stamp.astimezone(timezone.utc).date()


def parse_catalogue(entries, day):
    """Ignore weekly/other-day products. CMR polygons are lat/lon corner pairs."""
    records = {}
    for entry in entries:
        name = entry.get("producer_granule_id", "")
        match = PRODUCT_PATTERN.fullmatch(name)
        if not match or match["date"] != day.strftime("%Y%j"):
            continue
        region = match["region"]
        if bool(match["ak"]) != (region == "AK"):
            continue
        xy = []
        projector = Transformer.from_crs(4326, REGION_CRS[region], always_xy=True)
        for polygon in entry.get("polygons", []):
            for ring in polygon:
                values = np.asarray([float(v) for v in ring.split()]).reshape(-1, 2)
                x, y = projector.transform(values[:, 1], values[:, 0])
                xy.extend(zip(x, y))
        if not xy:
            raise CyanUnavailable("catalogue_error", f"NASA footprint missing: {name}")
        coords = np.asarray(xy)
        # CMR's rounded geographic corners can lose tens of metres. This pad
        # selects extra candidate files only; it NEVER expands the count mask.
        bounds = [float(coords[:, 0].min()-100), float(coords[:, 1].min()-100),
                  float(coords[:, 0].max()+100), float(coords[:, 1].max()+100)]
        records[name] = {
            "filename": name, "region": region, "tile": match["tile"] or "ALL",
            "selection_bounds": bounds, "url": GETFILE + name,
            "cmr_granule_id": entry.get("id"), "cmr_updated": entry.get("updated"),
            "collection_concept_id": COLLECTION,
        }
    return [records[name] for name in sorted(records)]


def discover_tiles(day):
    """Bounded, sequential public metadata requests; no credentials required."""
    entries = []
    with requests.Session() as session:
        session.headers["User-Agent"] = "CyanoTrend/NASA-CyAN-comparison"
        for page in range(1, 11):
            response = session.get(CMR_URL, params={
                "collection_concept_id": COLLECTION,
                "temporal": f"{day}T00:00:00Z,{day}T23:59:59Z",
                "page_size": 200, "page_num": page,
            }, timeout=(30, 90))
            response.raise_for_status()
            batch = response.json()["feed"]["entry"]
            entries.extend(batch)
            if len(batch) < 200:
                return parse_catalogue(entries, day)
    raise CyanUnavailable("catalogue_error", "NASA catalogue pagination exceeded 2000 granules")


class EarthdataSession(requests.Session):
    def rebuild_auth(self, prepared_request, response):
        old = urlparse(response.request.url).hostname
        new = urlparse(prepared_request.url).hostname
        # Only preserve credentials between the explicitly approved NASA hosts.
        allowed = {"urs.earthdata.nasa.gov", "oceandata.sci.gsfc.nasa.gov"}
        if old != new and not ({old, new} <= allowed):
            prepared_request.headers.pop("Authorization", None)


def raster_info(path):
    with rasterio.open(path) as source:
        crs = CRS.from_user_input(source.crs) if source.crs else None
        if (crs is None or not crs.is_projected or crs.coordinate_operation is None
                or "albers" not in crs.coordinate_operation.method_name.lower()):
            raise CyanUnavailable("invalid_raster", "NASA raster must use Albers Equal Area")
        if source.count != 1 or source.dtypes[0] != "uint8":
            raise CyanUnavailable("invalid_raster", "NASA DN raster must be single-band uint8")
        t = source.transform
        if t.b != 0 or t.d != 0 or t.a <= 0 or t.e >= 0:
            raise CyanUnavailable("invalid_raster", "Unexpected rotated/reversed NASA grid")
        unit_x, unit_y = (axis.unit_conversion_factor for axis in crs.axis_info[:2])
        return {
            "crs": crs.to_wkt(), "transform": list(t)[:6],
            "width": source.width, "height": source.height,
            "bounds": list(source.bounds), "tags": source.tags(),
            "pixel_area_km2": abs(t.a*t.e-t.b*t.d)*unit_x*unit_y/1e6,
            "nodata": source.nodata,
        }


def fetch_tile(record, cache):
    """Atomic, locked shared cache. Never store or log credentials."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    name = record["filename"]
    if not PRODUCT_PATTERN.fullmatch(name):
        raise ValueError("Invalid NASA product filename")
    path = cache / name
    with file_lock(cache / f".{name}.lock"):
        if path.exists():
            raster_info(path)
            return path
        credentials = netrc.netrc().authenticators("urs.earthdata.nasa.gov")
        if not credentials:
            raise CyanUnavailable("download_error", "Earthdata credentials are not configured")
        temporary = path.with_suffix(".tif.part")
        try:
            with EarthdataSession() as session:
                session.auth = (credentials[0], credentials[2])
                session.headers["User-Agent"] = "CyanoTrend/NASA-CyAN-comparison"
                with session.get(GETFILE + name, stream=True, timeout=(30, 180)) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as target:
                        for chunk in response.iter_content(1024*1024):
                            if chunk:
                                target.write(chunk)
            info = raster_info(temporary)
            temporary.replace(path)
            atomic_json(cache / f"{name}.json", {
                **record, **info, "downloaded_utc": utcnow(), "sha256": digest(path),
            })
        finally:
            temporary.unlink(missing_ok=True)
    return path


def projected_lake(geometry, crs):
    # Densification preserves geographic polygon edges in a curved projection.
    return transform_geometry(Transformer.from_crs(4326, crs, always_xy=True).transform,
                              geometry.segmentize(0.01))


def count_lake_cells(geometry, paths):
    """Require full polygon coverage; merge integer grid addresses, not rasters."""
    paths = list(dict.fromkeys(Path(p) for p in paths))
    if not paths:
        raise CyanUnavailable("unavailable", "No daily NASA raster available")
    infos = [raster_info(p) for p in paths]
    crs = CRS.from_wkt(infos[0]["crs"])
    from affine import Affine
    anchor = Affine(*infos[0]["transform"])
    offsets = []
    for info in infos:
        t = Affine(*info["transform"])
        if CRS.from_wkt(info["crs"]) != crs or not np.allclose(
                [t.a, t.e], [anchor.a, anchor.e], rtol=0, atol=1e-8):
            raise CyanUnavailable("incompatible_grid", "NASA tiles have incompatible CRS or pixel size")
        col, row = (~anchor) * (t.c, t.f)
        if not np.allclose([col, row], np.rint([col, row]), rtol=0, atol=1e-6):
            raise CyanUnavailable("incompatible_grid", "NASA tiles are not on the same original pixel grid")
        offsets.append((int(round(row)), int(round(col))))
    lake = projected_lake(geometry, crs)
    footprint = union_all([box(*i["bounds"]) for i in infos])
    if not footprint.covers(lake):
        raise CyanUnavailable("incomplete_coverage", "Available NASA tiles do not cover the whole lake")
    to_geo = Transformer.from_crs(crs, 4326, always_xy=True)
    selected = []
    for path, info, (row_offset, col_offset) in zip(paths, infos, offsets):
        intersection = lake.intersection(box(*info["bounds"]))
        if intersection.is_empty:
            continue
        with rasterio.open(path) as source:
            w = from_bounds(*intersection.bounds, transform=source.transform)
            c0 = max(0, int(np.floor(w.col_off))-1)
            r0 = max(0, int(np.floor(w.row_off))-1)
            c1 = min(source.width, int(np.ceil(w.col_off+w.width))+1)
            r1 = min(source.height, int(np.ceil(w.row_off+w.height))+1)
            for start in range(r0, r1, 256):
                end = min(start+256, r1)
                if c1 <= c0:
                    continue
                raw = source.read(1, window=Window(c0, start, c1-c0, end-start), masked=False)
                rows, cols = np.indices(raw.shape)
                rows += start
                cols += c0
                xs = source.transform.c + (cols+0.5)*source.transform.a
                ys = source.transform.f + (rows+0.5)*source.transform.e
                lon, lat = to_geo.transform(xs, ys)
                inside = contains_xy(geometry, lon, lat)
                if inside.any():
                    selected.append(np.column_stack((rows[inside]+row_offset,
                                                     cols[inside]+col_offset, raw[inside])))
    counts = np.zeros(256, dtype=np.int64)
    if selected:
        cells = np.concatenate(selected)
        cells = cells[np.lexsort((cells[:, 1], cells[:, 0]))]
        same = np.all(cells[1:, :2] == cells[:-1, :2], axis=1)
        if np.any(same & (cells[1:, 2] != cells[:-1, 2])):
            raise CyanUnavailable("overlap_conflict", "Overlapping NASA cells have different DN values")
        unique = cells[np.r_[True, ~same], 2]
        counts = np.bincount(unique.astype(np.uint8), minlength=256)
    total = int(counts.sum())
    return {
        "NASA_CYAN_Lake_area_km2": total * infos[0]["pixel_area_km2"],
        "NASA_CYAN_Lake_pixels": total,
        "NASA_CYAN_Non_detectable_pixels": int(counts[0]),
        NASA_DETECTION_COLUMN: int(counts[1:254].sum()),
        "NASA_CYAN_Land_pixels": int(counts[254]),
        "NASA_CYAN_No_valid_data_pixels": int(counts[255]),
    }, infos


def lake_sources(geometry, records):
    candidates = []
    for region, epsg in REGION_CRS.items():
        lake = projected_lake(geometry, epsg)
        relevant = [r for r in records if r["region"] == region
                    and box(*r["selection_bounds"]).intersects(lake)]
        if relevant:
            candidates.append((region, relevant))
    return candidates


def compare_scene(stats, lakes, acquisition_start, cache):
    """Add one count to stats; keep all detailed measurements in comparison CSV."""
    day = utc_date(acquisition_start)
    result = stats.copy()
    comparison = stats[COMPARISON_LOCAL_COLUMNS].copy()
    catalogue_error = None
    try:
        records = discover_tiles(day)
    except Exception as exc:
        records = []
        catalogue_error = redact(f"{type(exc).__name__}: {exc}")[:500]
        LOG.warning("NASA CyAN catalogue unavailable for %s: %s", day, catalogue_error)
    geometries = lakes.to_crs(4326).set_index("Hylak_id").geometry
    rows, provenance, lake_warnings = [], {}, {}
    file_cache = {}

    def obtain(record):
        name = record["filename"]
        if name not in file_cache:
            try:
                file_cache[name] = fetch_tile(record, cache)
            except Exception as exc:
                file_cache[name] = exc
        if isinstance(file_cache[name], Exception):
            raise file_cache[name]
        return file_cache[name]

    for lid in comparison.Hylak_id:
        record = {key: None for key in NASA_MEASUREMENT_COLUMNS}
        record.update({"NASA_CYAN_date": day.isoformat(), "NASA_CYAN_source_files": "",
                       "NASA_CYAN_tile_ids": "", "NASA_CYAN_coverage_status": "unavailable",
                       "NASA_CYAN_status_reason": "No daily NASA coverage available"})
        used, failures = [], []
        try:
            if catalogue_error:
                raise CyanUnavailable("catalogue_error", catalogue_error)
            geometry = geometries.loc[lid]
            if geometry.is_empty or not geometry.is_valid:
                raise CyanUnavailable("invalid_geometry", "Lake polygon is empty or invalid")
            options = lake_sources(geometry, records)
            if not options:
                raise CyanUnavailable("unavailable", "No daily NASA coverage available for this lake")
            success = False
            for region, candidates in options:
                tiles = [r for r in candidates if r["tile"] != "ALL"]
                regional = [r for r in candidates if r["tile"] == "ALL"]
                # Prefer individual intersecting tiles. A single covering tile is
                # sufficient; additional tiles are collected for crossing lakes.
                paths, used = [], []
                for tile in tiles:
                    try:
                        path = obtain(tile)
                        paths.append(path)
                        used.append(tile)
                        info = raster_info(path)
                        if box(*info["bounds"]).covers(projected_lake(geometry, info["crs"])):
                            paths, used = [path], [tile]
                            break
                    except Exception as exc:
                        failures.append(redact(f"{tile['filename']}: {type(exc).__name__}: {exc}")[:500])
                attempts = [(paths, used)] if paths else []
                # Regional product can supply coverage outside the numbered tile
                # grid, or replace a missing daily tile; it is never double-added.
                for paths, used in attempts:
                    try:
                        values, infos = count_lake_cells(geometry, paths)
                        success = True
                        break
                    except CyanUnavailable as exc:
                        failures.append(f"{exc.status}: {exc}")
                if not success:
                    for tile in regional:
                        try:
                            paths, used = [obtain(tile)], [tile]
                            values, infos = count_lake_cells(geometry, paths)
                            success = True
                            break
                        except Exception as exc:
                            failures.append(redact(f"{type(exc).__name__}: {exc}")[:500])
                if success:
                    record.update(values)
                    record["NASA_CYAN_coverage_status"] = "complete"
                    record["NASA_CYAN_status_reason"] = ""
                    for tile, path, info in zip(used, paths, infos):
                        name = tile["filename"]
                        if name not in provenance:
                            provenance[name] = {**tile, **info, "sha256": digest(path)}
                    break
            if not success:
                raise CyanUnavailable("incomplete_coverage", "; ".join(failures) or "Whole-lake NASA coverage unavailable")
        except Exception as exc:
            record.update({key: None for key in NASA_MEASUREMENT_COLUMNS})
            record["NASA_CYAN_coverage_status"] = getattr(exc, "status", "error")
            record["NASA_CYAN_status_reason"] = redact(str(exc))[:1000]
            LOG.warning("NASA CyAN %s Hylak_id=%s: %s", day, lid, record["NASA_CYAN_status_reason"])
        for message in failures:
            LOG.warning("NASA CyAN %s Hylak_id=%s: %s", day, lid, message)
        if failures:
            lake_warnings[str(lid)] = failures
        record["NASA_CYAN_source_files"] = ";".join(r["filename"] for r in used)
        record["NASA_CYAN_tile_ids"] = ";".join(f"{r['region']}:{r['tile']}" for r in used)
        rows.append(record)
    nasa = pd.DataFrame(rows, index=comparison.index)
    for column in NASA_MEASUREMENT_COLUMNS[1:]:
        nasa[column] = pd.array(nasa[column], dtype="Int64")
    comparison = pd.concat([comparison, nasa], axis=1)[COMPARISON_COLUMNS]
    result[NASA_DETECTION_COLUMN] = nasa[NASA_DETECTION_COLUMN]
    metadata = {
        "date_utc": day.isoformat(), "collection_concept_id": COLLECTION,
        "catalogue_url": CMR_URL, "source_files": provenance,
        "lake_warnings": lake_warnings,
        "coverage_status_counts": comparison.NASA_CYAN_coverage_status.value_counts().to_dict(),
        "count_rule": "Original raster cell centres strictly inside full HydroLAKES polygon; no resampling",
        "dn_categories": {"non_detectable": [0], "CI_cyano_detectable": [1, 253], "land": [254], "no_valid_data": [255]},
        "area_method": "unique selected cells times abs(affine determinant), converted from CRS units to km2; all DN classes included",
        "overlap_rule": "identical integer grid addresses counted once; conflicting DN values rejected",
        "missing_policy": "blank measurements and logged warning when whole-lake coverage is unavailable",
    }
    return result[COLUMNS], comparison, metadata
