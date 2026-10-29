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

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from project_site import build, paths
from project_site.collect import common as collect_common
from project_site.publish import PublishRefused, publish

# Quarto is what a publish renders with; the dev environment, where the suites
# run, has it.
QUARTO = shutil.which("quarto")


REPO_ROOT = Path(__file__).resolve().parents[2]


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


def epic(key: str, title: str) -> dict:
    repo, number = key.split("#")
    return {
        "key": key, "title": title, "url": f"https://example.org/{number}", "state": "OPEN",
        "status": "Active", "workstream": "Engineering", "next": "one thing", "priority": None,
        "sub_issues": {"total": 0, "completed": 0}, "purpose": None, "children": [],
    }


def collected(source: str, **payload) -> dict:
    return collect_common.envelope(source, available=True, error=None, **payload)


def stub_collect(data_dir: Path, **kwargs) -> list[dict]:
    """Stand in for the collectors: one epic, one Store, and nothing else."""
    results = [
        collected("board", project="opengwas/1", epics=[epic("opengwasdb-stores#9", "Collected, not copied")],
                  milestones=[], items=[]),
        collected("catalog", root="/nowhere", current=None, versions=[]),
        collected("releases", repos=[]),
        collected("stores", registry=str(kwargs.get("registry")), stores=[{
            "store_id": "OGS-00042", "label": "collected", "status": "validated", "trial": False,
            "layout": "dense", "completion_state": "observed_only", "association_coverage": "gwas",
            "access_posture": "public", "derived_from": None, "superseded_by": None,
            "created_at": None, "accepted_at": None, "validated_at": None, "validation_status": None,
            "observed": None, "description": None, "notes": None, "problems": [],
        }]),
        collected("records", epics=[], invalid={}, yardstick={"results": [], "invalid": {}}),
    ]
    STUBBED.append((Path(data_dir), kwargs))
    for result in results:
        collect_common.write_source(Path(data_dir), result)
    return results


STUBBED: list[tuple[Path, dict]] = []


