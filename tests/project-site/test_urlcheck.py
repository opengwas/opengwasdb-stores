#!/usr/bin/env python3
"""Every path the site serves today still resolves in a render (#388).

The published site's URLs are shared with readers, so a render may not drop one.
The check reads the fixed baseline `resources/site/legacy-served-paths.txt`
(#397): the paths the legacy Pages deployment serves at the commit it was built
from, one per line with `#` comments. An output directory is checked for each
as present, aliased (an HTML redirect page) or missing, failing on any missing
path. A baseline that cannot be read is refused rather than treated as empty.

For an ad-hoc check the command can list a revision's `docs/` instead
(`--git-rev`).

Run from the repository root:
    python3 tests/project-site/test_urlcheck.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_site.urlcheck import (
    PathStatus,
    ServedPathsUnreadable,
    check_paths,
    paths_from_file,
    summarise,
)

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

    def test_the_command_refuses_a_paths_file_it_cannot_read(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "project_site.urlcheck",
                "--paths-file",
                str(Path(self._tmp.name) / "absent.txt"),
                "--output",
                str(self.output),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("absent.txt", completed.stderr)

    def test_the_command_reports_gits_own_error_for_a_bad_revision(self) -> None:
        repo = Path(self._tmp.name) / "git-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "project_site.urlcheck",
                "--git-rev",
                "no-such-revision",
                "--repo",
                str(repo),
                "--output",
                str(self.output),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        # The command alone does not say why: git's own stderr does (#397 review).
        self.assertIn("fatal", completed.stderr.lower())
        self.assertIn("no-such-revision", completed.stderr)

    def _write_paths(self, paths: list[str]) -> Path:
        path = Path(self._tmp.name) / "paths.txt"
        path.write_text("\n".join(paths) + "\n", encoding="utf-8")
        return path


class PathsFromFileTest(unittest.TestCase):
    """The baseline is a file, and a file that cannot be read is refused (#397)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-baseline-")
        self.addCleanup(self._tmp.cleanup)
        self.baseline = Path(self._tmp.name) / "legacy-served-paths.txt"

    def test_each_malformed_line_is_refused_and_named(self) -> None:
        for line in ("../x.md", "a/../b.md", "./x.md", "a//b.md", "a/", "/absolute.md", "a\x00b.md"):
            with self.subTest(line=line):
                self.baseline.write_bytes(b"index.html\n" + line.encode("utf-8") + b"\n")

                with self.assertRaises(ServedPathsUnreadable) as caught:
                    paths_from_file(self.baseline)

                self.assertIn(":2:", str(caught.exception))
                self.assertIn(repr(line), str(caught.exception))

    def test_a_crlf_file_and_surrounding_spaces_are_accepted(self) -> None:
        self.baseline.write_bytes(b"# the baseline\r\n\r\n  index.html  \r\n")

        self.assertEqual(paths_from_file(self.baseline), ["index.html"])

    def test_comments_and_blank_lines_are_not_paths(self) -> None:
        self.baseline.write_text(
            "# the paths at ff204b6\n\n# generated once\nindex.html\nadr/0001.md\n", encoding="utf-8"
        )

        self.assertEqual(paths_from_file(self.baseline), ["index.html", "adr/0001.md"])

    def test_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(ServedPathsUnreadable):
            paths_from_file(self.baseline)

    def test_a_file_holding_no_paths_is_refused(self) -> None:
        self.baseline.write_text("# only a header\n\n", encoding="utf-8")

        with self.assertRaises(ServedPathsUnreadable):
            paths_from_file(self.baseline)

    def test_a_file_that_is_not_utf8_is_refused_and_named(self) -> None:
        self.baseline.write_bytes(b"index.html\n\xff\n")

        with self.assertRaises(ServedPathsUnreadable) as caught:
            paths_from_file(self.baseline)

        self.assertIn(str(self.baseline), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
