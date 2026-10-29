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
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site import pages

# Quarto is what turns a page into a site, and some of what these tests assert --
# that a trend line survives the render, that a fact's Markdown is not obeyed --
# only holds in what Quarto writes. It is in the `dev` environment, which is
# where the suites run.
QUARTO = shutil.which("quarto")


def raw_block_after(page: str, caption: str) -> str:
    """The raw HTML block a page writes under `caption`: Pandoc passes it through as it is."""
    lines = page.splitlines()
    start = next(index for index, line in enumerate(lines) if caption in line)
    opening = lines.index("```{=html}", start)
    closing = lines.index("```", opening + 1)
    return "\n".join(lines[opening + 1 : closing])


def render_with_quarto(body: str, root: Path) -> str:
    """Render one page with Quarto and return the HTML it wrote."""
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "_quarto.yml").write_text("project:\n  type: website\n", encoding="utf-8")
    (docs / "index.qmd").write_text(body, encoding="utf-8")
    subprocess.run([QUARTO, "render", str(docs), "--output-dir", str(root / "out")],
                   check=True, capture_output=True, text=True)
    return (root / "out" / "index.html").read_text(encoding="utf-8")

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
                tickets: list | None = None, versions: list | None = None,
                summary: str = "One sentence about the epic. A second sentence about what it changed.",
                decisions: list | None = None) -> dict:
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
        "summary": summary,
        "prs": [],
        "versions": versions or [],
        "tickets": tickets or [],
        "decisions": decisions or [],
        "test_time": test_time,
        "yardstick": {"before": None, "after": None},
        "catalog": catalog,
        "followups": [],
    }


def catalog_version(version: str, *, built_at: str, analyses: int, stores: int,
                    current: bool = False, counts: list | None = None, changes: dict | None = None,
                    selection: list | None = None) -> dict:
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
        "selection": selection,
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
                        selection=[
                            {"store_id": "OGS-00006", "decision": "listed", "reason": "validated"},
                            {"store_id": "OGS-00027", "decision": "listed",
                             "reason": "Release Status is validated, Access Posture is public"},
                        ],
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
        self.assertIn("observed\\_only", page)  # a fact, so its `_` is not read as emphasis
        self.assertIn("The full eQTLGen cis-eQTL catalogue.", page)
        self.assertIn("2026-10-28: validated and scientifically accepted.", page)

    def test_the_catalog_section_shows_the_decision_and_the_reason_it_recorded(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)

        self.assertIn("Its decision is **listed**, and the reason the catalog gives is:", page)
        self.assertIn("> Release Status is validated, Access Posture is public", page)
        self.assertIn("The version counts **19,250** of its Analyses as listed.", page)

    def test_a_store_with_no_recorded_decision_says_so(self) -> None:
        documents_ = documents(
            stores=envelope("stores", registry="/x", stores=[self.silent]),
            catalog=envelope("catalog", root="/x", current="bbbbbbbbbbbbbbbb", versions=[
                catalog_version("bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", analyses=1, stores=1,
                                current=True, selection=[{"store_id": "OGS-00006", "decision": "listed",
                                                          "reason": "validated"}])]),
        )

        page = pages.render_store_page(documents_, self.silent)

        self.assertIn("records no decision for this Store Release", page)

    def test_the_page_links_the_bundle_at_the_commit_the_render_documents(self) -> None:
        commit = "0d59f151c7618933747a59f825deeef6a51de9e1"

        page = pages.render_store_page(self.documents, self.silent, commit=commit)

        self.assertIn(
            "https://github.com/opengwas/opengwasdb-stores/blob/0d59f151c7618933747a59f825deeef6a51de9e1"
            "/stores/OGS-00027",
            page,
        )
        self.assertNotIn("/tree/dev/", page)

    def test_without_a_commit_the_bundle_is_named_and_not_linked(self) -> None:
        page = pages.render_store_page(self.documents, self.silent)
        self.assertIn("`stores/OGS-00027`", page)
        self.assertNotIn("github.com", page)


