#!/usr/bin/env python3
"""Hermetic process-accounting tests for the bounded pilot wrapper (#221).

A synthetic ``/proc`` lets these tests count exactly how much host enumeration
and per-pid ``/proc/<pid>/stat`` reading one accounting tick performs, without
touching a real process or a real association body.

The contract under test: a tick takes **one** host snapshot and reuses it for
group membership, transitive/reparented-descendant membership, starttimes and
RSS, so the cost is linear in the host process count and independent of how
many processes the tracked tree holds. The same snapshot must still find a
reparented detached grandchild (the escape case) and sum its RSS.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from manifest_generation.providers.dbgap.mvp.pilot import wrapper


def _stat_text(pid: int, entry: dict) -> str:
    """A /proc/<pid>/stat whose rsplit(')') fields match the wrapper's indices."""
    fields = [entry["state"], str(entry["ppid"]), str(entry["pgid"])]
    fields += ["0"] * 16
    fields.append(str(entry["starttime"]))
    return f"{pid} (comm) " + " ".join(fields) + "\n"


def _status_text(pid: int, entry: dict) -> str:
    return f"Name:\tcomm\nVmRSS:\t{entry['rss_kb']} kB\n"


class _FakeNode:
    def __init__(self, fs: "_FakeProcFS", raw: str) -> None:
        self._fs = fs
        self._raw = raw.rstrip("/") or "/"
        self.name = self._raw.split("/")[-1]

    def is_dir(self) -> bool:
        return self._raw == "/proc"

    def iterdir(self):
        if self._raw != "/proc":
            raise NotADirectoryError(self._raw)
        return self._fs.iterdir()

    def joinpath(self, *parts: str) -> "_FakeNode":
        return _FakeNode(self._fs, self._raw + "/" + "/".join(parts))

    def read_text(self, encoding: str | None = None) -> str:
        pieces = self._raw.split("/")
        if len(pieces) != 4 or pieces[1] != "proc":
            raise FileNotFoundError(self._raw)
        pid = int(pieces[2])
        if pieces[3] == "stat":
            return self._fs.read_stat(pid)
        if pieces[3] == "status":
            return self._fs.read_status(pid)
        raise FileNotFoundError(self._raw)


class _FakeProcFS:
    """A synthetic host process table plus read counters."""

    def __init__(self, tree: dict[int, dict]) -> None:
        self.tree = tree
        self.counts = {"iterdir": 0, "stat": 0, "status": 0}

    def path_class(self):
        fs = self

        class _FakePath(_FakeNode):
            def __init__(self, raw, *args, **kwargs) -> None:
                super().__init__(fs, str(raw))

        return _FakePath

    def read_stat(self, pid: int) -> str:
        self.counts["stat"] += 1
        return _stat_text(pid, self.tree[pid])

    def read_status(self, pid: int) -> str:
        self.counts["status"] += 1
        return _status_text(pid, self.tree[pid])

    def iterdir(self):
        self.counts["iterdir"] += 1
        return [_FakeNode(self, f"/proc/{pid}") for pid in sorted(self.tree)]


def _build_tree(host_pids: int, group_children: int) -> dict[int, dict]:
    """A host table with a tracked group, grandchildren and a reparented orphan."""
    me = os.getpid()
    tree: dict[int, dict] = {}
    for offset in range(host_pids):
        tree[1000 + offset] = {
            "state": "S", "ppid": 1, "pgid": 1, "starttime": 10_000 + offset, "rss_kb": 128,
        }
    root = 9000
    group_pgid = 9000
    tree[root] = {
        "state": "S", "ppid": me, "pgid": group_pgid, "starttime": 500_000, "rss_kb": 4096,
    }
    for index in range(group_children):
        child = 9001 + index
        tree[child] = {
            "state": "S", "ppid": root, "pgid": group_pgid,
            "starttime": 500_001 + index, "rss_kb": 2048,
        }
        grandchild = 9500 + index
        tree[grandchild] = {
            "state": "S", "ppid": child, "pgid": group_pgid,
            "starttime": 600_001 + index, "rss_kb": 1024,
        }
    # Reparented detached grandchild: a different pgid, but ppid == the wrapper
    # (the subreaper), so only the "our children" term catches it.
    tree[9800] = {
        "state": "S", "ppid": me, "pgid": 42, "starttime": 700_000, "rss_kb": 8192,
    }
    # A zombie in the group and a foreign process must be ignored.
    tree[9900] = {
        "state": "Z", "ppid": root, "pgid": group_pgid, "starttime": 800_000, "rss_kb": 4096,
    }
    tree[9901] = {
        "state": "S", "ppid": 1, "pgid": 7, "starttime": 800_001, "rss_kb": 4096,
    }
    return tree


