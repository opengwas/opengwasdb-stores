"""Build-environment provenance capture (issue #41).

Complements the existing generator-script-hash provenance (see
`release_yaml.py`'s module docstring) with the software environment that
actually executed a generator: this repository's commit, the immutable
`opengwasdb` revision installed, the `pixi.lock` this environment was
resolved from, which Pixi environment ran the command, and the interpreter
version. Best-effort throughout — a field that can't be determined (e.g. not
running under Pixi, or `opengwasdb` not installed via its pinned Git commit)
is recorded as `unavailable` rather than raising, since this is provenance
metadata, not something a build should fail over.
"""
from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

from ogstores import engine

UNAVAILABLE = "unavailable"


def _run(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _repo_commit(root: Path) -> str:
    return _run(["git", "-C", str(root), "rev-parse", "HEAD"]) or UNAVAILABLE


def _pixi_lock_sha256(root: Path) -> str:
    lock = root / "pixi.lock"
    if not lock.exists():
        return UNAVAILABLE
    import hashlib

    digest = hashlib.sha256()
    digest.update(lock.read_bytes())
    return digest.hexdigest()


def capture(root: Path) -> dict[str, str]:
    """Capture this process's build-environment provenance, rooted at `root`
    (the opengwasdb-stores repository root)."""
    import os

    ran = engine.in_process()
    return {
        "repo_commit": _repo_commit(root),
        "opengwasdb_version": ran.version,
        "opengwasdb_commit": ran.commit,
        "pixi_lock_sha256": _pixi_lock_sha256(root),
        "pixi_environment": os.environ.get("PIXI_ENVIRONMENT_NAME", UNAVAILABLE),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


if __name__ == "__main__":
    from manifest_generation import REPO_ROOT

    print(json.dumps(capture(REPO_ROOT), indent=2, sort_keys=True))
