#!/usr/bin/env python3
"""`tools/check_changelog.py`: a fragment per pull request (#386).

A pull request that changes what this repository *is* -- `src/`, `workflow/`,
`tools/`, `resources/` or `stores/` -- must add a fragment under `changelog.d/`,
because the merge to `main` becomes a version whose notes are that pull
request's fragments. `docs/` alone does not: a report or an ADR changes no
Store, no generator and no tool.

These tests run the check against a throwaway git repository, which is the only
way to exercise it: what it reads is a diff between a merge base and `HEAD`, so
a fixture that stubbed the diff would never reach the code that matters.

Run from the repository root:
    python3 tests/release-versioning/test_check_changelog.py
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from script_modules import load_script

changelog = load_script("check_changelog")

SOURCE = "src/ogstores/bundle.py"
FRAGMENT = "changelog.d/386-a-change.fixed.md"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {result.stderr}")


class ChangelogCheckTest(unittest.TestCase):
    """A repository whose `main` is the merge base of the feature branch."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="check-changelog-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        repo = self.repo = self.root / "repo"
        for directory in ("src/ogstores", "changelog.d", "docs/spec"):
            (repo / directory).mkdir(parents=True)
        self.write(SOURCE, "VALUE = 1\n")
        self.write("changelog.d/README.md", "how to write one\n")
        self.write("docs/spec/store-release-workflow.md", "a specification\n")
        git(repo, "init", "-q", "-b", "main")
        self.commit("base")
        git(repo, "checkout", "-q", "-b", "feature")
        previous = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, previous)

    def write(self, path: str, text: str = "- Something changed.\n") -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def check(self, *args: str) -> tuple[int, str, str]:
        """Run the check, and answer with its status, stdout and stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = changelog.main(["--base-ref", "main", *args])
        return status, out.getvalue(), err.getvalue()


class FragmentsDirectoryTest(unittest.TestCase):
    def test_the_watched_paths_are_this_repositorys_sources(self) -> None:
        """What counts as a source change, stated once and checked below.

        `docs/` is deliberately absent: a report, a specification or an ADR
        changes no Store, no generator and no tool, and the site's own pages
        live under `docs/` too. `pixi.toml`, `pixi.lock` and `pyproject.toml`
        are present because a dependency re-pin changes what every build
        resolves and installs, which a reader of the release notes needs to
        know.
        """
        self.assertEqual(changelog.WATCHED, (
            "src/", "workflow/", "tools/", "resources/", "stores/",
            "pixi.toml", "pixi.lock", "pyproject.toml",
        ))
        self.assertEqual(
            changelog.FRAGMENT_TYPES, ("breaking", "added", "changed", "fixed", "internal")
        )
        self.assertEqual(changelog.SKIP_LABEL, "no-changelog")


class VocabularyTest(unittest.TestCase):
    def test_both_scripts_know_one_fragment_vocabulary(self) -> None:
        """`check_changelog` accepts exactly what `release` can classify.

        The two scripts are separate entry points -- neither imports the other,
        because either may be run alone -- so the vocabulary is stated in both
        and held equal here. A type the check accepts but the release cannot
        classify would fail the release workflow after the merge, where the
        pull request's author can no longer see it.
        """
        release = load_script("release")
        self.assertEqual(changelog.FRAGMENT_TYPES, release.FRAGMENT_TYPES)
        self.assertEqual(changelog.FRAGMENT_PATTERN.pattern, release.FRAGMENT_PATTERN.pattern)


class AcceptedTest(ChangelogCheckTest):
    def test_a_fragment_added_alongside_source_changes_is_accepted(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.write(FRAGMENT)
        self.commit("change")

        status, out, _ = self.check()
        self.assertEqual(status, 0)
        self.assertIn("changelog.d/", out)

    def test_a_fragment_not_committed_yet_still_counts(self) -> None:
        """A local run before the fragment's first commit must not cry wolf.

        CI only ever sees committed files, but the author reads the same
        message locally, and a check that fails a fine pull request teaches
        people to pass `--labels no-changelog` reflexively.
        """
        self.write(SOURCE, "VALUE = 2\n")
        self.commit("change")
        self.write(FRAGMENT)

        self.assertEqual(self.check()[0], 0)

    def test_a_fragment_staged_but_not_committed_still_counts(self) -> None:
        """`git add` is not a commit: the author runs the check in both states.

        A staged fragment is neither in `HEAD` nor untracked, so a check that
        reads only those two misses it -- and refusing a fragment that exists is
        the kind of false alarm that teaches people to reach for the
        `no-changelog` label.
        """
        self.write(SOURCE, "VALUE = 2\n")
        self.commit("change")
        self.write(FRAGMENT)
        git(self.repo, "add", FRAGMENT)

        status, out, err = self.check()
        self.assertEqual(status, 0, err)
        self.assertIn("changelog.d/", out)

    def test_a_broken_fragment_staged_but_not_committed_is_refused(self) -> None:
        """The staged scan reads the fragment, it does not only count it.

        Asserting on the message matters: a check that never saw the staged
        fragment would refuse this change for having *no* fragment, which is a
        different -- and wrong -- reason.
        """
        self.write(SOURCE, "VALUE = 2\n")
        self.commit("change")
        self.write("changelog.d/386-a.security.md")
        git(self.repo, "add", "changelog.d/386-a.security.md")

        status, _, err = self.check()
        self.assertEqual(status, 1)
        self.assertIn("386-a.security.md", err)

    def test_a_change_outside_the_watched_directories_needs_no_fragment(self) -> None:
        self.write("docs/spec/store-release-workflow.md", "a better specification\n")
        self.commit("docs")

        status, out, _ = self.check()
        self.assertEqual(status, 0)
        self.assertIn("not required", out)

    def test_the_no_changelog_label_exempts_the_pull_request(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.commit("change")

        status, out, _ = self.check("--labels", "chore,no-changelog")
        self.assertEqual(status, 0)
        self.assertIn("no-changelog", out)


class RefusedTest(ChangelogCheckTest):
    def test_source_changes_without_a_fragment_are_refused(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.commit("change")

        status, _, err = self.check()
        self.assertEqual(status, 1)
        self.assertIn(SOURCE, err)
        self.assertIn("no-changelog", err)
        self.assertIn("changelog.d/README.md", err)

    def test_every_source_needs_a_fragment(self) -> None:
        """One path per watched entry: none is watched by accident.

        Each case starts again from `main`, because the check reads the whole
        diff against the merge base: a file added by an earlier case would
        excuse the one under test.
        """
        for path in (
            "src/pkg.py", "workflow/rule.smk", "tools/tool.py", "resources/panel.tsv",
            "stores/OGS-00001/release.yaml", "pixi.toml", "pixi.lock", "pyproject.toml",
        ):
            with self.subTest(path=path):
                git(self.repo, "reset", "-q", "--hard", "main")
                self.write(path, "a source change\n")
                self.commit(f"change {path}")
                self.assertEqual(self.check()[0], 1)

    def test_editing_a_document_outside_the_watched_directories_is_not_enough(self) -> None:
        """`docs/` alone needs nothing, so it cannot excuse a source change."""
        self.write(SOURCE, "VALUE = 2\n")
        self.write("docs/spec/store-release-workflow.md", "a better specification\n")
        self.commit("change and a doc")

        self.assertEqual(self.check()[0], 1)

    def test_the_fragments_readme_is_not_a_fragment(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.write("changelog.d/README.md", "how to write one, revised\n")
        self.commit("change")

        self.assertEqual(self.check()[0], 1)

    def test_an_unknown_type_is_refused(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.write("changelog.d/386-a.security.md")
        self.commit("change")

        status, _, err = self.check()
        self.assertEqual(status, 1)
        self.assertIn("386-a.security.md", err)

    def test_a_name_the_release_cannot_read_is_refused(self) -> None:
        for name in ("386-a.md", "386-a.fixed.txt", "386-a.fixed.markdown"):
            with self.subTest(name=name):
                self.write(SOURCE, "VALUE = 2\n")
                self.write(f"changelog.d/{name}")
                self.commit(f"add {name}")
                self.assertEqual(self.check()[0], 1)

    def test_a_fragment_in_a_subdirectory_is_refused(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.write("changelog.d/nested/386-a.fixed.md")
        self.commit("change")

        status, _, err = self.check()
        self.assertEqual(status, 1)
        self.assertIn("directly under", err)

    def test_an_empty_fragment_is_refused(self) -> None:
        self.write(SOURCE, "VALUE = 2\n")
        self.write(FRAGMENT, "\n\n")
        self.commit("change")

        status, _, err = self.check()
        self.assertEqual(status, 1)
        self.assertIn("empty", err)

    def test_a_broken_fragment_is_refused_even_when_the_label_is_applied(self) -> None:
        """The label excuses the absence of notes, not a note nothing can read."""
        self.write(SOURCE, "VALUE = 2\n")
        self.write("changelog.d/386-a.security.md")
        self.commit("change")

        self.assertEqual(self.check("--labels", "no-changelog")[0], 1)


if __name__ == "__main__":
    unittest.main()
