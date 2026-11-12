"""Day stage: NASA CyAN grid placement, per-lake checklist and daily maximum merge."""
import csv
import json
from pathlib import Path
from unittest.mock import patch
import uuid

import geopandas as gpd
import netCDF4
import numpy as np
import pytest
from shapely.geometry import Polygon, box, mapping

from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger
from cyanotrend.daily import grid as g
from cyanotrend.daily.merge import acquisition, ci_to_dn, detection_limit, merge_cells, run_daily

DAY = "2024-08-14"


def test_nasa_tile_7_5_lies_on_the_lattice():
    row, col = g.tile_offset((300.0, 0.0, 1034402.95, 0.0, -300.0, 914826.27))
    assert (row, col) == (8000, 12000)
    assert g.tile_name(row, col) == "7_5"
    with pytest.raises(ValueError):
        g.tile_offset((300.0, 0.0, 1034400.00, 0.0, -300.0, 914826.27))


def test_two_satellites_pixels_of_one_place_share_a_cell():
    x, y = g.project([-83.6031, -83.6024], [43.9512, 43.9518])
    row, col = g.cell_of(x, y)
    assert row[0] == row[1] and col[0] == col[1]
    cx, cy = g.cell_centre(row[0], col[0])
    assert abs(cx - x[0]) <= 150 and abs(cy - y[0]) <= 150


def _pixels(rows, cols, values, valid=None, shift=(0.0, 0.0)):
    rows, cols = np.asarray(rows), np.asarray(cols)
    x, y = g.cell_centre(rows, cols)
    lon, lat = g.unproject(x + shift[0], y + shift[1])
    values = np.asarray(values, dtype=float)
    return {"lat": lat, "lon": lon, "row": rows - rows.min(), "col": cols - cols.min(),
            "valid": np.ones(rows.size, bool) if valid is None else np.asarray(valid, bool),
            "value": values}


def _value(cells, key, mode):
    i = np.searchsorted(cells["key"], key)
    assert cells["key"][i] == key
    if mode == "area":
        return cells["value_sum"][i] / cells["valid_weight"][i]
    return cells["near_value"][i]


def test_aligned_pixels_keep_their_values_and_shifted_pixels_share_area():
    rr, cc = np.meshgrid(np.arange(3070, 3076), np.arange(11845, 11851), indexing="ij")
    values = (np.arange(rr.size) % 5) * 1e-3
    cells = g.pixels_to_cells(_pixels(rr.ravel(), cc.ravel(), values))
    assert cells["key"].size == rr.size
    for k, v in zip(g.cell_key(rr.ravel(), cc.ravel()), values):
        assert _value(cells, k, "area") == pytest.approx(v, abs=1e-12)
        assert _value(cells, k, "nearest") == pytest.approx(v, abs=1e-12)
    # Half a cell east and south: each interior cell gets four quarter-pixels.
    shifted = g.pixels_to_cells(_pixels(rr.ravel(), cc.ravel(), np.full(rr.size, 2e-3), shift=(150.0, -150.0)))
    interior = g.cell_key(3072, 11847)
    i = np.searchsorted(shifted["key"], interior)
    assert shifted["weight"][i] == pytest.approx(90000.0, rel=0.02)
    assert _value(shifted, interior, "area") == pytest.approx(2e-3)


def test_invalid_pixels_do_not_dilute_the_area_mean():
    cells = g.pixels_to_cells(_pixels([3070, 3070], [11845, 11846], [1e-3, 0.0], valid=[True, False]))
    assert _value(cells, g.cell_key(3070, 11845), "area") == pytest.approx(1e-3)
    i = np.searchsorted(cells["key"], g.cell_key(3070, 11846))
    assert cells["valid_weight"][i] == 0 and not cells["near_valid"][i]


def _lake_polygon(r0, r1, c0, c1):
    """Lon/lat polygon whose interior holds exactly the cell centres r0..r1 x c0..c1."""
    x0, _ = g.cell_centre(r0, c0 - 0.5)
    x1, _ = g.cell_centre(r0, c1 + 0.5)
    _, y0 = g.cell_centre(r1 + 0.5, c0)
    _, y1 = g.cell_centre(r0 - 0.5, c0)
    xs = np.r_[np.linspace(x0, x1, 20), np.full(20, x1), np.linspace(x1, x0, 20), np.full(20, x0)]
    ys = np.r_[np.full(20, y1), np.linspace(y1, y0, 20), np.full(20, y0), np.linspace(y0, y1, 20)]
    lon, lat = g.unproject(xs, ys)
    return Polygon(zip(lon, lat))


