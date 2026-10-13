"""Bounded, process-tree-accounted runner for the MVP metric-resolution pilot.

`plan` is metadata-only and the default; `run` requires an explicit `--execute`,
the approved immutable metadata SHA and a parent start receipt. The wrapper
starts its resource monitor *before* the resolver (so the ancestry-reference
load is covered), pins CPU/BLAS, enforces a whole-pool wall timeout and an
aggregate process-tree RSS ceiling, and kills the entire process group --
including grandchildren -- on breach, re-checking that no descendant escaped.

The core owns its own JSON writes; the registry uses exclusive
containment-checked writes only for its selection and receipt files.
"""

from __future__ import annotations

import importlib.util
import json
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

#: Reference/panel paths the pilot is authorised to have the public reader load.
ANCESTRY_REFERENCE = Path("/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz")
ANCESTRY_GROUPS = Path("/data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv")
QC_PANEL = REPO_ROOT / "resources" / "reference-resources" / "qc-panel-hg38" / "qc_panel.tsv"
REFERENCE_VERSION = "ukb-ancestry-mixture-hg38"

#: Proposed resource limits (the wrapper implements them for the pilot).
WALL_LIMIT_S = 1800.0
RSS_LIMIT_MB = 4096.0
MAX_WORKERS = 2
CPU_SET = (0, 1, 2, 3)
ROW_CAP = 200000
ANCESTRY_CAP = 50000
EVIDENCE_SAMPLE = 20000
POLL_INTERVAL_S = 0.2


class WrapperError(ValueError):
    """The pilot cannot proceed under the reviewed constraints."""


@dataclass(frozen=True)
class ResourceLimits:
    wall_limit_s: float = WALL_LIMIT_S
    rss_limit_mb: float = RSS_LIMIT_MB
    max_workers: int = MAX_WORKERS
    cpu_set: tuple[int, ...] = CPU_SET


@dataclass
class RunOutcome:
    returncode: int
    wall_s: float
    peak_rss_mb: float
    timed_out: bool = False
    rss_breached: bool = False
    escaped_pids: tuple[int, ...] = ()
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.rss_breached
            and not self.escaped_pids
        )


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
    """Create a fresh output root, refusing aliases, protected roots and stores/."""
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


def _pgid_members(pgid: int) -> set[int]:
    members: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return members
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # pid (comm) state ppid pgrp ...
        try:
            fields = stat.rsplit(")", 1)[1].split()
            pgrp = int(fields[2])
        except (IndexError, ValueError):
            continue
        if pgrp == pgid:
            members.add(int(entry.name))
    return members


def _tree_members(roots: set[int]) -> set[int]:
    """All descendants of ``roots`` (one pass over /proc)."""
    parents: dict[int, int] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return set(roots)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            fields = stat.rsplit(")", 1)[1].split()
            parents[int(entry.name)] = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
    members = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if pid not in members and ppid in members:
                members.add(pid)
                changed = True
    return members


def process_tree_rss_mb(pgid: int) -> float:
    """Aggregate RSS of the process group's tree, in MB.

    Summing ``VmRSS`` double-counts shared pages, so the value is a conservative
    over-estimate (documented in the receipt). It is a ceiling, not an exact
    resident measurement.
    """
    total_kb = 0.0
    for pid in _tree_members(_pgid_members(pgid)):
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
    """Poll a process group's aggregate RSS until stopped or breached."""

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
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            current = process_tree_rss_mb(self.pgid)
            self.peak_mb = max(self.peak_mb, current)
            if current > self.limit_mb:
                self.breached = True
                return
            self._stop.wait(self.interval_s)


def _preexec(cpu_set: Sequence[int]):
    def _apply() -> None:
        try:
            os.sched_setaffinity(0, set(cpu_set))
        except (AttributeError, OSError):
            pass

    return _apply


def run_bounded(
    argv: Sequence[str],
    *,
    limits: ResourceLimits,
    env: Mapping[str, str] | None = None,
) -> RunOutcome:
    """Run ``argv`` in its own session under the wall/RSS/CPU bounds."""
    child_env = dict(os.environ)
    child_env.update(
        {
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
    )
    if env:
        child_env.update(env)
    start = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
        start_new_session=True,
        preexec_fn=_preexec(limits.cpu_set),
    )
    pgid = os.getpgid(process.pid)
    monitor = ResourceMonitor(pgid, limits.rss_limit_mb)
    # The monitor starts before the child can load the reference panel.
    monitor.start()
    timed_out = False
    stdout = stderr = ""
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=POLL_INTERVAL_S)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - start > limits.wall_limit_s:
                    timed_out = True
                if timed_out or monitor.breached:
                    _terminate_group(pgid)
                    stdout, stderr = process.communicate()
                    break
    finally:
        monitor.stop()
    if monitor.breached:
        _terminate_group(pgid)
    escaped = _terminate_group(pgid)
    wall = time.monotonic() - start
    return RunOutcome(
        returncode=process.returncode if process.returncode is not None else -1,
        wall_s=wall,
        peak_rss_mb=monitor.peak_mb,
        timed_out=timed_out,
        rss_breached=monitor.breached,
        escaped_pids=tuple(sorted(escaped)),
        stdout_tail=(stdout or "")[-2000:],
        stderr_tail=(stderr or "")[-2000:],
    )


def _terminate_group(pgid: int, grace_s: float = 3.0) -> set[int]:
    """Kill the whole group and its descendants; return any that escaped."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not _tree_members(_pgid_members(pgid)):
                return set()
            time.sleep(0.1)
    remaining = _tree_members(_pgid_members(pgid))
    for pid in sorted(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.2)
    return _tree_members(_pgid_members(pgid))


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


def _atomic_json(path: Path, data: Mapping, root: Path, *, overwrite: bool = False) -> None:
    try:
        safe_write.atomic_write_text(
            path, json.dumps(data, indent=2, sort_keys=True) + "\n", root, overwrite=overwrite
        )
    except safe_write.SafeWriteError as exc:
        raise WrapperError(str(exc)) from exc


__all__ = [
    "ANCESTRY_CAP",
    "ANCESTRY_GROUPS",
    "ANCESTRY_REFERENCE",
    "CPU_SET",
    "EVIDENCE_SAMPLE",
    "MAX_WORKERS",
    "QC_PANEL",
    "REFERENCE_VERSION",
    "ROW_CAP",
    "RSS_LIMIT_MB",
    "ResourceLimits",
    "ResourceMonitor",
    "RunOutcome",
    "WALL_LIMIT_S",
    "WrapperError",
    "assert_protected",
    "build_resolver_argv",
    "preflight_output_root",
    "process_tree_rss_mb",
    "run_bounded",
]
