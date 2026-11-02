#!/usr/bin/env python3
"""Branches for the Work map, read from a fixture git repository (#419).

A branch counts when it is ahead of `main` and its newest commit is inside the
window; its files are the diff against the merge-base, read NUL-delimited, and
its ticket comes from its name. Expected values are literals written from the
fixture built in `Fixture`.

Run from the repository root:
    python3 tests/project-site-architecture/test_work_branches.py
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import architecture_fixtures as fx

from project_site.collect import branches

NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


def commit(repo: Path, files: dict[str, str], when: str, message: str = "work") -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    fx.git(repo, "add", "--", *files)
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.org", "commit", "-q", "-m", message],
        check=True, env=env, capture_output=True,
    )


class Fixture:
    """`main` with one commit; branches cut from it at fixed dates (window: 21 days before NOW)."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="branches-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        fx.make_repo(self.repo, {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n"})
        fx.git(self.repo, "branch", "-m", "main")
        # 29 Oct 2026: inside the window, ahead of main by two commits.
        fx.git(self.repo, "checkout", "-q", "-b", "orch/416/419", "main")
        commit(self.repo, {"src/a.py": "a = 2\n"}, "2026-10-28T10:00:00+00:00")
        commit(self.repo, {"src/new dir/c.py": "c = 1\n", "src/odd\nname.py": "x = 1\n"}, "2026-10-29T10:00:00Z")
        # 1 Sep 2026: older than 21 days.
        fx.git(self.repo, "checkout", "-q", "-b", "stale-work", "main")
        commit(self.repo, {"src/b.py": "b = 2\n"}, "2026-09-01T10:00:00+00:00")
        # Recent, but nothing ahead of main.
        fx.git(self.repo, "branch", "caught-up", "main")
        # An issue number after a slash, and a lane of an epic with no ticket in its name.
        fx.git(self.repo, "checkout", "-q", "-b", "fix/issue-345", "main")
        commit(self.repo, {"src/b.py": "b = 3\n"}, "2026-11-01T10:00:00+00:00")
        fx.git(self.repo, "checkout", "-q", "-b", "orch/302-integration", "main")
        commit(self.repo, {"src/a.py": "a = 9\n"}, "2026-11-01T11:00:00+00:00")
        fx.git(self.repo, "checkout", "-q", "main")

    def collect(self, **kwargs):
        return branches.collect(self.repo, "opengwasdb-stores", main_ref="main",
                                prefixes=("refs/heads/",), now=NOW, **kwargs)


class BranchTests(unittest.TestCase):
    def test_a_branch_ahead_of_main_within_the_window_is_listed_with_its_files_and_ticket(self) -> None:
        result = Fixture(self).collect()
        lane = next(b for b in result["branches"] if b["name"] == "orch/416/419")
        self.assertEqual(
            {k: lane[k] for k in ("repo", "ahead", "ticket", "epic")},
            {"repo": "opengwasdb-stores", "ahead": 2, "ticket": "opengwasdb-stores#419", "epic": "opengwasdb-stores#416"},
        )
        self.assertEqual(lane["files"], ["src/a.py", "src/new dir/c.py", "src/odd\nname.py"])
        self.assertEqual(lane["committed"], "2026-10-29T10:00:00Z")

    def test_a_branch_older_than_the_window_or_not_ahead_is_not_listed(self) -> None:
        names = [b["name"] for b in Fixture(self).collect()["branches"]]
        self.assertEqual(names, ["fix/issue-345", "orch/302-integration", "orch/416/419"])

    def test_the_window_is_a_parameter(self) -> None:
        names = [b["name"] for b in Fixture(self).collect(days=90)["branches"]]
        self.assertIn("stale-work", names)

    def test_a_ticket_number_after_a_slash_names_a_ticket_and_an_epic_lane_names_only_the_epic(self) -> None:
        by_name = {b["name"]: b for b in Fixture(self).collect()["branches"]}
        self.assertEqual((by_name["fix/issue-345"]["epic"], by_name["fix/issue-345"]["ticket"]), (None, "opengwasdb-stores#345"))
        self.assertEqual((by_name["orch/302-integration"]["epic"], by_name["orch/302-integration"]["ticket"]),
                         ("opengwasdb-stores#302", None))

    def test_a_lane_whose_ticket_number_is_followed_by_a_slug_still_names_its_ticket(self) -> None:
        fixture = Fixture(self)
        fx.git(fixture.repo, "checkout", "-q", "-b", "orch/58/354-guard", "main")
        commit(fixture.repo, {"src/b.py": "b = 5\n"}, "2026-11-02T08:00:00+00:00")
        by_name = {b["name"]: b for b in fixture.collect()["branches"]}
        self.assertEqual((by_name["orch/58/354-guard"]["epic"], by_name["orch/58/354-guard"]["ticket"]),
                         ("opengwasdb-stores#58", "opengwasdb-stores#354"))

    def test_a_branch_that_names_no_ticket_has_neither(self) -> None:
        fixture = Fixture(self)
        fx.git(fixture.repo, "checkout", "-q", "-b", "scratch", "main")
        commit(fixture.repo, {"src/b.py": "b = 4\n"}, "2026-11-02T08:00:00+00:00")
        by_name = {b["name"]: b for b in fixture.collect()["branches"]}
        self.assertEqual((by_name["scratch"]["epic"], by_name["scratch"]["ticket"]), (None, None))

    def test_a_failed_fetch_is_recorded_and_the_branches_already_there_are_read(self) -> None:
        fixture = Fixture(self)
        result = fixture.collect(fetch="+refs/heads/*:refs/heads/*", remote=str(fixture.root / "no-such-origin"))
        self.assertFalse(result["fetch"]["ok"])
        self.assertIn("no-such-origin", result["fetch"]["error"])
        self.assertEqual(len(result["branches"]), 3)

    def test_a_fetch_that_works_brings_a_pushed_branch(self) -> None:
        fixture = Fixture(self)
        mirror = fixture.root / "mirror.git"
        fx.git(fixture.root, "clone", "-q", "--bare", str(fixture.repo), str(mirror))
        for name in ("orch/416/419", "stale-work", "caught-up", "fix/issue-345", "orch/302-integration"):
            fx.git(mirror, "branch", "-D", name)
        result = branches.collect(mirror, "opengwasdb-stores", main_ref="main", prefixes=("refs/heads/",), now=NOW,
                                  fetch="+refs/heads/*:refs/heads/*", remote=str(fixture.repo))
        self.assertEqual(result["fetch"], {"ok": True, "error": None})
        self.assertEqual([b["name"] for b in result["branches"]], ["fix/issue-345", "orch/302-integration", "orch/416/419"])


if __name__ == "__main__":
    unittest.main()
