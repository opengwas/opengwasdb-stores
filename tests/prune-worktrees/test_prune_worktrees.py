#!/usr/bin/env python3
"""`tools/prune_worktrees.py` (#312): which worktrees it removes, which it keeps
and why, and that `--apply` removes exactly the removable ones.

Fixtures are throwaway repositories under `$TMPDIR` (keep it on /data): a bare
"origin" with `main` and `dev`, a primary checkout, and one worktree per state
the command must tell apart. The in-use cases are real `sleep` processes, one
with its cwd in a worktree and one with the worktree's path in its argv.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
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
KEPT_NAMES = [
    "unmerged", "dirty-tracked", "dirty-untracked", "locked", "in-use-cwd", "in-use-argv", "in-use-fd",
    "in-use-string", "in-use-link", "empty", "outer",
]
# Detached worktrees: their HEAD is the only ref to what they hold.
DETACHED_NAMES = ["detached-whitespace", "detached-merge", "detached-missing"]


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
        self.add_worktree("in-use-fd")
        self.add_worktree("in-use-string")
        self.add_worktree("in-use-link")
        self.add_worktree("here")  # the worktree the command runs from
        self.add_detached_worktrees()
        self.add_empty_and_nested()

        self.cwd_proc = self.start(["sleep", "3600"], cwd=self.trees / "in-use-cwd")
        self.argv_proc = self.start(
            ["sh", "-c", "sleep 3600; :", "sh", str(self.trees / "in-use-argv" / "pixi.toml")], cwd=self.root)
        self.ignore(self.trees / "in-use-fd", "log")
        (self.trees / "in-use-fd" / "log").write_text("")
        with open(self.trees / "in-use-fd" / "log") as log:  # inherited: the path is in no argument
            self.fd_proc = self.start(["sleep", "3600"], cwd=self.root, stdin=log)
        self.string_proc = self.start(
            ["sh", "-c", f"sleep 3600; : see {self.trees / 'in-use-string'}."], cwd=self.root)
        (self.root / "link").symlink_to(self.trees)  # another spelling of the same directory
        self.link_proc = self.start(
            ["sh", "-c", "sleep 3600; :", "sh", str(self.root / "link" / "in-use-link" / "pixi.toml")], cwd=self.root)
        # A directory whose name merely starts with a worktree's: not a use of "decoy".
        (self.trees / "decoy-cwd").mkdir()
        self.start(["sleep", "3600"], cwd=self.trees / "decoy-cwd")
        # Mentions a sibling whose name merely starts with "decoy": not a use of "decoy".
        self.start(["sh", "-c", "sleep 3600; :", "sh", str(self.trees / "decoy-other" / "x")], cwd=self.root)

    def add_worktree(self, name: str) -> None:
        git(self.primary, "worktree", "add", "-q", "-b", f"branch/{name}", str(self.trees / name), "origin/dev")

    def add_empty_and_nested(self) -> None:
        # An empty commit: cherry calls it merged because dev has some other empty commit.
        self.add_worktree("empty")
        git(self.trees / "empty", "commit", "-q", "--allow-empty", "-m", "Start orchestration")
        git(self.seed, "pull", "-q", "origin", "dev")
        git(self.seed, "commit", "-q", "--allow-empty", "-m", "Start another orchestration")
        git(self.seed, "push", "-q", "origin", "dev")
        # A merged, clean worktree with another worktree (unmerged, dirty) inside an ignored directory.
        self.add_worktree("outer")
        self.ignore(self.trees / "outer", ".claude/")
        self.inner = self.trees / "outer" / ".claude" / "worktrees" / "inner"
        git(self.primary, "worktree", "add", "-q", "-b", "branch/inner", str(self.inner), "origin/dev")
        commit_file(self.inner, "inner.txt", "inner\n")
        (self.inner / "uncommitted.txt").write_text("not saved anywhere else\n")
        git(self.primary, "fetch", "-q", "origin")

    def add_detached_worktrees(self) -> None:
        """Detached HEADs that `git cherry` would call merged, though removing the
        worktree would leave their commits with no ref."""
        seed = self.seed
        git(self.primary, "worktree", "add", "-q", "--detach", str(self.trees / "detached-whitespace"), "origin/dev")
        commit_file(self.trees / "detached-whitespace", "ws.txt", "a  b\n")
        git(seed, "pull", "-q", "origin", "dev")
        commit_file(seed, "ws.txt", "a b\n")  # the same patch, bar whitespace
        git(seed, "push", "-q", "origin", "dev")

        # HEAD is a merge of two commits dev also has, as other commits with the same patches;
        # the merge itself adds a file. Cherry skips merges, so it finds nothing left.
        merge = self.trees / "detached-merge"
        git(self.primary, "worktree", "add", "-q", "--detach", str(merge), "origin/dev")
        base = git(merge, "rev-parse", "HEAD")
        commit_file(merge, "a.txt", "a\n")
        first = git(merge, "rev-parse", "HEAD")
        git(merge, "checkout", "-q", "--detach", base)
        commit_file(merge, "b.txt", "b\n")
        git(merge, "merge", "-q", "--no-ff", "--no-commit", first)
        (merge / "only-in-the-merge.txt").write_text("evil merge\n")
        git(merge, "add", "only-in-the-merge.txt")
        git(merge, "commit", "-q", "-m", "merge with its own content")
        git(seed, "pull", "-q", "origin", "dev")
        commit_file(seed, "a.txt", "a\n")
        commit_file(seed, "b.txt", "b\n")
        git(seed, "push", "-q", "origin", "dev")

        missing = self.trees / "detached-missing"
        git(self.primary, "worktree", "add", "-q", "--detach", str(missing), "origin/dev")
        commit_file(missing, "gone.txt", "gone\n")
        shutil.rmtree(missing)
        git(self.primary, "fetch", "-q", "origin")

    def ignore(self, worktree: Path, pattern: str) -> None:
        exclude = Path(git(worktree, "rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else worktree / exclude
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a") as fh:
            fh.write(pattern + "\n")

    def start(self, argv: list[str], cwd: Path, **kwargs) -> subprocess.Popen:
        """A fixture process that is still running when this returns: a process that
        exited early would be found "unused" and pass for the wrong reason."""
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        errors = tempfile.TemporaryFile()  # so a fixture that dies can say why
        self.addCleanup(errors.close)
        kwargs.setdefault("stderr", errors)
        proc = subprocess.Popen(argv, cwd=cwd, **kwargs)
        self.procs.append(proc)
        want = kwargs.get("executable") or shutil.which(argv[0]) or argv[0]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            try:  # until the exec has happened: /proc/<pid>/exe is no longer the forked test process
                if os.path.samefile(f"/proc/{proc.pid}/exe", want):
                    break
            except OSError:
                pass
            time.sleep(0.02)
        time.sleep(0.2)  # a process that cannot run (an unknown multi-call name) dies at once
        if proc.poll() is not None:
            errors.seek(0)
            real = shutil.which(argv[0])
            self.fail(
                f"fixture process {argv} (executable={kwargs.get('executable')}) exited with {proc.returncode}; "
                f"which({argv[0]!r})={real}, realpath={os.path.realpath(real) if real else None}; "
                f"stderr: {errors.read().decode(errors='replace').strip()!r}")
        return proc

    def assert_fixture_processes_running(self) -> None:
        for proc in self.procs:  # one that has run out, or died, would read as "unused"
            self.assertIsNone(proc.poll(), f"fixture process {proc.args} is no longer running")

    def _stop_processes(self) -> None:
        for proc in self.procs:
            proc.kill()
            proc.wait()

    def run_tool(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        self.assert_fixture_processes_running()
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
        self.assertIn("sleep 3600", reason)

    def test_a_process_with_the_path_in_its_argv_is_named(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-argv"][1]
        self.assertIn("in use", reason)
        self.assertIn(f"pid {self.argv_proc.pid}", reason)

    def test_a_process_using_the_executable_in_the_worktree_is_named(self) -> None:
        # Named "sleep", like the original: a multi-call binary may dispatch on its own file
        # name as well as on argv[0].
        exe = self.trees / "merged" / "bin" / "sleep"
        exe.parent.mkdir()
        shutil.copy2(shutil.which("sleep"), exe)
        self.ignore(self.trees / "merged", "bin/")  # the worktree stays clean: only the process pins it
        # argv[0] is the real name too, so either dispatch style works (uutils, busybox),
        # and no argument holds the worktree's path: only the executable pins it.
        proc = self.start(["sleep", "3600"], cwd=self.root, executable=str(exe))
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

    def line(self, proc: subprocess.CompletedProcess, name: str) -> str:
        prefix = f"{self.trees / name}  "
        return next(l for l in proc.stdout.splitlines() if l.startswith(prefix))

    def check_detached_is_kept(self, name: str) -> None:
        # `git cherry` ignores whitespace and skips merge commits, so both fixtures look
        # merged; removing either would leave its commits with no ref.
        line = self.line(self.run_tool(), name)
        self.assertIn("KEPT", line)
        self.assertIn("not merged", line)
        self.run_tool("--apply")
        self.assertTrue((self.trees / name).is_dir(), name)
        self.assertIn(str(self.trees / name), git(self.primary, "worktree", "list", "--porcelain"))

    def test_a_detached_head_differing_from_dev_only_in_whitespace_is_kept(self) -> None:
        self.check_detached_is_kept("detached-whitespace")

    def test_a_detached_head_whose_content_is_in_a_merge_commit_is_kept(self) -> None:
        self.check_detached_is_kept("detached-merge")

    def test_a_missing_directory_is_kept_and_never_forgotten(self) -> None:
        self.assertFalse((self.trees / "detached-missing").exists())
        line = self.line(self.run_tool(), "detached-missing")
        self.assertIn("KEPT", line)
        self.assertIn("directory missing", line)
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(str(self.trees / "detached-missing"), git(self.primary, "worktree", "list", "--porcelain"))

    def test_apply_forgets_nothing_it_did_not_remove(self) -> None:
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listed = git(self.primary, "worktree", "list", "--porcelain")
        for name in [*KEPT_NAMES, *DETACHED_NAMES, "here"]:
            self.assertIn(str(self.trees / name), listed, name)

    def test_an_open_file_in_the_worktree_is_a_use(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-fd"][1]
        self.assertIn(f"pid {self.fd_proc.pid}", reason)
        self.assertIn(str(self.trees / "in-use-fd" / "log"), reason)

    def test_a_path_inside_a_command_string_is_a_use(self) -> None:
        # "... see /x/in-use-string." : the path ends a sentence.
        reason = self.verdicts(self.run_tool())["in-use-string"][1]
        self.assertIn(f"pid {self.string_proc.pid}", reason)

    def test_another_spelling_of_the_path_is_a_use(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-link"][1]
        self.assertIn(f"pid {self.link_proc.pid}", reason)

    def test_the_matched_path_is_printed(self) -> None:
        reason = self.verdicts(self.run_tool())["in-use-argv"][1]
        self.assertIn(str(self.trees / "in-use-argv" / "pixi.toml"), reason)

    def test_a_directory_sharing_a_prefix_is_not_the_worktree(self) -> None:
        # decoy-cwd (a process's cwd) and decoy-other (in an argument) both start with "decoy".
        self.assertIn("REMOVABLE", self.line(self.run_tool(), "decoy"))

    def in_process(self, *args: str) -> tuple[object, tuple]:
        spec = importlib.util.spec_from_file_location("prune_worktrees_under_test", TOOL)
        tool = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = tool  # dataclasses look their module up here
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(tool)
        return tool, args

    def run_in_process(self, tool, args, **patches) -> tuple[int, str]:
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(self.trees / "here")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = tool.main(list(args))
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def test_each_worktree_is_checked_again_just_before_it_is_removed(self) -> None:
        tool, _ = self.in_process()
        real_remove = tool.remove
        removed: list[str] = []

        def remove_then_start_a_process_in_squashed(wt) -> None:
            real_remove(wt)
            removed.append(wt.path)
            if len(removed) == 1:  # a process starts in "squashed" while the first is being removed
                self.start(["sleep", "3600"], cwd=self.trees / "squashed")

        with mock.patch.object(tool, "remove", remove_then_start_a_process_in_squashed):
            code, out = self.run_in_process(tool, ["--no-fetch", "--apply"])
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(removed), sorted(str(self.trees / n) for n in ("merged", "decoy")))
        self.assertTrue((self.trees / "squashed").is_dir())
        self.assertIn("changed since", out)

    def test_a_process_it_cannot_inspect_keeps_every_worktree(self) -> None:
        tool, _ = self.in_process()
        real_open = open
        hidden = f"/proc/{self.cwd_proc.pid}/cmdline"

        def open_hiding_one_command_line(path, *a, **kw):
            if str(path) == hidden:
                raise PermissionError(13, "hidden", hidden)
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", open_hiding_one_command_line):
            code, out = self.run_in_process(tool, ["--no-fetch", "--apply"])
        self.assertEqual(code, 0, out)
        for name in REMOVABLE_NAMES:
            self.assertTrue((self.trees / name).is_dir(), f"{name} must be kept: it cannot be told apart from in use")
        self.assertIn("cannot tell", out)
        self.assertIn(f"{self.cwd_proc.pid}", out)

    def test_a_refused_removal_is_reported_and_never_forced(self) -> None:
        tool, _ = self.in_process()
        real_git = tool.git
        calls: list[tuple] = []

        def git_refusing_removal(*args, cwd=None, check=True):
            if args[:2] == ("worktree", "remove"):
                calls.append(args)
                if check:
                    raise tool.PruneError("refused")
                return subprocess.CompletedProcess(args, 1, "", "refused")
            return real_git(*args, cwd=cwd, check=check)

        with mock.patch.object(tool, "git", git_refusing_removal):
            code, out = self.run_in_process(tool, ["--no-fetch", "--apply"])
        self.assertEqual(code, 1)
        self.assertTrue(calls)
        self.assertFalse(any("--force" in c for c in calls), calls)
        for name in REMOVABLE_NAMES:
            self.assertTrue((self.trees / name).is_dir(), name)

    def test_an_empty_commit_is_not_evidence_of_a_merge(self) -> None:
        line = self.line(self.run_tool(), "empty")
        self.assertIn("KEPT", line)
        self.assertIn("not merged: 1 commit", line)

    def test_a_worktree_containing_another_is_kept_and_both_survive_apply(self) -> None:
        line = self.line(self.run_tool(), "outer")
        self.assertIn("KEPT", line)
        self.assertIn(str(self.inner), line)
        proc = self.run_tool("--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.inner / "uncommitted.txt").exists())
        listed = git(self.primary, "worktree", "list", "--porcelain")
        self.assertIn(str(self.inner), listed)
        self.assertIn(str(self.trees / "outer"), listed)

    def test_the_matched_path_is_printed_not_the_whole_word(self) -> None:
        # The word is "<path>." (it ends a sentence): print the path.
        reason = self.verdicts(self.run_tool())["in-use-string"][1]
        self.assertRegex(reason, r"uses " + re.escape(str(self.trees / "in-use-string")) + r"(?![.\w/])")

    def test_an_error_checking_one_worktree_keeps_it_and_carries_on(self) -> None:
        tool, _ = self.in_process()
        real_survey = tool.survey
        target = str(self.trees / "decoy")

        def survey_failing_for_decoy(here, only=None):
            if only == target:
                raise tool.PruneError("could not check")
            return real_survey(here, only=only)

        with mock.patch.object(tool, "survey", survey_failing_for_decoy):
            code, out = self.run_in_process(tool, ["--no-fetch", "--apply"])
        self.assertEqual(code, 1)
        self.assertTrue((self.trees / "decoy").is_dir())
        self.assertFalse((self.trees / "merged").exists())
        self.assertFalse((self.trees / "squashed").exists())

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
