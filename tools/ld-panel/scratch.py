"""Where an LD-panel script's scratch work goes (#390).

`tempfile` defaults to the host's `/tmp`, and everything this project writes
lives under `/data/opengwasdb` (CONTRIBUTING, Worktrees). So every script here
asks this module for its scratch directory instead of calling `tempfile` itself:
one default, under `/data`, and one variable, `LD_PANEL_TMPDIR`, for a caller
that has a better root -- a run that sets `$TMPDIR` gets that, which is how the
test runner keeps a suite's files together.

A plain module, not a package: the scripts beside it are run as scripts, and
their own directory is first on `sys.path`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMPDIR_ENV = "LD_PANEL_TMPDIR"
DEFAULT_SCRATCH_ROOT = Path("/data/opengwasdb/work/ld-panel-tmp")


def scratch_root() -> Path:
    """`$LD_PANEL_TMPDIR`, else `$TMPDIR`, else the `/data` default -- never the host's `/tmp`."""
    for name in (TMPDIR_ENV, "TMPDIR"):
        configured = os.environ.get(name)
        if configured and configured.strip():
            return Path(configured)
    return DEFAULT_SCRATCH_ROOT


def scratch_dir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """A scratch directory under `scratch_root`, created and removed by the caller's `with`."""
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root)
