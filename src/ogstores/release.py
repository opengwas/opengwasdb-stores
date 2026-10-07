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
4. The run, with `--config force_transaction=<UTC>` naming that snapshot.
5. Settlement. A publication the run left pending is completed; then a
   failed run's snapshots are restored and a successful run's are archived.

The options are an allowlist. Anything else, including `--no-hooks` and
`--touch`, is refused before anything is written. Running `snakemake`
directly bypasses this entry point, and with it everything above except the
Snakefile's own `onstart` refusal.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ogstores import paths, register, run

SNAKEFILE: Path = paths.REPO_ROOT / "workflow" / "Snakefile"

# Configuration only the entry point may set.
RESERVED_CONFIG_KEYS: frozenset[str] = frozenset({"force_transaction"})

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
    "--keep-going, --rerun-incomplete"
)

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


def _cleanup_register_metadata(inv: Invocation, store_id: str, root: Path) -> None:
    """Clear Snakemake's mark that a killed register job's output is incomplete.

    `complete_publication` has just written that output, `records/register.json`,
    so it is complete. Snakemake's own `--cleanup-metadata` is the supported way
    to say so. When the killed job never wrote metadata, Snakemake removes the
    incomplete mark and still exits 1 saying the metadata "was not present".
    That one outcome is expected; any other failure is raised.
    """
    record = paths.record_path(store_id, "register", root=root)
    argv = [
        sys.executable, "-m", "snakemake", "--snakefile", str(SNAKEFILE),
        "--cleanup-metadata", str(record),
    ]
    if inv.config:
        argv += ["--config", *(f"{key}={value}" for key, value in inv.config.items())]
    result = subprocess.run(argv, cwd=paths.REPO_ROOT, capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode != 0 and "because the metadata was not present" not in output:
        raise RuntimeError(
            f"Completed the publication of {store_id}, but could not clear Snakemake's "
            f"incomplete mark on {record}:\n{output}"
        )


def recover_releases(inv: Invocation, registry_root: Path, root: Path) -> list[str]:
    """Finish what a crash interrupted, before any refusal runs (#195)."""
    messages: list[str] = []
    for store_id in registered_ids(registry_root):
        if register.complete_publication(store_id, root) is not None:
            _cleanup_register_metadata(inv, store_id, root)
            messages.append(f"release: completed the interrupted publication of {store_id}")
        elif paths.backup_store_path(store_id, root=root).is_dir():
            run._recover_pending_backup(store_id, root)
            messages.append(f"release: recovered {paths.backup_store_path(store_id, root=root)}")
    return messages


def settle(inv: Invocation, scheduled: list[tuple[str, str]], snapshots: list[Path], succeeded: bool) -> None:
    """Complete any publication the run left pending, then resolve its snapshots."""
    for root, store_id in scheduled:
        if register.complete_publication(store_id, root) is not None:
            _cleanup_register_metadata(inv, store_id, Path(root))
            print(f"release: completed the publication of {store_id} that this run left pending")
    run.settle_force_snapshots(snapshots, succeeded=succeeded)


def _dry_run(inv: Invocation, registry_root: Path, root: Path, force: bool) -> int:
    """Show the plan and what the guard would do, writing nothing."""
    result = subprocess.run(
        snakemake_argv(inv, dry_run=True), cwd=paths.REPO_ROOT, capture_output=True, text=True
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        return result.returncode
    pending = [
        store_id for store_id in registered_ids(registry_root)
        if paths.publication_marker(store_id, root=root).exists()
    ]
    for store_id in pending:
        print(f"release-dry: the real run first completes the interrupted publication of {store_id}")
    scheduled = scheduled_releases(result.stdout + result.stderr)
    forced = run.forced_releases(force, inv.targets)
    try:
        run.refuse_pending_force_snapshots(scheduled)
        run.refuse_rebuilding_published_releases(scheduled, forced=forced)
    except (run.ForceSnapshotPendingError, run.StoreExistsError) as exc:
        print(f"release-dry: the real run would refuse:\n{exc}")
        return result.returncode
    for scheduled_root, store_id in scheduled:
        if store_id in forced and paths.store_path(store_id, root=scheduled_root).exists():
            print(f"release-dry: the real run would snapshot the records of {store_id} and replace it")
    return result.returncode


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

    try:
        for message in recover_releases(inv, registry_root, root):
            print(message)
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
        stamp = run.utc_stamp()
        snapshots = run.prepare_release_run(
            scheduled, forced=run.forced_releases(force, inv.targets), stamp=stamp
        )
        run.fault_boundary("snapshots-taken")
    except (
        run.StoreExistsError,
        run.ForceSnapshotPendingError,
        run.PublicationPendingError,
        register.PublicationError,
    ) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1

    extra = {"force_transaction": stamp} if snapshots else None
    returncode = subprocess.run(
        snakemake_argv(inv, dry_run=False, extra_config=extra), cwd=paths.REPO_ROOT
    ).returncode
    run.fault_boundary("before-settle")
    try:
        settle(inv, scheduled, snapshots, succeeded=returncode == 0)
    except Exception as exc:
        print(f"release: the run finished with exit code {returncode}, but settling it failed: {exc}", file=sys.stderr)
        return returncode or 1
    return returncode


__all__ = [
    "ALLOWED_OPTIONS",
    "Invocation",
    "RESERVED_CONFIG_KEYS",
    "SNAKEFILE",
    "UNSAFE_OPTIONS",
    "UsageError",
    "main",
    "parse_invocation",
    "recover_releases",
    "registered_ids",
    "scheduled_releases",
    "settle",
    "snakemake_argv",
]
