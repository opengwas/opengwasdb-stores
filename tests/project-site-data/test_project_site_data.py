#!/usr/bin/env python3
"""The project site's data: the epic record and the collectors (#389, epic #385).

Each test is at a seam named in the spec, `docs/spec/project-site.md`. Expected
values are literals. Fixtures are built here or read from `fixtures/`; nothing
is read from `/data/opengwasdb/stores` and nothing is written outside a
temporary directory.

Run from the repository root:
    python3 tests/project-site-data/test_project_site_data.py
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import sqlite3
import tempfile
import unittest

import yaml
from pathlib import Path

from project_site import records
from project_site.collect import board, catalog, common, releases, stores

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def minimal_record() -> dict:
    return {
        "repo": "opengwasdb-stores",
        "number": 313,
        "title": "Analysis catalog across all Stores",
        "status": "finished",
        "workstream": "Analyses",
        "started": datetime.date(2026, 10, 21),
        "finished": datetime.date(2026, 10, 29),
        "summary": "The catalog lists every validated Store. It was first published on 28 October.",
        "prs": ["opengwasdb-stores#353"],
        "versions": [],
        "tickets": [
            {"key": "opengwasdb-stores#319", "title": "analysis_id never contains ':'",
             "pr": None, "merge": "7832deb"},
        ],
        "decisions": [
            {"date": datetime.date(2026, 10, 21), "text": "Integration branch cut from dev.", "link": None},
        ],
        "test_time": {
            "baseline": {"commit": "e6e829d", "wall_s": 158.4, "suites": None},
            "final": {"commit": "a84ecfb", "wall_s": 162.05, "suites": None},
        },
        "yardstick": {"before": None, "after": None},
        "catalog": {"before": None, "after": "1b3a155d449bb84a"},
        "followups": [{"key": "opengwasdb#294", "text": "builders print n_associations"}],
    }


class TestEpicRecordValidator(unittest.TestCase):
    def test_a_minimal_valid_record_has_no_errors(self) -> None:
        self.assertEqual(records.validate_epic(minimal_record()), [])

    def test_an_all_unknown_record_is_valid(self) -> None:
        record = {
            "repo": "opengwasdb", "number": 7, "title": "T", "status": "active",
            "workstream": None, "started": None, "finished": None, "summary": None,
            "prs": [], "versions": [], "tickets": [], "decisions": [],
            "test_time": {"baseline": None, "final": None},
            "yardstick": {"before": None, "after": None},
            "catalog": {"before": None, "after": None}, "followups": [],
        }
        self.assertEqual(records.validate_epic(record), [])

    def test_a_missing_required_field_is_refused_by_name(self) -> None:
        record = minimal_record()
        del record["summary"]
        self.assertEqual(records.validate_epic(record), ["summary is required (write null when unknown)"])

    def test_an_unknown_status_is_refused(self) -> None:
        record = minimal_record()
        record["status"] = "done"
        self.assertEqual(
            records.validate_epic(record),
            ["status 'done' is not one of active, finished"],
        )

    def test_a_finished_epic_needs_a_finish_date_and_an_active_one_has_none(self) -> None:
        record = minimal_record()
        record["finished"] = None
        self.assertEqual(records.validate_epic(record), ["a finished epic needs a finished date"])
        record = minimal_record()
        record["status"] = "active"
        self.assertEqual(records.validate_epic(record), ["an active epic has finished: null"])

    def test_a_bad_repo_number_key_is_refused(self) -> None:
        for bad in ("opengwasdb-stores#0", "opengwasdb-stores", "#12", "stores#1a", 12):
            record = minimal_record()
            record["prs"] = [bad]
            self.assertEqual(
                records.validate_epic(record),
                [f"prs[0] {bad!r} is not a repo#n key"],
                bad,
            )

    def test_an_estimate_where_null_is_required_is_refused(self) -> None:
        for field, value in (("started", "~2026-10-20"), ("started", "unknown"), ("finished", "about 29 Oct")):
            record = minimal_record()
            record[field] = value
            errors = records.validate_epic(record)
            self.assertEqual(len(errors), 1, (field, value))
            self.assertIn(f"{field} {value!r} is not an ISO date", errors[0])
        record = minimal_record()
        record["test_time"]["final"]["wall_s"] = "about 160"
        self.assertEqual(
            records.validate_epic(record),
            ["test_time.final.wall_s 'about 160' is not a number (write null when unknown)"],
        )
        record = minimal_record()
        record["summary"] = "TBD"
        self.assertEqual(
            records.validate_epic(record),
            ["summary 'TBD' is a placeholder, not a value (write null when unknown)"],
        )

    def test_a_summary_of_one_or_five_sentences_is_refused(self) -> None:
        record = minimal_record()
        record["summary"] = "One sentence only."
        self.assertEqual(records.validate_epic(record), ["summary has 1 sentences; write 2 to 4"])
        record["summary"] = "A one. B two. C three. D four. E five."
        self.assertEqual(records.validate_epic(record), ["summary has 5 sentences; write 2 to 4"])

    def test_unknown_fields_and_bad_references_are_refused(self) -> None:
        record = minimal_record()
        record["owner"] = "x"
        record["catalog"]["after"] = "1b3a155"
        record["yardstick"]["before"] = "../secrets.json"
        record["tickets"][0]["merge"] = "abc"
        record["decisions"][0]["link"] = "not a url"
        self.assertEqual(
            sorted(records.validate_epic(record)),
            sorted([
                "unknown field 'owner'",
                "catalog.after '1b3a155' is not a 16-hex catalog version",
                "yardstick.before '../secrets.json' is not a relative path under docs/_yardstick",
                "tickets[0].merge 'abc' is not a 7 to 40 hex commit",
                "decisions[0].link 'not a url' is not an http(s) URL",
            ]),
        )

    def test_the_file_name_must_match_the_record(self) -> None:
        self.assertEqual(records.validate_epic(minimal_record(), "opengwasdb-stores-313.yaml"), [])
        self.assertEqual(
            records.validate_epic(minimal_record(), "opengwasdb-stores-314.yaml"),
            ["file name 'opengwasdb-stores-314.yaml' should be 'opengwasdb-stores-313.yaml'"],
        )

    def test_a_record_is_not_changed_by_validation(self) -> None:
        record = minimal_record()
        before = copy.deepcopy(record)
        records.validate_epic(record)
        self.assertEqual(record, before)


class TestBoardCollector(unittest.TestCase):
    """`board.parse` over a response recorded once from the real API (fixtures/board-graphql.json)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = board.parse(json.loads((FIXTURES / "board-graphql.json").read_text()))
        cls.epics = {e["key"]: e for e in cls.data["epics"]}
        cls.milestones = {m["key"]: m for m in cls.data["milestones"]}

    def test_every_project_item_is_an_epic_a_milestone_or_other(self) -> None:
        self.assertEqual(
            (len(self.data["epics"]), len(self.data["milestones"]), len(self.data["items"])),
            (17, 2, 2),
        )
        self.assertEqual(sorted(i["key"] for i in self.data["items"]), ["opengwasdb-stores#312", "opengwasdb-stores#314"])

    def test_an_epic_carries_its_board_fields_and_sub_issue_progress(self) -> None:
        epic = self.epics["opengwasdb-stores#313"]
        self.assertEqual(
            {k: epic[k] for k in ("title", "url", "state", "status", "workstream", "priority", "sub_issues")},
            {
                "title": "Epic: Analysis catalog across all Stores",
                "url": "https://github.com/opengwas/opengwasdb-stores/issues/313",
                "state": "OPEN",
                "status": "Active",
                "workstream": "Interface",
                "priority": None,
                "sub_issues": {"total": 12, "completed": 11},
            },
        )
        self.assertTrue(epic["next"].startswith("9/10 merged on PR #353 · implementing #359"))
        self.assertEqual(self.epics["opengwasdb-stores#366"]["priority"], "P2 · Medium")

    def test_an_epic_without_an_issue_type_is_found_by_its_title(self) -> None:
        self.assertIn("opengwasdb-stores#214", self.epics)

    def test_an_epic_lists_its_sub_issues_and_their_blocked_by_edges(self) -> None:
        children = {c["key"]: c for c in self.epics["opengwasdb-stores#385"]["children"]}
        self.assertEqual(
            list(children),
            ["opengwasdb#333", "opengwasdb-stores#386", "opengwasdb-stores#387", "opengwasdb-stores#388",
             "opengwasdb-stores#389", "opengwasdb-stores#390", "opengwasdb-stores#391", "agent-skills#1"],
        )
        self.assertEqual(children["opengwasdb-stores#389"]["blocked_by"], [])
        self.assertEqual(
            children["opengwasdb-stores#391"]["blocked_by"],
            [
                {"key": "opengwasdb-stores#390", "state": "OPEN"},
                {"key": "opengwasdb-stores#389", "state": "OPEN"},
                {"key": "opengwasdb-stores#388", "state": "OPEN"},
            ],
        )
        self.assertEqual(
            children["opengwasdb-stores#387"],
            {
                "key": "opengwasdb-stores#387",
                "title": "Pin opengwasdb by release tag",
                "url": "https://github.com/opengwas/opengwasdb-stores/issues/387",
                "state": "OPEN",
                "blocked_by": [{"key": "opengwasdb#333", "state": "OPEN"}],
            },
        )

    def test_a_milestone_checklist_is_read_from_its_boxes_only(self) -> None:
        lines = self.milestones["opengwasdb#292"]["lines"]
        self.assertEqual(len(lines), 10)
        self.assertEqual([line["checked"] for line in lines].count(True), 6)
        self.assertEqual(
            lines[1],
            {
                "checked": True,
                "text": "opengwas/opengwasdb-stores#282 stage 1 merged to `dev` (opengwas/opengwasdb-stores#299). Stage 2 is not required.",
                "refs": ["opengwasdb-stores#282", "opengwasdb-stores#299"],
            },
        )
        self.assertEqual(lines[0]["refs"], [])
        self.assertEqual(
            lines[5],
            {
                "checked": False,
                "text": "Hybrid: OGS-00011 (GWAS Catalog EUR) rebuilt with its 3,169 Analyses, opengwas/opengwasdb-stores#225",
                "refs": ["opengwasdb-stores#225"],
            },
        )

    def test_checklist_lines_follow_the_box_syntax_and_resolve_bare_refs(self) -> None:
        body = "\n".join([
            "Prose with a #5 reference and no box.",
            "- plain bullet #6",
            "* [x] star bullet #7",
            "- [X] upper #8 and other/repo#9 and opengwas/agent-skills#1",
            "- [ ] open",
            "  - [x] nested #10",
            "- [] no space",
        ])
        self.assertEqual(
            board.checklist(body, "opengwasdb-stores"),
            [
                {"checked": True, "text": "upper #8 and other/repo#9 and opengwas/agent-skills#1",
                 "refs": ["opengwasdb-stores#8", "agent-skills#1"]},
                {"checked": False, "text": "open", "refs": []},
                {"checked": True, "text": "nested #10", "refs": ["opengwasdb-stores#10"]},
            ],
        )


