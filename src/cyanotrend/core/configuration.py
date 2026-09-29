"""Notebook scientific settings identity (cell 57)."""
import hashlib
import json
from .settings import (
    ADDITIONAL_INDEX_NAMES,
    BLOOM_AWARE_HARD_FLAGS,
    BLOOM_RESCUE_AFAI_MIN,
    BLOOM_RESCUE_REQUIRE_CI_CYANO,
    BLOOM_RESCUE_REQUIRE_NIR_OVER_BLUE,
    BLOOM_RESCUE_REQUIRE_POSITIVE_AFAI,
    CI_DETECTION_LIMIT,
    CYAN_STRICT_QA_FLAGS,
    DEFAULT_BRIGHT_SCREEN,
    DEFAULT_MASK_PROFILE,
    DEFAULT_SHORE_BUFFER_M,
    FULL_SCENE_FRACTION_THRESHOLD,
    GLOBAL_ANALYSIS_RHOS_WAVELENGTHS,
    L2_MODE,
    L2_WINDOW_MARGIN_KM,
    MAX_L2_WINDOWS_PER_SCENE,
    NDCI_SOURCE,
    PROCESSING_STRATEGY,
    SAVE_FULL_REFLECTANCE_SPECTRA,
    TRUE_COLOR_RHOS_WAVELENGTHS,
    V264_SCHEMA
)

def processing_config_hash(min_area_km2: float, region: str = "WORLD") -> str:
    payload = {
        "l2_mode": L2_MODE,
        "ndci_source": NDCI_SOURCE,
        "analysis_rhos": list(GLOBAL_ANALYSIS_RHOS_WAVELENGTHS),
        "true_color_rhos": list(TRUE_COLOR_RHOS_WAVELENGTHS),
        "indices": ["CI", "CI_cyano", "NDCI", *ADDITIONAL_INDEX_NAMES],
        "full_spectrum": bool(SAVE_FULL_REFLECTANCE_SPECTRA),
        "primary_qa_profile": DEFAULT_MASK_PROFILE,
        "primary_hard_flags": list(BLOOM_AWARE_HARD_FLAGS),
        "cyan_strict_flags": list(CYAN_STRICT_QA_FLAGS),
        "require_l2_nonland": True,
        "bloom_rescue_require_ci_cyano": bool(BLOOM_RESCUE_REQUIRE_CI_CYANO),
        "bloom_rescue_require_positive_afai": bool(BLOOM_RESCUE_REQUIRE_POSITIVE_AFAI),
        "bloom_rescue_afai_min": float(BLOOM_RESCUE_AFAI_MIN),
        "bloom_rescue_require_nir_over_blue": bool(BLOOM_RESCUE_REQUIRE_NIR_OVER_BLUE),
        "detection_limit": float(CI_DETECTION_LIMIT),
        "bright_screen": bool(DEFAULT_BRIGHT_SCREEN),
        "shore_buffer_m": float(DEFAULT_SHORE_BUFFER_M),
        "processing_strategy": PROCESSING_STRATEGY,
        "window_margin_km": float(L2_WINDOW_MARGIN_KM),
        "max_windows": int(MAX_L2_WINDOWS_PER_SCENE),
        "full_scene_fraction": float(FULL_SCENE_FRACTION_THRESHOLD),
        "min_lake_area_km2": float(min_area_km2),
        "region": str(region).upper(),
        "schema": V264_SCHEMA,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
