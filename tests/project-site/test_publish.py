#!/usr/bin/env python3
"""Publish the site to `gh-pages`: a clean checkout of the ref, committed, then pushed (#388).

A publish renders the ref itself, not a developer's working tree, and records
which commit it came from. `--dry-run` does everything except the push: it
renders, commits the output to `gh-pages`, and leaves both locally, so the run
can be inspected before anything leaves the host.

Run from the repository root:
    python3 tests/project-site/test_publish.py
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from project_site.publish import PublishRefused, publish


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class PublishDryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-publish-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bare = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "index.qmd").write_text("# Overview\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "docs")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "origin", "dev")
        self.work = self.root / "work"

    def render(self, source_commit: str, checkout: Path, output: Path) -> Path:
        """A stand-in for the Quarto render: one page, naming the commit it came from."""
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text(f"<html><body>{source_commit}</body></html>\n", encoding="utf-8")
        (output / ".nojekyll").write_text("", encoding="utf-8")
        self.rendered_commit = git(checkout, "rev-parse", "HEAD")
        return output

    def test_a_dry_run_commits_gh_pages_locally_and_pushes_nothing(self) -> None:
        result = publish(
            repo=self.repo,
            ref="origin/dev",
            remote="origin",
            dry_run=True,
            render=self.render,
            work_root=self.work,
        )

        source = git(self.repo, "rev-parse", "origin/dev")
        self.assertFalse(result.pushed)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.source_commit, source)
        self.assertEqual(result.gh_pages_commit, git(self.repo, "rev-parse", "gh-pages"))
        self.assertIn(source, git(self.repo, "log", "-1", "--format=%B", "gh-pages"))
        self.assertEqual(
            sorted(git(self.repo, "ls-tree", "-r", "--name-only", "gh-pages").splitlines()),
            [".nojekyll", "index.html"],
        )
        # Nothing left the host: the remote still has only dev.
        self.assertEqual(
            [line.split()[1] for line in git(self.repo, "ls-remote", "--heads", "origin").splitlines()],
            ["refs/heads/dev"],
        )
        # The render read the ref's own checkout, not the developer's working tree.
        self.assertEqual(self.rendered_commit, source)
        # The transient checkout is gone; the gh-pages worktree is kept for the next run.
        worktrees = [
            Path(line.split(" ", 1)[1])
            for line in git(self.repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]
        self.assertNotIn(self.work / "checkout-publish", worktrees)
        self.assertIn(self.work / "checkout-gh-pages", worktrees)

    def test_a_second_run_updates_the_existing_gh_pages_worktree(self) -> None:
        publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work)
        first = git(self.repo, "rev-parse", "gh-pages")

        result = publish(
            repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work
        )

        self.assertNotEqual(result.gh_pages_commit, first)
        self.assertEqual(
            sorted(git(self.repo, "ls-tree", "-r", "--name-only", "gh-pages").splitlines()),
            [".nojekyll", "index.html"],
        )

    def test_a_ref_that_is_not_on_the_remote_is_refused(self) -> None:
        with self.assertRaises(PublishRefused):
            publish(
                repo=self.repo,
                ref="origin/main",
                remote="origin",
                dry_run=True,
                render=self.render,
                work_root=self.work,
            )

    def test_a_dirty_gh_pages_worktree_is_refused(self) -> None:
        publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work)
        (self.work / "checkout-gh-pages" / "index.html").write_text("edited\n", encoding="utf-8")

        with self.assertRaises(PublishRefused):
            publish(
                repo=self.repo,
                ref="origin/dev",
                remote="origin",
                dry_run=True,
                render=self.render,
                work_root=self.work,
            )


if __name__ == "__main__":
    unittest.main()
