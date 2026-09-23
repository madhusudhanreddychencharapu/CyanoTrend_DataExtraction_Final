"""One isolated full-scene attempt. The coordinator owns master CSV publication."""

from __future__ import annotations
import argparse
import json
import math
import os
import signal
import time
import zipfile
from .common import Workspace, atomic_json, digest, file_lock, sync_dir, utcnow
from .ledger import Ledger
from .identity import science_fingerprint
from .telemetry import Telemetry, redact


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def candidate_path(workspace, sid):
    return workspace.output / ".pending" / f"{sid}.zip"


def build_bundle(workspace, scene, reference_id, compact, stats, metadata):
    """Only three final artifacts; intermediate L1/L2/par/log files are excluded."""
    from .schema import COLUMNS

    path = candidate_path(workspace, scene["id"])
    path.parent.mkdir(exist_ok=True)
    temp = path.with_suffix(".zip.part")
    stats = stats.reindex(columns=COLUMNS)
    # A portable archive-member locator replaces a soon-to-be-deleted scratch path.
    stats["compact_netcdf"] = (
        str(workspace.archive(scene).relative_to(workspace.output)) + "::lakepixels.nc"
    )
    csv_path = compact.parent / "statistics.csv"
    stats.to_csv(csv_path, index=False)
    meta = {
        **metadata,
        "schema": "cyanotrend-scene-1",
        "software_version": __import__("cyanotrend").__version__,
        "science_fingerprint": science_fingerprint(),
        "scene": scene,
        "reference_id": reference_id,
        "created": utcnow(),
        "statistics_rows": len(stats),
        "processing_scope": "full_scene",
        "coordinate_source": {
            "latitude": "HydroLAKES Pour_lat",
            "longitude": "HydroLAKES Pour_long",
        },
        "state_source": "geoBoundaries gbOpen ADM1 shapeName/shapeID at original HydroLAKES coordinates",
        "sha256": {
            "lakepixels.nc": digest(compact),
            "statistics.csv": digest(csv_path),
        },
    }
    with zipfile.ZipFile(temp, "w", allowZip64=True) as z:
        z.write(compact, "lakepixels.nc", compress_type=zipfile.ZIP_STORED)
        z.write(csv_path, "statistics.csv", compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(
            "metadata.json",
            json.dumps(clean_json(meta), indent=2, default=str, allow_nan=False),
            compress_type=zipfile.ZIP_DEFLATED,
        )
    with temp.open("rb") as f:
        os.fsync(f.fileno())
    from .publication import validate_archive

    validate_archive(temp, scene["id"], reference_id)
    os.replace(temp, path)
    sync_dir(path.parent)
    return path


def process(workspace, sid, attempt):
    from . import references
    from .core import catalogue, compact, statistics, staging
    from . import execution

    ledger = Ledger(workspace.db)
    workspace.ensure()
    scratch = workspace.scratch / sid
    with file_lock(workspace.output / ".scene-locks" / f"{sid}.lock"):
        record = ledger.scene(sid)
        # An old orphan that starts late must not execute a newer attempt.
        if record["attempts"] != attempt or record["status"] != "running":
            return
        scene = json.loads(record["scene_json"])
        if references.manifest(workspace)["reference_id"] != record["reference_id"]:
            raise ValueError("Worker reference data differs from saved plan")
        scratch.mkdir(parents=True, exist_ok=True)
        metrics = Telemetry(ledger, sid, attempt)
        try:
            with metrics.stage("total"):
                with metrics.stage("reference_loading"):
                    lakes = catalogue.candidate_lakes_for_scene(
                        scene, references.load_lakes(workspace)
                    )
                execution.configure(workspace, scratch, metrics)
                with metrics.stage("download"):
                    metrics.start = time.monotonic()
                    raw, sen3, method = staging.stage_scene_efficient(
                        scene, os.environ["CDSE_USERNAME"], os.environ["CDSE_PASSWORD"]
                    )
                    metrics.finish_download()
                from .core.l2gen import run_l2gen

                with metrics.stage("l2_processing"):
                    l2, info = run_l2gen(sen3, process_full_scene=True)
                with metrics.stage("native_extraction"):
                    nc = scratch / "lakepixels.nc"
                    product, counts = compact.extract_lake_only_netcdf(
                        l2, scene, lakes, nc, config_hash=record["reference_id"]
                    )
                    if product is None:
                        # A successfully processed scene with no geolocated lake
                        # observations still gets an explicit empty native product.
                        ds, out, tmp = compact._open_compact_writer_v264(
                            nc,
                            {
                                "scene_id": sid,
                                "scene_name": scene["name"],
                                "acquisition_start": scene["start"],
                                "window_id": "FULL",
                                "processing_config_hash": record["reference_id"],
                                "empty_reason": counts.get(
                                    "reason", "no geolocated native lake observations"
                                ),
                            },
                        )
                        ds.close()
                        tmp.replace(nc)
                    elif not compact.validate_compact_product(
                        nc, scene_id=sid, config_hash=record["reference_id"]
                    ):
                        raise ValueError("Native NetCDF validation failed")
                with metrics.stage("statistics"):
                    table = statistics.compact_stats(nc, lakes)
                    if not table.empty:
                        additional = lakes[
                            [
                                "Hylak_id",
                                "state_name",
                                "state_id",
                                "latitude",
                                "longitude",
                            ]
                        ]
                        table = table.merge(
                            additional,
                            on="Hylak_id",
                            how="left",
                            validate="many_to_one",
                        )
                with metrics.stage("packaging"):
                    unmatched = lakes[lakes.admin_match_status != "matched"]
                    from .core import settings

                    metadata = {
                        "download_method": method,
                        "native_observations": counts,
                        "l2gen": info,
                        "science": {
                            "qa_profile": settings.DEFAULT_MASK_PROFILE,
                            "min_lake_area_km2": 1.3,
                            "ci_detection_limit": settings.CI_DETECTION_LIMIT,
                            "ndci_source": "rhos",
                            "band_mapping": {"885": "rhos_884"},
                            "climatology_fallback": False,
                        },
                        "unresolved_state_assignments": unmatched[
                            ["Hylak_id", "admin_match_status"]
                        ].to_dict("records"),
                        "timings": ledger.scene(sid)["timings"],
                        "attempt": attempt,
                    }
                    build_bundle(
                        workspace, scene, record["reference_id"], nc, table, metadata
                    )
            ledger.update(sid, stage="ready_to_publish")
        except BaseException as e:
            atomic_json(
                scratch / "failure.json",
                {"error": redact(f"{type(e).__name__}: {e}")[-4000:]},
            )
            raise
        finally:
            # Each worker deletes only its own temporary key and ancillary files.
            staging.release_temporary_s3_credentials(
                os.environ.get("CDSE_USERNAME", ""), os.environ.get("CDSE_PASSWORD", "")
            )


def main():
    from .execution import die_with_parent

    p = argparse.ArgumentParser()
    for name in ("output", "references", "scratch", "ocssw", "scene-id"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--attempt", type=int, required=True)
    a = p.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt("Worker stopping")

    signal.signal(signal.SIGTERM, stop)
    die_with_parent()
    ws = Workspace.create(a.output, a.references, a.scratch, a.ocssw)
    try:
        process(ws, a.scene_id, a.attempt)
    except BaseException as e:
        print(redact(f"{type(e).__name__}: {e}"), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
