"""Day stage: ISIN bins, area-weighted binning, per-lake checklist and daily maximum merge."""
import csv
import json
from unittest.mock import patch
import uuid

import geopandas as gpd
import netCDF4
import numpy as np
import pytest
from shapely.geometry import box, mapping

from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger
from cyanotrend.daily import grid as g
from cyanotrend.schema import NASA_DETECTION_COLUMN
from cyanotrend.daily.merge import acquisition, ci_to_dn, detection_limit, merge_cells, run_daily

DAY = "2024-08-14"
ROW = 52000  # ISIN row at about 45.4 N (Lake Michigan latitudes)


# ---------------------------------------------------------------------------
# Helpers: lakes and pixel lattices defined in ISIN rows and columns
# ---------------------------------------------------------------------------

def lat_edge(row):
    return row * 180.0 / g.N_ROWS - 90.0


def dlon(row):
    return 360.0 / int(g.bins_per_row()[row])


def first_col(row, lon):
    return int(np.floor((lon + 180.0) / dlon(row)))


def lon_edge(row, col):
    return -180.0 + col * dlon(row)


# (first row, rows, first column in that row, columns)
LAKE_A = (ROW, 5, first_col(ROW, -83.60), 5)
LAKE_B = (ROW, 5, first_col(ROW, -83.60) + 15, 5)
BOTH = (ROW, 5, LAKE_A[2], 20)


def _lake_polygon(first_row, n_rows, col0, n_cols):
    return box(lon_edge(first_row, col0), lat_edge(first_row),
               lon_edge(first_row, col0 + n_cols), lat_edge(first_row + n_rows))


def _expected_bins(*boxes):
    """Brute force 'bin centre strictly inside the rectangle', independent of g.lake_bins."""
    keys = []
    for rect in boxes:
        minx, miny, maxx, maxy = rect.bounds
        for row in range(int((miny + 90) * g.N_ROWS / 180) - 1, int((maxy + 90) * g.N_ROWS / 180) + 2):
            col = np.arange(int(g.bins_per_row()[row]))
            lon, lat = g.bin_centre_row_col(np.full(col.size, row), col)
            inside = (lon > minx) & (lon < maxx) & (lat > miny) & (lat < maxy)
            keys.extend(g.bin_key(np.full(int(inside.sum()), row), col[inside]).tolist())
    return np.array(sorted(set(keys)), dtype=np.int64)


def _bins_per_row(polygon):
    rows, _ = g.bin_row_col(_expected_bins(polygon))
    return {int(r): int((rows == r).sum()) for r in np.unique(rows)}


def _lattice(first_row, n_rows, col0, n_cols, margin=2, east=0.1):
    """Pixel centres of a regular pixel grid over a lake block.

    One pixel per bin row (exactly aligned in latitude) and per bin of the first
    row (`east` bin widths off the bin centres). Other rows' bins are not lined
    up with it, as in real data.
    """
    i, j = np.meshgrid(np.arange(-margin, n_rows + margin), np.arange(-margin, n_cols + margin), indexing="ij")
    i, j = i.ravel(), j.ravel()
    return {"lat": lat_edge(first_row) + (i + 0.5) * 180.0 / g.N_ROWS,
            "lon": lon_edge(first_row, col0) + (j + 0.5 + east) * dlon(first_row),
            "i": i, "j": j, "row": i + margin, "col": j + margin}


def _pixels(lattice, values, valid=None):
    values = np.asarray(values, dtype=float)
    valid = np.ones(values.size, bool) if valid is None else np.asarray(valid, bool)
    ci = np.where(valid, values, np.nan)
    none = np.full(values.size, np.nan)
    return {"lat": lattice["lat"], "lon": lattice["lon"], "row": lattice["row"], "col": lattice["col"],
            "valid": valid, "value": values,
            "indices": {"CI": ci, "CI_cyano": ci, "NDCI": none, "MPH": none, "FAI": none},
            "flags": {"primary_retained": valid, "cloud_excluded": ~valid}}


def _interior(bins, lattice, rows):
    """Bins of the given rows lying well inside the longitude span of the pixel lattice."""
    row, col = g.bin_row_col(bins["key"])
    lon, _ = g.bin_centre_row_col(row, col)
    w = dlon(ROW)
    inside = (lon > lattice["lon"].min() + w) & (lon < lattice["lon"].max() - w)
    return inside & np.isin(row, rows), row


# ---------------------------------------------------------------------------
# ISIN grid
# ---------------------------------------------------------------------------

