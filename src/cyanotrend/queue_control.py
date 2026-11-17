"""Plan and queue control: list plans, withdraw a plan or queued scenes, and report the real state.

A plan is a JSON file in <output>/plans plus rows in the ledger. `run` imports every plan file the
ledger does not know, so withdrawing a plan must also move its file out of plans/ (to plans/cancelled/).
Only scenes that have not started (queued), wait to retry, or failed can be removed. Running,
publishing and finished scenes are never touched.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import time

from .common import file_lock, utcnow

NOT_STARTED = ("queued", "retry")
UNFINISHED = ("queued", "retry", "failed")
STATUS_ORDER = ("queued", "retry", "running", "publishing", "done", "failed", "skipped_no_lakes")
SAMPLE = 20


def require_workspace(workspace):
    """Stop a mistyped --output from silently creating a new, empty workspace."""
    if not workspace.db.is_file():
        raise ValueError(
            f"No cyanotrend workspace at {workspace.output} (operations.sqlite3 not found). "
            "Check --output; it goes before the command."
        )


def service_running(workspace):
    """True while a `cyanotrend run` holds the service lock."""
    try:
        with file_lock(workspace.output / ".service.lock", blocking=False):
            return False
    except RuntimeError:
        return True


def _plan_files(workspace):
    folder = workspace.output / "plans"
    return {p.stem: p for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}


def _selection_of_file(path):
    try:
        return json.loads(Path(path).read_text()).get("selection", {}) or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _plan_row(plan_id, created, selection, counts, plan_file):
    return {
        "plan_id": plan_id,
        "created": str(created)[:19],
        "start": selection.get("start", ""),
        "end": selection.get("end", ""),
        "country": selection.get("country", ""),
        "state": selection.get("state_name", ""),
        "scenes": sum(counts.values()),
        "queued": counts.get("queued", 0),
        "retry": counts.get("retry", 0),
        "running": counts.get("running", 0) + counts.get("publishing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped_no_lakes", 0),
        "plan_file": plan_file,
    }


def list_plans(workspace, ledger):
    """Registered plans plus plan files in plans/ that the ledger does not know."""
    files = _plan_files(workspace)
    rows, seen = [], set()
    for plan in ledger.plan_rows():
        seen.add(plan["id"])
        present = plan["id"] in files or Path(plan["path"]).is_file()
        rows.append(_plan_row(plan["id"], plan["created"], json.loads(plan["metadata"] or "{}"),
                              plan["counts"], "ok" if present else "MISSING"))
    for plan_id, path in files.items():
        if plan_id not in seen:
            rows.append(_plan_row(plan_id, "", _selection_of_file(path), {}, "not registered"))
    return rows


def resolve_plan(workspace, ledger, reference):
    ref = str(reference or "").strip().lower()
    if not ref:
        raise ValueError("A plan ID is required; list them with `cyanotrend plans`")
    known = {p["id"] for p in ledger.plan_rows()} | set(_plan_files(workspace))
    matches = [ref] if ref in known else sorted(p for p in known if p.startswith(ref))
    if not matches:
        raise ValueError(f"Unknown plan {reference!r}; list them with `cyanotrend plans`")
    if len(matches) > 1:
        raise ValueError(f"Plan {reference!r} is ambiguous: " + ", ".join(matches))
    return matches[0]


def _move_plan_file(workspace, plan_id):
    """Keep the plan file as a record, but out of plans/ so `run` never imports it again."""
    source = workspace.output / "plans" / f"{plan_id}.json"
    if not source.is_file():
        return "plan file already missing"
    folder = workspace.output / "plans" / "cancelled"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.exists():
        target = folder / f"{plan_id}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    shutil.move(str(source), str(target))
    return f"moved to {target.relative_to(workspace.output)}"


def _sample(ids):
    ids = list(ids)
    return ids[:SAMPLE] + ([f"... and {len(ids) - SAMPLE} more"] if len(ids) > SAMPLE else [])


def cancel_plan(workspace, ledger, reference, dry_run=False):
    """Withdraw a plan: delete its queued, retrying and failed scenes that no other plan needs."""
    with file_lock(workspace.output / ".plan.lock"):
        plan_id = resolve_plan(workspace, ledger, reference)
        registered = plan_id in {p["id"] for p in ledger.plan_rows()}
        if registered:
            result = ledger.withdraw(UNFINISHED, plan_id=plan_id, drop_plan=True, dry_run=dry_run)
        else:  # a plan file the ledger never accepted
            result = {"removed": [], "removed_by_status": {}, "unlinked_shared": [], "left_in_plan": {},
                      "plan_removed": True}
        if result["plan_removed"]:
            plan_file = "would be moved to plans/cancelled/" if dry_run else _move_plan_file(workspace, plan_id)
        else:
            plan_file = "kept (the plan still has scenes that cannot be removed)"
    notes = []
    if dry_run:
        notes.append("Dry run: nothing was changed.")
    left = result["left_in_plan"]
    if left.get("running") or left.get("publishing"):
        notes.append("Some scenes are running or publishing. They cannot be cancelled here: stop `run` "
                     "(Ctrl+C) and let them finish or fail, then run cancel-plan again.")
    if left.get("done"):
        notes.append("Finished scenes stay, with their results; the plan stays as their record.")
    if result["unlinked_shared"]:
        notes.append("Scenes another plan also lists were kept and only unlinked from this plan.")
    return {
        "plan_id": plan_id,
        "dry_run": dry_run,
        "removed_scenes": len(result["removed"]),
        "removed_by_status": result["removed_by_status"],
        "kept_because_another_plan_lists_them": len(result["unlinked_shared"]),
        "left_in_plan": left,
        "plan_removed": result["plan_removed"],
        "plan_file": plan_file,
        "scene_ids": _sample(result["removed"]),
        "notes": notes,
    }


def cancel_queued(workspace, ledger, plan=None, start=None, end=None, include_failed=False, dry_run=False):
    """Delete scenes that have not started (and optionally failed ones), by plan and/or date."""
    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    if first and last and first > last:
        raise ValueError("--start must not be after --end")
    with file_lock(workspace.output / ".plan.lock"):
        plan_id = resolve_plan(workspace, ledger, plan) if plan else None
        result = ledger.withdraw(UNFINISHED if include_failed else NOT_STARTED, plan_id=plan_id,
                                 start=first, end=last, dry_run=dry_run)
        empty = [] if dry_run else [p["id"] for p in ledger.plan_rows() if not p["counts"]]
    notes = []
    if dry_run:
        notes.append("Dry run: nothing was changed.")
    if empty:
        notes.append("Plans left with no scenes: " + ", ".join(empty)
                     + ". Remove each with `cyanotrend cancel-plan <id>` (this also moves its plan file aside).")
    return {
        "dry_run": dry_run,
        "scope": ("plan " + plan_id if plan_id else "all plans")
                 + (f", scenes starting {start or '...'} to {end or '...'} (UTC)" if first or last else ""),
        "statuses_removed": list(UNFINISHED if include_failed else NOT_STARTED),
        "removed_scenes": len(result["removed"]),
        "removed_by_status": result["removed_by_status"],
        "kept_because_another_plan_lists_them": len(result["unlinked_shared"]),
        "scene_ids": _sample(result["removed"]),
        "notes": notes,
    }


def _minutes_since(stamp, now):
    try:
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round((now - then).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def status_report(workspace, ledger):
    """Queue state from the ledger, plus whether `run` is really active and what needs attention."""
    rows = ledger.light_rows()
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    ordered = {k: counts[k] for k in STATUS_ORDER if k in counts}
    ordered.update({k: v for k, v in counts.items() if k not in ordered})
    active_run = service_running(workspace)
    now = datetime.now(timezone.utc)
    epoch = time.time()
    active = []
    for row in rows:
        if row["status"] not in ("running", "publishing"):
            continue
        mib = (row["bytes_downloaded"] or 0) / 1024**2
        active.append({
            "scene_id": row["id"],
            "status": row["status"],
            "stage": row["stage"],
            "attempt": row["attempts"],
            "downloaded_MiB": round(mib, 2),
            "minutes_since_update": _minutes_since(row["updated"], now),
            "state": "active" if active_run else "ABANDONED (no run is active)",
        })
    retry_waits = [max(0.0, (row["next_attempt"] or 0) - epoch) for row in rows if row["status"] == "retry"]
    plans = list_plans(workspace, ledger)
    missing = [p["plan_id"] for p in plans if p["plan_file"] == "MISSING"]
    unregistered = [p["plan_id"] for p in plans if p["plan_file"] == "not registered"]
    orphans = ledger.scenes_without_plan()
    to_start = counts.get("queued", 0) + counts.get("retry", 0)
    attention = []
    if to_start and not active_run:
        attention.append(f"{to_start} scene(s) wait to start but `run` is not active. Start it: cyanotrend run")
    if active and not active_run:
        attention.append(f"{len(active)} scene(s) show running/publishing but no `run` is active (stopped or "
                         "crashed). The next `run` recovers them.")
    if missing:
        attention.append(f"{len(missing)} plan(s) have no plan file (deleted by hand?) but their scenes are still "
                         "in the queue. See `cyanotrend plans`; remove one with `cyanotrend cancel-plan <id>`.")
    if unregistered:
        attention.append(f"{len(unregistered)} plan file(s) in plans/ are not registered; `run` will try to import "
                         "them. Inspect with `cyanotrend plans`; set one aside with `cyanotrend cancel-plan <id>`.")
    if orphans:
        attention.append(f"{sum(orphans.values())} scene(s) belong to no plan ({', '.join(f'{k}: {v}' for k, v in sorted(orphans.items()))}). "
                         "Remove queued ones with `cyanotrend cancel-queued`.")
    report = {
        "scope": "Scenes in the ledger (saved plans); not an estimate of the entire satellite archive",
        "run_active": "yes" if active_run else "no",
        "scenes_total": len(rows),
        "counts": ordered,
        "waiting_to_start": to_start,
        "plans": len(plans),
        "plans_without_file": len(missing),
        "unregistered_plan_files": len(unregistered),
        "scenes_without_plan": sum(orphans.values()),
        "active": active,
        "attention": attention or ["nothing needs attention"],
    }
    if retry_waits:
        report["next_retry_in_seconds"] = round(min(retry_waits))
    return report
