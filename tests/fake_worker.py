"""Subprocess fixture: exercise scheduler concurrency without satellite processing."""

import argparse, csv, hashlib, io, json, time, zipfile
from cyanotrend.common import Workspace, file_lock
from cyanotrend.ledger import Ledger
from cyanotrend.schema import COLUMNS
from cyanotrend.worker import candidate_path

p = argparse.ArgumentParser()
for name in ("output", "references", "scratch", "ocssw", "scene-id", "attempt"):
    p.add_argument("--" + name)
a = p.parse_args()
ws = Workspace.create(a.output, a.references, a.scratch, a.ocssw)
with file_lock(ws.output / ".scene-locks" / f"{a.scene_id}.lock"):
    # At least five workers must overlap, not just start sequentially.
    mark = ws.output / (a.scene_id + ".active")
    mark.touch()
    time.sleep(0.3)
    n = len(list(ws.output.glob("*.active")))
    (ws.output / (a.scene_id + ".concurrency")).write_text(str(n))
    ledger = Ledger(ws.db)
    record = ledger.scene(a.scene_id)
    scene = json.loads(record["scene_json"])
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=COLUMNS)
    w.writeheader()
    w.writerow({"scene_id": a.scene_id, "Hylak_id": 1, "window_id": "FULL"})
    content = {"lakepixels.nc": b"fixture", "statistics.csv": b.getvalue().encode()}
    meta = {
        "scene": scene,
        "reference_id": "ref",
        "statistics_rows": 1,
        "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in content.items()},
    }
    path = candidate_path(ws, a.scene_id)
    path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in content.items():
            z.writestr(k, v)
        z.writestr("metadata.json", json.dumps(meta))
    mark.unlink()
