"""Runtime functions selected from the v2.6.8 reference implementation.

See docs/SCIENCE_PARITY.md for the source audit and operational adaptations.
"""

from __future__ import annotations
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from . import settings as _m_settings


def read_bash_environment(script: Path) -> dict[str, str]:
    """Return a sourced Bash environment without mutating this Python process."""
    environment = os.environ.copy()
    environment["OCSSWROOT"] = str(_m_settings.OCSSWROOT)
    script = Path(script)
    if not script.is_file():
        return environment
    command = f"source {shlex.quote(str(script))} >/dev/null 2>&1 && env -0"
    result = subprocess.run(
        ["bash", "-lc", command], capture_output=True, check=True, env=environment
    )
    for item in result.stdout.split(b"\x00"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        environment[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return environment


def find_ocssw_tool(name: str) -> Path | None:
    candidates = [
        _m_settings.OCSSWROOT / "bin" / name,
        _m_settings.OCSSWROOT / "scripts" / name,
        _m_settings.OCSSWROOT / "scripts" / f"{name}.py",
    ]
    found = shutil.which(name) or shutil.which(f"{name}.py")
    if found:
        candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


def command_version(path: Path | None) -> str:
    if path is None:
        return "not found"
    for option in ("--version", "version"):
        result = subprocess.run(
            [str(path), option],
            capture_output=True,
            text=True,
            env=_m_settings.OCSSW_SUBPROCESS_ENV,
            timeout=30,
        )
        text = (result.stdout + "\n" + result.stderr).strip()
        if text:
            return text.splitlines()[0]
    return str(path)


def require_ocssw(require_getanc: bool = False) -> None:
    if _m_settings.L2GEN_BIN is None:
        raise RuntimeError("l2gen was not found. Complete the local OCSSW setup first.")
    if require_getanc and _m_settings.GETANC_BIN is None:
        raise RuntimeError("getanc was not found in the active OCSSW installation.")