def measured_store(store_id: str, **medians: float) -> dict:
    """A measured Store of a yardstick result, with one timed record per measure given."""
    return {
        "store_id": store_id,
        "layout": "dense",
        "coverage": "gwas",
        "status": "measured",
        "measures": {
            name: {"status": "measured", "median_ms": value, "p95_ms": value * 1.2, "reps": 5,
                   "first_read_ms": value, "result_count": 3, "first_result_count": 3}
            for name, value in medians.items()
        },
    }


def yardstick_result(started: str, stores: list[dict]) -> dict:
    """A yardstick result, in the shape `docs/spec/yardstick.md` documents."""
    return {
        "schema": 1,
        "workload_id": "w",
        "workload_sha256": "0" * 64,
        "opengwasdb": {"version": "0.3.0", "revision": "0d59f151c7618933747a59f825deeef6a51de9e1"},
        "ogstores": {"version": "0.1.0"},
        "host": {"name": "host", "cpu_count": 4},
        "load_average": {"before": [0, 0, 0], "after": [0, 0, 0]},
        "started_utc": started,
        "ended_utc": started,
        "duration_s": 60.0,
        "local_utc_offset_minutes": 0,
        "veeam_window": False,
        "repetitions": {"default": 5},
        "stores": stores,
    }


def results_envelope(*results: tuple[str, dict]) -> dict:
    """The records file holding the yardstick results given, oldest first."""
    return envelope(
        "records",
        epics=[],
        invalid={},
        yardstick={"results": [{"name": name, "result": result} for name, result in results], "invalid": {}},
    )


def full_documents() -> dict:
    """Every source available and holding something, for the pages that need more than one of them."""
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
    return documents(
        board=envelope(
            "board", project="opengwas/1", epics=[
                site_epic,
                epic("opengwasdb-stores#214", "Epic: Indexed variant subsets", status="Todo",
                     workstream="Build architecture"),
            ],
            milestones=[milestone("opengwasdb#292", "Milestone: API-ready", checked=6, total=10)],
            items=[],
        ),
        catalog=envelope(
            "catalog", root="/data/opengwasdb/stores/catalog/versions",
            current="bbbbbbbbbbbbbbbb",
            versions=[
                catalog_version("aaaaaaaaaaaaaaaa", built_at="2026-10-23T18:25:29Z", analyses=19250, stores=1,
                                counts=[{"dimension": "coverage", "value": "cis", "n_analyses": 19250}]),
                catalog_version("bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", analyses=31940, stores=9,
                                current=True,
                                counts=[
                                    {"dimension": "store", "value": "OGS-00027", "n_analyses": 19250},
                                    {"dimension": "coverage", "value": "cis", "n_analyses": 19250},
                                    {"dimension": "coverage", "value": "gwas", "n_analyses": 12690},
                                ]),
            ],
        ),
        releases=envelope("releases", repos=[
            {"repo": "opengwasdb", "tags": [{"name": "v0.2.0", "commit": "0" * 40}], "releases": []},
            {"repo": "opengwasdb-stores", "tags": [{"name": "v0.3.0", "commit": "1" * 40}],
             "releases": [{"tag": "v0.3.0", "name": "0.3.0", "published_at": "2026-10-30T09:00:00Z",
                            "draft": False, "prerelease": False,
                            "url": "https://github.com/opengwas/opengwasdb-stores/releases/tag/v0.3.0"}]},
        ]),
        stores=envelope("stores", registry="/data/opengwasdb/stores", stores=[
            store("OGS-00006", "chen-2023",
                  observed={"n_analyses": 1400, "n_variants": 12086463, "n_associations": 50}),
            store("OGS-00027", "eqtlgen-full",
                  observed={"n_analyses": 19250, "n_variants": 8927061, "n_associations": None},
                  description="The full eQTLGen cis-eQTL catalogue.",
                  notes="2026-10-28: validated and scientifically accepted."),
        ]),
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
                    versions=["v0.3.0"],
                )
            ],
            invalid={},
            yardstick={"results": [], "invalid": {}},
        ),
    )


