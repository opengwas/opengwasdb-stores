#!/usr/bin/env python3
"""The import graph across both repositories (#418).

An edge is counted per (file, imported name): one file importing three names of
another component is three imports. The fixtures in `architecture_fixtures.py`
are two small repositories whose imports are listed, file by file, in the
comments of the expected values below.

Run from the repository root:
    python3 tests/project-site-architecture/test_architecture_imports.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site.collect import architecture


def collect_edges(test: unittest.TestCase) -> list[dict]:
    root = Path(tempfile.mkdtemp(prefix="arch-"))
    test.addCleanup(__import__("shutil").rmtree, root, True)
    fx.make_repo(root / "registry", fx.REGISTRY_FILES)
    fx.make_repo(root / "engine", fx.ENGINE_FILES)
    result = architecture.collect(root / "registry", engine_git=root / "engine")
    return result["edges"]


def edge(src: str, dst: str, imports: int, scope: str, upward: bool = False) -> dict:
    return {"from": src, "to": dst, "imports": imports, "scope": scope, "upward": upward}


class ImportGraphTests(unittest.TestCase):
    def test_edges_across_both_repositories_are_counted_per_file_and_imported_name(self) -> None:
        self.assertEqual(
            collect_edges(self),
            [
                edge("check", "query", 1, "engine"),  # same layer (2 -> 2): not upward
                edge("cli", "query", 1, "engine"),  # relative `from ..query import facade`, 3 -> 2
                edge("cli", "store", 1, "engine"),  # 3 -> 1
                edge("gen", "model", 1, "registry_to_engine"),  # src/gen/make.py
                edge("gen", "store", 2, "registry_to_engine"),  # two different names in one file
                edge("model", "store", 1, "engine", upward=True),  # 0 -> 1
                edge("ogs", "cli", 1, "registry_to_engine"),
                edge("ogs", "gen", 1, "registry"),
                edge("query", "cli", 1, "engine", upward=True),  # 2 -> 3
                edge("query", "store", 1, "engine"),  # 2 -> 1
                edge("store", "model", 1, "engine"),  # 1 -> 0; `from opengwasdb.store import util` is inside store
                edge("store", "query", 1, "engine", upward=True),  # 1 -> 2
                edge("tools", "cli", 1, "registry_to_engine"),  # tools/run.py is a script, not an importable module
            ],
        )

    def test_the_summary_counts_upward_edges_imports_and_the_seam(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="arch-"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        fx.make_repo(root / "registry", fx.REGISTRY_FILES)
        fx.make_repo(root / "engine", fx.ENGINE_FILES)
        summary = architecture.collect(root / "registry", engine_git=root / "engine")["summary"]
        self.assertEqual(summary["upward_edges"], 3)
        self.assertEqual(summary["upward_imports"], 3)
        self.assertEqual(summary["registry_to_engine_imports"], 5)


if __name__ == "__main__":
    unittest.main()
