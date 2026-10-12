"""Pre-write runtime guard for the supported Phase A release entry point (#218).

The ordinary release invocation is `pixi run release`. This module is the one
place that proves, *before any artifact is written*, that the `opengwasdb`
runtime that run will actually execute:

* is the environment's tracked, immutable pin (the pixi lock's git revision),
  so a supported run does not silently follow an ad-hoc executable substituted
  onto `PATH`;
* provides every subcommand the scheduled Release Bundles plan to run; and
* can complete each scheduled Reference-Completed release, i.e. its existing
  parent Store is format 0.2.0.

The last point is a *format* gate, not a Store inspection: a 0.1.0 parent can
be read but not completed by a format-0.2.0 builder (opengwas/opengwasdb#247,
`opengwasdb.store.open.check_completion_source`), and the supported order is to
convert the parent first (`scripts/convert_store_to_0_2_0.py`) or rebuild it.
Asking the runtime's own `info --format json` reports the parent's declared
format version through the core's supported interface, so this repository
never opens the Store itself (`CONTRIBUTING.md`).

This is deliberately strict. A missing capability or a format-0.1.0 parent is
refused with the release and the reason rather than discovered hours into a
build, which is the same discipline ADR 0034 applied to Indexed Variant
Subsets. The guard runs from the entry point and, as defence in depth, from the
Snakefile's `onstart` hook for a direct `snakemake` run.

See docs/spec/store-release-workflow.md and ADR 0034.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
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
PINNED_OPENGWASDB_REVISION: str = "1be2a156649e4d444f31f9b4107c3f47a046f855"
OPENGWASDB_GIT_URL: str = "https://github.com/opengwas/opengwasdb"

#: The Store format the pinned builders write and the only format Reference
#: Completion may consume. It lives in the core, not here; this is the value the
#: guard reports and compares against `opengwasdb info`.
REQUIRED_FORMAT_VERSION: str = "0.2.0"

#: Command the guard probes in addition to those a scheduled plan names, for the
#: completion-parent format gate.
INFO_COMMAND: str = "info"

DEFAULT_PROBE_TIMEOUT_SECONDS: float = 60.0


class RuntimeGuardError(RuntimeError):
    """A scheduled run would use a runtime this release workflow refuses."""


class RuntimeCapabilityError(RuntimeGuardError):
    """The resolved `opengwasdb` does not provide a command a scheduled plan needs."""


class RuntimeRevisionError(RuntimeGuardError):
    """The resolved `opengwasdb` is a known revision other than the tracked pin."""


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

    def describe(self) -> str:
        """One line an operator can copy into a run note or issue comment."""
        return (
            f"opengwasdb {self.version} revision {self.revision} at {self.executable} "
            f"(pinned {self.pinned_revision}; writes format {self.format_version})"
        )


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


def required_commands_for_release(
    store_id: str, *, registry_root: Path | str, artifact_root: Path | str | None = None
) -> list[str]:
    """The distinct `opengwasdb` subcommands one scheduled release's plan names."""
    loaded = bundle_module.load(store_id, registry_root=registry_root)
    commands: list[str] = []
    for step in plan(loaded, artifact_root=artifact_root):
        command = step.argv[1] if len(step.argv) > 1 else ""
        if command and command not in commands:
            commands.append(command)
    return commands


def completion_parents_for_release(
    store_id: str, *, registry_root: Path | str, artifact_root: Path | str | None = None
) -> list[str]:
    """Parent Store ids a scheduled Reference-Completed release will complete.

    Empty for an observed-only release. A parent may itself be scheduled in the
    same run, in which case it will be a fresh format-0.2.0 build and needs no
    format gate (its path does not exist yet).
    """
    loaded = bundle_module.load(store_id, registry_root=registry_root)
    if not any(step.name == "complete" for step in plan(loaded, artifact_root=artifact_root)):
        return []
    return [loaded.derived_from] if loaded.derived_from else []


