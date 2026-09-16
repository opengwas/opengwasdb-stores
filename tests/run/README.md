# Step execution runner checks

Validates `ogstores.run`: executing an `opengwasdb` `Step`, capturing stdout,
stderr, execution timing, exit status, exact `opengwasdb` revision and executable
provenance, atomically recording `records/<step>.json`, and enforcing staged release
transaction safety rules governing Store Releases (ADR 0022, ADR 0023).

Covered by `tests/run/test_run.py`:
- **Staged release transaction lifecycle**: A release executes entirely against `store.opengwasdb.partial` across all steps: `build` / `complete` creates `store.opengwasdb.partial`, and every mutating post-step (`top-hits`, `rho`, `overview`) as well as `validate` operates directly on that staged `.partial` path. Only upon terminal validation/finalization is `store.opengwasdb.partial` published (atomically renamed) to the final `store.opengwasdb` path.
- **Strict destination token validation**: Replaces exact target store path occurrences and embedded substrings (`--flag=<path>`) with canonical `.partial` path. Rejects missing, ambiguous, or non-canonical equivalent paths before execution. Direct-to-final output fallback without staging is prohibited.
- **Parent Store preservation**: Reference-completion `complete` steps rewrite only the child's store destination to `.partial` while leaving parent store inputs untouched.
- **Failure isolation & zero contamination**: Any failed or interrupted step leaves pre-existing final `store.opengwasdb` and `validation.yaml` completely untouched without needing whole-Store copying; a failed new release leaves no final Store.
- **Process group isolation & cleanup**: Commands launch in an isolated process group (`start_new_session=True`) led by a small supervisor (`ogstores._pdeath_supervisor`). On timeout, SIGINT, or error, the orchestrator sends SIGTERM to the whole group, waits out a grace window, then SIGKILLs it so orphaned workers cannot keep writing. The supervisor absorbs the group's SIGTERM rather than escalating, so a command's own SIGTERM handler runs to completion during that window.
- **Abrupt parent death**: The supervisor holds the read end of a parent-liveness pipe whose write end only the orchestrator holds and which is non-inheritable. If the orchestrator is killed abruptly (SIGKILL included), the pipe closes, the supervisor SIGKILLs its own isolated process group, and no detached build survives to collide with a retry. The supervisor only ever signals a group it leads itself.
- **Existing final Store refusal**: Store-producing steps refuse to run when a final `store.opengwasdb` already exists unless explicit `force=True` is provided (`StoreExistsError`).
- **Safe force replacement and rollback**: Force replacement backs up the existing store (`store.opengwasdb.backup`) before swapping, fsyncs directory metadata on POSIX, rolls back on publication failure, and treats backup cleanup failure after a successful swap as recoverable success.
- **Crash recovery on start**: Any pending backup directory left from an interrupted prior run is recovered automatically before executing the next step.
- **Command exec classification**: A failed exec inside the supervisor is reported back by errno, so a missing command keeps exit 127 / `MissingCommandError` while a present-but-non-executable command (EACCES) or a directory stays exit 1 / `StepExecutionError`.
- **Interruption handling**: SIGINT / `KeyboardInterrupt` SIGTERMs the child process group, waits out the grace window, preserves the existing store and `validation.yaml`, writes the record (exit code 130), and re-raises `KeyboardInterrupt`.
- **Timeout handling**: Subprocess timeouts SIGTERM the process group, wait out the grace window, then SIGKILL it, and record timeout failures.
- **Signal status preservation**: A command killed by a signal (including SIGKILL, `-9`) is still recorded with the negative exit code the orchestrator always used.
- **Atomic record writing**: `records/<step>.json` is written atomically using a temporary file and `os.replace` with `fsync`; record retrieval via `load_record()`.
- **Planned vs executed argv**: `planned_argv` retains the final destination while `argv` contains the canonical `.partial` destination, documenting the staging normalization required by #117.
- **Revision & executable provenance**: Exact 40/64-character `opengwasdb` commit SHA is captured from package distribution metadata (`direct_url.json`); branch/version strings are rejected and reported as `unavailable`. Executable path is resolved from PATH.
- **No Store readback (tripwire test)**: `execute_step` never opens, inspects, or interprets files within `store.opengwasdb` (ADR 0023).
- **Sequential plan execution & failure halting**: `run_plan()` executes steps sequentially, publishes only on terminal step success, and halts immediately after any failed step (even in `check=False` mode).
- **Explicit resumption**: `resume=True` preserves existing `.partial` checkpoint directories across invocations; `resume=False` cleans up stale `.partial` state.
- **Input validation**: Malformed `store_id` format and symlink artifact states are rejected up front via lstat/no-follow.

Run from repository root:
    pixi run python tests/run/test_run.py
