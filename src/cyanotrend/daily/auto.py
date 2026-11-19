"""Automatic daily merge (`run --auto-daily`) and the `progress` report.

The unit of work is one solar date of one state: (country, state_id, date D).

A date D is READY to merge when every scene that can feed that state's lakes and starts (UTC)
from D-1 to D+1 has a final status: done, skipped_no_lakes, or failed (after its three attempts).
Those are the same three days `daily` looks at, because a lake's solar date can differ from the
UTC date of its pass by one day. A failed scene does not hold the merge back: the lakes it
touches come out "partial" and are flagged. A scene still queued, running, retrying or publishing
holds it back, and `progress` says how many.

"Scenes that can feed the state's lakes" are the scenes of that state's plans plus any scene
(of any plan) recorded as touching the state. The ledger knows nothing about satellite passes that
no plan lists. The merge itself checks the catalogue for them and lists them as "not planned" in
its result; this module prints them and the `plan` command that would add them, but never queues
scenes itself.

Each merge is the ordinary `daily --start D --end D --country C --state-id S` command run as a
separate process, one at a time. It records itself in the ledger table `daily_runs` (manual
`daily` runs do the same). The "signature" stored with a finished merge is a fingerprint of the
final statuses of the scenes it looked at; when a later change alters it (retry-failed, another
plan adding scenes) the date is merged again.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import subprocess
import sys
import time

from ..common import file_lock, utcnow

FINAL = {"done": "done", "skipped_no_lakes": "skipped", "failed": "failed"}
RESCAN_SECONDS = 60


# ---------------------------------------------------------------------------
# Readiness: pure functions of scene statuses
# ---------------------------------------------------------------------------

def utc_day(start):
    stamp = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date()


def outcome(status):
    """done, skipped or failed once a scene is final; "open" while it still queued/running/retrying."""
    return FINAL.get(status, "open")


def bucket(scenes):
    """[(scene id, status, UTC start day)] -> {day: [(scene id, status)]}"""
    by_day = {}
    for sid, status, day in scenes:
        by_day.setdefault(day, []).append((sid, status))
    return by_day


def items_for(by_day, day):
    """[(scene id, outcome)] for scenes starting (UTC) from day-1 to day+1, in a fixed order."""
    return sorted((sid, outcome(status)) for offset in (-1, 0, 1)
                  for sid, status in by_day.get(day + timedelta(days=offset), ()))


def signature(items):
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def classify(items, latest=None):
    """State of one date. latest: the newest `daily_runs` row of that date, if any.

    no scenes | waiting (scenes still to finish) | ready (never merged) | changed (merged before,
    scene statuses differ now) | merging | merged | failed (the last merge with these same scene
    statuses failed) | nothing to merge (every scene failed or had no lakes)
    """
    sig = signature(items)
    done = sum(1 for _, o in items if o == "done")
    open_ = sum(1 for _, o in items if o == "open")
    same = bool(latest) and latest["signature"] == sig
    if latest and latest["state"] == "merging":
        state = "merging"
    elif not items:
        state = "no scenes"
    elif open_:
        state = "waiting"
    elif same and latest["state"] == "merged":
        state = "merged"
    elif same and latest["state"] in ("failed", "interrupted"):
        state = "failed"
    elif not done:
        state = "nothing to merge"
    elif latest and latest["state"] == "merged":
        state = "changed"
    else:
        state = "ready"
    return {"state": state, "signature": sig, "scenes": len(items), "open": open_, "done": done, "last": latest}


def plan_days(plans):
    days = set()
    for plan in plans:
        try:
            day, last = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
        except (TypeError, ValueError):
            continue
        while day <= last:
            days.add(day)
            day += timedelta(days=1)
    return sorted(days)


def date_states(ledger, country, state_id, plans, cache, alive=True):
    """{date: classify(...)} for every date inside the plans of one state. cache: {scene id: UTC day}.
    alive=False: a merge row still 'merging' belongs to a process that is gone (shown as interrupted)."""
    status = ledger.state_scenes(country, state_id)
    unknown = [sid for sid in status if sid not in cache]
    for sid, start in ledger.scene_starts(unknown).items():
        cache[sid] = utc_day(start) if start else None
    by_day = bucket((sid, st, cache[sid]) for sid, st in status.items() if cache.get(sid))
    latest = ledger.daily_latest(country, state_id)
    if not alive:
        latest = {d: ({**row, "state": "interrupted"} if row["state"] == "merging" else row) for d, row in latest.items()}
    return {day: classify(items_for(by_day, day), latest.get(day.isoformat())) for day in plan_days(plans)}


# ---------------------------------------------------------------------------
# What a finished merge tells the ledger
# ---------------------------------------------------------------------------

def counts_of(result):
    """Counts of one merge_day result, in the names the ledger uses."""
    return {"complete": result.get("merged_complete"), "partial": result.get("merged_partial"),
            "waiting": result.get("waiting"), "unavailable": result.get("unavailable"),
            "missing": result.get("missing_scenes"), "not_planned": result.get("not_planned_scenes")}


def details_of(result):
    details = {}
    if result.get("_missing"):
        details["missing"] = result["_missing"]
    if result.get("result"):
        details["note"] = result["result"]
    if result.get("folders"):
        details["folders"] = result["folders"]
    return details


def iso_day(text):
    found = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", str(text or ""))
    return f"{found[1]}-{found[2]}-{found[3]}" if found else None


def plan_hint(workspace, country, state_id, missing):
    """The `plan` command that would add the scenes the ledger has never heard of."""
    days = sorted({d for d in (iso_day(m.get("sensing_start")) for m in missing
                               if m.get("ledger_status") == "not planned") if d})
    if not days:
        return None
    return (f'cyanotrend --output "{workspace.output}" plan --start {days[0]} --end {days[-1]} '
            f"--country {country} --state-id {state_id}")


def _missing(row):
    try:
        return json.loads(row.get("details") or "{}").get("missing", [])
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# The coordinator part: one merge at a time, started from `run`
# ---------------------------------------------------------------------------

def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))[:60]


class AutoDaily:
    """Starts a `daily` process for each ready date, one at a time. Driven by serve()'s loop."""

    def __init__(self, workspace, ledger, say=None):
        self.ws, self.ledger = workspace, ledger
        self.say = say or (lambda text: print(text, flush=True))
        self.proc = self.handle = self.run_id = None
        self.cache, self.tried, self.todo = {}, set(), []
        self.marker, self.scanned, self.dirty = None, 0.0, True

    def start(self):
        """A merge row still 'merging' when `run` starts belongs to a merge that died with the previous run.
        If a manual `daily` is merging right now, its row is genuine and is left alone."""
        try:
            with file_lock(self.ws.output / "daily" / ".daily.lock", blocking=False):
                self.ledger.daily_close_interrupted()
        except RuntimeError:
            pass

    def running(self):
        return self.proc is not None

    def tick(self, allow_new=True):
        """Collect a finished merge; start the next ready one unless told not to (stopping)."""
        self._reap()
        if self.proc is None and allow_new:
            job = self._next()
            if job:
                self._launch(job)

    def has_work(self):
        return self.proc is not None or self._next() is not None

    def shutdown(self):
        """Unexpected stop of `run`: end the merge; its publication journal is recovered by the next merge."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._reap()

    # -- internals ---------------------------------------------------------

    def _scan(self):
        jobs = []
        for (country, state_id), info in sorted(self.ledger.plan_regions().items()):
            states = date_states(self.ledger, country, state_id, info["plans"], self.cache)
            for day, found in sorted(states.items()):
                # A failed merge is tried once more by every new `run`, but never twice in one (see _launch).
                if found["state"] in ("ready", "changed", "failed"):
                    jobs.append((country, state_id, day.isoformat(), found["signature"]))
        return jobs

    def _next(self):
        marker = self.ledger.change_marker()
        now = time.monotonic()
        if self.dirty or marker != self.marker or now - self.scanned > RESCAN_SECONDS:
            self.todo, self.marker, self.scanned, self.dirty = self._scan(), marker, now, False
        return next((job for job in self.todo if job not in self.tried), None)

    def _launch(self, job):
        country, state_id, day, sig = job
        self.tried.add(job)  # a failed or odd merge is not repeated in this run unless scene statuses change
        stamp = time.strftime("%Y%m%dT%H%M%S")
        log_path = self.ws.output / "logs" / f"daily_{_safe(country)}_{_safe(state_id)}_{day}_{stamp}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run_id = self.ledger.daily_start(country, state_id, day, "auto", sig, str(log_path))
        handle = log_path.open("w")
        command = [sys.executable, "-m", "cyanotrend", "--output", str(self.ws.output),
                   "--references", str(self.ws.references), "--scratch", str(self.ws.scratch),
                   "--ocssw", str(self.ws.ocssw), "daily", "--start", day, "--end", day,
                   "--country", country, "--state-id", state_id, "--run-id", str(run_id)]
        try:
            # Its own session: Ctrl-C in the terminal reaches `run`, not the merge, which then finishes.
            proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            handle.close()
            self.ledger.daily_finish(run_id, "failed", error=f"could not start the merge: {exc}")
            self.say(f"{utcnow()} merge of {country}/{state_id} {day} could not start: {exc}")
            return
        self.proc, self.handle, self.run_id = proc, handle, run_id
        self.say(f"{utcnow()} merging {country}/{state_id} {day} (log: {log_path.name})")

    def _reap(self):
        if self.proc is None or self.proc.poll() is None:
            return
        code = self.proc.returncode
        self.handle.close()
        run_id, self.proc, self.handle, self.run_id, self.dirty = self.run_id, None, None, None, True
        row = self.ledger.daily_row(run_id)
        if row["state"] == "merging":  # the process ended without closing its own row
            tail = ""
            try:
                tail = open(row["log"]).read()[-400:].strip()
            except (OSError, TypeError):
                pass
            self.ledger.daily_finish(run_id, "failed", error=f"the merge process ended with exit code {code}"
                                     + (f": {tail}" if tail else ""))
            row = self.ledger.daily_row(run_id)
        self.say(self._summary(row))

    def _summary(self, row):
        where = f"{row['country']}/{row['state_id']} {row['date_solar']}"
        if row["state"] != "merged":
            return (f"{utcnow()} merge of {where} FAILED: {str(row['error'])[:300]}. "
                    f"Details: {row['log']}. It is not retried in this run.")
        text = (f"{utcnow()} merged {where}: {row['complete_lakes'] or 0} complete, "
                f"{row['partial_lakes'] or 0} partial, {row['waiting_lakes'] or 0} lake(s) waiting, "
                f"{row['unavailable_lakes'] or 0} unavailable")
        missing = _missing(row)
        if row["not_planned_scenes"]:
            hint = plan_hint(self.ws, row["country"], row["state_id"], missing)
            text += (f"\n  {row['not_planned_scenes']} scene(s) this date needs are in no plan, so lakes wait for them."
                     + (f" To add them: {hint}" if hint else ""))
        elif row["waiting_lakes"]:
            text += "\n  The waiting lakes need planned scenes that are not finished; this date is merged again when they are."
        return text


# ---------------------------------------------------------------------------
# `progress`
# ---------------------------------------------------------------------------

SHOW = 20


def _lock_held(path):
    if not path.exists():
        return False
    try:
        with file_lock(path, blocking=False):
            return False
    except RuntimeError:
        return True


def progress(workspace, ledger):
    """Per state: how many dates are merged, which wait and for what. Read-only."""
    from ..queue_control import service_running

    merging_now = _lock_held(workspace.output / "daily" / ".daily.lock")
    running = service_running(workspace)
    alive = merging_now or running  # a 'merging' row is genuine only while a merge or `run` (which starts them) lives
    cache, states, waiting, merged_waiting, ready, failed, hints = {}, [], [], [], [], [], []
    for (country, state_id), info in sorted(ledger.plan_regions().items()):
        found = date_states(ledger, country, state_id, info["plans"], cache, alive)
        label = f"{info['name'] or state_id} ({country})"
        tally = {}
        for day, item in sorted(found.items()):
            tally[item["state"]] = tally.get(item["state"], 0) + 1
            last = item["last"]
            if item["state"] == "waiting":
                waiting.append({"state": label, "date": day.isoformat(), "scenes_not_finished": item["open"],
                                "of_scenes": item["scenes"],
                                "merged_before": "yes" if last and last["state"] == "merged" else ""})
            elif item["state"] in ("ready", "changed"):
                ready.append({"state": label, "date": day.isoformat(), "why": "never merged" if item["state"] == "ready"
                              else "scene statuses changed since the last merge"})
            elif item["state"] == "failed":
                failed.append({"state": label, "date": day.isoformat(), "error": str(last["error"] or "")[:200],
                               "log": last["log"] or ""})
            if item["state"] == "merged" and (last["waiting_lakes"] or 0) > 0:
                to_come = (last["missing_scenes"] or 0) - (last["not_planned_scenes"] or 0)
                merged_waiting.append({"state": label, "date": day.isoformat(), "lakes_waiting": last["waiting_lakes"],
                                       "scenes_in_a_plan_not_finished": max(to_come, 0),
                                       "scenes_in_no_plan": last["not_planned_scenes"] or 0})
                hint = plan_hint(workspace, country, state_id, _missing(last))
                if hint and hint not in hints:
                    hints.append(hint)
        states.append({
            "state": label, "dates_in_plans": sum(tally.values()),
            "merged": tally.get("merged", 0), "ready_to_merge": tally.get("ready", 0) + tally.get("changed", 0),
            "waiting_for_scenes": tally.get("waiting", 0), "merging_now": tally.get("merging", 0),
            "merge_failed": tally.get("failed", 0), "no_scenes": tally.get("no scenes", 0),
            "nothing_to_merge": tally.get("nothing to merge", 0),
        })
    recent = []
    for row in ledger.daily_rows()[-10:][::-1]:
        shown = row["state"]
        if shown == "merging" and not alive:
            shown = "interrupted"
        recent.append({"date": row["date_solar"], "state": f"{row['state_id'] or 'all'}", "result": shown,
                       "by": row["source"], "complete": row["complete_lakes"], "partial": row["partial_lakes"],
                       "waiting": row["waiting_lakes"], "finished": str(row["finished"] or "")[:19]})
    attention = []
    if ready:
        attention.append(f"{len(ready)} date(s) are ready to merge. `run --auto-daily` merges them by itself; "
                         "by hand: cyanotrend daily --start D --end D --country C --state-id S")
    if failed:
        attention.append(f"{len(failed)} merge(s) failed; the log of each is listed below")
    if merged_waiting:
        attention.append(f"{len(merged_waiting)} merged date(s) still have waiting lakes (see below)")
    if waiting and not running:
        attention.append("Dates wait for scenes but `run` is not active. Start it: cyanotrend run")
    report = {
        "scope": "Per state and solar date, from the saved plans and the scene ledger; "
                 "a date can only merge once all its scenes (UTC day before, same day, day after) are final",
        "run_active": "yes" if running else "no",
        "merge_running": "yes" if merging_now else "no",
        "states": states,
        "attention": attention or ["nothing needs attention"],
    }
    for key, rows in (("ready_to_merge", ready), ("merge_failed", failed), ("merged_but_lakes_waiting", merged_waiting),
                      ("waiting_for_scenes", waiting)):
        if rows:
            report[key] = rows[:SHOW] + ([{"state": f"... and {len(rows) - SHOW} more"}] if len(rows) > SHOW else [])
    if hints:
        report["add_missing_scenes_with"] = hints
    if recent:
        report["recent_merges"] = recent
    return report
