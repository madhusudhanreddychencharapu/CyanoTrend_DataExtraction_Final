"""Science functions selected from the v2.6.8 reference implementation.

See docs/SCIENCE_PARITY.md for the source audit and operational adaptations.
"""

from __future__ import annotations
from typing import Any, Iterable
import netCDF4
import numpy as np
from . import settings as _m_settings


def to_float(values: Any) -> np.ndarray:
    return np.ma.asarray(values).filled(np.nan).astype(np.float32, copy=False)


def product_wavelength(name: str) -> float:
    match = _m_settings.PRODUCT_PATTERN.match(name)
    if not match:
        raise ValueError(f"Cannot parse wavelength from product name: {name}")
    return float(match.group("wavelength"))


def nearest_product(
    variables: Iterable[str], prefix: str, target: float, tolerance_nm: float = 12.0
) -> str:
    candidates = []
    for name in variables:
        if not name.startswith(prefix + "_"):
            continue
        try:
            wavelength = product_wavelength(name)
        except ValueError:
            continue
        candidates.append((abs(wavelength - target), name))
    if not candidates or min(candidates)[0] > tolerance_nm:
        raise KeyError(f"No {prefix} product within {tolerance_nm} nm of {target}")
    return min(candidates)[1]


def decode_flag_metadata(variable: netCDF4.Variable) -> dict[str, np.uint32]:
    meanings = str(getattr(variable, "flag_meanings", "")).replace(",", " ").split()
    masks = np.asarray(getattr(variable, "flag_masks", []), dtype=np.uint32).ravel()
    if len(meanings) != len(masks):
        raise RuntimeError(
            "l2_flags flag_meanings and flag_masks metadata do not align"
        )
    return {meaning.upper(): np.uint32(mask) for meaning, mask in zip(meanings, masks)}


def flag_is_set(
    flags: np.ndarray, flag_map: dict[str, np.uint32], name: str
) -> np.ndarray:
    mask = flag_map.get(name.upper())
    if mask is None:
        return np.zeros(flags.shape, dtype=bool)
    return flags.astype(np.uint32, copy=False) & mask != 0


def locate_l2_flags(root: netCDF4.Dataset) -> netCDF4.Variable:
    for group_name in ("geophysical_data", "navigation_data"):
        group = root.groups.get(group_name)
        if group is not None and "l2_flags" in group.variables:
            return group.variables["l2_flags"]
    if "l2_flags" in root.variables:
        return root.variables["l2_flags"]
    raise KeyError("l2_flags is absent from the L2 file")


def ci_family(
    r620: np.ndarray,
    r665: np.ndarray,
    r681: np.ndarray,
    r709: np.ndarray,
    wavelengths: tuple[float, float, float, float] = (620.0, 665.0, 681.0, 709.0),
    detection_limit: float = _m_settings.CI_DETECTION_LIMIT,
    ss681_max: float = 0.0,
    ss665_min: float = 0.0,
) -> dict[str, np.ndarray]:
    """Two-stage CI family with explicit, tunable spectral gates."""
    r620, r665, r681, r709 = [
        np.asarray(array, dtype=np.float32) for array in (r620, r665, r681, r709)
    ]
    w620, w665, w681, w709 = map(float, wavelengths)
    finite = (
        np.isfinite(r620) & np.isfinite(r665) & np.isfinite(r681) & np.isfinite(r709)
    )
    ss681 = r681 - r665 - (r709 - r665) * ((w681 - w665) / (w709 - w665))
    ss665 = r665 - r620 - (r681 - r620) * ((w665 - w620) / (w681 - w620))
    ss681 = np.where(finite, ss681, np.nan).astype(np.float32)
    ss665 = np.where(finite, ss665, np.nan).astype(np.float32)
    candidate = finite & (ss681 < float(ss681_max))
    ci = np.where(candidate, -ss681, np.nan).astype(np.float32)
    detection = candidate & (ss665 > float(ss665_min)) & (ci > float(detection_limit))
    return {
        "SS_681": ss681,
        "SS_665": ss665,
        "CI": ci,
        "CI_cyano": np.where(detection, ci, np.nan).astype(np.float32),
        "finite": finite,
        "ci_candidate": candidate,
        "ci_detection": detection,
    }


