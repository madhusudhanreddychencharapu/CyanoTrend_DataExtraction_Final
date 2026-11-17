"""Thin CLI: application functions remain usable by a future frontend."""

from __future__ import annotations
import argparse
import getpass
import json
import os
import sys
from .common import Workspace
from .ledger import Ledger


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero")
    return n


def catalogue_cap(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("Must be zero (all) or positive")
    return n


def parser():
    p = argparse.ArgumentParser(
        prog="cyanotrend",
        description="Notebook adaptive-window Sentinel-3 lake processing; five parallel scenes by default.",
    )
    p.add_argument(
        "--output",
        default="Output Data",
        help="Final ZIPs, master CSVs, plans and operational ledger",
    )
    p.add_argument(
        "--references",
        default="reference_data",
        help="One reusable copy of HydroLAKES and boundaries",
    )
    p.add_argument(
        "--scratch",
        default=".scratch",
        help="Temporary scene data; successful scratch removed, failed scratch retained as in notebook",
    )
    p.add_argument(
        "--ocssw",
        default="~/cyanoTrend/software/ocssw",
        help="Existing OCSSW installation; never installed automatically",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable report for scripts/frontends",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "prepare",
        help="Download global HydroLAKES and ADM0/ADM1 once; resume interrupted setup",
    )
    sub.add_parser(
        "preflight",
        help="Check dependencies, OCSSW, credentials, reference checksums and free storage",
    )
    r = sub.add_parser("regions", help="List country or state boundary names and IDs")
    r.add_argument("--country", help="ISO3 country, e.g. USA; omit to list countries")
    q = sub.add_parser(
        "plan", help="Save a dated discovery plan and queue its distinct scenes"
    )
    q.add_argument("--start", required=True, help="First date, YYYY-MM-DD (inclusive)")
    q.add_argument("--end", required=True, help="Last date, YYYY-MM-DD (inclusive)")
    q.add_argument("--country", required=True, help="ISO3 country for the selected state")
    q.add_argument("--state-id", required=True, help="Exact state/province shapeID from regions")
    q.add_argument("--lake-region", choices=["WORLD", "USA"], default="WORLD", help="Loaded HydroLAKES universe, independent of planning state (notebook default: WORLD)")
    q.add_argument(
        "--max-scenes",
        type=catalogue_cap,
        help="Notebook catalogue cap before lake filtering; zero or omitted means all",
    )
    q.add_argument(
        "--scene-rule",
        choices=["lake-complete", "state"],
        default="lake-complete",
        help="lake-complete (default): every scene touching any lake of the region, so each "
        "lake's daily checklist is planned together; state: notebook rule, scene must also "
        "intersect the region itself",
    )
    q = sub.add_parser(
        "run", help="Run queue; wait for future saved plans until stopped"
    )
    q.add_argument(
        "--workers", type=positive, default=5, help="Concurrent scenes (default: 5)"
    )
    q.add_argument(
        "--once",
        action="store_true",
        help="Exit when queued work and scheduled retries finish",
    )
    q = sub.add_parser(
        "daily",
        help="Merge processed S3A/S3B scenes per local solar date and lake on ISIN bins (69120 rows, about 290 m)",
    )
    q.add_argument("--start", required=True, help="First solar date, YYYY-MM-DD (inclusive)")
    q.add_argument("--end", required=True, help="Last solar date, YYYY-MM-DD (inclusive)")
    q.add_argument(
        "--method",
        choices=["area"],
        default="area",
        help="Area-weighted binning of pixels into ISIN bins within each scene; the daily value is the maximum over scenes, independently per index",
    )
    q.add_argument(
        "--country",
        help="ISO3 country of --state-id. With --state-id, merge only the lakes filed under that state "
        "(much faster than all lakes); other states' results are left untouched",
    )
    q.add_argument("--state-id", help="Exact state/province shapeID from `regions`; needs --country")
    q = sub.add_parser(
        "lakes",
        help="Find the lakes of a state that have a merged daily row and a NASA CyAN value on a date, biggest first",
    )
    q.add_argument("--country", required=True, help="ISO3 country, e.g. USA")
    q.add_argument("--state-id", required=True, help="Exact state/province shapeID from `regions`")
    q.add_argument("--date", required=True, help="Solar date of the daily result, YYYY-MM-DD")
    q.add_argument("--name", help="Only lakes whose name contains this text (case-insensitive)")
    q.add_argument("--limit", type=positive, default=30, help="How many lakes to list (default: 30)")
    q = sub.add_parser(
        "scatter",
        help="Scatter plot, NASA CyAN CI_cyano (x) vs our CI_cyano (y), one NASA cell per dot, for lakes of a state on a date",
    )
    q.add_argument("--country", required=True, help="ISO3 country, e.g. USA")
    q.add_argument("--state-id", required=True, help="Exact state/province shapeID from `regions`")
    q.add_argument("--date", required=True, help="Solar date of the daily result, YYYY-MM-DD")
    q.add_argument("--lake", action="append", required=True,
                   help="Lake name or Hylak_id (see `lakes`); repeat the option for several lakes")
    q.add_argument("--out-dir", help="Folder for the PNG and the CSV of the pairs (default: <output>/compare/...)")
    sub.add_parser("status", help="Queue totals, whether `run` is active, stages of running scenes and what needs attention")
    sub.add_parser("plans", help="List saved plans: dates, region, scene counts by status, and whether the plan file exists")
    q = sub.add_parser(
        "cancel-plan",
        help="Withdraw a plan: remove its queued/retrying/failed scenes and move its plan file aside",
    )
    q.add_argument("plan_id", help="Plan ID or a unique start of it (see `plans`)")
    q.add_argument("--dry-run", action="store_true", help="Show what would be removed; change nothing")
    q = sub.add_parser(
        "cancel-queued",
        help="Remove scenes that have not started (queued or waiting to retry) from the queue",
    )
    q.add_argument("--plan", help="Only scenes of this plan (ID or unique start); scenes another plan lists are kept")
    q.add_argument("--start", help="Only scenes starting on or after this UTC date, YYYY-MM-DD")
    q.add_argument("--end", help="Only scenes starting on or before this UTC date, YYYY-MM-DD")
    q.add_argument("--include-failed", action="store_true", help="Also remove failed scenes")
    q.add_argument("--dry-run", action="store_true", help="Show what would be removed; change nothing")
    sub.add_parser("retry-failed", help="Requeue all failed scenes with three additional attempts; preserve logs and attempt history")
    q = sub.add_parser("scenes", help="List scene progress and staging transfer rates")
    q.add_argument(
        "--status",
        choices=["queued", "running", "retry", "publishing", "done", "failed", "skipped_no_lakes"],
    )
    q = sub.add_parser(
        "scene", help="Inspect one scene, attempts, errors, timings and archive"
    )
    q.add_argument("scene_id")
    sub.add_parser(
        "coverage",
        help="Discovered/processed scenes by country and state (regions overlap)",
    )
    q = sub.add_parser(
        "verify", help="Verify completed archive checksums and master CSV row identity"
    )
    q.add_argument("--scene-id")
    return p


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, default=str, allow_nan=False))
        return
    if isinstance(value, list):
        if not value:
            print("No records.")
            return
        keys = list(dict.fromkeys(k for row in value for k in row))
        rows = [[str(row.get(k, "")) for k in keys] for row in value]
        widths = [
            max(len(k), *(len(row[i]) for row in rows)) for i, k in enumerate(keys)
        ]
        print("  ".join(k.ljust(w) for k, w in zip(keys, widths)))
        for row in rows:
            print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(k + ":")
                emit(v)
            elif isinstance(v, dict):
                print(k + ":")
                emit(v)
            elif isinstance(v, list) and v:
                print(k + ":")
                for item in v:
                    print(f"  - {item}")
            else:
                print(f"{k}: {v}")
    else:
        print(value)