def test_isin_grid_matches_the_nasa_level3_scheme():
    bins = g.bins_per_row()
    assert g.N_ROWS == 69120 and bins.size == 69120
    assert bins.max() == 138240 and bins[0] == bins[-1] == 3  # equator row; polar rows
    assert g.total_bins() == 6082996640
    assert g.BIN_HEIGHT_M == pytest.approx(289.57, abs=0.01)
    area = g.bin_area_m2(np.arange(5000, 64120))  # 80 S to 80 N
    assert area.mean() == pytest.approx(83851, rel=1e-4)
    assert (area.max() - area.min()) / area.mean() < 5e-5  # equal area to 0.005%


def test_bin_numbers_round_trip_and_wrap_at_the_date_line():
    rng = np.random.default_rng(7)
    lon, lat = rng.uniform(-180, 180, 50000), rng.uniform(-89.99, 89.99, 50000)
    key = g.bin_number(lon, lat)
    assert key.min() >= 1 and key.max() <= g.total_bins()
    row, col = g.bin_row_col(key)
    assert np.array_equal(g.bin_key(row, col), key)
    clon, clat = g.bin_centre_row_col(row, col)
    assert np.abs(clat - lat).max() <= 90.0 / g.N_ROWS + 1e-9
    assert (np.abs(((clon - lon + 180) % 360) - 180) / (360 / g.bins_per_row()[row])).max() <= 0.5 + 1e-6
    east, west = g.bin_number([179.99999, -179.99999], [10.0, 10.0])
    (r1, c1), (r2, c2) = g.bin_row_col(east), g.bin_row_col(west)
    assert r1 == r2 and c1 == g.bins_per_row()[r1] - 1 and c2 == 0
    assert g.bin_number([0.0, 0.0, np.nan], [91.0, 0.0, 0.0]).tolist()[0] == 0
    assert g.bin_number([np.nan], [0.0])[0] == 0
    assert g.bin_number([-180.0], [-90.0])[0] == 1  # numbering starts at the south-west corner


def test_two_satellites_pixels_of_one_place_share_a_bin():
    centre_lon, centre_lat = g.bin_centre(g.bin_number(-83.6031, 43.9512))
    a = g.bin_number(centre_lon, centre_lat)
    b = g.bin_number(centre_lon + 1e-5, centre_lat - 1e-5)  # about a metre away
    assert a == b


def test_lake_bins_follow_the_centre_inside_rule():
    polygon = _lake_polygon(*LAKE_A)
    keys = g.lake_bins(polygon)
    assert np.array_equal(keys, _expected_bins(polygon))
    assert 20 <= keys.size <= 30  # 5 rows of about 5 bins
    rows, _ = g.bin_row_col(keys)
    assert rows.min() == ROW and rows.max() == ROW + 4


def test_lake_split_at_the_date_line_is_found_in_both_halves():
    east, west = box(179.98, 10.0, 180.0, 10.02), box(-180.0, 10.0, -179.98, 10.02)
    keys = g.lake_bins(__import__("shapely").MultiPolygon([east, west]))
    assert np.array_equal(keys, _expected_bins(east, west))
    lon, _ = g.bin_centre(keys)
    assert (lon > 0).any() and (lon < 0).any()


def test_lake_with_an_island_loses_the_island_bins():
    outer = _lake_polygon(*LAKE_A)
    island = box(*[v for v in (_lake_polygon(ROW + 1, 3, LAKE_A[2] + 1, 3).bounds)])
    from shapely.geometry import Polygon
    with_hole = Polygon(outer.exterior.coords, [island.exterior.coords])
    assert g.lake_bins(with_hole).size < g.lake_bins(outer).size


# ---------------------------------------------------------------------------
# Area-weighted binning
# ---------------------------------------------------------------------------

def test_aligned_pixels_keep_their_values_and_shifted_pixels_share_area():
    lattice = _lattice(ROW, 6, LAKE_A[2], 8, margin=0, east=0.0)
    per_row = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]) * 1e-3
    bins = g.pixels_to_bins(_pixels(lattice, per_row[lattice["i"]]))
    mean = g.area_means(bins)["CI_cyano"]
    inside, row = _interior(bins, lattice, np.arange(ROW, ROW + 6))
    assert inside.sum() >= 20
    assert np.allclose(mean[inside], per_row[row[inside] - ROW], rtol=0, atol=1e-12)
    assert np.allclose(bins["weight"][inside], g.bin_area_m2(row[inside]), rtol=1e-3)

    # Half a bin north: each bin gets half of two pixel rows, so its value is their mean.
    shifted = dict(lattice, lat=lattice["lat"] + 0.5 * 180.0 / g.N_ROWS)
    bins = g.pixels_to_bins(_pixels(shifted, per_row[lattice["i"]]))
    mean = g.area_means(bins)["CI_cyano"]
    inside, row = _interior(bins, lattice, np.arange(ROW + 1, ROW + 6))
    assert inside.sum() >= 20
    k = row[inside] - ROW
    # Pixel widths differ by ~5e-5 between rows, so the two halves are not weighted exactly equally.
    assert np.allclose(mean[inside], (per_row[k - 1] + per_row[k]) / 2, rtol=1e-3, atol=0)
    assert np.allclose(bins["weight"][inside], g.bin_area_m2(row[inside]), rtol=1e-3)


