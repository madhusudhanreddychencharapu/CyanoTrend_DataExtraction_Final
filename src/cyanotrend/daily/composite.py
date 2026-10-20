"""Independent maxima in NASA L3 bins, followed by area-weighted Albers maps."""
from pathlib import Path

import netCDF4

from .schema import INDEX_PRODUCTS, DIAGNOSTIC_PRODUCTS


def has_bins(path, product):
    with netCDF4.Dataset(path) as ds:
        group = ds.groups.get("level-3_binned_data")
        if group is None or "BinList" not in group.variables or product not in group.variables:
            raise ValueError(f"Invalid L3 bin file: {path}")
        return len(group["BinList"]) > 0


def daily_bins(runner, sources, day):
    """Use one product per pass; missing platforms never borrow the other's data."""
    day_code = day.strftime("%Y%j")
    folder = runner.work / "bins"
    folder.mkdir(parents=True, exist_ok=True)
    results = {}
    products = {**INDEX_PRODUCTS, **DIAGNOSTIC_PRODUCTS}
    for label, product in products.items():
        platform_bins = {}
        for platform in ("S3A", "S3B"):
            paths = [path for scene, path in sources if scene["satellite"] == platform]
            if not paths:
                continue
            listing = folder / f"{platform}.txt"
            listing.write_text("".join(f"{Path(p).resolve()}\n" for p in paths))
            parameters = {
                "ifile": listing, "ofile": folder / f"{platform}_{label}.nc",
                "suite": "CYAN", "sday": day_code, "eday": day_code,
                "resolve": "Q", "l3bprod": product, "composite_prod": product,
                "composite_scheme": "max",
                # Actual distributed CYAN defaults; no Python bin simulation.
                "area_weighting": 0,
                "flaguse": "LAND,CLDICE,HISATZEN" if label in INDEX_PRODUCTS else "",
            }
            path = runner.run("l2bin", parameters, f"bin_{platform}_{label}")
            if path is not None and has_bins(path, product):
                platform_bins[platform] = path
        if label == "CI_cyano":
            for platform, path in platform_bins.items():
                results[platform] = (path, product)
        if not platform_bins:
            # No observations is distinct from a numerical zero.
            results[label] = None
        elif len(platform_bins) == 1:
            results[label] = (next(iter(platform_bins.values())), product)
        else:
            listing = folder / f"merge_{label}.txt"
            listing.write_text("".join(f"{p.resolve()}\n" for p in platform_bins.values()))
            path = runner.run("l3bin", {
                "infile": listing, "ofile": folder / f"merged_{label}.nc",
                "out_parm": product, "composite_prod": product, "composite_scheme": "max",
            }, f"merge_{label}")
            if not has_bins(path, product):
                raise ValueError("Merging nonempty daily inputs produced no bins")
            results[label] = (path, product)
    return results


def mapping_extent(bounds):
    west, south, east, north = map(float, bounds)
    if not (-180 <= west < east <= 180 and -90 < south < north < 90):
        raise ValueError("Daily map bounds must be WEST SOUTH EAST NORTH without a dateline crossing")
    return {"west": west, "south": south, "east": east, "north": north}


def lake_mapping_bounds(lakes):
    """Enclose all whole lakes, with padding for l3mapgen pixel rounding."""
    import numpy as np
    from pyproj import Transformer
    from ..nasa_cyan import projected_lake

    projected = [projected_lake(geometry, "EPSG:5070") for geometry in lakes.geometry]
    if not projected or any(g.is_empty for g in projected):
        raise ValueError("Cannot derive a daily grid from empty lake geometry")
    bounds = np.asarray([g.bounds for g in projected])
    # Two cells on each side protect lake edges against rounded map dimensions.
    west, south = bounds[:, :2].min(axis=0) - 600
    east, north = bounds[:, 2:].max(axis=0) + 600
    extent = Transformer.from_crs(5070, 4326, always_xy=True).transform_bounds(
        west, south, east, north, densify_pts=1000)
    mapping_extent(extent)
    return extent


def map_products(runner, bins, bounds):
    """Use NASA's CONUS projection and one explicitly chosen geographic extent.

    No per-scene bounds: every platform/index must have the same grid origin.
    The application checks domain coverage before publication.
    """
    extent = mapping_extent(bounds)
    folder = runner.work / "maps"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for label, record in bins.items():
        if record is None:
            paths[label] = None
            continue
        source, product = record
        # apply_pal=0 gives physical floating-point values in a GeoTIFF.
        path = runner.run("l3mapgen", {
            "ifile": source, "ofile": folder / f"{label}.tif",
            "suite": "CYAN", "product": product,
            "oformat": "tif", "apply_pal": 0,
            "projection": "conus", "resolution": "300m",
            "interp": "area", "fudge": 2.0,
            "mask_land": 0 if label in DIAGNOSTIC_PRODUCTS else 1,
            **extent,
        }, f"map_{label}")
        paths[label] = path
        if label in ("CI", "CI_cyano", "S3A", "S3B"):
            paths[f"{label}_DN"] = runner.run("l3mapgen", {
                "ifile": source, "ofile": folder / f"{label}_DN.tif",
                "suite": "CYAN", "product": product,
                "oformat": "tif", "apply_pal": 1,
                "projection": "conus", "resolution": "300m",
                "interp": "area", "fudge": 2.0, "mask_land": 1,
                **extent,
            }, f"map_{label}_DN")
    if not paths.get("CI_cyano_DN") and bins.get("support"):
        source, product = bins["support"]
        paths["land_DN"] = runner.run("l3mapgen", {
            "ifile": source, "ofile": folder / "land_DN.tif",
            "suite": "CYAN", "product": product,
            "oformat": "tif", "apply_pal": 1,
            "projection": "conus", "resolution": "300m",
            "interp": "area", "fudge": 2.0, "mask_land": 1,
            **extent,
        }, "map_land_DN")
    return paths
