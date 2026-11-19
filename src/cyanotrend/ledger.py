"""SQLite is operational state; users access it through CLI/API reports.

A unique scene ID deduplicates all plans. BEGIN IMMEDIATE makes claims atomic.
Only the coordinator publishes outputs; workers may record their own timings.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import time
from pathlib import Path
from .common import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
 id TEXT PRIMARY KEY, scene_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, stage TEXT, bytes_downloaded INTEGER DEFAULT 0,
 download_seconds REAL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL,
 archive TEXT, archive_sha256 TEXT, rows INTEGER DEFAULT 0,
 reference_id TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
 retry_start_attempt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, path TEXT NOT NULL, created TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_scenes (plan_id TEXT NOT NULL, scene_id TEXT NOT NULL,
 PRIMARY KEY(plan_id,scene_id), FOREIGN KEY(plan_id) REFERENCES plans(id), FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS scene_regions (scene_id TEXT NOT NULL, country TEXT NOT NULL, state_id TEXT NOT NULL,
 state_name TEXT NOT NULL, PRIMARY KEY(scene_id,country,state_id), FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS timings (scene_id TEXT NOT NULL, attempt INTEGER NOT NULL, stage TEXT NOT NULL,
 started TEXT NOT NULL, seconds REAL NOT NULL, ok INTEGER NOT NULL, details TEXT NOT NULL,
 FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS scene_queue ON scenes(status,next_attempt,created);
CREATE INDEX IF NOT EXISTS timing_scene ON timings(scene_id);
CREATE TABLE IF NOT EXISTS daily_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, country TEXT NOT NULL DEFAULT '', state_id TEXT NOT NULL DEFAULT '',
 date_solar TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', state TEXT NOT NULL,
 started TEXT NOT NULL, finished TEXT, complete_lakes INTEGER, partial_lakes INTEGER,
 waiting_lakes INTEGER, unavailable_lakes INTEGER, missing_scenes INTEGER, not_planned_scenes INTEGER,
 signature TEXT, log TEXT, details TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS daily_run_date ON daily_runs(country,state_id,date_solar);
"""

# Everything except the (large) scene_json: enough for status, listings and the queue.
LIGHT_COLUMNS = ("id,status,attempts,stage,bytes_downloaded,download_seconds,error,created,updated,"
                 "next_attempt,retry_start_attempt")


