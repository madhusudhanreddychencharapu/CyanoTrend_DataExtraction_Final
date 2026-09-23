"""Bind outputs to the scientific implementation without user configuration files."""

import hashlib
from pathlib import Path


def science_fingerprint():
    root = Path(__file__).parent
    files = sorted((root / "core").glob("*.py")) + [
        root / "worker.py",
        root / "execution.py",
        root / "references.py",
        root / "schema.py",
    ]
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()