def test_lake_cells_follow_nasa_centre_inside_rule():
    keys = g.lake_cells(_lake_polygon(3070, 3074, 11845, 11849))
    rows, cols = g.key_cell(keys)
    assert keys.size == 25
    assert rows.min() == 3070 and rows.max() == 3074 and cols.min() == 11845 and cols.max() == 11849


def test_dn_matches_nasa_v6_scale():
    limit = detection_limit()
    values = np.array([np.nan, 0.0, limit * 0.99, limit, 10 ** (253 * 0.011714 - 4.1870866), 1.0])
    valid = np.array([False, True, True, True, True, True])
    assert ci_to_dn(values, valid).tolist() == [255, 0, 0, 1, 253, 253]


def test_catalogue_versions_of_one_acquisition_prefer_non_time_critical():
    nr = {"name": "S3A_OL_1_EFR____20240814T161518_20240814T161818_20240814T182001_0179_115_368_2340_PS1_O_NR_004"}
    nt = {"name": "S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004"}
    (k1, p1), (k2, p2) = acquisition(nr), acquisition(nt)
    assert k1 == k2 and p2 < p1


def test_merge_keeps_highest_valid_value_and_no_data_only_without_any_view():
    keys = np.array([1, 2, 3, 4], dtype=np.int64)
    a = {"key": np.array([1, 2, 3]), "area_valid": np.array([True, True, False]),
         "area_value": np.array([1e-3, 0.0, np.nan]), "nearest_valid": np.array([True, True, False]),
         "nearest_value": np.array([1e-3, 0.0, np.nan])}
    b = {"key": np.array([1, 2]), "area_valid": np.array([True, True]),
         "area_value": np.array([5e-4, 2e-3]), "nearest_valid": np.array([True, True]),
         "nearest_value": np.array([5e-4, 2e-3])}
    valid, value, seen = merge_cells(keys, [a, b], "area")
    assert valid.tolist() == [True, True, False, False]
    assert value[:2].tolist() == [1e-3, 2e-3]
    assert seen.tolist() == [2, 2, 0, 0]


# ---------------------------------------------------------------------------
# End to end: Michigan run, then Wisconsin run, same UTC day
# ---------------------------------------------------------------------------

LAKE_A = (3070, 3074, 11845, 11849)  # seen by S3A-1 and S3B-1
LAKE_B = (3070, 3074, 11860, 11864)  # seen by S3A-1 and S3B-2


def _footprint(r0, r1, c0, c1):
    x0, y0 = g.cell_centre(r1 + 3, c0 - 3)
    x1, y1 = g.cell_centre(r0 - 3, c1 + 3)
    lon, lat = g.unproject([x0, x1, x1, x0], [y0, y0, y1, y1])
    return mapping(Polygon(zip(lon, lat)))


def _scene(name, satellite, start, footprint):
    return {"id": str(uuid.uuid4()), "name": name, "satellite": satellite,
            "output_region": {"country": "USA", "state_name": "Michigan", "state_id": "MI"},
            "start": start, "end": start, "geofootprint": footprint}


def _write_native(ws, scene, cells):
    """cells: list of (row, col, value or None for invalid)."""
    folder = ws.archive(scene).parent
    folder.mkdir(parents=True, exist_ok=True)
    rows = np.array([c[0] for c in cells])
    cols = np.array([c[1] for c in cells])
    x, y = g.cell_centre(rows, cols)
    lon, lat = g.unproject(x + 20.0, y - 20.0)  # pixel centres slightly off cell centres
    valid = np.array([c[2] is not None for c in cells])
    value = np.array([np.nan if c[2] is None else c[2] for c in cells])
    with netCDF4.Dataset(folder / f"{scene['id']}_full_cfg_lakepixels.nc", "w") as ds:
        ds.createDimension("obs", rows.size)
        for name, dtype, data in [
            ("latitude", "f4", lat), ("longitude", "f4", lon),
            ("source_row", "i4", rows - rows.min()), ("source_col", "i4", cols - cols.min()),
            ("ci_valid_mask", "u1", valid.astype(np.uint8)),
            ("ci_candidate_mask", "u1", (valid & (value > 0)).astype(np.uint8)),
            ("valid_water_mask", "u1", valid.astype(np.uint8)),
            ("cloud_excluded_mask", "u1", (~valid).astype(np.uint8)),
            *[(f"{name.lower()}_valid_mask", "u1", valid.astype(np.uint8)) for name in ("NDCI", "MPH", "FAI")],
            *[(name, "f4", value) for name in ("NDCI", "MPH", "FAI")],
            ("CI", "f4", np.where(valid & (value > 0), value, np.nan)),
            ("SS_665", "f4", np.where(valid, 1e-3, np.nan)),
        ]:
            ds.createVariable(name, dtype, ("obs",))[:] = data


