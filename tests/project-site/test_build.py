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

import fnmatch
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from project_site import build

REPO_ROOT = Path(__file__).resolve().parents[2]

# What the published site serves besides the pages Quarto renders, as
# `docs/_quarto.yml` must keep copying them (`origin/main`'s docs/ holds these).
SERVED_RESOURCES = (
    "data/trait-mapping-summary.json",
    "data/trait-mapping-validation-ogs00011.tsv",
    "data/eur-panel-imputation-summary.csv",
    "benchmark-output/opengwasdb_hm3_release_benchmark.json",
    "diagrams/store-release-workflow.dot",
    "diagrams/store-release-workflow.svg",
    "readiness-evidence.yaml",
    "adr/0039-analysis-catalog.md",
    ".nojekyll",
)


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


class ServedResourcesTest(unittest.TestCase):
    """The site copies what it serves, and not the data its pages read (#391 review, item 12)."""

    def setUp(self) -> None:
        self.config = yaml.safe_load((REPO_ROOT / "docs" / "_quarto.yml").read_text(encoding="utf-8"))
        self.patterns = self.config["project"]["resources"]

    def test_the_collected_data_is_not_copied_into_the_site(self) -> None:
        self.assertNotIn("**/*.json", self.patterns)
        self.assertFalse([pattern for pattern in self.patterns if "_data" in pattern], self.patterns)

    def test_every_path_the_site_serves_today_is_still_copied(self) -> None:
        for path in SERVED_RESOURCES:
            self.assertTrue(
                any(fnmatch.fnmatch(path, pattern) for pattern in self.patterns),
                f"{path} is served today and no resource pattern copies it",
            )


class SyncTreeModeTest(unittest.TestCase):
    """The preview tree is what Quarto runs the project's pre-render hook from (#391).

    A file written fresh is 0644, so a sync that carried only the bytes left
    `docs/pre-render.sh` unrunnable and `quarto preview` refused to start:
    "Failed to spawn ...: Permission denied". The mode is part of what syncs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-sync-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "mirror"
        self.served = self.root / "served"
        self.source.mkdir()
        (self.source / "pre-render.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.source / "pre-render.sh").chmod(0o755)
        (self.source / "index.qmd").write_text("# Overview\n", encoding="utf-8")
        (self.source / "index.qmd").chmod(0o644)

    def test_a_copied_hook_is_executable_and_a_copied_page_is_not(self) -> None:
        build._sync_tree(self.source, self.served)

        self.assertTrue(os.access(self.served / "pre-render.sh", os.X_OK))
        self.assertFalse(os.access(self.served / "index.qmd", os.X_OK))

    def test_the_mode_is_repaired_when_the_bytes_already_match(self) -> None:
        self.served.mkdir()
        (self.served / "pre-render.sh").write_bytes((self.source / "pre-render.sh").read_bytes())
        (self.served / "pre-render.sh").chmod(0o644)  # what a sync that ignored the mode left behind
        (self.served / "index.qmd").write_bytes((self.source / "index.qmd").read_bytes())

        build._sync_tree(self.source, self.served)

        self.assertTrue(os.access(self.served / "pre-render.sh", os.X_OK))
        self.assertEqual(build._sync_tree(self.source, self.served), 0, "a settled tree needs no change")


if __name__ == "__main__":
    unittest.main()
