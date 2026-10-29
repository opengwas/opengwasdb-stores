#!/usr/bin/env python3
"""Stage the engine's docs at the pinned revision, and read that revision from the lock (#388).

The engine section of the site is opengwasdb's own documentation at the
revision `pixi.lock` pins, so it describes the runtime the registry actually
imports. Only its Markdown and its self-contained HTML are documentation: its
`docs/` also holds 26 logs, 89 JSON and 8 JSONL files, and 21 `.qmd` sources
whose reports are published beside them as HTML, so those are not staged.

Run from the repository root:
    python3 tests/project-site/test_staging.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site.staging import stage_docs

REPO_ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class StageDocsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-staging-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "engine"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        docs = self.repo / "docs"
        (docs / "spec").mkdir(parents=True)
        (docs / "a.md").write_text("# A\n", encoding="utf-8")
        (docs / "b.html").write_text("<html><body>B</body></html>\n", encoding="utf-8")
        (docs / "spec" / "c.md").write_text("# C\n", encoding="utf-8")
        # Not documentation, and not staged: a log, a data file, a qmd source.
        (docs / "run.log").write_text("log\n", encoding="utf-8")
        (docs / "stats.json").write_text("{}\n", encoding="utf-8")
        (docs / "report.qmd").write_text("# qmd\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# engine\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")

        (docs / "later.md").write_text("# Later\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "second")

    def test_staging_a_revision_yields_exactly_that_revision_s_docs(self) -> None:
        dest = self.root / "staged"
        staged = stage_docs(self.repo, self.first, dest)

        self.assertEqual(staged, ["a.md", "b.html", "spec/c.md"])
        self.assertEqual(
            sorted(str(path.relative_to(dest)) for path in dest.rglob("*") if path.is_file()),
            ["a.md", "b.html", "spec/c.md"],
        )

    def test_staging_the_newer_revision_adds_its_file(self) -> None:
        dest = self.root / "staged-newer"
        staged = stage_docs(self.repo, "HEAD", dest)

        self.assertEqual(staged, ["a.md", "b.html", "later.md", "spec/c.md"])
        self.assertEqual((dest / "later.md").read_text(encoding="utf-8"), "# Later\n")


class PinnedRevisionTest(unittest.TestCase):
    def test_the_pin_is_read_from_the_lock_s_own_line(self) -> None:
        from project_site.paths import pinned_opengwasdb_revision

        lock = (
            "environments: {}\n"
            "packages:\n"
            "  - pypi: git+https://github.com/opengwas/opengwasdb"
            "?rev=0d59f151c7618933747a59f825deeef6a51de9e1"
            "#0d59f151c7618933747a59f825deeef6a51de9e1\n"
        )
        path = Path(tempfile.mkdtemp(prefix="project-site-lock-")) / "pixi.lock"
        self.addCleanup(lambda: path.parent and os.chmod(path.parent, 0o755))
        path.write_text(lock, encoding="utf-8")

        self.assertEqual(
            pinned_opengwasdb_revision(path), "0d59f151c7618933747a59f825deeef6a51de9e1"
        )

    def test_the_committed_lock_pins_one_revision(self) -> None:
        from project_site.paths import pinned_opengwasdb_revision

        revision = pinned_opengwasdb_revision(REPO_ROOT / "pixi.lock")

        self.assertRegex(revision, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