def test_invalid_pixels_do_not_dilute_the_area_mean():
    lattice = _lattice(ROW, 6, LAKE_A[2], 8, margin=0, east=0.0)
    per_row = np.full(6, 2e-3)
    valid = lattice["i"] != 2
    bins = g.pixels_to_bins(_pixels(lattice, per_row[lattice["i"]], valid))
    mean = g.area_means(bins)["CI_cyano"]
    inside, row = _interior(bins, lattice, [ROW + 2])
    assert inside.any() and np.isnan(mean[inside]).all()  # only an invalid pixel overlaps
    assert (bins["CI_cyano_weight"][inside] == 0).all() and (bins["weight"][inside] > 0).all()
    assert bins["cloud_excluded"][inside].all()

    shifted = dict(lattice, lat=lattice["lat"] + 0.5 * 180.0 / g.N_ROWS)
    bins = g.pixels_to_bins(_pixels(shifted, per_row[lattice["i"]], valid))
    mean = g.area_means(bins)["CI_cyano"]
    inside, row = _interior(bins, lattice, [ROW + 2, ROW + 3])  # half valid, half invalid
    assert inside.any()
    assert np.allclose(mean[inside], 2e-3, rtol=0, atol=1e-12)  # not diluted towards zero
    assert np.allclose(bins["CI_cyano_weight"][inside], 0.5 * g.bin_area_m2(row[inside]), rtol=2e-3)
    assert np.allclose(bins["weight"][inside], g.bin_area_m2(row[inside]), rtol=1e-3)


def test_footprint_area_is_conserved_across_the_date_line():
    row = 40000  # about 24 N
    lat = lat_edge(row) + (np.arange(6) + 0.5) * 180.0 / g.N_ROWS
    ii, jj = np.meshgrid(np.arange(6), np.arange(8), indexing="ij")
    lon = 180.0 - 4 * dlon(row) + (jj.ravel() + 0.5) * dlon(row)  # crosses 180 E / 180 W
    lattice = {"lat": lat[ii.ravel()], "lon": g.wrap_degrees(lon), "i": ii.ravel(), "row": ii.ravel(), "col": jj.ravel()}
    bins = g.pixels_to_bins(_pixels(lattice, np.full(48, 1e-3)))
    expected = 48 * g.BIN_HEIGHT_M ** 2 * 0.999
    assert bins["weight"].sum() == pytest.approx(expected, rel=0.01)
    lon_b, _ = g.bin_centre(bins["key"])
    assert (lon_b > 0).any() and (lon_b < 0).any()
    assert np.allclose(g.area_means(bins)["CI_cyano"], 1e-3)


def _tilted_footprints(lat0, lon0, n=25, seed=3):
    """Randomly sized, rotated and sheared pixel footprints around (lat0, lon0)."""
    rng = np.random.default_rng(seed)
    lon = lon0 + rng.uniform(-0.02, 0.02, n)
    lat = lat0 + rng.uniform(-0.02, 0.02, n)
    meridian = g.central_meridian(lon)
    x, y = g.project(lon, lat, meridian)
    angle = rng.uniform(0, 2 * np.pi, n)
    length_a, length_b = rng.uniform(250, 700, n), rng.uniform(250, 700, n)
    other = angle + np.pi / 2 + rng.uniform(-0.3, 0.3, n)
    return (x, y, length_a * np.cos(angle), length_a * np.sin(angle),
            length_b * np.cos(other), length_b * np.sin(other), meridian)


@pytest.mark.parametrize("lat0, lon0", [(0.3, 10.0), (43.6, -87.0), (-33.0, 151.0), (75.0, -100.0), (45.0, 179.98)])
def test_exact_overlap_matches_shapely_and_conserves_pixel_area(lat0, lon0):
    from shapely.geometry import Polygon
    x, y, ax, ay, bx, by, meridian = _tilted_footprints(lat0, lon0)
    pixel, key, weight = g.pixel_bin_overlaps(x, y, ax, ay, bx, by, meridian)
    area = np.abs(ax * by - ay * bx)
    assert np.allclose(np.bincount(pixel, weight, x.size), area, rtol=1e-9)  # every pixel fully placed

    corner_x, corner_y = g._footprint_corners(x, y, ax, ay, bx, by)
    corner_lon, corner_lat = g.unproject(corner_x, corner_y, meridian)
    for i in range(x.size):
        lons = corner_lon[i][0] + g.wrap_degrees(corner_lon[i] - corner_lon[i][0])
        polygon = Polygon(list(zip(lons, corner_lat[i])))
        for k, w in zip(key[pixel == i], weight[pixel == i]):
            row, col = (int(v[0]) for v in g.bin_row_col(np.array([k])))
            n_col = int(g.bins_per_row()[row])
            west, east = lon_edge(row, col), lon_edge(row, col + 1)
            overlap = sum(polygon.intersection(box(west + s, lat_edge(row), east + s, lat_edge(row + 1))).area
                          for s in (0.0, 360.0, -360.0))
            assert w == pytest.approx(overlap / polygon.area * area[i], rel=1e-7, abs=1e-6)
            assert 0 <= col < n_col


