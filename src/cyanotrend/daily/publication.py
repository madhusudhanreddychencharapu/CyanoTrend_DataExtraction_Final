"""Atomic, validated daily ZIPs independent of master.csv and scene archives."""
import csv
import io
import json
import os
from pathlib import Path
import shutil
import zipfile

import numpy as np
import rasterio

from ..common import digest, sync_dir
from ..worker import clean_json
from .schema import DAILY_COLUMNS


def safe_component(value):
    value = str(value).strip()
    if not value or value in (".", "..") or any(c in value for c in ("/", "\\", "\0", "\n", "\r")):
        raise ValueError("Invalid daily country/state folder component")
    return value


def output_path(workspace, country, state, day, identity):
    return (workspace.output / safe_component(country) / safe_component(state)
            / day.strftime("%Y-%m") / day.isoformat()
            / f"S3_OLCI_DAILY_{day.isoformat()}_{identity}.zip")


def physical_tiff(paths, destination):
    """Physical CI values; no resampling and no invented cloud values."""
    source = paths.get("CI_cyano")
    if source is not None:
        shutil.copyfile(source, destination)
    else:
        # A completed all-cloud day still has a footprint, but no CI bins.
        with rasterio.open(paths["support"]) as anchor:
            profile = anchor.profile.copy()
            profile.update(driver="GTiff", count=1, dtype="float32", nodata=-32767.0,
                           compress="deflate")
            with rasterio.open(destination, "w", **profile) as target:
                for _, window in target.block_windows(1):
                    target.write(np.full((int(window.height), int(window.width)),
                                         -32767, np.float32), 1, window=window)
    with rasterio.open(destination, "r+") as target:
        target.nodata = -32767.0
        target.set_band_description(1, "CI_cyano")
        target.update_tags(value_encoding="physical CI_cyano", land_value="-32766",
                           processing="independent daily maximum in L3 bins; area mapping")
    return destination


def dn_tiff(paths, destination):
    source = paths.get("CI_cyano_DN")
    if source is not None:
        shutil.copyfile(source, destination)
    else:
        # l3mapgen's masked support image supplies the same static land class,
        # even when no usable CI bin exists anywhere on a completed cloudy day.
        with rasterio.open(paths["land_DN"]) as land:
            if land.count != 1 or land.dtypes[0] != "uint8":
                raise ValueError("OCSSW did not produce indexed single-band DN output")
            profile = land.profile.copy()
            profile.update(driver="GTiff", nodata=255, compress="deflate")
            with rasterio.open(destination, "w", **profile) as target:
                for _, window in land.block_windows(1):
                    data = land.read(1, window=window)
                    target.write(np.where(data == 254, 254, 255).astype("uint8"),
                                 1, window=window)
    with rasterio.open(destination, "r+") as target:
        target.nodata = 255
        target.set_band_description(1, "CI_cyano_DN")
        target.update_tags(value_encoding="NASA CyAN DN: 0 non-detection; 1-253 detection; 254 land; 255 no data")
    return destination


def validate_daily_archive(path, identity):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != 3 or archive.testzip() is not None:
            raise ValueError("Daily ZIP must contain intact CSV, TIFF and metadata")
        names = archive.namelist()
        members = {}
        for extension in (".csv", ".tif", ".json"):
            found = [name for name in names if name.endswith(extension)]
            if len(found) != 1:
                raise ValueError("Daily archive member mismatch")
            members[extension] = found[0]
        metadata = json.loads(archive.read(members[".json"]))
        if metadata["daily_id"] != identity or not metadata["complete"]:
            raise ValueError("Daily identity/completeness mismatch")
        reader = csv.DictReader(io.StringIO(archive.read(members[".csv"]).decode()))
        if reader.fieldnames != DAILY_COLUMNS:
            raise ValueError("Daily CSV schema mismatch")
        rows = list(reader)
        if not rows or len({r["Hylak_id"] for r in rows}) != len(rows):
            raise ValueError("Daily CSV must contain unique, nonempty lake rows")
        import hashlib
        for extension in (".csv", ".tif"):
            name = members[extension]
            h = hashlib.sha256()
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != metadata["sha256"][name]:
                raise ValueError(f"Daily member checksum mismatch: {name}")
    return {"archive": str(path), "daily_id": identity, "rows": len(rows), "complete": True}


def publish(path, table, raster, metadata, work):
    path, raster, work = Path(path), Path(raster), Path(work)
    if list(table.columns) != DAILY_COLUMNS or table.Hylak_id.duplicated().any():
        raise ValueError("Daily table schema or lake identity is invalid")
    if table.empty:
        raise ValueError("No daily lake rows to publish")
    with rasterio.open(raster) as ds:
        if ds.count != 1 or ds.crs is None:
            raise ValueError("Daily TIFF must be a georeferenced single-band image")
    csv_path = work / "DAILY_LAKE_STATS.csv"
    table.to_csv(csv_path, index=False)
    metadata = clean_json({
        **metadata, "complete": True, "rows": len(table),
        "sha256": {csv_path.name: digest(csv_path), raster.name: digest(raster)},
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(csv_path, csv_path.name)
            z.write(raster, raster.name)
            z.writestr("DAILY_METADATA.json", json.dumps(metadata, indent=2, allow_nan=False))
        result = validate_daily_archive(temporary, metadata["daily_id"])
        if path.exists():
            raise ValueError("Daily output already exists; refusing replacement")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "archive": str(path)}
