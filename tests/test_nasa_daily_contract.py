"""The shared NASA measurement path preserves counts, missingness and ZIP provenance."""
import json
import zipfile

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from cyanotrend import nasa_cyan as nasa
from cyanotrend.common import Workspace
from cyanotrend.publication import validate_archive
from cyanotrend.schema import COLUMNS, NASA_DETECTION_COLUMN, NASA_MEASUREMENT_COLUMNS
from cyanotrend.worker import build_bundle


@pytest.mark.parametrize("detected", [0, 17])
def test_measured_nasa_count_survives_shared_lookup_and_three_file_bundle(tmp_path, monkeypatch, detected):
    sid = "12345678-1234-5678-1234-567812345678"
    lakes = gpd.GeoDataFrame({"Hylak_id": [7]}, geometry=[box(-84, 43, -83, 44)], crs=4326)
    tile = {"filename": "fixture.tif", "region": "CONUS", "tile": "1_1"}
    source = tmp_path / "fixture.tif"
    source.write_bytes(b"raster transport fixture")
    monkeypatch.setattr(nasa, "discover_tiles", lambda day: [tile])
    monkeypatch.setattr(nasa, "lake_sources", lambda geometry, records: [("CONUS", records)])
    monkeypatch.setattr(nasa, "fetch_tile", lambda *a: source)
    info = {"bounds": [-180, -90, 180, 90], "crs": "EPSG:4326"}
    monkeypatch.setattr(nasa, "raster_info", lambda path: info)
    monkeypatch.setattr(nasa, "projected_lake", lambda geometry, crs: geometry)
    values = dict(zip(NASA_MEASUREMENT_COLUMNS, [1.8, 20, 18 - detected, detected, 1, 1]))
    monkeypatch.setattr(nasa, "count_lake_cells", lambda geometry, paths: (values, [info]))
    stats = pd.DataFrame([{**dict.fromkeys(COLUMNS, ""), "scene_id": sid, "Hylak_id": 7}])
    enriched, comparison, metadata = nasa.compare_scene(stats, lakes, "2024-08-14T12:00:00Z", tmp_path)
    assert enriched[NASA_DETECTION_COLUMN].tolist() == [detected]
    assert metadata["lake_results"][0][NASA_DETECTION_COLUMN] == detected
    ws = Workspace.create(tmp_path / "out")
    ws.ensure()
    raster = tmp_path / f"S3_OLCI_{sid}_ALL_SCIENCE_300m_CF.nc"
    raster.write_bytes(b"bundle fixture, not scientific NetCDF")
    scene = {"id": sid, "start": "2024-08-14T12:00:00Z"}
    archive = build_bundle(ws, scene, "ref", raster, enriched,
                           {"nasa_cyan_comparison": metadata}, comparison)
    meta, rows = validate_archive(archive, sid, "ref")
    assert rows[0][NASA_DETECTION_COLUMN] == str(detected)
    assert meta["nasa_cyan_comparison"]["lake_results"][0]["NASA_CYAN_coverage_status"] == "complete"
    with zipfile.ZipFile(archive) as z:
        assert len(z.namelist()) == 3 and sum(n.endswith(".csv") for n in z.namelist()) == 1
        contents = {n: z.read(n) for n in z.namelist()}
    # A disagreement in JSON cannot be silently published.
    meta["nasa_cyan_comparison"]["lake_results"][0][NASA_DETECTION_COLUMN] = detected + 1
    contents[f"S3_OLCI_{sid}_METADATA.json"] = json.dumps(meta).encode()
    with zipfile.ZipFile(archive, "w") as z:
        for name, value in contents.items():
            z.writestr(name, value)
    with pytest.raises(ValueError, match="differs"):
        validate_archive(archive, sid, "ref")


def test_unavailable_nasa_is_blank_not_zero(tmp_path, monkeypatch):
    lakes = gpd.GeoDataFrame({"Hylak_id": [7]}, geometry=[box(-84, 43, -83, 44)], crs=4326)
    monkeypatch.setattr(nasa, "discover_tiles", lambda day: [])
    values, metadata = nasa.measure_daily(lakes, "2024-08-14T00:00:00Z", tmp_path)
    assert pd.isna(values.iloc[0][NASA_DETECTION_COLUMN])
    assert metadata["lake_results"][0][NASA_DETECTION_COLUMN] is None
    assert metadata["coverage_status_counts"] == {"unavailable": 1}
