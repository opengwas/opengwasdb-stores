#!/usr/bin/env python3
"""Every path the site serves today still resolves in a render (#388).

The published site's URLs are shared with readers, so a render may not drop one.
The check takes the paths `origin/main` serves under `docs/` today
(`git ls-tree -r --name-only origin/main docs`) and an output directory, and
reports each as present, aliased (an HTML redirect page) or missing, failing on
any missing path.

Run from the repository root:
    python3 tests/project-site/test_urlcheck.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_site.urlcheck import PathStatus, check_paths, summarise

REDIRECT = (
    "<!DOCTYPE html><html><head>"
    '<meta http-equiv="refresh" content="0; url=store-curation.html">'
    "</head><body></body></html>\n"
)


class CheckPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-urlcheck-")
        self.addCleanup(self._tmp.cleanup)
        self.output = Path(self._tmp.name) / "_site"
        (self.output / "adr").mkdir(parents=True)
        (self.output / "store-curation.html").write_text("<html><body>curation</body></html>\n", encoding="utf-8")
        (self.output / "adr" / "0001-store-registry-boundary.md").write_text("# ADR 1\n", encoding="utf-8")
        (self.output / "index.html").write_text("<html><body>overview</body></html>\n", encoding="utf-8")
        (self.output / "progress-2026-10.html").write_text(REDIRECT, encoding="utf-8")

    def test_each_path_is_present_aliased_or_missing(self) -> None:
        statuses = check_paths(
            [
                "store-curation.html",
                "adr/0001-store-registry-boundary.md",
                "progress-2026-10.html",
                "imputation-filters.html",
            ],
            self.output,
        )

        self.assertEqual(
            statuses,
            [
                PathStatus("store-curation.html", "present"),
                PathStatus("adr/0001-store-registry-boundary.md", "present"),
                PathStatus("progress-2026-10.html", "aliased", "store-curation.html"),
                PathStatus("imputation-filters.html", "missing"),
            ],
        )

    def test_a_redirect_to_a_path_that_is_absent_is_missing_not_aliased(self) -> None:
        (self.output / "progress-2026-10.html").write_text(
            '<meta http-equiv="refresh" content="0; url=gone.html">\n', encoding="utf-8"
        )

        statuses = check_paths(["progress-2026-10.html"], self.output)

        self.assertEqual(statuses, [PathStatus("progress-2026-10.html", "missing", "gone.html")])

    def test_the_summary_counts_each_state(self) -> None:
        statuses = check_paths(
            ["index.html", "progress-2026-10.html", "imputation-filters.html"], self.output
        )
        self.assertEqual(summarise(statuses), "3 paths: 1 present, 1 aliased, 1 missing")

    def test_the_command_fails_on_a_missing_path(self) -> None:
        paths = self._write_paths(["index.html", "imputation-filters.html"])

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "project_site.urlcheck",
                "--paths-file",
                str(paths),
                "--output",
                str(self.output),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("missing  imputation-filters.html", completed.stdout)
        self.assertIn("2 paths: 1 present, 0 aliased, 1 missing", completed.stdout)

    def test_the_command_passes_when_every_path_resolves(self) -> None:
        paths = self._write_paths(["index.html", "progress-2026-10.html"])

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "project_site.urlcheck",
                "--paths-file",
                str(paths),
                "--output",
                str(self.output),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("2 paths: 1 present, 1 aliased, 0 missing", completed.stdout)

    def _write_paths(self, paths: list[str]) -> Path:
        path = Path(self._tmp.name) / "paths.txt"
        path.write_text("\n".join(paths) + "\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
