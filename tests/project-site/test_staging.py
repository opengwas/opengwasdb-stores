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

from unittest import mock

from project_site import build, paths, staging
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


class StageIntoTheMirrorTest(unittest.TestCase):
    """A render stages into the mirror, never into the checkout (#388 review)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-stage-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "registry"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "docs" / "adr").mkdir(parents=True)
        (self.repo / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
        (self.repo / "docs" / "adr" / "x.md").write_text("# X\n", encoding="utf-8")
        (self.repo / "CONTEXT.md").write_text("# Glossary\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "docs")
        self.commit = git(self.repo, "rev-parse", "HEAD")

        self.engine = self.root / "engine"
        self.engine.mkdir()
        git(self.engine, "init", "-q", "-b", "dev")
        git(self.engine, "config", "user.email", "test@example.org")
        git(self.engine, "config", "user.name", "Test")
        (self.engine / "docs").mkdir()
        (self.engine / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        git(self.engine, "add", "-A")
        git(self.engine, "commit", "-q", "-m", "docs")
        self.engine_commit = git(self.engine, "rev-parse", "HEAD")

        self.work = self.root / "work"
        self.environ = mock.patch.dict(os.environ, {"OPENGWASDB_SITE_ROOT": str(self.work)})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def status_of_checkout(self) -> str:
        """Every file git knows about or would ignore: the checkout's whole shape."""
        return git(self.repo, "status", "--porcelain", "--ignored")

    def test_staging_leaves_the_checkout_untouched(self) -> None:
        before = self.status_of_checkout()

        stage = build.stage_everything(self.repo, clone=self.engine, revision=self.engine_commit)

        self.assertEqual(self.status_of_checkout(), before)
        self.assertFalse((self.repo / "docs" / "opengwasdb").exists())
        self.assertFalse((self.repo / "docs" / "glossary.md").exists())
        self.assertEqual(
            sorted(path.relative_to(stage.mirror).as_posix() for path in stage.mirror.rglob("*") if path.is_file()),
            ["a.md", "adr/x.md", "glossary.md", "opengwasdb/guide.md", "opengwasdb/index.qmd"],
        )
        # The records the render reports still name the commits it documented.
        self.assertEqual(stage.commit, self.commit)
        self.assertEqual(stage.engine_commit, self.engine_commit)

    def test_two_work_roots_stage_independently(self) -> None:
        first = build.stage_everything(self.repo, clone=self.engine, revision=self.engine_commit)
        other = self.root / "other-work"
        with mock.patch.dict(os.environ, {"OPENGWASDB_SITE_ROOT": str(other)}):
            second = build.stage_everything(self.repo, clone=self.engine, revision=self.engine_commit)

        self.assertNotEqual(first.mirror, second.mirror)
        self.assertEqual((first.mirror / "a.md").read_text(encoding="utf-8"), "# A\n")
        self.assertEqual((second.mirror / "a.md").read_text(encoding="utf-8"), "# A\n")


class MermaidPageReachabilityTest(unittest.TestCase):
    """A `.md` staged as `.qmd` for its Mermaid cell stays reachable (#388 review)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-mermaid-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "registry"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "site.md").write_text(
            "# The site\n\n```mermaid\ngraph TD; A-->B;\n```\n", encoding="utf-8"
        )
        (self.repo / "docs" / "reference.md").write_text(
            "# Reference\n\nSee [the site page](site.md#publishing).\n", encoding="utf-8"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "docs")
        self.commit = git(self.repo, "rev-parse", "HEAD")

        self.engine = self.root / "engine"
        self.engine.mkdir()
        git(self.engine, "init", "-q", "-b", "dev")
        git(self.engine, "config", "user.email", "test@example.org")
        git(self.engine, "config", "user.name", "Test")
        (self.engine / "docs").mkdir()
        (self.engine / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        git(self.engine, "add", "-A")
        git(self.engine, "commit", "-q", "-m", "docs")
        self.engine_commit = git(self.engine, "rev-parse", "HEAD")

    def test_a_link_to_the_mermaid_page_reaches_its_rendered_page(self) -> None:
        mirror = self.root / "mirror"
        staging.mirror_docs(self.repo, mirror)

        report = staging.rewrite_mirror(
            mirror,
            registry=self.repo,
            commit=self.commit,
            registry_url="https://github.com/opengwas/opengwasdb-stores",
            engine=self.engine,
            engine_commit=self.engine_commit,
            engine_url="https://github.com/opengwas/opengwasdb",
            engine_prefix="opengwasdb/",
        )

        self.assertTrue((mirror / "site.qmd").is_file())
        self.assertFalse((mirror / "site.md").exists())
        self.assertEqual(
            (mirror / "reference.md").read_text(encoding="utf-8"),
            "# Reference\n\nSee [the site page](site.html#publishing).\n",
        )
        self.assertEqual([page for page, _ in report.raw_pages], ["site.md"])
        self.assertIn("```{mermaid}", report.raw_pages[0][1])
        self.assertEqual(report.unparsed, [])


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