def test_exact_overlap_of_a_bin_sized_pixel_on_the_bin_is_the_bin():
    row = ROW
    col = first_col(row, -83.6)
    lat = (lat_edge(row) + lat_edge(row + 1)) / 2
    lon = (lon_edge(row, col) + lon_edge(row, col + 1)) / 2
    meridian = lon
    x, y = g.project(np.array([lon]), np.array([lat]), meridian)
    width = dlon(row) * np.pi / 180 * g.EARTH_RADIUS_M * np.cos(np.radians(lat))
    ones = np.ones(1)
    pixel, key, weight = g.pixel_bin_overlaps(x, y, 0 * ones, -g.BIN_HEIGHT_M * ones, width * ones, 0 * ones, meridian)
    own = key == int(g.bin_number(lon, lat))
    assert own.sum() == 1
    assert weight[own][0] == pytest.approx(width * g.BIN_HEIGHT_M, rel=1e-3)
    assert weight[~own].sum() < 1e-3 * weight.sum()  # at most slivers from rounding of the row width


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
    a = {"key": np.array([1, 2, 3]), "values": {"CI_cyano": np.array([1e-3, 0.0, np.nan])}}
    b = {"key": np.array([1, 2]), "values": {"CI_cyano": np.array([5e-4, 2e-3])}}
    valid, value, seen = merge_cells(keys, [a, b])
    assert valid.tolist() == [True, True, False, False]
    assert value[:2].tolist() == [1e-3, 2e-3]
    assert seen.tolist() == [2, 2, 0, 0]


# ---------------------------------------------------------------------------
# End to end: Michigan run, then Wisconsin run, same UTC day
# ---------------------------------------------------------------------------

def _footprint(first_row, n_rows, col0, n_cols):
    return mapping(box(lon_edge(first_row, col0 - 3), lat_edge(first_row - 3),
                       lon_edge(first_row, col0 + n_cols + 3), lat_edge(first_row + n_rows + 3)))


def _scene(name, satellite, start, footprint):
    return {"id": str(uuid.uuid4()), "name": name, "satellite": satellite,
            "output_region": {"country": "USA", "state_name": "Michigan", "state_id": "MI"},
            "start": start, "end": start, "geofootprint": footprint}


