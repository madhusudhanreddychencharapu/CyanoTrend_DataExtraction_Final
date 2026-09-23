"""Catalogue functions selected from the v2.6.8 reference implementation.

See docs/SCIENCE_PARITY.md for the source audit and operational adaptations.
"""

from __future__ import annotations
from typing import Any, Iterable
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import shape as shapely_shape
from . import settings as _m_settings


def normalize_bbox(bbox: Iterable[float]) -> tuple[float, float, float, float]:
    west, south, east, north = map(float, bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(
            "bbox must be (west, south, east, north) in valid longitude/latitude ranges"
        )
    return (west, south, east, north)


def _odata_scene(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("Name", "")).removesuffix(".SEN3")
    content = item.get("ContentDate") or {}
    return {
        "id": str(item["Id"]),
        "name": name,
        "satellite": name.split("_", 1)[0] if name else "S3",
        "start": content.get("Start", ""),
        "end": content.get("End", ""),
        "size_bytes": int(item.get("ContentLength") or 0),
        "online": bool(item.get("Online", True)),
        "s3_path": item.get("S3Path"),
        "geofootprint": item.get("GeoFootprint"),
    }


def search_olci_l1_catalog(
    start_date: str,
    end_date: str,
    bbox: tuple[float, float, float, float] | None = None,
    max_products: int | None = None,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Catalogue OLCI EFR L1 scenes once; global search uses time only."""
    start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
    end_exclusive = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    filters = [
        "Collection/Name eq 'SENTINEL-3'",
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and att/OData.CSC.StringAttribute/Value eq 'OL_1_EFR___')",
        f"ContentDate/Start ge {start}T00:00:00.000Z",
        f"ContentDate/Start lt {end_exclusive}T00:00:00.000Z",
    ]
    if bbox is not None:
        west, south, east, north = normalize_bbox(bbox)
        polygon = f"POLYGON(({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))"
        filters.append(f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')")
    params = {
        "$filter": " and ".join(filters),
        "$orderby": "ContentDate/Start asc",
        "$top": min(int(page_size), 1000),
        "$select": "Id,Name,S3Path,GeoFootprint,ContentLength,ContentDate,Online",
    }
    rows: list[dict[str, Any]] = []
    url = _m_settings.CDSE_CATALOGUE_URL
    while url:
        response = requests.get(
            url,
            params=params if url == _m_settings.CDSE_CATALOGUE_URL else None,
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("value", []):
            rows.append(_odata_scene(item))
            if max_products and len(rows) >= int(max_products):
                return rows[: int(max_products)]
        next_url = payload.get("@odata.nextLink")
        if next_url and str(next_url).startswith("/"):
            next_url = "https://catalogue.dataspace.copernicus.eu" + str(next_url)
        url = next_url
        params = None
    return rows


def scene_geometry(scene: dict[str, Any]):
    payload = scene.get("geofootprint")
    if not payload:
        return None
    try:
        geom = shapely_shape(payload)
        if not geom.is_valid:
            geom = geom.buffer(0)
        return geom
    except Exception:
        return None


def candidate_lakes_for_scene(
    scene: dict[str, Any], lakes: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    geom = scene_geometry(scene)
    if geom is None or lakes is None or lakes.empty:
        return lakes.iloc[0:0].copy()
    try:
        idx = lakes.sindex.query(geom, predicate="intersects")
        candidates = lakes.iloc[np.asarray(idx, dtype=int)].copy()
    except Exception:
        candidates = lakes[lakes.intersects(geom)].copy()
    if candidates.empty:
        return candidates
    return candidates[candidates.intersects(geom)].copy().reset_index(drop=True)
