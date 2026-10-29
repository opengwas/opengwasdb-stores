"""Load a script from `tools/` so it can be tested (#386).

The scripts under `tools/` are entry points, not part of the installed package,
so a test loads them by path -- the convention
`tests/prune-worktrees/test_prune_worktrees.py` already uses. Kept in one place
so the callers cannot drift. `tests/packaging/test_imports.py` lists this file
among the exemptions it needs, because loading a script by path is exactly what
that test otherwise forbids.

Run the importing test from the repository root; Python puts the test's own
directory first on `sys.path`, so `from script_modules import load_script`
resolves here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def load_script(name: str) -> ModuleType:
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so that anything the module resolves through
    # its own namespace -- a dataclass reads `sys.modules[cls.__module__]` --
    # finds it.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