def _write_native(ws, scene, blocks):
    """blocks: (lake spec, value_of(i, j)); i is the bin-row offset inside the lake, None = invalid."""
    folder = ws.archive(scene).parent
    folder.mkdir(parents=True, exist_ok=True)
    columns = {name: [] for name in ("lat", "lon", "row", "col", "value")}
    for k, (spec, value_of) in enumerate(blocks):
        lattice = _lattice(*spec)
        n_rows = spec[1]
        columns["lat"].append(lattice["lat"])
        columns["lon"].append(lattice["lon"])
        columns["row"].append(lattice["row"])
        columns["col"].append(lattice["col"] + 100 * k)  # blocks never touch in source indices
        columns["value"].append(np.array([np.nan if (v := value_of(int(np.clip(i, 0, n_rows - 1)), int(j))) is None else v
                                          for i, j in zip(lattice["i"], lattice["j"])]))
    lat, lon, rows, cols, value = (np.concatenate(columns[n]) for n in ("lat", "lon", "row", "col", "value"))
    valid = np.isfinite(value)
    with netCDF4.Dataset(folder / f"{scene['id']}_full_cfg_lakepixels.nc", "w") as ds:
        ds.createDimension("obs", rows.size)
        for name, dtype, data in [
            ("latitude", "f8", lat), ("longitude", "f8", lon),
            ("source_row", "i4", rows), ("source_col", "i4", cols),
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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("cyanotrend.nasa_cyan.discover_tiles", lambda day: [])
    ws = Workspace.create(tmp_path / "out", tmp_path / "refs", tmp_path / "scratch", tmp_path / "ocssw")
    ws.ensure()
    return ws


def _lakes():
    return gpd.GeoDataFrame({
        "Hylak_id": [101, 202], "Lake_name": ["Lake A", "Lake B"], "Lake_area": [2.25, 2.25],
        "latitude": [45.4, 45.4], "longitude": [-83.6, -83.5],
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
    s3a1 = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                  "S3A", "2024-08-14T16:15:18Z", _footprint(*BOTH))
    s3a1_nr = dict(_scene(s3a1["name"].replace("20240815T200533", "20240814T182001").replace("_NT_", "_NR_"),
                          "S3A", s3a1["start"], s3a1["geofootprint"]))
    s3b1 = _scene("S3B_OL_1_EFR____20240814T155400_20240814T155700_20240815T190000_0179_096_011_2340_PS2_O_NT_004",
                  "S3B", "2024-08-14T15:54:00Z", _footprint(*LAKE_A))
    s3b2 = _scene("S3B_OL_1_EFR____20240814T155700_20240814T160000_20240815T190100_0179_096_011_2520_PS2_O_NT_004",
                  "S3B", "2024-08-14T15:57:00Z", _footprint(*LAKE_B))
    catalogue_scenes = [s3a1, s3a1_nr, s3b1, s3b2]
    s3b2["output_region"] = {"country": "USA", "state_name": "Wisconsin", "state_id": "WI"}

    def catalogue(a, b, bbox=None):
        assert a == DAY and b == "2024-08-15" and bbox is not None  # solar date at lon -83.6 spans two UTC days
        return catalogue_scenes

    ledger = Ledger(ws.db)
    # Michigan plan: S3A-1 and S3B-1. (S3B-2 is planned later, by Wisconsin.)
    ledger.enqueue({"id": "mi", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [s3a1, s3b1]}, ws.output / "plans" / "mi.json")
    # Lake A, five bin rows (i = 0..4). S3A-1 detects rows 0-1, S3B-1 detects rows 2-3 (higher),
    # and row 4 is never valid in either pass.
    _write_native(ws, s3a1, [(LAKE_A, lambda i, j: None if i == 4 else (1e-3 if i < 2 else 0.0)),
                             (LAKE_B, lambda i, j: 0.0)])
    _write_native(ws, s3b1, [(LAKE_A, lambda i, j: None if i == 4 else (5e-4 if i < 2 else 2e-3))])
    for s in (s3a1, s3b1):
        ledger.update(s["id"], status="done", stage="complete", archive=str(ws.archive(s)))

    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes())
    assert result["ok"], result
    rows, manifest, folder = _read(ws)
    assert set(rows) == {101}  # Lake B waits for S3B-2
    a = rows[101]
    per_row_a = _bins_per_row(_lake_polygon(*LAKE_A))
    n_a = sum(per_row_a.values())
    assert a["day_status"] == "complete" and a["satellites"] == "S3A;S3B" and a["scenes_used"] == "2"
    assert a["within_pass_method"] == "area"
    assert int(a["Lake_cells"]) == n_a
    assert int(a["CI_cyano_detectable_cells"]) == sum(per_row_a[ROW + r] for r in range(4))
    assert int(a["Non_detectable_cells"]) == 0
    assert int(a["No_valid_data_cells"]) == per_row_a[ROW + 4]
    assert float(a["CI_cyano_max"]) == pytest.approx(2e-3, rel=1e-5)
    assert manifest["waiting_lakes"] == []
    assert manifest["grid"]["rows"] == 69120 and manifest["within_pass_method"] == "area"
    _, wi_manifest, _ = _read(ws, "Wisconsin")
    assert [w["Hylak_id"] for w in wi_manifest["waiting_lakes"]] == [202]
    assert wi_manifest["waiting_lakes"][0]["missing_scenes"] == [s3b2["name"]]
    s3a_entry = next(x for x in manifest["acquisitions"] if x["satellite"] == "S3A")
    assert s3a_entry["status"] == "done" and len(s3a_entry["versions"]) == 2  # NR version not needed
    assert (ws.archive(s3a1).parent / f"{s3a1['id']}_cfg_isin_grid.nc").exists()
    with netCDF4.Dataset(folder / f"{DAY}_grid.nc") as ds:
        assert ds.dimensions["bin"].size == n_a
        assert sorted(set(ds["DN"][:].tolist()))[-1] == 255
        assert set(ds["scenes_valid"][:].tolist()) == {0, 2}  # two passes see rows 0-3
        keys = ds["bin"][:]
        assert np.array_equal(np.sort(keys), g.lake_bins(_lake_polygon(*LAKE_A)))
        assert np.allclose(ds["bin_area"][:], g.bin_area_m2(ds["row"][:]), rtol=1e-6)
        lon, lat = g.bin_centre(keys)
        assert np.allclose(ds["longitude"][:], lon) and np.allclose(ds["latitude"][:], lat)

    # Wisconsin plan: S3A-1 and S3B-1 are already processed; only S3B-2 is new.
    ledger.enqueue({"id": "wi", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [s3a1, s3b1, s3b2]}, ws.output / "plans" / "wi.json")
    assert ledger.scene(s3a1["id"])["status"] == "done"
    _write_native(ws, s3b2, [(LAKE_B, lambda i, j: 3e-3 if i == 2 else 0.0)])
    ledger.update(s3b2["id"], status="done", stage="complete", archive=str(ws.archive(s3b2)))
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        assert run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes())["ok"]
    rows2, manifest2, _ = _read(ws)
    wi_rows, wi_manifest2, wi_folder = _read(ws, "Wisconsin")
    assert set(rows2) == {101} and set(wi_rows) == {202}
    assert manifest2["waiting_lakes"] == wi_manifest2["waiting_lakes"] == []
    assert rows2[101] == a  # unchanged lake: identical row
    b = wi_rows[202]
    per_row_b = _bins_per_row(_lake_polygon(*LAKE_B))
    n_b = sum(per_row_b.values())
    assert b["day_status"] == "complete" and b["states"] == "Michigan; Wisconsin"
    assert int(b["Lake_cells"]) == n_b
    assert int(b["CI_cyano_detectable_cells"]) == per_row_b[ROW + 2]
    assert int(b["Non_detectable_cells"]) == n_b - per_row_b[ROW + 2]
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
    _write_native(ws, s3a1, [(LAKE_A, lambda i, j: 1e-3)])
    ledger.update(s3a1["id"], status="done", stage="complete", archive=str(ws.archive(s3a1)))
    ledger.update(s3b1["id"], status="failed", stage="failed", error="download failed")
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=lambda a, b, bbox=None: [s3a1, s3b1], lakes=_lakes().iloc[[0]])
    rows, manifest, _ = _read(ws)
    assert rows[101]["day_status"] == "partial"
    assert rows[101]["failed_or_unavailable_scenes"] == s3b1["name"]
    assert int(rows[101]["CI_cyano_detectable_cells"]) == int(rows[101]["Lake_cells"]) > 0
    assert any("max-scenes" in w for w in manifest["warnings"])
    assert result["days"][0]["merged_partial"] == 1


