"""Frozen scene plans, with administrative regions as discovery filters only."""

from __future__ import annotations
from datetime import date, timedelta
import hashlib
import json
import uuid
from .common import atomic_json, file_lock, utcnow
from .ledger import Ledger
from . import references
from .identity import science_fingerprint


def date_chunks(start, end):
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ValueError("Start date must not exceed end date")
    while first <= last:
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(last, next_month - timedelta(days=1))
        yield first.isoformat(), stop.isoformat()
        first = stop + timedelta(days=1)


def create_plan(workspace, start, end, country=None, state_id=None, max_scenes=None):
    from .core.catalogue import search_olci_l1_catalog, candidate_lakes_for_scene
    from shapely import union_all

    workspace.ensure()
    m = references.manifest(workspace)
    if m["coverage"] != "global":
        raise ValueError(
            "Full-scene processing requires globally prepared references; use a global reference directory"
        )
    intervals = list(date_chunks(start, end))
    lakes = references.load_lakes(workspace)
    pool = lakes
    bbox = None
    if state_id and not country:
        raise ValueError("A state selection also requires --country")
    if country:
        admin = references.boundaries(workspace, "ADM1" if state_id else "ADM0")
        chosen = admin[admin.country_iso3 == country.upper()]
        if state_id:
            chosen = chosen[chosen.shapeID.astype(str) == state_id]
        if chosen.empty:
            raise ValueError("Country/state not found; inspect regions first")
        geom = union_all(chosen.geometry)
        pool = lakes[lakes.intersects(geom)].copy()
        bbox = tuple(float(x) for x in geom.bounds)
    if pool.empty:
        raise ValueError("No eligible lakes in selected region")
    scenes = {}
    for a, b in intervals:
        # Date shards bound catalogue pagination; no arbitrary catalogue cap is
        # applied before lake filtering. max_scenes counts eligible scene IDs.
        for scene in search_olci_l1_catalog(a, b, bbox=bbox):
            if scene["satellite"] not in ("S3A", "S3B"):
                continue
            uuid.UUID(scene["id"])
            if scene["id"] in scenes:
                continue
            if not scene.get("geofootprint"):
                raise ValueError(f"Missing catalogue footprint: {scene['id']}")
            if candidate_lakes_for_scene(scene, pool).empty:
                continue
            all_hits = candidate_lakes_for_scene(scene, lakes)
            groups = all_hits[
                ["country_iso3", "state_id", "state_name"]
            ].drop_duplicates()
            scene["regions"] = [
                {
                    "country": r.country_iso3,
                    "state_id": r.state_id,
                    "state_name": r.state_name,
                }
                for r in groups.itertuples()
            ]
            scene["candidate_lakes"] = len(all_hits)
            scenes[scene["id"]] = scene
            if max_scenes and len(scenes) >= max_scenes:
                break
        if max_scenes and len(scenes) >= max_scenes:
            break
    selection = {
        "start": start,
        "end": end,
        "country": country,
        "state_id": state_id,
        "max_scenes": max_scenes,
    }
    payload = {
        "schema": "cyanotrend-plan-1",
        "science_fingerprint": science_fingerprint(),
        "reference_id": m["reference_id"],
        "selection": selection,
        "scenes": list(scenes.values()),
        "created": utcnow(),
    }
    identity = {
        "selection": selection,
        "reference_id": m["reference_id"],
        "scene_ids": sorted(scenes),
        "science_fingerprint": science_fingerprint(),
    }
    payload["id"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()[:24]
    path = workspace.output / "plans" / f"{payload['id']}.json"
    # Register only once the complete plan is atomically on disk. A service
    # also imports saved-but-unregistered plans after an interrupted CLI call.
    with file_lock(workspace.output / ".plan.lock"):
        if not path.exists():
            atomic_json(path, payload)
        else:
            payload = json.loads(path.read_text())
        Ledger(workspace.db).enqueue(payload, path)
    return {
        "plan": str(path),
        "id": payload["id"],
        "eligible_scenes": len(scenes),
        "selection": selection,
    }


def import_plans(workspace, ledger):
    with ledger.connect() as c:
        known = {r[0] for r in c.execute("SELECT id FROM plans")}
    for p in sorted((workspace.output / "plans").glob("*.json")):
        if p.stem in known:
            continue
        plan = json.loads(p.read_text())
        if plan.get("schema") != "cyanotrend-plan-1" or plan.get("id") != p.stem:
            raise ValueError(f"Invalid saved plan: {p.name}")
        if plan["reference_id"] != references.manifest(workspace)["reference_id"]:
            raise ValueError(f"Saved plan references a different dataset: {p.name}")
        if plan.get("science_fingerprint") != science_fingerprint():
            raise ValueError(
                f"Saved plan uses a different scientific implementation: {p.name}"
            )
        for scene in plan["scenes"]:
            uuid.UUID(scene["id"])
        ledger.enqueue(plan, p)