class TestAnUnavailableSource(unittest.TestCase):
    """A source that could not be read is a visible notice, and no page fails over it."""

    def page_sources(self) -> list[tuple[str, str, object]]:
        """Each page and the sources it reads, so each can be taken away in turn."""
        fixtures = full_documents()
        site_epic = fixtures["board"]["epics"][0]
        silent_store = fixtures["stores"]["stores"][1]
        return [
            ("the overview", pages.render_overview, ("board", "catalog", "releases", "stores", "records")),
            ("an epic page", lambda documents: pages.render_epic_page(documents, site_epic),
             ("board", "catalog", "releases", "records")),
            ("a Store page", lambda documents: pages.render_store_page(documents, silent_store),
             ("stores", "catalog")),
            ("the Stores index", pages.render_stores_index, ("stores", "catalog")),
            ("the Epics index", pages.render_epics_index, ("board", "records")),
            ("the trends", pages.render_trends, ("catalog", "records")),
            ("the changelog", pages.render_changelog, ("releases",)),
        ]

    def test_each_page_renders_with_the_notice_for_each_source_it_reads(self) -> None:
        for name, render, sources in self.page_sources():
            for source in sources:
                with self.subTest(page=name, source=source):
                    broken = full_documents()
                    broken[source] = {
                        "source": source,
                        "available": False,
                        "collected_at": COLLECTED,
                        "error": "CollectError: gh api graphql exited 4",
                    }
                    text = render(broken)
                    self.assertIn(f"{pages.TITLES[source]} unavailable", text)
                    self.assertIn("CollectError: gh api graphql exited 4", text)
                    self.assertIn(COLLECTED, text)

    def test_a_file_that_was_never_written_is_a_notice_too(self) -> None:
        for name, render, sources in self.page_sources():
            for source in sources:
                with self.subTest(page=name, source=source):
                    missing = full_documents()
                    missing[source] = None
                    text = render(missing)
                    self.assertIn(f"{pages.TITLES[source]} unavailable", text)
                    self.assertIn("was not written", text)


# A fact a collector may hold: a title, a Next line, a release name, a note. It is
# data, and a page must state it rather than obey the markup it happens to hold.
NASTY = "<b>x</b> *y* [z](w) | q `c` _e_ & r"
# A legal Git tag: it holds a backtick, which closes a one-backtick code span, and
# a `<b>`, which every other escaping rule turns into text (#391 review, item 2).
NASTY_TAG = "v`<b>x</b>`"


def adversarial_documents() -> dict:
    """Every source available, with a fact that holds Markdown and HTML in each."""
    site_epic = epic(
        "opengwasdb-stores#7",
        NASTY,
        status="Active",
        workstream=NASTY,
        next_line=NASTY,
        children=[child("opengwasdb-stores#8", NASTY)],
    )
    return documents(
        board=envelope("board", project="opengwas/1", epics=[site_epic],
                       milestones=[milestone("opengwasdb#292", NASTY, checked=1, total=2)], items=[]),
        catalog=envelope(
            "catalog", root="/data/opengwasdb/stores/catalog/versions", current="bbbbbbbbbbbbbbbb",
            versions=[
                catalog_version("aaaaaaaaaaaaaaaa", built_at="2026-10-23T18:25:29Z", analyses=1, stores=1,
                                counts=[{"dimension": "coverage", "value": NASTY, "n_analyses": 1}]),
                catalog_version("bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", analyses=1, stores=1,
                                current=True,
                                counts=[{"dimension": "coverage", "value": NASTY, "n_analyses": 1}],
                                selection=[{"store_id": "OGS-00009", "decision": "listed", "reason": NASTY}]),
            ],
        ),
        releases=envelope("releases", repos=[
            {"repo": "opengwasdb", "tags": [{"name": "v0.1.0", "commit": "0" * 40}], "releases": []},
            {"repo": "opengwasdb-stores", "tags": [{"name": "v0.1.0", "commit": "1" * 40}],
             "releases": [{"tag": "v0.1.0", "name": NASTY, "published_at": "2026-10-30T09:00:00Z",
                           "draft": False, "prerelease": False, "url": "https://example.org/rel"}]},
        ]),
        stores=envelope("stores", registry="/data/opengwasdb/stores", stores=[
            store("OGS-00009", NASTY, description=NASTY, notes=NASTY,
                  observed={"n_analyses": 1, "n_variants": None}),
        ]),
        records=envelope(
            "records",
            epics=[epic_record("opengwasdb-stores#7", title=NASTY, summary=NASTY,
                               catalog={"before": "aaaaaaaaaaaaaaaa", "after": "bbbbbbbbbbbbbbbb"},
                               test_time={"baseline": {"commit": "e6e829d", "wall_s": 1.0, "suites": None},
                                          "final": {"commit": "a84ecfb", "wall_s": 1.1, "suites": None}},
                               versions=["v0.1.0", NASTY_TAG],
                               decisions=[{"date": "2026-10-21", "text": NASTY, "link": None}])],
            invalid={},
            yardstick={"results": [], "invalid": {}},
        ),
    )


