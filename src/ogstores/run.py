"""Execute one Step, record it, and enforce staged release transaction safety.

Runs the argv, captures stdout/stderr/timing/exit status, and writes
`records/<step>.json` including the argv as executed and the exact `opengwasdb`
revision actually used -- which `register` later compares against `plan()`'s
planned argv, so drift between what was documented and what ran is caught.

Staged release transaction lifecycle (ADR 0022, ADR 0023):
* Staged isolation: All release execution steps ('build' / 'complete' -> 'top-hits'
  -> 'rho' -> 'overview' -> 'validate') execute against `store.opengwasdb.partial`.
* Variant-reference pre-stage (#145/#147): 'variant-reference' is not store-producing
  and takes no staging rewrite. If its declared destination already exists at runtime
  no subprocess runs and a success record is written with `skipped: true`; otherwise
  `extract-variant-reference` writes to a staged sibling that is atomically renamed
  into place on success and removed on failure, so a partial artifact is never left
  behind to satisfy a later existence check.
* Exact canonical destination rewriting: planned argv targets `store.opengwasdb`;
  `rewrite_argv_for_staging()` rewrites the single target store token/substring to
  `store.opengwasdb.partial` during execution while preserving parent inputs. A
  completion-phase step of a release that completes its own Store (ADR 0037) names
  the published Store and the staged one with the same token; the argument its Step
  marks as the published Store (`published_store_arg`) is left as it is.
* Completion resume (#305): before a fresh `complete-dense` starts, its planned argv
  is recorded (`paths.completion_start_path`). An interrupted one leaves its checkpoint
  beside the staged Store; the next `complete` runs `complete-dense-resume` from it
  (`resume_argv_for_completion`) when that record holds the argv planned now, and
  refuses, naming the difference, when it does not or there is none.
* Rejection of invalid paths: Non-canonical equivalent tokens, embedded substrings,
  symlinks (both live and dangling), or non-directories are rejected before execution via lstat/no-follow.
* Publication gating: Publication is permitted ONLY on the terminal 'validate' step
  (or via explicit `publish_store()` which asserts a successful 'validate' record
  exists). Premature publication or publication on plans lacking 'validate' is rejected.
* Force timing: Staging in `store.opengwasdb.partial` is permitted beside an existing
  final Store without force; `force=True` is required only at terminal publication.
  The entry point (`ogstores.release`) refuses, before Snakemake starts, a run
  that would rebuild a published release unless it was named with
  `--config force=1` (`prepare_release_run`, #195).
* Forced replacement (#195): the entry point snapshots the release's records first,
  and restores them if the run fails before publishing (`settle_force_snapshots`).
  Publication itself is `register.complete_publication`'s transaction. A replaced
  Store is renamed into `replaced/<UTC>/`, never deleted, and so is a `.backup`
  that crash recovery finds; an archive failure is raised, never swallowed.
* Zero contamination: Any failed or interrupted step leaves any pre-existing final
  Store and `validation.yaml` completely untouched without needing whole-Store copying.
  A failed new release leaves no final Store.
* Process group isolation: Commands launch in an isolated session/group
  (`start_new_session=True`) led by a small supervisor (`ogstores._pdeath_supervisor`).
  On timeout, SIGINT, or nonzero exit, the process group PGID is terminated and
  reaped to prevent orphaned workers from continuing to write; SIGTERM is followed
  by a grace window before SIGKILL, and the supervisor absorbs the group's SIGTERM
  rather than escalating so this module's sequence stays authoritative. The
  supervisor also holds the read end of a parent-liveness pipe whose write end only
  this process holds, so an abrupt death of this process (SIGKILL included) makes the
  supervisor SIGKILL the detached group instead of leaving it running (external
  cgroups/containers are still required if processes escape the group via setsid).
* Step.name security: Step names are validated against an allowlist and strictly
  reject path separators ('/', '\\') or '..' traversal.
* Preflight failure records: Safely representable errors during preflight write a failed
  record (`success: false`) before raising.
* Staging normalization rule (#117): Planned argv contains `<root>/<store_id>/store.opengwasdb`;
  executed argv contains `<root>/<store_id>/store.opengwasdb.partial`. Registration (#115/#117)
  reconciles planned vs executed argv by normalizing this single target Store path.

It does not read a step's output back, interpret it, or re-validate it (ADR 0023).

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0026.
"""

from __future__ import annotations

import datetime
import difflib
import glob
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ogstores import paths
from ogstores.bundle import (
    INDEXED_SUBSET_STEP_PREFIX,
    Bundle,
    is_valid_indexed_subset_name,
)
from ogstores.bundle import load as load_bundle
from ogstores.plan import (
    BUILD_PHASE,
    COMPLETION_PHASE,
    COMPLETION_STEP_PREFIX,
    OBSERVED_VIEW_CHECK_STEP,
    REGISTER_STEPS,
    Step,
    completion_step_name,
    plan,
)

STORE_PRODUCING_STEPS: frozenset[str] = frozenset({"build", "complete"})
VARIANT_REFERENCE_STEP: str = "variant-reference"
# The completion phase of a release that completes its own Store names its
# post steps and its register apart from the build phase's (ADR 0037).
VALID_STEP_NAMES: frozenset[str] = frozenset({
    "build",
    "complete",
    "variant-reference",
    "top-hits",
    "rho",
    "overview",
    "validate",
    *(completion_step_name(name) for name in ("rho", "overview", "validate")),
    OBSERVED_VIEW_CHECK_STEP,
    *REGISTER_STEPS.values(),
    "unknown",
})
UNAVAILABLE: str = "unavailable"
_HEX_40_OR_64: re.Pattern[str] = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

# Bootstrap for the isolated-group supervisor (ogstores._pdeath_supervisor),
# which imports `ogstores` from the environment it is installed in (#257).
# argv layout after the code string: <liveness-fd> <exec-status-fd> <command...>.
_SUPERVISOR_BOOTSTRAP: str = (
    "import sys; "
    "liveness_fd, exec_status_fd, *cmd = sys.argv[1:]; "
    "from ogstores._pdeath_supervisor import main; "
    "raise SystemExit(main(cmd, int(liveness_fd), int(exec_status_fd)))"
)


# Test-only fault injection at the named boundaries of a publication (#195).
# Each holds a comma-separated list of boundary names.
KILL_AT_ENV: str = "OGSTORES_TEST_KILL_AT"
KILL_GROUP_AT_ENV: str = "OGSTORES_TEST_KILL_GROUP_AT"
PAUSE_AT_ENV: str = "OGSTORES_TEST_PAUSE_AT"
PAUSE_FILE_ENV: str = "OGSTORES_TEST_PAUSE_FILE"
# Set by the entry point for the Snakemake run it starts, so a job knows whose
# run it belongs to (and a whole-stack test kill can reach the entry point).
ENTRY_POINT_PID_ENV: str = "OGSTORES_ENTRY_POINT_PID"


