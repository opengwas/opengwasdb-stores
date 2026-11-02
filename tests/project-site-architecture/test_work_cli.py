#!/usr/bin/env python3
"""The `check` and `conflicts` commands of `python3 -m project_site.architecture` (#419).

`conflicts` is run over a collection directory (`--data`) holding `architecture.json`
and `work.json` written from the fixtures of `work_fixtures.py`; `check` collects the
architecture on the spot from the fixture repositories of `architecture_fixtures.py`.
Expected output is written by hand.

Run from the repository root:
    python3 tests/project-site-architecture/test_work_cli.py
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as afx
from work_fixtures import ARCHITECTURE, board, branch, branch_set, compute, epic, ticket

from project_site import architecture as cli
from project_site.collect import common

CITED = ["opengwasdb/encoding/se_range.py"]


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdout=out)
    return code, out.getvalue()


class ConflictsTests(unittest.TestCase):
    def data(self, *, blocked: bool) -> str:
        """#350 (epic 58) cites an encoding file and has no branch; #338 (epic 347) is built on a branch."""
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=CITED)]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338", blocked_by=["opengwasdb#350"] if blocked else None)])),
            branch_set(branch("orch/347/338", ["opengwasdb/encoding/dense.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        directory = Path(tempfile.mkdtemp(prefix="cli-"))
        self.addCleanup(shutil.rmtree, directory, True)
        common.write_source(directory, common.envelope("work", available=True, error=None, **result))
        common.write_source(directory, ARCHITECTURE)
        return str(directory)

    def test_files_that_meet_uncoordinated_work_exit_1_and_name_it(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--files", "opengwasdb/encoding/new.py")
        self.assertEqual(code, 1)
        self.assertIn("Store Encoding", text)
        for named in ("opengwasdb#350", "opengwasdb#58", "opengwasdb/encoding/se_range.py", "orch/347/338", "opengwasdb#338",
                      "opengwasdb#347"):
            self.assertIn(named, text)
        self.assertIn("UNCOORDINATED", text)

    def test_a_branch_whose_ticket_has_no_edge_to_the_other_work_exits_1(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--branch", "orch/347/338")
        self.assertEqual(code, 1)
        self.assertIn("opengwasdb#350", text)
        self.assertIn("opengwasdb/encoding/se_range.py", text)
        self.assertNotIn("opengwasdb:orch/347/338", text.split("\n", 1)[1])  # below the heading: its own branch is not "other work"

    def test_a_blocking_edge_between_the_two_tickets_makes_it_exit_0(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=True), "--branch", "orch/347/338")
        self.assertEqual(code, 0)
        self.assertIn("coordinated", text)
        self.assertIn("opengwasdb#338 waits for opengwasdb#350", text)
        self.assertNotIn("UNCOORDINATED", text)

    def test_a_ticket_given_with_files_is_judged_by_its_edges(self) -> None:
        code, _ = run("conflicts", "--data", self.data(blocked=True), "--ticket", "opengwasdb#338",
                      "--files", "opengwasdb/encoding/dense.py")
        self.assertEqual(code, 0)

    def test_files_that_meet_nothing_exit_0(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--files", "opengwasdb/store/x.py", "README.md")
        self.assertEqual(code, 0)
        self.assertIn("meets no other work", text)

    def test_a_branch_the_collection_does_not_hold_is_an_error_not_a_pass(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--branch", "no/such-branch")
        self.assertEqual(code, 2)
        self.assertIn("no/such-branch", text)


class CheckTests(unittest.TestCase):
    def fixtures(self, *, registry: dict | None = None, engine: dict | None = None) -> list[str]:
        root = Path(tempfile.mkdtemp(prefix="check-"))
        self.addCleanup(shutil.rmtree, root, True)
        afx.make_repo(root / "registry", registry or afx.REGISTRY_FILES, branch="trunk")
        afx.make_repo(root / "engine", engine or afx.ENGINE_FILES)
        return ["--registry-root", str(root / "registry"), "--engine-git", str(root / "engine"), "--engine-ref", "main"]

    def test_clean_claims_exit_0(self) -> None:
        code, text = run("check", *self.fixtures())
        self.assertEqual(code, 0, text)
        self.assertIn("this repository: 0 unclaimed, 0 stale", text)

    def test_an_unclaimed_file_in_this_repository_exits_1_naming_it(self) -> None:
        code, text = run("check", *self.fixtures(registry={**afx.REGISTRY_FILES, "src/extra/new.py": "x = 1\n"}))
        self.assertEqual(code, 1)
        self.assertIn("unclaimed: src/extra/new.py", text)

    def test_an_unclaimed_engine_file_and_a_stale_claim_exit_1(self) -> None:
        code, text = run("check", *self.fixtures(engine={**afx.ENGINE_FILES, "opengwasdb/other/x.py": "x = 1\n"}))
        self.assertEqual(code, 1)
        self.assertIn("unclaimed: opengwasdb/other/x.py", text)

    BASELINE = (
        "allowed:\n"
        "  - {file: opengwasdb/model/enums.py, name: opengwasdb.store.util}\n"
        "  - {file: opengwasdb/query/facade.py, name: opengwasdb.cli.main}\n"
        "  - {file: opengwasdb/store/paths.py, name: opengwasdb.query.facade}\n"
    )

    def baseline(self, text: str) -> list[str]:
        return self.fixtures(engine={**afx.ENGINE_FILES, "docs/architecture/layering-baseline.yaml": text})

    def test_a_baseline_listing_exactly_the_upward_imports_passes(self) -> None:
        code, text = run("check", *self.baseline(self.BASELINE))
        self.assertEqual(code, 0, text)
        self.assertIn("layering baseline: 3 allowed, 3 measured", text)

    def test_one_import_fixed_and_one_new_with_equal_counts_exits_1_naming_both(self) -> None:
        listed = self.BASELINE.replace(
            "  - {file: opengwasdb/store/paths.py, name: opengwasdb.query.facade}\n",
            "  - {file: opengwasdb/store/gone.py, name: opengwasdb.query.old}\n",
        )
        code, text = run("check", *self.baseline(listed))
        self.assertEqual(code, 1)
        self.assertIn("layering baseline: 3 allowed, 3 measured", text)
        self.assertIn("new: opengwasdb/store/paths.py opengwasdb.query.facade (Store envelope, layer 1 -> Query, layer 2)", text)
        self.assertIn("stale: opengwasdb/store/gone.py opengwasdb.query.old", text)

    def test_a_baseline_missing_entries_reports_each_new_import(self) -> None:
        code, text = run("check", *self.baseline("allowed:\n  - {file: opengwasdb/model/enums.py, name: opengwasdb.store.util}\n"))
        self.assertEqual(code, 1)
        self.assertIn("new: opengwasdb/query/facade.py opengwasdb.cli.main", text)
        self.assertNotIn("stale:", text)

    def test_an_engine_without_a_baseline_says_so_and_still_passes(self) -> None:
        code, text = run("check", *self.fixtures())
        self.assertEqual(code, 0)
        self.assertIn("no layering baseline", text)


if __name__ == "__main__":
    unittest.main()