def ndci_index(red: np.ndarray, red_edge: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    red = np.asarray(red, dtype=np.float32)
    red_edge = np.asarray(red_edge, dtype=np.float32)
    denominator = red_edge + red
    valid = np.isfinite(red) & np.isfinite(red_edge) & (np.abs(denominator) > 1e-08)
    result = np.full(red.shape, np.nan, dtype=np.float32)
    np.divide(red_edge - red, denominator, out=result, where=valid)
    return (result, valid)


def _spectral_line_height(center, left, right, wl_center, wl_left, wl_right):
    center = np.asarray(center, dtype=float)
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    baseline = left + (right - left) * (
        (float(wl_center) - float(wl_left)) / (float(wl_right) - float(wl_left))
    )
    return center - baseline


def additional_olci_indices(
    rhos: dict[int, np.ndarray], wavelengths: dict[int, float] | None = None
):
    """Compute production OLCI bloom indices from Rayleigh-corrected rhos.

    MPH: maximum peak height among ~681/~709/~754 nm above a common 665--~885 nm baseline.
    FAI: OLCI-adapted floating-algae index (AFAI form), 754 nm above the 665--865 nm baseline.
         This is not the original Hu SWIR-based FAI because OLCI has no suitable SWIR band.
    """
    need = (665, 681, 709, 754, 865, 885)
    missing = [w for w in need if w not in rhos]
    if missing:
        raise KeyError(f"Missing rhos bands required for MPH/FAI: {missing}")
    wl = {w: float((wavelengths or {}).get(w, w)) for w in need}
    finite = {w: np.isfinite(rhos[w]) for w in need}
    fai = _spectral_line_height(
        rhos[754], rhos[665], rhos[865], wl[754], wl[665], wl[865]
    )
    fai_valid = finite[754] & finite[665] & finite[865]
    mph_candidates = []
    mph_valid_components = []
    for peak in (681, 709, 754):
        h = _spectral_line_height(
            rhos[peak], rhos[665], rhos[885], wl[peak], wl[665], wl[885]
        )
        v = finite[peak] & finite[665] & finite[885]
        mph_candidates.append(np.where(v, h, np.nan))
        mph_valid_components.append(v)
    stack = np.stack(mph_candidates, axis=0)
    with np.errstate(all="ignore"):
        mph = np.nanmax(stack, axis=0)
    mph_valid = np.logical_or.reduce(mph_valid_components) & np.isfinite(mph)
    peak_waves = np.asarray([wl[681], wl[709], wl[754]], dtype=np.float32)
    safe = np.where(np.isfinite(stack), stack, -np.inf)
    peak_idx = np.argmax(safe, axis=0)
    mph_peak_nm = peak_waves[peak_idx]
    mph_peak_nm[~mph_valid] = np.nan
    return {
        "MPH": mph,
        "MPH_valid": mph_valid,
        "MPH_peak_nm": mph_peak_nm,
        "FAI": fai,
        "FAI_valid": fai_valid,
    }


def habs_cloud_mask(
    rhos: dict[int, np.ndarray],
    cloud_albedo: np.ndarray | None = None,
    valid_min: float = -np.inf,
    valid_max: float = np.inf,
    threshold: float = _m_settings.HABS_CLOUD_THRESHOLD,
) -> np.ndarray:
    """NASA OCSSW HABS cloud test for MERIS/OLCI (libl1 cloud_flag.c, get_cloudmask_meris).

    Used by NASA CyAN for CI products. The turbidity signal (red bands above a
    443-754 nm baseline) is removed from the cloud albedo before a 0.08 threshold,
    then scum/bloom pixels with a green peak are un-flagged, very bright red
    targets are re-flagged, and high glint is flagged. Documented in NOAA TM NOS
    NCCOS 296 (Wynne et al. 2018/2021), Appendix B2; the rho560 > rho681 condition
    on the scum exceptions follows the OCSSW code rather than the memo.

    rhos keys: 443, 490, 560, 620, 665, 681, 709, 754, 865 (Rayleigh-corrected).
    cloud_albedo: L2Gen cloud_albedo (glint-corrected rhos at cloud_wave); where it
    is missing, NaN or negative, rhos_865 is used, as in OCSSW.
    valid_min/valid_max: rhos valid range (product.xml validMin/validMax).
    """
    r = {w: np.asarray(rhos[w], dtype=np.float32) for w in
         (443, 490, 560, 620, 665, 681, 709, 754, 865)}
    shape = r[865].shape
    ca = (np.full(shape, np.nan, np.float32) if cloud_albedo is None
          else np.asarray(cloud_albedo, dtype=np.float32))
    le = {w: r[w] <= valid_max for w in r}
    with np.errstate(invalid="ignore", divide="ignore"):
        # OCSSW only evaluates pixels whose visible/red-edge rhos are non-negative.
        evaluated = np.ones(shape, dtype=bool)
        for w in (443, 560, 620, 665, 681, 709, 754):
            evaluated &= r[w] >= 0.0
        # Turbidity signal in water.
        turb_ok = le[443] & le[665] & le[620] & le[681] & le[754]
        ftemp = (r[620] + r[665] + r[681]) - 3.0 * r[443] - (r[754] - r[443]) / (
            754.0 - 443.0
        ) * (620.0 + 665.0 + 681.0 - 3.0 * 443.0)
        ftemp = np.where(turb_ok, ftemp, 0.0)
        # Switch to rhos_865 where cloud_albedo fails, then remove turbidity.
        cld = np.where(ca >= 0.0, ca, r[865])
        cld = np.where(ftemp > 0.0, cld - 3.0 * ftemp, cld)
        cloud = cld > float(threshold)
        # Scum: NIR/red-edge bright relative to blue or red, with a green peak.
        green_peak = r[560] > r[681]
        scum_ok = le[754] & le[709] & le[681] & le[560]
        edge_nir = r[754] + r[709]
        excess = edge_nir - (r[665] + r[681])
        cloud &= ~(scum_ok & le[443] & le[490]
                   & (edge_nir > r[443] + r[490]) & (cld < 0.1) & green_peak)
        cloud &= ~(scum_ok & le[665] & (excess > 0.01) & (cld < 0.15) & green_peak)
        cloud &= ~(scum_ok & le[665] & (excess / cld > 0.1) & green_peak)
        # Very bright in the red is cloud regardless.
        cloud |= le[665] & (r[665] > 0.1) & (cld > 0.15)
        # High glint, or rhos_865 outside its valid range.
        in_range = (r[865] >= valid_min) & (r[865] <= valid_max)
        cloud |= in_range & ((r[865] - ca) > 0.25)
        cloud |= ~in_range
    return cloud & evaluated


def physical_valid_range(variable: netCDF4.Variable) -> tuple[float, float]:
    """valid_min/valid_max of an L2 variable in physical units (unpacked)."""
    def unpack(value):
        scale = float(getattr(variable, "scale_factor", 1.0))
        offset = float(getattr(variable, "add_offset", 0.0))
        return float(np.asarray(value).ravel()[0]) * scale + offset
    vmin = unpack(variable.valid_min) if hasattr(variable, "valid_min") else -np.inf
    vmax = unpack(variable.valid_max) if hasattr(variable, "valid_max") else np.inf
    if hasattr(variable, "valid_range"):
        lo, hi = np.asarray(variable.valid_range).ravel()[:2]
        vmin, vmax = unpack(lo), unpack(hi)
    return (vmin, vmax)


def kd_rhos(r442, r490, r_red_a, r_red_b, r865):
    """MERIS/OLCI Kd proxy, NOAA TM NOS NCCOS 296 Eq. 13 (uncalibrated form).

    kd = 0.7 * ((r620 + r665)/2 - r865) / ((r442 + r490)/2 - r865)
    """
    blue = (np.asarray(r442, dtype=float) + np.asarray(r490, dtype=float)) / 2.0
    red = (np.asarray(r_red_a, dtype=float) + np.asarray(r_red_b, dtype=float)) / 2.0
    r865 = np.asarray(r865, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return 0.7 * ((red - r865) / (blue - r865))


def clear_water_mask(
    rhos: dict[int, np.ndarray], kd_max: float = _m_settings.CI_CLEAR_WATER_KD_MAX
) -> np.ndarray:
    """Pixels whose CI must be set to non-detect (clear water, no algae).

    NOAA TM NOS NCCOS 296, Section 3.1 ("False-positive CI's can occur in clear
    water ... classified as non-detect") and Appendix C2 (MERIS/OLCI clear water
    correction):
      kd     = Eq. 13 Kd; kd_709 uses rho709 in place of rho620 and rho665 and
               replaces kd where larger ("709 switching modification for scum")
      ss560  = r560 - r442 + (r442 - r620) * (560 - 442) / (620 - 442)
      clear  = kd < 0.31 & kd > 0
               & (r865 <= r490 | r865 <= r665 | r865 <= r709) & ss560 < 0.01
    rhos keys: 443 (OLCI Oa3, 442.5 nm), 490, 560, 620, 665, 709, 865.
    """
    r = {w: np.asarray(rhos[w], dtype=float) for w in (443, 490, 560, 620, 665, 709, 865)}
    kd = kd_rhos(r[443], r[490], r[620], r[665], r[865])
    kd_709 = kd_rhos(r[443], r[490], r[709], r[709], r[865])
    with np.errstate(invalid="ignore"):
        kd = np.where(kd_709 > kd, kd_709, kd)
        ss560 = r[560] - r[443] + (r[443] - r[620]) * (560.0 - 442.0) / (620.0 - 442.0)
        nir_not_brightest = (r[865] <= r[490]) | (r[865] <= r[665]) | (r[865] <= r[709])
        return (kd < float(kd_max)) & (kd > 0.0) & nir_not_brightest & (ss560 < 0.01)


def mci_index(r681, r709, r754) -> np.ndarray:
    """Maximum Chlorophyll Index, NOAA TM NOS NCCOS 296 Eq. 9 (Gower et al. 1999):
    height of rho709 above the 681-754 nm baseline."""
    r681, r709, r754 = (np.asarray(a, dtype=float) for a in (r681, r709, r754))
    return r709 - r681 - (r754 - r681) * (709.0 - 681.0) / (754.0 - 681.0)


def ci_adjacency_mask(ss681: np.ndarray, clear_water: np.ndarray, mci: np.ndarray) -> np.ndarray:
    """NOAA TM NOS NCCOS 296 Section 3.1 / Appendix B2 adjacency flag:
    CI (after the clear-water correction) > 0 while MCI < 0."""
    with np.errstate(invalid="ignore"):
        ci = np.where(clear_water, 0.0, -np.asarray(ss681, dtype=float))
        return (ci > 0.0) & (np.asarray(mci) < 0.0)
