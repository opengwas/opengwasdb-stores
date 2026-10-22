"""Pre-write runtime guard for the supported Phase A release entry point (#218).

The ordinary release invocation is `pixi run release`. This module is the one
place that proves, *before any artifact is written*, that the `opengwasdb`
runtime that run will actually execute:

* is the environment's tracked, immutable pin (the pixi lock's git revision),
  so a supported run cannot silently follow an ad-hoc executable substituted
  onto `PATH`, and an unprovable identity is refused rather than assumed;
* provides every subcommand the *scheduled* jobs will actually run; and
* can complete each scheduled Reference-Completed release, i.e. the parent it
  will consume is format 0.2.0 or is itself built in the same run.

The last point is a *format* gate, not a Store inspection: a 0.1.0 parent can
be read but not completed by a format-0.2.0 builder (opengwas/opengwasdb#247,
`opengwasdb.store.open.check_completion_source`). Asking the runtime's own
`info --format json` reports the parent's declared format through the core's
supported interface, so this repository never opens the Store itself
(`CONTRIBUTING.md`).

The gate is precise about *scheduled* work, because "the id appears in the dry
run" is not the same as "the Store is rebuilt". `workflow/release.py` and the
Snakefile `onstart` pass `(artifact_root, store_id, step)` for every dry-run
job, so:

* a child's parent is gated only when the child's own `complete` step is
  scheduled -- an enhancement-only rerun of an existing completed Store (build
  its HM3 subset, validate, overview) never consults its unused completion
  recipe;
* a parent on disk at 0.1.0 is not gated when a same-run `build`/`complete`
  producing step for that parent is scheduled, because that produces a fresh
  0.2.0 Store;
* a parent that is neither on disk nor scheduled to be produced is refused
  loudly rather than left for Snakemake to fail on hours later.

See docs/runtime.md, docs/spec/store-release-workflow.md and ADR 0034.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ogstores import bundle as bundle_module
from ogstores import paths, run
from ogstores.plan import plan

#: The immutable opengwasdb git revision this repository pins. It is the single
#: source of truth for the runtime identity the tests assert against
#: `pixi.toml` and `pixi.lock`; the pixi dependency is the executable form of
#: it. Never edit this without `pixi lock` and the tests in
#: `tests/runtime/test_runtime_guard.py`.
#:
#: Repinned from `1be2a15` (PR #275) to `4e922566` (PR #276) with core #254's
#: bounded Dense-validation memory work; the Store format stays 0.2.0. Then to
#: `023774a7` for `check-observed-view` (core #289, stores #306), which gates a
#: single-id completion's swap. Then to `d405de0a`, the core `dev` merge of PR
#: #291 whose first parent is PR #288's merge: stores #312's OGS-00009 rebuild
#: needs #288's strand-aware hg19 lift. The range `023774a..d405de0` is #288's
#: commits and merge plus the #291 merge; #288 is its only functional change.
#: Then to `eb4c749b`, core #295's merge into the core integration branch for
#: stores epic #232 (core PR #303), not yet on core `dev`: its
#: `check-observed-view` compares a Hybrid pair, which stores #333 needs for a
#: Hybrid release to complete its own Store. The range `d405de0..eb4c749` is
#: #295's commits and merge, the branch's empty first commit and core `dev`'s
#: docs-only PR #293; #295 is its only functional change.
#: Then to `32f47b3b`, core #296's merge into the same integration branch, not
#: yet on core `dev` either: its `check-observed-view` compares a Ragged pair,
#: which stores #336 needs for a Ragged release to complete its own Store. The
#: range `eb4c749..32f47b3` is #296's commits and merge; #296 is its only
#: functional change.
PINNED_OPENGWASDB_REVISION: str = "32f47b3b9c9bbab684a2f424a6f6ff617be0a8b1"
OPENGWASDB_GIT_URL: str = "https://github.com/opengwas/opengwasdb"

#: The Store format the pinned builders write and the only format Reference
#: Completion may consume. It lives in the core, not here; this is the value the
#: guard reports and compares against `opengwasdb info`.
REQUIRED_FORMAT_VERSION: str = "0.2.0"

#: Command the guard probes in addition to those a scheduled plan names, for the
#: completion-parent format gate.
INFO_COMMAND: str = "info"

#: A Snakemake dry-run job: `(artifact_root, store_id, step)`. `step` is None
#: for jobs with no `step` wildcard (`build_manifest`, `register`).
ScheduledJob = tuple[str, str, str | None]

DEFAULT_PROBE_TIMEOUT_SECONDS: float = 60.0

#: Set by the release entry point for the Snakemake run it starts, to
#: `RuntimeIdentity.handoff()` of the runtime it has just verified. The
#: Snakefile's `onstart` check then skips the `--help` probes the entry point
#: already made, for the same executable at the same revision; it still checks
#: the pin itself. A direct `snakemake` run has no hand-off and probes them all.
VERIFIED_RUNTIME_ENV: str = "OGSTORES_VERIFIED_RUNTIME"

_STORE_PRODUCING_STEPS: frozenset[str] = frozenset({"build", "complete"})


class RuntimeGuardError(RuntimeError):
    """A scheduled run would use a runtime this release workflow refuses."""


class RuntimeCapabilityError(RuntimeGuardError):
    """The resolved `opengwasdb` does not provide a command a scheduled plan needs."""


class RuntimeRevisionError(RuntimeGuardError):
    """The resolved `opengwasdb` is not the tracked, provable pin."""


class CompletionParentFormatError(RuntimeGuardError):
    """A scheduled Reference-Completed release has a parent no format-0.2.0 builder may complete."""


@dataclass(frozen=True)
class RuntimeIdentity:
    """The resolved runtime a run will use, recorded before the run starts."""

    executable: str
    revision: str
    version: str
    pinned_revision: str
    format_version: str
    #: The subcommands this verification found the runtime provides.
    commands: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line an operator can copy into a run note or issue comment."""
        return (
            f"opengwasdb {self.version} revision {self.revision} at {self.executable} "
            f"(pinned {self.pinned_revision}; writes format {self.format_version})"
        )

    def handoff(self) -> str:
        """The `VERIFIED_RUNTIME_ENV` value that passes this verification to `onstart`."""
        return json.dumps(
            {"executable": self.executable, "revision": self.revision, "commands": list(self.commands)}
        )


