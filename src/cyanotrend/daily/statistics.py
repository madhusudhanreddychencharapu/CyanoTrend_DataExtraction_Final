"""One lake/day record on a shared map grid; never aggregate scene summaries."""
from contextlib import ExitStack

import numpy as np
import pandas as pd
from pyproj import CRS
import rasterio
from rasterio.windows import Window, from_bounds
from shapely import contains_xy
from shapely.geometry import box

from ..nasa_cyan import projected_lake
from .schema import INDEX_PRODUCTS, DETECTION_COLUMNS, DAILY_COLUMNS


def summary(values, positive=False):
    values = np.asarray(values, dtype=float)
    take = np.isfinite(values)
    if positive:
        take &= values > 0
    values = values[take]
    if not len(values):
        return dict(valid_pixels=0, mean=np.nan, median=np.nan, p10=np.nan,
                    p90=np.nan, max=np.nan)
    return dict(valid_pixels=len(values), mean=float(values.mean()),
                median=float(np.median(values)), p10=float(np.percentile(values, 10)),
                p90=float(np.percentile(values, 90)), max=float(values.max()))


def lake_record(lake, scenes, values):
    """All arrays have already been restricted to this lake's cell centres."""
    support = np.isfinite(values["support"]) & (values["support"] > 0)
    clear = support & np.isfinite(values["clear"]) & (values["clear"] > 0)
    cloudy = support & (values["cloud"] > 0) & ~clear
    ci_input = clear & (values["ci_input"] > 0)
    ordered = sorted({s["id"]: s for s in scenes}.values(),
                     key=lambda s: (s["start"], s["id"]))
    record = {
        "scene_id": ",".join(s["id"] for s in ordered),
        "scene_name": ",".join(s["name"] for s in ordered),
        "acquisition_start": ",".join(s["start"] for s in ordered),
        "Hylak_id": int(lake.Hylak_id),
        "Lake_name": lake.get("Lake_name", ""),
        "Lake_area_km2": float(lake.Lake_area),
        "daily_lake_pixels": int(support.sum()),
        "primary_retained_pixels": int(clear.sum()),
        # The existing field actually counts retained strict-QA pixels.
        "generic_flag_exclusion_sensitivity_pixels": int(clear.sum()),
        # No local bloom-rescue algorithm is applied to the new daily branch.
        "bloom_rescued_pixels": 0,
        "CLDICE_pixels": int(cloudy.sum()),
        "cloud_excluded_pixels": int(cloudy.sum()),
        "bloom_rescue_fraction_of_CLDICE": 0.0 if cloudy.any() else np.nan,
        "ci_input_valid_pixels": int(ci_input.sum()),
        "ci_candidate_pixels": int(((values["CI_DN"] >= 1) & (values["CI_DN"] <= 253) & clear).sum()),
        "NASA_CI_Cyano_detectable_cells": np.nan,
        **{key: lake.get(key, "") for key in ("state_name", "state_id", "latitude", "longitude")},
    }
    for key, label in zip(DETECTION_COLUMNS, ("S3A", "S3B", "CI_cyano")):
        dn = values[f"{label}_DN"]
        record[key] = int(((dn >= 1) & (dn <= 253) & clear).sum())
    for name in INDEX_PRODUCTS:
        array = np.where(clear, values[name], np.nan)
        if name in ("CI", "CI_cyano"):
            dn = values[f"{name}_DN"]
            array = np.where((dn >= 1) & (dn <= 253), array, np.nan)
        for key, value in summary(array).items():
            record[f"{name}_{key}"] = value
    return record


def validate_grid(source, anchor=None, dn=False):
    crs = CRS.from_user_input(source.crs) if source.crs else None
    if (crs is None or not crs.is_projected or crs.coordinate_operation is None
            or "albers" not in crs.coordinate_operation.method_name.lower()):
        raise ValueError("Daily maps must use Albers Equal Area")
    expected = ("uint8",) if dn else ("float32", "float64")
    if source.count != 1 or source.dtypes[0] not in expected:
        raise ValueError(f"Daily statistics require single-band rasters with dtype {expected}")
    t = source.transform
    if t.b or t.d or not np.allclose([t.a, t.e], [300, -300], atol=1e-5, rtol=0):
        raise ValueError("Daily maps must have an unrotated 300 m grid")
    if anchor is not None and (
            source.crs != anchor.crs or source.transform != anchor.transform
            or source.shape != anchor.shape):
        raise ValueError("Daily platform/index maps have different grid origins or extents")


def build_table(lakes, membership, paths):
    if not paths.get("support"):
        raise ValueError("No daily observation footprint; no ZIP published")
    records = []
    with ExitStack() as stack:
        datasets = {name: stack.enter_context(rasterio.open(path))
                    for name, path in paths.items() if path is not None}
        anchor = datasets["support"]
        for name, ds in datasets.items():
            validate_grid(ds, anchor, dn=name.endswith("_DN"))
        for _, lake in lakes.sort_values("Hylak_id").iterrows():
            geometry = projected_lake(lake.geometry, anchor.crs)
            if not box(*anchor.bounds).covers(geometry):
                raise ValueError(
                    f"Hylak_id={lake.Hylak_id} exceeds the installed CYAN CONUS map domain; "
                    "daily ZIP withheld rather than dropping part of the selected lake")
            w = from_bounds(*geometry.bounds, transform=anchor.transform)
            r0, c0 = max(0, int(np.floor(w.row_off))), max(0, int(np.floor(w.col_off)))
            r1 = min(anchor.height, int(np.ceil(w.row_off + w.height)))
            c1 = min(anchor.width, int(np.ceil(w.col_off + w.width)))
            window = Window(c0, r0, c1 - c0, r1 - r0)
            rr, cc = np.mgrid[r0:r1, c0:c1]
            x = anchor.transform.c + (cc + 0.5) * anchor.transform.a
            y = anchor.transform.f + (rr + 0.5) * anchor.transform.e
            inside = contains_xy(geometry, x, y)
            values = {}
            for name in (*INDEX_PRODUCTS, "support", "clear", "cloud", "ci_input", "S3A", "S3B",
                         "CI_DN", "CI_cyano_DN", "S3A_DN", "S3B_DN"):
                if name not in datasets:
                    values[name] = np.full(int(inside.sum()), np.nan)
                else:
                    a = datasets[name].read(1, window=window, masked=True)
                    a = np.ma.asarray(a, dtype=float).filled(np.nan)
                    if not name.endswith("_DN"):
                        # OCSSW float TIFFs can lack GDAL_NODATA metadata.
                        # OutFile.h explicitly defines these physical sentinels.
                        a[(a == -32767.0) | (a == -32766.0)] = np.nan
                    values[name] = a[inside]
            records.append(lake_record(lake, membership[int(lake.Hylak_id)], values))
    return pd.DataFrame(records, columns=DAILY_COLUMNS)
