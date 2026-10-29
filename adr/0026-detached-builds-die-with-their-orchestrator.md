# Detached builds die with their orchestrator

`ogstores.run` launches a release command with `start_new_session=True` so the command and every worker it forks share one signalable process group. That is what makes timeout, `SIGINT`, and nonzero-exit cleanup possible, but it also detaches the group from the orchestrator: if the process running `execute_step` is killed abruptly -- Snakemake reaping a rule, a CI runner cancelling a job -- no handler runs, the group keeps writing to `store.opengwasdb.partial`, and the next attempt collides with a build that is still in flight. Staging makes such a collision safe for the *published* Store but not for the retry.

The orchestrator therefore runs each command under a small supervisor that leads the isolated session (`src/ogstores/_pdeath_supervisor.py`) and holds the read end of a liveness pipe whose write end only the orchestrator holds and which is non-inheritable. When the orchestrator dies for any reason, including `SIGKILL`, the kernel closes the write end, the supervisor `SIGKILL`s the process group it leads, and nothing detached survives.

The supervisor does not own signal escalation. On timeout, `SIGINT`, or nonzero exit, `_terminate_process_group` keeps its existing two-phase contract -- `SIGTERM` the group, wait out a grace window, then `SIGKILL` -- and the supervisor's handlers for `SIGTERM`/`SIGINT`/`SIGHUP` merely absorb the signal and keep waiting, so a command's own `SIGTERM` handler runs to completion inside that window instead of being preempted by an immediate `SIGKILL`. Staying alive also keeps the supervisor's stdio pipes open, so the orchestrator's `communicate()` cannot return before the whole build is gone. Only the abrupt-parent-death path `SIGKILL`s, because no orchestrator remains to run the grace window.

Public behaviour and records are otherwise unchanged. The supervisor mirrors the command's exit status, including death by signal (a `SIGKILL`ed command still records `-9`), and reports a failed exec back through a one-shot exec-status pipe carrying the errno: `ENOENT` still becomes exit 127 / `MissingCommandError`, while `EACCES` or a directory still becomes exit 1 / `StepExecutionError`, exactly as the old direct `Popen` classified them.

The supervisor signals only a group it leads itself (`os.getpgid(0) == os.getpid()`). It never signals by pattern or by the parent's group, so it cannot take out the orchestrator or a concurrent release in another worktree.

Three alternatives were considered and rejected:

* **Bare Linux `PR_SET_PDEATHSIG`.** It reaches only the direct child, so `opengwasdb`'s forked `ProcessPoolExecutor` workers survive their leader's death, which is exactly the collision being fixed. It also fires when the *thread* that called `fork(2)` exits, not only when the process dies, so a threaded orchestrator could lose a healthy build. The liveness pipe is process-scoped and needs no Linux-only interface.
* **Inherited locking.** An exclusive lock held by the build group makes a retry fail fast instead of colliding, but it neither stops the orphan nor frees the artifact, so it does not satisfy "cannot leave the group alive". It also adds a new failure mode to normal retries.
* **Pattern kills (`pkill -f opengwasdb`).** Unsafe by construction: concurrent releases in other worktrees share the pattern.

Escape via a further `setsid()` still defeats group containment; that remains out of scope and requires external cgroup or container isolation, as the runner already documents.
