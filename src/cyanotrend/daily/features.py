"""Index names, observation flags and per-lake statistics for the daily stage.

Masks and index values are read from the native products, never recomputed here.
Binning of pixels into ISIN bins lives in grid.py.
"""
import numpy as np

INDICES = ("CI", "CI_cyano", "NDCI", "MPH", "FAI")
FLAGS = ("primary_retained", "cloud_excluded")


def summary(values):
    """Scene-style statistics over finite daily cell values, including negatives."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not values.size:
        return dict(valid_cells=0, mean="", median="", p10="", p90="", max="")
    return {"valid_cells": int(values.size), "mean": float(values.mean()),
            "median": float(np.median(values)), "p10": float(np.percentile(values, 10)),
            "p90": float(np.percentile(values, 90)), "max": float(values.max())}
