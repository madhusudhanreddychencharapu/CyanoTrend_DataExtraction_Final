"""Adaptive window decisions copied from reference cell 42."""
from __future__ import annotations
import math
import hashlib
from typing import Any
import numpy as np
import geopandas as gpd
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union
from .catalogue import scene_geometry
from .settings import (PROCESSING_STRATEGY, L2_WINDOW_MARGIN_KM,
    MAX_L2_WINDOWS_PER_SCENE, FULL_SCENE_FRACTION_THRESHOLD)

# -----------------------------------------------------------------------------
# Adaptive scene window planner
# -----------------------------------------------------------------------------
def _expanded_lonlat_box(bounds, margin_km: float):
    west, south, east, north = map(float, bounds)
    lat0 = float(np.clip((south + north) / 2.0, -85.0, 85.0))
    dlat = float(margin_km) / 111.32
    dlon = float(margin_km) / max(111.32 * math.cos(math.radians(lat0)), 1.0)
    return shapely_box(max(-180, west-dlon), max(-90, south-dlat),
                       min(180, east+dlon), min(90, north+dlat))


def plan_scene_windows(
    scene: dict[str, Any], scene_lakes: gpd.GeoDataFrame,
    strategy: str = PROCESSING_STRATEGY,
    margin_km: float = L2_WINDOW_MARGIN_KM,
    max_windows: int = MAX_L2_WINDOWS_PER_SCENE,
    full_scene_fraction: float = FULL_SCENE_FRACTION_THRESHOLD,
) -> list[tuple[float, float, float, float] | None]:
    if str(strategy).lower() == "full_scene":
        return [None]
    geom = scene_geometry(scene)
    if geom is None or scene_lakes is None or scene_lakes.empty:
        return [None]
    minx, miny, maxx, maxy = geom.bounds
    if maxx - minx > 170:  # dateline geometry: prefer safe full scene
        return [None]
    boxes = [_expanded_lonlat_box(g.bounds, margin_km) for g in scene_lakes.geometry]
    merged = unary_union(boxes)
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    windows = []
    for part in parts:
        clipped = part.intersection(geom)
        if clipped.is_empty:
            continue
        west, south, east, north = clipped.bounds
        if west < east and south < north:
            windows.append((float(west), float(south), float(east), float(north)))
    if not windows or len(windows) > int(max_windows):
        return [None]
    scene_area = max((maxx-minx)*(maxy-miny), 1e-9)
    window_area = sum(max(0, e-w)*max(0, n-s) for w, s, e, n in windows)
    if window_area / scene_area >= float(full_scene_fraction):
        return [None]
    return sorted(windows, key=lambda b: (b[1], b[0]))


def window_id(window) -> str:
    if window is None:
        return "FULL"
    text = ",".join(f"{v:.6f}" for v in window)
    return "W_" + hashlib.sha1(text.encode()).hexdigest()[:10]


def lakes_for_window(lakes: gpd.GeoDataFrame, window) -> gpd.GeoDataFrame:
    if window is None:
        return lakes.copy().reset_index(drop=True)
    geom = shapely_box(*window)
    return lakes[lakes.intersects(geom)].copy().reset_index(drop=True)