def _block(r0, r1, c0, c1, value_of):
    return [(r, c, value_of(r, c)) for r in range(r0 - 1, r1 + 2) for c in range(c0 - 1, c1 + 2)]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("cyanotrend.nasa_cyan.discover_tiles", lambda day: [])
    ws = Workspace.create(tmp_path / "out", tmp_path / "refs", tmp_path / "scratch", tmp_path / "ocssw")
    ws.ensure()
    return ws


def _lakes():
    return gpd.GeoDataFrame({
        "Hylak_id": [101, 202], "Lake_name": ["Lake A", "Lake B"], "Lake_area": [2.25, 2.25],
        "latitude": [43.95, 43.95], "longitude": [-83.6, -83.5],
        "states": ["Michigan", "Michigan; Wisconsin"], "state_ids": ["MI", "MI; WI"],
        "country_iso3": ["USA", "USA"], "state_name": ["Michigan", "Wisconsin"],
        "admin_match_status": ["matched", "matched"],
    }, geometry=[_lake_polygon(*LAKE_A), _lake_polygon(*LAKE_B)], crs=4326)


def _read(ws, state="Michigan"):
    folder = ws.output / "daily" / "USA" / state / "2024" / "08" / "14"
    with (folder / f"{DAY}_lakes.csv").open() as f:
        rows = {int(r["Hylak_id"]): r for r in csv.DictReader(f)}
    manifest = json.loads((folder / f"{DAY}_manifest.json").read_text())
    return rows, manifest, folder


def test_michigan_then_wisconsin_day(workspace):
    ws = workspace
    both = (LAKE_A[0], LAKE_A[1], LAKE_A[2], LAKE_B[3])
    s3a1 = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                  "S3A", "2024-08-14T16:15:18Z", _footprint(*both))
    s3a1_nr = dict(_scene(s3a1["name"].replace("20240815T200533", "20240814T182001").replace("_NT_", "_NR_"),
                          "S3A", s3a1["start"], s3a1["geofootprint"]))
    s3b1 = _scene("S3B_OL_1_EFR____20240814T155400_20240814T155700_20240815T190000_0179_096_011_2340_PS2_O_NT_004",
                  "S3B", "2024-08-14T15:54:00Z", _footprint(*LAKE_A))
    s3b2 = _scene("S3B_OL_1_EFR____20240814T155700_20240814T160000_20240815T190100_0179_096_011_2520_PS2_O_NT_004",
                  "S3B", "2024-08-14T15:57:00Z", _footprint(*LAKE_B))
    catalogue_scenes = [s3a1, s3a1_nr, s3b1, s3b2]
    s3b2["output_region"] = {"country": "USA", "state_name": "Wisconsin", "state_id": "WI"}

    def catalogue(a, b, bbox=None):
        assert a == b == DAY and bbox is not None
        return catalogue_scenes

    ledger = Ledger(ws.db)
    # Michigan plan: S3A-1 and S3B-1. (S3B-2 is planned later, by Wisconsin.)
    ledger.enqueue({"id": "mi", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [s3a1, s3b1]}, ws.output / "plans" / "mi.json")
    # Lake A: S3A-1 detects the west half, S3B-1 the east half (higher), one cell never valid.
    _write_native(ws, s3a1, _block(*LAKE_A, lambda r, c: 1e-3 if c <= 11846 else 0.0)
                  + _block(*LAKE_B, lambda r, c: 0.0))
    _write_native(ws, s3b1, _block(*LAKE_A, lambda r, c: None if (r, c) == (3074, 11849)
                                   else (2e-3 if c >= 11848 else 5e-4)))
    for s in (s3a1, s3b1):
        ledger.update(s["id"], status="done", stage="complete", archive=str(ws.archive(s)))
    # Make S3A-1's never-valid cell invalid in its own pass too.
    path = ws.archive(s3a1).parent / f"{s3a1['id']}_full_cfg_lakepixels.nc"
    with netCDF4.Dataset(path, "a") as ds:
        x, y = g.project(ds["longitude"][:], ds["latitude"][:])
        r, c = g.cell_of(x, y)
        bad = (r == 3074) & (c == 11849)
        mask = ds["ci_valid_mask"][:]
        mask[bad] = 0
        ds["ci_valid_mask"][:] = mask

    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes())
    assert result["ok"], result
    rows, manifest, folder = _read(ws)
    assert set(rows) == {101}  # Lake B waits for S3B-2
    a = rows[101]
    assert a["day_status"] == "complete" and a["satellites"] == "S3A;S3B" and a["scenes_used"] == "2"
    assert int(a["Lake_cells"]) == 25
    assert int(a["CI_cyano_detectable_cells"]) == 24  # every column detected by one pass or the other
    assert int(a["No_valid_data_cells"]) == 1
    assert float(a["CI_cyano_max"]) == pytest.approx(2e-3, rel=1e-5)
    assert manifest["waiting_lakes"] == []
    _, wi_manifest, _ = _read(ws, "Wisconsin")
    assert [w["Hylak_id"] for w in wi_manifest["waiting_lakes"]] == [202]
    assert wi_manifest["waiting_lakes"][0]["missing_scenes"] == [s3b2["name"]]
    s3a_entry = next(x for x in manifest["acquisitions"] if x["satellite"] == "S3A")
    assert s3a_entry["status"] == "done" and len(s3a_entry["versions"]) == 2  # NR version not needed
    assert (ws.archive(s3a1).parent / f"{s3a1['id']}_cfg_nasa_grid.nc").exists()
    with netCDF4.Dataset(folder / f"{DAY}_grid.nc") as ds:
        assert ds.dimensions["cell"].size == 25
        assert sorted(set(ds["DN"][:].tolist()))[-1] == 255

    # Wisconsin plan: S3A-1 and S3B-1 are already processed; only S3B-2 is new.
    ledger.enqueue({"id": "wi", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [s3a1, s3b1, s3b2]}, ws.output / "plans" / "wi.json")
    assert ledger.scene(s3a1["id"])["status"] == "done"
    _write_native(ws, s3b2, _block(*LAKE_B, lambda r, c: 3e-3 if r == 3072 else 0.0))
    ledger.update(s3b2["id"], status="done", stage="complete", archive=str(ws.archive(s3b2)))
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        assert run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes())["ok"]
    rows2, manifest2, _ = _read(ws)
    wi_rows, wi_manifest2, wi_folder = _read(ws, "Wisconsin")
    assert set(rows2) == {101} and set(wi_rows) == {202}
    assert manifest2["waiting_lakes"] == wi_manifest2["waiting_lakes"] == []
    assert rows2[101] == a  # unchanged lake: identical row
    b = wi_rows[202]
    assert b["day_status"] == "complete" and b["states"] == "Michigan; Wisconsin"
    assert int(b["CI_cyano_detectable_cells"]) == 5 and int(b["Non_detectable_cells"]) == 20
    with netCDF4.Dataset(wi_folder / f"{DAY}_grid.nc") as ds:
        assert set(ds["Hylak_id"][:].tolist()) == {202}
    with (ws.output / "daily/master_merged_daily.csv").open() as f:
        master = list(csv.DictReader(f))
    with (ws.output / "daily/master_merged_daily_backup.csv").open() as f:
        backup = list(csv.DictReader(f))
    assert {int(r["Hylak_id"]) for r in master} == {101, 202} and len(master) == 2
    assert backup == [a]


