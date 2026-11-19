"""Index names, observation flags and per-lake statistics for the daily stage.

Masks and index values are read from the native products, never recomputed here.
Binning of pixels into ISIN bins lives in grid.py.
"""
import numpy as np

INDICES = ("CI", "CI_cyano", "NDCI", "MPH", "FAI")
FLAGS = ("primary_retained", "cloud_excluded")

# Daily science NetCDF: the layers of the single-scene science product, carried on ISIN bins.
# Reflectance bands. The native compact files call the 885 nm band rhos_885; the science product
# (and L2Gen) call it rhos_884. Wavelengths = core.settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS
# (a test keeps the two in step).
_RHOS_NM = (490, 560, 620, 665, 681, 709, 754, 865, 885)
NATIVE_BANDS = {f"rhos_{884 if nm == 885 else nm}": f"rhos_{nm}" for nm in _RHOS_NM}
BANDS = tuple(NATIVE_BANDS)
# Masks kept per bin as "at least one overlapping native pixel has the mask set".
# output name -> native variable. primary_retained (valid_water_mask) and cloud_excluded are FLAGS.
NATIVE_MASKS = {
    "generic_flag_exclusion_sensitivity_mask": "cyan_strict_valid_mask",
    "bloom_rescue_mask": "bloom_rescue_mask",
    "cldice_mask": "cldice_mask",
    "hisatzen_mask": "hisatzen_mask",
    "navfail_mask": "navfail_mask",
    "ci_valid_mask": "ci_valid_mask",
    "ci_candidate_mask": "ci_candidate_mask",
    "ci_detection_mask": "ci_detection_mask",
    "mph_valid_mask": "mph_valid_mask",
    "fai_valid_mask": "fai_valid_mask",
}
MASKS = tuple(NATIVE_MASKS)
FLAG_LAYER = {"primary_retained": "valid_water_mask", "cloud_excluded": "cloud_excluded_mask"}


def summary(values):
    """Scene-style statistics over finite daily cell values, including negatives."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not values.size:
        return dict(valid_cells=0, mean="", median="", p10="", p90="", max="")
    return {"valid_cells": int(values.size), "mean": float(values.mean()),
            "median": float(np.median(values)), "p10": float(np.percentile(values, 10)),
            "p90": float(np.percentile(values, 90)), "max": float(values.max())}