def verify_runtime_for_releases(
    scheduled: Iterable[tuple[str, str]],
    *,
    registry_root: Path | str,
    executable: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> RuntimeIdentity:
    """Refuse a run whose runtime is missing, unpinned, or cannot complete a parent.

    `scheduled` is the Snakemake dry run's `[(artifact_root, store_id)]`. Every
    check happens before the entry point snapshots anything or a build writes a
    Store. The returned `RuntimeIdentity` is what the run records as its runtime.
    """
    scheduled = list(scheduled)
    resolved = executable if executable is not None else run.get_opengwasdb_executable()
    revision = (
        run.get_opengwasdb_revision(executable=resolved)
        if resolved != run.UNAVAILABLE
        else run.UNAVAILABLE
    )
    version = run.get_opengwasdb_version()

    identity = RuntimeIdentity(
        executable=resolved,
        revision=revision,
        version=version,
        pinned_revision=PINNED_OPENGWASDB_REVISION,
        format_version=REQUIRED_FORMAT_VERSION,
    )

    if resolved == run.UNAVAILABLE:
        raise RuntimeCapabilityError(
            "no opengwasdb executable was found on PATH. The supported release "
            "invocation runs the environment's pinned runtime; run it through "
            "`pixi run release ...` rather than an ad-hoc executable."
        )

    if run.is_exact_commit_hash(revision) and revision != PINNED_OPENGWASDB_REVISION:
        raise RuntimeRevisionError(
            f"the resolved opengwasdb at {resolved!r} is revision {revision}, but this "
            f"repository pins {PINNED_OPENGWASDB_REVISION} ({OPENGWASDB_GIT_URL}). A "
            "supported release run must use the pinned runtime; do not substitute an "
            "executable on PATH. Run `pixi install`/`pixi run release` so the tracked "
            "lock selects the pin, or repin the repository deliberately (stores#218)."
        )

    commands: dict[str, list[str]] = {}
    parents: dict[str, tuple[str, str]] = {}
    for root, store_id in scheduled:
        commands[store_id] = required_commands_for_release(
            store_id, registry_root=registry_root, artifact_root=root
        )
        for parent in completion_parents_for_release(
            store_id, registry_root=registry_root, artifact_root=root
        ):
            parents.setdefault(parent, (store_id, root))
    if parents:
        # The parent-format gate calls `opengwasdb info`; require the subcommand
        # explicitly so a runtime that lacks it is refused as a capability, not
        # as an unreadable Store.
        for parent_id, (child_id, _parent_root) in parents.items():
            commands.setdefault(child_id, [])
            if INFO_COMMAND not in commands[child_id]:
                commands[child_id].append(INFO_COMMAND)

    missing: dict[str, list[str]] = {}
    for store_id, store_commands in commands.items():
        for command in store_commands:
            if not _probe_command(resolved, command, timeout=timeout):
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

    for parent_id, (child_id, root) in sorted(parents.items()):
        parent_store = paths.store_path(parent_id, root=root)
        if not parent_store.is_dir():
            # A parent scheduled in the same run does not exist yet; it will be a
            # fresh format-0.2.0 build, so there is nothing to gate.
            continue
        format_version = _read_store_format_version(resolved, parent_store, timeout=timeout)
        if format_version != REQUIRED_FORMAT_VERSION:
            raise CompletionParentFormatError(
                f"Release {child_id} is Reference-Completed from parent {parent_id}, whose "
                f"existing Store at {parent_store} is format_version={format_version!r}. A "
                f"format-{REQUIRED_FORMAT_VERSION} builder reads but cannot write that "
                "format; completion preserves its source's format (opengwas/opengwasdb "
                "ADR 0038 §4). Convert the parent first with "
                "`scripts/convert_store_to_0_2_0.py`, then complete the converted release; "
                "or rebuild the parent at the pinned runtime. Upgrading the runtime does "
                "not convert or requalify an existing Store."
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
    "completion_parents_for_release",
    "required_commands_for_release",
    "verify_runtime_for_releases",
]
