#!/usr/bin/env python3
"""List this repository's worktrees and remove the ones that are safe to lose
(#312). Run via:

  pixi run -e dev worktree-prune             # dry run: one line per worktree
  pixi run -e dev worktree-prune --sizes     # ... with each `.pixi` size
  pixi run -e dev worktree-prune --apply     # remove the removable ones

A worktree is *removable* only if all of these hold; anything else is *kept*,
with the reason:

* it is not the primary checkout, and not the worktree the command runs from;
* it is not locked (`git worktree lock`);
* its HEAD is merged into `origin/dev` or `origin/main`: an ancestor of one of
  them, or `git cherry` shows no `+` commit (squash-merged);
* `git status --porcelain` is empty: no tracked changes, no untracked files
  that are not ignored;
* no running process uses it: a process's cwd, its executable, or any word of
  its command line is under the worktree's path.

Removal is `git worktree remove` (`--force` only once the worktree has just
been re-checked as clean, so it can only discard ignored files), then `git
worktree prune`. A branch is never deleted: removing a worktree discards a
view, not commits.

Out of scope: an ignored file that cannot be regenerated (a scratch note in a
`.gitignore`d directory, a local `.env`) is not detected, and goes with the
worktree. The process scan reads `/proc`; see `scan_processes` for what it does
with processes it may not inspect.

Standard library and `git` only. Exits non-zero on errors (a failed fetch or
removal, a repository it cannot read), never because something was kept.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MERGE_TARGETS = ("origin/dev", "origin/main")

REMOVABLE = "removable"
KEPT = "kept"


class PruneError(Exception):
    pass


@dataclass
class Worktree:
    path: str
    head: str = ""
    branch: str = ""  # short name, "" when detached
    bare: bool = False
    locked: str | None = None  # the lock reason ("" when none was given)
    prunable: bool = False  # its directory is gone


@dataclass
class Verdict:
    worktree: Worktree
    verdict: str
    reason: str
    size: int | None = None


@dataclass
class ProcessScan:
    """Who uses what, and what could not be inspected."""
    uses: list[tuple[int, str, str]] = field(default_factory=list)  # (pid, command, path used)
    uninspectable: list[int] = field(default_factory=list)  # own processes with no readable command line
    foreign_unreadable: int = 0  # processes whose cwd and executable are hidden: command line only


def git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise PruneError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def list_worktrees() -> list[Worktree]:
    out = git("worktree", "list", "--porcelain").stdout
    worktrees: list[Worktree] = []
    for block in out.split("\n\n"):
        current: Worktree | None = None
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                current = Worktree(path=value)
            elif current is None:
                continue
            elif key == "HEAD":
                current.head = value
            elif key == "branch":
                current.branch = value.removeprefix("refs/heads/")
            elif key == "bare":
                current.bare = True
            elif key == "locked":
                current.locked = value
            elif key == "prunable":
                current.prunable = True
        if current is not None:
            worktrees.append(current)
    return worktrees


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _mentions(word: str, root: str) -> bool:
    """`word` holds `root` as a path: `/x/wt`, `/x/wt/a`, `--p=/x/wt/a`, not `/x/wt-2`."""
    start = word.find(root)
    while start != -1:
        end = start + len(root)
        if end == len(word) or not (word[end].isalnum() or word[end] in "_-."):
            return True
        start = word.find(root, start + 1)
    return False


def scan_processes(roots: list[str]) -> dict[str, ProcessScan]:
    """For each root, the processes using it: cwd, executable, or a word of the
    command line under it.

    A process of ours whose command line cannot be read is listed in
    `uninspectable`, and the caller keeps every worktree ("can't tell" is never
    "unused"). A process whose cwd and executable `/proc` hides from us (other
    users', and privilege-separated ones such as an `sshd` session, which show
    our uid) is checked by its command line alone and counted in
    `foreign_unreadable`: keeping everything for them would keep everything on
    any shared host. So a worktree used only as the cwd or executable of such
    a process is not detected. A process that exits mid-scan is gone, not
    unreadable.
    """
    roots = list(dict.fromkeys(roots))
    scans = {root: ProcessScan() for root in roots}
    me = os.getuid()
    unreadable = 0
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError as exc:
        raise PruneError(f"cannot read /proc: {exc}") from exc
    for pid in pids:
        base = f"/proc/{pid}"
        try:
            owner = os.stat(base).st_uid
        except OSError:
            continue  # exited
        mine = owner == me
        try:
            with open(f"{base}/cmdline", "rb") as fh:
                words = [w.decode(errors="replace") for w in fh.read().split(b"\0") if w]
        except FileNotFoundError:
            continue
        except OSError:
            words = []
            if mine:
                for scan in scans.values():
                    scan.uninspectable.append(pid)
                continue
        paths: list[str] = []
        hidden = False
        for link in ("cwd", "exe"):
            try:
                paths.append(os.readlink(f"{base}/{link}").removesuffix(" (deleted)"))
            except FileNotFoundError:
                pass  # a kernel thread, an exited process, or an executable that no longer exists
            except OSError:
                hidden = True
        if hidden:
            unreadable += 1
        command = " ".join(words)[:90] or f"[{pid}]"
        for root in roots:
            hit = next((p for p in paths if _under(p, root)), None)
            if hit is None:
                hit = next((w for w in words if _mentions(w, root)), None)
            if hit is not None:
                scans[root].uses.append((pid, command, hit))
    for scan in scans.values():
        scan.foreign_unreadable = unreadable
    return scans


def unmerged_commits(head: str, cwd: str) -> int | None:
    """None when `head` is merged into a target, else the fewest commits it has
    that no target has (by patch, so a squash merge counts as merged)."""
    fewest: int | None = None
    present = 0
    for ref in MERGE_TARGETS:
        if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd, check=False).returncode != 0:
            continue
        present += 1
        if git("merge-base", "--is-ancestor", head, ref, cwd=cwd, check=False).returncode == 0:
            return None
        cherry = git("cherry", ref, head, cwd=cwd).stdout.splitlines()
        ahead = sum(1 for line in cherry if line.startswith("+"))
        if ahead == 0:
            return None
        fewest = ahead if fewest is None else min(fewest, ahead)
    if not present:
        raise PruneError(f"neither {' nor '.join(MERGE_TARGETS)} exists; fetch origin first")
    assert fewest is not None
    return fewest


def pixi_size(path: str) -> int | None:
    """Disk bytes under `<path>/.pixi`, a hard-linked file once."""
    root = os.path.join(path, ".pixi")
    if not os.path.isdir(root):
        return None
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
    return total


def human(size: int | None) -> str:
    if size is None:
        return "no .pixi"
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    raise AssertionError


def classify(worktrees: list[Worktree], here: str, scans: dict[str, ProcessScan]) -> list[Verdict]:
    verdicts = []
    primary = os.path.realpath(worktrees[0].path) if worktrees and not worktrees[0].bare else None
    for index, wt in enumerate(worktrees):
        real = os.path.realpath(wt.path)
        verdict = _classify_one(wt, index == 0, real, primary, here, scans)
        verdicts.append(verdict)
    return verdicts


def _classify_one(wt: Worktree, is_first: bool, real: str, primary: str | None, here: str,
                  scans: dict[str, ProcessScan]) -> Verdict:
    def kept(reason: str) -> Verdict:
        return Verdict(wt, KEPT, reason)

    if wt.bare:
        return kept("bare repository")
    if is_first or real == primary:
        return kept("primary checkout")
    if real == here:
        return kept("the worktree this command runs from")
    if wt.locked is not None:
        return kept("locked" + (f": {wt.locked}" if wt.locked else ""))
    if not wt.head:
        return kept("HEAD unknown")
    ahead = unmerged_commits(wt.head, here)
    if ahead is not None:
        return kept(f"not merged: {ahead} commit{'s' if ahead != 1 else ''} not in {' or '.join(MERGE_TARGETS)}")
    if wt.prunable or not os.path.isdir(wt.path):
        return Verdict(wt, REMOVABLE, "merged; directory is gone, `git worktree prune` forgets it")
    status = git("status", "--porcelain", cwd=wt.path, check=False)
    if status.returncode != 0:
        return kept(f"cannot tell if dirty: {status.stderr.strip()}")
    if status.stdout.strip():
        lines = status.stdout.splitlines()
        untracked = sum(1 for line in lines if line.startswith("??"))
        return kept(f"dirty: {len(lines) - untracked} changed, {untracked} untracked")
    scan = scans[wt.path]
    if scan.uninspectable:
        pids = ", ".join(map(str, scan.uninspectable[:5]))
        return kept(f"cannot tell if in use: could not read the command line of your processes {pids}")
    if scan.uses:
        shown = "; ".join(f"pid {pid} `{cmd}`" for pid, cmd, _ in scan.uses[:3])
        more = f" (+{len(scan.uses) - 3} more)" if len(scan.uses) > 3 else ""
        return kept(f"in use: {shown}{more}")
    return Verdict(wt, REMOVABLE, "merged, clean, unlocked, unused")


def survey(here: str) -> tuple[list[Verdict], int]:
    worktrees = list_worktrees()
    scans = scan_processes([wt.path for wt in worktrees] + [os.path.realpath(wt.path) for wt in worktrees])
    # A process may name either spelling of a path: merge the two scans.
    merged: dict[str, ProcessScan] = {}
    for wt in worktrees:
        a, b = scans[wt.path], scans[os.path.realpath(wt.path)]
        seen = {u[0] for u in a.uses}
        merged[wt.path] = ProcessScan(
            uses=a.uses + [u for u in b.uses if u[0] not in seen],
            uninspectable=a.uninspectable,
            foreign_unreadable=a.foreign_unreadable,
        )
    foreign = next(iter(scans.values())).foreign_unreadable if scans else 0
    return classify(worktrees, here, merged), foreign


def remove(wt: Worktree) -> None:
    if wt.prunable or not os.path.isdir(wt.path):
        return  # `git worktree prune` does it
    if git("worktree", "remove", wt.path, check=False).returncode == 0:
        return
    # Status was clean: what is left is ignored content, or a submodule.
    if git("status", "--porcelain", cwd=wt.path).stdout.strip():
        raise PruneError(f"{wt.path} became dirty; kept")
    git("worktree", "remove", "--force", wt.path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="remove the removable worktrees (default: dry run)")
    parser.add_argument("--sizes", action="store_true", help="show each worktree's .pixi size")
    parser.add_argument("--no-fetch", action="store_true", help="do not fetch origin first")
    args = parser.parse_args(argv)

    try:
        here = os.path.realpath(git("rev-parse", "--show-toplevel").stdout.strip())
        if not args.no_fetch:
            fetched = git("fetch", "origin", check=False)
            if fetched.returncode != 0:
                raise PruneError(f"git fetch origin failed (use --no-fetch to go on with what is local): "
                                 f"{fetched.stderr.strip()}")
        verdicts, foreign_unreadable = survey(here)
    except PruneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.sizes:
        for v in verdicts:
            v.size = pixi_size(v.worktree.path)
    for v in verdicts:
        branch = v.worktree.branch or f"(detached {v.worktree.head[:9]})"
        size = f"  [{human(v.size)}]" if args.sizes else ""
        print(f"{v.worktree.path}  {branch}  {v.verdict.upper()}  {v.reason}{size}")
    n_rm = sum(v.verdict == REMOVABLE for v in verdicts)
    print(f"{n_rm} removable, {len(verdicts) - n_rm} kept of {len(verdicts)}")
    if foreign_unreadable:
        print(f"note: {foreign_unreadable} processes hide their cwd and executable; "
              "only their command lines were checked")

    errors = 0
    if args.apply:
        # Re-check just before removing: a process may have started since the survey.
        fresh = {v.worktree.path: v for v in survey(here)[0]}
        for v in verdicts:
            if v.verdict != REMOVABLE:
                continue
            now = fresh.get(v.worktree.path)
            if now is None or now.verdict != REMOVABLE:
                print(f"kept   {v.worktree.path}: changed since the survey: {now.reason if now else 'gone'}")
                continue
            try:
                remove(v.worktree)
                print(f"removed {v.worktree.path}")
            except PruneError as exc:
                errors += 1
                print(f"error: {exc}", file=sys.stderr)
        try:
            git("worktree", "prune")
        except PruneError as exc:
            errors += 1
            print(f"error: {exc}", file=sys.stderr)
    else:
        print("dry run: nothing removed; --apply removes the removable ones (branches are never deleted)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
