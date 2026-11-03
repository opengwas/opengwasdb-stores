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
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as afx
from work_fixtures import ARCHITECTURE, NO_ENGINE_MODEL, board, branch, branch_set, compute, epic, ticket

from project_site import architecture as cli
from project_site.collect import architecture as architecture_collector, common

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
        code, text = run("conflicts", "--data", self.data(blocked=False), "--repo", "engine", "--files", "opengwasdb/encoding/new.py")
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
                      "--repo", "engine", "--files", "opengwasdb/encoding/dense.py")
        self.assertEqual(code, 0)

    def test_files_without_a_repo_exit_2_with_a_one_line_reason(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--files", "opengwasdb/encoding/new.py")
        self.assertEqual(code, 2)
        self.assertEqual(text.strip(), "cannot decide: --files needs --repo engine or --repo registry: a bare path can mean a file of either repository")

    def test_files_that_meet_nothing_exit_0(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--repo", "engine", "--files", "opengwasdb/store/x.py", "README.md")
        self.assertEqual(code, 0)
        self.assertIn("meets no other work", text)

    def test_a_branch_the_collection_does_not_hold_is_an_error_not_a_pass(self) -> None:
        code, text = run("conflicts", "--data", self.data(blocked=False), "--branch", "no/such-branch")
        self.assertEqual(code, 2)
        self.assertIn("no/such-branch", text)


class UnmeasuredAndUnlinkableTests(unittest.TestCase):
    def collection(self, result: dict, architecture: dict = ARCHITECTURE) -> str:
        directory = Path(tempfile.mkdtemp(prefix="cli-"))
        self.addCleanup(shutil.rmtree, directory, True)
        common.write_source(directory, common.envelope("work", available=True, error=None, **result))
        common.write_source(directory, architecture)
        return str(directory)

    def test_engine_files_without_the_engines_model_cannot_be_decided(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11", cited=["opengwasdb/encoding/a.py"])])),
            architecture=NO_ENGINE_MODEL,
        )
        code, text = run("conflicts", "--data", self.collection(result, NO_ENGINE_MODEL), "--repo", "engine", "--files", "opengwasdb/encoding/a.py")
        self.assertEqual(code, 2)
        self.assertIn("cannot decide", text)
        self.assertIn("engine", text)
        self.assertNotIn("meets no other work", text)

    def test_registry_files_are_still_decided_without_the_engines_model(self) -> None:
        result = compute(board(), architecture=NO_ENGINE_MODEL)
        code, text = run("conflicts", "--data", self.collection(result, NO_ENGINE_MODEL), "--repo", "registry", "--files", "src/project_site/x.py")
        self.assertEqual((code, "meets no other work" in text), (0, True))

    def test_an_engine_branch_without_the_engines_model_cannot_be_decided(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11")])),
            branch_set(branch("orch/10/11", ["opengwasdb/encoding/a.py"], epic="opengwasdb#10", ticket="opengwasdb#11")),
            architecture=NO_ENGINE_MODEL,
        )
        code, _ = run("conflicts", "--data", self.collection(result, NO_ENGINE_MODEL), "--branch", "orch/10/11")
        self.assertEqual(code, 2)

    def test_a_meet_with_an_epic_branch_exits_1_calling_it_unlinkable(self) -> None:
        result = compute(
            board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#57")]),
                  epic("opengwasdb#347", "Active", [ticket("opengwasdb#338")])),
            branch_set(branch("orch/58-deepen-modules", ["opengwasdb/encoding/a.py"], epic="opengwasdb#58"),
                       branch("orch/347/338", ["opengwasdb/encoding/b.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        code, text = run("conflicts", "--data", self.collection(result), "--branch", "orch/347/338")
        self.assertEqual(code, 1)
        self.assertIn("UNLINKABLE", text)
        self.assertIn("opengwasdb:orch/58-deepen-modules belongs to an epic as a whole", text)
        self.assertNotIn("UNCOORDINATED", text)


class UnplacedAndUnrefreshedTests(unittest.TestCase):
    """Code the map cannot place, and data a fetch could not bring up to date, are never "no conflict"."""

    def collection(self, result: dict, architecture: dict = ARCHITECTURE) -> str:
        directory = Path(tempfile.mkdtemp(prefix="cli-"))
        self.addCleanup(shutil.rmtree, directory, True)
        common.write_source(directory, common.envelope("work", available=True, error=None, **result))
        common.write_source(directory, architecture)
        return str(directory)

    def cited(self, *sets: dict) -> str:
        """#350 (epic 58) cites an encoding file, as in ConflictsTests."""
        return self.collection(compute(board(epic("opengwasdb#58", "Active", [ticket("opengwasdb#350", cited=CITED)])), *sets))

    def test_engine_code_no_component_claims_cannot_be_decided(self) -> None:
        # The engine's CLI is a package, opengwasdb/cli/; a mistyped or brand-new path is on no component.
        code, text = run("conflicts", "--data", self.cited(), "--repo", "engine", "--files", "opengwasdb/cli.py")
        self.assertEqual(code, 2, text)
        self.assertIn("cannot decide: no component claims opengwasdb/cli.py", text)
        self.assertNotIn("meets no other work", text)

    def test_registry_code_no_component_claims_cannot_be_decided(self) -> None:
        code, text = run("conflicts", "--data", self.cited(), "--repo", "registry", "--files", "workflow/rules/new.smk")
        self.assertEqual(code, 2, text)
        self.assertIn("workflow/rules/new.smk", text)

    def test_a_meet_still_exits_1_beside_an_unplaced_file_and_names_it(self) -> None:
        code, text = run("conflicts", "--data", self.cited(), "--repo", "engine",
                         "--files", "opengwasdb/encoding/new.py", "opengwasdb/nowhere/new.py")
        self.assertEqual(code, 1, text)
        self.assertIn("UNCOORDINATED", text)
        self.assertIn("also: no component claims opengwasdb/nowhere/new.py", text)

    def test_files_outside_the_map_are_named_and_do_not_block(self) -> None:
        code, text = run("conflicts", "--data", self.cited(), "--repo", "engine",
                         "--files", "opengwasdb/store/x.py", "tests/test_x.py", "docs/spec/x.md")
        self.assertEqual(code, 0, text)
        self.assertIn("outside the map (not code a component claims): tests/test_x.py, docs/spec/x.md", text)
        self.assertIn("meets no other work", text)

    def test_a_branch_with_code_no_component_claims_cannot_be_decided(self) -> None:
        result = compute(
            board(epic("opengwasdb#10", "Active", [ticket("opengwasdb#11")])),
            branch_set(branch("orch/10/11", ["opengwasdb/nowhere/a.py", "tests/test_a.py"], epic="opengwasdb#10",
                              ticket="opengwasdb#11")),
        )
        code, text = run("conflicts", "--data", self.collection(result), "--branch", "orch/10/11")
        self.assertEqual(code, 2, text)
        self.assertIn("no component claims opengwasdb/nowhere/a.py", text)
        self.assertNotIn("tests/test_a.py", text)

    def test_a_failed_branch_fetch_cannot_be_decided(self) -> None:
        failed = {"ok": False, "error": "fatal: unable to access 'https://github.com/opengwas/opengwasdb/':\n"
                                        "Could not resolve host: github.com"}
        code, text = run("conflicts", "--data", self.cited(branch_set(fetch=failed)), "--repo", "engine",
                         "--files", "opengwasdb/store/x.py")
        self.assertEqual(code, 2, text)
        self.assertIn("cannot decide: the fetch failed", text)
        self.assertIn("Could not resolve host: github.com", text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("cannot decide")]), 1, text)
        self.assertNotIn("meets no other work", text)

    def test_a_failed_engine_fetch_cannot_be_decided(self) -> None:
        stale = {**ARCHITECTURE, "engine": {**ARCHITECTURE["engine"],
                                            "refresh": {"ok": False, "error": "fatal: could not read from remote repository."}}}
        result = compute(board(), architecture=stale)
        code, text = run("conflicts", "--data", self.collection(result, stale), "--repo", "registry",
                         "--files", "src/project_site/x.py")
        self.assertEqual(code, 2, text)
        self.assertIn("could not read from remote repository", text)

    def test_a_meet_found_in_data_a_fetch_could_not_refresh_still_exits_1(self) -> None:
        code, text = run("conflicts", "--data", self.cited(branch_set(fetch={"ok": False, "error": "boom"})),
                         "--repo", "engine", "--files", "opengwasdb/encoding/new.py")
        self.assertEqual(code, 1, text)
        self.assertIn("UNCOORDINATED", text)
        self.assertIn("also: the fetch failed", text)


class OffBoardEpicBranchTests(unittest.TestCase):
    def test_an_epic_branch_is_unlinkable_even_when_its_epic_is_not_on_the_board(self) -> None:
        result = compute(
            board(epic("opengwasdb#347", "Active", [ticket("opengwasdb#338")])),
            branch_set(branch("orch/58-deepen-modules", ["opengwasdb/encoding/a.py"], epic="opengwasdb#58"),
                       branch("orch/347/338", ["opengwasdb/encoding/b.py"], epic="opengwasdb#347", ticket="opengwasdb#338")),
        )
        self.assertEqual(result["meets"][0]["coordination"], "unlinkable")
        directory = Path(tempfile.mkdtemp(prefix="cli-"))
        self.addCleanup(shutil.rmtree, directory, True)
        common.write_source(directory, common.envelope("work", available=True, error=None, **result))
        common.write_source(directory, ARCHITECTURE)
        code, text = run("conflicts", "--data", str(directory), "--branch", "orch/347/338")
        self.assertEqual(code, 1)
        self.assertIn("UNLINKABLE", text)
        self.assertNotIn("UNCOORDINATED", text)


class GitFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cli-git-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        afx.make_repo(self.root / "registry", afx.REGISTRY_FILES, branch="trunk")
        afx.make_repo(self.root / "engine", afx.ENGINE_FILES)
        self.data = self.root / "data"
        common.write_source(self.data, board())
        common.write_source(self.data, common.collect_source("architecture", lambda: architecture_collector.collect(
            self.root / "registry", engine_git=self.root / "engine", engine_ref="main")))
        self.args = ["--data", str(self.data), "--registry-root", str(self.root / "registry"),
                     "--engine-git", str(self.root / "engine"), "--repo", "registry", "--files", "src/gen/x.py"]

    def test_an_inaccessible_local_directory_exits_2_with_a_reason(self) -> None:
        code, text = run("conflicts", *self.args, "--local", str(self.root / "no-such-clone"))
        self.assertEqual(code, 2)
        self.assertIn("cannot decide", text)
        self.assertIn("no-such-clone", text)
        self.assertNotIn("Traceback", text)

    def unusable_work_root(self) -> Path:
        """A work root that cannot be created, on any host: a path below a regular file."""
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a directory\n")
        old = os.environ.get(cli.site_paths.WORK_ROOT_ENV)
        os.environ[cli.site_paths.WORK_ROOT_ENV] = str(blocker / "site")
        self.addCleanup(lambda: os.environ.pop(cli.site_paths.WORK_ROOT_ENV, None) if old is None
                        else os.environ.__setitem__(cli.site_paths.WORK_ROOT_ENV, old))
        return blocker / "site"

    def test_conflicts_over_a_collection_never_touches_the_work_root(self) -> None:
        root = self.unusable_work_root()
        code, text = run("conflicts", *self.args, "--local", str(self.root / "no-such-clone"))
        self.assertEqual(code, 2)
        self.assertIn("no-such-clone", text)
        self.assertNotIn(str(root), text)
        self.assertNotIn("Traceback", text)

    def test_collecting_on_the_spot_with_no_usable_work_root_exits_2_naming_the_path_and_the_variable(self) -> None:
        root = self.unusable_work_root()
        code, text = run("conflicts", "--registry-root", str(self.root / "registry"), "--engine-git", str(self.root / "engine"),
                         "--repo", "registry", "--files", "src/gen/x.py")
        self.assertEqual(code, 2)
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn(str(root), text)
        self.assertIn("OPENGWASDB_SITE_ROOT", text)

    def test_a_local_branch_whose_diff_fails_exits_2(self) -> None:
        clone = self.root / "clone"
        afx.make_repo(clone, {"a.txt": "x\n"})
        afx.git(clone, "checkout", "-q", "--orphan", "unrelated")
        (clone / "b.txt").write_text("y\n")
        afx.git(clone, "add", "b.txt")
        afx.git(clone, "-c", "user.name=t", "-c", "user.email=t@e.org", "commit", "-q", "-m", "unrelated")
        code, text = run("conflicts", *self.args, "--local", str(clone))
        self.assertEqual(code, 2)
        self.assertIn("diff", text)

    def test_check_with_an_unreadable_engine_exits_2_naming_it(self) -> None:
        code, text = run("check", "--data", str(self.data), "--engine-git", str(self.root / "no-such-engine.git"))
        self.assertEqual(code, 2)
        self.assertIn("no-such-engine.git", text)
        self.assertIn("layering baseline", text)


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

    def assert_undecided(self, text: str, *needles: str) -> None:
        code, out = run("check", *self.baseline(text))
        self.assertEqual(code, 2, out)
        self.assertIn("layering-baseline.yaml", out)
        for needle in needles:
            self.assertIn(needle, out)
        self.assertNotIn("Traceback", out)

    def test_a_baseline_that_is_not_yaml_exits_2_naming_the_file(self) -> None:
        self.assert_undecided("allowed: [\n", "not valid YAML")

    def test_a_baseline_yaml_that_fails_to_construct_exits_2_not_a_traceback(self) -> None:
        self.assert_undecided("allowed: [2026-13-32]\n", "not valid YAML")

    def test_the_one_loader_refuses_a_duplicate_and_an_extra_key_in_the_engines_baseline_too(self) -> None:
        self.assert_undecided("allowed:\n  - {file: a.py, name: n}\n  - {file: a.py, name: n}\n", "listed twice")
        self.assert_undecided("allowed:\n  - {file: a.py, name: n, why: because}\n", "entry #0")

    def test_a_baseline_without_allowed_is_not_an_empty_baseline(self) -> None:
        self.assert_undecided("something: else\n", "`allowed`")
        self.assert_undecided("", "`allowed`")

    def test_a_baseline_entry_of_the_wrong_shape_exits_2_naming_it(self) -> None:
        self.assert_undecided("allowed:\n  - just-a-string\n", "entry #0")
        self.assert_undecided("allowed:\n  - {file: a.py}\n", "entry #0")
        self.assert_undecided("allowed:\n  - {file: a.py, name: 3}\n", "entry #0")

    NO_MODEL = {k: v for k, v in afx.ENGINE_FILES.items() if k != "docs/architecture/components.yaml"}

    def test_no_engine_model_and_a_clean_registry_is_undecided_not_a_pass(self) -> None:
        code, text = run("check", *self.fixtures(engine=self.NO_MODEL))
        self.assertEqual(code, 2, text)
        self.assertIn("this repository: 0 unclaimed, 0 stale", text)
        self.assertIn("no architecture model there, so nothing to check", text)

    def test_no_engine_model_and_an_unclaimed_registry_file_still_exits_1(self) -> None:
        code, text = run("check", *self.fixtures(registry={**afx.REGISTRY_FILES, "src/extra/new.py": "x = 1\n"}, engine=self.NO_MODEL))
        self.assertEqual(code, 1)
        self.assertIn("unclaimed: src/extra/new.py", text)

    def test_a_malformed_baseline_with_a_registry_gap_exits_1_and_still_says_what_was_unreadable(self) -> None:
        code, text = run("check", *self.fixtures(
            registry={**afx.REGISTRY_FILES, "src/extra/new.py": "x = 1\n"},
            engine={**afx.ENGINE_FILES, "docs/architecture/layering-baseline.yaml": "allowed: [\n"}))
        self.assertEqual(code, 1)
        self.assertIn("not valid YAML", text)

    def test_an_engine_fetch_that_fails_is_undecided_not_a_pass(self) -> None:
        # The fixture engine has no `origin`, so `--refresh` cannot fetch it on any host.
        code, text = run("check", *self.fixtures(), "--refresh")
        self.assertEqual(code, 2, text)
        self.assertIn("cannot check: the fetch failed for the engine's main", text)
        self.assertIn("origin", text)

    def test_a_failed_engine_fetch_with_a_gap_still_exits_1(self) -> None:
        code, text = run("check", *self.fixtures(registry={**afx.REGISTRY_FILES, "src/extra/new.py": "x = 1\n"}), "--refresh")
        self.assertEqual(code, 1, text)
        self.assertIn("unclaimed: src/extra/new.py", text)
        self.assertIn("the fetch failed", text)

    def test_an_engine_without_a_baseline_says_so_and_still_passes(self) -> None:
        code, text = run("check", *self.fixtures())
        self.assertEqual(code, 0)
        self.assertIn("no layering baseline", text)


if __name__ == "__main__":
    unittest.main()
