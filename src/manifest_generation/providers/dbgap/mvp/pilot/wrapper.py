"""Bounded, process-tree-accounted runner for the MVP metric-resolution pilot.

Hardened against process escape and pipe suspension:

* the wrapper becomes a **child subreaper** (``prctl``) so orphaned descendants
  reparent to it; if that is unavailable the run fails closed rather than
  claiming success;
* the child is held at a **startup barrier** until the resource monitor is
  running, so the resolver cannot load the reference before accounting starts;
* child stdio goes to files, never a pipe, so a detached grandchild holding the
  pipe cannot suspend the bounded wait;
* after the root exits, the whole session group and any reparented children are
  killed and reaped; an outcome is ``ok`` only when nothing escaped.

The core owns its own JSON writes. The registry uses exclusive,
containment-checked writes only for its own selection and receipt files.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from manifest_generation import REPO_ROOT, safe_write
from manifest_generation import metadata_content_sha as metadata_content

ANCESTRY_REFERENCE = Path("/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz")
ANCESTRY_GROUPS = Path("/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv")
QC_PANEL = REPO_ROOT / "resources" / "reference-resources" / "qc-panel-hg38" / "qc_panel.tsv"
REFERENCE_VERSION = "ukb-ancestry-mixture-hg38"

PILOT_CORE_COMMIT = "4e922566d7fa4c13a11be3637069da63e48be28c"

WALL_LIMIT_MAX_S = 7200.0
RSS_LIMIT_MAX_MB = 65536.0
MAX_WORKERS = 2
CPU_SET = (0, 1, 2, 3)
ROW_CAP = 200000
ANCESTRY_CAP = 50000
EVIDENCE_SAMPLE = 20000
POLL_INTERVAL_S = 0.2

_PR_SET_CHILD_SUBREAPER = 36


class WrapperError(ValueError):
    """The pilot cannot proceed under the reviewed constraints."""


def _set_subreaper() -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise WrapperError(f"prctl(PR_SET_CHILD_SUBREAPER) failed: errno {errno}")


def allowed_cpus() -> frozenset[int]:
    try:
        return frozenset(os.sched_getaffinity(0))
    except (AttributeError, OSError) as exc:  # pragma: no cover - platform
        raise WrapperError(f"cannot determine allowed CPU affinity: {exc}") from exc


@dataclass(frozen=True)
class ResourceLimits:
    wall_limit_s: float = WALL_LIMIT_MAX_S
    rss_limit_mb: float = RSS_LIMIT_MAX_MB
    max_workers: int = MAX_WORKERS
    cpu_set: tuple[int, ...] = CPU_SET


def validate_limits(limits: ResourceLimits) -> tuple[int, ...]:
    """Reject NaN/∞/non-positive/over-authorized limits; return the CPU set."""
    for name, value, maximum in (
        ("wall_limit_s", limits.wall_limit_s, WALL_LIMIT_MAX_S),
        ("rss_limit_mb", limits.rss_limit_mb, RSS_LIMIT_MAX_MB),  # 16 GiB: the ancestry reference decompresses to ~1.35 GiB and is fork-shared across the parent and workers, so an aggregate (shared-page-double-counted) ceiling must be several times larger.
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise WrapperError(f"{name} must be a finite number")
        if value <= 0:
            raise WrapperError(f"{name} must be positive")
        if value > maximum:
            raise WrapperError(f"{name}={value} exceeds the authorized maximum {maximum}")
    if not isinstance(limits.max_workers, int) or isinstance(limits.max_workers, bool):
        raise WrapperError("max_workers must be an integer")
    if limits.max_workers < 1 or limits.max_workers > MAX_WORKERS:
        raise WrapperError(f"max_workers must be in [1, {MAX_WORKERS}]")
    allowed = allowed_cpus()
    requested = set(limits.cpu_set)
    usable = requested & allowed
    if not usable:
        raise WrapperError(
            f"requested CPU set {sorted(requested)} has no intersection with allowed {sorted(allowed)}"
        )
    if len(usable) > 4:
        raise WrapperError("at most 4 CPUs may be pinned")
    return tuple(sorted(usable))


class RunOutcome:
    def __init__(
        self,
        *,
        returncode: int,
        wall_s: float,
        peak_rss_mb: float,
        timed_out: bool,
        rss_breached: bool,
        escaped_pids: Sequence[int],
        stdout_path: Path,
        stderr_path: Path,
    ) -> None:
        self.returncode = returncode
        self.wall_s = wall_s
        self.peak_rss_mb = peak_rss_mb
        self.timed_out = timed_out
        self.rss_breached = rss_breached
        self.escaped_pids = tuple(sorted(escaped_pids))
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.rss_breached
            and not self.escaped_pids
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "wall_s": self.wall_s,
            "peak_rss_mb": self.peak_rss_mb,
            "timed_out": self.timed_out,
            "rss_breached": self.rss_breached,
            "escaped_pids": list(self.escaped_pids),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


def assert_protected(path: Path, protected_roots: Sequence[Path]) -> None:
    resolved = Path(path).resolve()
    for root in protected_roots:
        root_resolved = Path(root).resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            raise WrapperError(f"output {resolved} is inside protected root {root_resolved}")


def preflight_output_root(
    out_dir: Path | str,
    *,
    protected_roots: Sequence[Path | str] = (),
    registry_roots: Sequence[Path | str] = (),
) -> Path:
    path = Path(out_dir)
    resolved = path.resolve()
    if path.exists() or path.is_symlink():
        raise WrapperError(f"refusing to reuse an existing output root: {path}")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise WrapperError(f"refusing a symlinked output path: {ancestor}")
    if "stores" in resolved.parts:
        raise WrapperError(f"refusing an output under a 'stores' component: {path}")
    assert_protected(path, [Path(root) for root in (*protected_roots, *registry_roots)])
    path.mkdir(parents=True, exist_ok=False)
    return resolved


@dataclass(frozen=True)
class _ProcEntry:
    pid: int
    ppid: int
    pgid: int
    state: str
    starttime: int


def _parse_proc_stat(pid: int, text: str) -> _ProcEntry | None:
    """Parse the fields the accounting needs from one ``/proc/<pid>/stat``."""
    try:
        fields = text.rsplit(")", 1)[1].split()
        return _ProcEntry(
            pid=pid,
            state=fields[0],
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            starttime=int(fields[19]),
        )
    except (IndexError, ValueError):
        return None


class ProcSnapshot:
    """One host-wide ``/proc`` scan reused for a whole accounting tick.

    The previous accounting walked ``/proc`` once per tracked member (and twice
    more per member for the ppid map), so a tick cost ``O(host_pids * members)``
    and starved the resolver. One capture reads each ``/proc/<pid>/stat`` exactly
    once and indexes children in memory, so group membership, transitive and
    reparented descendants, starttimes and RSS all come from the same snapshot
    and the cost is linear in the host process count, independent of the tree
    size. ``rss_*`` reads ``/proc/<pid>/status`` once per resolved member.
    """

    __slots__ = ("entries", "_children")

    def __init__(self, entries: dict[int, _ProcEntry]) -> None:
        self.entries = entries
        children: dict[int, list[int]] = {}
        for entry in entries.values():
            children.setdefault(entry.ppid, []).append(entry.pid)
        self._children = children

    @classmethod
    def capture(cls) -> "ProcSnapshot":
        entries: dict[int, _ProcEntry] = {}
        proc = Path("/proc")
        if not proc.is_dir():
            return cls(entries)
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = entry.joinpath("stat").read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = _parse_proc_stat(int(entry.name), text)
            if parsed is not None:
                entries[parsed.pid] = parsed
        return cls(entries)

    def children_of(self, pid: int) -> list[int]:
        return self._children.get(pid, [])

    def group_members(self, pgid: int) -> set[int]:
        return {
            entry.pid
            for entry in self.entries.values()
            if entry.pgid == pgid and entry.state != "Z"
        }

    def descendants(self, roots: Iterable[int]) -> set[int]:
        """Roots plus every ppid-reachable live descendant, from this snapshot."""
        members = set(roots)
        frontier = list(members)
        while frontier:
            for child in self._children.get(frontier.pop(), ()):
                if child in members:
                    continue
                entry = self.entries.get(child)
                if entry is not None and entry.state == "Z":
                    continue
                members.add(child)
                frontier.append(child)
        return members

    def tree_pids(self, pgid: int, self_pid: int) -> set[int]:
        """The session group plus any live descendant reparented to the subreaper."""
        roots = self.group_members(pgid) | {
            pid
            for pid in self.children_of(self_pid)
            if self.entries[pid].state != "Z"
        }
        return self.descendants(roots)

    def rss_mb(self, pids: Iterable[int]) -> float:
        total_kb = 0.0
        for pid in pids:
            try:
                status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
            except OSError:
                continue
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += float(line.split()[1])
                    break
        return total_kb / 1024.0


def descendants_of(pid: int, sample: ProcSnapshot | None = None) -> set[int]:
    snapshot = sample if sample is not None else ProcSnapshot.capture()
    return snapshot.descendants({pid})


def _reap_orphans() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def descendants_of_group(pgid: int, sample: ProcSnapshot | None = None) -> set[int]:
    snapshot = sample if sample is not None else ProcSnapshot.capture()
    return snapshot.group_members(pgid)


def process_tree_rss_mb(pgid: int) -> float:
    """Aggregate RSS over the process group and any reparented descendants."""
    sample = ProcSnapshot.capture()
    return sample.rss_mb(sample.tree_pids(pgid, os.getpid()))


class ResourceMonitor:
    def __init__(self, pgid: int, limit_mb: float, interval_s: float = POLL_INTERVAL_S) -> None:
        self.pgid = pgid
        self.limit_mb = limit_mb
        self.interval_s = interval_s
        self.peak_mb = 0.0
        self.breached = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            current = process_tree_rss_mb(self.pgid)
            self.peak_mb = max(self.peak_mb, current)
            if current > self.limit_mb:
                self.breached = True
                return
            self._stop.wait(self.interval_s)


_LAUNCHER = (
    "import os,sys\n"
    "fd=int(sys.argv[1]); argv=sys.argv[2:]\n"
    "try:\n"
    "    os.sched_setaffinity(0, {cpus})\n"
    "except (AttributeError, OSError) as exc:\n"
    "    os.write(2, ('affinity error: %s\\n' % exc).encode()); os._exit(1)\n"
    "if os.read(fd, 1) != b'1': os._exit(1)\n"
    "os.execvp(argv[0], argv)\n"
)


def run_bounded(
    argv: Sequence[str],
    *,
    limits: ResourceLimits,
    stdout_path: Path,
    stderr_path: Path,
    extra_env: Mapping[str, str] | None = None,
) -> RunOutcome:
    """Run ``argv`` in a subreaper-managed session under wall/RSS/CPU bounds."""
    cpu_set = validate_limits(limits)
    _set_subreaper()

    child_env = dict(os.environ)
    if extra_env:
        child_env.update(extra_env)
    # BLAS/OpenMP pinned last: a caller cannot override them.
    child_env.update(
        {
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
    )

    barrier_r, barrier_w = os.pipe()
    launcher = (
        sys.executable,
        "-c",
        _LAUNCHER.format(cpus=set(cpu_set)),
        str(barrier_r),
        *argv,
    )
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(
            list(launcher),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=child_env,
            start_new_session=True,
            pass_fds=(barrier_r,),
        )
        pid = process.pid
        pgid = os.getpgid(pid)
        monitor = ResourceMonitor(pgid, limits.rss_limit_mb)
        tracker = ProcessTracker(pgid)
        # Both accounting threads are running before the launcher releases the
        # resolver past the barrier, so no reference load happens unaccounted.
        monitor.start()
        tracker.start()
        os.close(barrier_r)
        os.write(barrier_w, b"1")
        os.close(barrier_w)

        timed_out = False
        try:
            while True:
                try:
                    returncode = process.wait(timeout=POLL_INTERVAL_S)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - start > limits.wall_limit_s:
                        timed_out = True
                    if timed_out or monitor.breached:
                        _terminate_all(pgid)
                        returncode = process.wait()
                        break
        finally:
            monitor.stop()
    escaped = set(_terminate_all(pgid))
    tracker.stop()
    escaped |= tracker.survivors()
    wall = time.monotonic() - start
    return RunOutcome(
        returncode=returncode,
        wall_s=wall,
        peak_rss_mb=monitor.peak_mb,
        timed_out=timed_out,
        rss_breached=monitor.breached,
        escaped_pids=escaped,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


class ProcessTracker:
    """Persist (pid, starttime) for observed descendants during a run.

    PPID membership alone loses a descendant after its parent dies and it
    reparents; recording the starttime lets a survivor be identified by
    identity even if the pid was reused.
    """

    def __init__(self, pgid: int, interval_s: float = POLL_INTERVAL_S) -> None:
        self.pgid = pgid
        self.interval_s = interval_s
        self.seen: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)

    def _snapshot(self, sample: ProcSnapshot | None = None) -> None:
        snapshot = sample if sample is not None else ProcSnapshot.capture()
        for pid in snapshot.tree_pids(self.pgid, os.getpid()):
            entry = snapshot.entries.get(pid)
            if entry is not None and entry.state != "Z":
                self.seen[pid] = entry.starttime

    def _run(self) -> None:
        while not self._stop.is_set():
            self._snapshot()
            self._stop.wait(self.interval_s)

    def survivors(self) -> set[int]:
        sample = ProcSnapshot.capture()
        self._snapshot(sample)
        alive: set[int] = set()
        for pid, start in self.seen.items():
            entry = sample.entries.get(pid)
            if entry is not None and entry.starttime == start and entry.state != "Z":
                alive.add(pid)
        return alive


def _terminate_all(pgid: int, grace_s: float = 3.0) -> set[int]:
    """Kill the group and any reparented descendants; return survivors.

    The wrapper is a child subreaper, so an orphaned grandchild reparents to it
    instead of escaping to init; the loop kills that child too. SIGKILL is used
    directly because the run is already being stopped (wall timeout or RSS
    breach).
    """
    me = os.getpid()
    deadline = time.monotonic() + grace_s
    while True:
        _reap_orphans()
        targets = ProcSnapshot.capture().tree_pids(pgid, me)
        if not targets:
            return set()
        for pid in sorted(targets):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if time.monotonic() > deadline:
            time.sleep(0.5)
            _reap_orphans()
            return ProcSnapshot.capture().tree_pids(pgid, me)
        time.sleep(0.1)


def build_resolver_argv(
    *,
    resolver_executable: str,
    manifest_path: Path,
    records_dir: Path,
    limits: ResourceLimits,
) -> list[str]:
    return [
        resolver_executable,
        "resolve-analyses",
        str(manifest_path),
        str(records_dir),
        "--ancestry-reference",
        str(ANCESTRY_REFERENCE),
        "--ancestry-groups",
        str(ANCESTRY_GROUPS),
        "--default-source-reader-capability",
        "opengwasdb.gwas-ssf",
        "--maf-floor",
        "0.01",
        "--tau",
        "0.5",
        "--delta",
        "0.2",
        "--n-min",
        "5000",
        "--residual-max",
        "0.06",
        "--orientation-flip-r",
        "-0.5",
        "--evidence-sample",
        str(EVIDENCE_SAMPLE),
        "--max-ancestry-sites",
        str(ANCESTRY_CAP),
        "--max-rows",
        str(ROW_CAP),
        "--n-workers",
        str(limits.max_workers),
        "--resume",
        "--largest-first",
        "--reference-version",
        REFERENCE_VERSION,
    ]


__all__ = [
    "ANCESTRY_CAP",
    "ANCESTRY_GROUPS",
    "ANCESTRY_REFERENCE",
    "CPU_SET",
    "EVIDENCE_SAMPLE",
    "MAX_WORKERS",
    "PILOT_CORE_COMMIT",
    "QC_PANEL",
    "REFERENCE_VERSION",
    "ROW_CAP",
    "ProcessTracker",
    "ResourceLimits",
    "ResourceMonitor",
    "RunOutcome",
    "WrapperError",
    "allowed_cpus",
    "assert_protected",
    "build_resolver_argv",
    "descendants_of",
    "descendants_of_group",
    "preflight_output_root",
    "process_tree_rss_mb",
    "run_bounded",
    "validate_limits",
]
