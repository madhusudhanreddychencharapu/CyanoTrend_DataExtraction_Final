"""Read a frozen plan without changing its scene queue or legacy publications."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import uuid

from ..ledger import Ledger


def utc_day(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Daily scene timestamps must specify a timezone")
    return stamp.astimezone(timezone.utc).date()


def select_day(plan, day, records):
    """Every selected input must be terminal-successful, even if it has no lakes."""
    day = date.fromisoformat(str(day))
    selection = plan["selection"]
    if not selection.get("country") or not selection.get("state_id"):
        raise ValueError("Daily output requires a plan with a selected country and state")
    if selection.get("max_scenes"):
        raise ValueError("Daily output requires an uncapped plan (--max-scenes omitted or 0)")
    scenes = {}
    for scene in plan["scenes"]:
        if utc_day(scene["start"]) != day:
            continue
        uuid.UUID(scene["id"])
        if scene.get("satellite") not in ("S3A", "S3B"):
            raise ValueError("Daily inputs must identify S3A or S3B")
        if scene["id"] in scenes and scenes[scene["id"]] != scene:
            raise ValueError("Conflicting duplicate scene identity")
        scenes[scene["id"]] = scene
    if not scenes:
        raise ValueError(f"No scenes in the plan for {day}")
    incomplete = [sid for sid in scenes if records.get(sid, {}).get("status")
                  not in ("done", "skipped_no_lakes")]
    if incomplete:
        raise ValueError("Daily ZIP withheld: incomplete scenes: " + ", ".join(incomplete))
    return sorted(scenes.values(), key=lambda s: (s["start"], s["id"]))


def read_inputs(workspace, plan_path, day):
    path = Path(plan_path).expanduser().resolve()
    plan = json.loads(path.read_text())
    if plan.get("schema") != "cyanotrend-plan-1":
        raise ValueError("Unsupported scene plan")
    records = {r["id"]: r for r in Ledger(workspace.db).rows()}
    return plan, select_day(plan, day, records)


def lake_scene_membership(lakes, scenes):
    """Per-lake footprint provenance, including cloudy and partial coverage."""
    from ..core.catalogue import scene_geometry, candidate_lakes_for_scene
    membership = {}
    for scene in scenes:
        if scene_geometry(scene) is None:
            raise ValueError(f"Missing scene footprint: {scene['id']}")
        for lid in candidate_lakes_for_scene(scene, lakes).Hylak_id:
            membership.setdefault(int(lid), []).append(scene)
    return membership