class TestFreeTextIsData(unittest.TestCase):
    """A fact that holds Markdown or HTML is shown as written, not obeyed (#391 review, item 2)."""

    def setUp(self) -> None:
        self.documents = adversarial_documents()
        self.rendered = [
            pages.render_overview(self.documents),
            pages.render_epics_index(self.documents),
            pages.render_epic_page(self.documents, self.documents["board"]["epics"][0]),
            pages.render_stores_index(self.documents),
            pages.render_store_page(self.documents, self.documents["stores"]["stores"][0]),
            pages.render_trends(self.documents),
            pages.render_changelog(self.documents),
        ]

    def test_no_page_obeys_the_markup_a_fact_holds(self) -> None:
        for page in self.rendered:
            markdown = re.sub(r"```\{mermaid\}.*?```", "", page, flags=re.S)  # a graph is Mermaid, not Markdown
            for markup in ("<b>", "[z](w)", "*y*", "`c`", "_e_"):
                self.assertNotIn(markup, markdown, markup)

    def test_the_fact_is_still_readable_where_it_lands(self) -> None:
        overview = self.rendered[0]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", overview)
        self.assertIn("\\[z\\](w)", overview)
        self.assertIn("\\*y\\*", overview)
        self.assertIn("\\| q", overview)  # an unescaped pipe would cut the table cell in two

    def test_a_version_in_a_code_span_is_literal_through_its_own_backtick(self) -> None:
        """A tag is free text: its backtick must not close the code span it is written in."""
        epic_page = self.rendered[2]
        self.assertIn("<code>v&#96;&lt;b&gt;x&lt;/b&gt;&#96;</code>", epic_page)
        self.assertNotIn(f"`{NASTY_TAG}`", epic_page)