def _oracle_pids(tree: dict[int, dict], pgid: int, me: int) -> set[int]:
    members = {
        pid for pid, entry in tree.items()
        if entry["pgid"] == pgid and entry["state"] != "Z"
    }
    pids = set(members)
    pids |= {pid for pid, entry in tree.items() if entry["ppid"] == me}
    changed = True
    while changed:
        changed = False
        for pid, entry in tree.items():
            if entry["state"] == "Z":
                continue
            if entry["ppid"] in pids and pid not in pids:
                pids.add(pid)
                changed = True
    return pids


class _PatchedProc:
    def __init__(self, tree: dict[int, dict]) -> None:
        self.fs = _FakeProcFS(tree)

    def __enter__(self) -> _FakeProcFS:
        self._original = wrapper.Path
        wrapper.Path = self.fs.path_class()
        return self.fs

    def __exit__(self, *exc) -> None:
        wrapper.Path = self._original


class TestProcAccountingLinearCost(unittest.TestCase):
    def test_one_tick_reads_each_host_stat_once(self) -> None:
        tree = _build_tree(host_pids=60, group_children=16)
        with _PatchedProc(tree) as fs:
            wrapper.process_tree_rss_mb(9000)
        # One snapshot: every pid's stat read exactly once, /proc enumerated once.
        self.assertEqual(
            fs.counts["stat"],
            len(tree),
            f"one tick must read one stat per host pid, got {fs.counts}",
        )
        self.assertEqual(
            fs.counts["iterdir"], 1, f"one tick must enumerate /proc once, got {fs.counts}"
        )

    def test_tick_cost_is_independent_of_tree_size(self) -> None:
        small = _build_tree(host_pids=60, group_children=2)
        large = _build_tree(host_pids=60, group_children=24)
        with _PatchedProc(small) as fs_small:
            wrapper.process_tree_rss_mb(9000)
        with _PatchedProc(large) as fs_large:
            wrapper.process_tree_rss_mb(9000)
        # The read count tracks host pids + tracked pids, i.e. exactly one read
        # each, rather than the old N-scans-per-member shape.
        self.assertEqual(fs_small.counts["stat"], len(small))
        self.assertEqual(fs_large.counts["stat"], len(large))
        self.assertEqual(fs_small.counts["iterdir"], 1)
        self.assertEqual(fs_large.counts["iterdir"], 1)

    def test_old_shape_would_fail_the_bound(self) -> None:
        """Guard the regression: with N=60 and k=16 the quadratic shape exceeds it."""
        tree = _build_tree(host_pids=60, group_children=16)
        with _PatchedProc(tree) as fs:
            wrapper.process_tree_rss_mb(9000)
        quadratic_floor = 2 * 60 * 16
        self.assertLess(
            fs.counts["stat"],
            quadratic_floor,
            "the tick still scales with host_pids * group_members",
        )


class TestProcAccountingCorrectness(unittest.TestCase):
    def test_rss_includes_transitive_and_reparented_descendants(self) -> None:
        tree = _build_tree(host_pids=10, group_children=3)
        expected_pids = _oracle_pids(tree, 9000, os.getpid())
        expected_mb = sum(tree[pid]["rss_kb"] for pid in expected_pids) / 1024.0
        # The reparented detached grandchild must be counted.
        self.assertIn(9800, expected_pids)
        with _PatchedProc(tree):
            measured = wrapper.process_tree_rss_mb(9000)
        self.assertAlmostEqual(measured, expected_mb, places=6)

    def test_descendants_of_walks_transitive_children(self) -> None:
        tree = _build_tree(host_pids=5, group_children=2)
        with _PatchedProc(tree):
            found = wrapper.descendants_of(9000)
        self.assertEqual(found, {9000, 9001, 9002, 9500, 9501})

    def test_group_membership_excludes_zombies(self) -> None:
        tree = _build_tree(host_pids=5, group_children=1)
        with _PatchedProc(tree):
            members = wrapper.descendants_of_group(9000)
        self.assertNotIn(9900, members)
        self.assertNotIn(9901, members)
        self.assertIn(9000, members)

    def test_tracker_records_reparented_starttimes_from_one_snapshot(self) -> None:
        tree = _build_tree(host_pids=40, group_children=4)
        with _PatchedProc(tree) as fs:
            tracker = wrapper.ProcessTracker(9000)
            tracker._snapshot()
        self.assertEqual(tracker.seen[9800], tree[9800]["starttime"])
        self.assertEqual(tracker.seen[9500], tree[9500]["starttime"])
        self.assertEqual(fs.counts["iterdir"], 1)
        self.assertEqual(fs.counts["stat"], len(tree))


if __name__ == "__main__":
    unittest.main()
