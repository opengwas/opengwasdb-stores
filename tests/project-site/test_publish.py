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
        # Every scratch directory this run made is gone again, and it made none
        # it did not own.
        self.assertEqual(self.worktrees(), [self.repo])
        self.assertEqual(self.leftovers(), [])

    def test_a_second_run_publishes_again_on_top_of_the_branch(self) -> None:
        publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work)
        first = git(self.repo, "rev-parse", "gh-pages")

        result = publish(
            repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work
        )

        self.assertNotEqual(result.gh_pages_commit, first)
        self.assertEqual(git(self.repo, "rev-parse", "gh-pages~1"), first)
        self.assertEqual(
            sorted(git(self.repo, "ls-tree", "-r", "--name-only", "gh-pages").splitlines()),
            [".nojekyll", "index.html"],
        )
        self.assertEqual(self.worktrees(), [self.repo])

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

    def test_a_pre_existing_directory_at_a_scratch_path_survives_untouched(self) -> None:
        # The old implementation used fixed scratch paths and deleted whatever it
        # found there. Whatever sits at such a path is not this run's to remove.
        old_scratch = self.work / "checkout-publish"
        old_scratch.mkdir(parents=True)
        (old_scratch / "important.txt").write_text("mine\n", encoding="utf-8")
        old_published = self.work / "checkout-gh-pages"
        old_published.mkdir()
        (old_published / "keep.txt").write_text("also mine\n", encoding="utf-8")

        publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work)

        self.assertEqual((old_scratch / "important.txt").read_text(encoding="utf-8"), "mine\n")
        self.assertEqual((old_published / "keep.txt").read_text(encoding="utf-8"), "also mine\n")
        self.assertEqual(sorted(path.name for path in old_scratch.iterdir()), ["important.txt"])
        self.assertEqual(sorted(path.name for path in old_published.iterdir()), ["keep.txt"])

    def test_a_gh_pages_worktree_this_run_does_not_own_is_refused(self) -> None:
        # Someone else is holding gh-pages, and has unsaved edits in it.
        publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True, render=self.render, work_root=self.work)
        git(self.repo, "worktree", "add", "--quiet", str(self.root / "theirs"), "gh-pages")
        theirs = self.root / "theirs" / "index.html"
        theirs.write_text("edited elsewhere\n", encoding="utf-8")

        with self.assertRaises(PublishRefused) as refusal:
            publish(
                repo=self.repo,
                ref="origin/dev",
                remote="origin",
                dry_run=True,
                render=self.render,
                work_root=self.work,
            )

        self.assertIn(str(self.root / "theirs"), str(refusal.exception))
        self.assertEqual(theirs.read_text(encoding="utf-8"), "edited elsewhere\n")

    def worktrees(self) -> list[Path]:
        return [
            Path(line.split(" ", 1)[1]).resolve()
            for line in git(self.repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]

    def leftovers(self) -> list[Path]:
        if not self.work.exists():
            return []
        return sorted(path for path in self.work.iterdir() if path.is_dir())


if __name__ == "__main__":
    unittest.main()
