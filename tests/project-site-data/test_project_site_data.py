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
import unittest
from pathlib import Path

from project_site import records
from project_site.collect import board

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


if __name__ == "__main__":
    unittest.main()
