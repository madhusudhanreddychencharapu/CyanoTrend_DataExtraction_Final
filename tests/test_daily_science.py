"""Daily science NetCDF: every layer on exact ISIN cells, and rebuilding the lakes CSV from it."""
from datetime import date
import csv
from unittest.mock import patch

import netCDF4
import numpy as np
import pandas as pd
import pytest

from cyanotrend.common import Workspace
from cyanotrend.daily import grid as g
from cyanotrend.daily.features import BANDS, INDICES, MASKS, NATIVE_BANDS
from cyanotrend.daily.merge import CSV_COLUMNS, _write_csv, detection_limit, ensure_scene_grid, merge_extras, run_daily
from cyanotrend.daily.publication import DailyPublisher
from cyanotrend.daily.reproduce import reproduce
from cyanotrend.daily.science import _geometry, layout
from cyanotrend.ledger import Ledger
from cyanotrend.schema import NASA_DETECTION_COLUMN
from test_daily import _write_native, _scene, _footprint, _lakes, _read, DAY, LAKE_A, workspace
from test_daily_publication import row as csv_row

EXTRA_MASKS = ("cyan_strict_valid_mask", "bloom_rescue_mask", "cldice_mask", "hisatzen_mask", "navfail_mask",
               "ci_detection_mask")


def test_band_names_follow_the_pipeline_wavelengths():
    from cyanotrend.core.settings import GLOBAL_ANALYSIS_RHOS_WAVELENGTHS as waves
    assert tuple(int(native.split("_")[1]) for native in NATIVE_BANDS.values()) == tuple(waves)
    assert BANDS[-1] == "rhos_884" and NATIVE_BANDS["rhos_884"] == "rhos_885"  # the pipeline's own renaming
    assert len(BANDS) == 9 and len(MASKS) == 10


@pytest.mark.parametrize("lat0, lon0", [(27.0, -84.0), (-33.0, 151.0), (64.0, 179.8), (-17.0, -179.9), (0.0, 10.0)])
def test_every_bin_has_its_own_cell_and_the_cell_gives_the_exact_bin_back(lat0, lon0):
    lon = (lon0 + np.arange(0, 0.6, 0.002) + 180.0) % 360.0 - 180.0
    lat = lat0 + np.arange(0, 0.4, 0.002)
    mesh_lon, mesh_lat = np.meshgrid(lon, lat)
    keys = np.unique(g.bin_number(mesh_lon.ravel(), mesh_lat.ravel()))
    row, col = g.bin_row_col(keys)
    L = layout(row, col)
    assert np.unique(L["iy"] * L["width"] + L["ix"]).size == keys.size  # one cell per bin
    assert L["ix"].min() >= 0
    assert L["ix"].max() < L["width"] and L["iy"].max() < L["height"]
    geo = _geometry(L, 0, L["height"], 0, L["width"])
    assert (geo["bin"][L["iy"], L["ix"]] == keys).all()  # no resampling: a cell is exactly its bin
    assert (geo["row"][L["iy"], L["ix"]] == row).all() and (geo["col"][L["iy"], L["ix"]] == col).all()
    # x is nominal: the true sinusoidal x of every bin centre is within half a cell of its cell centre
    lon_c, lat_c = g.bin_centre_row_col(row, col)
    x_true = g.EARTH_RADIUS_M * np.cos(np.radians(lat_c)) * np.radians(g.wrap_degrees(lon_c - L["lon0"]))
    nominal = L["x_lo"] + (L["ix"] + 0.5) * g.BIN_HEIGHT_M
    assert np.abs(x_true - nominal).max() < 0.55 * g.BIN_HEIGHT_M


def test_merge_extras_combines_bands_like_indices_and_masks_with_any():
    keys = np.array([10, 11, 12, 13])

    def scene(key, band, flag, footprint):
        key = np.array(key)
        return {"key": key, "footprint": np.array(footprint, float),
                "bands": {name: np.array(band, float) for name in BANDS},
                "masks": {name: np.array(flag, bool) for name in MASKS}}

    a = scene([10, 11, 12], [0.1, np.nan, 0.3], [True, False, False], [100., 50., 80.])
    b = scene([10, 12], [0.2, np.nan], [False, False], [120., 60.])
    out = merge_extras(keys, [a, b])
    band = out["bands"]["rhos_490"]
    assert band.dtype == np.float32
    assert np.allclose(band[:3], [0.2, np.nan, 0.3], equal_nan=True) and np.isnan(band[3])  # highest valid value
    assert list(out["observed"]) == [True, True, True, False]
    assert list(out["masks"]["cldice_mask"]) == [True, False, False, False]
    assert list(out["footprint"]) == [120.0, 50.0, 80.0, 0.0]


