#!/usr/bin/env python3
"""The render's two gates: what it checks against, and what a preview watches (#388).

The served-path gate compares a render to the paths the published site serves
today, read from `origin/main`. If that revision cannot be read there is nothing
to compare with, and a render that cannot be checked must not be published: the
gate fails closed instead of passing an unchecked render.

The preview re-stages when an input changes. Its inputs are not only `docs/`:
`CONTEXT.md` is the glossary page and `pixi.lock` pins the engine revision, so an
edit to either is a change the preview must see.

Run from the repository root:
    python3 tests/project-site/test_build.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site import build


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class ServedPathGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-gate-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "registry"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "index.md").write_text("# Index\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "docs")

    def test_the_served_paths_are_read_from_origin_main(self) -> None:
        self._add_remote_with_main()

        self.assertEqual(build.served_paths(self.repo), ["index.md"])

    def test_the_gate_fails_closed_when_origin_main_cannot_be_read(self) -> None:
        # No remote at all: there is no origin/main to list.
        with self.assertRaises(build.SiteCheckFailed):
            build.served_paths(self.repo)

    def _add_remote_with_main(self) -> None:
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "push", "-q", "origin", "dev:main")
        git(self.repo, "fetch", "-q", "origin")


class PreviewWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-watch-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "registry"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "index.md").write_text("# Index\n", encoding="utf-8")
        (self.repo / "CONTEXT.md").write_text("# Glossary\n", encoding="utf-8")
        (self.repo / "pixi.lock").write_text("packages: []\n", encoding="utf-8")

    def test_a_docs_page_change_changes_the_snapshot(self) -> None:
        before = build.watched_snapshot(self.repo)
        self._append(self.repo / "docs" / "index.md", "\nmore\n")

        self.assertNotEqual(build.watched_snapshot(self.repo), before)

    def test_a_glossary_change_changes_the_snapshot(self) -> None:
        before = build.watched_snapshot(self.repo)
        self._append(self.repo / "CONTEXT.md", "\nmore\n")

        self.assertNotEqual(build.watched_snapshot(self.repo), before)

    def test_an_engine_pin_change_changes_the_snapshot(self) -> None:
        before = build.watched_snapshot(self.repo)
        self._append(self.repo / "pixi.lock", "# rev=0\n")

        self.assertNotEqual(build.watched_snapshot(self.repo), before)

    def test_a_report_source_is_not_watched(self) -> None:
        before = build.watched_snapshot(self.repo)
        (self.repo / "docs" / "reports").mkdir()
        (self.repo / "docs" / "reports" / "r.qmd").write_text("# Report\n", encoding="utf-8")

        self.assertEqual(build.watched_snapshot(self.repo), before)

    @staticmethod
    def _append(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
        # Make the mtime move even on a coarse filesystem clock.
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


if __name__ == "__main__":
    unittest.main()
