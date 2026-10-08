"""The supported operator entry point for Phase A releases (#195).

    pixi run release OGS-00005 [OGS-00004 ...] [--config key=value ...]
    pixi run release OGS-00005 --config force=1     replace a published release
    pixi run release-dry OGS-00005                  plan only; writes nothing

It wraps one `snakemake --snakefile workflow/Snakefile` run with the guard no
Snakemake option can switch off:

1. Recovery. A publication a crash left half done (`publication.json`) is
   completed first, and a stray `store.opengwasdb.backup` is put back or
   archived, so no refusal below ever blocks recovery.
2. Preflight. A dry run lists the jobs. A published release that would be
   rebuilt is refused unless it is named with `--config force=1`, and a
   release holding a leftover records snapshot is refused until an operator
   resolves it. Nothing has been written when either refusal happens.
3. Snapshot. Each forced published release's `records/` is copied to
   `records.before-force-<UTC>/`.
4. The run, with `--config release_run=<UTC>` naming this run, and so any
   snapshot it took. Under that token a register job publishes but leaves
   `publication.json` in place.
5. Settlement. Each publication the run left is completed if it needs to be,
   Snakemake's mark that a killed register job's output is incomplete is
   cleared, and only then is the marker removed. A crash at any point before
   that leaves the marker, so the next run's recovery repeats the step. Then a
   failed run's snapshots are restored and a successful run's are archived.

`--resolve-snapshot restore|delete` carries out an operator's resolution of
a leftover snapshot, and clears Snakemake's incomplete marks for that release.

Two runs never touch one release at once. Each run holds an exclusive
`fcntl.flock` on `<artifact-root>/.release-locks/<ID>.lock` for every release
it touches, from recovery through settlement, and passes it to Snakemake's
supervisor. A second run of a locked release is refused as "in progress"; the
kernel drops the lock when the holders die, which is what tells a live run
from a dead one's leftovers. Ctrl-C or SIGTERM stops Snakemake (one SIGINT to
its process group), waits for it, and settles the run as failed.

The options are an allowlist. Anything else, including `--no-hooks` and
`--touch`, is refused before anything is written. Running `snakemake`
directly bypasses this entry point, and with it everything above except the
Snakefile's own `onstart` refusal.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ogstores import paths, register, run

SNAKEFILE: Path = paths.REPO_ROOT / "workflow" / "Snakefile"

# Configuration only the entry point may set.
RESERVED_CONFIG_KEYS: frozenset[str] = frozenset({"release_run"})

# The entry point's own crash boundaries, in run order (#195). The register
# job's are register.PUBLICATION_BOUNDARIES; "marker-removed" is in both,
# because a direct register_release() removes its own marker.
ENTRY_POINT_BOUNDARIES: tuple[str, ...] = (
    "snapshots-taken",
    "before-settle",
    "metadata-cleaned",
    "marker-removed",
)

# Options that would bypass or weaken the guard. They are named in the refusal;
# every other option outside the allowlist is refused too.
UNSAFE_OPTIONS: frozenset[str] = frozenset({
    "--no-hooks",
    "--touch", "-t",
    "--forceall", "-F",
    "--forcerun", "-R",
    "--force", "-f",
    "--nolock",
    "--ignore-incomplete", "--ii",
    "--keep-incomplete",
    "--delete-all-output",
    "--delete-temp-output",
    "--unlock",
    "--cleanup-metadata", "--cm",
    "--snakefile", "-s",
    "--directory", "-d",
    "--force-use-threads",
})
ALLOWED_OPTIONS: str = (
    "release ids or `all`, --config key=value ..., --cores N, --dry-run, "
    "--keep-going, --rerun-incomplete, --resolve-snapshot restore|delete"
)
SNAPSHOT_RESOLUTIONS: frozenset[str] = frozenset({"restore", "delete"})

_TARGET = re.compile(r"\AOGS-\d{5}\Z|\Aall\Z")
_WILDCARDS = re.compile(r"^\s*wildcards:\s*(?P<body>.*)$")
_RELEASE_JOB = re.compile(r"^\s*(?:local)?rule (?:step|register|build_manifest):\s*$")


class UsageError(ValueError):
    """An invocation the entry point refuses before writing anything."""


@dataclass
class Invocation:
    targets: list[str] = field(default_factory=list)
    config: dict[str, str] = field(default_factory=dict)
    cores: str = "all"
    dry_run: bool = False
    keep_going: bool = False
    rerun_incomplete: bool = False
    resolve_snapshot: str | None = None


def parse_invocation(argv: Iterable[str]) -> Invocation:
    """Parse the operator's words, refusing any option outside the allowlist."""
    words = list(argv)
    inv = Invocation()
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("-n", "--dry-run", "--dryrun"):
            inv.dry_run = True
        elif word in ("-k", "--keep-going"):
            inv.keep_going = True
        elif word in ("--rerun-incomplete", "--ri"):
            inv.rerun_incomplete = True
        elif word in ("-c", "--cores"):
            if i + 1 >= len(words):
                raise UsageError(f"{word} needs a value")
            i += 1
            inv.cores = _cores(words[i])
        elif word.startswith("--cores="):
            inv.cores = _cores(word.split("=", 1)[1])
        elif word == "--resolve-snapshot" or word.startswith("--resolve-snapshot="):
            if "=" in word:
                action = word.split("=", 1)[1]
            elif i + 1 < len(words):
                i += 1
                action = words[i]
            else:
                raise UsageError("--resolve-snapshot needs `restore` or `delete`")
            if action not in SNAPSHOT_RESOLUTIONS:
                raise UsageError(f"--resolve-snapshot takes `restore` or `delete`, not {action!r}")
            inv.resolve_snapshot = action
        elif word == "--config" or word.startswith("--config="):
            items = [word.split("=", 1)[1]] if word.startswith("--config=") else []
            while i + 1 < len(words) and "=" in words[i + 1] and not words[i + 1].startswith("-"):
                i += 1
                items.append(words[i])
            if not items:
                raise UsageError("--config needs key=value entries")
            for item in items:
                key, _, value = item.partition("=")
                if not key:
                    raise UsageError(f"--config entry {item!r} has no key")
                if key in RESERVED_CONFIG_KEYS:
                    raise UsageError(f"--config {key} is set by the entry point, not by an operator")
                if key in inv.config:
                    raise UsageError(f"--config {key} is given twice")
                inv.config[key] = value
        elif word.split("=", 1)[0] in UNSAFE_OPTIONS:
            raise UsageError(
                f"{word} is refused: it would bypass or weaken the release guard (#195). "
                f"Accepted: {ALLOWED_OPTIONS}"
            )
        elif word.startswith("-"):
            raise UsageError(f"{word} is not accepted. Accepted: {ALLOWED_OPTIONS}")
        elif _TARGET.match(word):
            inv.targets.append(word)
        else:
            raise UsageError(f"{word!r} is not a release id (OGS-NNNNN) or `all`")
        i += 1
    if inv.resolve_snapshot is not None and (len(inv.targets) != 1 or inv.targets[0] == "all" or inv.dry_run):
        raise UsageError("--resolve-snapshot needs exactly one release id, and no --dry-run")
    return inv