@unittest.skipUnless(QUARTO, "Quarto is not on PATH; a publish renders with it")
class PublishCollectsItsOwnData(unittest.TestCase):
    """A publish collects the data it renders, in the checkout it published (#391 review, item 11)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="project-site-publish-data-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bare = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)

        # The engine the site documents, at the revision the checkout pins.
        self.engine = self.root / "engine.git"
        engine = self.root / "engine"
        engine.mkdir()
        git(engine, "init", "-q", "-b", "main")
        git(engine, "config", "user.email", "test@example.org")
        git(engine, "config", "user.name", "Test")
        # The engine's own landing page is a hand-built HTML file, and staging drops
        # it in favour of the generated engine index (project_site.build).
        (engine / "docs").mkdir()
        (engine / "docs" / "index.html").write_text("<html><body>Engine</body></html>\n", encoding="utf-8")
        git(engine, "add", "-A")
        git(engine, "commit", "-q", "-m", "engine docs")
        subprocess.run(["git", "clone", "-q", "--bare", str(engine), str(self.engine)], check=True)
        self.engine_commit = git(engine, "rev-parse", "HEAD")

        self.repo = self.root / "checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.org")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "docs").mkdir()
        # What the site serves today: the path the publish's check must find.
        (self.repo / "docs" / "keep.md").write_text("# Kept\n", encoding="utf-8")
        (self.repo / "CONTEXT.md").write_text("# Glossary\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "the served page")
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "origin", "dev")
        git(self.repo, "push", "-q", "origin", "dev:main")

        (self.repo / "docs" / "_quarto.yml").write_text(
            "project:\n  type: website\n  pre-render:\n    - \"./pre-render.sh\"\n"
            "  resources:\n    - \"**/*.md\"\n",
            encoding="utf-8",
        )
        shutil.copyfile(REPO_ROOT / "docs" / "pre-render.sh", self.repo / "docs" / "pre-render.sh")
        (self.repo / "docs" / "pre-render.sh").chmod(0o755)
        (self.repo / "docs" / "index.qmd").write_text("# Overview\n\nGenerated at render time.\n", encoding="utf-8")
        (self.repo / "stores").mkdir()
        (self.repo / "pixi.lock").write_text(
            "packages:\n  - pypi: git+https://github.com/opengwas/opengwasdb"
            f"?rev={self.engine_commit}\n",
            encoding="utf-8",
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "the site")
        git(self.repo, "push", "-q", "origin", "dev")
        self.work = self.root / "work"

    def test_the_publish_collects_into_the_render_and_renders_what_it_collected(self) -> None:
        STUBBED.clear()
        with mock.patch.dict(os.environ, {"OPENGWASDB_SITE_ENGINE_CLONE": str(self.engine)}), \
                mock.patch.object(collect_common, "gh_runner", lambda *a, **k: ""), \
                mock.patch("project_site.collect.__main__.run", stub_collect):
            result = publish(repo=self.repo, ref="origin/dev", remote="origin", dry_run=True,
                             render=build.render_checkout, work_root=self.work)

        self.assertEqual(len(STUBBED), 1, "one collection per render")
        data_dir, kwargs = STUBBED[0]
        self.assertEqual(data_dir.name, "_data")
        self.assertTrue(data_dir.parent.name.endswith("-stage"), "collected into the render's own staging tree")
        self.assertFalse((self.repo / "docs" / "_data").exists(), "the checkout itself is never written to")
        registry = Path(kwargs["registry"])
        # The registry is the published checkout's — the scratch worktree this run
        # rendered — and not the developer's tree the command was run in.
        self.assertEqual(registry.name, "stores")
        self.assertIn(str(self.work), str(registry))
        self.assertTrue(registry.parent.name.startswith("checkout-publish"), registry)
        self.assertNotEqual(registry.parent, self.repo)
        self.assertEqual(Path(kwargs["epics_dir"]), registry.parent / "docs" / "_epics")
        self.assertEqual(Path(kwargs["yardstick_dir"]), registry.parent / "docs" / "_yardstick")

        published = sorted(git(self.repo, "ls-tree", "-r", "--name-only", "gh-pages").splitlines())
        self.assertIn("stores/OGS-00042.html", published)
        self.assertIn("epics/opengwasdb-stores-9.html", published)
        self.assertIn("Collected, not copied", git(self.repo, "show", "gh-pages:epics.html"))
        # And the pages point at the commit they were rendered from (#391 review, item 13).
        self.assertIn(
            f"/blob/{result.source_commit}/stores/OGS-00042",
            git(self.repo, "show", "gh-pages:stores/OGS-00042.html"),
        )
        self.assertTrue(result.dry_run)
        self.assertFalse(result.pushed)

    def test_a_named_data_directory_is_used_instead_of_collecting(self) -> None:
        with mock.patch.dict(os.environ, {collect_common.DATA_DIR_ENV_VAR: str(self.root / "collected")}), \
                mock.patch("project_site.collect.__main__.run", stub_collect):
            self.assertEqual(build.collect_data(self.root / "mirror", self.repo), [])

        self.assertEqual(STUBBED, [])


class DefaultRefTest(unittest.TestCase):
    """Every merge lands on `main` and is a version (#386), so that is what a publish renders."""

    def test_the_default_publish_ref_is_main(self) -> None:
        self.assertEqual(paths.DEFAULT_PUBLISH_REF, "origin/main")

    def test_a_publish_of_the_default_ref_renders_the_commit_main_names(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="project-site-publish-main-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bare = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        repo = root / "checkout"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.org")
        git(repo, "config", "user.name", "Test")
        (repo / "docs").mkdir()
        (repo / "docs" / "index.qmd").write_text("# Overview\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "docs")
        git(repo, "remote", "add", "origin", str(bare))
        git(repo, "push", "-q", "origin", "main")
        rendered = {}

        def render(source_commit: str, checkout: Path, output: Path) -> Path:
            rendered["commit"] = source_commit
            output.mkdir(parents=True, exist_ok=True)
            (output / "index.html").write_text(source_commit, encoding="utf-8")
            return output

        publish(repo=repo, ref=paths.DEFAULT_PUBLISH_REF, remote="origin", dry_run=True,
                render=render, work_root=root / "work")

        self.assertEqual(rendered["commit"], git(repo, "rev-parse", "origin/main"))


if __name__ == "__main__":
    unittest.main()
