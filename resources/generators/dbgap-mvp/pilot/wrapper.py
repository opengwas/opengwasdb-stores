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
import importlib.util
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
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[4]
_LIB = REPO_ROOT / "resources" / "generators" / "lib"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _LIB / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


safe_write = _load("ogstores_safe_write", "safe_write.py")
metadata_content = _load("ogstores_metadata_content_sha", "metadata_content_sha.py")

ANCESTRY_REFERENCE = Path("/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz")
ANCESTRY_GROUPS = Path("/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv")
QC_PANEL = REPO_ROOT / "resources" / "reference-resources" / "qc-panel-hg38" / "qc_panel.tsv"
REFERENCE_VERSION = "ukb-ancestry-mixture-hg38"

PILOT_CORE_COMMIT = "4e922566d7fa4c13a11be3637069da63e48be28c"

WALL_LIMIT_MAX_S = 1800.0
RSS_LIMIT_MAX_MB = 4096.0
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
        ("rss_limit_mb", limits.rss_limit_mb, RSS_LIMIT_MAX_MB),
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


def _proc_stat(pid: int) -> tuple[int, int] | None:
    """Return ``(ppid, starttime)`` for a live pid, or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _procs_by_ppid() -> dict[int, list[int]]:
    mapping: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return mapping
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        parsed = _proc_stat(int(entry.name))
        if parsed is None or _proc_state(int(entry.name)) == "Z":
            continue
        mapping.setdefault(parsed[0], []).append(int(entry.name))
    return mapping


def descendants_of(pid: int) -> set[int]:
    by_ppid = _procs_by_ppid()
    members = {pid}
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        for child in by_ppid.get(current, []):
            if child not in members:
                members.add(child)
                frontier.append(child)
    return members


def _proc_state(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def _reap_orphans() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def descendants_of_group(pgid: int) -> set[int]:
    members: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return members
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = entry.joinpath("stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                members.add(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return members


def process_tree_rss_mb(pgid: int) -> float:
    """Aggregate RSS over the process group and any reparented descendants."""
    pids = set(descendants_of_group(pgid))
    for pid in list(pids):
        pids |= descendants_of(pid)
    pids |= {pid for pid in _procs_by_ppid().get(os.getpid(), [])}
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
        # The monitor is running before the launcher releases the resolver past
        # the barrier, so no reference load happens before accounting starts.
        monitor.start()
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
    escaped = _terminate_all(pgid)
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
        targets = descendants_of_group(pgid) | {
            pid for pid in _procs_by_ppid().get(me, []) if pid != 0
        }
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
            return descendants_of_group(pgid) | {
                pid for pid in _procs_by_ppid().get(me, []) if pid != 0
            }
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
        "--extraction-panel",
        str(QC_PANEL),
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
