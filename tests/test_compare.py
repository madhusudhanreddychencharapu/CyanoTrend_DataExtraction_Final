"""Lake finder and the NASA CyAN vs our CI_cyano scatter plot."""
import csv
import json

import netCDF4
import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely import contains_xy
from shapely.geometry import box

from cyanotrend.cli import main, parser
from cyanotrend.common import Workspace
from cyanotrend.daily import compare
from cyanotrend.daily import grid as g
from cyanotrend.daily.merge import DN_INTERCEPT, DN_SLOPE, detection_limit
from cyanotrend.ledger import Ledger
from cyanotrend.schema import NASA_DETECTION_COLUMN

DAY = "2024-08-14"
POLY = {101: box(-83.60, 45.38, -83.55, 45.42), 303: box(-83.50, 45.38, -83.46, 45.41)}
TILE = "L2024227.L3m_DAY_CYAN_CI_cyano_CYAN_CONUS_300m_7_5.tif"


@pytest.fixture
def ws(tmp_path):
    w = Workspace.create(tmp_path / "out", tmp_path / "refs", tmp_path / "scratch", tmp_path / "ocssw")
    w.ensure()
    Ledger(w.db).enqueue({"id": "mi", "created": "now", "reference_id": "ref", "scenes": [],
                          "selection": {"country": "USA", "state_id": "MI", "state_name": "Michigan"}},
                         w.output / "plans" / "mi.json")
    return w


def _folder(ws):
    folder = ws.output / "daily" / "USA" / "Michigan" / "2024" / "08" / "14"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ours(lon, lat):
    """South detected, north below detection, one invalid stripe."""
    value = np.where(lat < 45.40, 3e-3, 1e-5)
    return np.where((lon > -83.57) & (lon < -83.565), np.nan, value)


