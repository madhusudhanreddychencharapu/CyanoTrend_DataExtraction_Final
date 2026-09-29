"""One reusable HydroLAKES source archive and geoBoundaries ADM0/ADM1 files.

No target_hydrolakes.gpkg is generated. Lake coordinates are original Pour_lat /
Pour_long values. An unambiguous boundary match supplies state attributes; gaps
and multiple matches remain blank and are reported rather than guessed.
"""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import zipfile
from .common import atomic_json, digest, file_lock, utcnow

HYDRO_URL = "https://data.hydrosheds.org/file/hydrolakes/HydroLAKES_polys_v10_shp.zip"
GEOB = "https://www.geoboundaries.org/api/current/gbOpen/{country}/{level}/"
MIN_AREA = 1.3


def fetch(url, path):
    import requests

    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    with requests.get(url, stream=True, timeout=(30, 180)) as response:
        response.raise_for_status()
        with temp.open("wb") as f:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    temp.replace(path)
    return path


def prepare(workspace, countries=None):
    """Download globally by default. Frozen indexes make interrupted setup resumable."""
    import requests

    workspace.ensure()
    root = workspace.references
    with file_lock(root / ".prepare.lock"):
        if (root / "manifest.json").exists():
            return json.loads((root / "manifest.json").read_text())
        hydro = fetch(HYDRO_URL, root / "HydroLAKES_polys_v10_shp.zip")
        with zipfile.ZipFile(hydro) as z:
            if z.testzip():
                raise ValueError("HydroLAKES ZIP failed CRC validation")
        requested = set(c.upper() for c in countries) if countries else None
        entries = []
        for level in ("ADM0", "ADM1"):
            index = root / f"{level}_index.json"
            if not index.exists():
                response = requests.get(
                    GEOB.format(country="ALL", level=level), timeout=120
                )
                response.raise_for_status()
                atomic_json(index, response.json())
            records = json.loads(index.read_text())
            if not isinstance(records, list):
                raise ValueError("Unexpected geoBoundaries index format")
            if requested:
                available = {r["boundaryISO"] for r in records}
                if requested - available:
                    raise ValueError(
                        f"{level} unavailable for {sorted(requested-available)}"
                    )
            for r in records:
                country = r["boundaryISO"]
                if requested and country not in requested:
                    continue
                # Same simplified-boundary preference as the reference notebook.
                url = r.get("simplifiedGeometryGeoJSON") or r["gjDownloadURL"]
                p = fetch(url, root / "boundaries" / f"{country}_{level}.geojson")
                entries.append(
                    {
                        "country": country,
                        "level": level,
                        "path": str(p.relative_to(root)),
                        "sha256": digest(p),
                        "url": url,
                        "boundary_id": r.get("boundaryID"),
                        "year": r.get("boundaryYearRepresented"),
                        "license": r.get("boundaryLicense"),
                    }
                )
        payload = {
            "created": utcnow(),
            "hydrolakes": {
                "path": hydro.name,
                "url": HYDRO_URL,
                "sha256": digest(hydro),
            },
            "boundaries": entries,
            "coverage": "global" if requested is None else sorted(requested),
            "min_lake_area_km2": MIN_AREA,
            "coordinate_fields": ["Pour_lat", "Pour_long"],
        }
        payload["reference_id"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        atomic_json(root / "manifest.json", payload)
        return payload


def manifest(workspace):
    p = workspace.references / "manifest.json"
    if not p.is_file():
        raise ValueError("Reference data is not prepared; run prepare first")
    return json.loads(p.read_text())


def boundaries(workspace, level="ADM1"):
    import geopandas as gpd
    import pandas as pd

    frames = []
    for entry in manifest(workspace)["boundaries"]:
        if entry["level"] != level:
            continue
        g = gpd.read_file(workspace.references / entry["path"], engine="pyogrio")
        if g.crs is None:
            raise ValueError(f"Boundary CRS missing: {entry['path']}")
        g = g.to_crs(4326)
        if not {"shapeName", "shapeID"} <= set(g):
            raise ValueError("Boundary file lacks shapeName/shapeID")
        g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
        g.geometry = g.geometry.make_valid()
        g["country_iso3"] = entry["country"]
        frames.append(g[["country_iso3", "shapeName", "shapeID", "geometry"]])
    if not frames:
        raise ValueError(f"No {level} reference boundaries available")
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=4326)


