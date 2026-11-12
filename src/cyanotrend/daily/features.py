"""Maximum valid index values and observation diagnostics on the daily lattice.

Masks and index values are read from the native products, never recomputed here.
Footprints use the same sampling and lattice as the existing grid placement.
"""
import numpy as np

INDICES = ("CI", "CI_cyano", "NDCI", "MPH", "FAI")
FLAGS = ("primary_retained", "cloud_excluded")


def reduce_maximum(parts):
    if not parts:
        return {"key": np.zeros(0, dtype=np.int64),
                **{k: np.zeros(0, dtype=float) for k in INDICES},
                **{k: np.zeros(0, dtype=bool) for k in FLAGS}}
    key = np.concatenate([p["key"] for p in parts])
    unique, inverse = np.unique(key, return_inverse=True)
    result = {"key": unique}
    for name in INDICES:
        values = np.concatenate([p[name] for p in parts])
        maximum = np.full(unique.size, -np.inf)
        np.maximum.at(maximum, inverse, np.where(np.isfinite(values), values, -np.inf))
        result[name] = np.where(np.isfinite(maximum), maximum, np.nan)
    for name in FLAGS:
        seen = np.zeros(unique.size, dtype=bool)
        np.logical_or.at(seen, inverse, np.concatenate([p[name] for p in parts]))
        result[name] = seen
    return result


def maximum_pixels_to_cells(pixels, subsamples=None, chunk=None):
    """Each overlapping valid native observation competes independently per index."""
    from . import grid as g
    subsamples = g.SUBSAMPLES if subsamples is None else subsamples
    chunk = g.CHUNK_PIXELS if chunk is None else chunk
    x, y = g.project(pixels["lon"], pixels["lat"])
    (ax, ay), (bx, by) = g._neighbour_vectors(x, y, pixels["row"], pixels["col"])
    offsets = (np.arange(subsamples) + 0.5) / subsamples - 0.5
    u, v = (a.ravel() for a in np.meshgrid(offsets, offsets, indexing="ij"))
    parts = []
    for start in range(0, x.size, chunk):
        end = min(start + chunk, x.size)
        sx = x[start:end, None] + u[None, :] * ax[start:end, None] + v[None, :] * bx[start:end, None]
        sy = y[start:end, None] + u[None, :] * ay[start:end, None] + v[None, :] * by[start:end, None]
        row, col = g.cell_of(sx, sy)
        keep = g.inside_grid(row, col)
        if not keep.any():
            continue
        pix = np.broadcast_to(np.arange(start, end)[:, None], sx.shape)[keep]
        parts.append(reduce_maximum([{
            "key": g.cell_key(row[keep], col[keep]),
            **{name: pixels["indices"][name][pix] for name in INDICES},
            **{name: pixels["flags"][name][pix] for name in FLAGS},
        }]))
    return reduce_maximum(parts)


def summary(values):
    """Scene-style statistics over finite daily cell values, including negatives."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not values.size:
        return dict(valid_cells=0, mean="", median="", p10="", p90="", max="")
    return {"valid_cells": int(values.size), "mean": float(values.mean()),
            "median": float(np.median(values)), "p10": float(np.percentile(values, 10)),
            "p90": float(np.percentile(values, 90)), "max": float(values.max())}