def test_daily_command_is_available():
    from cyanotrend.cli import parser
    a = parser().parse_args(["daily", "--start", DAY, "--end", DAY])
    assert a.command == "daily" and a.method == "area"
    a = parser().parse_args(["plan", "--start", DAY, "--end", DAY, "--country", "USA", "--state-id", "MI"])
    assert a.scene_rule == "lake-complete"
    with pytest.raises(SystemExit):
        parser().parse_args(["daily", "--start", DAY, "--end", DAY, "--method", "max"])


# ---------------------------------------------------------------------------
# Local solar date
# ---------------------------------------------------------------------------

def test_solar_date_shifts_utc_by_longitude_over_15_hours():
    from datetime import date
    assert g.solar_date("2024-08-14T16:15:18Z", -83.6) == date(2024, 8, 14)   # 10:41 local
    assert g.solar_date("2024-08-13T23:50:00Z", 150.0) == date(2024, 8, 14)   # 09:50 local, UTC date is a day earlier
    assert g.solar_date("2024-08-14T00:10:00Z", -150.0) == date(2024, 8, 13)  # a UTC-early pass is the previous local day
    assert g.solar_date("2024-08-14T01:00:00+02:00", 0.0) == date(2024, 8, 13)  # offsets are honoured
    assert g.utc_dates_of_solar_date(date(2024, 8, 14), -83.6, -83.5) == (date(2024, 8, 14), date(2024, 8, 15))
    assert g.utc_dates_of_solar_date(date(2024, 8, 14), 149.0, 151.0) == (date(2024, 8, 13), date(2024, 8, 14))


FAR_EAST = (ROW, 5, first_col(ROW, 150.0), 5)  # a lake near 150 E, where UTC and solar dates differ


def _far_lake():
    return gpd.GeoDataFrame({
        "Hylak_id": [303], "Lake_name": ["Lake East"], "Lake_area": [2.25],
        "latitude": [45.4], "longitude": [150.0], "states": ["Sakhalin"], "state_ids": ["SA"],
        "country_iso3": ["RUS"], "state_name": ["Sakhalin"], "admin_match_status": ["matched"],
    }, geometry=[_lake_polygon(*FAR_EAST)], crs=4326)


