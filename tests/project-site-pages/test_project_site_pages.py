#!/usr/bin/env python3
"""The project site's pages: the overview, epic and Store pages, trends, changelog (#391).

Every page is generated from the five JSON files `pixi run site-data` writes, in
the shapes `docs/spec/project-site.md` documents, so the fixtures here are
literals in that shape and nothing is read from a real data directory.

Run from the repository root:
    python3 tests/project-site-pages/test_project_site_pages.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_site import pages

COLLECTED = "2026-10-29T12:00:00Z"


def envelope(source: str, **payload) -> dict:
    """One collector file: the four keys every file starts with, then the source's own."""
    return {"source": source, "available": True, "collected_at": COLLECTED, "error": None, **payload}


def epic(key: str, title: str, *, status: str = "Todo", workstream: str | None = "Engineering",
         next_line: str | None = None, state: str = "OPEN", children: list | None = None) -> dict:
    """A board row for an Epic. `key` is `repo#n`, as the collector writes it."""
    repo, number = key.split("#")
    return {
        "key": key,
        "title": title,
        "url": f"https://github.com/opengwas/{repo}/issues/{number}",
        "state": state,
        "status": status,
        "workstream": workstream,
        "next": next_line,
        "priority": None,
        "sub_issues": {"total": len(children or []), "completed": 0},
        "children": children or [],
    }


def milestone(key: str, title: str, checked: int, total: int) -> dict:
    """A board row for a Milestone: an epic's fields without `children`, plus its checklist lines."""
    repo, number = key.split("#")
    return {
        "key": key,
        "title": title,
        "url": f"https://github.com/opengwas/{repo}/issues/{number}",
        "state": "OPEN",
        "status": "Active",
        "workstream": None,
        "next": None,
        "priority": None,
        "sub_issues": {"total": 0, "completed": 0},
        "lines": [
            {"checked": index < checked, "text": f"Line {index + 1}", "refs": []}
            for index in range(total)
        ],
    }


def catalog_version(version: str, *, built_at: str, analyses: int, stores: int,
                    current: bool = False, counts: list | None = None, changes: dict | None = None) -> dict:
    """One Analysis Catalog version, as the catalog collector writes it."""
    return {
        "version": version,
        "current": current,
        "catalog_meta": {
            "built_at": built_at,
            "registry_commit": "0" * 7,
            "builder_version": "1",
            "serving_policy": "[]",
        },
        "counts": counts or [],
        "totals": {"analyses": analyses, "stores": stores},
        "changes": changes,
    }


def documents(**overrides) -> dict:
    """The five collector files, available and holding nothing, unless a test overrides one."""
    everything = {
        "board": envelope("board", project="opengwas/1", epics=[], milestones=[], items=[]),
        "catalog": envelope("catalog", root="/data/opengwasdb/stores/catalog/versions", current=None, versions=[]),
        "releases": envelope("releases", repos=[
            {"repo": "opengwasdb", "tags": [], "releases": []},
            {"repo": "opengwasdb-stores", "tags": [], "releases": []},
        ]),
        "stores": envelope("stores", registry="/data/opengwasdb/stores", stores=[]),
        "records": envelope("records", epics=[], invalid={}, yardstick={"results": [], "invalid": {}}),
    }
    everything.update(overrides)
    return everything


class TestOverview(unittest.TestCase):
    """The home page: milestone progress, each workstream's Active epics, and trends with the data below them."""

    def setUp(self) -> None:
        self.documents = documents(
            board=envelope(
                "board",
                project="opengwas/1",
                epics=[
                    epic("opengwasdb-stores#313", "Epic: Analysis catalog across all Stores",
                         status="Active", workstream="Interface",
                         next_line="9/10 merged on PR #353 · implementing #359"),
                    epic("opengwasdb-stores#214", "Epic: Indexed variant subsets",
                         status="Todo", workstream="Build architecture",
                         next_line="Build the hm3 subset for OGS-00009"),
                ],
                milestones=[milestone("opengwasdb#292", "Milestone: API-ready", checked=6, total=10)],
                items=[],
            ),
            catalog=envelope(
                "catalog",
                root="/data/opengwasdb/stores/catalog/versions",
                current="bbbbbbbbbbbbbbbb",
                versions=[
                    catalog_version("aaaaaaaaaaaaaaaa", built_at="2026-10-23T18:25:29Z", analyses=19250, stores=1),
                    catalog_version("bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z",
                                    analyses=31940, stores=9, current=True),
                ],
            ),
            releases=envelope("releases", repos=[
                {"repo": "opengwasdb", "tags": [{"name": "v0.2.0", "commit": "0" * 40}], "releases": []},
                {"repo": "opengwasdb-stores", "tags": [], "releases": []},
            ]),
        )

    def test_a_milestone_says_how_many_lines_are_met_and_which_are_not(self) -> None:
        page = pages.render_overview(self.documents)
        self.assertIn("6 of 10", page)
        self.assertIn("- Line 7", page)
        self.assertNotIn("- Line 6", page)

    def test_a_workstream_lists_its_active_epics_with_the_next_line_and_skips_the_rest(self) -> None:
        page = pages.render_overview(self.documents)
        self.assertIn("### Interface", page)
        self.assertIn("9/10 merged on PR #353 · implementing #359", page)
        self.assertNotIn("opengwasdb-stores#214", page)
        self.assertNotIn("Build the hm3 subset for OGS-00009", page)

    def test_the_catalog_trend_has_a_point_for_each_version(self) -> None:
        page = pages.render_overview(self.documents)
        analyses = next(line for line in page.splitlines() if line.startswith("| Analyses "))
        self.assertEqual(analyses.count("<circle"), 2)
        self.assertIn("31,940", analyses)
        stores = next(line for line in page.splitlines() if line.startswith("| Stores "))
        self.assertEqual(stores.count("<circle"), 2)
        self.assertIn("9", stores)

    def test_the_latest_tag_per_repository_or_untagged(self) -> None:
        page = pages.render_overview(self.documents)
        self.assertIn("| opengwasdb | `v0.2.0` |", page)
        self.assertIn("| opengwasdb-stores | untagged |", page)

    def test_each_source_says_when_it_was_collected(self) -> None:
        page = pages.render_overview(self.documents)
        self.assertIn("| Board data | 2026-10-29T12:00:00Z | available |", page)
        self.assertIn("| Catalog data | 2026-10-29T12:00:00Z | available |", page)


if __name__ == "__main__":
    unittest.main()