def _daily_result(ws):
    """A merged daily result: lakes CSV (as published by `daily`) and the daily grid file."""
    rows = [
        {"Hylak_id": 101, "Lake_name": "Lake A", "Lake_area_km2": 2.0, "states": "Michigan", "day_status": "complete",
         "scenes_used": 1, "Lake_cells": 10, "ci_input_valid_cells": 9, "CI_cyano_detectable_cells": 5, NASA_DETECTION_COLUMN: 120},
        {"Hylak_id": 202, "Lake_name": "Lake B", "Lake_area_km2": 5.0, "states": "Michigan", "day_status": "complete",
         "scenes_used": 2, "Lake_cells": 20, "ci_input_valid_cells": 19, "CI_cyano_detectable_cells": 7, NASA_DETECTION_COLUMN: ""},
        {"Hylak_id": 303, "Lake_name": "", "Lake_area_km2": 9.0, "states": "Michigan", "day_status": "partial",
         "scenes_used": 1, "Lake_cells": 30, "ci_input_valid_cells": 29, "CI_cyano_detectable_cells": 9, NASA_DETECTION_COLUMN: 30},
        {"Hylak_id": 404, "Lake_name": "Lake Alpha", "Lake_area_km2": 1.0, "states": "Michigan", "day_status": "complete",
         "scenes_used": 1, "Lake_cells": 3, "ci_input_valid_cells": 3, "CI_cyano_detectable_cells": 0, NASA_DETECTION_COLUMN: 0},
    ]
    folder = _folder(ws)
    with (folder / f"{DAY}_lakes.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    keys, lake_id, value = [], [], []
    for hylak_id, polygon in POLY.items():
        lake_keys = g.lake_bins(polygon)
        lon, lat = g.bin_centre(lake_keys)
        keys.append(lake_keys)
        lake_id.append(np.full(lake_keys.size, hylak_id))
        value.append(_ours(lon, lat))
    with netCDF4.Dataset(folder / f"{DAY}_grid.nc", "w") as ds:
        ds.createDimension("bin", sum(k.size for k in keys))
        for name, dtype, data in (("bin", "i8", np.concatenate(keys)), ("Hylak_id", "i8", np.concatenate(lake_id)),
                                  ("CI_cyano", "f4", np.concatenate(value))):
            ds.createVariable(name, dtype, ("bin",), fill_value=False)[:] = data
    return folder


def _nasa_tile(cache):
    """A small NASA-like DN raster (Albers, 300 m) over both lakes, with its sidecar file."""
    cache.mkdir(parents=True, exist_ok=True)
    to_xy = Transformer.from_crs(4326, 5070, always_xy=True)
    x0, y0 = to_xy.transform(-83.65, 45.34)
    x1, y1 = to_xy.transform(-83.42, 45.46)
    west, north = np.floor(min(x0, x1) / 300) * 300, np.ceil(max(y0, y1) / 300) * 300
    width, height = int((max(x0, x1) - west) // 300) + 2, int((north - min(y0, y1)) // 300) + 2
    rows, cols = np.indices((height, width))
    dn = np.zeros((height, width), dtype=np.uint8)
    dn[(rows // 3) % 2 == 0] = (20 + cols % 40)[(rows // 3) % 2 == 0]
    dn[rows % 11 == 0] = 255
    dn[cols % 13 == 0] = 254
    path = cache / TILE
    with rasterio.open(path, "w", driver="GTiff", height=height, width=width, count=1, dtype="uint8", crs="EPSG:5070",
                       transform=from_origin(west, north, 300, 300)) as dst:
        dst.write(dn, 1)
    with rasterio.open(path) as src:
        bounds = list(src.bounds)
    (cache / f"{TILE}.json").write_text(json.dumps({"filename": TILE, "region": "CONUS", "tile": "7_5",
                                                    "selection_bounds": bounds}))
    return path


def _expected_pairs(tile, polygon, hylak_id):
    """Brute force: every NASA cell centre inside the lake, matched to the bin of that point."""
    keys = g.lake_bins(polygon)
    lon_c, lat_c = g.bin_centre(keys)
    table = dict(zip(keys.tolist(), _ours(lon_c, lat_c).tolist()))
    to_geo = Transformer.from_crs(5070, 4326, always_xy=True)
    pairs, leftovers = [], {"nasa_special": 0, "no_bin": 0, "invalid": 0}
    with rasterio.open(tile) as src:
        dn = src.read(1)
        t = src.transform
    for r in range(dn.shape[0]):
        for c in range(dn.shape[1]):
            lon, lat = to_geo.transform(t.c + (c + 0.5) * t.a, t.f + (r + 0.5) * t.e)
            if not contains_xy(polygon, lon, lat):
                continue
            if dn[r, c] > 253:
                leftovers["nasa_special"] += 1
                continue
            value = table.get(int(g.bin_number(lon, lat)))
            if value is None:
                leftovers["no_bin"] += 1
            elif np.isnan(value):
                leftovers["invalid"] += 1
            else:
                x = 0.0 if dn[r, c] == 0 else 10 ** (int(dn[r, c]) * DN_SLOPE + DN_INTERCEPT)
                pairs.append((int(dn[r, c]), round(x, 12), 0.0 if value < detection_limit() else round(value, 9)))
    return pairs, leftovers


def test_lake_finder_lists_only_nasa_comparable_lakes_biggest_first(ws):
    _daily_result(ws)
    result = compare.find_lakes(ws, "usa", "MI", DAY)
    assert result["state"] == "Michigan" and result["lakes_with_daily_row"] == 4 and result["lakes_with_nasa_value"] == 3
    assert [r["Hylak_id"] for r in result["lakes"]] == ["303", "101", "404"]   # by area; lake 202 has no NASA value
    assert result["lakes"][0]["Lake_name"] == "(unnamed, Hylak_id 303)"
    assert any("scatter --country USA --state-id MI" in note for note in result["notes"])
    named = compare.find_lakes(ws, "USA", "MI", DAY, name="LAKE A")
    assert [r["Hylak_id"] for r in named["lakes"]] == ["101", "404"]   # "lake a" is part of both names; biggest first
    assert [r["Hylak_id"] for r in compare.find_lakes(ws, "USA", "MI", DAY, name="alpha")["lakes"]] == ["404"]
    short = compare.find_lakes(ws, "USA", "MI", DAY, limit=1)
    assert len(short["lakes"]) == 1 and any("biggest of 3" in note for note in short["notes"])
    none = compare.find_lakes(ws, "USA", "MI", DAY, name="superior")
    assert none["lakes"] == [] and any("superior" in note for note in none["notes"])


def test_missing_daily_result_says_which_command_to_run(ws):
    with pytest.raises(ValueError, match=r"daily --start 2024-08-14 --end 2024-08-14 --country USA --state-id MI"):
        compare.find_lakes(ws, "USA", "MI", DAY)
    with pytest.raises(ValueError, match="daily --start"):
        compare.scatter(ws, "USA", "MI", DAY, ["Lake A"])


def test_scatter_pairs_each_nasa_cell_with_our_bin_at_its_centre(ws):
    _daily_result(ws)
    tile = _nasa_tile(ws.references / "obpg_cyan")
    result = compare.scatter(ws, "USA", "MI", DAY, ["Lake A"], polygons=POLY)
    pairs, left = _expected_pairs(tile, POLY[101], 101)
    assert pairs and left["no_bin"] + left["invalid"] + left["nasa_special"] > 0   # the test covers leftovers
    with open(result["pairs_csv"], newline="") as f:
        table = list(csv.DictReader(f))
    got = sorted((int(r["NASA_DN"]), round(float(r["NASA_CI_cyano"]), 12), round(float(r["our_CI_cyano"]), 9)) for r in table)
    assert got == sorted(pairs)
    assert {int(r["Hylak_id"]) for r in table} == {101} and {r["Lake_name"] for r in table} == {"Lake A"}
    lake = result["lakes"][0]
    assert lake["cells_plotted"] == len(pairs)
    assert lake["left_out_nasa_land_or_no_data"] == left["nasa_special"]
    assert lake["left_out_no_bin_of_ours_at_cell_centre"] == left["no_bin"]
    assert lake["left_out_our_bin_without_valid_data"] == left["invalid"]
    assert lake["nasa_cells_in_lake"] == len(pairs) + sum(left.values())
    assert {y for _, _, y in pairs} == {0.0, 3e-3}                  # non-detects are 0, detections keep their value
    assert 0.0 in {x for _, x, _ in pairs} and min(x for _, x, _ in pairs if x > 0) >= 6.6e-5
    x = np.array([p[1] for p in pairs])
    y = np.array([p[2] for p in pairs])
    assert result["overall"]["cells"] == len(pairs)
    assert result["overall"]["R"] == pytest.approx(np.corrcoef(x, y)[0, 1], abs=1e-4)
    slope, intercept = np.polyfit(x, y, 1)
    assert result["overall"]["slope"] == pytest.approx(slope, abs=1e-4)
    png = open(result["png"], "rb").read()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    assert result["png"].endswith("2024-08-14_lake_101_scatter.png")
    assert "compare/USA/Michigan/2024/08/14" in result["png"]


def test_scatter_for_several_lakes_on_one_plot(ws):
    _daily_result(ws)
    tile = _nasa_tile(ws.references / "obpg_cyan")
    result = compare.scatter(ws, "USA", "MI", DAY, ["Lake A", "303"], polygons=POLY, out_dir=ws.output / "plots")
    expected = {101: _expected_pairs(tile, POLY[101], 101)[0], 303: _expected_pairs(tile, POLY[303], 303)[0]}
    assert [lake["Hylak_id"] for lake in result["lakes"]] == [101, 303]
    assert [lake["cells_plotted"] for lake in result["lakes"]] == [len(expected[101]), len(expected[303])]
    assert result["overall"]["cells"] == len(expected[101]) + len(expected[303])
    assert result["png"].endswith("2024-08-14_lakes_101_303_scatter.png") and "plots" in result["png"]
    with open(result["pairs_csv"], newline="") as f:
        assert {r["Hylak_id"] for r in csv.DictReader(f)} == {"101", "303"}
    assert result["lakes"][1]["Lake_name"] == "(unnamed, Hylak_id 303)"


def test_scatter_lake_choice_errors_are_clear(ws):
    _daily_result(ws)
    _nasa_tile(ws.references / "obpg_cyan")
    with pytest.raises(ValueError, match="matches several lakes"):
        compare.scatter(ws, "USA", "MI", DAY, ["lake"], polygons=POLY)
    with pytest.raises(ValueError, match="no NASA CyAN value"):
        compare.scatter(ws, "USA", "MI", DAY, ["Lake B"], polygons=POLY)
    with pytest.raises(ValueError, match="No lake named like"):
        compare.scatter(ws, "USA", "MI", DAY, ["Erie"], polygons=POLY)
    with pytest.raises(ValueError, match="has no daily row"):
        compare.scatter(ws, "USA", "MI", DAY, ["999"], polygons=POLY)
    assert compare._pick(compare._rows(_folder(ws) / f"{DAY}_lakes.csv"), "lake a")["Hylak_id"] == "101"  # exact beats partial


def test_cli_commands_exist_and_lakes_prints_a_table(ws, capsys):
    a = parser().parse_args(["scatter", "--country", "USA", "--state-id", "MI", "--date", DAY,
                             "--lake", "Lake A", "--lake", "303"])
    assert a.lake == ["Lake A", "303"] and a.out_dir is None
    with pytest.raises(SystemExit):
        parser().parse_args(["scatter", "--country", "USA", "--state-id", "MI", "--date", DAY])  # a lake is required
    _daily_result(ws)
    assert main(["--output", str(ws.output), "lakes", "--country", "USA", "--state-id", "MI", "--date", DAY]) == 0
    out = capsys.readouterr().out
    assert "Lake A" in out and "(unnamed, Hylak_id 303)" in out and "Lake B" not in out
