"""Which country/state folder holds a lake's daily record, and which states a lake touches.

A lake that borders several states or countries (Lake Michigan, Lake Erie) gets exactly one
record per date. It is filed under the region whose plan processed it first:

1. the earliest-created plan whose country/state contains (touches) the lake and that listed
   one of the lake's scenes of the date;
2. otherwise the state holding the lake's HydroLAKES pour point (when unambiguous);
3. otherwise the region of the first plan that listed the scene.

Scenes are stored once, in the first plan's region (the ledger ignores a scene that already
exists), so a later plan for a neighbouring region reuses the processed scene and creates no
second record.
"""
from __future__ import annotations

import json

import numpy as np


def lake_longitude(lake):
    """Longitude that fixes the lake's local solar date: pour point, else a point inside the lake."""
    try:
        value = float(getattr(lake, "longitude", np.nan))
    except (TypeError, ValueError):
        value = np.nan
    if np.isfinite(value) and -180.0 <= value <= 180.0:
        return value
    return float(lake.geometry.representative_point().x)


def with_states(scope, admin):
    """Add `states` / `state_ids`: every state or province the lake touches, in any country."""
    import geopandas as gpd

    if {"states", "state_ids"} <= set(scope.columns):
        return scope
    hits = gpd.sjoin(scope[["Hylak_id", "geometry"]], admin[["shapeName", "shapeID", "geometry"]],
                     how="inner", predicate="intersects")
    states = hits.groupby("Hylak_id").agg(
        states=("shapeName", lambda s: "; ".join(sorted(set(map(str, s))))),
        state_ids=("shapeID", lambda s: "; ".join(sorted(set(map(str, s))))),
    )
    out = scope.drop(columns=["states", "state_ids"], errors="ignore").merge(
        states, left_on="Hylak_id", right_index=True, how="left")
    out[["states", "state_ids"]] = out[["states", "state_ids"]].fillna("")
    return out.reset_index(drop=True)


class RegionOwners:
    """Decides the country/state folder of a lake's record (rules in the module docstring)."""

    def __init__(self, plans, membership, load_admin):
        """plans: ledger rows (id, created, metadata JSON); membership: plan id -> scene ids."""
        self.membership = membership
        self.load_admin = load_admin
        self._admin = None
        self._geometry = {}
        self.plans = []
        for plan in sorted(plans, key=lambda p: (str(p["created"]), str(p["id"]))):
            selection = json.loads(plan["metadata"] or "{}")
            if selection.get("country") and selection.get("state_id"):
                self.plans.append({
                    "id": plan["id"],
                    "region": {"country": selection["country"], "state_id": str(selection["state_id"]),
                               "state_name": selection.get("state_name") or ""},
                })

    @property
    def admin(self):
        if self._admin is None:
            self._admin = self.load_admin()
        return self._admin

    def with_states(self, scope):
        if {"states", "state_ids"} <= set(scope.columns):
            return scope
        return with_states(scope, self.admin)

    def _region_geometry(self, region):
        key = (region["country"], region["state_id"])
        if key not in self._geometry:
            from shapely import prepared, union_all
            admin = self.admin
            chosen = admin[(admin.country_iso3 == region["country"].upper())
                           & (admin.shapeID.astype(str) == region["state_id"])]
            self._geometry[key] = prepared.prep(union_all(chosen.geometry)) if len(chosen) else None
        return self._geometry[key]

    def owner(self, lake, scene_ids, first_scene_region=None):
        """Folder region of one lake for one date, or None. `basis` says which rule decided."""
        ids = set(scene_ids)
        for plan in self.plans:
            if not ids & self.membership.get(plan["id"], set()):
                continue
            geometry = self._region_geometry(plan["region"])
            if geometry is not None and geometry.intersects(lake.geometry):
                return {**plan["region"], "basis": "first_plan_region"}
        country = str(getattr(lake, "country_iso3", "") or "")
        state = str(getattr(lake, "state_name", "") or "")
        if getattr(lake, "admin_match_status", "matched") == "matched" and country and state:
            return {"country": country, "state_id": str(getattr(lake, "state_id", "") or ""),
                    "state_name": state, "basis": "pour_point"}
        if first_scene_region and first_scene_region.get("country") and first_scene_region.get("state_name"):
            return {"country": first_scene_region["country"],
                    "state_id": str(first_scene_region.get("state_id", "")),
                    "state_name": first_scene_region["state_name"], "basis": "first_scene_region"}
        return None
