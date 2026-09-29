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
  `store.opengwasdb.partial` during execution while preserving parent inputs.
* Rejection of invalid paths: Non-canonical equivalent tokens, embedded substrings,
  symlinks (both live and dangling), or non-directories are rejected before execution via lstat/no-follow.
* Publication gating: Publication is permitted ONLY on the terminal 'validate' step
  (or via explicit `publish_store()` which asserts a successful 'validate' record
  exists). Premature publication or publication on plans lacking 'validate' is rejected.
* Force timing: Staging in `store.opengwasdb.partial` is permitted beside an existing
  final Store without force; `force=True` is required only at terminal publication.
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
from typing import Any, Mapping

from ogstores import paths
from ogstores.bundle import Bundle
from ogstores.plan import Step

STORE_PRODUCING_STEPS: frozenset[str] = frozenset({"build", "complete"})
VARIANT_REFERENCE_STEP: str = "variant-reference"
VALID_STEP_NAMES: frozenset[str] = frozenset({
    "build",
    "complete",
    "variant-reference",
    "top-hits",
    "rho",
    "overview",
    "validate",
    "register",
    "unknown",
})
UNAVAILABLE: str = "unavailable"
_HEX_40_OR_64: re.Pattern[str] = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

# Bootstrap for the isolated-group supervisor (ogstores._pdeath_supervisor).
# argv layout after the code string: <src-dir> <liveness-fd> <exec-status-fd> <command...>.
_SUPERVISOR_BOOTSTRAP: str = (
    "import sys; "
    "src, liveness_fd, exec_status_fd, *cmd = sys.argv[1:]; "
    "sys.path.insert(0, src); "
    "from ogstores._pdeath_supervisor import main; "
    "raise SystemExit(main(cmd, int(liveness_fd), int(exec_status_fd)))"
)


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
    """Validate step.name against allowlist and reject path traversal characters."""
    if (
        not isinstance(name, str)
        or name not in VALID_STEP_NAMES
        or "/" in name
        or "\\" in name
        or ".." in name
    ):
        raise ValueError(
            f"Invalid step name {name!r}; must be one of {sorted(VALID_STEP_NAMES)} "
            "and contain no path separators or traversal sequences."
        )
    return name


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


def get_opengwasdb_revision(
    executable: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the exact resolved 40/64-char opengwasdb git commit hash bound to executable/env."""
    # 1. If an explicit executable is provided, search only its environment prefix
    if executable and executable != UNAVAILABLE:
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

        for c_root in candidate_roots:
            pattern = str(c_root / "lib" / "python*" / "site-packages" / "opengwasdb*.dist-info" / "direct_url.json")
            for direct_url_match in glob.glob(pattern):
                commit = _extract_commit_from_direct_url_file(Path(direct_url_match))
                if commit:
                    return commit
            pattern_win = str(c_root / "Lib" / "site-packages" / "opengwasdb*.dist-info" / "direct_url.json")
            for direct_url_match in glob.glob(pattern_win):
                commit = _extract_commit_from_direct_url_file(Path(direct_url_match))
                if commit:
                    return commit
            pattern_direct = str(c_root / "opengwasdb*.dist-info" / "direct_url.json")
            for direct_url_match in glob.glob(pattern_direct):
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


def get_opengwasdb_version() -> str:
    """Return the installed opengwasdb version or 'unavailable'."""
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
    """
    validate_step_name(step.name)
    target_store = paths.store_path(store_id, root=artifact_root)
    partial_store = paths.partial_store_path(store_id, root=artifact_root)

    target_str = str(target_store)
    partial_str = str(partial_store)
    target_resolved = target_store.resolve()

    rewritten: list[str] = []
    replacement_count = 0

    for token in step.argv:
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
    """Recover or clean up any stale backup directory left by an interrupted previous run."""
    target_p = paths.store_path(store_id, root=root)
    backup_p = paths.store_dir(store_id, root=root) / "store.opengwasdb.backup"

    _assert_real_directory(backup_p, "backup store")
    _assert_real_directory(target_p, "target store")
    if not _lstat_exists(backup_p):
        return

    if not _lstat_exists(target_p):
        # Previous run crashed after moving target -> backup but before publishing partial.
        # Recover original target store.
        backup_p.rename(target_p)
        _fsync_dir(target_p.parent)
    else:
        # Previous run finished publishing partial -> target, but crashed before removing backup.
        try:
            if backup_p.is_dir():
                shutil.rmtree(backup_p)
            elif _lstat_exists(backup_p):
                backup_p.unlink()
            _fsync_dir(target_p.parent)
        except Exception:
            pass


def _safe_publish_partial_store(
    partial_p: Path,
    target_p: Path,
    backup_p: Path,
    force: bool = False,
) -> None:
    """Safely publish partial_p to target_p with rollback protection, backup, and fsync."""
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

        # Backup cleanup: if rmtree fails here, target_p is already valid and published.
        # Treat as recoverable success; retained backup will be cleaned up on next start.
        try:
            if backup_p.is_dir():
                shutil.rmtree(backup_p)
            elif _lstat_exists(backup_p):
                backup_p.unlink()
            _fsync_dir(target_p.parent)
        except Exception:
            pass
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
) -> Path:
    """Atomically publish a validated staging store (store.opengwasdb.partial) to final store.opengwasdb.

    Requires that a successful 'validate' execution record exists for this release.
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
    _safe_publish_partial_store(partial_p, target_p, backup_p, force=force)
    return target_p


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
    src_dir = str(Path(__file__).resolve().parent.parent)
    return [
        sys.executable,
        "-c",
        _SUPERVISOR_BOOTSTRAP,
        src_dir,
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
        raise StepExecutionError(
            f"Step {step.name!r} for {store_id} failed with exit code {exit_code}:\n{stderr_captured}",
            result,
        )

    return result


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
    "MissingCommandError",
    "STORE_PRODUCING_STEPS",
    "StepExecutionError",
    "StepResult",
    "StoreExistsError",
    "UNAVAILABLE",
    "VALID_STEP_NAMES",
    "VARIANT_REFERENCE_STEP",
    "execute_step",
    "get_opengwasdb_executable",
    "get_opengwasdb_revision",
    "get_opengwasdb_version",
    "is_exact_commit_hash",
    "is_store_producing_step",
    "load_record",
    "publish_store",
    "rewrite_argv_for_staging",
    "rewrite_argv_for_variant_reference",
    "run_plan",
    "run_step",
    "validate_step_name",
    "variant_reference_partial_path",
]