def _chunks(values, size=400):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _scene_day(scene_json):
    stamp = datetime.fromisoformat(json.loads(scene_json)["start"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            # Keep old attempt/log identities when enabling explicit retry batches. Only an old
            # database needs the write lock, so status and listings never wait behind a busy run.
            if "retry_start_attempt" not in {r[1] for r in c.execute("PRAGMA table_info(scenes)")}:
                c.execute("BEGIN IMMEDIATE")
                if "retry_start_attempt" not in {r[1] for r in c.execute("PRAGMA table_info(scenes)")}:
                    c.execute("ALTER TABLE scenes ADD COLUMN retry_start_attempt INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def enqueue(self, plan, path):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            ref = c.execute(
                "SELECT value FROM workspace_meta WHERE key='reference_id'"
            ).fetchone()
            if ref and ref[0] != plan["reference_id"]:
                raise ValueError(
                    "Reference data changed: use a separate output workspace"
                )
            c.execute(
                "INSERT OR IGNORE INTO workspace_meta VALUES ('reference_id',?)",
                (plan["reference_id"],),
            )
            c.execute(
                "INSERT OR IGNORE INTO plans VALUES (?,?,?,?)",
                (plan["id"], str(path), plan["created"], json.dumps(plan["selection"])),
            )
            lake_region = plan.get("selection", {}).get("lake_region", "WORLD")
            existing_region = c.execute("SELECT value FROM workspace_meta WHERE key='lake_region'").fetchone()
            if existing_region and existing_region[0] != lake_region:
                raise ValueError("HydroLAKES universe differs; use a separate output directory for this notebook configuration")
            c.execute("INSERT OR IGNORE INTO workspace_meta VALUES ('lake_region',?)", (lake_region,))
            fingerprint = plan.get("science_fingerprint", "test-unversioned")
            old = c.execute(
                "SELECT value FROM workspace_meta WHERE key='science_fingerprint'"
            ).fetchone()
            if old and old[0] != fingerprint:
                raise ValueError(
                    "Scientific implementation changed: use a separate output workspace"
                )
            c.execute(
                "INSERT OR IGNORE INTO workspace_meta VALUES ('science_fingerprint',?)",
                (fingerprint,),
            )
            for scene in plan["scenes"]:
                sid = scene["id"]
                c.execute(
                    "INSERT OR IGNORE INTO scenes (id,scene_json,created,updated,reference_id) VALUES (?,?,?,?,?)",
                    (sid, json.dumps(scene), utcnow(), utcnow(), plan["reference_id"]),
                )
                c.execute(
                    "INSERT OR IGNORE INTO plan_scenes VALUES (?,?)", (plan["id"], sid)
                )
                for region in scene.get("regions", []):
                    c.execute(
                        "INSERT OR IGNORE INTO scene_regions VALUES (?,?,?,?)",
                        (
                            sid,
                            region["country"],
                            region["state_id"],
                            region["state_name"],
                        ),
                    )

    def claim(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM scenes WHERE status IN ('queued','retry') AND attempts < retry_start_attempt + 3 AND next_attempt<=? ORDER BY created,id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE scenes SET status='running',attempts=attempts+1,error=NULL,stage='starting',bytes_downloaded=0,download_seconds=0,updated=? WHERE id=?",
                (utcnow(), row["id"]),
            )
            return {
                **dict(row),
                "attempts": row["attempts"] + 1,
                "scene": json.loads(row["scene_json"]),
            }

    def update(self, sid, **fields):
        allowed = {
            "status",
            "error",
            "stage",
            "archive",
            "archive_sha256",
            "rows",
            "bytes_downloaded",
            "download_seconds",
            "next_attempt",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("Unsupported scene update")
        fields["updated"] = utcnow()
        with self.connect() as c:
            c.execute(
                "UPDATE scenes SET "
                + ",".join(f"{k}=?" for k in fields)
                + " WHERE id=?",
                (*fields.values(), sid),
            )

    def fail(self, sid, message):
        with self.connect() as c:
            row = c.execute("SELECT attempts,retry_start_attempt FROM scenes WHERE id=?", (sid,)).fetchone()
            n = row[0] - row[1]
        self.update(
            sid,
            status="failed" if n >= 3 else "retry",
            error=message[-4000:],
            stage="failed",
            next_attempt=time.time() + min(60 * 2 ** (n - 1), 300),
        )

    def requeue_failed(self):
        """Grant all terminal failures three more attempts without resetting history."""
        stamp = utcnow()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            failed = list(c.execute("SELECT id,attempts,error FROM scenes WHERE status='failed' ORDER BY id"))
            for row in failed:
                c.execute(
                    "INSERT INTO timings VALUES (?,?,?,?,?,?,?)",
                    (row["id"], row["attempts"], "requeue_failed", stamp, 0.0, 1,
                     json.dumps({"previous_error": row["error"], "additional_attempts": 3})),
                )
            c.execute("""UPDATE scenes SET status='queued',stage='requeued',error=NULL,
                      retry_start_attempt=attempts,next_attempt=0,updated=? WHERE status='failed'""", (stamp,))
        return {"requeued": len(failed), "additional_attempts_per_scene": 3,
                "scene_ids": [row["id"] for row in failed]}

    def timing(self, sid, attempt, stage, started, seconds, ok, details=None):
        with self.connect() as c:
            c.execute(
                "INSERT INTO timings VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    attempt,
                    stage,
                    started,
                    seconds,
                    int(ok),
                    json.dumps(details or {}),
                ),
            )

    def rows(self, status=None):
        with self.connect() as c:
            sql = "SELECT * FROM scenes"
            args = ()
            if status:
                sql += " WHERE status=?"
                args = (status,)
            return [dict(r) for r in c.execute(sql + " ORDER BY created,id", args)]

    def scene(self, sid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM scenes WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ValueError(f"Unknown scene: {sid}")
            result = dict(row)
            result["timings"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM timings WHERE scene_id=? ORDER BY attempt,started",
                    (sid,),
                )
            ]
            result["regions"] = [
                dict(r)
                for r in c.execute(
                    "SELECT country,state_id,state_name FROM scene_regions WHERE scene_id=?",
                    (sid,),
                )
            ]
            return result

    def summary(self):
        with self.connect() as c:
            return {
                "scope": "Scenes discovered in saved plans; not an estimate of the entire satellite archive",
                "counts": {
                    r[0]: r[1]
                    for r in c.execute(
                        "SELECT status,count(*) FROM scenes GROUP BY status"
                    )
                },
                "plans": c.execute("SELECT count(*) FROM plans").fetchone()[0],
            }

    def regions(self):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT r.country,r.state_id,r.state_name,
              count(DISTINCT s.id) AS discovered,
              count(DISTINCT CASE WHEN s.status='done' THEN s.id END) AS processed,
              count(DISTINCT CASE WHEN s.status='failed' THEN s.id END) AS failed
              FROM scene_regions r JOIN scenes s ON r.scene_id=s.id
              GROUP BY r.country,r.state_id,r.state_name ORDER BY r.country,r.state_name"""
                )
            ]

    def light_rows(self, status=None):
        """Like rows(), without scene_json: fast on large queues."""
        with self.connect() as c:
            sql = f"SELECT {LIGHT_COLUMNS} FROM scenes"
            args = ()
            if status:
                sql += " WHERE status=?"
                args = (status,)
            return [dict(r) for r in c.execute(sql + " ORDER BY created,id", args)]

    def plan_rows(self):
        """Every registered plan with its scene counts by status."""
        with self.connect() as c:
            plans = [dict(r) for r in c.execute("SELECT id,path,created,metadata FROM plans ORDER BY created,id")]
            counts = {}
            for plan_id, status, n in c.execute(
                "SELECT p.plan_id, s.status, count(*) FROM plan_scenes p JOIN scenes s ON s.id=p.scene_id "
                "GROUP BY p.plan_id, s.status"
            ):
                counts.setdefault(plan_id, {})[status] = n
        for plan in plans:
            plan["counts"] = counts.get(plan["id"], {})
        return plans

    def scenes_without_plan(self):
        """Scene counts by status for scenes that no registered plan lists."""
        with self.connect() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT status,count(*) FROM scenes WHERE id NOT IN (SELECT scene_id FROM plan_scenes) GROUP BY status")}

    def withdraw(self, statuses, plan_id=None, start=None, end=None, drop_plan=False, dry_run=False):
        """Delete scenes in the given statuses from the queue, in one write transaction.

        A scene a worker has claimed is `running` before this lock is granted, so it is never
        removed; neither are publishing or finished scenes unless named in `statuses`.
        With plan_id only that plan's scenes are considered, and a scene that another plan still
        lists is only unlinked from this plan. start/end (dates) limit by the scene's UTC start day.
        drop_plan: also delete the plan row once no scene is linked to it.
        """
        statuses = tuple(statuses)
        marks = ",".join("?" * len(statuses))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if plan_id:
                found = c.execute(
                    f"SELECT s.id,s.status,s.scene_json FROM scenes s JOIN plan_scenes p ON p.scene_id=s.id "
                    f"WHERE p.plan_id=? AND s.status IN ({marks}) ORDER BY s.id", (plan_id, *statuses)).fetchall()
            else:
                found = c.execute(
                    f"SELECT id,status,scene_json FROM scenes WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
            chosen = []
            for row in found:
                if start is not None or end is not None:
                    day = _scene_day(row["scene_json"])
                    if (start is not None and day < start) or (end is not None and day > end):
                        continue
                chosen.append((row["id"], row["status"]))
            shared = set()
            if plan_id:
                for part in _chunks(sid for sid, _ in chosen):
                    shared |= {r[0] for r in c.execute(
                        f"SELECT DISTINCT scene_id FROM plan_scenes WHERE plan_id<>? "
                        f"AND scene_id IN ({','.join('?' * len(part))})", (plan_id, *part))}
            removed = [(sid, status) for sid, status in chosen if sid not in shared]
            unlinked = [(sid, status) for sid, status in chosen if sid in shared]
            before = {}
            if plan_id:
                before = {r[0]: r[1] for r in c.execute(
                    "SELECT s.status,count(*) FROM plan_scenes p JOIN scenes s ON s.id=p.scene_id "
                    "WHERE p.plan_id=? GROUP BY s.status", (plan_id,))}
            if not dry_run:
                for part in _chunks(sid for sid, _ in removed):
                    inside = ",".join("?" * len(part))
                    for table, column in (("timings", "scene_id"), ("scene_regions", "scene_id"),
                                          ("plan_scenes", "scene_id"), ("scenes", "id")):
                        c.execute(f"DELETE FROM {table} WHERE {column} IN ({inside})", part)
                for part in _chunks(sid for sid, _ in unlinked):
                    c.execute(f"DELETE FROM plan_scenes WHERE plan_id=? AND scene_id IN ({','.join('?' * len(part))})",
                              (plan_id, *part))
            left = dict(before)
            for _, status in removed + unlinked:
                left[status] = left.get(status, 0) - 1
            left = {k: v for k, v in left.items() if v > 0}
            plan_removed = bool(plan_id and drop_plan and not left)
            if plan_removed and not dry_run:
                c.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        by_status = {}
        for _, status in removed:
            by_status[status] = by_status.get(status, 0) + 1
        return {"removed": [sid for sid, _ in removed], "removed_by_status": by_status,
                "unlinked_shared": [sid for sid, _ in unlinked], "left_in_plan": left,
                "plan_removed": plan_removed}

    # --- merge history: one row per daily merge (manual `daily` and `run --auto-daily`) ----------

    def daily_start(self, country, state_id, day, source="manual", signature=None, log=None):
        """Record that a merge of one solar date began. Returns the row id."""
        with self.connect() as c:
            return c.execute(
                "INSERT INTO daily_runs (country,state_id,date_solar,source,state,started,signature,log) "
                "VALUES (?,?,?,?,'merging',?,?,?)",
                (str(country or "").upper(), str(state_id or ""), str(day), source, utcnow(), signature, log),
            ).lastrowid

    def daily_finish(self, run_id, state, counts=None, details=None, error=None, signature=None):
        """Close a merge row. state: merged, failed or interrupted. counts: complete, partial, waiting,
        unavailable (lakes), missing, not_planned (scenes)."""
        counts = counts or {}
        with self.connect() as c:
            c.execute(
                "UPDATE daily_runs SET state=?, finished=?, complete_lakes=?, partial_lakes=?, waiting_lakes=?, "
                "unavailable_lakes=?, missing_scenes=?, not_planned_scenes=?, details=?, error=?, "
                "signature=COALESCE(?,signature) WHERE id=?",
                (state, utcnow(), counts.get("complete"), counts.get("partial"), counts.get("waiting"),
                 counts.get("unavailable"), counts.get("missing"), counts.get("not_planned"),
                 json.dumps(details, default=str) if details else None, error, signature, run_id),
            )

    def daily_close_interrupted(self, keep=None):
        """A merge whose process is gone can never close its own row. Call this only while holding
        the daily lock (so no other merge is running); `keep` is the row of the merge that holds it."""
        with self.connect() as c:
            c.execute(
                "UPDATE daily_runs SET state='interrupted', finished=?, "
                "error='the merge process stopped before it finished' WHERE state='merging' AND id IS NOT ?",
                (utcnow(), keep),
            )

    def daily_row(self, run_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM daily_runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def daily_rows(self, country=None, state_id=None):
        """Merge history, oldest first; limited to one state when country and state_id are given."""
        with self.connect() as c:
            if country:
                rows = c.execute("SELECT * FROM daily_runs WHERE country=? AND state_id=? ORDER BY id",
                                 (str(country).upper(), str(state_id))).fetchall()
            else:
                rows = c.execute("SELECT * FROM daily_runs ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def daily_latest(self, country, state_id):
        """{solar date: newest merge row} for one state."""
        return {r["date_solar"]: r for r in self.daily_rows(country, state_id)}

    def plan_regions(self):
        """{(COUNTRY, state_id): {"name", "plans": [{"id", "start", "end"}]}} from the saved plans."""
        found = {}
        with self.connect() as c:
            for r in c.execute("SELECT id,metadata FROM plans ORDER BY created,id"):
                m = json.loads(r["metadata"] or "{}")
                if not m.get("country") or not m.get("state_id"):
                    continue
                entry = found.setdefault((str(m["country"]).upper(), str(m["state_id"])),
                                         {"name": m.get("state_name", ""), "plans": []})
                entry["plans"].append({"id": r["id"], "start": m.get("start"), "end": m.get("end")})
        return found

    def state_scenes(self, country, state_id):
        """{scene id: status} of every scene that can feed the lakes of one state: the scenes of that
        state's plans, plus any scene (of any plan) recorded as touching the state."""
        country, state_id = str(country).upper(), str(state_id)
        plans = [p["id"] for p in self.plan_regions().get((country, state_id), {"plans": []})["plans"]]
        with self.connect() as c:
            found = {r[0]: r[1] for r in c.execute(
                "SELECT s.id,s.status FROM scenes s JOIN scene_regions r ON r.scene_id=s.id "
                "WHERE r.country=? AND r.state_id=?", (country, state_id))}
            for part in _chunks(plans, 50):
                for r in c.execute(
                    "SELECT s.id,s.status FROM scenes s JOIN plan_scenes p ON p.scene_id=s.id "
                    f"WHERE p.plan_id IN ({','.join('?' * len(part))})", part):
                    found[r[0]] = r[1]
        return found

    def scene_starts(self, ids):
        """{scene id: start time text} read without loading whole scene records where SQLite allows."""
        found = {}
        with self.connect() as c:
            for part in _chunks(ids):
                marks = ",".join("?" * len(part))
                try:
                    rows = c.execute(
                        f"SELECT id, json_extract(scene_json,'$.start') FROM scenes WHERE id IN ({marks})", part)
                    found.update({r[0]: r[1] for r in rows})
                except sqlite3.OperationalError:  # SQLite built without JSON support
                    rows = c.execute(f"SELECT id, scene_json FROM scenes WHERE id IN ({marks})", part)
                    found.update({r[0]: json.loads(r[1]).get("start") for r in rows})
        return found

    def change_marker(self):
        """Cheap fingerprint of what decides whether a date can merge: how many scenes are final (done, skipped,
        failed), how many exist, how many plans, and the merge history. Queued -> running changes nothing here."""
        with self.connect() as c:
            counts = {r[0]: r[1] for r in c.execute("SELECT status,count(*) FROM scenes GROUP BY status")}
            return (tuple(counts.get(k, 0) for k in ("done", "skipped_no_lakes", "failed")), sum(counts.values()),
                    c.execute("SELECT count(*) FROM plans").fetchone()[0],
                    tuple(c.execute("SELECT count(*),COALESCE(max(id),0) FROM daily_runs").fetchone()))

    def status_counts(self):
        """{status: scenes}, cheap: answered from the status index."""
        with self.connect() as c:
            return {r[0]: r[1] for r in c.execute("SELECT status,count(*) FROM scenes GROUP BY status")}