def _day(ws):
    """Three passes over one lake: the third is cloud-only. Native files also carry rhos and masks."""
    scenes = [_scene(f"S3{sat}_TEST", f"S3{sat}", f"{DAY}T{hour}:00:00Z", _footprint(*LAKE_A))
              for sat, hour in [("A", "12"), ("B", "14"), ("A", "16")]]
    ledger = Ledger(ws.db)
    ledger.enqueue({"id": "p", "created": "now", "reference_id": "ref", "selection": {}, "scenes": scenes},
                   ws.output / "plans/p.json")
    for i, scene in enumerate(scenes):
        _write_native(ws, scene, [(LAKE_A, lambda r, c, i=i: (i + 1) * 0.001)])
        native = ws.archive(scene).parent / f"{scene['id']}_full_cfg_lakepixels.nc"
        with netCDF4.Dataset(native, "a") as ds:
            for k, name in enumerate(NATIVE_BANDS.values()):
                ds.createVariable(name, "f4", ("obs",))[:] = (i + 1) * 0.01 + k * 0.001
            for name in EXTRA_MASKS:
                ds.createVariable(name, "u1", ("obs",))[:] = 1 if (name == "cldice_mask" and i == 0) else 0
            if i == 2:
                ds["cloud_excluded_mask"][:] = 1
                for name in ("ci_valid_mask", "ci_candidate_mask", "valid_water_mask",
                             "ndci_valid_mask", "mph_valid_mask", "fai_valid_mask"):
                    ds[name][:] = 0
        ledger.update(scene["id"], status="done", stage="complete")
    nasa = pd.DataFrame({"Hylak_id": [101], NASA_DETECTION_COLUMN: pd.array([17], dtype="Int64")})
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}), \
         patch("cyanotrend.nasa_cyan.measure_daily", return_value=(nasa, {"date_utc": DAY})):
        result = run_daily(ws, DAY, DAY, catalogue=lambda *a, **k: scenes, lakes=_lakes().iloc[[0]])
    assert result["ok"], result
    return scenes, _read(ws)


def test_science_file_has_every_layer_exact_cells_and_reproduces_the_csv(workspace):
    ws = workspace
    scenes, (rows, manifest, folder) = _day(ws)
    path = folder / f"{DAY}_science.nc"
    assert path.is_file() and "science_nc" in manifest["rules"]
    with netCDF4.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        names = set(ds.variables)
        for needed in [*BANDS, *INDICES, "DN", "scenes_valid", "Hylak_id", "lake_support_mask", "native_source_match_mask",
                       "footprint_area", "valid_water_mask", "generic_flag_exclusion_sensitivity_mask", "bloom_rescue_mask",
                       "cldice_mask", "hisatzen_mask", "navfail_mask", "cloud_excluded_mask", "ci_valid_mask",
                       "ci_candidate_mask", "ci_detection_mask", "mph_valid_mask", "fai_valid_mask",
                       "row", "col", "bin", "latitude", "longitude", "x", "y", "crs"]:
            assert needed in names, needed
        assert ds["rhos_490"].dimensions == ("y", "x") and ds["crs"].grid_mapping_name == "sinusoidal"
        assert float(ds.CI_cyano_detection_limit) == detection_limit() and ds.schema.startswith("cyanotrend-daily-science")
        assert ds.scene_ids.count(";") == 2 and set(ds.satellites.split(";")) == {"S3A", "S3B"}
        lake = ds["Hylak_id"][:] == 101
        assert lake.sum() == int(rows[101]["Lake_cells"])
        assert (ds["lake_support_mask"][:][lake] == 1).all() and (ds["lake_support_mask"][:][~lake] <= 0).all()
        # rhos: highest valid pass per band (the cloud-only third pass is not valid), masks: set in any pass
        assert np.allclose(ds["rhos_490"][:][lake], 0.02) and np.allclose(ds["rhos_884"][:][lake], 0.028)
        assert (ds["cldice_mask"][:][lake] == 1).all() and (ds["hisatzen_mask"][:][lake] == 0).all()
        assert (ds["cloud_excluded_mask"][:][lake] == 1).all() and (ds["valid_water_mask"][:][lake] == 1).all()
        assert (ds["native_source_match_mask"][:][lake] == 1).all() and (ds["footprint_area"][:][lake] > 0).all()
        assert (ds["scenes_valid"][:][lake] == 2).all()
        # the same bins and values as the existing grid file
        keys = ds["bin"][:][lake]
        with netCDF4.Dataset(folder / f"{DAY}_grid.nc") as grid:
            order = np.argsort(grid["bin"][:])
            pos = order[np.searchsorted(grid["bin"][:][order], keys)]
            assert (grid["bin"][:][pos] == keys).all() and keys.size == grid["bin"].size
            for name in INDICES:
                assert np.array_equal(np.asarray(grid[name][:], dtype=np.float32)[pos], ds[name][:][lake])
            assert np.array_equal(grid["DN"][:][pos], ds["DN"][:][lake])
        assert ds[f"lake_{NASA_DETECTION_COLUMN}"][:].tolist() == [17]
    result = reproduce(path)
    assert result["ok"], result
    assert result["lakes"] == 1 and result["values_compared"] >= 40


