"""Explicit additional daily command; legacy queue and publication stay intact."""
from datetime import date
from pathlib import Path
import os
import shutil

from .. import references
from ..common import file_lock, utcnow
from .inputs import read_inputs, lake_scene_membership
from .ocssw import inventory, job_identity, process_scene, Runner
from .products import create_data_overlay
from .composite import daily_bins, map_products, mapping_extent, lake_mapping_bounds
from .statistics import build_table
from .publication import output_path, physical_tiff, dn_tiff, publish, validate_daily_archive


def region_names(workspace, selection):
    countries = references.boundaries(workspace, "ADM0")
    countries = countries[countries.country_iso3 == selection["country"]]
    states = references.boundaries(workspace, "ADM1")
    states = states[(states.country_iso3 == selection["country"])
                    & (states.shapeID.astype(str) == selection["state_id"])]
    if len(countries) != 1 or len(states) != 1:
        raise ValueError("Selected country/state names cannot be resolved uniquely")
    return str(countries.iloc[0].shapeName), str(states.iloc[0].shapeName)


def add_nasa_count(table, lakes, day, workspace):
    """Reuse the unchanged external NASA whole-lake counting implementation."""
    from ..nasa_cyan import compare_scene
    from ..schema import NASA_DETECTION_COLUMN
    # Compatibility view is in memory only, never sent to legacy publication.
    view = table.rename(columns={
        "daily_lake_pixels": "native_lake_pixels",
        "merged_CI_cyano_detection_pixels": "ci_cyano_detection_pixels",
    })
    enriched, _, metadata = compare_scene(
        view, lakes, f"{day}T00:00:00Z", workspace.references / "obpg_cyan")
    result = table.copy()
    lookup = enriched.set_index("Hylak_id")[NASA_DETECTION_COLUMN]
    result[NASA_DETECTION_COLUMN] = result.Hylak_id.map(lookup)
    return result, metadata


def run_daily(workspace, plan_path, day, source_root=None, *, tiff_encoding="nasa-dn", map_bounds=None):
    """All selected inputs succeed before the single daily publication step."""
    day = date.fromisoformat(day)
    if tiff_encoding not in ("physical", "nasa-dn"):
        raise ValueError("Unknown TIFF encoding")
    plan, scenes = read_inputs(workspace, plan_path, day)
    if plan["reference_id"] != references.manifest(workspace)["reference_id"]:
        raise ValueError("Saved plan and current reference data differ")
    country, state = region_names(workspace, plan["selection"])
    # State selects the output folder, not a clipping boundary for whole lakes.
    lakes = references.load_lakes(workspace, region="WORLD")
    membership = lake_scene_membership(lakes, scenes)
    lakes = lakes[lakes.Hylak_id.isin(membership)].copy()
    if lakes.empty:
        raise ValueError("Selected scenes cover no eligible HydroLAKES lakes")
    if map_bounds is None:
        map_bounds = lake_mapping_bounds(lakes)
    mapping_extent(map_bounds)
    installed = inventory(workspace.ocssw)
    installed["tiff_encoding"] = tiff_encoding
    installed["map_bounds_wsen"] = list(map_bounds)
    identity = job_identity(plan, scenes, day, installed)
    final = output_path(workspace, country, state, day, identity)
    work = workspace.scratch / "daily" / identity
    lock = workspace.output / ".daily-locks" / f"{identity}.lock"
    with file_lock(lock):
        if final.exists():
            return {**validate_daily_archive(final, identity), "reused": True}
        work.mkdir(parents=True, exist_ok=True)
        sources, provenance, environment = [], [], None
        from ..core import staging
        try:
            for scene in scenes:
                path, info, environment = process_scene(
                    workspace, scene, work / scene["id"], source_root)
                sources.append((scene, path))
                provenance.append(info)
            overlay = work / "ocdata"
            if overlay.exists():
                # Own private overlay only; no installed OCSSW paths are removed.
                shutil.rmtree(overlay)
            create_data_overlay(workspace.ocssw / "share", overlay)
            environment["OCDATAROOT"] = str(overlay)
            runner = Runner(workspace.ocssw, work, environment)
            bins = daily_bins(runner, sources, day)
            maps = map_products(runner, bins, map_bounds)
            table = build_table(lakes, membership, maps)
            table, nasa_metadata = add_nasa_count(table, lakes, day, workspace)
            raster = work / "DAILY_CI_cyano.tif"
            if tiff_encoding == "physical":
                physical_tiff(maps, raster)
            else:
                dn_tiff(maps, raster)
            metadata = {
                "schema": "cyanotrend-daily-1", "daily_id": identity,
                "created_utc": utcnow(), "day": day.isoformat(),
                "day_basis": "UTC plan acquisition date plus OCSSW sday/eday",
                "plan_id": plan["id"], "reference_id": plan["reference_id"],
                "map_bounds_wsen": list(map_bounds),
                "selected_region": {"country": country, "state": state},
                "lake_scope": "all whole eligible lakes intersecting selected scene footprints",
                "scenes": scenes, "l2_processing": provenance, "installed": installed,
                "commands": runner.calls, "tiff_encoding": tiff_encoding,
                "compositing": "independent maximum per index in Q L3 bins, then area mapping",
                "statistics_source": "daily 300 m Albers cells; lake pixel centres; no scene-row aggregation",
                "detection_rule": "NASA-encoded DN 1..253 on the matching platform/merged grid, not physical CI > 0",
                "daily_lake_pixels": "mapped observation support in lake; includes cloud/invalid cells",
                "cloud_rule": "cloud-excluded only where no clear daily support remains",
                "bloom_rescue": "not applied in daily branch; counts are zero, not NASA HABS recovery estimates",
                "NASA_comparison": nasa_metadata,
                "validation": "NASA tools/configuration used; numerical parity with operational NASA products not established",
            }
            return publish(final, table, raster, metadata, work)
        finally:
            staging.release_temporary_s3_credentials(
                os.environ.get("CDSE_USERNAME", ""), os.environ.get("CDSE_PASSWORD", ""))
