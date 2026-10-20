"""Isolated NASA executables, suite provenance, and separate daily L2 products."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import netCDF4

from ..common import atomic_json, digest
from ..telemetry import redact
from .products import enrich_l2

L2_PRODUCTS = ("CI_stumpf", "CI_cyano", "flags_habs",
               "rhos_490", "rhos_560", "rhos_620", "rhos_665", "rhos_681",
               "rhos_709", "rhos_754", "rhos_865", "rhos_884")


class DailyTelemetry:
    """Daily progress deliberately does not update the legacy scene ledger."""
    @contextmanager
    def stage(self, name, details=None):
        print(f"Daily: {name}", flush=True)
        yield

    def download(self, n):
        pass


def inventory(root):
    """Hash installed executables and scientific tables, not assumed versions."""
    root = Path(root)
    tools = {}
    for name in ("l2gen", "l2bin", "l3bin", "l3mapgen"):
        path = root / "bin" / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"Daily processing requires executable {path}")
        tools[name] = {"path": str(path), "sha256": digest(path)}
    relative = [
        "common/product.xml", "common/msl12_defaults.par",
        "common/msl12_defaults_CYAN.par", "common/l2bin_defaults_CYAN.par",
        "common/l3mapgen_defaults_CYAN.par", "common/lwm_CONUS_SRTM60m_v3.2.nc",
        "olci/msl12_defaults.par", "olci/msl12_defaults_CYAN.par",
    ]
    for platform in ("s3a", "s3b"):
        relative.extend((
            f"olci/{platform}/msl12_defaults.par",
            f"olci/{platform}/msl12_defaults_CYAN.par",
            f"olci/{platform}/olci{platform}_gas_transmittance_cyan.nc",
        ))
    files = {}
    for name in relative:
        path = root / "share" / name
        if not path.is_file():
            raise ValueError(f"Daily CyAN suite input missing: {path}")
        files[name] = digest(path)
    return {"executables": tools, "suite_files_sha256": files}


class Runner:
    def __init__(self, root, work, environment):
        self.root, self.work = Path(root), Path(work)
        self.environment = dict(environment)
        self.calls = []

    def run(self, tool, parameters, label):
        from ..core.l2gen import write_par_file
        folder = self.work / "commands"
        folder.mkdir(parents=True, exist_ok=True)
        par = write_par_file(folder / f"{label}.par", parameters)
        log_path = folder / f"{label}.log"
        command = [str(self.root / "bin" / tool), f"par={par}"]
        print(f"Daily: {label}", flush=True)
        start = time.monotonic()
        with log_path.open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    env=self.environment, timeout=4 * 3600)
        self.calls.append({"tool": tool, "parameters": {k: str(v) for k, v in parameters.items()},
                           "elapsed_seconds": time.monotonic() - start,
                           "returncode": result.returncode, "log": str(log_path)})
        if tool == "l2bin" and result.returncode == 110:
            # Documented OCSSW no-filled-bins outcome, not a processing failure.
            return None
        if result.returncode:
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 3000))
                tail = redact(log.read().decode(errors="replace"))
            raise RuntimeError(f"{tool} failed ({result.returncode}): {tail}")
        return Path(parameters["ofile"])


def effective_parameters(path):
    with netCDF4.Dataset(path) as ds:
        try:
            group = ds["processing_control/input_parameters"]
        except (KeyError, IndexError) as exc:
            raise ValueError("CYAN L2 lacks effective processing parameters") from exc
        return {name: str(group.getncattr(name)) for name in group.ncattrs()}


def process_scene(workspace, scene, directory, source_root=None):
    """Separate full-swath CYAN calibration; no calls to legacy run_l2gen."""
    from .. import execution
    from ..core import settings, staging, l2gen, utils
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    execution.configure(workspace, directory, DailyTelemetry())
    local = None
    if source_root:
        root = Path(source_root)
        name = str(scene["name"]).removesuffix(".SEN3")
        local = next((p for p in (root / f"{name}.SEN3", root / f"{name}.zip")
                      if p.exists()), None)
        if local is None:
            raise ValueError(f"Local L1 missing for {scene['id']} under {root}")
    if local:
        _, sen3 = staging.stage_existing_input(local)
    else:
        _, sen3, _ = staging.stage_scene_efficient(
            scene, os.environ["CDSE_USERNAME"], os.environ["CDSE_PASSWORD"])
    ancillary = l2gen.run_getanc(sen3, allow_fallback=False)
    if ancillary is None:
        raise ValueError("Daily CyAN processing requires validated ancillary inputs")
    output = directory / "CYAN.L2.nc"
    # A failed/partial L2 is never treated as a completed input.
    output.unlink(missing_ok=True)
    par = l2gen.write_par_file(directory / "CYAN.par", {
        "ifile": sen3 / "xfdumanifest.xml", "ofile": output, "suite": "CYAN",
        "l2prod": ",".join(L2_PRODUCTS), "deflate": 4,
    })
    utils.run_logged([str(settings.L2GEN_BIN), f"par={ancillary}", f"par={par}"],
                     directory / "CYAN.log", timeout=4 * 3600)
    parameters = effective_parameters(output)
    if parameters.get("suite", "").strip() != "CYAN":
        raise ValueError("L2 effective parameters do not confirm suite=CYAN")
    enrich_l2(output)
    info = {"scene": scene, "effective_parameters": parameters,
            "l2_sha256": digest(output)}
    atomic_json(directory / "CYAN.json", info)
    # Daily scratch is separate. L1 is not needed after a successful L2.
    for folder in ("raw", "staging"):
        shutil.rmtree(directory / folder, ignore_errors=True)
    return output, info, dict(settings.OCSSW_SUBPROCESS_ENV)


def job_identity(plan, scenes, day, installed):
    h = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    from ..identity import science_fingerprint
    payload = {"plan_id": plan["id"], "reference_id": plan["reference_id"],
               "selection": plan["selection"], "day": str(day), "scenes": scenes,
               "installed": installed, "daily_code": h.hexdigest(),
               "science": science_fingerprint()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