def scene_summary(row):
    return {
        "scene_id": row["id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "stage": row["stage"],
        "downloaded_MiB": round(row["bytes_downloaded"] / 1024**2, 2),
        "staging_MiB_per_second": (
            round(row["bytes_downloaded"] / 1024**2 / row["download_seconds"], 3)
            if row["download_seconds"]
            else None
        ),
        "error": row["error"] or "",
    }


def verify(workspace, scene_id=None):
    import csv
    from .common import digest
    from .publication import validate_archive

    ledger = Ledger(workspace.db)
    errors = []
    counts = {}
    keys = set()
    master = workspace.output / "master.csv"
    if not master.exists():
        errors.append("master.csv is missing")
    else:
        from .schema import COLUMNS

        with master.open(newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != COLUMNS:
                errors.append("Master schema mismatch")
            for row in reader:
                key = (row["scene_id"], row["Hylak_id"])
                if key in keys:
                    errors.append(f"Duplicate master key: {key}")
                keys.add(key)
                counts[row["scene_id"]] = counts.get(row["scene_id"], 0) + 1
    checked = 0
    for row in ledger.rows("done"):
        if scene_id and row["id"] != scene_id:
            continue
        if row["stage"] == "complete_no_native":
            if counts.get(row["id"], 0):
                errors.append(f"Empty completed scene has master rows: {row['id']}")
            else:
                checked += 1
            continue
        from pathlib import Path

        p = Path(row["archive"])
        try:
            if not p.is_file() or digest(p) != row["archive_sha256"]:
                raise ValueError("archive checksum mismatch/missing")
            meta, table = validate_archive(p, row["id"], row["reference_id"])
            if len(table) != counts.get(row["id"], 0):
                raise ValueError("Master row count mismatch")
            checked += 1
        except (ValueError, OSError) as e:
            errors.append(f"{row['id']}: {e}")
    if scene_id and checked == 0 and not errors:
        errors.append("No completed scene matched the requested ID")
    return {"ok": not errors, "checked_archives": checked, "errors": errors}


NEEDS_WORKSPACE = ("daily", "verify", "retry-failed", "status", "plans", "cancel-plan", "cancel-queued",
                   "scenes", "scene", "coverage", "lakes", "scatter")


def dispatch(a):
    ws = Workspace.create(a.output, a.references, a.scratch, a.ocssw)
    if a.command in NEEDS_WORKSPACE:
        from .queue_control import require_workspace

        require_workspace(ws)
    if a.command == "prepare":
        from .references import prepare

        m = prepare(ws)
        return {
            "reference_id": m["reference_id"],
            "boundary_files": len(m["boundaries"]),
            "coverage": m["coverage"],
        }
    if a.command == "preflight":
        from .preflight import check

        return check(ws)
    if a.command == "regions":
        from .references import boundaries

        g = boundaries(ws, "ADM1" if a.country else "ADM0")
        if a.country:
            g = g[g.country_iso3 == a.country.upper()]
        return (
            g.drop(columns="geometry")
            .rename(columns={"shapeName": "name", "shapeID": "id"})
            .to_dict("records")
        )
    if a.command == "plan":
        from .planning import create_plan

        return create_plan(ws, a.start, a.end, a.country, a.state_id, a.max_scenes, a.lake_region, a.scene_rule)
    if a.command == "daily":
        from .daily.merge import run_daily

        return run_daily(ws, a.start, a.end, method=a.method, country=a.country, state_id=a.state_id)
    if a.command == "lakes":
        from .daily.compare import find_lakes

        return find_lakes(ws, a.country, a.state_id, a.date, name=a.name, limit=a.limit)
    if a.command == "scatter":
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            raise RuntimeError("The scatter plot needs matplotlib: pip install matplotlib") from None
        from .daily.compare import scatter

        return scatter(ws, a.country, a.state_id, a.date, a.lake, out_dir=a.out_dir)
    if a.command == "run":
        from .service import serve

        if sys.stdin.isatty():
            if not os.environ.get("CDSE_USERNAME"):
                os.environ["CDSE_USERNAME"] = input("CDSE username: ").strip()
            if not os.environ.get("CDSE_PASSWORD"):
                os.environ["CDSE_PASSWORD"] = getpass.getpass(
                    "CDSE password (hidden): "
                )
        return serve(ws, a.workers, a.once)
    if a.command == "verify":
        return verify(ws, a.scene_id)
    ledger = Ledger(ws.db)
    if a.command == "retry-failed":
        return ledger.requeue_failed()
    if a.command == "status":
        from .queue_control import status_report

        return status_report(ws, ledger)
    if a.command == "plans":
        from .queue_control import list_plans

        return list_plans(ws, ledger)
    if a.command == "cancel-plan":
        from .queue_control import cancel_plan

        return cancel_plan(ws, ledger, a.plan_id, dry_run=a.dry_run)
    if a.command == "cancel-queued":
        from .queue_control import cancel_queued

        return cancel_queued(ws, ledger, plan=a.plan, start=a.start, end=a.end,
                             include_failed=a.include_failed, dry_run=a.dry_run)
    if a.command == "scenes":
        return [scene_summary(r) for r in ledger.light_rows(a.status)]
    if a.command == "scene":
        result = ledger.scene(a.scene_id)
        result.pop("scene_json", None)
        return result
    if a.command == "coverage":
        return ledger.regions()
    raise ValueError("Unknown command")


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    try:
        if a.json and a.command in ("prepare", "plan", "run", "daily"):
            from contextlib import redirect_stdout

            with redirect_stdout(sys.stderr):
                result = dispatch(a)
        else:
            result = dispatch(a)
        emit(result, a.json)
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (ValueError, RuntimeError, OSError, KeyError) as e:
        from .telemetry import redact

        emit({"ok": False, "error": redact(str(e))}, a.json)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
