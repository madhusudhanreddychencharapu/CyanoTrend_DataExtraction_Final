"""Per-scene OCSSW environment and bounded, logged subprocess execution."""

from __future__ import annotations
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def die_with_parent():
    """Linux workers/OCSSW children stop if their owning process disappears."""
    if sys.platform.startswith("linux"):
        parent = os.getppid()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Unable to set parent-death signal")
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)


def configure(workspace, scene_dir, telemetry):
    from .core import settings, runtime, utils

    settings.OCSSWROOT = workspace.ocssw
    for key, folder in [
        ("RAW_L1_DIR", "raw"),
        ("STAGING_DIR", "staging"),
        ("L2_DIR", "l2"),
        ("PAR_DIR", "par"),
        ("LOG_DIR", "logs"),
        ("EXPORT_DIR", "provenance"),
    ]:
        p = scene_dir / folder
        p.mkdir(parents=True, exist_ok=True)
        setattr(settings, key, p)
    settings.OCSSW_SUBPROCESS_ENV = runtime.read_bash_environment(
        workspace.ocssw / "OCSSW_bash.env"
    )
    # getanc defaults otherwise share a writable ancillary store across workers.
    ancillary = scene_dir / "ancillary"
    ancillary.mkdir(exist_ok=True)
    settings.OCSSW_SUBPROCESS_ENV["L2ANCILLARY"] = str(ancillary)
    settings.L2GEN_BIN = runtime.find_ocssw_tool("l2gen")
    settings.GETANC_BIN = runtime.find_ocssw_tool("getanc")
    runtime.require_ocssw(require_getanc=True)
    utils.download_progress = telemetry.download
    utils.run_logged = lambda command, log_path, timeout=None, cwd=None: run_logged(
        command, log_path, timeout, cwd, settings.OCSSW_SUBPROCESS_ENV, telemetry
    )


def run_logged(command, log_path, timeout, cwd, environment, telemetry):
    from .telemetry import redact

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = "getanc" if "getanc" in Path(command[0]).name else "l2gen"
    started = time.monotonic()
    with telemetry.stage(stage), path.open("w") as log:
        log.write("command=" + repr([str(x) for x in command]) + "\n")
        log.flush()
        proc = subprocess.Popen(
            [str(x) for x in command],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=environment,
            start_new_session=True,
            preexec_fn=die_with_parent if sys.platform.startswith("linux") else None,
        )
        try:
            rc = proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        log.write(
            f"\nelapsed_seconds={time.monotonic()-started:.3f}\nreturn_code={rc}\n"
        )
    with path.open("rb") as log:
        log.seek(max(0, path.stat().st_size - 4000))
        tail = redact(log.read().decode(errors="replace"))
    if rc:
        raise RuntimeError(f"{stage} exited {rc}: {tail}")
    return subprocess.CompletedProcess(command, rc, tail, "")
