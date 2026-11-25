"""Read manifest-level facts of a published Store through the engine's command line.

`opengwasdb info --format json` is the one sanctioned way the registry looks at
a Store (ADR 0023). This module holds its reader, with no opengwasdb import, so
`readiness` and `register` share it: importing `ogstores.register` must not load
the engine (`tests/bundle`, `TestImportCost`).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


def store_info(
    store_path: Path | str,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Read manifest-level facts through ``opengwasdb info --format json``.

    This is the one sanctioned way the registry looks at a published Store
    (ADR 0023): the core command owns opening the Store, and this function
    only parses its JSON. It reads no Store file or directory itself.
    """
    argv = ["opengwasdb", "info", str(store_path), "--format", "json"]
    run = runner or _default_runner
    result = run(argv)
    if result.returncode != 0:
        raise ValueError(
            f"`{' '.join(argv)}` failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"`{' '.join(argv)}` did not print JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"`{' '.join(argv)}` printed {type(payload).__name__}, not an object")
    return payload


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


__all__ = ["store_info"]
