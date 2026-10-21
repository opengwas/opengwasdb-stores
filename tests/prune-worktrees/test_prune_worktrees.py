#!/usr/bin/env python3
"""`tools/prune_worktrees.py` (#312): which worktrees it removes, which it keeps
and why, and that `--apply` removes exactly the removable ones.

Fixtures are throwaway repositories under `$TMPDIR` (keep it on /data): a bare
"origin" with `main` and `dev`, a primary checkout, and one worktree per state
the command must tell apart. The in-use cases are real `sleep` processes, one
with its cwd in a worktree and one with the worktree's path in its argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "prune_worktrees.py"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

REMOVABLE_NAMES = ["merged", "squashed", "decoy"]
KEPT_NAMES = ["unmerged", "dirty-tracked", "dirty-untracked", "locked", "in-use-cwd", "in-use-argv"]


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {proc.stderr}")
    return proc.stdout.strip()


def commit_file(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


class PruneWorktreesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="prune-worktrees-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._stop_processes)

        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        git(self.root, "clone", "-q", str(self.origin), str(seed))
        git(seed, "checkout", "-q", "-b", "main")
        commit_file(seed, "base.txt", "base\n")
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", "dev")
        git(seed, "push", "-q", "origin", "dev")
        self.seed = seed

        self.primary = self.root / "primary"
        git(self.root, "clone", "-q", "-b", "dev", str(self.origin), str(self.primary))
        self.trees = self.root / "trees"
        self.trees.mkdir()

        # Merged by ancestry: the branch is at dev's tip.
        self.add_worktree("merged")
        self.add_worktree("decoy")  # its name is a prefix of what a process below mentions
        # Squash-merged: its commit reaches dev as a different commit with the same patch.
        self.add_worktree("squashed")
        commit_file(self.trees / "squashed", "squashed.txt", "squashed\n")
        commit_file(seed, "other.txt", "advance dev first\n")
        (seed / "squashed.txt").write_text("squashed\n")
        git(seed, "add", "squashed.txt")
        git(seed, "commit", "-q", "-m", "squash of squashed")
        git(seed, "push", "-q", "origin", "dev")
        git(self.primary, "fetch", "-q", "origin")

        self.add_worktree("unmerged")
        commit_file(self.trees / "unmerged", "unmerged.txt", "work\n")
        self.add_worktree("dirty-tracked")
        (self.trees / "dirty-tracked" / "base.txt").write_text("edited\n")
        self.add_worktree("dirty-untracked")
        (self.trees / "dirty-untracked" / "new.txt").write_text("new\n")
        self.add_worktree("locked")
        git(self.primary, "worktree", "lock", "--reason", "on a USB stick", str(self.trees / "locked"))
        self.add_worktree("in-use-cwd")
        self.add_worktree("in-use-argv")
        self.add_worktree("here")  # the worktree the command runs from

        self.cwd_proc = self.start(["sleep", "60"], cwd=self.trees / "in-use-cwd")
        self.argv_proc = self.start(
            ["sh", "-c", "sleep 60; :", "sh", str(self.trees / "in-use-argv" / "pixi.toml")], cwd=self.root)
        # Mentions a sibling whose name merely starts with "decoy": not a use of "decoy".
        self.start(["sh", "-c", "sleep 60; :", "sh", str(self.trees / "decoy-other" / "x")], cwd=self.root)

    def add_worktree(self, name: str) -> None:
        git(self.primary, "worktree", "add", "-q", "-b", f"branch/{name}", str(self.trees / name), "origin/dev")

    def ignore(self, worktree: Path, pattern: str) -> None:
        exclude = Path(git(worktree, "rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else worktree / exclude
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a") as fh:
            fh.write(pattern + "\n")

    def start(self, argv: list[str], cwd: Path) -> subprocess.Popen:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def _stop_processes(self) -> None:
        for proc in self.procs:
            proc.kill()
            proc.wait()

    def run_tool(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), *args], cwd=cwd or self.trees / "here",
            env=GIT_ENV, capture_output=True, text=True)

    def verdicts(self, proc: subprocess.CompletedProcess) -> dict[str, tuple[str, str]]:
        found = {}
        for name in [*REMOVABLE_NAMES, *KEPT_NAMES, "here"]:
            prefix = f"{self.trees / name}  "
            lines = [line for line in proc.stdout.splitlines() if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, f"{name}: {proc.stdout}")
            rest = lines[0][len(prefix):]
            branch, _, tail = rest.partition("  ")
            self.assertEqual(branch, f"branch/{name}")
            verdict, _, reason = tail.partition("  ")
            found[name] = (verdict, reason)
        return found

    def test_each_state_is_classified(self) -> None:
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found = self.verdicts(proc)
        for name in REMOVABLE_NAMES:
            self.assertEqual(found[name][0], "REMOVABLE", f"{name}: {found[name]}")
        for name in KEPT_NAMES:
            self.assertEqual(found[name][0], "KEPT", f"{name}: {found[name]}")
        self.assertIn("1 commit not in origin/dev", found["unmerged"][1])
        self.assertIn("dirty", found["dirty-tracked"][1])
        self.assertIn("dirty", found["dirty-untracked"][1])
        self.assertIn("locked", found["locked"][1])
        self.assertIn("on a USB stick", found["locked"][1])
        self.assertIn("runs from", found["here"][1])

    def test_primary_checkout_is_kept(self) -> None:
        proc = self.run_tool()
        line = next(l for l in proc.stdout.splitlines() if l.startswith(f"{self.primary}  "))
        self.assertIn("KEPT", line)
        self.assertIn("primary checkout", line)

    def test_a_process_with_its_cwd_in_the_worktree_is_named(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-cwd"][1]
        self.assertIn("in use", reason)
        self.assertIn(f"pid {self.cwd_proc.pid}", reason)
        self.assertIn("sleep 60", reason)

    def test_a_process_with_the_path_in_its_argv_is_named(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-argv"][1]
        self.assertIn("in use", reason)
        self.assertIn(f"pid {self.argv_proc.pid}", reason)

    def test_a_process_using_the_executable_in_the_worktree_is_named(self) -> None:
        exe = self.trees / "merged" / "bin" / "sleeper"
        exe.parent.mkdir()
        shutil.copy2(shutil.which("sleep"), exe)
        self.ignore(self.trees / "merged", "bin/")  # the worktree stays clean: only the process pins it
        proc = self.start([str(exe), "60"], cwd=self.root)
        reason = self.verdicts(self.run_tool())["merged"]
        self.assertEqual(reason[0], "KEPT", reason)
        self.assertIn(f"pid {proc.pid}", reason[1])

    def test_a_dry_run_changes_nothing(self) -> None:
        before = git(self.primary, "worktree", "list", "--porcelain")
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(git(self.primary, "worktree", "list", "--porcelain"), before)
        for name in REMOVABLE_NAMES:
            self.assertTrue((self.trees / name).is_dir(), name)

    def test_apply_removes_exactly_the_removable_ones_and_keeps_every_branch(self) -> None:
        branches = git(self.primary, "branch", "--list", "--format=%(refname:short)")
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in REMOVABLE_NAMES:
            self.assertFalse((self.trees / name).exists(), f"{name} should be gone")
        for name in [*KEPT_NAMES, "here"]:
            self.assertTrue((self.trees / name).is_dir(), f"{name} should be kept")
        self.assertEqual(git(self.primary, "branch", "--list", "--format=%(refname:short)"), branches)
        listed = git(self.primary, "worktree", "list", "--porcelain")
        for name in REMOVABLE_NAMES:
            self.assertNotIn(str(self.trees / name), listed)
        for name in KEPT_NAMES:
            self.assertIn(str(self.trees / name), listed)
        # What was kept is untouched.
        self.assertEqual((self.trees / "dirty-tracked" / "base.txt").read_text(), "edited\n")
        self.assertTrue((self.trees / "dirty-untracked" / "new.txt").exists())

    def test_apply_removes_ignored_files_with_the_worktree(self) -> None:
        wt = self.trees / "merged"
        (wt / "cache").mkdir()
        (wt / "cache" / "blob").write_text("x")
        self.ignore(wt, "cache/")
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(wt.exists())

    def test_a_worktree_whose_directory_is_gone_is_forgotten(self) -> None:
        shutil.rmtree(self.trees / "merged")
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(str(self.trees / "merged"), git(self.primary, "worktree", "list", "--porcelain"))
        self.assertIn("branch/merged", git(self.primary, "branch", "--list", "--format=%(refname:short)"))

    def test_fetch_is_the_default_and_no_fetch_skips_it(self) -> None:
        self.add_worktree("ahead")
        sha = commit_file(self.trees / "ahead", "ahead.txt", "ahead\n")
        git(self.trees / "ahead", "push", "-q", "origin", "HEAD:dev")  # merged, but primary's origin/dev is stale
        git(self.primary, "update-ref", "refs/remotes/origin/dev", git(self.primary, "rev-parse", "origin/main"))
        self.assertNotEqual(git(self.primary, "rev-parse", "origin/dev"), sha)
        stale = self.run_tool("--no-fetch")
        self.assertIn("KEPT", next(l for l in stale.stdout.splitlines() if l.startswith(f"{self.trees / 'ahead'}  ")))
        fresh = self.run_tool()
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertIn("REMOVABLE", next(l for l in fresh.stdout.splitlines() if l.startswith(f"{self.trees / 'ahead'}  ")))

    def test_sizes_reports_pixi(self) -> None:
        env = self.trees / "merged" / ".pixi" / "envs"
        env.mkdir(parents=True)
        (env / "blob").write_bytes(b"x" * 200_000)
        proc = self.run_tool("--sizes")
        line = next(l for l in proc.stdout.splitlines() if l.startswith(f"{self.trees / 'merged'}  "))
        self.assertRegex(line, r"\[\d+(\.\d)?K\]$")
        self.assertIn("[no .pixi]", next(l for l in proc.stdout.splitlines() if l.startswith(f"{self.trees / 'squashed'}  ")))

    def test_errors_are_non_zero_and_kept_worktrees_are_not(self) -> None:
        self.assertEqual(self.run_tool().returncode, 0)  # kept worktrees are not an error
        outside = self.root / "not-a-repo"
        outside.mkdir()
        proc = self.run_tool(cwd=outside)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error", proc.stderr)
        # a fetch that cannot reach origin is an error, not a silent stale answer
        git(self.primary, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assertNotEqual(self.run_tool(cwd=self.primary).returncode, 0)


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PruneWorktreesTestCase)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {result.testsRun} CHECKS PASSED")


if __name__ == "__main__":
    main()
