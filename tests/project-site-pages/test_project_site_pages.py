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


def child(key: str, title: str, *, state: str = "OPEN", blocked_by: list | None = None) -> dict:
    """One of an epic's sub-issues, with the blocked-by edges the board reports for it."""
    repo, number = key.split("#")
    return {
        "key": key,
        "title": title,
        "url": f"https://github.com/opengwas/{repo}/issues/{number}",
        "state": state,
        "blocked_by": blocked_by or [],
    }


def epic_record(key: str, *, title: str, catalog: dict, test_time: dict,
                tickets: list | None = None, versions: list | None = None) -> dict:
    """An epic record, in the shape `docs/spec/project-site.md` documents."""
    repo, number = key.split("#")
    return {
        "repo": repo,
        "number": int(number),
        "title": title,
        "status": "active",
        "workstream": "Engineering",
        "started": "2026-10-21",
        "finished": None,
        "summary": "One sentence about the epic. A second sentence about what it changed.",
        "prs": [],
        "versions": versions or [],
        "tickets": tickets or [],
        "decisions": [],
        "test_time": test_time,
        "yardstick": {"before": None, "after": None},
        "catalog": catalog,
        "followups": [],
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


class TestEpicPage(unittest.TestCase):
    """One page per epic: its tickets as a graph, and what it changed where a record exists."""

    def setUp(self) -> None:
        site_epic = epic(
            "opengwasdb-stores#385",
            "Epic: The project site",
            status="Active",
            workstream="Engineering",
            next_line="4 of 8 merged",
            children=[
                child("opengwasdb-stores#388", "One Quarto site", state="CLOSED"),
                child("opengwasdb-stores#391", "The site's pages", state="OPEN",
                      blocked_by=[{"key": "opengwasdb-stores#388", "state": "CLOSED"}]),
            ],
        )
        self.documents = documents(
            board=envelope("board", project="opengwas/1", epics=[site_epic], milestones=[], items=[]),
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
            records=envelope(
                "records",
                epics=[
                    epic_record(
                        "opengwasdb-stores#385",
                        title="Epic: The project site",
                        catalog={"before": "aaaaaaaaaaaaaaaa", "after": "bbbbbbbbbbbbbbbb"},
                        test_time={
                            "baseline": {"commit": "e6e829d", "wall_s": 158.4, "suites": None},
                            "final": {"commit": "a84ecfb", "wall_s": 162.05, "suites": None},
                        },
                        tickets=[{"key": "opengwasdb-stores#388", "title": "One Quarto site",
                                  "pr": "opengwasdb-stores#390", "merge": "7832deb"}],
                        versions=["v0.3.0"],
                    )
                ],
                invalid={},
                yardstick={"results": [], "invalid": {}},
            ),
            releases=envelope("releases", repos=[
                {"repo": "opengwasdb", "tags": [], "releases": []},
                {"repo": "opengwasdb-stores", "tags": [{"name": "v0.3.0", "commit": "0" * 40}],
                 "releases": [{"tag": "v0.3.0", "name": "0.3.0 — the project site",
                                "published_at": "2026-10-30T09:00:00Z", "draft": False,
                                "prerelease": False, "url": "https://github.com/opengwas/opengwasdb-stores/releases/tag/v0.3.0"}]},
            ]),
        )
        self.page = pages.render_epic_page(self.documents, site_epic)

    def test_the_ticket_graph_has_a_node_per_sub_issue_styled_by_state(self) -> None:
        self.assertIn('n0["opengwasdb-stores#35;388 — One Quarto site"]:::closed', self.page)
        self.assertIn('n1["opengwasdb-stores#35;391 — The site\'s pages"]:::open', self.page)
        self.assertIn("classDef closed", self.page)

    def test_a_blocked_by_edge_is_an_arrow_from_the_blocker_to_the_blocked(self) -> None:
        self.assertIn("n0 --> n1", self.page)

    def test_the_record_adds_the_catalog_and_the_test_time_before_and_after(self) -> None:
        self.assertIn("| Catalog Analyses | 19,250 (`aaaaaaaaaaaaaaaa`) | 31,940 (`bbbbbbbbbbbbbbbb`) |", self.page)
        self.assertIn("| Catalog Stores | 1 | 9 |", self.page)
        self.assertIn("158.4 s at `e6e829d`", self.page)
        self.assertIn("162.05 s at `a84ecfb`", self.page)

    def test_a_version_the_record_names_is_shown_with_its_release_notes(self) -> None:
        self.assertIn("v0.3.0", self.page)
        self.assertIn("0.3.0 — the project site", self.page)
        self.assertIn("2026-10-30T09:00:00Z", self.page)

    def test_an_epic_without_a_record_shows_only_the_board_facts(self) -> None:
        bare = epic("opengwasdb-stores#214", "Epic: Indexed variant subsets", status="Todo",
                    workstream="Build architecture", children=[])
        documents_ = documents(board=envelope("board", project="opengwas/1", epics=[bare], milestones=[], items=[]))
        page = pages.render_epic_page(documents_, bare)
        self.assertIn("No epic record", page)
        self.assertIn("Build architecture", page)
        self.assertNotIn("Catalog Analyses", page)


def store(store_id: str, label: str, **overrides) -> dict:
    """One registered Store Release, as the stores collector writes it."""
    row = {
        "store_id": store_id,
        "label": label,
        "status": "validated",
        "trial": False,
        "layout": "ragged",
        "completion_state": "observed_only",
        "association_coverage": "cis",
        "access_posture": "public",
        "derived_from": None,
        "superseded_by": None,
        "created_at": "2026-10-27T19:28:05Z",
        "accepted_at": "2026-10-27T19:42:54Z",
        "validated_at": "2026-10-27T20:13:36Z",
        "validation_status": "passed",
        "observed": None,
        "description": None,
        "notes": None,
        "problems": [],
    }
    row.update(overrides)
    return row


class TestStorePages(unittest.TestCase):
    """One page per registered Store: its lifecycle, its counts, its catalog presence and its notes."""

    def setUp(self) -> None:
        self.reported = store(
            "OGS-00006",
            "chen-2023",
            observed={"n_analyses": 1400, "n_variants": 12086463, "n_associations": 50},
        )
        self.silent = store(
            "OGS-00027",
            "eqtlgen-full",
            observed={"n_analyses": 19250, "n_variants": 8927061, "n_associations": None},
            description="The full eQTLGen cis-eQTL catalogue.",
            notes="2026-10-28: validated and scientifically accepted.",
        )
        self.documents = documents(
            stores=envelope("stores", registry="/data/opengwasdb/stores",
                            stores=[self.reported, self.silent]),
            catalog=envelope(
                "catalog",
                root="/data/opengwasdb/stores/catalog/versions",
                current="bbbbbbbbbbbbbbbb",
                versions=[
                    catalog_version(
                        "bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", analyses=20650, stores=2,
                        current=True,
                        counts=[{"dimension": "store", "value": "OGS-00006", "n_analyses": 1400},
                                {"dimension": "store", "value": "OGS-00027", "n_analyses": 19250}],
                    )
                ],
            ),
        )

    def test_a_count_the_validation_record_left_null_says_not_reported(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)
        self.assertIn("| Associations (`n_associations`) | _not reported_ |", page)
        self.assertIn("| Analyses (`n_analyses`) | 19,250 |", page)

    def test_no_total_includes_a_count_a_store_did_not_report(self) -> None:
        index = pages.render_stores_index(self.documents)
        total = next(line for line in index.splitlines() if line.startswith("| **Total**"))
        self.assertIn("unknown (1 of 2 Stores report it)", total)
        self.assertIn("| 20,650 |", total)

    def test_the_page_shows_the_lifecycle_the_layout_and_the_release_notes(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)
        self.assertIn("2026-10-27T20:13:36Z", page)
        self.assertIn("observed_only", page)
        self.assertIn("The full eQTLGen cis-eQTL catalogue.", page)
        self.assertIn("2026-10-28: validated and scientifically accepted.", page)

    def test_the_catalog_section_says_the_store_is_listed_and_that_a_decision_is_not_collected(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)
        self.assertIn("19,250", page)
        self.assertIn("`selection`", page)

    def test_the_page_links_to_the_bundle_on_github(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)
        self.assertIn(f"{pages.BUNDLES}/OGS-00027", page)


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
