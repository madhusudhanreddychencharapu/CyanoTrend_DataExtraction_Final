"""Hydrolakes functions selected from the v2.6.8 reference implementation.

See docs/SCIENCE_PARITY.md for the source audit and operational adaptations.
"""

from __future__ import annotations
import geopandas as gpd


def erode_lake_geometries(
    lakes: gpd.GeoDataFrame, distance_m: float
) -> gpd.GeoDataFrame:
    result = lakes.copy()
    if distance_m <= 0 or result.empty:
        return result
    local_crs = result.estimate_utm_crs() or "EPSG:6933"
    projected = result.to_crs(local_crs)
    projected.geometry = projected.geometry.buffer(-float(distance_m))
    projected = projected[~projected.geometry.is_empty].copy()
    return projected.to_crs(4326)