def _cores(value: str) -> str:
    if value != "all" and not value.isdigit():
        raise UsageError(f"--cores takes a number or `all`, not {value!r}")
    return value


def snakemake_argv(
    inv: Invocation, *, dry_run: bool, extra_config: dict[str, str] | None = None
) -> list[str]:
    """The one Snakemake command line, with the ids before `--config`, which takes every word after it."""
    argv = [sys.executable, "-m", "snakemake", "--snakefile", str(SNAKEFILE), "--cores", inv.cores]
    if dry_run:
        argv.append("--dry-run")
    if inv.keep_going:
        argv.append("--keep-going")
    if inv.rerun_incomplete:
        argv.append("--rerun-incomplete")
    argv.extend(inv.targets)
    config = {**inv.config, **(extra_config or {})}
    if config:
        argv.append("--config")
        argv.extend(f"{key}={value}" for key, value in config.items())
    return argv


def scheduled_releases(dry_run_output: str) -> list[tuple[str, str]]:
    """The (artifact root, store_id) of every job a Snakemake dry run scheduled.

    Every step, register and build_manifest job prints its wildcards, so a job
    block without one means the output is not what this parser expects, and
    that fails loudly rather than under-reporting what the run will touch.
    """
    found: list[tuple[str, str]] = []
    jobs = 0
    for line in dry_run_output.splitlines():
        if _RELEASE_JOB.match(line):
            jobs += 1
        match = _WILDCARDS.match(line)
        if not match:
            continue
        fields = dict(part.split("=", 1) for part in match["body"].split(", ") if "=" in part)
        if "root" in fields and "store_id" in fields:
            found.append((fields["root"], fields["store_id"]))
    if len(found) != jobs:
        raise RuntimeError(
            f"The dry run listed {jobs} release job(s) but the wildcards of {len(found)}; "
            "refusing to guess what the run would touch"
        )
    return sorted(set(found))


