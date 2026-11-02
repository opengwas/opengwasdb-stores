#!/usr/bin/env python3
"""The board collector's `cited` field, for the Work map (#419).

`board.parse` over the recorded response (`tests/project-site-data/fixtures/
board-graphql.json`, extended with the bodies of one epic and three sub-issues):
each epic and child carries the paths and dotted modules its text cites, and no
body is kept. Expected values are literals written from the fixture.

Run from the repository root:
    python3 tests/project-site-architecture/test_work_board.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from project_site.collect import board

FIXTURE = Path(__file__).resolve().parents[1] / "project-site-data" / "fixtures" / "board-graphql.json"


class BoardCitedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = board.parse(json.loads(FIXTURE.read_text()))
        cls.epic = {e["key"]: e for e in cls.data["epics"]}["opengwasdb-stores#385"]
        cls.children = {c["key"]: c for c in cls.epic["children"]}

    def test_an_epic_cites_the_code_its_body_names(self) -> None:
        self.assertEqual(self.epic["cited"], ["src/project_site/pages.py"])

    def test_an_epic_with_no_body_cites_nothing(self) -> None:
        other = {e["key"]: e for e in self.data["epics"]}["opengwasdb#58"]
        self.assertEqual(other["cited"], [])

    def test_a_child_cites_paths_modules_and_links_to_files(self) -> None:
        self.assertEqual(
            self.children["opengwasdb#333"]["cited"],
            ["opengwasdb.encoding.se_range", "opengwasdb/query/facade.py", "opengwasdb/store/paths.py"],
        )
        self.assertEqual(self.children["opengwasdb-stores#387"]["cited"], ["src/ogstores/paths.py"])

    def test_a_child_that_names_no_code_cites_nothing(self) -> None:
        self.assertEqual(self.children["opengwasdb-stores#391"]["cited"], [])

    def test_no_body_is_kept_in_the_output(self) -> None:
        text = json.dumps(self.data)
        self.assertNotIn("No code is named", text)
        self.assertNotIn("Closes the v0.3.2 gap", text)
        for child in self.epic["children"]:
            self.assertNotIn("body", child)
        self.assertNotIn("body", self.epic)

    def test_the_existing_board_fields_are_unchanged(self) -> None:
        self.assertEqual(
            {k: self.children["opengwasdb-stores#387"][k] for k in ("key", "title", "url", "state", "blocked_by")},
            {
                "key": "opengwasdb-stores#387",
                "title": "Pin opengwasdb by release tag",
                "url": "https://github.com/opengwas/opengwasdb-stores/issues/387",
                "state": "OPEN",
                "blocked_by": [{"key": "opengwasdb#333", "state": "OPEN"}],
            },
        )
        self.assertEqual(self.epic["purpose"], "The site draws the project from its data.")


if __name__ == "__main__":
    unittest.main()
