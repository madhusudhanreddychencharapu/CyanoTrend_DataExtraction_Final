"""Compact functions selected from the v2.6.8 reference implementation.

See docs/SCIENCE_PARITY.md for the source audit and operational adaptations.
"""

from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Any, Iterable
import geopandas as gpd
import netCDF4
import numpy as np
from scipy.ndimage import binary_dilation
from shapely import contains_xy
from shapely.geometry import box as shapely_box
from tqdm.auto import tqdm
from . import hydrolakes as _m_hydrolakes
from . import runtime as _m_runtime
from . import science as _m_science
from . import settings as _m_settings
from . import utils as _m_utils


def validate_compact_product(path, *, scene_id=None, config_hash=None):
    """Validate stored identity and complete variable access before resume reuse."""
    try:
        with netCDF4.Dataset(path) as ds:
            if "obs" not in ds.dimensions or len(ds.dimensions["obs"]) == 0:
                return False
            if scene_id is not None and str(getattr(ds, "scene_id", "")) != str(
                scene_id
            ):
                return False
            if config_hash is not None and str(
                getattr(ds, "processing_config_hash", "")
            ) != str(config_hash):
                return False
            required = [
                "latitude",
                "longitude",
                "Hylak_id",
                "l2_flags",
                "valid_water_mask",
                "cyan_strict_valid_mask",
                "bloom_rescue_mask",
                "CI",
                "CI_cyano",
                "NDCI",
                "MPH",
                "FAI",
            ]
            required += [
                f"rhos_{w}" for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS
            ]
            for name in required:
                if name not in ds.variables or ds.variables[name].shape != (
                    len(ds.dimensions["obs"]),
                ):
                    return False
                ds.variables[name][-1]
        return True
    except (OSError, RuntimeError, ValueError, KeyError):
        return False


def _append_float(var, start: int, values):
    a = np.asarray(values, dtype=np.float32)
    var[start : start + len(a)] = np.ma.masked_invalid(a)


def _membership_block(latitude, longitude, lakes: gpd.GeoDataFrame) -> np.ndarray:
    """Assign HydroLAKES IDs to one row block, using the GeoPandas spatial index first."""
    ids = np.zeros(latitude.shape, dtype=np.int64)
    if lakes is None or lakes.empty:
        return ids
    finite = np.isfinite(latitude) & np.isfinite(longitude)
    if not finite.any():
        return ids
    bwest, beast = (
        float(np.nanmin(longitude[finite])),
        float(np.nanmax(longitude[finite])),
    )
    bsouth, bnorth = (
        float(np.nanmin(latitude[finite])),
        float(np.nanmax(latitude[finite])),
    )
    block_geom = shapely_box(bwest, bsouth, beast, bnorth)
    try:
        hit_idx = lakes.sindex.query(block_geom, predicate="intersects")
        block_lakes = lakes.iloc[np.asarray(hit_idx, dtype=int)]
    except Exception:
        block_lakes = lakes
    for _, lake in block_lakes.iterrows():
        west, south, east, north = lake.geometry.bounds
        if east < bwest or west > beast or north < bsouth or (south > bnorth):
            continue
        rough = (
            finite
            & (longitude >= west)
            & (longitude <= east)
            & (latitude >= south)
            & (latitude <= north)
        )
        rr, cc = np.where(rough)
        if not rr.size:
            continue
        rs = slice(rr.min(), rr.max() + 1)
        cs = slice(cc.min(), cc.max() + 1)
        inside = contains_xy(lake.geometry, longitude[rs, cs], latitude[rs, cs])
        local = ids[rs, cs]
        local[(local == 0) & inside] = int(lake["Hylak_id"])
        ids[rs, cs] = local
    return ids


