"""Independent cell maxima, cloud diagnostics, NASA lookup and archive contract."""
import csv
import json
from unittest.mock import patch

import netCDF4
import numpy as np
import pandas as pd
import pytest

from cyanotrend.daily import grid as g
from cyanotrend.daily.features import INDICES, maximum_pixels_to_cells, summary
from cyanotrend.daily.merge import CSV_COLUMNS, merge_cells, merge_flags, run_daily
from cyanotrend.ledger import Ledger
from cyanotrend.schema import NASA_DETECTION_COLUMN
from test_daily import (_pixels, _write_native, _scene, _footprint, _block, _lakes,
                        _read, DAY, LAKE_A, workspace)


def test_maximum_overlapping_pixels_is_not_an_average_and_preserves_negatives():
    pixels = _pixels([3070, 3070], [11845, 11846], [1.0, 3.0], shift=(150, 0))
    pixels["indices"] = {"CI": np.array([1., 3.]), "CI_cyano": np.array([1., 3.]),
                         "NDCI": np.array([-0.2, -0.8]), "MPH": np.array([4., np.nan]),
                         "FAI": np.array([np.nan, np.nan])}
    pixels["flags"] = {"primary_retained": np.array([True, True]),
                       "cloud_excluded": np.array([False, True])}
    result = maximum_pixels_to_cells(pixels)
    i = np.searchsorted(result["key"], g.cell_key(3070, 11846))
    assert result["CI"][i] == 3.0
    assert result["NDCI"][i] == -0.2
    assert result["MPH"][i] == 4.0
    assert np.isnan(result["FAI"][i]) and result["cloud_excluded"][i]


def test_daily_maximum_uses_all_scenes_independently_and_does_not_count_no_view_as_cloud():
    keys = np.array([10, 11, 12, 13])
    scenes = []
    for ci, ndci, cloud in [([1, np.nan, np.nan], [-0.1, np.nan, np.nan], [False, True, True]),
                            ([3, 2, np.nan], [-0.7, -0.4, np.nan], [True, False, False]),
                            ([2, 5, np.nan], [-0.5, -0.2, np.nan], [False, False, False])]:
        scenes.append({"key": keys[:3], "maximum": {"CI_cyano": np.array(ci), "NDCI": np.array(ndci)},
                       "flags": {"primary_retained": np.isfinite(ci), "cloud_excluded": np.array(cloud)}})
    valid, values, seen = merge_cells(keys, scenes, "max")
    assert values[:2].tolist() == [3, 5]
    assert seen.tolist() == [3, 2, 0, 0]
    _, ndci, _ = merge_cells(keys, scenes, "max", "NDCI")
    assert ndci[:2].tolist() == [-0.1, -0.2]
    clouds = merge_flags(keys, scenes)["cloud_excluded"]
    assert clouds.tolist() == [True, True, True, False]
    assert (clouds & valid).sum() == 2 and (clouds & ~valid).sum() == 1
    assert summary(values)["mean"] == 4  # Mean of cell maxima, not max(scene means).
    assert summary([np.nan])["valid_cells"] == 0 and summary([np.nan])["mean"] == ""


def test_columns_preserve_first_sixteen_and_follow_scene_index_order():
    assert CSV_COLUMNS[:16] == [
        "date_utc", "Hylak_id", "Lake_name", "Lake_area_km2", "states", "state_ids",
        "latitude", "longitude", "day_status", "within_pass_method", "satellites",
        "scenes_used", "scene_ids", "scene_names", "failed_or_unavailable_scenes", "Lake_cells"]
    assert "native_lake_pixels" not in CSV_COLUMNS
    assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))
    assert [CSV_COLUMNS.index(f"{name}_valid_cells") for name in INDICES] == sorted(
        CSV_COLUMNS.index(f"{name}_valid_cells") for name in INDICES)
    for old in ("Non_detectable_cells", "CI_cyano_detectable_cells", "No_valid_data_cells",
                "Cells_valid_in_two_or_more_scenes", "CI_cyano_max", "CI_cyano_mean_detectable"):
        assert old in CSV_COLUMNS


def test_daily_outputs_include_independent_indices_cloud_counts_and_one_nasa_count(workspace):
    ws = workspace
    scenes = [_scene(f"S3{sat}_TEST", f"S3{sat}", f"{DAY}T{hour}:00:00Z", _footprint(*LAKE_A))
              for sat, hour in [("A", "12"), ("B", "14"), ("A", "16")]]
    ledger = Ledger(ws.db)
    ledger.enqueue({"id": "p", "created": "now", "reference_id": "ref", "selection": {}, "scenes": scenes},
                   ws.output / "plans/p.json")
    for i, scene in enumerate(scenes):
        _write_native(ws, scene, _block(*LAKE_A, lambda r, c: (i + 1) * 0.001))
        native = ws.archive(scene).parent / f"{scene['id']}_full_cfg_lakepixels.nc"
        with netCDF4.Dataset(native, "a") as ds:
            ds["NDCI"][:] = -0.2 if i == 0 else -0.8
            ds["MPH"][:] = 0.3 if i == 0 else 0.6
            ds["FAI"][:] = -0.01 if i == 0 else -0.04
            if i == 2:
                ds["cloud_excluded_mask"][:] = 1
                for name in ("ci_valid_mask", "ci_candidate_mask", "valid_water_mask",
                             "ndci_valid_mask", "mph_valid_mask", "fai_valid_mask"):
                    ds[name][:] = 0
        ledger.update(scene["id"], status="done", stage="complete")
    nasa = pd.DataFrame({"Hylak_id": [101], NASA_DETECTION_COLUMN: pd.array([17], dtype="Int64")})
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}), \
         patch("cyanotrend.nasa_cyan.measure_daily", return_value=(nasa, {"date_utc": DAY})) as lookup:
        result = run_daily(ws, DAY, DAY, catalogue=lambda *a, **k: scenes, lakes=_lakes().iloc[[0]])
    assert result["ok"], result
    assert lookup.call_count == 1 and lookup.call_args.args[3] == [101]
    rows, manifest, folder = _read(ws)
    row = rows[101]
    assert row[NASA_DETECTION_COLUMN] == "17"
    assert row["within_pass_method"] == "max"
    assert row["Cloud_excluded_any_scene_cells"] == row["Cloud_excluded_with_valid_daily_CI_cells"] == "25"
    assert row["Cloud_excluded_without_valid_daily_CI_cells"] == "0"
    assert float(row["NDCI_mean"]) == pytest.approx(-0.2)
    assert float(row["MPH_max"]) == pytest.approx(0.6)
    assert float(row["FAI_p10"]) == pytest.approx(-0.01)
    assert float(row["CI_cyano_mean_detectable"]) == pytest.approx(0.002)
    assert manifest["nasa_cyan_comparison"]["date_utc"] == DAY
    with netCDF4.Dataset(folder / f"{DAY}_grid.nc") as ds:
        for name in INDICES:
            assert name in ds.variables
        assert np.allclose(ds["NDCI"][:], -0.2)
    with (ws.output / "daily/master_merged_daily.csv").open() as f:
        assert next(csv.DictReader(f)) == row
