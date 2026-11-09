"""Deterministic SHA-256 file and byte hashing utilities for SpecSnap."""

from __future__ import annotations

import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"File not found for hashing: {resolved}")

    h = hashlib.sha256()
    with open(resolved, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