def extract_lake_only_netcdf(l2_path: Path, scene: dict[str, Any], lakes: gpd.GeoDataFrame, output_path: Path, window=None, qa_flags: Iterable[str]=_m_settings.QA_EXCLUDE_FLAGS, land_adjacency_pixels: int=_m_settings.LAND_ADJACENCY_PIXELS, require_l2_nonland: bool=_m_settings.REQUIRE_L2_NONLAND, detection_limit: float=_m_settings.CI_DETECTION_LIMIT, ss681_max: float=0.0, ss665_min: float=0.0, bright_screen: bool=_m_settings.DEFAULT_BRIGHT_SCREEN, bright_rhos865: float=_m_settings.DEFAULT_BRIGHT_RHOS865, rows_per_block: int=_m_settings.ROW_BLOCK_SIZE, config_hash: str | None=None, shore_buffer_m: float=_m_settings.DEFAULT_SHORE_BUFFER_M) -> tuple[Path | None, dict[str, Any]]:
    """Persist HydroLAKES pixels with dual QA masks.

    Cloud exclusion uses the NASA CyAN HABS cloud test (science.habs_cloud_mask)
    when rhos_443 is in the L2 file; otherwise it falls back to the generic l2gen
    CLDICE flag. CLDICE is always stored in cldice_mask as a diagnostic.
    """
    output_path = Path(output_path)
    analysis_lakes = _m_hydrolakes.erode_lake_geometries(lakes, float(shore_buffer_m)) if float(shore_buffer_m) > 0 else lakes
    if analysis_lakes is None or analysis_lakes.empty:
        return (None, {'nobs': 0, 'missing_flags': [], 'reason': 'no lake area after shoreline buffer'})
    with netCDF4.Dataset(l2_path) as src:
        geo = src.groups['geophysical_data']
        nav = src.groups['navigation_data']
        flags_var = _m_science.locate_l2_flags(src)
        flag_map = _m_science.decode_flag_metadata(flags_var)
        if 'LAND' not in flag_map:
            raise RuntimeError('L2 l2_flags lacks LAND')
        rhos_names = {w: _m_science.nearest_product(geo.variables, 'rhos', w, tolerance_nm=30) for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS}
        actual_waves = {w: _m_science.product_wavelength(rhos_names[w]) for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS}
        ndci_prefix = 'rhos' if _m_settings.NDCI_SOURCE.lower() == 'rhos' else 'Rrs'
        ndci_names = {665: _m_science.nearest_product(geo.variables, ndci_prefix, 665), 709: _m_science.nearest_product(geo.variables, ndci_prefix, 709)}
        shape_2d = nav.variables['latitude'].shape
        try:
            rhos443_name = _m_science.nearest_product(geo.variables, 'rhos', 443, tolerance_nm=5)
        except KeyError:
            rhos443_name = None
        cloud_albedo_name = 'cloud_albedo' if 'cloud_albedo' in geo.variables else None
        use_habs = _m_settings.CLOUD_MASK_METHOD.upper() == 'HABS' and rhos443_name is not None
        if _m_settings.CLOUD_MASK_METHOD.upper() == 'HABS' and not use_habs:
            print(f'WARNING: {Path(l2_path).name} has no rhos_443; HABS cloud test unavailable, falling back to CLDICE.')
        habs_valid_min, habs_valid_max = _m_science.physical_valid_range(geo.variables[rhos_names[865]])
        if use_habs:
            cloud_mask_method = 'HABS (OCSSW get_cloudmask_meris)' + ('' if cloud_albedo_name else '; cloud_albedo absent, rhos_865 used')
        else:
            cloud_mask_method = 'CLDICE (l2gen generic flag)'
        bloom_rescue_enabled = bool(_m_settings.BLOOM_RESCUE_ENABLED)
        cloud_flag_label = 'HABS_CLOUD' if use_habs else 'CLDICE'
        metadata = {'Conventions': 'CF-1.8 ACDD-1.3', 'title': 'Sentinel-3 OLCI derived lake-pixel point collection', 'schema': _m_settings.V264_SCHEMA, 'processing_config_hash': str(config_hash or ''), 'scene_id': str(scene.get('id', '')), 'scene_name': str(scene.get('name', '')), 'acquisition_start': str(scene.get('start', '')), 'acquisition_end': str(scene.get('end', '')), 'window_id': window_id(window), 'window_bbox': 'FULL' if window is None else ','.join(map(str, window)), 'source_l2': str(Path(l2_path).name), 'l2gen_version': _m_runtime.command_version(_m_settings.L2GEN_BIN), 'primary_qa_profile': _m_settings.DEFAULT_MASK_PROFILE, 'primary_hard_exclude_flags': ','.join(_m_settings.BLOOM_AWARE_HARD_FLAGS), 'cyan_strict_exclude_flags': f'LAND,{cloud_flag_label},HISATZEN,NAVFAIL_safety', 'cloud_mask_method': cloud_mask_method, 'cloud_mask_definition': 'cloud = (cloud_albedo, or rhos865 where invalid) - 3*max(0, (rhos620+rhos665+rhos681) - 3*rhos443 - (rhos754-rhos443)/(754-443)*(620+665+681-3*443)) > 0.08; un-flagged for scum when rhos560>rhos681 and NIR/red-edge exceeds blue or red; re-flagged when rhos665>0.1 and albedo>0.15; high glint (rhos865-cloud_albedo>0.25) flagged' if use_habs else 'l2gen CLDICE: cloud_albedo(865) > 0.027', 'cloud_mask_reference': 'NASA OCSSW libl1 cloud_flag.c get_cloudmask_meris; NOAA TM NOS NCCOS 296 Appendix B2 (doi:10.25923/606t-m243)', 'cldice_mask_semantics': 'generic l2gen CLDICE flag, diagnostic only' if use_habs else 'generic l2gen CLDICE flag, used for exclusion', 'conditional_cldice_bloom_recovery': int(bloom_rescue_enabled), 'bloom_rescue_definition': ('cloud AND NOT HISATZEN AND NOT NAVFAIL AND standard CIcyano spectral detection AND OLCI AFAI>0 AND rhos754>rhos490' if bloom_rescue_enabled else 'disabled: superseded by the HABS scum exceptions'), 'bloom_rescue_method_note': 'Conservative research implementation informed by the false-cloud recovery concept of Wang & Jiang 2025 (DOI 10.1016/j.jag.2025.104408); not an exact reproduction of NOAA MSL12 nAFAI/reflectance-ratio thresholds.', 'l2_flag_map_json': json.dumps({k: int(v) for k, v in flag_map.items()}, sort_keys=True), 'require_l2_nonland': 1, 'shore_buffer_m': float(shore_buffer_m), 'ci_detection_limit': float(detection_limit), 'ss681_max': float(ss681_max), 'ss665_min': float(ss665_min), 'non_lake_storage': 'omitted entirely; not dense NaN cells', 'masked_value_semantics': '_FillValue decodes as NaN', 'true_color_bands': 'rhos665,rhos560,rhos490', 'additional_indices': 'MPH, OLCI-adapted FAI/AFAI', 'l2gen_nominal_band_mapping': 'nominal 885 nm -> L2Gen rhos_884'}
        ds, out, tmp = _open_compact_writer_v264(output_path, metadata)
        nobs = 0
        missing_flags = [x for x in set(_m_settings.CYAN_STRICT_QA_FLAGS) if x not in flag_map]
        try:
            for start in tqdm(range(0, shape_2d[0], int(rows_per_block)), desc=f'Lake pixels {window_id(window)}'):
                stop = min(shape_2d[0], start + int(rows_per_block))
                rs = slice(start, stop)
                lat = _m_science.to_float(nav.variables['latitude'][rs, :])
                lon = _m_science.to_float(nav.variables['longitude'][rs, :])
                lake_ids = _membership_block(lat, lon, analysis_lakes)
                inside = lake_ids > 0
                if not inside.any():
                    continue
                flags_ma = np.ma.asarray(flags_var[rs, :])
                flags_valid = ~np.ma.getmaskarray(flags_ma)
                flags = flags_ma.filled(0).astype(np.uint32)
                geolocated = np.isfinite(lat) & np.isfinite(lon) & flags_valid
                land = _m_science.flag_is_set(flags, flag_map, 'LAND')
                cldice = _m_science.flag_is_set(flags, flag_map, 'CLDICE')
                hisatzen = _m_science.flag_is_set(flags, flag_map, 'HISATZEN')
                navfail = _m_science.flag_is_set(flags, flag_map, 'NAVFAIL')
                base = inside & geolocated & ~land
                arrays = {w: _m_science.to_float(geo.variables[rhos_names[w]][rs, :]) for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS}
                if use_habs:
                    habs_rhos = dict(arrays)
                    habs_rhos[443] = _m_science.to_float(geo.variables[rhos443_name][rs, :])
                    cldalb = _m_science.to_float(geo.variables[cloud_albedo_name][rs, :]) if cloud_albedo_name else None
                    cloud = _m_science.habs_cloud_mask(habs_rhos, cldalb, valid_min=habs_valid_min, valid_max=habs_valid_max)
                else:
                    cloud = cldice
                family = _m_science.ci_family(arrays[620], arrays[665], arrays[681], arrays[709], wavelengths=tuple((actual_waves[w] for w in _m_settings.CORE_RHOS_WAVELENGTHS)), detection_limit=detection_limit, ss681_max=ss681_max, ss665_min=ss665_min)
                extra = _m_science.additional_olci_indices(arrays, actual_waves)
                bloom_evidence = family['finite'] & family['ci_detection']
                bloom_evidence &= extra['FAI_valid'] & np.isfinite(extra['FAI']) & (extra['FAI'] > float(_m_settings.BLOOM_RESCUE_AFAI_MIN))
                bloom_evidence &= np.isfinite(arrays[490]) & np.isfinite(arrays[754]) & (arrays[754] > arrays[490])
                bloom_rescue = base & cloud & ~hisatzen & ~navfail & bloom_evidence & bloom_rescue_enabled
                cyan_strict_valid = base & ~cloud & ~hisatzen & ~navfail
                valid_water = base & ~hisatzen & ~navfail & ~cloud | bloom_rescue
                cloud_excluded = base & cloud & ~bloom_rescue
                if int(land_adjacency_pixels) > 0:
                    adjacent = binary_dilation(land, iterations=int(land_adjacency_pixels)) & ~land
                    valid_water &= ~adjacent
                    cyan_strict_valid &= ~adjacent
                if bright_screen:
                    bright = np.isfinite(arrays[865]) & (arrays[865] > float(bright_rhos865))
                    valid_water &= ~bright
                    cyan_strict_valid &= ~bright
                    bloom_rescue &= ~bright
                ci_valid = valid_water & family['finite']
                candidate = ci_valid & family['ci_candidate']
                detected = ci_valid & family['ci_detection']
                red = _m_science.to_float(geo.variables[ndci_names[665]][rs, :])
                edge = _m_science.to_float(geo.variables[ndci_names[709]][rs, :])
                ndci, ndci_finite = _m_science.ndci_index(red, edge)
                ndci_valid = valid_water & ndci_finite
                extra_valid = {k: valid_water & extra[f'{k}_valid'] for k in _m_settings.ADDITIONAL_INDEX_NAMES}
                take = inside & geolocated
                rr, cc = np.where(take)
                if not rr.size:
                    continue
                count = len(rr)
                sl = slice(nobs, nobs + count)
                out['Hylak_id'][sl] = lake_ids[rr, cc]
                out['source_row'][sl] = rr.astype(np.int32) + int(start)
                out['source_col'][sl] = cc.astype(np.int32)
                out['l2_flags'][sl] = flags[rr, cc]
                mask_map = {'valid_water_mask': valid_water, 'cyan_strict_valid_mask': cyan_strict_valid, 'bloom_rescue_mask': bloom_rescue, 'cldice_mask': cldice, 'hisatzen_mask': hisatzen, 'navfail_mask': navfail, 'cloud_excluded_mask': cloud_excluded, 'ci_valid_mask': ci_valid, 'ci_candidate_mask': candidate, 'ci_detection_mask': detected, 'ndci_valid_mask': ndci_valid, 'mph_valid_mask': extra_valid['MPH'], 'fai_valid_mask': extra_valid['FAI']}
                for name, mask in mask_map.items():
                    out[name][sl] = mask[rr, cc].astype(np.uint8)
                _append_float(out['latitude'], nobs, lat[rr, cc])
                _append_float(out['longitude'], nobs, lon[rr, cc])
                for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS:
                    _append_float(out[f'rhos_{w}'], nobs, arrays[w][rr, cc])
                _append_float(out['SS_681'], nobs, np.where(ci_valid, family['SS_681'], np.nan)[rr, cc])
                _append_float(out['SS_665'], nobs, np.where(ci_valid, family['SS_665'], np.nan)[rr, cc])
                _append_float(out['CI'], nobs, np.where(candidate, family['CI'], np.nan)[rr, cc])
                _append_float(out['CI_cyano'], nobs, np.where(detected, family['CI_cyano'], np.nan)[rr, cc])
                _append_float(out['NDCI'], nobs, np.where(ndci_valid, ndci, np.nan)[rr, cc])
                _append_float(out['MPH'], nobs, np.where(extra_valid['MPH'], extra['MPH'], np.nan)[rr, cc])
                _append_float(out['MPH_peak_nm'], nobs, np.where(extra_valid['MPH'], extra['MPH_peak_nm'], np.nan)[rr, cc])
                _append_float(out['FAI'], nobs, np.where(extra_valid['FAI'], extra['FAI'], np.nan)[rr, cc])
                nobs += count
        finally:
            ds.close()
        if nobs == 0:
            tmp.unlink(missing_ok=True)
            return (None, {'nobs': 0, 'missing_flags': missing_flags})
        if tmp.parent == output_path.parent:
            tmp.replace(output_path)
        else:
            transfer = output_path.with_suffix(output_path.suffix + '.copying')
            transfer.unlink(missing_ok=True)
            shutil.copy2(tmp, transfer)
            transfer.replace(output_path)
            tmp.unlink(missing_ok=True)
    return (output_path, {'nobs': int(nobs), 'size_bytes': int(output_path.stat().st_size), 'size_human': _m_utils.human_size(output_path.stat().st_size), 'missing_flags': missing_flags, 'window_id': window_id(window)})