def test_pass_on_the_previous_utc_day_belongs_to_the_solar_date_of_the_lake(workspace):
    ws = workspace
    scene = _scene("S3A_OL_1_EFR____20240813T235000_20240813T235300_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                   "S3A", "2024-08-13T23:50:00Z", _footprint(*FAR_EAST))
    ledger = Ledger(ws.db)
    ledger.enqueue({"id": "p", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [scene]}, ws.output / "plans" / "p.json")
    _write_native(ws, scene, [(FAR_EAST, lambda i, j: 1e-3)])
    ledger.update(scene["id"], status="done", stage="complete", archive=str(ws.archive(scene)))
    asked = []

    def catalogue(a, b, bbox=None):
        asked.append((a, b))
        return [scene]

    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        for day in ("2024-08-13", "2024-08-14", "2024-08-15"):
            result = run_daily(ws, day, day, catalogue=catalogue, lakes=_far_lake())["days"][0]
            folder = ws.output / "daily" / "RUS" / "Sakhalin" / day[:4] / day[5:7] / day[8:]
            if day == "2024-08-14":
                with (folder / f"{day}_lakes.csv").open() as f:
                    (row,) = list(csv.DictReader(f))
                assert row["date_solar"] == day and row["Hylak_id"] == "303" and row["day_status"] == "complete"
                assert row[NASA_DETECTION_COLUMN] == ""  # NASA's product is by UTC date: no comparison here
                assert json.loads((folder / f"{day}_manifest.json").read_text())["date_solar"] == day
            else:
                assert not folder.exists()  # the pass is not on this solar date
    assert ("2024-08-13", "2024-08-14") in asked  # the catalogue window follows the lake longitude


# ---------------------------------------------------------------------------
# Every country has the same layout; a shared lake has one record
# ---------------------------------------------------------------------------

def _regions():
    """Michigan covers lake A only; Wisconsin covers part of lake A and all of lake B."""
    def block(col0, col1):
        return box(lon_edge(ROW, col0), lat_edge(ROW - 6), lon_edge(ROW, col1), lat_edge(ROW + 11))
    first = LAKE_A[2]
    return gpd.GeoDataFrame({
        "country_iso3": ["USA", "USA"], "shapeName": ["Michigan", "Wisconsin"], "shapeID": ["MI", "WI"],
    }, geometry=[block(first - 2, first + 8), block(first + 3, first + 25)], crs=4326)


def _planned(ws, scene, order):
    ledger = Ledger(ws.db)
    for k, (state_id, name) in enumerate(order):
        ledger.enqueue({"id": state_id.lower(), "created": f"2024-09-0{k + 1}T00:00:00Z", "reference_id": "ref",
                        "selection": {"country": "USA", "state_id": state_id, "state_name": name},
                        "scenes": [scene]}, ws.output / "plans" / f"{state_id}.json")
    _write_native(ws, scene, [(LAKE_A, lambda i, j: 1e-3), (LAKE_B, lambda i, j: 2e-3)])
    ledger.update(scene["id"], status="done", stage="complete", archive=str(ws.archive(scene)))


@pytest.mark.parametrize("order, owner", [
    ([("MI", "Michigan"), ("WI", "Wisconsin")], "Michigan"),
    ([("WI", "Wisconsin"), ("MI", "Michigan")], "Wisconsin"),
])
def test_lake_shared_by_two_regions_has_one_record_under_the_first_processed_region(workspace, order, owner):
    ws = workspace
    scene = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                   "S3A", "2024-08-14T16:15:18Z", _footprint(*BOTH))
    _planned(ws, scene, order)
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=lambda a, b, bbox=None: [scene], lakes=_lakes(), admin=_regions())
    assert result["ok"], result
    shared, _, shared_folder = _read(ws, owner)
    assert 101 in shared  # lake A: in both regions, filed under the first plan's region
    wisconsin_rows, _, _ = _read(ws, "Wisconsin")
    assert 202 in wisconsin_rows  # lake B: only inside Wisconsin, whichever plan came first
    assert (101 in wisconsin_rows) == (owner == "Wisconsin")  # never filed twice
    manifest = json.loads((shared_folder / f"{DAY}_manifest.json").read_text())
    assert manifest["output_region"]["lake_basis"]["101"] == "first_plan_region"
    with (ws.output / "daily/master_merged_daily.csv").open() as f:
        master = list(csv.DictReader(f))
    assert sorted(int(r["Hylak_id"]) for r in master) == [101, 202]  # one record per lake and date
    # The scene was processed once: both plans listed it, but it lives in the first region's folder.
    assert len(list(ws.archive(scene).parent.glob("*_isin_grid.nc"))) == 1