def _handed_off_commands(handoff: str | None, *, executable: str, revision: str) -> frozenset[str]:
    """The commands a hand-off verified for this executable at this revision; none otherwise.

    A hand-off for another executable or revision, or one that does not parse,
    verifies nothing, so every command is probed as if there were none.
    """
    if not handoff:
        return frozenset()
    try:
        verified = json.loads(handoff)
        if verified["executable"] != executable or verified["revision"] != revision:
            return frozenset()
        return frozenset(str(command) for command in verified["commands"])
    except (ValueError, TypeError, KeyError):
        return frozenset()


def _probe_command(executable: str, command: str, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> bool:
    """Return True when `opengwasdb <command> --help` succeeds."""
    if not executable or executable == run.UNAVAILABLE:
        return False
    try:
        result = subprocess.run(
            [str(executable), command, "--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _probe_commands(executable: str, commands: Iterable[str], timeout: float) -> frozenset[str]:
    """The `commands` that `opengwasdb <command> --help` answers, each probed once.

    Each probe starts the runtime's interpreter and imports its CLI, so they run
    side by side rather than one after another.
    """
    distinct = sorted(set(commands))
    if not distinct:
        return frozenset()
    with ThreadPoolExecutor(max_workers=len(distinct)) as pool:
        answered = pool.map(lambda command: _probe_command(executable, command, timeout=timeout), distinct)
        return frozenset(command for command, ok in zip(distinct, answered) if ok)


def _read_store_format_version(
    executable: str,
    store_path: Path,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> str:
    """Read a Store Release's declared format through `opengwasdb info --format json`.

    Raises `CompletionParentFormatError` (not a bare subprocess error) when the
    runtime cannot report the parent, because completion cannot be proven safe.
    """
    try:
        result = subprocess.run(
            [str(executable), INFO_COMMAND, str(store_path), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompletionParentFormatError(
            f"cannot inspect the existing parent Store {store_path} with "
            f"{executable!r}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise CompletionParentFormatError(
            f"cannot inspect the existing parent Store {store_path} with "
            f"{executable!r}: opengwasdb info exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CompletionParentFormatError(
            f"cannot inspect the existing parent Store {store_path}: "
            f"opengwasdb info did not return JSON: {result.stdout.strip()!r}"
        ) from exc
    format_version = payload.get("format_version")
    if not isinstance(format_version, str) or not format_version:
        raise CompletionParentFormatError(
            f"cannot inspect the existing parent Store {store_path}: "
            f"opengwasdb info reported no format_version: {result.stdout.strip()!r}"
        )
    return format_version


def normalize_scheduled(scheduled: Iterable[tuple]) -> list[ScheduledJob]:
    """Normalize `(root, store_id)` or `(root, store_id, step)` dry-run jobs.

    An item without a step is a job that has no CLI step (`build_manifest`,
    `register`) or a caller that did not supply one; see
    `resolve_scheduled_steps` for how an unknown step set is handled.
    """
    jobs: list[ScheduledJob] = []
    for item in scheduled:
        if len(item) == 2:
            jobs.append((str(item[0]), str(item[1]), None))
        elif len(item) == 3:
            step = item[2]
            jobs.append((str(item[0]), str(item[1]), None if step is None else str(step)))
        else:
            raise ValueError(
                f"scheduled job must be (root, store_id) or (root, store_id, step), not {item!r}"
            )
    return jobs


def _planned_steps(store_id: str, *, registry_root: Path | str, artifact_root: Path | str | None):
    return plan(bundle_module.load(store_id, registry_root=registry_root), artifact_root=artifact_root)


def required_commands_for_release(
    store_id: str, *, registry_root: Path | str, artifact_root: Path | str | None = None
) -> list[str]:
    """Every distinct `opengwasdb` subcommand one release's plan can name."""
    commands: list[str] = []
    for step in _planned_steps(store_id, registry_root=registry_root, artifact_root=artifact_root):
        command = step.argv[1] if len(step.argv) > 1 else ""
        if command and command not in commands:
            commands.append(command)
    return commands


def commands_for_scheduled_steps(
    store_id: str,
    steps: Iterable[str],
    *,
    registry_root: Path | str,
    artifact_root: Path | str | None = None,
) -> list[str]:
    """The `opengwasdb` subcommands the named planned steps will actually run."""
    planned = {
        step.name: step
        for step in _planned_steps(store_id, registry_root=registry_root, artifact_root=artifact_root)
    }
    commands: list[str] = []
    for name in steps:
        planned_step = planned.get(name)
        if planned_step is None:
            continue
        command = planned_step.argv[1] if len(planned_step.argv) > 1 else ""
        if command and command not in commands:
            commands.append(command)
    return commands


def completion_parents_for_release(
    store_id: str, *, registry_root: Path | str, artifact_root: Path | str | None = None
) -> list[str]:
    """Parent Store ids a Reference-Completed release's plan would complete."""
    loaded = bundle_module.load(store_id, registry_root=registry_root)
    steps = plan(loaded, artifact_root=artifact_root)
    if not any(step.name == "complete" for step in steps):
        return []
    return [loaded.derived_from] if loaded.derived_from else []


def _steps_by_store(jobs: Iterable[ScheduledJob]) -> dict[tuple[str, str], set[str]]:
    """The resolved step names per `(root, store_id)`, ignoring step-less jobs."""
    by_store: dict[tuple[str, str], set[str]] = {}
    for root, store_id, step in jobs:
        by_store.setdefault((root, store_id), set())
        if step is not None:
            by_store[(root, store_id)].add(step)
    return by_store


def _produces_store(jobs: Iterable[ScheduledJob]) -> set[tuple[str, str]]:
    """`(root, store_id)` pairs with a scheduled `build` or `complete` job."""
    return {
        (root, store_id)
        for root, store_id, step in jobs
        if step in _STORE_PRODUCING_STEPS
    }


def verify_runtime_for_releases(
    scheduled: Iterable[tuple],
    *,
    registry_root: Path | str,
    executable: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    handoff: str | None = None,
) -> RuntimeIdentity:
    """Refuse a run whose runtime is missing, unpinned, or cannot complete a parent.

    `scheduled` is the Snakemake dry run's jobs, `(artifact_root, store_id)` or
    `(artifact_root, store_id, step)`. Every check happens before the entry
    point snapshots anything or a build writes a Store. The returned
    `RuntimeIdentity` is what the run records as its runtime.

    `handoff` is the entry point's `RuntimeIdentity.handoff()`, which the
    Snakefile's `onstart` check receives through `VERIFIED_RUNTIME_ENV`. Its
    commands are not probed again when it names this executable at this
    revision. The pin is checked either way.
    """
    jobs = normalize_scheduled(scheduled)
    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    revision = (
        run.get_opengwasdb_revision(executable=resolved)
        if resolved != run.UNAVAILABLE
        else run.UNAVAILABLE
    )
    version = run.get_opengwasdb_version(executable=resolved)

    if resolved == run.UNAVAILABLE:
        raise RuntimeCapabilityError(
            "no opengwasdb executable was found on PATH. The supported release "
            "invocation runs the environment's pinned runtime; run it through "
            "`pixi run release ...` rather than an ad-hoc executable."
        )

    # Fail closed: an unknown or unprovable identity is not the pin. A git ref,
    # empty metadata or "unavailable" must never pass as the tracked runtime.
    # This verified identity, not the `--help` probes below, is what licenses the
    # format/by_variant guarantee: the pinned revision is the one documented to
    # write format 0.2.0 with the required Ragged/Overflow index. A subcommand
    # that answers `--help` proves only that the CLI surface exists.
    if not run.is_exact_commit_hash(revision) or revision != PINNED_OPENGWASDB_REVISION:
        raise RuntimeRevisionError(
            f"the resolved opengwasdb at {resolved!r} reports revision {revision!r}, which "
            f"is not the tracked pin {PINNED_OPENGWASDB_REVISION} ({OPENGWASDB_GIT_URL}). A "
            "supported release run must use the pinned runtime and must be able to prove "
            "its identity from the executable's own metadata; do not substitute an "
            "executable on PATH. Run `pixi install`/`pixi run release` so the tracked lock "
            "selects the pin, or repin the repository deliberately (stores#218)."
        )

    steps_by_store = _steps_by_store(jobs)
    produced = _produces_store(jobs)

    # Required commands come only from scheduled steps. A store with no resolved
    # step (only build_manifest/register scheduled) needs no capability; a
    # caller that gave no step at all is treated conservatively and probed for
    # its whole plan.
    commands: dict[str, list[str]] = {}
    for (root, store_id), steps in steps_by_store.items():
        if steps:
            commands[store_id] = commands_for_scheduled_steps(
                store_id, sorted(steps), registry_root=registry_root, artifact_root=root
            )
        else:
            commands[store_id] = required_commands_for_release(
                store_id, registry_root=registry_root, artifact_root=root
            )

    parents: dict[tuple[str, str], tuple[str, set[str] | None]] = {}
    for (root, store_id), steps in steps_by_store.items():
        loaded = bundle_module.load(store_id, registry_root=registry_root)
        planned = _planned_steps(store_id, registry_root=registry_root, artifact_root=root)
        if not any(step.name == "complete" for step in planned):
            continue
        # Gate only when the child's own complete step is scheduled. A caller
        # that provided no step for this store is treated as potentially
        # completing (conservative), since the plan has a complete step.
        completion_scheduled = ("complete" in steps) if steps else True
        if not completion_scheduled or not loaded.derived_from:
            continue
        parents[(root, store_id)] = (loaded.derived_from, steps or None)
        commands.setdefault(store_id, [])
        if INFO_COMMAND not in commands.get(store_id, []):
            commands.setdefault(store_id, []).append(INFO_COMMAND)

    required = {command for store_commands in commands.values() for command in store_commands}
    handed_off = required & _handed_off_commands(handoff, executable=resolved, revision=revision)
    provided = handed_off | _probe_commands(resolved, required - handed_off, timeout=timeout)
    identity = RuntimeIdentity(
        executable=resolved,
        revision=revision,
        version=version,
        pinned_revision=PINNED_OPENGWASDB_REVISION,
        format_version=REQUIRED_FORMAT_VERSION,
        commands=tuple(sorted(provided)),
    )

    missing: dict[str, list[str]] = {}
    for store_id, store_commands in commands.items():
        for command in store_commands:
            if command not in provided:
                missing.setdefault(store_id, []).append(command)
    if missing:
        details = "; ".join(
            f"{store_id} needs {', '.join(sorted(set(cmds)))}"
            for store_id, cmds in sorted(missing.items())
        )
        raise RuntimeCapabilityError(
            f"the resolved opengwasdb runtime at {resolved!r} (revision {revision!r}) "
            f"does not provide every required capability: {details}. Install the "
            f"repository's pinned runtime (pixi.lock revision {PINNED_OPENGWASDB_REVISION}) "
            "and re-run; never substitute a different executable."
        )

    for (root, store_id), (parent_id, _steps) in sorted(parents.items()):
        if (root, parent_id) in produced:
            # A same-run build or complete of the parent produces a fresh
            # format-0.2.0 Store; the stale on-disk artifact is not consumed.
            continue
        parent_store = paths.store_path(parent_id, root=root)
        if not parent_store.is_dir():
            raise CompletionParentFormatError(
                f"Release {store_id} is Reference-Completed from parent {parent_id}, but "
                f"no Store exists at {parent_store} and no `build`/`complete` step for "
                f"{parent_id} is scheduled in this run. Schedule the parent build first "
                "(lineage does this automatically) or correct `derived_from`."
            )
        format_version = _read_store_format_version(resolved, parent_store, timeout=timeout)
        if format_version != REQUIRED_FORMAT_VERSION:
            raise CompletionParentFormatError(
                f"Release {store_id} is Reference-Completed from parent {parent_id}, whose "
                f"existing Store at {parent_store} is format_version={format_version!r}. A "
                f"format-{REQUIRED_FORMAT_VERSION} builder reads but cannot write that "
                "format; completion preserves its source's format (opengwas/opengwasdb ADR "
                "0038 §4). Convert the parent to format 0.2.0 with the documented core "
                "conversion procedure or rebuild the parent at the pinned runtime, then "
                "complete the converted release. See docs/runtime.md; upgrading the runtime "
                "does not convert or requalify an existing Store."
            )

    return identity


__all__ = [
    "CompletionParentFormatError",
    "INFO_COMMAND",
    "OPENGWASDB_GIT_URL",
    "PINNED_OPENGWASDB_REVISION",
    "REQUIRED_FORMAT_VERSION",
    "RuntimeCapabilityError",
    "RuntimeGuardError",
    "RuntimeIdentity",
    "RuntimeRevisionError",
    "ScheduledJob",
    "VERIFIED_RUNTIME_ENV",
    "commands_for_scheduled_steps",
    "completion_parents_for_release",
    "normalize_scheduled",
    "required_commands_for_release",
    "verify_runtime_for_releases",
]