def load_lakes(workspace, with_admin=True, region="WORLD"):
    import geopandas as gpd

    m = manifest(workspace)
    archive = workspace.references / m["hydrolakes"]["path"]
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if len(names) != 1:
        raise ValueError("Expected one HydroLAKES polygon shapefile")
    g = gpd.read_file(
        f"/vsizip/{archive}/{names[0]}", engine="pyogrio", where="Lake_area >= 1.3"
    )
    required = {"Hylak_id", "Lake_area", "Pour_lat", "Pour_long"}
    if not required <= set(g):
        raise ValueError(f"Missing HydroLAKES fields: {required-set(g)}")
    if g.crs is None:
        raise ValueError("HydroLAKES CRS missing")
    g = g.to_crs(4326)
    g = (
        g[g.geometry.notna() & ~g.geometry.is_empty & (g.Lake_area >= MIN_AREA)]
        .copy()
        .reset_index(drop=True)
    )
    g.geometry = g.geometry.make_valid()
    if g.Hylak_id.duplicated().any():
        raise ValueError("Duplicate HydroLAKES IDs in source")
    g["latitude"] = g["Pour_lat"]
    g["longitude"] = g["Pour_long"]
    if region not in {"WORLD", "USA"}:
        raise ValueError("HydroLAKES region must be WORLD or USA")
    if region == "USA":
        # Same Census polygon source and full-polygon intersection as cell 29.
        from shapely import union_all
        with file_lock(workspace.references / ".usa-boundary.lock"):
            states_zip = fetch(
                "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_5m.zip",
                workspace.references / "cb_2025_us_state_5m.zip",
            )
        states = gpd.read_file(f"zip://{states_zip}").to_crs(4326)
        g = g[g.intersects(union_all(states.geometry.values))].copy().reset_index(drop=True)
    if with_admin:
        g = assign_states(g, boundaries(workspace))
    return g


def assign_states(lakes, admin):
    import geopandas as gpd

    lakes = lakes.copy()
    # Numeric source coordinates remain untouched; invalid coordinates are not replaced by centroids.
    good = lakes.latitude.between(-90, 90) & lakes.longitude.between(-180, 180)
    points = gpd.GeoDataFrame(
        index=lakes.index[good],
        geometry=gpd.points_from_xy(
            lakes.loc[good, "longitude"], lakes.loc[good, "latitude"]
        ),
        crs=4326,
    )
    joined = gpd.sjoin(points, admin, how="left", predicate="intersects")
    matched = joined[joined["shapeID"].notna()]
    unique = matched[~matched.index.duplicated(keep=False)]
    lakes["state_name"] = ""
    lakes["state_id"] = ""
    lakes["country_iso3"] = ""
    for target, source in [
        ("state_name", "shapeName"),
        ("state_id", "shapeID"),
        ("country_iso3", "country_iso3"),
    ]:
        lakes.loc[unique.index, target] = unique[source].astype(str)
    lakes["admin_match_status"] = "unmatched"
    lakes.loc[unique.index, "admin_match_status"] = "matched"
    multiple = matched.index[matched.index.duplicated(keep=False)].unique()
    lakes.loc[multiple, "admin_match_status"] = "ambiguous"
    return lakes


def verify(workspace):
    m = manifest(workspace)
    errors = []
    for item in [m["hydrolakes"], *m["boundaries"]]:
        p = workspace.references / item["path"]
        if not p.is_file() or digest(p) != item["sha256"]:
            errors.append(item["path"])
    return {
        "ok": not errors,
        "changed_or_missing": errors,
        "reference_id": m["reference_id"],
    }
