#!/usr/bin/env python3
"""The test runner finds every Python suite itself (#257).

`tests/run_all_tests.py` no longer keeps a list of Python suites:
a new `tests/**/test_*.py` or `tests/**/run_tests.py` is a suite as soon as it
exists. Only the R suites, and a suite CI splits in two, are written down.

Run from the repository root:
    python3 tests/packaging/test_discovery.py
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("run_all_tests", REPO_ROOT / "tests" / "run_all_tests.py")
assert _spec and _spec.loader
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def touch(root: Path, relative: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tests = Path(self._tmp.name) / "tests"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def discover(self, r_suites=(), splits=None) -> dict[str, list[str]]:
        suites = runner.discover_suites(self.tests, r_suites=list(r_suites), splits=splits or {})
        return {label: script for label, kind, script in suites if kind == "python"}

    def test_a_suite_is_named_by_its_directory_and_file(self) -> None:
        for relative in (
            "tests/alpha/test_alpha.py",
            "tests/solo/test_anything.py",
            "tests/gamma/run_tests.py",
            "tests/beta/test_one.py",
            "tests/beta/test_two_words.py",
            "tests/alpha/fixtures/helper.py",
            "tests/beta/__pycache__/test_one.cpython-312.pyc",
        ):
            touch(self.tests.parent, relative)

        self.assertEqual(
            self.discover(),
            {
                "alpha": ["tests/alpha/test_alpha.py"],
                "solo": ["tests/solo/test_anything.py"],
                "gamma": ["tests/gamma/run_tests.py"],
                "beta/one": ["tests/beta/test_one.py"],
                "beta/two-words": ["tests/beta/test_two_words.py"],
            },
        )

    def test_a_name_an_r_suite_already_has_falls_back_to_the_file(self) -> None:
        touch(self.tests.parent, "tests/delta/test_annotation.py")

        suites = self.discover(r_suites=[("delta", "r", ["tests/delta/run_tests.R"])])

        self.assertEqual(suites, {"delta/annotation": ["tests/delta/test_annotation.py"]})

    def test_two_suites_with_one_name_are_refused(self) -> None:
        touch(self.tests.parent, "tests/a/b/test_x.py")
        touch(self.tests.parent, "tests/a/test_b.py")
        touch(self.tests.parent, "tests/a/test_c.py")

        with self.assertRaises(ValueError):
            self.discover()

    def test_a_split_suite_runs_as_its_named_halves(self) -> None:
        touch(self.tests.parent, "tests/epsilon/test_epsilon.py")
        touch(self.tests.parent, "tests/epsilon/test_faults.py")
        splits = {"tests/epsilon/test_faults.py": [("faults/a", ["-k", "A"]), ("faults/b", ["-k", "B"])]}

        self.assertEqual(
            self.discover(splits=splits),
            {
                "epsilon": ["tests/epsilon/test_epsilon.py"],
                "faults/a": ["tests/epsilon/test_faults.py", "-k", "A"],
                "faults/b": ["tests/epsilon/test_faults.py", "-k", "B"],
            },
        )


class RepositorySuitesTest(unittest.TestCase):
    """What the runner finds in this repository."""

    def test_every_python_test_file_is_one_suite_and_names_are_unique(self) -> None:
        labels = [label for label, _, _ in runner.SUITES]
        self.assertEqual(len(labels), len(set(labels)))
        files = sorted(
            str(p.relative_to(REPO_ROOT))
            for p in (REPO_ROOT / "tests").rglob("*.py")
            if "__pycache__" not in p.parts and (p.name.startswith("test_") or p.name == "run_tests.py")
        )
        run = {script[0] for _, kind, script in runner.SUITES if kind == "python"}
        self.assertEqual(sorted(run), files)

    def test_ci_suite_names_are_kept(self) -> None:
        """CI's matrix names the suites that run alone, `workflow` first."""
        labels = {label for label, _, _ in runner.SUITES}
        self.assertEqual(runner.EXCLUSIVE_ORDER, ("workflow", "release-faults/boundaries", "release-faults/interrupts"))
        self.assertEqual((set(runner.EXCLUSIVE_ORDER) | set(runner.SUITE_COST)) - labels, set())

    def test_this_environment_imports_this_checkout(self) -> None:
        """The editable install points at this checkout, not another worktree's (#257)."""
        self.assertIsNone(runner.foreign_install())


if __name__ == "__main__":
    unittest.main()
