#!/usr/bin/env python3
"""This repository's imports of engine modules, held to a committed baseline (#421).

The engine's `CLAUDE.md` says this repository depends on the engine's command
line and its manifest columns. In fact many files here import engine modules
directly, and each import is a coupling nobody decided on: an engine refactor
can break it. Epic #416, decision 14, is to measure those imports and ratchet
them, the way the engine ratchets its own upward imports
(`docs/architecture/layering-baseline.yaml` in opengwas/opengwasdb,
opengwas/opengwasdb#359).

The unit is one **(importing file, imported name)** per entry, as the source
writes it and package-qualified. The imports are measured with the graph
functions the project site's collector uses (`project_site.collect.architecture_graph`,
in og-dev-hub, which the `dev` environment installs; og-dev-hub#7), so the site's
seam counts and this baseline can never disagree about what an import is. Those
functions' own tests are og-dev-hub's.

Run from the repository root:
    python3 tests/architecture/test_engine_imports_baseline.py
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from project_site.collect import architecture_graph as graph
from project_site.collect import architecture_model as model

REPO = Path(__file__).resolve().parents[2]
NAME = "opengwasdb-stores"
BASELINE = "docs/architecture/engine-imports-baseline.yaml"


class WorkingTree:
    """This checkout's tracked files, read from disk.

    The collector reads a repository through git at a ref; the test that gates a
    pull request must see the working tree, so that a new import fails before it
    is committed. `files` and `read` are the two things the graph functions use
    of a tree.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode()
        self.files = model.split_nul(listing)

    def read(self, paths: list[str]) -> dict[str, str]:
        return {
            path: (self.root / path).read_text(encoding="utf-8", errors="replace")
            for path in paths
            if (self.root / path).is_file()
        }


def this_repository() -> list[dict]:
    """What this checkout imports from the engine, measured the way the collector measures it.

    The trees and claims are keyed by this repository's name, which is how the
    collector keys every repository and how `engine_imports` finds the seam's
    importing side (og-dev-hub#9).
    """
    loaded = model.load_model((REPO / "docs/architecture/components.yaml").read_text(encoding="utf-8"))
    claims = {NAME: model.claims_of(loaded["components"])}
    return graph.imports_of({NAME: WorkingTree(REPO)}, claims)


class ThisRepositoryTests(unittest.TestCase):
    def test_every_engine_import_is_in_the_baseline_and_every_entry_is_still_imported(self) -> None:
        records = this_repository()
        # The guard is on the code that was read, not on the imports: the ratchet
        # must allow the seam to shrink to nothing, and this only catches a wrong
        # directory or ref, which would measure a corner of the repository.
        self.assertGreater(len({record["file"] for record in records}), 100)
        found = graph.engine_imports(records)
        baseline = graph.load_baseline((REPO / BASELINE).read_text(encoding="utf-8"))
        self.assertEqual(graph.check_engine_imports(found, baseline), [])


if __name__ == "__main__":
    unittest.main()