def test_permanently_failed_scene_gives_partial_row(workspace):
    ws = workspace
    s3a1 = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                  "S3A", "2024-08-14T16:15:18Z", _footprint(*LAKE_A))
    s3b1 = _scene("S3B_OL_1_EFR____20240814T155400_20240814T155700_20240815T190000_0179_096_011_2340_PS2_O_NT_004",
                  "S3B", "2024-08-14T15:54:00Z", _footprint(*LAKE_A))
    ledger = Ledger(ws.db)
    ledger.enqueue({"id": "p", "created": "now", "reference_id": "ref", "selection": {"max_scenes": 3},
                    "scenes": [s3a1, s3b1]}, ws.output / "plans" / "p.json")
    _write_native(ws, s3a1, _block(*LAKE_A, lambda r, c: 1e-3))
    ledger.update(s3a1["id"], status="done", stage="complete", archive=str(ws.archive(s3a1)))
    ledger.update(s3b1["id"], status="failed", stage="failed", error="download failed")
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=lambda a, b, bbox=None: [s3a1, s3b1], lakes=_lakes().iloc[[0]])
    rows, manifest, _ = _read(ws)
    assert rows[101]["day_status"] == "partial"
    assert rows[101]["failed_or_unavailable_scenes"] == s3b1["name"]
    assert int(rows[101]["CI_cyano_detectable_cells"]) == 25
    assert any("max-scenes" in w for w in manifest["warnings"])
    assert result["days"][0]["merged_partial"] == 1


def test_daily_command_is_available():
    from cyanotrend.cli import parser
    a = parser().parse_args(["daily", "--start", DAY, "--end", DAY])
    assert a.command == "daily" and a.method == "max"
    a = parser().parse_args(["plan", "--start", DAY, "--end", DAY, "--country", "USA", "--state-id", "MI"])
    assert a.scene_rule == "lake-complete"
