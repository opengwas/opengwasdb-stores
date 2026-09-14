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
- **Process group isolation & cleanup**: Commands launch in an isolated process group (`start_new_session=True`). On timeout, SIGINT, or error, the entire process group PGID is terminated and reaped to prevent orphaned workers from writing in the background (stubborn descendants ignoring SIGTERM are terminated with SIGKILL).
- **Existing final Store refusal**: Store-producing steps refuse to run when a final `store.opengwasdb` already exists unless explicit `force=True` is provided (`StoreExistsError`).
- **Safe force replacement and rollback**: Force replacement backs up the existing store (`store.opengwasdb.backup`) before swapping, fsyncs directory metadata on POSIX, rolls back on publication failure, and treats backup cleanup failure after a successful swap as recoverable success.
- **Crash recovery on start**: Any pending backup directory left from an interrupted prior run is recovered automatically before executing the next step.
- **Missing command handling**: Missing executables are caught cleanly (exit code 127), recorded to `records/<step>.json`, and raised as `MissingCommandError` when `check=True`.
- **Interruption handling**: SIGINT / `KeyboardInterrupt` terminates child process groups cleanly, preserves existing store and `validation.yaml`, writes record (exit code 130), and re-raises `KeyboardInterrupt`.
- **Timeout handling**: Subprocess timeouts kill the process group and record timeout failures.
- **Atomic record writing**: `records/<step>.json` is written atomically using a temporary file and `os.replace` with `fsync`; record retrieval via `load_record()`.
- **Planned vs executed argv**: `planned_argv` retains the final destination while `argv` contains the canonical `.partial` destination, documenting the staging normalization required by #117.
- **Revision & executable provenance**: Exact 40/64-character `opengwasdb` commit SHA is captured from package distribution metadata (`direct_url.json`); branch/version strings are rejected and reported as `unavailable`. Executable path is resolved from PATH.
- **No Store readback (tripwire test)**: `execute_step` never opens, inspects, or interprets files within `store.opengwasdb` (ADR 0023).
- **Sequential plan execution & failure halting**: `run_plan()` executes steps sequentially, publishes only on terminal step success, and halts immediately after any failed step (even in `check=False` mode).
- **Explicit resumption**: `resume=True` preserves existing `.partial` checkpoint directories across invocations; `resume=False` cleans up stale `.partial` state.
- **Input validation**: Malformed `store_id` format and symlink artifact states are rejected up front via lstat/no-follow.

Run from repository root:
    pixi run python tests/run/test_run.py