@unittest.skipUnless(QUARTO, "Quarto is not on PATH; the render assertions need it")
class TestWhatQuartoWrites(unittest.TestCase):
    """The two things that only fail in the rendered HTML (#391 review, items 2 and 6)."""

    def test_each_headline_trend_line_survives_the_render(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-site-render-") as tmp:
            html = render_with_quarto(pages.render_overview(full_documents()), Path(tmp))

        self.assertEqual(html.count("<polyline"), 2, "the Analyses and Stores trend lines")
        self.assertGreaterEqual(html.count("<circle"), 4, "a point per version per trend")

    def test_a_fact_holding_markup_is_rendered_literally(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-site-render-") as tmp:
            html = render_with_quarto(pages.render_overview(adversarial_documents()), Path(tmp))

        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn('href="w"', html)

    def test_a_version_holding_a_backtick_and_a_tag_renders_as_text(self) -> None:
        documents = adversarial_documents()
        with tempfile.TemporaryDirectory(prefix="project-site-render-") as tmp:
            html = render_with_quarto(
                pages.render_epic_page(documents, documents["board"]["epics"][0]), Path(tmp)
            )

        self.assertIn("v`&lt;b&gt;x&lt;/b&gt;`", html)
        self.assertNotIn("<b>x</b>", html)


class TestNoInventedCurrentVersion(unittest.TestCase):
    """A catalog that names no current version is shown as naming none (#391 review, item 1)."""

    def documents(self) -> dict:
        return documents(catalog=envelope(
            "catalog", root="/data/opengwasdb/stores/catalog/versions", current=None,
            versions=[
                catalog_version("aaaaaaaaaaaaaaaa", built_at="2026-10-23T18:25:29Z", analyses=19250, stores=1),
                catalog_version("bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", analyses=31940, stores=9),
            ],
        ))

    def test_the_overview_says_no_version_is_current(self) -> None:
        page = pages.render_overview(self.documents())

        self.assertIn("**No current version**", page)
        self.assertNotIn("The current version is", page)

    def test_a_store_page_says_no_version_has_decided_on_it(self) -> None:
        documents_ = self.documents()
        documents_["stores"] = envelope("stores", registry="/x", stores=[store("OGS-00009", "ukb-b-full")])

        page = pages.render_store_page(documents_, documents_["stores"]["stores"][0])

        self.assertIn("**No current version**", page)
        self.assertNotIn("The current catalog version is", page)


class TestRefusedFiles(unittest.TestCase):
    """A refused file is named with its errors, never passed off as an absent one (#391 review, item 3)."""

    def setUp(self) -> None:
        self.documents = full_documents()
        self.documents["records"] = envelope(
            "records",
            epics=full_documents()["records"]["epics"],
            invalid={"opengwasdb-stores-313.yaml": ["status 'done' is not one of active, finished"]},
            yardstick={"results": [], "invalid": {"20261020T100000Z-0.3.0.json": ["result schema must be 1, got 2"]}},
        )

    def test_an_unreadable_epic_record_is_named_on_the_epic_and_the_index(self) -> None:
        epic = self.documents["board"]["epics"][0]
        for page in (pages.render_epic_page(self.documents, epic), pages.render_epics_index(self.documents)):
            self.assertIn("opengwasdb-stores-313.yaml", page)
            self.assertIn("status 'done' is not one of active, finished", page)

    def test_a_refused_yardstick_result_is_not_reported_as_no_results(self) -> None:
        for page in (pages.render_overview(self.documents), pages.render_trends(self.documents)):
            self.assertIn("20261020T100000Z-0.3.0.json", page)
            self.assertIn("result schema must be 1, got 2", page)
            self.assertNotIn("No yardstick results yet", page)
            self.assertIn("No yardstick result could be read", page)


class TestHeadlineYardstickTrends(unittest.TestCase):
    """Each key figure of the latest result is charted across the results (#391 review, item 7)."""

    def setUp(self) -> None:
        self.documents = full_documents()
        self.documents["records"] = results_envelope(
            ("20261020T100000Z-0.3.0.json", yardstick_result("2026-10-20T10:00:00Z",
                                                             [measured_store("OGS-00009", phewas_rsid=40.0)])),
            ("20261027T100000Z-0.3.0.json", yardstick_result("2026-10-27T10:00:00Z",
                                                             [measured_store("OGS-00009", phewas_rsid=30.0)])),
        )

    def test_a_key_figure_has_a_point_per_result(self) -> None:
        page = pages.render_overview(self.documents)

        block = raw_block_after(page, "`phewas_rsid` on OGS-00009")
        self.assertEqual(block.count("<circle"), 2)
        self.assertIn("40 ms → 30 ms over 2 results", page)
        self.assertNotIn("No yardstick results yet", page)

    def test_the_results_are_listed_with_what_each_measured(self) -> None:
        page = pages.render_trends(self.documents)

        self.assertIn("| `20261027T100000Z-0.3.0` | 2026-10-27T10:00:00Z |", page)
        self.assertIn("| OGS-00009 | `phewas_rsid` |", page)


class TestReleaseNotesByRepository(unittest.TestCase):
    """A tag is unique only within its repository (#391 review, item 8)."""

    def test_a_version_is_matched_inside_the_epics_own_repository(self) -> None:
        documents_ = full_documents()
        documents_["records"]["epics"][0]["versions"] = ["v0.1.0"]
        own = "opengwasdb-stores"
        documents_["releases"] = envelope("releases", repos=[
            {"repo": "opengwasdb", "tags": [{"name": "v0.1.0", "commit": "0" * 40}],
             "releases": [{"tag": "v0.1.0", "name": "the engine's notes", "published_at": "2026-10-29T09:00:00Z",
                           "draft": False, "prerelease": False, "url": "https://example.org/engine"}]},
            {"repo": own, "tags": [{"name": "v0.1.0", "commit": "1" * 40}],
             "releases": [{"tag": "v0.1.0", "name": "the registry's notes", "published_at": "2026-10-30T09:00:00Z",
                           "draft": False, "prerelease": False, "url": "https://example.org/registry"}]},
        ])

        page = pages.render_epic_page(documents_, documents_["board"]["epics"][0])

        self.assertIn("the registry's notes", page)
        self.assertNotIn("the engine's notes", page)


class TestEpicPurpose(unittest.TestCase):
    """The epic's own first paragraph comes first, then the record (#391 review, item 9)."""

    def test_the_board_purpose_is_shown_before_the_records_summary(self) -> None:
        documents_ = full_documents()
        epic = documents_["board"]["epics"][0]
        epic["purpose"] = "The site shows where the project stands, in one place."

        page = pages.render_epic_page(documents_, epic)

        section = page.split("## Purpose", 1)[1].split("\n##", 1)[0]
        self.assertIn("The site shows where the project stands, in one place.", section)
        self.assertLess(
            section.index("The site shows where the project stands, in one place."),
            section.index("One sentence about the epic."),
        )


class TestEpicsIndex(unittest.TestCase):
    """The Epics section's index: every epic on the board, and whether it has a record yet."""

    def test_every_epic_is_listed_with_its_status_and_whether_a_record_exists(self) -> None:
        page = pages.render_epics_index(full_documents())
        self.assertIn("[Epic: The project site](epics/opengwasdb-stores-385.html)", page)
        self.assertIn("[Epic: Indexed variant subsets](epics/opengwasdb-stores-214.html)", page)
        self.assertIn("The board holds **2** Epics: 1 Active, 1 Todo.", page)
        self.assertIn("none yet", page)


class TestTrends(unittest.TestCase):
    """What moved from version to version: the catalog, the yardstick, and the test time per epic."""

    def test_the_catalog_table_lists_every_version_with_its_totals(self) -> None:
        page = pages.render_trends(full_documents())
        self.assertIn("| `bbbbbbbbbbbbbbbb` | 2026-10-28T22:57:35Z | 31,940 | 9 |", page)
        self.assertIn("| `aaaaaaaaaaaaaaaa` | 2026-10-23T18:25:29Z | 19,250 | 1 |", page)

    def test_a_dimension_table_says_when_a_version_has_no_row_rather_than_zero(self) -> None:
        page = pages.render_trends(full_documents())
        self.assertIn("| cis | 19,250 | 19,250 |", page)
        self.assertIn("| gwas | — | 12,690 |", page)

    def test_multiple_versions_are_charted_with_a_trend_of_the_listed_analyses(self) -> None:
        page = pages.render_trends(full_documents())
        self.assertIn("| Value | `aaaaaaaa` | `bbbbbbbb` (current) |", page)
        self.assertIn("19,250 → 31,940 over 2 versions", page)

    def test_no_yardstick_result_is_an_empty_state_not_a_blank(self) -> None:
        page = pages.render_trends(full_documents())
        self.assertIn("**No yardstick results yet.**", page)

    def test_the_test_time_of_each_epic_that_measured_one(self) -> None:
        page = pages.render_trends(full_documents())
        self.assertIn("| [opengwasdb-stores#385](epics/opengwasdb-stores-385.html) | 158.4 s at `e6e829d`"
                      " | 162.05 s at `a84ecfb` | +2.3% on the baseline |", page)


class TestChangelog(unittest.TestCase):
    """Both repositories' Releases, newest first, and the empty state before the cutover."""

    def test_a_release_is_listed_newest_first_with_its_notes_linked(self) -> None:
        page = pages.render_changelog(full_documents())
        self.assertIn("| `v0.3.0` | [0.3.0](https://github.com/opengwas/opengwasdb-stores/releases/tag/v0.3.0)"
                      " | 2026-10-30T09:00:00Z | — |", page)

    def test_an_empty_repository_says_versions_start_at_the_cutover(self) -> None:
        page = pages.render_changelog(full_documents())
        self.assertIn("## opengwasdb", page)
        self.assertIn("**No Release yet.** Versions start at the cutover", page)
        self.assertIn("Its tags so far are `v0.2.0`; a tag is not a Release.", page)


class TestGenerate(unittest.TestCase):
    """The file boundary: what the pre-render step puts in the render tree, and what it leaves behind."""

    def setUp(self) -> None:
        opened = tempfile.TemporaryDirectory()
        self.addCleanup(opened.cleanup)
        self.docs = Path(opened.name) / "docs"
        self.docs.mkdir()
        self.data = Path(opened.name) / "_data"
        self.data.mkdir()
        for source, document in full_documents().items():
            (self.data / f"{source}.json").write_text(json.dumps(document))

    def test_every_section_page_and_one_page_per_epic_and_per_store_is_written(self) -> None:
        written = pages.generate(docs=self.docs, data_dir=self.data)
        self.assertEqual(
            sorted(path.relative_to(self.docs).as_posix() for path in written),
            [
                "changelog.qmd",
                "epics.qmd",
                "epics/opengwasdb-stores-214.qmd",
                "epics/opengwasdb-stores-385.qmd",
                "index.qmd",
                "stores.qmd",
                "stores/OGS-00006.qmd",
                "stores/OGS-00027.qmd",
                "trends.qmd",
            ],
        )
        overview = (self.docs / "index.qmd").read_text()
        self.assertTrue(overview.startswith('---\ntitle: "Overview"\n---\n'))
        self.assertIn("6 of 10", overview)
        self.assertIn('title: "OGS-00027 — eqtlgen-full"', (self.docs / "stores" / "OGS-00027.qmd").read_text())

    def test_every_relative_link_resolves_to_a_page_the_generator_wrote(self) -> None:
        written = pages.generate(docs=self.docs, data_dir=self.data)
        written_pages = {path.relative_to(self.docs).with_suffix(".html").as_posix() for path in written}

        for path in written:
            for target in re.findall(r"\]\(([^)#:]+?\.html)\)", path.read_text()):
                resolved = (path.parent / target).resolve().relative_to(self.docs.resolve()).as_posix()
                self.assertIn(resolved, written_pages, f"{path.relative_to(self.docs)} links {target}")
        self.assertIn("[OGS-00027](stores/OGS-00027.html)", (self.docs / "stores.qmd").read_text())

    def test_a_page_for_something_the_data_no_longer_holds_is_removed(self) -> None:
        stale = self.docs / "epics" / "opengwasdb-stores-999.qmd"
        stale.parent.mkdir(parents=True)
        stale.write_text("gone")
        pages.generate(docs=self.docs, data_dir=self.data)
        self.assertFalse(stale.exists())

    def test_no_data_directory_still_writes_every_section_page_with_the_notices(self) -> None:
        written = pages.generate(docs=self.docs, data_dir=Path(self.docs.name) / "never-collected")
        self.assertEqual(len(written), 5)
        overview = (self.docs / "index.qmd").read_text()
        self.assertIn("Board data unavailable", overview)
        self.assertIn("Catalog data unavailable", overview)


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
        analyses = raw_block_after(page, "Analyses, per catalog version")
        self.assertEqual(analyses.count("<circle"), 2)
        stores = raw_block_after(page, "Stores, per catalog version")
        self.assertEqual(stores.count("<circle"), 2)
        self.assertIn("| Analyses | 31,940 | 19,250 → 31,940 over 2 versions |", page)
        self.assertIn("| Stores | 9 | 1 → 9 over 2 versions |", page)

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
