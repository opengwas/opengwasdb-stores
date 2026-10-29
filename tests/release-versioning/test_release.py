#!/usr/bin/env python3
"""`tools/release.py`: the releases `main` is owed (#386).

Every merge to `main` is a version, so the script has to answer three things
that the workflow must not decide itself:

* **What each merge is**, from the fragment types it added: the bump rule.
* **Which merges are owed a release**, from the tags and the Releases that
  exist: reconciliation. A concurrency queue cannot be relied on to hand every
  merge a run -- GitHub keeps one pending run per group and drops the rest --
  so each run publishes every first-parent commit after the newest tag that has
  no tag yet, and any tagged commit that never got its Release, oldest first.
* **What publication still needs doing** for each of those, so a run that dies
  between pushing a tag and creating its Release is finished by the next one
  instead of leaving half a release behind.

The tests here are the specification of all three. The discovery and
publication tests use throwaway git repositories and a stub `gh` on `PATH`,
because the bugs they guard against (a git failure read as "no tag yet", a tag
read as a finished release) are exactly the ones a stubbed subprocess would
hide.

Run from the repository root:
    python3 tests/release-versioning/test_release.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from script_modules import load_script

changelog = load_script("check_changelog")
release = load_script("release")

REPO = "opengwas/opengwasdb-stores"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {result.stderr}")
    return result.stdout.strip()


class FragmentContractTest(unittest.TestCase):
    """One fragment contract, read by two scripts that must not drift (#386).

    `tools/check_changelog.py` refuses a fragment `tools/release.py` could not
    classify, and the release then classifies it -- so both define the fragment
    types and the file-name pattern. They stay standalone scripts, mirroring the
    sibling `opengwasdb` repository's, and this holds the two copies to the same
    contract.
    """

    def test_the_ci_gate_and_the_release_read_the_same_fragment_types(self) -> None:
        self.assertEqual(changelog.FRAGMENT_TYPES, release.FRAGMENT_TYPES)
        self.assertEqual(tuple(release.HEADINGS), release.FRAGMENT_TYPES)

    def test_the_ci_gate_and_the_release_accept_the_same_file_names(self) -> None:
        self.assertEqual(changelog.FRAGMENT_PATTERN.pattern, release.FRAGMENT_PATTERN.pattern)
        for kind in release.FRAGMENT_TYPES:
            with self.subTest(kind=kind):
                self.assertIsNotNone(changelog.FRAGMENT_PATTERN.match(f"386-a-change.{kind}.md"))
                self.assertIsNotNone(release.FRAGMENT_PATTERN.match(f"386-a-change.{kind}.md"))
        for name in ("386-a-change.markdown", "386-a-change.md", "386-a-change.txt.md", "386-a-change.ADDED.md"):
            with self.subTest(name=name):
                self.assertIsNone(changelog.FRAGMENT_PATTERN.match(name))
                self.assertIsNone(release.FRAGMENT_PATTERN.match(name))


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="release-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fragment(self, name: str, text: str = "- Something changed.\n") -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


def merge(
    *,
    commit: str = "a" * 40,
    subject: str = "Merge #12",
    fragments: tuple[Path, ...] = (),
    pr_number: int | None = 12,
    tag: str | None = None,
) -> object:
    return release.Merge(
        commit=commit,
        subject=subject,
        fragments=release.read_fragments(list(fragments)),
        pr_number=pr_number,
        tag=tag,
    )


# ── The bump rule ───────────────────────────────────────────────────────────


class BumpRuleTest(unittest.TestCase):
    def test_a_fix_is_a_patch(self) -> None:
        self.assertEqual(release.next_version("v0.3.0", ["fixed"]), "0.3.1")
        self.assertEqual(release.next_version("v0.3.0", ["internal"]), "0.3.1")

    def test_an_added_changed_or_breaking_fragment_is_a_minor(self) -> None:
        for kind in ("breaking", "added", "changed"):
            with self.subTest(kind=kind):
                self.assertEqual(release.next_version("v0.3.1", [kind]), "0.4.0")

    def test_the_strongest_fragment_type_wins(self) -> None:
        self.assertEqual(release.next_version("v0.3.0", ["fixed", "added", "internal"]), "0.4.0")

    def test_no_previous_tag_starts_from_zero(self) -> None:
        """Nothing to bump: a fix is 0.0.1, a feature 0.1.0.

        This is the cutover's case -- the first release is computed from no tag
        at all -- so the numbers have to be obviously the first rather than a
        plausible continuation. With `changed` fragments (as #386's are), it is
        `0.1.0`, which is the tag the cutover checklist names.
        """
        self.assertEqual(release.next_version(None, ["fixed"]), "0.0.1")
        self.assertEqual(release.next_version(None, ["added"]), "0.1.0")
        self.assertEqual(release.next_version(None, ["changed"]), "0.1.0")
        self.assertEqual(release.next_version(None, []), "0.0.1")

    def test_an_unknown_fragment_type_is_refused(self) -> None:
        with self.assertRaises(release.ReleaseError) as caught:
            release.next_version("v0.3.0", ["security"])
        self.assertIn("security", str(caught.exception))

    def test_a_tag_that_is_not_a_release_is_refused(self) -> None:
        """One tag convention, and it is checked: the bump needs real numbers."""
        for tag in ("0.3.0", "v0.3", "v0.3.0-rc1", "release-v0.3.0", "v"):
            with self.subTest(tag=tag):
                with self.assertRaises(release.ReleaseError):
                    release.next_version(tag, ["fixed"])

    def test_the_latest_tag_is_the_highest_version_not_the_highest_text(self) -> None:
        tags = {"v0.9.0": "a", "v0.10.0": "b", "v0.2.0": "c"}
        self.assertEqual(release.latest_tag(tags), "v0.10.0")

    def test_no_v_tags_at_all_means_no_previous_tag(self) -> None:
        self.assertIsNone(release.latest_tag({}))


# ── The fragments ───────────────────────────────────────────────────────────


class FragmentsTest(TemporaryDirectoryTest):
    def test_fragments_are_read_from_slug_dot_type_dot_md(self) -> None:
        path = self.fragment("386-release-script.changed.md", "- Versions come from tags.\n")
        fragments = release.read_fragments([path])
        self.assertEqual([(f.kind, f.text) for f in fragments], [("changed", "- Versions come from tags.")])

    def test_a_name_that_is_not_slug_dot_type_dot_md_is_refused(self) -> None:
        for name in ("fixed.md", "386.fixed.txt", "386.unknown.md", ".fixed.md"):
            with self.subTest(name=name):
                with self.assertRaises(release.ReleaseError) as caught:
                    release.read_fragments([self.fragment(name)])
                self.assertIn(name, str(caught.exception))

    def test_an_empty_fragment_is_refused(self) -> None:
        """A fragment that says nothing is a release note that looks like one."""
        with self.assertRaises(release.ReleaseError) as caught:
            release.read_fragments([self.fragment("386-empty.fixed.md", "  \n\n")])
        self.assertIn("empty", str(caught.exception))

    def test_the_directories_readme_is_not_a_fragment(self) -> None:
        """`changelog.d/README.md` explains the format, so it is skipped, not read."""
        readme = self.fragment("README.md", "how to write a fragment\n")
        real = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        self.assertEqual([f.kind for f in release.read_fragments([readme, real])], ["fixed"])


# ── The notes ───────────────────────────────────────────────────────────────


class NotesTest(TemporaryDirectoryTest):
    def test_the_notes_are_the_fragments_under_their_type_and_link_the_pr(self) -> None:
        fixed = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        added = self.fragment("386-b.added.md", "- Added the other thing.\n")
        (planned,) = release.plan_releases(
            tags={"v0.2.0": "0" * 40},
            merges=[merge(fragments=(fixed, added), pr_number=12)],
            repository=REPO,
        )
        self.assertEqual(planned.version, "0.3.0")
        self.assertEqual(planned.tag, "v0.3.0")
        self.assertLess(planned.notes.index("### Added"), planned.notes.index("### Fixed"))
        self.assertIn("- Added the other thing.", planned.notes)
        self.assertIn("- Fixed the thing.", planned.notes)
        self.assertIn(f"https://github.com/{REPO}/pull/12", planned.notes)

    def test_the_notes_keep_every_fragment_of_one_type(self) -> None:
        first = self.fragment("386-a.fixed.md", "- First fix.\n")
        second = self.fragment("386-b.fixed.md", "- Second fix.\n")
        (planned,) = release.plan_releases(
            tags={"v0.2.0": "0" * 40},
            merges=[merge(fragments=(first, second))],
            repository=REPO,
        )
        self.assertEqual(planned.notes.count("### Fixed"), 1)
        self.assertIn("- First fix.", planned.notes)
        self.assertIn("- Second fix.", planned.notes)

    def test_a_pull_request_with_no_fragments_is_a_patch_that_says_so(self) -> None:
        (planned,) = release.plan_releases(
            tags={"v0.3.0": "0" * 40},
            merges=[merge(fragments=(), pr_number=7, subject="Fix a thing")],
            repository=REPO,
        )
        self.assertEqual(planned.version, "0.3.1")
        self.assertIn("No changelog fragments", planned.notes)
        self.assertIn(f"{REPO}/pull/7", planned.notes)

    def test_a_push_without_a_pull_request_is_a_patch_that_names_its_commit(self) -> None:
        (planned,) = release.plan_releases(
            tags={"v0.3.0": "0" * 40},
            merges=[merge(
                fragments=(), pr_number=None,
                subject="Correct the allele frequency orientation (#400)",
            )],
            repository=REPO,
        )
        self.assertEqual(planned.version, "0.3.1")
        self.assertIn("Correct the allele frequency orientation (#400)", planned.notes)
        self.assertNotIn("/pull/", planned.notes, "there is no pull request to link to")

    def test_a_push_without_a_pull_request_still_uses_its_fragments(self) -> None:
        """The fragments set the bump whether or not a pull request was found.

        A direct push that adds an `added` fragment is a minor, its notes are the
        fragment, and the commit subject is named beside it. Saying "no fragments
        were read" there is wrong, and publishes a patch with empty notes.
        """
        added = self.fragment("400-a.added.md", "- Added the thing.\n")
        (planned,) = release.plan_releases(
            tags={"v0.3.0": "0" * 40},
            merges=[merge(fragments=(added,), pr_number=None, subject="Add the thing (#400)")],
            repository=REPO,
        )
        self.assertEqual(planned.version, "0.4.0")
        self.assertIn("- Added the thing.", planned.notes)
        self.assertIn("Add the thing (#400)", planned.notes)
        self.assertNotIn("No changelog fragments", planned.notes)


# ── Reconciliation: which merges are owed a release ─────────────────────────


class ReconciliationTest(TemporaryDirectoryTest):
    def test_numbering_never_goes_down_to_repair_an_older_release(self) -> None:
        """The base is the highest `v*` version, whatever older Releases are repaired.

        A resumed tag must not become the base for the next new merge: with
        `v0.3.0` released and `v0.2.0` without a Release, the new merge is
        `0.3.1`, not `0.2.1`.
        """
        fix = self.fragment("1-a.fixed.md", "- Fixed one.\n")
        planned = release.plan_releases(
            tags={"v0.2.0": "2" * 40, "v0.3.0": "3" * 40},
            merges=[
                merge(commit="2" * 40, fragments=(), tag="v0.2.0"),
                merge(commit="4" * 40, fragments=(fix,)),
            ],
            repository=REPO,
        )
        self.assertEqual([(p.tag, p.needs_tag) for p in planned], [
            ("v0.2.0", False),
            ("v0.3.1", True),
        ])

    def test_three_untagged_merges_are_three_versions_in_ancestry_order(self) -> None:
        """One run publishes every merge whose run was lost, oldest first."""
        fix = self.fragment("1-a.fixed.md", "- Fixed one.\n")
        feature = self.fragment("2-b.added.md", "- Added two.\n")
        internal = self.fragment("3-c.internal.md", "- Internal three.\n")
        planned = release.plan_releases(
            tags={"v0.3.0": "0" * 40},
            merges=[
                merge(commit="1" * 40, fragments=(fix,), pr_number=1),
                merge(commit="2" * 40, fragments=(feature,), pr_number=2),
                merge(commit="3" * 40, fragments=(internal,), pr_number=3),
            ],
            repository=REPO,
        )
        self.assertEqual([(p.commit, p.version) for p in planned], [
            ("1" * 40, "0.3.1"),
            ("2" * 40, "0.4.0"),
            ("3" * 40, "0.4.1"),
        ])
        self.assertTrue(all(p.needs_tag and p.needs_release for p in planned))

    def test_nothing_untagged_plans_nothing(self) -> None:
        self.assertEqual(
            release.plan_releases(tags={"v0.3.0": "0" * 40}, merges=[], repository=REPO), []
        )

    def test_a_tag_without_a_release_is_published_not_retagged(self) -> None:
        """The state a failed `gh release create` leaves: the tag exists.

        Rerunning creates the missing Release and must not tag again -- the plan
        says so, so each publishing step is idempotent on its own.
        """
        fix = self.fragment("1-a.fixed.md", "- Fixed one.\n")
        (planned,) = release.plan_releases(
            tags={"v0.3.1": "1" * 40, "v0.3.0": "0" * 40},
            merges=[merge(commit="1" * 40, fragments=(fix,), tag="v0.3.1")],
            repository=REPO,
        )
        self.assertEqual(planned.version, "0.3.1")
        self.assertEqual(planned.tag, "v0.3.1")
        self.assertFalse(planned.needs_tag)
        self.assertTrue(planned.needs_release)
        self.assertIn("- Fixed one.", planned.notes)

    def test_a_version_whose_tag_already_exists_is_refused(self) -> None:
        """Never publish a version twice.

        `plan_releases` numbers from the highest tag, so it cannot collide with
        one; this is the guard on the step that could -- a caller that names the
        previous tag (the `next` command, or a later reconciliation) -- and
        `publish` checks the tags actually present before it moves anything.
        """
        with self.assertRaises(release.ReleaseError) as caught:
            release._plan_one(
                previous_tag="v0.3.0",
                merge=merge(commit="1" * 40, fragments=()),
                tags={"v0.3.0": "0" * 40, "v0.3.1": "9" * 40},
                repository=REPO,
            )
        self.assertIn("v0.3.1", str(caught.exception))


# ── Discovery: the tags, the commits and their fragments ────────────────────


class RepositoryTest(TemporaryDirectoryTest):
    """A clone on `main` with an `origin`, and `v0.3.0` on both.

    The remote is part of the fixture because the remote's tags are the truth
    (#386): a plan built from local tags cannot tell a tag that was pushed from
    one a failed push left behind.
    """

    def setUp(self) -> None:
        super().setUp()
        root = self.repo = self.tmp / "repo"
        (root / "changelog.d").mkdir(parents=True)
        (root / "changelog.d" / "README.md").write_text("how to write one\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        self.commit("base")
        (root / "src" / "pkg.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.commit("second")
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=GIT_ENV)
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "-u", "origin", "main")
        self.tag("v0.3.0")
        self.enter(root)

    def enter(self, directory: Path) -> None:
        previous = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, previous)

    def commit(self, message: str) -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def add(self, name: str, text: str, message: str) -> str:
        (self.repo / "changelog.d" / name).write_text(text, encoding="utf-8")
        return self.commit(message)

    def tag(self, name: str, commit: str = "HEAD") -> None:
        """Annotate `commit` as `name` and push the tag: the remote must carry it."""
        git(self.repo, "tag", "--annotate", name, "--message", f"Release {name}", commit)
        git(self.repo, "push", "-q", "origin", f"refs/tags/{name}")


class DiscoveryTest(RepositoryTest):
    def test_remote_tags_are_read_from_v_star_only(self) -> None:
        """A stray `99.0.0` is not a version, and must not become one."""
        git(self.repo, "tag", "99.0.0")
        git(self.repo, "push", "-q", "origin", "refs/tags/99.0.0")
        self.assertEqual(list(release.remote_tags()), ["v0.3.0"])

    def test_a_git_failure_is_an_error_not_an_empty_answer(self) -> None:
        """`|| true` in the shell reads a broken repository as "no tag yet"."""
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        self.enter(outside)
        with self.assertRaises(release.ReleaseError) as caught:
            release.remote_tags()
        self.assertIn("git", str(caught.exception))

    def test_the_resume_scope_is_the_newest_release_and_no_older(self) -> None:
        tags = {"v0.2.0": "a", "v0.3.0": "b", "v0.3.2": "c"}
        self.assertEqual(release.resume_tags(tags, {"v0.3.0"}), {"v0.3.2"})
        self.assertEqual(release.resume_tags(tags, {"v0.3.0", "v0.3.2"}), set())
        # no Release exists at all: only the newest tag, never the whole history
        self.assertEqual(release.resume_tags(tags, set()), {"v0.3.2"})
        self.assertEqual(release.resume_tags({}, set()), set())

    def test_only_the_newest_tag_is_resumed_when_no_release_exists(self) -> None:
        """A repository whose tags all predate the workflow gets one Release, not N.

        At the cutover every tag on the remote is older than this machinery, so
        resuming them all would publish a Release for each historical tag -- the
        opposite of "the first run creates exactly the cutover's Release".
        """
        self.tag("v0.2.0", "HEAD~1")
        tags = release.remote_tags()

        self.assertEqual(release.pending(tags=tags, released=set()), [tags["v0.3.0"]])
        planned = release.plan_releases(
            tags=tags,
            merges=[release.Merge(commit=tags["v0.3.0"], subject="cutover", tag="v0.3.0")],
            repository=REPO,
        )
        self.assertEqual([(p.tag, p.needs_tag, p.needs_release) for p in planned],
                         [("v0.3.0", False, True)])

    def test_an_older_missing_release_is_left_alone(self) -> None:
        """Only tags newer than the newest Release are repaired."""
        first = self.add("1-a.fixed.md", "- Fixed one.\n", "fix one")
        self.tag("v0.2.0", "HEAD~1")
        self.tag("v0.3.1", first)
        third = self.add("2-b.fixed.md", "- Fixed two.\n", "fix two")
        tags = release.remote_tags()

        self.assertEqual(set(tags), {"v0.2.0", "v0.3.0", "v0.3.1"})
        self.assertEqual(release.pending(tags=tags, released={"v0.3.0", "v0.3.1"}), [third])
        self.assertEqual(release.resume_tags(tags, {"v0.3.0", "v0.3.1"}), set())

    def test_pending_lists_every_first_parent_commit_after_the_latest_tag(self) -> None:
        second = self.add("1-a.fixed.md", "- Fixed one.\n", "fix")
        third = self.add("2-b.added.md", "- Added two.\n", "feature")
        self.assertEqual(
            release.pending(tags=release.remote_tags(), released={"v0.3.0"}), [second, third]
        )

    def test_a_tagged_commit_in_the_middle_is_not_published_again(self) -> None:
        """A merge that already has its tag is not re-released, and the merges
        after it are numbered from it."""
        first = self.add("1-a.fixed.md", "- Fixed one.\n", "fix one")
        self.tag("v0.3.1", first)
        second = self.add("2-b.fixed.md", "- Fixed two.\n", "fix two")
        tags = release.remote_tags()

        self.assertEqual(release.pending(tags=tags, released={"v0.3.0", "v0.3.1"}), [second])
        planned = release.plan_releases(
            tags=tags,
            merges=[release.Merge(commit=second, subject="fix two", fragments=[])],
            repository=REPO,
        )
        self.assertEqual([p.version for p in planned], ["0.3.2"], first)

    def test_a_tag_whose_release_is_missing_is_pending(self) -> None:
        second = self.add("1-a.fixed.md", "- Fixed one.\n", "fix")
        self.tag("v0.3.1")
        self.assertEqual(release.pending(tags=release.remote_tags(), released={"v0.3.0"}), [second])

    def test_nothing_is_pending_once_every_merge_is_tagged_and_released(self) -> None:
        self.add("1-a.fixed.md", "- Fixed one.\n", "fix")
        self.tag("v0.3.1")
        self.assertEqual(
            release.pending(tags=release.remote_tags(), released={"v0.3.0", "v0.3.1"}), []
        )

    def test_pending_refuses_when_the_latest_tag_is_off_the_first_parent_line(self) -> None:
        """A tag reachable only from a side branch cannot order the merges on main."""
        git(self.repo, "checkout", "-q", "-b", "side")
        side = self.add("1-a.fixed.md", "- Fixed one.\n", "fix on side")
        self.tag("v0.4.0")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")

        self.assertNotIn(side, release.first_parent_history())
        with self.assertRaises(release.ReleaseError) as caught:
            release.pending(tags=release.remote_tags(), released=set())
        self.assertIn("v0.4.0", str(caught.exception))

    def test_commit_facts_read_the_subject_and_the_added_fragments(self) -> None:
        commit = self.add("1-a.fixed.md", "- Fixed one.\n", "Fix the thing (#1)")
        self.assertEqual(
            release.commit_facts(commit), ("Fix the thing (#1)", ["changelog.d/1-a.fixed.md"])
        )

    def test_commit_facts_handle_the_root_commit(self) -> None:
        """A repository's first commit has no parent to diff against."""
        root = git(self.repo, "rev-list", "--max-parents=0", "HEAD")
        self.assertEqual(release.commit_facts(root), ("base", ["changelog.d/README.md"]))


# ── End to end: the `plan` and `publish` commands ───────────────────────────


class PlanAndPublishTest(RepositoryTest):
    """The commands the workflow runs, against real git and a stub `gh`."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "gh-state.json"
        self.state.write_text(json.dumps({"releases": ["v0.3.0"], "prs": {}, "fail_create": False}))
        self.log = self.tmp / "gh.log"
        binary = self.tmp / "bin"
        binary.mkdir()
        stub = binary / "gh"
        stub.write_text(
            f"""#!{sys.executable}
import json, os, sys

state = json.loads(open(os.environ["FAKE_GH_STATE"]).read())
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as handle:
    handle.write(" ".join(args) + "\\n")
if args[:2] == ["release", "list"]:
    print(json.dumps([{{"tagName": tag}} for tag in state["releases"]]))
elif args[:2] == ["release", "create"]:
    if state["fail_create"]:
        print("could not create the release", file=sys.stderr)
        sys.exit(1)
    print("created")
elif args[:1] == ["api"]:
    sha = args[1].split("/commits/")[1].split("/")[0]
    number = state["prs"].get(sha)
    print(json.dumps([{{"number": number, "merged_at": "2026-10-29"}}] if number else []))
else:
    print("unexpected gh invocation: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
""",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.set_environment(PATH=f"{binary}:{os.environ['PATH']}",
                             FAKE_GH_STATE=str(self.state), FAKE_GH_LOG=str(self.log))

    def set_environment(self, **values: str) -> None:
        previous = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous: dict[str, str | None]) -> None:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def gh_calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def set_gh_state(self, **changes: object) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(changes)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def handwritten_plan(self, **overrides: object) -> Path:
        """A plan file naming a commit and tag this repository does not have."""
        entry = {
            "commit": "9" * 40,
            "subject": "elsewhere",
            "pr_number": None,
            "version": "0.3.1",
            "tag": "v0.3.1",
            "notes": "- Something.\n",
            "needs_tag": True,
            "needs_release": True,
        }
        entry.update(overrides)
        out = self.tmp / "plan.json"
        out.write_text(json.dumps([entry]), encoding="utf-8")
        return out

    def plan(self, out: Path | None = None, *extra: str) -> tuple[int, list[dict], str]:
        out = out or self.tmp / "plan.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = release.main(["plan", "--repository", REPO, "--json-out", str(out), *extra])
        entries = json.loads(out.read_text(encoding="utf-8")) if status == 0 else []
        return status, entries, stderr.getvalue()

    def publish(self, out: Path, *, stderr: io.StringIO | None = None) -> int:
        err = stderr if stderr is not None else io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            return release.main(["publish", "--plan", str(out)])

    def test_the_plan_lists_every_merge_that_is_owed_a_release(self) -> None:
        first = self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        second = self.add("2-b.added.md", "- Added two.\n", "Add two (#2)")
        self.set_gh_state(prs={first: 1, second: 2})

        status, plan, _ = self.plan()

        self.assertEqual(status, 0)
        self.assertEqual(
            [(e["commit"], e["version"], e["pr_number"]) for e in plan],
            [(first, "0.3.1", 1), (second, "0.4.0", 2)],
        )
        self.assertTrue(all(e["needs_tag"] and e["needs_release"] for e in plan))

    def test_the_plan_resumes_a_tag_whose_release_is_missing(self) -> None:
        self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        self.tag("v0.3.1")

        _, (entry,), _ = self.plan()

        self.assertEqual((entry["tag"], entry["needs_tag"], entry["needs_release"]),
                         ("v0.3.1", False, True))

    def test_the_plan_refuses_to_release_a_history_before_a_cutover(self) -> None:
        """A cutover replays a whole merged branch, and that is not 571 versions.

        `dev` is hundreds of first-parent commits past a `main` with no tag at
        all; the first push of it into `main` would make every one of them "owed
        a release". The plan refuses a burst larger than `--max-releases` and
        says what to do: tag the merge commit before pushing `main`.
        """
        for index in range(3):
            self.add(f"{index}-a.fixed.md", "- Fixed one.\n", f"fix {index}")

        status, _, error = self.plan(None, "--max-releases", "2")
        self.assertEqual(status, 1)
        self.assertIn("cutover", error)
        self.assertIn("3", error)

    def test_the_plan_accepts_a_burst_up_to_the_limit(self) -> None:
        for index in range(2):
            self.add(f"{index}-a.fixed.md", "- Fixed one.\n", f"fix {index}")
        out = self.tmp / "plan.json"

        status, plan, _ = self.plan(out, "--max-releases", "2")
        self.assertEqual(status, 0)
        self.assertEqual(len(plan), 2)

    def test_publish_tags_pushes_and_creates_the_release(self) -> None:
        commit = self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        out = self.tmp / "plan.json"
        self.plan(out)

        self.assertEqual(self.publish(out), 0)

        self.assertEqual(git(self.repo, "rev-parse", "v0.3.1^{commit}"), commit)
        self.assertEqual(release.remote_tags()["v0.3.1"], commit, "the tag reached the remote")
        self.assertTrue(any(call.startswith("release create v0.3.1") for call in self.gh_calls()))

    def test_the_notes_are_read_from_the_commit_that_added_the_fragment(self) -> None:
        """A later edit to a fragment must not rewrite an earlier release.

        Fragments are never edited, but that is a rule people keep, not
        something git enforces: reading the working tree meant a copy edit in a
        later commit silently changed what an earlier version's Release said.
        """
        first = self.add("1-a.fixed.md", "- The original note.\n", "fix one")
        (self.repo / "changelog.d" / "1-a.fixed.md").write_text(
            "- Rewritten much later.\n", encoding="utf-8"
        )
        self.commit("rewrite the fragment")

        planned = release.collect(release.remote_tags(), {"v0.3.0"}, REPO, [first])

        self.assertIn("- The original note.", planned[0].notes)
        self.assertNotIn("Rewritten much later", planned[0].notes)

    def test_publish_finishes_a_release_the_previous_run_could_not_create(self) -> None:
        """The tag is pushed, `gh release create` fails, and the next run completes it.

        This is the partial publication the reviews found: "tag exists" was read
        as "released", so the Release could never be created afterwards.
        """
        commit = self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        out = self.tmp / "plan.json"
        self.plan(out)

        self.set_gh_state(fail_create=True)
        self.assertEqual(self.publish(out), 1)
        self.assertEqual(git(self.repo, "rev-parse", "v0.3.1^{commit}"), commit)

        self.set_gh_state(fail_create=False)
        _, (entry,), _ = self.plan(out)
        self.assertEqual((entry["needs_tag"], entry["needs_release"]), (False, True))

        self.assertEqual(self.publish(out), 0)
        self.assertEqual(git(self.repo, "rev-parse", "v0.3.1^{commit}"), commit, "the tag did not move")
        self.assertTrue(any(call.startswith("release create v0.3.1") for call in self.gh_calls()))

    def test_publish_pushes_a_tag_a_failed_push_left_behind(self) -> None:
        """A push that failed leaves the tag locally; the retry must still push it.

        Otherwise the Release cannot be created (`gh release create` needs the
        tag on the remote, and `--verify-tag` fails), and every retry concludes
        from the *local* tag that there is nothing to do.
        """
        commit = self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        git(self.repo, "tag", "--annotate", "v0.3.1", "--message", "Release v0.3.1")
        # the remote does not have it: this is what a failed `git push` leaves
        out = self.tmp / "plan.json"
        _, (entry,), _ = self.plan(out)
        self.assertEqual((entry["needs_tag"], entry["commit"]), (True, commit))

        self.assertEqual(self.publish(out), 0)

        self.assertEqual(release.remote_tags()["v0.3.1"], commit)
        self.assertTrue(any(call.startswith("release create v0.3.1") for call in self.gh_calls()))

    def test_publish_refuses_a_remote_tag_on_another_commit(self) -> None:
        """The remote tag's commit decides, and no Release is created for it.

        `--verify-tag` checks existence, not which commit the tag names, so a
        plan naming the wrong commit would otherwise publish that commit's notes
        under a published tag.
        """
        self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        git(self.repo, "tag", "--annotate", "v0.3.1", "--message", "somewhere else")
        git(self.repo, "push", "-q", "origin", "refs/tags/v0.3.1")
        # Only the remote has it now, so a clone-local check cannot see the problem.
        git(self.repo, "tag", "--delete", "v0.3.1")

        error = io.StringIO()
        self.assertEqual(self.publish(self.handwritten_plan(), stderr=error), 1)
        # Refused because the *remote's* tag names another commit -- not because
        # some later step happened to fail.
        self.assertIn("on the remote", error.getvalue())
        self.assertFalse(any(call.startswith("release create") for call in self.gh_calls()))

    def test_publish_refuses_a_local_tag_on_another_commit(self) -> None:
        """A stale local tag is refused rather than silently shadowed by a new one."""
        self.add("1-a.fixed.md", "- Fixed one.\n", "Fix one (#1)")
        git(self.repo, "tag", "--annotate", "v0.3.1", "--message", "already here")

        error = io.StringIO()
        self.assertEqual(self.publish(self.handwritten_plan(), stderr=error), 1)
        self.assertIn("exists locally", error.getvalue())
        self.assertNotIn("v0.3.1", release.remote_tags())
        self.assertFalse(any(call.startswith("release create") for call in self.gh_calls()))


# ── The `next` command, which touches nothing ───────────────────────────────


class NextCommandTest(TemporaryDirectoryTest):
    def test_the_cli_prints_the_version_and_writes_the_notes(self) -> None:
        fragment = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        listed = self.tmp / "fragments.txt"
        listed.write_text(f"{fragment}\n\n", encoding="utf-8")
        notes_out = self.tmp / "notes.md"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = release.main([
                "next",
                "--previous-tag", "v0.2.0",
                "--fragments-from", str(listed),
                "--pr-number", "12",
                "--notes-out", str(notes_out),
            ])

        self.assertEqual(status, 0)
        self.assertEqual(out.getvalue().strip(), "0.2.1")
        self.assertIn("- Fixed the thing.", notes_out.read_text(encoding="utf-8"))

    def test_the_cli_refuses_a_fragment_it_cannot_read(self) -> None:
        """A refusal is a message on stderr and a non-zero status, not a crash."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = release.main([
                "next", "--previous-tag", "v0.2.0", "--fragment", "changelog.d/missing.md"
            ])
        self.assertEqual(status, 1)
        self.assertIn("missing.md", err.getvalue())


if __name__ == "__main__":
    unittest.main()
