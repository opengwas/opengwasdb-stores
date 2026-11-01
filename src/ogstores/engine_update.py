"""Move pixi.lock to opengwasdb `main`'s tip, and check what it moved to (#253).

    pixi run engine-update            # relock, install, check; say what moved
    pixi run engine-update --check    # check the lock and the installed engine only

`pixi.toml` follows opengwasdb's `main`; the lock holds the exact commit. This
moves the lock (`pixi update opengwasdb`), installs it, and checks that:

* the lock resolves exactly one opengwasdb commit;
* the engine each store-build environment installs is that commit;
* that engine writes the Store format the release guard accepts
  (`ogstores.runtime.REQUIRED_FORMAT_VERSION`).

Any failure puts the previous lock back and says why: a format change is a
deliberate repository change, never something an update slips in. Run it at the
start of a working session and commit `pixi.lock` on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from ogstores import engine, paths, runtime

REPO_ROOT = paths.REPO_ROOT
RELEASES_URL = "https://github.com/opengwas/opengwasdb/releases"

#: Prints the engine an environment imports: commit, version, Store format.
_PROBE = (
    "import json; from ogstores import engine; "
    "from opengwasdb.store.open import CURRENT_FORMAT_VERSION as f; "
    "e = engine.in_process(); print(json.dumps([e.commit, e.version, f]))"
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class EngineUpdateError(RuntimeError):
    """The updated lock or the engine it installs is not one this repository accepts."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), cwd=REPO_ROOT, capture_output=True, text=True)


def pixi() -> str:
    return os.environ.get("PIXI_EXE") or shutil.which("pixi") or "pixi"


def store_build_environments(manifest: Path) -> list[str]:
    """The environments that install the engine: those with the store-build feature."""
    import tomllib

    document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    return sorted(
        name
        for name, spec in document["environments"].items()
        if "store-build" in spec.get("features", [])
    )


def locked_version(lock: Path) -> str:
    """The opengwasdb version the lock's package entry records."""
    document = yaml.load(lock.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader) or {}
    for entry in document.get("packages") or []:
        if entry.get("name") == "opengwasdb":
            return str(entry.get("version"))
    return engine.UNAVAILABLE


def check_installed(environments: Sequence[str], expected: str, run: Runner = _run) -> str:
    """Each environment's engine is `expected` and writes the accepted format; returns its version."""
    versions = set()
    for name in environments:
        probe = run([pixi(), "run", "--frozen", "-e", name, "python3", "-c", _PROBE])
        if probe.returncode != 0:
            raise EngineUpdateError(f"environment {name}: cannot probe the engine: {probe.stderr.strip()}")
        commit, version, written = json.loads(probe.stdout.strip().splitlines()[-1])
        if commit != expected:
            raise EngineUpdateError(
                f"environment {name} installs opengwasdb {commit}, not the locked {expected}"
            )
        if written != runtime.REQUIRED_FORMAT_VERSION:
            raise EngineUpdateError(
                f"opengwasdb {version} ({commit}) writes Store format {written}, but the release "
                f"guard accepts {runtime.REQUIRED_FORMAT_VERSION}. Moving to it is a deliberate "
                "change (REQUIRED_FORMAT_VERSION, docs/runtime.md, the completion-parent rules), "
                "not an engine update."
            )
        versions.add(version)
    return ", ".join(sorted(versions))


def _dirty(paths_: Sequence[str], run: Runner) -> str:
    return run(["git", "status", "--porcelain", "--", *paths_]).stdout.strip()


def update(lock: Path, manifest: Path, *, run: Runner = _run) -> tuple[str, str, str, str]:
    """Relock to `main`'s tip and check it. Returns (old commit, old version, new commit, new version).

    The previous lock is restored when anything fails, so a refused update
    leaves the checkout as it was.
    """
    dirty = _dirty(["pixi.toml", "pixi.lock"], run)
    if dirty:
        raise EngineUpdateError(f"pixi.toml or pixi.lock has uncommitted changes; commit or revert them first:\n{dirty}")
    before, before_version, saved = engine.locked_commit(lock), locked_version(lock), lock.read_bytes()
    try:
        relock = run([pixi(), "update", "opengwasdb"])
        if relock.returncode != 0:
            raise EngineUpdateError(f"`pixi update opengwasdb` failed: {relock.stderr.strip()}")
        after = engine.locked_commit(lock)
        after_version = check_installed(store_build_environments(manifest), after, run)
    except (EngineUpdateError, engine.EnginePinError):
        lock.write_bytes(saved)
        raise
    return before, before_version, after, after_version


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="check the lock and installed engine; change nothing")
    args = parser.parse_args(argv)
    lock, manifest = REPO_ROOT / "pixi.lock", REPO_ROOT / "pixi.toml"
    try:
        if args.check:
            commit = engine.locked_commit(lock)
            version = check_installed(store_build_environments(manifest), commit)
            print(f"opengwasdb {version} at {commit}: locked, installed, and writes format {runtime.REQUIRED_FORMAT_VERSION}")
            return 0
        before, before_version, after, after_version = update(lock, manifest)
    except (EngineUpdateError, engine.EnginePinError) as exc:
        print(f"engine-update: refused: {exc}", file=sys.stderr)
        return 1
    if before == after:
        print(f"opengwasdb {after_version} at {after} is already main's tip; pixi.lock is unchanged.")
        return 0
    print(f"opengwasdb {before_version} ({before[:12]}) -> {after_version} ({after[:12]})")
    print(f"  what changed: https://github.com/opengwas/opengwasdb/compare/{before}...{after}")
    print(f"  release notes: {RELEASES_URL}")
    print("Commit the lock on its own:")
    print(f'  git commit pixi.lock -m "Move opengwasdb to {after_version} ({after[:12]})"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
