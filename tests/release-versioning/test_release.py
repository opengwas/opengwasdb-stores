#!/usr/bin/env python3
"""`tools/release.py`: the next version and its notes (#386).

Every merge to `main` is a version. The workflow finds the pull request the
merge came from and the fragments that pull request added, then calls this
script, which owns the two decisions the workflow must not make itself: how the
fragment types set the bump, and what the release notes say. The tests here are
the specification of both, including the cases with nothing to read -- no
previous tag, no pull request, no fragments -- where a silent default would
produce a plausible wrong version.

Run from the repository root:
    python3 tests/release-versioning/test_release.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from script_modules import load_script

release = load_script("release")

REPO = "opengwas/opengwasdb-stores"


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="release-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fragment(self, name: str, text: str = "- Something changed.\n") -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path


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

    def test_a_pull_request_with_no_fragments_is_a_patch(self) -> None:
        plan = release.plan_release(
            previous_tag="v0.3.0", fragments=[], pr_number=7, commit_subject="Fix a thing"
        )
        self.assertEqual(plan.version, "0.3.1")
        self.assertEqual(plan.tag, "v0.3.1")
        self.assertIn("No changelog fragments", plan.notes)
        self.assertIn(f"{REPO}/pull/7", plan.notes)

    def test_a_push_without_a_pull_request_is_a_patch_that_names_its_commit(self) -> None:
        plan = release.plan_release(
            previous_tag="v0.3.0",
            fragments=[],
            pr_number=None,
            commit_subject="Correct the allele frequency orientation (#400)",
        )
        self.assertEqual(plan.version, "0.3.1")
        self.assertIn("Correct the allele frequency orientation (#400)", plan.notes)
        self.assertNotIn("/pull/", plan.notes, "there is no pull request to link to")

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
        """`changelog.d/README.md` explains the format, so it is skipped, not read.

        The workflow filters it out of the diff by name, but the script is also
        run by hand, and `--fragment changelog.d/*.md` is the obvious way to try
        a release: refusing that glob would look like a bug in the fragments.
        """
        readme = self.fragment("README.md", "how to write a fragment\n")
        real = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        self.assertEqual([f.kind for f in release.read_fragments([readme, real])], ["fixed"])


class NotesTest(TemporaryDirectoryTest):
    def test_the_notes_are_the_fragments_under_their_type_and_link_the_pr(self) -> None:
        fixed = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        added = self.fragment("386-b.added.md", "- Added the other thing.\n")
        plan = release.plan_release(
            previous_tag="v0.2.0",
            fragments=release.read_fragments([fixed, added]),
            pr_number=12,
            commit_subject="Merge #12",
        )
        self.assertEqual(plan.version, "0.3.0")
        self.assertLess(plan.notes.index("### Added"), plan.notes.index("### Fixed"))
        self.assertIn("- Added the other thing.", plan.notes)
        self.assertIn("- Fixed the thing.", plan.notes)
        self.assertIn(f"https://github.com/{REPO}/pull/12", plan.notes)

    def test_the_notes_keep_every_fragment_of_one_type(self) -> None:
        first = self.fragment("386-a.fixed.md", "- First fix.\n")
        second = self.fragment("386-b.fixed.md", "- Second fix.\n")
        plan = release.plan_release(
            previous_tag="v0.2.0",
            fragments=release.read_fragments([first, second]),
            pr_number=12,
        )
        self.assertEqual(plan.notes.count("### Fixed"), 1)
        self.assertIn("- First fix.", plan.notes)
        self.assertIn("- Second fix.", plan.notes)


class CommandLineTest(TemporaryDirectoryTest):
    def test_the_cli_prints_the_version_and_writes_the_notes(self) -> None:
        fragment = self.fragment("386-a.fixed.md", "- Fixed the thing.\n")
        listed = self.tmp / "fragments.txt"
        listed.write_text(f"{fragment}\n\n", encoding="utf-8")
        notes_out = self.tmp / "notes.md"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = release.main([
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
            status = release.main(["--previous-tag", "v0.2.0", "--fragment", "changelog.d/missing.md"])
        self.assertEqual(status, 2)
        self.assertIn("missing.md", err.getvalue())


if __name__ == "__main__":
    unittest.main()