def _open_compact_writer_v264(path: Path, metadata: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    ds = netCDF4.Dataset(tmp, "w", format="NETCDF4")
    ds.createDimension("obs", None)
    for key, value in metadata.items():
        if value is not None:
            try:
                ds.setncattr(key, value)
            except Exception:
                ds.setncattr(key, str(value))
    chunk = (_m_settings.COMPACT_CHUNK_OBS,)
    out = {}
    out["Hylak_id"] = ds.createVariable(
        "Hylak_id",
        "i8",
        ("obs",),
        zlib=True,
        complevel=_m_settings.COMPACT_DEFLATE,
        shuffle=True,
        chunksizes=chunk,
    )
    out["source_row"] = ds.createVariable(
        "source_row",
        "i4",
        ("obs",),
        zlib=True,
        complevel=_m_settings.COMPACT_DEFLATE,
        shuffle=True,
        chunksizes=chunk,
    )
    out["source_col"] = ds.createVariable(
        "source_col",
        "i4",
        ("obs",),
        zlib=True,
        complevel=_m_settings.COMPACT_DEFLATE,
        shuffle=True,
        chunksizes=chunk,
    )
    out["l2_flags"] = ds.createVariable(
        "l2_flags",
        "u4",
        ("obs",),
        zlib=True,
        complevel=_m_settings.COMPACT_DEFLATE,
        shuffle=True,
        chunksizes=chunk,
    )
    mask_names = (
        "valid_water_mask",
        "cyan_strict_valid_mask",
        "bloom_rescue_mask",
        "cldice_mask",
        "hisatzen_mask",
        "navfail_mask",
        "cloud_excluded_mask",
        "ci_valid_mask",
        "ci_candidate_mask",
        "ci_detection_mask",
        "ndci_valid_mask",
        "mph_valid_mask",
        "fai_valid_mask",
    )
    for name in mask_names:
        out[name] = ds.createVariable(
            name,
            "u1",
            ("obs",),
            zlib=True,
            complevel=_m_settings.COMPACT_DEFLATE,
            shuffle=True,
            chunksizes=chunk,
        )
    float_names = (
        ["latitude", "longitude"]
        + [f"rhos_{w}" for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS]
        + ["SS_681", "SS_665", "CI", "CI_cyano", "NDCI", "MPH", "MPH_peak_nm", "FAI"]
    )
    for name in float_names:
        out[name] = ds.createVariable(
            name,
            "f4",
            ("obs",),
            zlib=True,
            complevel=_m_settings.COMPACT_DEFLATE,
            shuffle=True,
            chunksizes=chunk,
            fill_value=_m_settings.DERIVED_FILL,
        )
    out["latitude"].setncatts({"standard_name": "latitude", "units": "degrees_north"})
    out["longitude"].setncatts({"standard_name": "longitude", "units": "degrees_east"})
    for w in _m_settings.GLOBAL_ANALYSIS_RHOS_WAVELENGTHS:
        out[f"rhos_{w}"].setncatts(
            {
                "long_name": f"Rayleigh-corrected OLCI reflectance near {w} nm",
                "units": "1",
            }
        )
    out["valid_water_mask"].setncattr(
        "long_name", "primary bloom-aware quantitative valid-water mask"
    )
    out["cyan_strict_valid_mask"].setncattr(
        "long_name",
        "CyAN-strict comparison mask: non-LAND, non-CLDICE, non-HISATZEN, NAVFAIL safety excluded",
    )
    out["bloom_rescue_mask"].setncattr(
        "long_name", "CLDICE pixel restored by conservative bloom spectral evidence"
    )
    out["MPH"].setncatts(
        {
            "long_name": "Maximum Peak Height reflectance index",
            "units": "1",
            "formula": "max line height at 681/709/754 nm above 665-885 nm baseline",
        }
    )
    out["MPH_peak_nm"].setncatts(
        {"long_name": "wavelength of MPH maximum", "units": "nm"}
    )
    out["FAI"].setncatts(
        {
            "long_name": "OLCI-adapted Floating Algae Index (AFAI form)",
            "units": "1",
            "formula": "rhos754 - linear baseline(rhos665,rhos865)",
            "note": "OLCI AFAI form used as one independent bloom-rescue criterion.",
        }
    )
    return (ds, out, tmp)

from .windows import window_id
