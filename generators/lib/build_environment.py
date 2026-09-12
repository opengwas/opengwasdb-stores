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


def _opengwasdb_commit() -> str:
    """Best-effort: pip records the resolved Git commit of a `git+https://...`
    dependency in the installed distribution's `direct_url.json`."""
    try:
        import importlib.metadata as metadata

        dist = metadata.distribution("opengwasdb")
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            return UNAVAILABLE
        direct_url = json.loads(direct_url_text)
        commit = direct_url.get("vcs_info", {}).get("commit_id")
        return commit or UNAVAILABLE
    except Exception:
        return UNAVAILABLE


def _opengwasdb_version() -> str:
    try:
        import importlib.metadata as metadata

        return metadata.version("opengwasdb")
    except Exception:
        return UNAVAILABLE


def capture(root: Path) -> dict[str, str]:
    """Capture this process's build-environment provenance, rooted at `root`
    (the opengwasdb-stores repository root)."""
    import os

    return {
        "repo_commit": _repo_commit(root),
        "opengwasdb_version": _opengwasdb_version(),
        "opengwasdb_commit": _opengwasdb_commit(),
        "pixi_lock_sha256": _pixi_lock_sha256(root),
        "pixi_environment": os.environ.get("PIXI_ENVIRONMENT_NAME", UNAVAILABLE),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }


if __name__ == "__main__":
    print(json.dumps(capture(Path(__file__).resolve().parents[2]), indent=2, sort_keys=True))