def test_reproduce_reports_a_csv_that_no_longer_matches(workspace):
    _, (rows, manifest, folder) = _day(workspace)
    path = folder / f"{DAY}_lakes.csv"
    with path.open(newline="") as f:
        table = list(csv.DictReader(f))
    table[0]["Cloud_excluded_any_scene_cells"] = "1"
    _write_csv(path, table)
    result = reproduce(folder / f"{DAY}_science.nc")
    assert not result["ok"] and any("Cloud_excluded_any_scene_cells" in m for m in result["mismatches"])


def test_reproduce_command_runs_without_a_workspace(workspace, capsys):
    from cyanotrend.cli import main
    _, (rows, manifest, folder) = _day(workspace)
    assert main(["reproduce", str(folder / f"{DAY}_science.nc")]) == 0
    assert "mismatch_count" in capsys.readouterr().out
    assert main(["reproduce", str(folder / "missing_science.nc")]) == 1


def test_old_scene_bin_files_are_rebuilt_from_the_native_files(workspace):
    ws = workspace
    scenes, _ = _day(ws)
    scene = scenes[0]
    path = g.scene_grid_path(ws.archive(scene).parent, scene["id"], "cfg")
    assert g.read_scene_grid(path)["bands"]["rhos_490"].size
    with netCDF4.Dataset(path, "a") as ds:
        ds.setncattr("schema", "cyanotrend-isin-grid-1")
    with pytest.raises(ValueError):
        g.read_scene_grid(path)
    rebuilt, reason = ensure_scene_grid(ws, scene)
    assert reason is None and rebuilt == path
    data = g.read_scene_grid(path)
    assert np.isfinite(data["bands"]["rhos_490"]).any() and data["footprint"].min() > 0


def test_native_files_without_the_new_layers_still_bin_and_are_listed(workspace):
    ws = workspace
    scene = _scene("S3A_TEST", "S3A", f"{DAY}T12:00:00Z", _footprint(*LAKE_A))
    _write_native(ws, scene, [(LAKE_A, lambda r, c: 0.001)])  # no rhos, few masks
    native = ws.archive(scene).parent
    out = g.write_scene_grid(native, scene, native / "grid.nc", native_files=g.scene_native_files(native, scene["id"]))
    assert out["bins"] > 0
    with netCDF4.Dataset(native / "grid.nc") as ds:
        import json
        missing = json.loads(ds.native_variables_missing)
        assert "rhos_490" in missing and "navfail_mask" in missing
    data = g.read_scene_grid(native / "grid.nc")
    assert np.isnan(data["bands"]["rhos_490"]).all() and not data["masks"]["navfail_mask"].any()


def test_a_rewritten_state_folder_does_not_keep_an_old_science_file(tmp_path):
    ws = Workspace.create(tmp_path / "out")
    publisher = DailyPublisher(ws, CSV_COLUMNS)
    publisher.initialize()
    day = date(2024, 8, 14)

    def put(with_science):
        staging = publisher.stage()
        relative = "USA/Michigan/2024/08/14"
        files = {}
        for suffix in ["lakes.csv", "grid.nc", "manifest.json"] + (["science.nc"] if with_science else []):
            name = f"{day.isoformat()}_{suffix}"
            if suffix == "lakes.csv":
                _write_csv(staging / name, [csv_row(day, 1)])
            else:
                (staging / name).write_text("x")
            files[f"{relative}/{name}"] = staging / name
        publisher.publish(day, [csv_row(day, 1)], files, staging)
        return publisher.root / relative / f"{day.isoformat()}_science.nc"

    assert put(True).is_file()
    assert not put(False).exists()