def registered_ids(registry_root: Path) -> list[str]:
    if not registry_root.is_dir():
        return []
    return sorted(
        d.name for d in registry_root.iterdir()
        if d.is_dir() and paths.is_valid_store_id(d.name) and (d / "release.yaml").is_file()
    )


def _cleanup_metadata(inv: Invocation, files: Iterable[Path]) -> None:
    """Clear Snakemake's marks that a killed job left these outputs incomplete.

    Snakemake's own `--cleanup-metadata` is the supported way to do it. When
    the killed job never wrote metadata, Snakemake removes the incomplete mark
    and still exits 1 saying the metadata "was not present". That one outcome
    is expected; any other failure is raised.
    """
    for path in files:
        argv = [
            sys.executable, "-m", "snakemake", "--snakefile", str(SNAKEFILE),
            "--cleanup-metadata", str(path),
        ]
        if inv.config:
            argv += ["--config", *(f"{key}={value}" for key, value in inv.config.items())]
        result = subprocess.run(argv, cwd=paths.REPO_ROOT, capture_output=True, text=True)
        output = result.stdout + result.stderr
        if result.returncode != 0 and "because the metadata was not present" not in output:
            raise RuntimeError(f"Could not clear Snakemake's metadata for {path}:\n{output}")


def _cleanup_register_metadata(inv: Invocation, store_id: str, root: Path) -> None:
    """`complete_publication` has just written `records/register.json`, so it is complete."""
    _cleanup_metadata(inv, [paths.record_path(store_id, "register", root=root)])


def resolve_snapshot(inv: Invocation, store_id: str, root: Path, action: str) -> str:
    """Carry out an operator's resolution of a leftover records snapshot (#195).

    A forced run killed before it began to publish leaves the release's Store
    and Validation Record untouched, its records snapshot, and possibly records
    the run rewrote. Which records describe the Store is the operator's call
    (decided 7 Oct 2026). `restore` puts the snapshot back as `records/`;
    `delete` keeps `records/` as it is. Either way Snakemake's marks that the
    killed run's outputs are incomplete are then cleared, so the next run is
    not stopped by `IncompleteFilesException`.
    """
    if paths.publication_marker(store_id, root=root).exists():
        raise run.PublicationPendingError(
            f"{store_id} has a pending publication, not a leftover snapshot; "
            f"`pixi run release {store_id}` completes it"
        )
    snapshots = run.pending_force_snapshots(store_id, root)
    if len(snapshots) != 1:
        raise UsageError(f"{store_id} has {len(snapshots)} records snapshot(s), not one: {snapshots}")
    snapshot = snapshots[0]
    if action == "restore":
        run.restore_force_snapshot(snapshot)
    else:
        shutil.rmtree(snapshot)
    _cleanup_metadata(inv, release_outputs(store_id, root))
    return (
        f"release: {'restored' if action == 'restore' else 'deleted'} {snapshot} and cleared "
        f"Snakemake's incomplete marks for {store_id}. If Snakemake now reports the working "
        "directory locked, check that no other run is active, then run "
        "`snakemake --snakefile workflow/Snakefile --unlock`."
    )


