"""Local index products added to a private CYAN L2 file, never to legacy L2."""
from pathlib import Path
import xml.etree.ElementTree as ET

import netCDF4
import numpy as np

from ..core.science import (to_float, nearest_product, product_wavelength,
                           decode_flag_metadata, flag_is_set, ndci_index,
                           additional_olci_indices)
from .schema import CUSTOM_PRODUCTS

FILL = -32767.0


def create_data_overlay(source, destination):
    """Symlink read-only tables; copy/extend only common/product.xml locally."""
    source, destination = Path(source).resolve(), Path(destination)
    if destination.exists():
        raise ValueError("Product overlay destination already exists")
    destination.mkdir(parents=True)
    for entry in source.iterdir():
        if entry.name != "common":
            (destination / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    common = destination / "common"
    common.mkdir()
    for entry in (source / "common").iterdir():
        if entry.name != "product.xml":
            (common / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    tree = ET.parse(source / "common" / "product.xml")
    root = tree.getroot()
    namespace = root.tag.partition("}")[0][1:] if root.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    def tag(name):
        return f"{{{namespace}}}{name}" if namespace else name
    existing = {p.get("name") for p in root.findall(tag("product"))}
    for name, (description, low, high) in CUSTOM_PRODUCTS.items():
        if name in existing:
            raise ValueError(f"Daily product collides with installed product: {name}")
        node = ET.SubElement(root, tag("product"), name=name)
        for field, value in (("units", "dimensionless"), ("category", "Derived"),
                           ("type", "float"), ("displayScale", "linear")):
            ET.SubElement(node, tag(field)).text = value
        limits = ET.SubElement(node, tag("range"))
        for field, value in (("validMin", low), ("validMax", high),
                           ("displayMin", 0), ("displayMax", 1)):
            ET.SubElement(limits, tag(field)).text = str(value)
        algorithm = ET.SubElement(node, tag("algorithm"))
        ET.SubElement(algorithm, tag("cat_ix")).text = "-1"
        ET.SubElement(algorithm, tag("description")).text = description
    tree.write(common / "product.xml", encoding="utf-8", xml_declaration=True)
    return destination


def block_products(arrays, waves, flags, flag_map, habs, habs_map, geolocated):
    """Cloud-free local indices; preserve NASA's own CI products separately."""
    bad = np.zeros(flags.shape, bool)
    for name in ("LAND", "CLDICE", "HISATZEN", "NAVFAIL"):
        if name not in flag_map:
            raise ValueError(f"Required L2 flag missing: {name}")
        bad |= flag_is_set(flags, flag_map, name)
    habs_cloud = np.zeros(flags.shape, bool)
    cloud_names = [name for name in habs_map if "CLOUD" in name or "CLD" in name]
    if not cloud_names:
        raise ValueError("flags_habs metadata does not identify its cloud bit")
    for name in cloud_names:
        habs_cloud |= flag_is_set(habs, habs_map, name)
    cloud = flag_is_set(flags, flag_map, "CLDICE") | habs_cloud
    clear = geolocated & ~bad & ~habs_cloud
    ndci, ndci_valid = ndci_index(arrays[665], arrays[709])
    extra = additional_olci_indices(arrays, waves)
    finite_core = np.logical_and.reduce(
        [np.isfinite(arrays[w]) & (arrays[w] > 0) for w in (665, 681, 709)]
    )
    return {
        "CT_NDCI": np.where(clear & ndci_valid, ndci, np.nan),
        "CT_MPH": np.where(clear & extra["MPH_valid"], extra["MPH"], np.nan),
        "CT_FAI": np.where(clear & extra["FAI_valid"], extra["FAI"], np.nan),
        # Zero is meaningful: support is composited without ocean flag exclusion.
        "CT_support": np.where(geolocated, 1.0, np.nan),
        "CT_clear": np.where(geolocated, clear.astype(float), np.nan),
        "CT_cloud": np.where(geolocated, cloud.astype(float), np.nan),
        "CT_ci_input": np.where(geolocated, (clear & finite_core).astype(float), np.nan),
    }


def enrich_l2(path, rows_per_block=128):
    """Calculate local indices before binning, preserving nonlinear formulas."""
    with netCDF4.Dataset(path, "r+") as ds:
        geo, nav = ds["geophysical_data"], ds["navigation_data"]
        required = ("CI_stumpf", "CI_cyano", "flags_habs", "l2_flags")
        if any(name not in geo.variables for name in required):
            raise ValueError("CYAN L2 lacks required science or QA products")
        wavelengths = (490, 560, 620, 665, 681, 709, 754, 865, 885)
        names = {w: nearest_product(geo.variables, "rhos", w) for w in wavelengths}
        waves = {w: product_wavelength(name) for w, name in names.items()}
        flag_map = decode_flag_metadata(geo["l2_flags"])
        habs_map = decode_flag_metadata(geo["flags_habs"])
        dims = geo["CI_cyano"].dimensions
        variables = {}
        for name, (description, low, high) in CUSTOM_PRODUCTS.items():
            v = geo.createVariable(name, "f4", dims, fill_value=np.float32(FILL),
                                   zlib=True, complevel=4)
            v.setncatts({"long_name": description, "units": "dimensionless",
                        "valid_min": np.float32(low), "valid_max": np.float32(high)})
            variables[name] = v
        for start in range(0, nav["latitude"].shape[0], rows_per_block):
            sl = np.s_[start:start + rows_per_block, :]
            arrays = {w: to_float(geo[name][sl]) for w, name in names.items()}
            flags = np.ma.asarray(geo["l2_flags"][sl])
            habs = np.ma.asarray(geo["flags_habs"][sl])
            valid = (np.isfinite(to_float(nav["latitude"][sl]))
                     & np.isfinite(to_float(nav["longitude"][sl]))
                     & ~np.ma.getmaskarray(flags) & ~np.ma.getmaskarray(habs))
            products = block_products(
                arrays, waves, flags.filled(0).astype(np.uint32), flag_map,
                habs.filled(0).astype(np.uint32), habs_map, valid)
            for name, values in products.items():
                variables[name][sl] = np.ma.masked_invalid(values)
        ds.setncattr("cyanotrend_daily_indices", "independent maxima; indices computed before binning")