def write_catalog_version(
    root: Path, version: str, *, built_at: str, commit: str, counts: list[tuple], listed: list[tuple],
    changes: str | None = None,
) -> Path:
    """A catalog version with only the tables the collector reads (`analysis_catalog.database` has the rest)."""
    directory = root / version
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / "catalog.sqlite")
    db.executescript(
        "CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE counts (dimension TEXT NOT NULL, value TEXT, n_analyses INTEGER NOT NULL);"
        "CREATE TABLE listed_analyses (id TEXT, store_id TEXT, analysis_id TEXT);"
    )
    db.executemany("INSERT INTO catalog_meta VALUES (?, ?)",
                   [("built_at", built_at), ("registry_commit", commit), ("builder_version", "1")])
    db.executemany("INSERT INTO counts VALUES (?, ?, ?)", counts)
    db.executemany("INSERT INTO listed_analyses VALUES (?, ?, ?)", [(f"{s}:{a}", s, a) for s, a in listed])
    db.commit()
    db.close()
    if changes is not None:
        (directory / "changes.tsv").write_text(changes)
    return directory


class TestCatalogHistoryCollector(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog_dir = Path(self.tmp.name) / "catalog"
        self.root = self.catalog_dir / "versions"
        self.root.mkdir(parents=True)
        # The older version has no changes.tsv; the newer has two Stores, and
        # its dimension rows overlap (3 + 2 + 1 = 6 rows, 5 Analyses).
        write_catalog_version(
            self.root, "aaaaaaaaaaaaaaaa", built_at="2026-10-23T18:25:29Z", commit="560dae9",
            counts=[("store", "OGS-00027", 3), ("coverage", "cis", 3), ("ancestry", None, 3)],
            listed=[("OGS-00027", "g1"), ("OGS-00027", "g2"), ("OGS-00027", "g3")],
        )
        write_catalog_version(
            self.root, "bbbbbbbbbbbbbbbb", built_at="2026-10-28T22:57:35Z", commit="49d8099",
            counts=[("store", "OGS-00006", 2), ("store", "OGS-00027", 3), ("coverage", "cis", 3),
                    ("coverage", "signals_only", 2), ("ancestry", "EUR", 5)],
            listed=[("OGS-00006", "a"), ("OGS-00006", "b"), ("OGS-00027", "g1"), ("OGS-00027", "g2"), ("OGS-00027", "g3")],
            changes="id\tchange\nOGS-00006:a\tadded\nOGS-00006:b\tadded\nOGS-00027:g3\tchanged\n",
        )
        os.symlink("versions/bbbbbbbbbbbbbbbb", self.catalog_dir / "current")

    def test_each_version_has_its_meta_counts_and_sql_totals(self) -> None:
        result = catalog.collect(self.root)
        self.assertEqual([v["version"] for v in result["versions"]], ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])
        older, newer = result["versions"]
        self.assertEqual(older["catalog_meta"],
                         {"built_at": "2026-10-23T18:25:29Z", "registry_commit": "560dae9", "builder_version": "1"})
        self.assertEqual(older["counts"], [
            {"dimension": "store", "value": "OGS-00027", "n_analyses": 3},
            {"dimension": "coverage", "value": "cis", "n_analyses": 3},
            {"dimension": "ancestry", "value": None, "n_analyses": 3},
        ])
        self.assertEqual(older["totals"], {"analyses": 3, "stores": 1})
        self.assertEqual(newer["totals"], {"analyses": 5, "stores": 2})
        self.assertEqual(len(newer["counts"]), 5)

    def test_the_total_is_the_row_count_not_a_sum_of_dimension_rows(self) -> None:
        db = sqlite3.connect(self.root / "bbbbbbbbbbbbbbbb" / "catalog.sqlite")
        db.execute("INSERT INTO counts VALUES ('store', 'OGS-99999', 100)")
        db.commit()
        db.close()
        newer = catalog.collect(self.root)["versions"][1]
        self.assertEqual(newer["totals"]["analyses"], 5)
        self.assertEqual(sum(r["n_analyses"] for r in newer["counts"] if r["dimension"] == "store"), 105)

    def test_changes_are_tallied_and_a_missing_file_is_null(self) -> None:
        older, newer = catalog.collect(self.root)["versions"]
        self.assertIsNone(older["changes"])
        self.assertEqual(newer["changes"], {"added": 2, "changed": 1})

    def test_current_is_the_version_the_symlink_names(self) -> None:
        result = catalog.collect(self.root)
        self.assertEqual(result["current"], "bbbbbbbbbbbbbbbb")
        self.assertEqual([v["current"] for v in result["versions"]], [False, True])

    def test_the_collector_opens_catalogs_read_only(self) -> None:
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        os.chmod(self.root / "aaaaaaaaaaaaaaaa" / "catalog.sqlite", 0o444)
        catalog.collect(self.root)
        after = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file() and "-journal" in p.name), [])

    def test_a_missing_root_is_an_error_and_an_empty_root_is_not(self) -> None:
        with self.assertRaises(catalog.CollectError):
            catalog.collect(Path(self.tmp.name) / "nowhere")
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(catalog.collect(empty), {"root": str(empty), "current": None, "versions": []})