def finish_publication(inv: Invocation, store_id: str, root: Path | str) -> bool:
    """Finish a release's pending publication, removing its marker last (#195).

    `complete_publication` brings the Store and archive into line and rewrites
    the two records the marker holds (identical, after a register job that ran
    to the end). Then Snakemake's
    incomplete mark on `records/register.json`, which a killed job leaves, is
    cleared, and only then is `publication.json` removed. A crash anywhere in
    here leaves the marker, so the next run repeats this function. Returns
    whether a publication was pending.
    """
    if register.complete_publication(store_id, root, finalize=False) is None:
        return False
    _cleanup_register_metadata(inv, store_id, Path(root))
    run.fault_boundary("metadata-cleaned")
    register.remove_publication_marker(store_id, root)
    return True


class ReleaseInProgressError(RuntimeError):
    """Another live run holds the release's lock."""


def _holder(fd: int) -> str:
    try:
        text = os.pread(fd, 4096, 0).decode("utf-8", errors="replace").strip()
    except OSError:
        text = ""
    return text or "pid unknown"


class ReleaseLocks:
    """The exclusive per-release locks one entry-point run holds (#195).

    Per release, not per artifact root: a release build can run for hours, and
    one lock per root would stop every other release meanwhile. Locks are only
    ever tried, never waited for, so two runs cannot deadlock. Each lock file
    holds its holder's pid for the refusal message.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._fds: dict[str, int] = {}

    def holds(self, store_id: str) -> bool:
        return store_id in self._fds

    def acquire(self, store_id: str) -> None:
        if store_id in self._fds:
            return
        lock_p = paths.release_lock_path(store_id, root=self.root)
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_p, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _holder(fd)
            os.close(fd)
            raise ReleaseInProgressError(
                f"a run of {store_id} is in progress ({holder}). Nothing was written; "
                "wait for it to finish."
            ) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}, started {run.utc_stamp()}\n".encode(), 0)
        self._fds[store_id] = fd

    def try_acquire(self, store_id: str) -> bool:
        try:
            self.acquire(store_id)
        except ReleaseInProgressError:
            return False
        return True

    def release(self, store_id: str) -> None:
        fd = self._fds.pop(store_id, None)
        if fd is not None:
            os.close(fd)

    def release_all(self) -> None:
        for store_id in list(self._fds):
            self.release(store_id)

    def fds(self) -> tuple[int, ...]:
        return tuple(self._fds.values())


def lock_holder(store_id: str, root: Path | str) -> str | None:
    """Who holds a release's lock, or None; it reads, never creates or writes."""
    lock_p = paths.release_lock_path(store_id, root=root)
    if not lock_p.is_file():
        return None
    fd = os.open(lock_p, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return _holder(fd)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def release_outputs(store_id: str, root: Path | str) -> list[Path]:
    """The outputs Snakemake may mark incomplete for a release: its records and derived manifest."""
    records = paths.records_dir(store_id, root=root)
    outputs = sorted(records.glob("*.json")) if records.is_dir() else []
    return outputs + [
        paths.build_manifest_path(store_id, root=root),
        paths.build_manifest_sidecar_path(store_id, root=root),
    ]


def recover_releases(inv: Invocation, registry_root: Path, root: Path, locks: ReleaseLocks) -> list[str]:
    """Finish what a crash interrupted, before any refusal runs (#195).

    Only a release whose lock this run holds, or can take, is recovered: a
    locked one belongs to a live run, whose marker or backup is its own
    business. A lock taken only for recovery is released at once.
    """
    messages: list[str] = []
    for store_id in registered_ids(registry_root):
        held_before = locks.holds(store_id)
        if not held_before and not locks.try_acquire(store_id):
            continue
        try:
            if finish_publication(inv, store_id, root):
                messages.append(f"release: completed the interrupted publication of {store_id}")
            elif paths.backup_store_path(store_id, root=root).is_dir():
                run._recover_pending_backup(store_id, root)
                messages.append(f"release: recovered {paths.backup_store_path(store_id, root=root)}")
        finally:
            if not held_before:
                locks.release(store_id)
    return messages


def settle(inv: Invocation, scheduled: list[tuple[str, str]], snapshots: list[Path], succeeded: bool) -> None:
    """Finish every publication the run left, then resolve its snapshots.

    A failed run's restored records get Snakemake's incomplete marks cleared,
    as `--resolve-snapshot` does, so the next run is not stopped by them.
    """
    for root, store_id in scheduled:
        if finish_publication(inv, store_id, root):
            print(f"release: finished the publication of {store_id}")
    restored = [s for s in snapshots if s.exists()] if not succeeded else []
    run.settle_force_snapshots(snapshots, succeeded=succeeded)
    for snapshot in restored:
        store_id, root = snapshot.parent.name, snapshot.parent.parent
        _cleanup_metadata(inv, release_outputs(store_id, root))
        print(f"release: restored the records of {store_id} from {snapshot.name}")


class _Interrupted(Exception):
    """SIGTERM, raised in the entry point so it can stop Snakemake and settle."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum: int, _frame: object) -> None:
    raise _Interrupted(signum)


def _session_members(sid: int) -> list[int]:
    """The live processes of session `sid` (Linux /proc; empty where it is absent)."""
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if entry.isdigit():
            try:
                if os.getsid(int(entry)) == sid:
                    members.append(int(entry))
            except (ProcessLookupError, PermissionError):
                pass
    return members


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat[stat.rindex(")") + 2:].split()[1])


def _stop_snakemake(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Stop an interrupted Snakemake run so it exits on its own and releases its lock.

    One SIGINT to its session, as a terminal's Ctrl-C would send, puts
    Snakemake in cancel mode: it schedules nothing more. That does not stop a
    running job: a job process runs its `run:` block in a worker thread and
    waits for it, and `run.py` runs the step's command in its own supervised
    session. So after `grace` seconds the job processes, the session's members
    other than the supervisor and Snakemake itself, are SIGKILLed. Their step
    supervisors then kill the commands (ADR 0026), and Snakemake, its jobs
    failed, exits on its own. A job killed mid-publication leaves its marker,
    which settlement completes.
    """
    sid = proc.pid
    try:
        os.killpg(sid, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is not None:
        return
    keep = {sid} | {pid for pid in _session_members(sid) if _parent_pid(pid) == sid}
    for pid in _session_members(sid):
        if pid not in keep:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_snakemake(argv: list[str], lock_fds: tuple[int, ...]) -> tuple[int, int | None]:
    """Run Snakemake under the parent-death supervisor; return its exit code and any interrupt.

    Snakemake and its jobs get their own session, led by `run.py`'s
    supervisor (ADR 0026): if this process dies, SIGKILL included, the
    supervisor kills the session, so nothing outlives the entry point. The
    supervisor inherits the release locks, so they stay held while Snakemake
    lives. On Ctrl-C or SIGTERM this process ignores further interrupts, stops
    Snakemake (`_stop_snakemake`) and waits for it to exit; settlement then
    restores the records. Snakemake's own SIGTERM handling would wait for
    running jobs to finish, which can take hours.
    """
    liveness_read, liveness_write = os.pipe()
    status_read, status_write = os.pipe()
    env = {**os.environ, run.ENTRY_POINT_PID_ENV: str(os.getpid())}
    proc = subprocess.Popen(
        run._supervised_command(argv, liveness_read, status_write),
        cwd=paths.REPO_ROOT,
        env=env,
        start_new_session=True,
        pass_fds=(liveness_read, status_write, *lock_fds),
    )
    os.close(liveness_read)
    os.close(status_write)
    interrupted: int | None = None
    try:
        try:
            returncode = proc.wait()
        except (KeyboardInterrupt, _Interrupted) as exc:
            interrupted = exc.signum if isinstance(exc, _Interrupted) else signal.SIGINT
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(
                f"release: {signal.Signals(interrupted).name} received; stopping Snakemake, "
                "then settling the run",
                file=sys.stderr,
            )
            _stop_snakemake(proc)
            returncode = proc.wait()
    finally:
        os.close(liveness_write)
        status = run._read_exec_status(status_read)
        os.close(status_read)
    if status:
        raise RuntimeError(f"Snakemake could not be started: {status}")
    return returncode, interrupted


def _dry_run(inv: Invocation, registry_root: Path, root: Path, force: bool) -> int:
    """Show the plan and what the guard would do, writing nothing."""
    result = subprocess.run(
        snakemake_argv(inv, dry_run=True), cwd=paths.REPO_ROOT, capture_output=True, text=True
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        return result.returncode
    scheduled = scheduled_releases(result.stdout + result.stderr)
    busy = {}
    for _, store_id in scheduled:
        holder = lock_holder(store_id, root)
        if holder is not None:
            busy[store_id] = holder
    for store_id, holder in sorted(busy.items()):
        print(f"release-dry: a run of {store_id} is in progress ({holder}); the real run would refuse")
    # A pending marker under a live run's lock is that run's publication in
    # progress, not one a crash interrupted, whether or not this plan schedules
    # the release (review round 4 of #196).
    for store_id in registered_ids(registry_root):
        if store_id in busy or not paths.publication_marker(store_id, root=root).exists():
            continue
        holder = lock_holder(store_id, root)
        if holder is not None:
            print(
                f"release-dry: a run of {store_id} is in progress ({holder}) and is publishing; "
                "it finishes its own publication"
            )
        else:
            print(f"release-dry: the real run first completes the interrupted publication of {store_id}")
    idle = [(r, store_id) for r, store_id in scheduled if store_id not in busy]
    forced = run.forced_releases(force, inv.targets)
    try:
        run.refuse_pending_force_snapshots(idle)
        run.refuse_rebuilding_published_releases(idle, forced=forced)
    except (run.ForceSnapshotPendingError, run.StoreExistsError) as exc:
        print(f"release-dry: the real run would refuse:\n{exc}")
        return result.returncode
    for scheduled_root, store_id in idle:
        if store_id in forced and paths.store_path(store_id, root=scheduled_root).exists():
            print(f"release-dry: the real run would snapshot the records of {store_id} and replace it")
    return result.returncode


GUARD_ERRORS: tuple[type[BaseException], ...] = (
    ReleaseInProgressError,
    run.StoreExistsError,
    run.ForceSnapshotPendingError,
    run.PublicationPendingError,
    register.PublicationError,
    UsageError,
)


def _run(inv: Invocation, registry_root: Path, root: Path, force: bool, locks: ReleaseLocks) -> int:
    """Recovery, preflight, snapshot, the run and settlement, with `locks` taken as needed."""
    targets = [t for t in inv.targets if t != "all"]
    requested = targets if targets and "all" not in inv.targets else registered_ids(registry_root)
    for store_id in sorted(requested):
        locks.acquire(store_id)
    for message in recover_releases(inv, registry_root, root, locks):
        print(message)
    # A leftover snapshot is refused before the dry run, so an operator sees the
    # refusal and its resolutions, not the IncompleteFilesException it causes.
    run.refuse_pending_force_snapshots([(root, store_id) for store_id in requested])

    plan = subprocess.run(
        snakemake_argv(inv, dry_run=True), cwd=paths.REPO_ROOT, capture_output=True, text=True
    )
    if plan.returncode != 0:
        sys.stdout.write(plan.stdout)
        sys.stderr.write(plan.stderr)
        return plan.returncode
    scheduled = scheduled_releases(plan.stdout + plan.stderr)
    if not scheduled:
        print("release: nothing to be done; every requested release is up to date")
        return 0
    for _, store_id in scheduled:
        locks.acquire(store_id)

    stamp = run.utc_stamp()
    snapshots: list[Path] = []
    returncode, interrupted = 1, None
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        try:
            snapshots = run.prepare_release_run(
                scheduled, forced=run.forced_releases(force, inv.targets), stamp=stamp
            )
            run.fault_boundary("snapshots-taken")
            returncode, interrupted = _run_snakemake(
                snakemake_argv(inv, dry_run=False, extra_config={"release_run": stamp}), locks.fds()
            )
        except (KeyboardInterrupt, _Interrupted) as exc:
            # Interrupted before Snakemake started: any snapshot this run took is
            # its own (the locks are held), and records are untouched.
            interrupted = exc.signum if isinstance(exc, _Interrupted) else signal.SIGINT
            snapshots = [
                p for _, store_id in scheduled
                for p in run.pending_force_snapshots(store_id, root)
                if p.name.endswith(stamp)
            ]
        run.fault_boundary("before-settle")
        # Settlement runs to the end: a second Ctrl-C or SIGTERM must not leave it half done.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            settle(inv, scheduled, snapshots, succeeded=returncode == 0 and interrupted is None)
        except Exception as exc:
            print(
                f"release: the run finished with exit code {returncode}, but settling it failed: {exc}",
                file=sys.stderr,
            )
            return returncode or 1
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    if interrupted is not None and returncode != 0:
        return 128 + interrupted
    return returncode


def main(argv: Iterable[str]) -> int:
    try:
        inv = parse_invocation(argv)
        force = run.force_requested(inv.config.pop("force", None))
    except (UsageError, ValueError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 2
    registry_root = Path(inv.config.get("registry_root", "stores"))
    if not registry_root.is_absolute():
        registry_root = paths.REPO_ROOT / registry_root
    root = paths.artifact_root(inv.config.get("artifact_root"))
    if ", " in str(root):
        print(f"release: the artifact root {root} contains ', ', which the preflight cannot parse", file=sys.stderr)
        return 2
    if inv.dry_run:
        return _dry_run(inv, registry_root, root, force)
    if inv.resolve_snapshot is not None and force:
        print("release: --resolve-snapshot takes no --config force", file=sys.stderr)
        return 2

    locks = ReleaseLocks(root)
    try:
        if inv.resolve_snapshot is not None:
            locks.acquire(inv.targets[0])
            print(resolve_snapshot(inv, inv.targets[0], root, inv.resolve_snapshot))
            return 0
        return _run(inv, registry_root, root, force, locks)
    except GUARD_ERRORS as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    finally:
        locks.release_all()


__all__ = [
    "ALLOWED_OPTIONS",
    "ENTRY_POINT_BOUNDARIES",
    "GUARD_ERRORS",
    "Invocation",
    "ReleaseInProgressError",
    "ReleaseLocks",
    "RESERVED_CONFIG_KEYS",
    "SNAKEFILE",
    "UNSAFE_OPTIONS",
    "SNAPSHOT_RESOLUTIONS",
    "UsageError",
    "finish_publication",
    "lock_holder",
    "main",
    "parse_invocation",
    "recover_releases",
    "registered_ids",
    "release_outputs",
    "resolve_snapshot",
    "scheduled_releases",
    "settle",
    "snakemake_argv",
]