def test_other_countries_and_unmatched_lakes_use_the_same_layout(workspace):
    ws = workspace
    scene = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                   "S3A", "2024-08-14T16:15:18Z", _footprint(*BOTH))
    ledger = Ledger(ws.db)
    ledger.enqueue({"id": "p", "created": "now", "reference_id": "ref", "selection": {},
                    "scenes": [scene]}, ws.output / "plans" / "p.json")
    _write_native(ws, scene, [(LAKE_A, lambda i, j: 1e-3), (LAKE_B, lambda i, j: 2e-3)])
    ledger.update(scene["id"], status="done", stage="complete", archive=str(ws.archive(scene)))
    lakes = _lakes()
    lakes.loc[0, ["country_iso3", "state_name"]] = ["CAN", "Ontario"]          # another country: same layout
    lakes.loc[1, ["country_iso3", "state_name", "admin_match_status"]] = ["", "", "unmatched"]  # pour point off land
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        assert run_daily(ws, DAY, DAY, catalogue=lambda a, b, bbox=None: [scene], lakes=lakes)["ok"]
    canada = ws.output / "daily" / "CAN" / "Ontario" / "2024" / "08" / "14" / f"{DAY}_lakes.csv"
    with canada.open() as f:
        assert [int(r["Hylak_id"]) for r in csv.DictReader(f)] == [101]
    rows, manifest, _ = _read(ws, "Michigan")  # lake B: unmatched, so the region that processed the scene
    assert list(rows) == [202]
    assert manifest["output_region"]["lake_basis"] == {"202": "first_scene_region"}


# ---------------------------------------------------------------------------
# Daily merge limited to one state
# ---------------------------------------------------------------------------

def _master(ws):
    with (ws.output / "daily/master_merged_daily.csv").open() as f:
        return {int(r["Hylak_id"]): r for r in csv.DictReader(f)}


def test_daily_limited_to_one_state_replaces_only_that_states_lakes(workspace):
    ws = workspace
    scene = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                   "S3A", "2024-08-14T16:15:18Z", _footprint(*BOTH))
    _planned(ws, scene, [("MI", "Michigan"), ("WI", "Wisconsin")])  # lake A is filed under Michigan, lake B under Wisconsin
    catalogue = lambda a, b, bbox=None: [scene]
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        assert run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes(), admin=_regions())["ok"]
    master_before = _master(ws)
    michigan_files = {p.name: p.read_bytes() for p in (ws.output / "daily/USA/Michigan/2024/08/14").iterdir()}
    assert set(master_before) == {101, 202} and int(master_before[202]["CI_cyano_detectable_cells"]) > 0

    # Lake B no longer shows any detection; only a run limited to Wisconsin may notice.
    _write_native(ws, scene, [(LAKE_A, lambda i, j: 9e-3), (LAKE_B, lambda i, j: 0.0)])
    for old in ws.archive(scene).parent.glob("*_isin_grid.nc"):
        old.unlink()
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes(), admin=_regions(),
                           country="usa", state_id="WI")
    assert result["ok"], result
    assert result["restricted_to"] == {"country": "USA", "state_id": "WI", "state_name": "Wisconsin"}
    day = result["days"][0]
    assert day["skipped_filed_under_other_regions"] == 1  # lake A is Michigan's record

    master = _master(ws)
    assert master[101] == master_before[101]  # Michigan's lake: untouched
    assert int(master[202]["CI_cyano_detectable_cells"]) == 0  # Wisconsin's lake: replaced
    assert {p.name: p.read_bytes() for p in (ws.output / "daily/USA/Michigan/2024/08/14").iterdir()} == michigan_files
    rows, manifest, _ = _read(ws, "Wisconsin")
    assert set(rows) == {202} and manifest["run_scope"]["restricted_to"]["state_id"] == "WI"

    # Limited to Michigan: lake A is the only candidate, and lake B stays as it is.
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        result = run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes(), admin=_regions(),
                           country="USA", state_id="MI")
    assert result["ok"] and result["days"][0]["skipped_filed_under_other_regions"] == 0
    master = _master(ws)
    assert set(master) == {101, 202} and int(master[202]["CI_cyano_detectable_cells"]) == 0
    assert float(master[101]["CI_cyano_max"]) == pytest.approx(9e-3, rel=1e-5)  # Michigan's lake: replaced
    assert set(_read(ws, "Wisconsin")[0]) == {202}


def test_daily_state_limit_needs_both_arguments_and_a_known_state(workspace):
    ws = workspace
    scene = _scene("S3A_OL_1_EFR____20240814T161518_20240814T161818_20240815T200533_0179_115_368_2340_PS1_O_NT_004",
                   "S3A", "2024-08-14T16:15:18Z", _footprint(*BOTH))
    _planned(ws, scene, [("MI", "Michigan")])
    catalogue = lambda a, b, bbox=None: [scene]
    with patch("cyanotrend.references.manifest", return_value={"reference_id": "ref"}):
        with pytest.raises(ValueError, match="together"):
            run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes(), admin=_regions(), country="USA")
        with pytest.raises(ValueError, match="not found"):
            run_daily(ws, DAY, DAY, catalogue=catalogue, lakes=_lakes(), admin=_regions(),
                      country="USA", state_id="XX")
    from cyanotrend.cli import parser
    a = parser().parse_args(["daily", "--start", DAY, "--end", DAY, "--country", "USA", "--state-id", "MI"])
    assert (a.country, a.state_id) == ("USA", "MI")
