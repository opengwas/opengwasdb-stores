"""What every collector shares: the data directory, the envelope, the `gh` runner (#389)."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from ogstores import paths

DATA_DIR_ENV_VAR = "OPENGWASDB_SITE_DATA"
DEFAULT_DATA_DIR = paths.REPO_ROOT / "docs" / "_data"

# Runs `gh <args>` with optional standard input and returns its standard
# output. A fake of this is how the tests stand in for the network.
GhRunner = Callable[[list[str], str | None], str]


class CollectError(RuntimeError):
    """A source could not be read; its file says so instead of stopping the others."""


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    value = (os.environ if env is None else env).get(DATA_DIR_ENV_VAR)
    return Path(value) if value and value.strip() else DEFAULT_DATA_DIR


def gh_runner(args: list[str], stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], input=stdin, capture_output=True, text=True, check=False, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectError(f"gh {' '.join(args[:2])}: {type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        raise CollectError(f"gh {' '.join(args[:2])} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(source: str, *, available: bool, error: str | None, **payload: Any) -> dict[str, Any]:
    return {"source": source, "available": available, "collected_at": now(), "error": error, **payload}


def collect_source(source: str, collect: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one collector. Any failure becomes `available: false` with the error."""
    try:
        return envelope(source, available=True, error=None, **collect())
    except Exception as exc:  # a source that fails must never stop the others
        return envelope(source, available=False, error=f"{type(exc).__name__}: {exc}")


def write_source(directory: Path, result: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result['source']}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    return path
