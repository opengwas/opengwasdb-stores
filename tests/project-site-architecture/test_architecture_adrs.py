#!/usr/bin/env python3
"""Where an ADR is placed on the architecture (#418).

An ADR is placed by the code it cites (a path, or a dotted module name, resolved
through the claims) and by a component's name in its title. A component's name
that appears only in the body, however often, places nothing. The fixtures are in
`architecture_fixtures.py`, each ADR commented with what it is there to show.

Run from the repository root:
    python3 tests/project-site-architecture/test_architecture_adrs.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site.collect import architecture


def collect(test: unittest.TestCase) -> dict:
    root = Path(tempfile.mkdtemp(prefix="arch-"))
    test.addCleanup(__import__("shutil").rmtree, root, True)
    fx.make_repo(root / "registry", {**fx.REGISTRY_FILES, **fx.REGISTRY_ADRS})
    fx.make_repo(root / "engine", {**fx.ENGINE_FILES, **fx.ENGINE_ADRS})
    return architecture.collect(root / "registry", engine_git=root / "engine")


class AdrPlacementTests(unittest.TestCase):
    def test_adrs_are_placed_by_cited_code_and_by_names_in_titles_only(self) -> None:
        placed = {(a["repo"], a["number"]): a["components"] for a in collect(self)["adrs"]}
        self.assertEqual(
            placed,
            {
                ("registry", "0001"): {"gen": "code"},
                ("registry", "0002"): {"gen": "code", "store": "code"},
                ("registry", "0003"): {"ogs": "title"},
                ("registry", "0004"): {},
                ("registry", "0005"): {"store": "code"},
                ("registry", "0006"): {"ogs": "title"},
                ("engine", "0001"): {"query": "title"},
                ("engine", "0002"): {"check": "code", "cli": "code"},
                ("engine", "0003"): {},
                ("engine", "0004"): {},
            },
        )

    def test_the_summary_counts_adrs_placed_of_all_and_lists_the_rest(self) -> None:
        result = collect(self)
        self.assertEqual((result["summary"]["adrs_placed"], result["summary"]["adrs_total"]), (7, 10))
        unplaced = [(a["repo"], a["number"]) for a in result["adrs"] if not a["components"]]
        self.assertEqual(unplaced, [("registry", "0004"), ("engine", "0003"), ("engine", "0004")])

    def test_an_adr_carries_its_title_status_and_link(self) -> None:
        by_key = {(a["repo"], a["number"]): a for a in collect(self)["adrs"]}
        cites = by_key[("engine", "0002")]
        self.assertEqual(cites["title"], "0002. Layers")
        self.assertEqual(cites["status"], "superseded")
        self.assertEqual(cites["url"], "https://github.com/opengwas/opengwasdb/blob/main/docs/adr/0002-cites.md")
        self.assertEqual(by_key[("registry", "0001")]["status"], "current")


if __name__ == "__main__":
    unittest.main()