def fault_boundary(name: str) -> None:
    """A named point between two durable steps, where tests inject faults (#195).

    Test-only, through environment variables that hold comma-separated names:
    - `OGSTORES_TEST_KILL_AT`: this process exits there with status 137, as
      SIGKILL would leave it, with no `finally`, hook or cleanup;
    - `OGSTORES_TEST_KILL_GROUP_AT`: the whole stack is SIGKILLed, as a host
      or terminal crash would kill it: the entry point (named by
      `OGSTORES_ENTRY_POINT_PID`) first, then this process's group;
    - `OGSTORES_TEST_PAUSE_AT`: this process writes its pid to the file named
      by `OGSTORES_TEST_PAUSE_FILE`, and waits there until that file is
      removed, so a test can act while a run is live.
    None of them is set outside the test suites.
    """
    if name in os.environ.get(KILL_AT_ENV, "").split(","):
        os._exit(137)
    if name in os.environ.get(KILL_GROUP_AT_ENV, "").split(","):
        entry_point = os.environ.get(ENTRY_POINT_PID_ENV)
        if entry_point and int(entry_point) != os.getpid():
            try:
                os.kill(int(entry_point), signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.killpg(os.getpgrp(), signal.SIGKILL)
    if name in os.environ.get(PAUSE_AT_ENV, "").split(","):
        pause_file = Path(os.environ[PAUSE_FILE_ENV])
        pause_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        while pause_file.exists():
            time.sleep(0.1)


class PublicationPendingError(RuntimeError):
    """Raised when a release has a publication that has not finished (`publication.json`)."""

    pass


class StoreExistsError(FileExistsError):
    """Raised when target Store artifact already exists and force=False."""

    pass


class StepExecutionError(RuntimeError):
    """Raised when a Step execution fails (nonzero exit, missing command, or publication failure)."""

    def __init__(self, message: str, result: StepResult) -> None:
        super().__init__(message)
        self.result = result


class MissingCommandError(StepExecutionError, FileNotFoundError):
    """Raised when the step executable command cannot be found."""

    pass


@dataclass(frozen=True)
class StepResult:
    """Captured result of executing one Step."""

    step: str
    store_id: str
    exit_code: int
    success: bool
    start_time: str
    end_time: str
    elapsed_seconds: float
    argv: list[str]
    planned_argv: list[str]
    inputs: list[str]
    outputs: list[str]
    opengwasdb_rev: str
    opengwasdb_version: str
    opengwasdb_executable: str
    stdout: str
    stderr: str
    record_path: str
    published_store: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to a JSON-serializable dictionary for records/<step>.json."""
        return asdict(self)


def validate_step_name(name: str) -> str:
    """Validate a step name against the allowlist and path-traversal rules.

    A fixed name must be in :data:`VALID_STEP_NAMES`. A declared Indexed Variant
    Subset (#209) is allowed as ``indexed-subset-<name>``, where ``<name>`` is a
    valid subset name; the prefix bounds the dynamic space without opening the
    record filename to an arbitrary string. A completion phase's subset step is
    ``completion-indexed-subset-<name>`` (ADR 0037).
    """
    if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
        raise ValueError(
            f"Invalid step name {name!r}; must be one of {sorted(VALID_STEP_NAMES)} "
            f"or '{INDEXED_SUBSET_STEP_PREFIX}<name>', and contain no path "
            "separators or traversal sequences."
        )
    if name in VALID_STEP_NAMES:
        return name
    subset_step = name.removeprefix(COMPLETION_STEP_PREFIX)
    if subset_step.startswith(INDEXED_SUBSET_STEP_PREFIX):
        subset_name = subset_step[len(INDEXED_SUBSET_STEP_PREFIX):]
        if is_valid_indexed_subset_name(subset_name):
            return name
    raise ValueError(
        f"Invalid step name {name!r}; must be one of {sorted(VALID_STEP_NAMES)} "
        f"or '{INDEXED_SUBSET_STEP_PREFIX}<name>', and contain no path "
        "separators or traversal sequences."
    )


def is_exact_commit_hash(s: str | None) -> bool:
    """Return True if s is an exact 40-character or 64-character hex commit SHA."""
    return bool(isinstance(s, str) and _HEX_40_OR_64.fullmatch(s.strip()))


def get_opengwasdb_executable(cmd: str = "opengwasdb", env: Mapping[str, str] | None = None) -> str:
    """Return the absolute path of the opengwasdb executable or 'unavailable'."""
    path_val = env.get("PATH") if env is not None else None
    resolved = shutil.which(cmd, path=path_val)
    if resolved:
        return str(Path(resolved).resolve())
    cmd_p = Path(cmd)
    if cmd_p.is_file():
        return str(cmd_p.resolve())
    return UNAVAILABLE


def _extract_commit_from_direct_url_file(direct_url_p: Path) -> str | None:
    """Extract and validate commit hash from a direct_url.json file."""
    try:
        if direct_url_p.is_file():
            data = json.loads(direct_url_p.read_text(encoding="utf-8"))
            vcs_info = data.get("vcs_info")
            if isinstance(vcs_info, dict):
                commit = vcs_info.get("commit_id")
                if is_exact_commit_hash(commit):
                    return str(commit).strip()
            commit = data.get("commit_id")
            if is_exact_commit_hash(commit):
                return str(commit).strip()
    except Exception:
        pass
    return None


def _executable_metadata_roots(
    executable: str, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Candidate install-prefix roots to read `opengwasdb*.dist-info` metadata from.

    An explicit executable is trusted over ambient process metadata: the guard
    must report the runtime it will actually run, not whichever `opengwasdb`
    happens to be importable by the current interpreter.
    """
    exe_p = Path(executable).resolve()
    candidate_roots = [
        exe_p.parents[1] if len(exe_p.parents) > 1 else exe_p.parent,
        exe_p.parent,
    ]
    if env and "PYTHONPATH" in env:
        for pth in env["PYTHONPATH"].split(os.pathsep):
            if pth.strip():
                candidate_roots.append(Path(pth.strip()).resolve())
    if env and "CONDA_PREFIX" in env:
        candidate_roots.append(Path(env["CONDA_PREFIX"]).resolve())
    if env and "VIRTUAL_ENV" in env:
        candidate_roots.append(Path(env["VIRTUAL_ENV"]).resolve())
    return candidate_roots


def _dist_info_globs(c_root: Path, filename: str) -> list[str]:
    """The platform layouts a `opengwasdb*.dist-info` directory may use."""
    return [
        str(c_root / "lib" / "python*" / "site-packages" / "opengwasdb*.dist-info" / filename),
        str(c_root / "Lib" / "site-packages" / "opengwasdb*.dist-info" / filename),
        str(c_root / "opengwasdb*.dist-info" / filename),
    ]


def get_opengwasdb_revision(
    executable: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the exact resolved 40/64-char opengwasdb git commit hash bound to executable/env."""
    # 1. If an explicit executable is provided, search only its environment prefix
    if executable and executable != UNAVAILABLE:
        for c_root in _executable_metadata_roots(executable, env):
            for pattern in _dist_info_globs(c_root, "direct_url.json"):
                for direct_url_match in glob.glob(pattern):
                    commit = _extract_commit_from_direct_url_file(Path(direct_url_match))
                    if commit:
                        return commit
        return UNAVAILABLE

    # 2. Ambient process metadata (only when no explicit executable is specified)
    try:
        import importlib.metadata as metadata

        dist = metadata.distribution("opengwasdb")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            vcs_info = direct_url.get("vcs_info")
            if isinstance(vcs_info, dict):
                commit = vcs_info.get("commit_id")
                if is_exact_commit_hash(commit):
                    return str(commit).strip()
            commit = direct_url.get("commit_id")
            if is_exact_commit_hash(commit):
                return str(commit).strip()
    except Exception:
        pass

    return UNAVAILABLE


def _extract_version_from_metadata_file(metadata_p: Path) -> str | None:
    """Read `Version:` from a dist-info METADATA file, or None."""
    try:
        for line in metadata_p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
                return version or None
    except OSError:
        return None
    return None


def get_opengwasdb_version(
    executable: str | None = None, env: Mapping[str, str] | None = None
) -> str:
    """Return the opengwasdb version, from the given executable's install or ambient.

    With `executable`, read the version from that executable's own dist-info so
    the reported identity belongs to the runtime that will run, never to an
    unrelated ambient environment.
    """
    if executable and executable != UNAVAILABLE:
        for c_root in _executable_metadata_roots(executable, env):
            for pattern in _dist_info_globs(c_root, "METADATA"):
                for metadata_match in glob.glob(pattern):
                    version = _extract_version_from_metadata_file(Path(metadata_match))
                    if version:
                        return version
        return UNAVAILABLE
    try:
        import importlib.metadata as metadata

        ver = metadata.version("opengwasdb")
        return str(ver).strip() if ver else UNAVAILABLE
    except Exception:
        return UNAVAILABLE


def is_store_producing_step(step: Step) -> bool:
    """Return True if `step` creates a new Store (i.e. 'build' or 'complete')."""
    return step.name in STORE_PRODUCING_STEPS


def _lstat_exists(p: Path) -> bool:
    """Return True if path exists in directory table (including live or dangling symlinks)."""
    try:
        os.lstat(p)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False


def _assert_real_directory(p: Path, name: str) -> None:
    """Ensure path is a real directory and not a symlink (both live and dangling)."""
    try:
        st = os.lstat(p)
    except (FileNotFoundError, ProcessLookupError):
        return

    if stat.S_ISLNK(st.st_mode) or os.path.islink(p):
        raise ValueError(f"{p} is a symlink; symlinks are prohibited")
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{name} artifact at {p} must be a directory")


def _preflight_paths(store_id: str, root: Path) -> None:
    """Preflight check release directory, partial, target, and backup with lstat no-follow."""
    s_dir = paths.store_dir(store_id, root=root)
    _assert_real_directory(s_dir, "store directory")
    _assert_real_directory(paths.partial_store_path(store_id, root=root), "partial store")
    _assert_real_directory(paths.store_path(store_id, root=root), "target store")
    _assert_real_directory(s_dir / "store.opengwasdb.backup", "backup store")


def rewrite_argv_for_staging(
    step: Step,
    store_id: str,
    artifact_root: Path | str = paths.DEFAULT_ARTIFACT_ROOT,
) -> list[str]:
    """Rewrite target store references in argv to store.opengwasdb.partial for staged transactions.

    Strictly validates destination tokens and substrings:
    - Requires exactly one occurrence of the canonical target Store path for every release step.
    - Replaces exact target store path occurrences with the canonical partial store path.
    - Replaces embedded target path occurrences (e.g. `--flag=/data/.../store.opengwasdb`).
    - Rejects missing or multiple ambiguous destination tokens before execution.
    - Rejects non-canonical equivalent path tokens (relative paths, trailing slashes) before execution.
    - Preserves parent store paths and non-target arguments verbatim.
    - Leaves the argument a completion-phase step marks as the release's own
      published Store (`Step.published_store_arg`, ADR 0037) as it is. It is
      the same canonical token as the destination, so it must be that token
      exactly, and is not counted among the destinations.
    """
    validate_step_name(step.name)
    target_store = paths.store_path(store_id, root=artifact_root)
    partial_store = paths.partial_store_path(store_id, root=artifact_root)

    target_str = str(target_store)
    partial_str = str(partial_store)
    target_resolved = target_store.resolve()

    published_arg = step.published_store_arg
    if published_arg is not None and (
        not 0 <= published_arg < len(step.argv) or step.argv[published_arg] != target_str
    ):
        raise ValueError(
            f"Step {step.name!r} for {store_id} marks argument {published_arg} as the published "
            f"Store it reads, but that argument is not {target_str!r}: {step.argv}"
        )

    rewritten: list[str] = []
    replacement_count = 0

    for index, token in enumerate(step.argv):
        if index == published_arg:
            rewritten.append(token)
            continue
        val = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token

        is_equivalent = False
        try:
            is_equivalent = (Path(val).resolve() == target_resolved)
        except Exception:
            pass

        if is_equivalent:
            if val != target_str:
                raise ValueError(
                    f"Step {step.name!r} for {store_id} contains non-canonical equivalent destination "
                    f"token {token!r}; expected exact canonical token {target_str!r}"
                )
            if token == target_str:
                rewritten.append(partial_str)
            else:
                prefix = token.split("=", 1)[0] + "="
                rewritten.append(prefix + partial_str)
            replacement_count += 1
        else:
            rewritten.append(token)

    if replacement_count == 0:
        raise ValueError(
            f"Step {step.name!r} for {store_id} is missing expected canonical destination "
            f"token {target_str!r} in argv: {step.argv}"
        )
    if replacement_count > 1:
        raise ValueError(
            f"Step {step.name!r} for {store_id} contains multiple ({replacement_count}) ambiguous "
            f"matches for destination token {target_str!r} in argv: {step.argv}"
        )

    return rewritten


def argv_change(before: list[str], now: list[str]) -> str:
    """The token runs that differ between two argv, each with the flag it belongs to."""
    changes: list[str] = []
    matcher = difflib.SequenceMatcher(a=before, b=now, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], now[j1:j2]
        if i1 > 0 and before[i1 - 1].startswith("-") and not (old or new)[0].startswith("-"):
            old, new = [before[i1 - 1], *old], [before[i1 - 1], *new]
        changes.append(f"`{' '.join(old) or 'nothing'}` became `{' '.join(new) or 'nothing'}`")
    return "; ".join(changes)


COMPLETE_DENSE: str = "complete-dense"
# `complete-dense-resume` takes the checkpoint and, besides it, only how many
# workers to run (opengwasdb CLI). A planned `--n-workers` is passed on, so a
# resumed completion runs as the plan asked; it sets how the work runs, not
# what it computes.
_RESUME_PASSES_ON: tuple[str, ...] = ("--n-workers",)


def _started_completion_argv(store_id: str, root: Path | str) -> list[str] | None:
    """The planned argv this workflow recorded when it started the completion whose checkpoint exists."""
    try:
        recorded = json.loads(paths.completion_start_path(store_id, root=root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    argv = recorded.get("planned_argv") if isinstance(recorded, dict) else None
    return argv if isinstance(argv, list) else None


def resume_argv_for_completion(
    step: Step, executed_argv: list[str], store_id: str, artifact_root: Path | str
) -> list[str]:
    """Resume an interrupted `complete-dense` from its checkpoint, or return its argv unchanged (#305).

    `complete-dense` keeps a checkpoint beside the staged Store it writes
    (`paths.completion_checkpoint_path`) and refuses to start while one
    exists. Completion takes hours at UKB-B scale, so an interrupted one is
    resumed, not restarted, with `complete-dense-resume <checkpoint>`, which
    takes every parameter from the checkpoint. `execute_step` records the
    step's planned argv before it starts a fresh `complete-dense`
    (`paths.completion_start_path`), so a checkpoint is resumed only when that
    record holds the argv planned now. A different plan, or a checkpoint this
    workflow has no record of starting, is refused, naming it, and nothing
    runs. Only stores' own planned argv is compared: no `opengwasdb` option or
    checkpoint key is interpreted here (ADR 0023). An unforced run never
    discards a checkpoint, even for a planned `--overwrite`; a forced run's
    preflight does (`prepare_release_run`). `register` accepts the
    substitution and records the release as resumed.
    """
    checkpoint = paths.completion_checkpoint_path(store_id, root=artifact_root)
    if step.name != "complete" or executed_argv[1:2] != [COMPLETE_DENSE] or not _lstat_exists(checkpoint):
        return executed_argv
    started = _started_completion_argv(store_id, artifact_root)
    if started is None:
        raise ValueError(
            f"Step {step.name!r} for {store_id} finds the checkpoint {checkpoint}, but this workflow has no "
            f"record of starting it ({paths.completion_start_path(store_id, root=artifact_root)}): it was "
            "made outside this workflow, or by an older one. Nothing was resumed. `--config force=1` "
            "discards it and rebuilds the release."
        )
    if started != list(step.argv):
        raise ValueError(
            f"Step {step.name!r} for {store_id} would resume the checkpoint {checkpoint}, but it was started "
            f"from another plan: {argv_change(started, list(step.argv))}. Nothing was resumed. "
            "`--config force=1` discards it and rebuilds the release."
        )
    resume = [executed_argv[0], "complete-dense-resume", str(checkpoint)]
    for i, token in enumerate(executed_argv):
        name = token.split("=", 1)[0]
        if name in _RESUME_PASSES_ON:
            resume.extend([token] if "=" in token else executed_argv[i:i + 2])
    return resume


def variant_reference_partial_path(target: Path | str) -> Path:
    """Sibling path a variant-reference extraction writes before atomic rename.

    The declared destination is never written directly: a failed or interrupted
    extraction must not leave a file that a later run's existence check would
    mistake for a provided reference (#147). The staged name is deterministic so
    `register` can normalise the executed argv back to the planned argv.
    """
    target_p = Path(target)
    return target_p.with_name(f"{target_p.name}.partial")


def rewrite_argv_for_variant_reference(step: Step, target: Path | str) -> list[str]:
    """Rewrite the extraction's `--output-path` to its staged sibling path.

    Exactly one `--output-path` token (space- or `=`-separated) is required; a
    missing or ambiguous destination is rejected before execution.
    """
    partial = variant_reference_partial_path(target)
    rewritten: list[str] = []
    replacement_count = 0
    i = 0
    argv = step.argv
    while i < len(argv):
        token = argv[i]
        if token == "--output-path" and i + 1 < len(argv):
            rewritten.extend([token, str(partial)])
            replacement_count += 1
            i += 2
            continue
        if token.startswith("--output-path="):
            rewritten.append(f"--output-path={partial}")
            replacement_count += 1
            i += 1
            continue
        rewritten.append(token)
        i += 1

    if replacement_count == 0:
        raise ValueError(
            f"Step {step.name!r} is missing a '--output-path' destination in argv: {argv}"
        )
    if replacement_count > 1:
        raise ValueError(
            f"Step {step.name!r} contains multiple ({replacement_count}) ambiguous "
            f"'--output-path' destinations in argv: {argv}"
        )
    return rewritten


def _remove_path_if_exists(p: Path) -> None:
    """Best-effort removal of a staged file or symlink, never following links."""
    try:
        if os.path.islink(p):
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        elif _lstat_exists(p):
            p.unlink()
    except Exception:
        pass


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of a directory to persist directory metadata on POSIX."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def _write_record_atomically(record_data: dict[str, Any], record_p: Path) -> None:
    """Atomically write record_data to record_p using a temp file + os.replace."""
    record_p.parent.mkdir(parents=True, exist_ok=True)
    temp_p = record_p.with_name(f".{record_p.name}.tmp.{os.getpid()}.{time.time_ns()}")
    payload = json.dumps(record_data, indent=2, sort_keys=False) + "\n"
    with open(temp_p, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_p, record_p)
    _fsync_dir(record_p.parent)


def _recover_pending_backup(store_id: str, root: Path) -> None:
    """Recover a `store.opengwasdb.backup` left by an interrupted `publish_store(force=True)`.

    A backup that belongs to a pending `register` publication (its
    `publication.json` exists) is not touched here: only `complete_publication`
    knows which way that one goes, and the entry point runs it first (#195).
    Otherwise a backup with no Store is put back, and a backup beside a
    published Store is archived under `replaced/<UTC>/`, never deleted. A
    failure is raised, not swallowed.
    """
    target_p = paths.store_path(store_id, root=root)
    backup_p = paths.backup_store_path(store_id, root=root)

    _assert_real_directory(backup_p, "backup store")
    _assert_real_directory(target_p, "target store")
    if not _lstat_exists(backup_p):
        return
    marker = paths.publication_marker(store_id, root=root)
    if _lstat_exists(marker):
        raise PublicationPendingError(
            f"{backup_p} belongs to the pending publication {marker}; "
            "`pixi run release` completes it before anything else runs"
        )

    if not _lstat_exists(target_p):
        # Interrupted after moving target -> backup but before publishing partial.
        backup_p.rename(target_p)
        _fsync_dir(target_p.parent)
    else:
        # The new Store was published but the old one never archived.
        archived = target_p.parent / paths.REPLACED_DIRNAME / utc_stamp() / target_p.name
        if _lstat_exists(archived):
            raise FileExistsError(f"Cannot archive {backup_p}: {archived} already exists")
        archived.parent.mkdir(parents=True, exist_ok=True)
        backup_p.rename(archived)
        _fsync_dir(archived.parent)
        _fsync_dir(target_p.parent)


def _safe_publish_partial_store(
    partial_p: Path,
    target_p: Path,
    backup_p: Path,
    force: bool = False,
    archive_dir: Path | str | None = None,
) -> None:
    """Safely publish partial_p to target_p with rollback protection, backup, and fsync.

    A forced replacement keeps the old Store: once the new one is in place, the
    old one is renamed into `archive_dir` (default `replaced/<UTC>/` beside the
    target), and nothing deletes it (#195).
    """
    _assert_real_directory(partial_p, "partial store")
    _assert_real_directory(target_p, "target store")
    _assert_real_directory(backup_p, "backup store")

    if not _lstat_exists(partial_p):
        raise FileNotFoundError(f"Partial store artifact does not exist at {partial_p}")

    target_p.parent.mkdir(parents=True, exist_ok=True)
    _fsync_dir(target_p.parent)

    if _lstat_exists(target_p):
        if not force:
            raise StoreExistsError(
                f"Target store already exists at {target_p}. Set force=True to replace."
            )
        archive = (
            Path(archive_dir)
            if archive_dir is not None
            else target_p.parent / paths.REPLACED_DIRNAME / utc_stamp()
        )
        archived_store = archive / target_p.name
        if _lstat_exists(archived_store):
            raise FileExistsError(
                f"Refusing to replace {target_p}: its archive {archived_store} already exists"
            )
        try:
            target_p.rename(backup_p)
            _fsync_dir(target_p.parent)
            partial_p.rename(target_p)
            _fsync_dir(target_p.parent)
        except BaseException as exc:
            # Rollback: restore backup if target_p is missing
            if _lstat_exists(backup_p) and not _lstat_exists(target_p):
                try:
                    backup_p.rename(target_p)
                    _fsync_dir(target_p.parent)
                except Exception:
                    pass
            raise RuntimeError(
                f"Failed to replace existing store at {target_p}: {exc}"
            ) from exc

        # Archive the old Store. target_p is already published, so a failure here
        # is raised, not swallowed: the old Store stays at backup_p, nothing claims
        # an archive it is not in, and _recover_pending_backup archives it later (#195).
        try:
            archive.mkdir(parents=True, exist_ok=True)
            backup_p.rename(archived_store)
            _fsync_dir(archive)
            _fsync_dir(target_p.parent)
        except Exception as exc:
            raise RuntimeError(
                f"Published {target_p}, but could not archive the Store it replaced: it "
                f"remains at {backup_p} and is archived when the next step starts ({exc})"
            ) from exc
    else:
        partial_p.rename(target_p)
        _fsync_dir(target_p.parent)


def _terminate_process_group(
    pgid: int,
    proc: subprocess.Popen[str] | None,
    timeout: float = 2.0,
) -> tuple[str, str]:
    """Terminate, kill, and reap the entire process group spawned by proc.

    Note: POSIX process groups isolate processes spawned in the session. Processes
    that double-fork with setsid() escape group signals; containment of such processes
    requires external cgroup/container isolation.
    """
    stdout_extra = ""
    stderr_extra = ""

    # 1. SIGTERM whole process group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    if proc is not None:
        try:
            out, err = proc.communicate(timeout=timeout)
            stdout_extra = out or ""
            stderr_extra = err or ""
        except (subprocess.TimeoutExpired, BaseException):
            # 2. SIGKILL whole process group on timeout, non-zero exit, or interruption
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                out, err = proc.communicate(timeout=1.0)
                stdout_extra = out or ""
                stderr_extra = err or ""
            except Exception:
                pass
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    return stdout_extra, stderr_extra


def load_record(
    store_id: str,
    step_name: str,
    root: Path | str = paths.DEFAULT_ARTIFACT_ROOT,
) -> dict[str, Any] | None:
    """Read a step execution record from `records/<step_name>.json` if it exists."""
    try:
        validate_step_name(step_name)
    except ValueError:
        return None
    p = paths.record_path(store_id, step_name, root=root)
    if not p.is_file():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def publish_store(
    store_id: str,
    artifact_root: Path | str = paths.DEFAULT_ARTIFACT_ROOT,
    force: bool = False,
    archive_dir: Path | str | None = None,
) -> Path:
    """Atomically publish a validated staging store (store.opengwasdb.partial) to final store.opengwasdb.

    Requires that a successful 'validate' execution record exists for this release.
    With `force=True` an existing final Store is replaced and archived under
    `archive_dir` (default `replaced/<UTC>/`), never deleted (#195).
    """
    paths.require_valid_store_id(store_id)
    resolved_root = Path(artifact_root)

    # Validate that a successful validation record exists before publishing
    val_rec = load_record(store_id, "validate", root=resolved_root)
    if not val_rec or not val_rec.get("success") or val_rec.get("exit_code") != 0:
        raise ValueError(
            f"Cannot publish store for {store_id}: missing successful 'validate' record in records/."
        )

    target_p = paths.store_path(store_id, root=resolved_root)
    partial_p = paths.partial_store_path(store_id, root=resolved_root)
    backup_p = paths.store_dir(store_id, root=resolved_root) / "store.opengwasdb.backup"

    _preflight_paths(store_id, resolved_root)
    _recover_pending_backup(store_id, resolved_root)
    _safe_publish_partial_store(partial_p, target_p, backup_p, force=force, archive_dir=archive_dir)
    return target_p


class ForceSnapshotPendingError(RuntimeError):
    """Raised when a release still holds the records snapshot of an unfinished forced run."""

    pass


_FORCE_ON: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FORCE_OFF: frozenset[str] = frozenset({"", "0", "false", "no", "off", "none"})


def utc_stamp(when: datetime.datetime | None = None) -> str:
    """A forced run's UTC stamp, `YYYYMMDDTHHMMSSZ`: sortable and safe in a path (#195)."""
    moment = when or datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def force_requested(value: Any) -> bool:
    """Read the workflow's `force` config value, refusing anything that is not a clear yes or no."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _FORCE_ON:
        return True
    if text in _FORCE_OFF:
        return False
    raise ValueError(f"--config force={value!r} is not a yes or no; use force=1 to replace a release")


def forced_releases(value: Any, targets: Iterable[str]) -> frozenset[str]:
    """The releases a `--config force=1` run may replace: the ids named as its targets (#195).

    Force reaches only what the operator named. A release scheduled only as a
    dependency, or through `all`, is still refused if it is published.
    """
    if not force_requested(value):
        return frozenset()
    return frozenset(str(t) for t in targets if paths.is_valid_store_id(str(t)))


def _scheduled_releases(scheduled: Iterable[tuple[Path | str, str]]) -> list[tuple[Path, str]]:
    return sorted({
        (Path(root), store_id) for root, store_id in scheduled if paths.is_valid_store_id(store_id)
    }, key=lambda pair: (pair[1], str(pair[0])))


def pending_force_snapshots(store_id: str, root: Path | str) -> list[Path]:
    """Records snapshots a forced run of this release left behind (#195)."""
    store_dir_p = paths.store_dir(store_id, root=root)
    if not store_dir_p.is_dir():
        return []
    return sorted(store_dir_p.glob(f"{paths.FORCE_SNAPSHOT_PREFIX}*"))


def refuse_pending_force_snapshots(scheduled: Iterable[tuple[Path | str, str]]) -> None:
    """Refuse any run of a release that still holds an unfinished forced run's snapshot (#195).

    A snapshot outlives its run only when the run never finished its hooks, for
    example after SIGKILL. Whether `records/` then describes the Store beside it
    is a question for an operator, so nothing runs for that release until one
    resolves the snapshot.
    """
    leftovers = [
        (store_id, root, snapshot)
        for root, store_id in _scheduled_releases(scheduled)
        for snapshot in pending_force_snapshots(store_id, root)
    ]
    if not leftovers:
        return
    lines = []
    for store_id, root, snapshot in leftovers:
        lines.append(
            f"  {store_id}: {snapshot}\n"
            f"    restore it, if records/ no longer describes {paths.store_path(store_id, root=root)}:\n"
            f"      pixi run release {store_id} --resolve-snapshot restore\n"
            f"    or delete it, if records/ still describes that Store:\n"
            f"      pixi run release {store_id} --resolve-snapshot delete"
        )
    raise ForceSnapshotPendingError(
        "Refusing to run: a forced run of these releases stopped before it began to "
        "publish, and left the snapshot of the records it was replacing (#195):\n"
        + "\n".join(lines)
        + "\nNothing was written. Either resolution also clears Snakemake's marks that the "
        "killed run's outputs are incomplete. If Snakemake then reports the working directory "
        "locked, check that no other run is active and run "
        "`snakemake --snakefile workflow/Snakefile --unlock`."
    )


# The workflow's rules that write a phase's register record (ADR 0037).
_REGISTER_RULES: dict[str, str] = {"register": BUILD_PHASE, "completion_register": COMPLETION_PHASE}


def job_record(rule: str, step: str | None) -> str | None:
    """The record a workflow job writes: its step's, its phase's register's, or None.

    `build_manifest` writes no record. The preflight tells a completion-phase
    job from a build-phase one by this name (#305).
    """
    if step is not None:
        return step
    phase = _REGISTER_RULES.get(rule)
    return REGISTER_STEPS[phase] if phase is not None else None


def completing_own_stores(
    jobs: Iterable[tuple[Path | str, str, str | None]],
    *,
    registry_root: Path | str,
) -> frozenset[str]:
    """The releases whose every scheduled job completes their own published Observed-Only Store (#305).

    `jobs` is the (artifact root, store_id, record) of every job the run is
    about to execute, `record` as `job_record` names it. Completing that Store
    is a release's next step, not a rebuild of it (ADR 0037). Whether it is
    published and not yet completed is read from records, never from the Store
    (ADR 0023): the build phase's `records/register.json` exists and the
    completion phase's does not. Which records belong to the completion phase
    is the plan's to say, so a two-id child's `complete` stays a build of its
    Store.
    """
    scheduled: dict[tuple[Path, str], set[str | None]] = {}
    for root, store_id, record in jobs:
        if paths.is_valid_store_id(store_id):
            scheduled.setdefault((Path(root), store_id), set()).add(record)
    completing: set[str] = set()
    for (root, store_id), records in scheduled.items():
        registers = {
            phase: paths.record_path(store_id, name, root=root) for phase, name in REGISTER_STEPS.items()
        }
        if not registers[BUILD_PHASE].is_file() or _lstat_exists(registers[COMPLETION_PHASE]):
            continue
        loaded = load_bundle(store_id, registry_root=registry_root)
        if not loaded.completes_own_store:
            continue
        completion = {step.name for step in plan(loaded, artifact_root=root) if step.phase == COMPLETION_PHASE}
        completion.add(REGISTER_STEPS[COMPLETION_PHASE])
        if records <= completion:
            completing.add(store_id)
    return frozenset(completing)


def refuse_rebuilding_published_releases(
    scheduled: Iterable[tuple[Path | str, str]],
    forced: Iterable[str] = frozenset(),
    completing: Iterable[str] = frozenset(),
) -> None:
    """Refuse a workflow run that would execute a job for a published release (#195).

    `scheduled` is the (artifact root, store_id) of every job the run is about
    to execute. The workflow calls this before its first job starts, because
    Snakemake deletes a job's existing outputs before running it: a check made
    inside the job would already have cost the release its records. An
    up-to-date release schedules no job, so it never reaches this check.

    A release in `forced` (`forced_releases`) is let through to be replaced,
    and so is one in `completing` (`completing_own_stores`), whose jobs only
    complete the Observed-Only Store it published (#305). Every other
    published release is refused.
    """
    passed = frozenset(forced) | frozenset(completing)
    published = [
        (store_id, str(paths.store_path(store_id, root=root)))
        for root, store_id in _scheduled_releases(scheduled)
        if store_id not in passed and _lstat_exists(paths.store_path(store_id, root=root))
    ]
    if published:
        listed = "\n".join(f"  {store_id}: {store_p}" for store_id, store_p in published)
        raise StoreExistsError(
            "Refusing to run: these releases are already published, and this run would "
            f"rebuild them, rewriting their work/ and records/:\n{listed}\n"
            "Nothing was written. To replace one, name its id as a target and add "
            "`--config force=1`; the replaced Store, its records and its validation.yaml "
            "are archived under replaced/<UTC>/ beside it (#195)."
        )


def prepare_release_run(
    scheduled: Iterable[tuple[Path | str, str]],
    *,
    forced: Iterable[str] = frozenset(),
    stamp: str | None = None,
    completing: Iterable[str] = frozenset(),
) -> list[Path]:
    """The entry point's preflight: refuse, then snapshot what a forced run may replace (#195).

    The entry point (`ogstores.release`) calls this after it has completed any
    pending publication and before Snakemake starts. It refuses a scheduled
    release that still has a pending publication or a leftover snapshot, and
    an unforced published release unless it is `completing` its own
    Observed-Only Store (#305), so a refused run takes no snapshot. It also
    removes the hidden partial copy an interrupted snapshot can leave. Then, for
    each forced release whose Store is published, it copies `records/` to
    `records.before-force-<stamp>/`, keeping mtimes so a restored record is the
    one Snakemake saw. It returns the snapshots for the entry point to settle
    when the run ends.
    """
    releases = _scheduled_releases(scheduled)
    forced_ids = frozenset(forced)
    pending = [
        str(paths.publication_marker(store_id, root=root))
        for root, store_id in releases
        if _lstat_exists(paths.publication_marker(store_id, root=root))
    ]
    if pending:
        raise PublicationPendingError(
            "Refusing to run: these publications have not finished: " + ", ".join(pending)
        )
    refuse_pending_force_snapshots(releases)
    refuse_rebuilding_published_releases(releases, forced=forced_ids, completing=completing)
    for root, store_id in releases:
        store_dir_p = paths.store_dir(store_id, root=root)
        if store_dir_p.is_dir():
            for stale in store_dir_p.glob(f".{paths.FORCE_SNAPSHOT_PREFIX}*.copying"):
                shutil.rmtree(stale)
        # A forced run rebuilds the Store an interrupted completion was reading,
        # so it discards that completion's checkpoint, and the record of its
        # start, rather than resume it. Nothing else discards one (#305).
        checkpoint = paths.completion_checkpoint_path(store_id, root=root)
        if store_id in forced_ids and _lstat_exists(checkpoint):
            shutil.rmtree(checkpoint)
        if store_id in forced_ids:
            _remove_path_if_exists(paths.completion_start_path(store_id, root=root))

    run_stamp = stamp or utc_stamp()
    snapshots: list[Path] = []
    for root, store_id in releases:
        if store_id not in forced_ids or not _lstat_exists(paths.store_path(store_id, root=root)):
            continue
        snapshot = paths.force_snapshot_path(store_id, run_stamp, root=root)
        # Copied under a hidden name, then renamed, so a half-copied snapshot is
        # never mistaken for a complete one.
        staging = snapshot.with_name(f".{snapshot.name}.copying")
        records = paths.records_dir(store_id, root=root)
        if records.is_dir():
            shutil.copytree(records, staging, symlinks=True)
        else:
            staging.mkdir(parents=True)
        staging.rename(snapshot)
        _fsync_dir(snapshot.parent)
        snapshots.append(snapshot)
    return snapshots


def archive_force_snapshot(snapshot: Path, archive_dir: Path | None = None) -> Path:
    """Move a forced run's records snapshot into its release's archive, as `records/` (#195)."""
    stamp = snapshot.name[len(paths.FORCE_SNAPSHOT_PREFIX):]
    archive = archive_dir or snapshot.parent / paths.REPLACED_DIRNAME / stamp
    destination = archive / "records"
    if _lstat_exists(destination):
        raise FileExistsError(f"Cannot archive {snapshot}: {destination} already exists")
    archive.mkdir(parents=True, exist_ok=True)
    snapshot.rename(destination)
    _fsync_dir(archive)
    _fsync_dir(snapshot.parent)
    return destination


def restore_force_snapshot(snapshot: Path) -> Path:
    """Put a forced run's snapshot back as `records/`, discarding the failed run's records (#195)."""
    records = snapshot.parent / "records"
    stamp = snapshot.name[len(paths.FORCE_SNAPSHOT_PREFIX):]
    aside = snapshot.parent / f".records.failed-force-{stamp}"
    if _lstat_exists(records):
        records.rename(aside)
    snapshot.rename(records)
    _fsync_dir(snapshot.parent)
    if _lstat_exists(aside):
        shutil.rmtree(aside)
    return records


def settle_force_snapshots(snapshots: Iterable[Path], *, succeeded: bool) -> None:
    """Resolve a finished run's records snapshots (#195).

    A snapshot a completed publication already archived is gone and skipped.
    A release whose publication is still pending is refused here: only
    `register.complete_publication` may finish it, and the entry point calls it
    first. Otherwise, after success the snapshot joins `replaced/<stamp>/`; after
    a failure it is restored, so `records/` again describes the Store beside it.
    """
    for snapshot in snapshots:
        if not _lstat_exists(snapshot):
            continue
        marker = snapshot.parent / paths.PUBLICATION_MARKER
        if _lstat_exists(marker):
            raise PublicationPendingError(
                f"{snapshot} belongs to the pending publication {marker}; complete it first"
            )
        if succeeded:
            archive_force_snapshot(snapshot)
        else:
            restore_force_snapshot(snapshot)


_STAMP_PATTERN: re.Pattern[str] = re.compile(r"\A\d{8}T\d{6}Z\Z")


def forced_by_transaction(
    token: str | None, scheduled: Iterable[tuple[Path | str, str]]
) -> frozenset[str]:
    """The releases this entry-point run may replace: those holding its snapshot (#195).

    The entry point passes its run's stamp to Snakemake as `release_run`.
    A release is forced only when it holds `records.before-force-<that stamp>/`,
    which only the entry point creates, so a hand-written `--config` reaches
    nothing by accident.
    """
    if not token:
        return frozenset()
    if not _STAMP_PATTERN.match(str(token)):
        raise ValueError(f"release_run={token!r} is not a run stamp (YYYYMMDDTHHMMSSZ)")
    return frozenset(
        store_id
        for root, store_id in _scheduled_releases(scheduled)
        if paths.force_snapshot_path(store_id, str(token), root=root).is_dir()
    )


def _read_exec_status(fd: int) -> str:
    """Read the supervisor's one-shot exec-status pipe to EOF.

    Empty means the command was executed (the supervisor closes the pipe
    without writing). A decimal errno means `subprocess.Popen` failed inside
    the supervisor; the caller turns it back into the exception type the old
    direct `Popen` would have raised.
    """
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 64)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("ascii", "replace").strip()


def _supervised_command(
    executed_argv: list[str],
    liveness_fd: int,
    exec_status_fd: int,
) -> list[str]:
    """Return the argv that runs `executed_argv` under the isolated-group supervisor.

    The supervisor is started with `start_new_session=True`, so it leads the
    session/group, and it holds the read end of the parent-liveness pipe. If this
    process dies abruptly, the write end closes and the supervisor SIGKILLs the
    detached build group instead of leaving it running. It also holds the write
    end of a one-shot exec-status pipe, so a failed exec is reported back with
    its errno. See `ogstores._pdeath_supervisor`.
    """
    return [
        sys.executable,
        "-c",
        _SUPERVISOR_BOOTSTRAP,
        str(liveness_fd),
        str(exec_status_fd),
        *executed_argv,
    ]


def execute_step(
    step: Step,
    store_id: str | None = None,
    *,
    bundle: Bundle | None = None,
    artifact_root: Path | str | None = None,
    force: bool = False,
    check: bool = True,
    resume: bool = False,
    publish: bool = False,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: float | None = None,
) -> StepResult:
    """Execute one Step in a staged release transaction and record it atomically.

    Parameters:
        step: The Step to execute (holding name, argv, inputs, outputs).
        store_id: Target store release ID (e.g. 'OGS-00042'). Required if bundle is omitted.
        bundle: Optional Bundle to derive store_id and artifact_root if not explicitly supplied.
        artifact_root: Optional artifact directory override.
        force: If True, permit replacing an existing final Store during terminal publication.
        check: If True, raise StepExecutionError on nonzero exit or command failure.
        resume: If True, preserve existing .partial staging directory across executions.
        publish: If True, atomically publish .partial -> final Store upon successful 'validate' step.
        env: Optional environment variables for the subprocess.
        cwd: Optional working directory for the subprocess.
        timeout: Optional execution timeout in seconds.
    """
    if store_id is None:
        if bundle is not None:
            store_id = bundle.store_id
        else:
            raise ValueError("execute_step requires either store_id or bundle")

    paths.require_valid_store_id(store_id)

    # The artifact root is deployment configuration, never a Build Recipe fact
    # (issue #126). Callers normally pass the root resolved by
    # `paths.artifact_root()`; without one, resolve it here.
    resolved_root = (
        Path(artifact_root) if artifact_root is not None else paths.artifact_root()
    )

    # 1. Validate step.name security and write preflight record if invalid
    try:
        validate_step_name(step.name)
    except ValueError as val_err:
        if paths.is_valid_store_id(store_id):
            record_p = paths.record_path(store_id, "unknown", root=resolved_root)
            err_res = StepResult(
                step=str(step.name),
                store_id=store_id,
                exit_code=1,
                success=False,
                start_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                end_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                elapsed_seconds=0.0,
                argv=list(step.argv),
                planned_argv=list(step.argv),
                inputs=[str(p) for p in step.inputs],
                outputs=[str(p) for p in step.outputs],
                opengwasdb_rev=UNAVAILABLE,
                opengwasdb_version=UNAVAILABLE,
                opengwasdb_executable=UNAVAILABLE,
                stdout="",
                stderr=str(val_err),
                record_path=str(record_p),
                published_store=None,
            )
            _write_record_atomically(err_res.to_dict(), record_p)
        raise val_err

    # 2. Publication gating: reject publish=True on non-validate steps
    if publish and step.name != "validate":
        err_msg = (
            f"Premature publication rejected: publish=True is permitted only on terminal "
            f"'validate' step, not on step {step.name!r}."
        )
        record_p = paths.record_path(store_id, step.name, root=resolved_root)
        err_res = StepResult(
            step=step.name,
            store_id=store_id,
            exit_code=1,
            success=False,
            start_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            end_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            elapsed_seconds=0.0,
            argv=list(step.argv),
            planned_argv=list(step.argv),
            inputs=[str(p) for p in step.inputs],
            outputs=[str(p) for p in step.outputs],
            opengwasdb_rev=UNAVAILABLE,
            opengwasdb_version=UNAVAILABLE,
            opengwasdb_executable=UNAVAILABLE,
            stdout="",
            stderr=err_msg,
            record_path=str(record_p),
            published_store=None,
        )
        _write_record_atomically(err_res.to_dict(), record_p)
        raise ValueError(err_msg)

    target_store_p = paths.store_path(store_id, root=resolved_root)
    partial_store_p = paths.partial_store_path(store_id, root=resolved_root)
    backup_store_p = paths.store_dir(store_id, root=resolved_root) / "store.opengwasdb.backup"
    record_p = paths.record_path(store_id, step.name, root=resolved_root)
    is_producing = is_store_producing_step(step)
    is_variant_reference = step.name == VARIANT_REFERENCE_STEP
    ref_target_p = step.outputs[0] if (is_variant_reference and step.outputs) else None

    # 3. Preflight paths and recover any stale backup from prior crash/interruption
    _preflight_paths(store_id, resolved_root)
    if step.published_store_arg is not None:
        # The step reads the published Store its release's build phase has just
        # published; that publication is finished before anything is staged
        # beside it (ADR 0037). `register` imports this module, hence the import here.
        from ogstores import register

        register.finish_build_phase_publication(store_id, resolved_root)
    _recover_pending_backup(store_id, resolved_root)

    # 4. Clean up stale partial store before starting a fresh build (unless explicit resume)
    if is_producing and _lstat_exists(partial_store_p) and not resume:
        _assert_real_directory(partial_store_p, "partial store")
        if partial_store_p.is_dir():
            shutil.rmtree(partial_store_p)
        elif _lstat_exists(partial_store_p):
            partial_store_p.unlink()
        _fsync_dir(partial_store_p.parent)

    # 5. Prepare executed argv (with staging rewrite) and provenance
    try:
        if is_variant_reference:
            if ref_target_p is None:
                raise ValueError(
                    f"Step {step.name!r} requires a declared output path to extract to"
                )
            executed_argv = rewrite_argv_for_variant_reference(step, ref_target_p)
        else:
            executed_argv = rewrite_argv_for_staging(step, store_id, artifact_root=resolved_root)
            executed_argv = resume_argv_for_completion(step, executed_argv, store_id, resolved_root)
    except ValueError as rewrite_err:
        # Preflight failure record: write failed record before raising
        err_res = StepResult(
            step=step.name,
            store_id=store_id,
            exit_code=1,
            success=False,
            start_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            end_time=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            elapsed_seconds=0.0,
            argv=list(step.argv),
            planned_argv=list(step.argv),
            inputs=[str(p) for p in step.inputs],
            outputs=[str(p) for p in step.outputs],
            opengwasdb_rev=UNAVAILABLE,
            opengwasdb_version=UNAVAILABLE,
            opengwasdb_executable=UNAVAILABLE,
            stdout="",
            stderr=str(rewrite_err),
            record_path=str(record_p),
            published_store=None,
        )
        _write_record_atomically(err_res.to_dict(), record_p)
        raise rewrite_err

    planned_argv = list(step.argv)
    ogdb_exe = (
        get_opengwasdb_executable(executed_argv[0], env=env)
        if executed_argv
        else UNAVAILABLE
    )
    ogdb_rev = get_opengwasdb_revision(executable=ogdb_exe, env=env)
    ogdb_ver = get_opengwasdb_version()

    # Variant-reference skip-if-provided: an artifact already on disk (a shared
    # or pre-computed reference) is used as-is, so no subprocess is launched and
    # the record states the skip explicitly. Existence is a runtime fact, which
    # is why the planner never checks it (#145/#147).
    if is_variant_reference and ref_target_p is not None and _lstat_exists(ref_target_p):
        skipped_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        skip_result = StepResult(
            step=step.name,
            store_id=store_id,
            exit_code=0,
            success=True,
            start_time=skipped_at,
            end_time=skipped_at,
            elapsed_seconds=0.0,
            argv=planned_argv,
            planned_argv=planned_argv,
            inputs=[str(p) for p in step.inputs],
            outputs=[str(p) for p in step.outputs],
            opengwasdb_rev=ogdb_rev,
            opengwasdb_version=ogdb_ver,
            opengwasdb_executable=ogdb_exe,
            stdout="",
            stderr="",
            record_path=str(record_p),
            published_store=None,
            skipped=True,
            skip_reason="provided",
        )
        _write_record_atomically(skip_result.to_dict(), record_p)
        return skip_result

    # A fresh `complete-dense` records the plan it starts from before it can
    # write a checkpoint, so an interrupted one is resumed only from that plan.
    starts_completion = step.name == "complete" and executed_argv[1:2] == [COMPLETE_DENSE]
    if starts_completion:
        _write_record_atomically(
            {"planned_argv": list(step.argv), "checkpoint": str(paths.completion_checkpoint_path(store_id, root=resolved_root))},
            paths.completion_start_path(store_id, root=resolved_root),
        )

    # A test-only pause or crash point: the step's record is gone and its
    # command has not started, so a run stopped here is mid-step (#195).
    fault_boundary(f"{step.name}-started")

    # 6. Execute subprocess in its own process group and capture timing/outputs
    start_dt = datetime.datetime.now(datetime.timezone.utc)
    start_time_iso = start_dt.isoformat().replace("+00:00", "Z")
    t0 = time.monotonic()

    stdout_captured = ""
    stderr_captured = ""
    exit_code = 0
    interrupted = False
    proc_error: BaseException | None = None
    exec_status_text = ""
    proc: subprocess.Popen[str] | None = None
    pgid: int | None = None
    liveness_read: int | None = None
    liveness_write: int | None = None
    exec_status_read: int | None = None
    exec_status_write: int | None = None

    try:
        # The build runs under a supervisor that leads the isolated session and
        # holds the read end of a parent-liveness pipe. The write end stays
        # private to this process (non-inheritable), so an abrupt death of this
        # process -- SIGKILL included -- closes it and the supervisor tears the
        # detached group down before a retry can collide with it. A second pipe
        # reports a failed exec (missing / non-executable command) back with its
        # errno, so the exception type and record stay unchanged.
        liveness_read, liveness_write = os.pipe()
        exec_status_read, exec_status_write = os.pipe()
        spawn_argv = _supervised_command(executed_argv, liveness_read, exec_status_write)

        proc = subprocess.Popen(
            spawn_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=True,  # Supervisor (and its build) lead a new process group
            pass_fds=(liveness_read, exec_status_write),
        )
        os.close(liveness_read)
        liveness_read = None
        os.close(exec_status_write)
        exec_status_write = None
        pgid = os.getpgid(proc.pid)
        stdout_captured, stderr_captured = proc.communicate(timeout=timeout)
        exit_code = proc.returncode

        # Process-group cleanup on nonzero returncode: terminate/kill any surviving child workers
        if exit_code != 0 and pgid is not None:
            out_extra, err_extra = _terminate_process_group(pgid, proc, timeout=1.0)
            stdout_captured = (stdout_captured or "") + out_extra
            stderr_captured = (stderr_captured or "") + err_extra
    except FileNotFoundError as fnf:
        exit_code = 127
        stderr_captured = f"Command not found: {executed_argv[0]} ({fnf})"
        proc_error = fnf
    except subprocess.TimeoutExpired as toe:
        if pgid is not None:
            out_extra, err_extra = _terminate_process_group(pgid, proc, timeout=1.0)
            stdout_captured = (stdout_captured or "") + out_extra
            stderr_captured = (stderr_captured or "") + err_extra + f"\nCommand timed out after {timeout}s"
        exit_code = -signal.SIGKILL
        proc_error = toe
    except KeyboardInterrupt as ki:
        interrupted = True
        if pgid is not None:
            out_extra, err_extra = _terminate_process_group(pgid, proc, timeout=2.0)
            stdout_captured = (stdout_captured or "") + out_extra
            stderr_captured = (stderr_captured or "") + err_extra + "\nExecution interrupted by SIGINT / KeyboardInterrupt"
        exit_code = 130
        proc_error = ki
    except BaseException as exc:
        if pgid is not None:
            out_extra, err_extra = _terminate_process_group(pgid, proc, timeout=1.0)
            stdout_captured = (stdout_captured or "") + out_extra
            stderr_captured = (stderr_captured or "") + err_extra
        exit_code = 1
        stderr_captured = (stderr_captured or "") + f"\n{exc}"
        proc_error = exc
    finally:
        # Drop our own copy of the exec-status write end first: if Popen itself
        # raised, this process still holds it and reading the status pipe would
        # otherwise block forever waiting for an EOF the supervisor never sends.
        if exec_status_write is not None:
            try:
                os.close(exec_status_write)
            except OSError:
                pass
            exec_status_write = None
        # A written errno means the supervisor could not exec the real command.
        # Empty (EOF) means it did, or was itself terminated first.
        if exec_status_read is not None:
            exec_status_text = _read_exec_status(exec_status_read)
        # Closing the liveness write end lets the supervisor observe this
        # process's exit; by here the build has already been reaped or terminated.
        for fd in (exec_status_read, liveness_read, liveness_write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        elapsed_seconds = round(time.monotonic() - t0, 3)
        end_dt = datetime.datetime.now(datetime.timezone.utc)
        end_time_iso = end_dt.isoformat().replace("+00:00", "Z")

    # A failed exec inside the supervisor is reported by errno. Rebuild the
    # exception the old direct Popen raised, so its class, exit code, stderr,
    # and the resulting MissingCommandError / StepExecutionError are unchanged.
    if exec_status_text and executed_argv:
        try:
            exec_err_no = int(exec_status_text)
        except ValueError:
            exec_err_no = 0
        if exec_err_no:
            exec_error = OSError(exec_err_no, os.strerror(exec_err_no), executed_argv[0])
            proc_error = exec_error
            if isinstance(exec_error, FileNotFoundError):
                exit_code = 127
                stderr_captured = f"Command not found: {executed_argv[0]} ({exec_error})"
            else:
                exit_code = 1
                stderr_captured = (stderr_captured or "") + f"\n{exec_error}"

    success = (exit_code == 0 and not interrupted and proc_error is None)
    published_store_str: str | None = None
    publication_error: BaseException | None = None

    # 7a. Variant-reference: atomically publish the staged artifact, or remove
    # the staged file on failure so no incomplete reference is left behind.
    if is_variant_reference and ref_target_p is not None:
        ref_partial_p = variant_reference_partial_path(ref_target_p)
        if success:
            if not _lstat_exists(ref_partial_p):
                success = False
                exit_code = 1
                err_msg = (
                    f"Step {step.name!r} exited with code 0 but produced no staged "
                    f"artifact at {ref_partial_p}"
                )
                stderr_captured = (stderr_captured or "") + f"\n{err_msg}"
                proc_error = FileNotFoundError(err_msg)
            else:
                try:
                    os.replace(ref_partial_p, ref_target_p)
                    _fsync_dir(ref_target_p.parent)
                except BaseException as exc:
                    success = False
                    exit_code = 1
                    stderr_captured = (
                        stderr_captured or ""
                    ) + f"\nVariant reference publish error: {exc}"
                    proc_error = exc
                    _remove_path_if_exists(ref_partial_p)
        else:
            _remove_path_if_exists(ref_partial_p)

    # 7. Verify partial output on exit code 0 for store-producing steps
    if success and is_producing:
        _assert_real_directory(partial_store_p, "partial store")
        if not _lstat_exists(partial_store_p):
            success = False
            exit_code = 1
            err_msg = (
                f"Step {step.name!r} exited with code 0 but failed to produce "
                f"canonical partial store artifact at {partial_store_p}"
            )
            stderr_captured = (stderr_captured or "") + f"\n{err_msg}"
            proc_error = FileNotFoundError(err_msg)

    # 8. Terminal publication (permitted only on validate step when publish=True)
    if success and publish:
        try:
            _safe_publish_partial_store(
                partial_store_p,
                target_store_p,
                backup_store_p,
                force=force,
            )
            published_store_str = str(target_store_p)
        except BaseException as exc:
            success = False
            exit_code = 1 if exit_code == 0 else exit_code
            stderr_captured = (stderr_captured or "") + f"\nPublication error: {exc}"
            publication_error = exc

    # A finished completion, fresh or resumed, has no checkpoint left to resume.
    if success and step.name == "complete" and executed_argv[1:2] in ([COMPLETE_DENSE], ["complete-dense-resume"]):
        _remove_path_if_exists(paths.completion_start_path(store_id, root=resolved_root))

    # 9. Construct StepResult and atomically write record
    result = StepResult(
        step=step.name,
        store_id=store_id,
        exit_code=exit_code,
        success=success,
        start_time=start_time_iso,
        end_time=end_time_iso,
        elapsed_seconds=elapsed_seconds,
        argv=executed_argv,
        planned_argv=planned_argv,
        inputs=[str(p) for p in step.inputs],
        outputs=[str(p) for p in step.outputs],
        opengwasdb_rev=ogdb_rev,
        opengwasdb_version=ogdb_ver,
        opengwasdb_executable=ogdb_exe,
        stdout=stdout_captured or "",
        stderr=stderr_captured or "",
        record_path=str(record_p),
        published_store=published_store_str,
    )

    _write_record_atomically(result.to_dict(), record_p)

    # 10. Re-raise interruption, missing command, publication error, or StepExecutionError
    if interrupted:
        raise KeyboardInterrupt(f"Step {step.name!r} execution was interrupted")

    if isinstance(proc_error, FileNotFoundError):
        if check:
            raise MissingCommandError(
                f"Step {step.name!r} for {store_id} failed: {proc_error}",
                result,
            )

    if publication_error is not None and check:
        raise StepExecutionError(
            f"Step {step.name!r} for {store_id} publication failed:\n{publication_error}",
            result,
        )

    if not success and check:
        if step.name == OBSERVED_VIEW_CHECK_STEP:
            message = observed_view_check_failure(
                step, store_id, executed_argv, exit_code, stdout_captured or "", stderr_captured or ""
            )
        else:
            message = f"Step {step.name!r} for {store_id} failed with exit code {exit_code}:\n{stderr_captured}"
        raise StepExecutionError(message, result)

    return result


def _observed_view_failure_counts(verdict: Mapping[str, Any]) -> str:
    """Each name in a verdict's `failures`, with its count where the verdict holds one.

    A counter is in `totals`. The three axis and Analyses failures carry
    theirs elsewhere: `axis.parent_rows_unmapped`, `axis.order_preserved` and
    `analyses.identical`.
    """
    totals, axis, analyses = verdict["totals"], verdict["axis"], verdict["analyses"]
    named: list[str] = []
    for name in verdict["failures"]:
        if name in totals:
            named.append(f"{name}={totals[name]}")
        elif name == "parent_rows_unmapped":
            named.append(f"parent_rows_unmapped={axis['parent_rows_unmapped']}")
        elif name == "order_not_preserved":
            named.append(f"order_preserved={str(axis['order_preserved']).lower()}")
        elif name == "analyses_mismatch":
            named.append(
                f"analyses_identical={str(analyses['identical']).lower()} "
                f"(n_parent={analyses['n_parent']}, n_child={analyses['n_child']})"
            )
        else:
            named.append(name)
    return ", ".join(named)


def observed_view_check_failure(
    step: Step, store_id: str, argv: list[str], exit_code: int, stdout: str, stderr: str
) -> str:
    """What the operator reads when the Observed-View Check does not pass (ADR 0037).

    Every message names where the verdict is kept. Exit 1 is a mismatch: the
    command printed its verdict, so the message names the failing counters
    with their counts. Exit 2 is a refusal, which prints no verdict: it is
    reported as one, with the command's stderr. A verdict that cannot be read
    from stdout is said to be unreadable, with the reason and the start of
    stdout, never replaced by a generic failure.
    """
    where = f"Step {step.name!r} for {store_id}"
    output = argv[argv.index("--output") + 1] if "--output" in argv else "(no --output)"
    if exit_code == 2:
        return f"{where} was refused by {step.argv[1]} (exit 2); verdict: {output}\n{stderr}"
    try:
        # The first object: a step that exits non-zero has its output captured twice.
        verdict, _ = json.JSONDecoder().raw_decode(stdout.lstrip())
        failing = _observed_view_failure_counts(verdict)
    except (ValueError, KeyError, TypeError) as exc:
        return (
            f"{where} failed with exit code {exit_code}; verdict unreadable: {exc!r}; "
            f"verdict: {output}; stdout begins {stdout[:200]!r}\n{stderr}"
        )
    return (
        f"{where} found the completed Store differs from the published one in what was observed "
        f"(exit {exit_code}); failing counters: {failing or 'none named'}; verdict: {output}\n"
        f"Nothing was published and the published Store is as it was.\n{stderr}"
    )


def run_step(
    step: Step,
    store_id: str | None = None,
    *,
    bundle: Bundle | None = None,
    artifact_root: Path | str | None = None,
    force: bool = False,
    check: bool = True,
    resume: bool = False,
    publish: bool = False,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: float | None = None,
) -> StepResult:
    """Alias for execute_step."""
    return execute_step(
        step=step,
        store_id=store_id,
        bundle=bundle,
        artifact_root=artifact_root,
        force=force,
        check=check,
        resume=resume,
        publish=publish,
        env=env,
        cwd=cwd,
        timeout=timeout,
    )


def run_plan(
    steps: list[Step],
    store_id: str | None = None,
    *,
    bundle: Bundle | None = None,
    artifact_root: Path | str | None = None,
    force: bool = False,
    check: bool = True,
    resume: bool = False,
    publish: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: float | None = None,
) -> list[StepResult]:
    """Execute a list of planned Steps sequentially in a staged release transaction.

    Stops immediately upon any step failure even in check=False result-returning mode.
    Publishes the staged .partial store to final store.opengwasdb on terminal 'validate'
    step success if publish=True. Rejects plans lacking a terminal 'validate' step if publish=True.
    """
    if publish:
        has_validate = any(step.name == "validate" for step in steps)
        if not has_validate:
            raise ValueError(
                "Publication rejected: plan lacks a terminal 'validate' step. "
                "Set publish=False or ensure 'validate' step is included."
            )

    results: list[StepResult] = []

    for step in steps:
        is_validate = (step.name == "validate")
        step_publish = publish if is_validate else False

        res = execute_step(
            step=step,
            store_id=store_id,
            bundle=bundle,
            artifact_root=artifact_root,
            force=force,
            check=check,
            resume=resume,
            publish=step_publish,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )
        results.append(res)
        if not res.success:
            # Stop immediately on failure: do not execute subsequent steps or publish
            break

    return results


__all__ = [
    "ForceSnapshotPendingError",
    "KILL_AT_ENV",
    "ENTRY_POINT_PID_ENV",
    "KILL_GROUP_AT_ENV",
    "PAUSE_AT_ENV",
    "PAUSE_FILE_ENV",
    "MissingCommandError",
    "PublicationPendingError",
    "STORE_PRODUCING_STEPS",
    "StepExecutionError",
    "StepResult",
    "StoreExistsError",
    "UNAVAILABLE",
    "VALID_STEP_NAMES",
    "VARIANT_REFERENCE_STEP",
    "archive_force_snapshot",
    "argv_change",
    "completing_own_stores",
    "execute_step",
    "fault_boundary",
    "force_requested",
    "forced_by_transaction",
    "forced_releases",
    "get_opengwasdb_executable",
    "get_opengwasdb_revision",
    "get_opengwasdb_version",
    "is_exact_commit_hash",
    "is_store_producing_step",
    "job_record",
    "load_record",
    "pending_force_snapshots",
    "prepare_release_run",
    "publish_store",
    "refuse_pending_force_snapshots",
    "refuse_rebuilding_published_releases",
    "restore_force_snapshot",
    "resume_argv_for_completion",
    "rewrite_argv_for_staging",
    "rewrite_argv_for_variant_reference",
    "run_plan",
    "run_step",
    "settle_force_snapshots",
    "utc_stamp",
    "validate_step_name",
    "variant_reference_partial_path",
]