class FakeGh:
    """A `gh` runner that answers each `gh api <endpoint> ... --jq .[]` from a table, or fails."""

    def __init__(self, answers: dict[str, list[dict] | Exception]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append(args)
        answer = self.answers[args[1]]
        if isinstance(answer, Exception):
            raise answer
        return "".join(json.dumps(row) + "\n" for row in answer)


class TestReleasesCollector(unittest.TestCase):
    def test_both_repositories_tags_and_releases_are_listed(self) -> None:
        gh = FakeGh({
            "repos/opengwas/opengwasdb/tags": [
                {"name": "v0.2.0", "commit": {"sha": "0123456789abcdef0123456789abcdef01234567"}},
            ],
            "repos/opengwas/opengwasdb/releases": [
                {"tag_name": "v0.2.0", "name": "0.2.0", "published_at": "2026-09-01T10:00:00Z",
                 "draft": False, "prerelease": False, "html_url": "https://github.com/opengwas/opengwasdb/releases/tag/v0.2.0",
                 "body": "ignored"},
            ],
            "repos/opengwas/opengwasdb-stores/tags": [],
            "repos/opengwas/opengwasdb-stores/releases": [],
        })
        self.assertEqual(
            releases.collect(gh),
            {"repos": [
                {"repo": "opengwasdb",
                 "tags": [{"name": "v0.2.0", "commit": "0123456789abcdef0123456789abcdef01234567"}],
                 "releases": [{"tag": "v0.2.0", "name": "0.2.0", "published_at": "2026-09-01T10:00:00Z",
                               "draft": False, "prerelease": False,
                               "url": "https://github.com/opengwas/opengwasdb/releases/tag/v0.2.0"}]},
                {"repo": "opengwasdb-stores", "tags": [], "releases": []},
            ]},
        )

    def test_no_tags_and_no_releases_is_not_an_error(self) -> None:
        gh = FakeGh({f"repos/opengwas/{r}/{k}": [] for r in ("opengwasdb", "opengwasdb-stores") for k in ("tags", "releases")})
        result = common.collect_source("releases", lambda: releases.collect(gh))
        self.assertTrue(result["available"])
        self.assertIsNone(result["error"])
        self.assertEqual([(r["tags"], r["releases"]) for r in result["repos"]], [([], []), ([], [])])


def write_store_bundle(registry: Path, store_id: str, release: dict, build: dict, validation: dict | None) -> None:
    root = registry / store_id
    root.mkdir(parents=True)
    (root / "release.yaml").write_text(yaml.safe_dump({"store_id": store_id, **release}))
    (root / "build.yaml").write_text(yaml.safe_dump({"store_id": store_id, **build}))
    (root / "analyses.tsv").write_text("analysis_id\nA1\n")
    if validation is not None:
        (root / "validation.yaml").write_text(yaml.safe_dump(validation))


class TestStoresCollector(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = Path(tmp.name) / "stores"
        self.registry.mkdir()
        write_store_bundle(
            self.registry, "OGS-00001",
            {"label": "eqtlgen-cis", "status": "built", "trial": True, "association_coverage": "cis",
             "access_posture": "public", "derived_from": None, "created_at": "2026-09-15T10:00:00Z",
             "description": "Trial eQTLGen.", "notes": "Built at 0.1.0."},
            {"layout": "ragged", "completion_state": "observed_only"},
            None,
        )
        write_store_bundle(
            self.registry, "OGS-00009",
            {"label": "ukb-b-full", "status": "validated", "association_coverage": "gwas",
             "access_posture": "public", "derived_from": None, "created_at": "2026-10-21T22:20:44Z",
             "accepted_at": "2026-10-21T22:23:58Z", "description": "Full UKB-B.",
             "notes": "Release Erratum: n_associations is null."},
            {"layout": "dense", "completion_state": "reference_completed"},
            {"status": "passed", "validated_at": "2026-10-23T19:04:59Z",
             "observed": {"n_analyses": 2514, "n_variants": 11191070, "n_associations": None, "store_bytes": None}},
        )
        # Not Store Releases: the by-label index, a README, and the emitter's staging area.
        (self.registry / "by-label").mkdir()
        (self.registry / ".staging").mkdir()
        (self.registry / "README.md").write_text("x")

    def test_each_registered_store_has_its_bundle_facts(self) -> None:
        result = stores.collect(self.registry)
        self.assertEqual([s["store_id"] for s in result["stores"]], ["OGS-00001", "OGS-00009"])
        first, second = result["stores"]
        self.assertEqual(first, {
            "store_id": "OGS-00001", "label": "eqtlgen-cis", "status": "built", "trial": True,
            "layout": "ragged", "completion_state": "observed_only", "association_coverage": "cis",
            "access_posture": "public", "derived_from": None, "superseded_by": None,
            "created_at": "2026-09-15T10:00:00Z", "accepted_at": None, "validated_at": None,
            "validation_status": None, "observed": None,
            "description": "Trial eQTLGen.", "notes": "Built at 0.1.0.", "problems": [],
        })
        self.assertEqual(second["trial"], False)
        self.assertEqual(second["accepted_at"], "2026-10-21T22:23:58Z")
        self.assertEqual(second["validated_at"], "2026-10-23T19:04:59Z")
        self.assertEqual(second["validation_status"], "passed")
        self.assertEqual(
            second["observed"],
            {"n_analyses": 2514, "n_variants": 11191070, "n_associations": None, "store_bytes": None},
        )

    def test_an_unreadable_fact_is_reported_on_its_store_and_stops_nothing(self) -> None:
        write_store_bundle(
            self.registry, "OGS-00002",
            {"label": "x", "status": "built", "trial": "yes", "association_coverage": "nonsense"},
            {"layout": "dense", "completion_state": "observed_only"}, None,
        )
        result = stores.collect(self.registry)
        bad = next(s for s in result["stores"] if s["store_id"] == "OGS-00002")
        self.assertIsNone(bad["trial"])
        self.assertIsNone(bad["association_coverage"])
        self.assertEqual(len(bad["problems"]), 2)
        self.assertEqual(len(result["stores"]), 3)

    def test_a_registry_that_is_not_a_directory_is_an_error(self) -> None:
        with self.assertRaises(common.CollectError):
            stores.collect(self.registry / "nowhere")


if __name__ == "__main__":
    unittest.main()
