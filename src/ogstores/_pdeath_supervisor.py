"""Isolated-group supervisor: keep a detached build from outliving its orchestrator.

`ogstores.run.execute_step` launches `opengwasdb` with `start_new_session=True`
so a build and every worker it forks form one signalable process group. That
same call also detaches the build: if the orchestrator is killed abruptly --
Snakemake reaping a rule, a CI runner cancelling a job, a dropped shell -- no
handler in `execute_step` runs, the detached group keeps writing to
`store.opengwasdb.partial`, and the next attempt collides with it.

This module is the smallest process that closes that window on POSIX. The
orchestrator starts it with `start_new_session=True`, so it is the session and
group leader, and gives it the read end of a liveness pipe whose write end the
orchestrator keeps private and non-inheritable (`os.pipe()` fds are
non-inheritable by default, and only the read end is passed on). When the
orchestrator dies for any reason -- including `SIGKILL` -- the kernel closes
that write end, the read returns EOF, and the supervisor `SIGKILL`s its own
isolated process group before exiting. Because the pipe is scoped to the
orchestrator *process*, this also does not depend on the thread that called
`fork(2)` still being alive, which a bare Linux `PR_SET_PDEATHSIG` does.

The supervisor is otherwise a transparent shim. It runs the real command in the
same group, inherits the orchestrator's stdio and environment, mirrors the
command's exit status (death by signal included), and never writes to stdout.
The orchestrator still records the real `opengwasdb` argv, so records and public
behaviour are unchanged.

It deliberately does **not** own signal escalation. The orchestrator's
`_terminate_process_group` already implements the contract -- `SIGTERM` the
group, wait out a grace window, then `SIGKILL` -- for timeout, `SIGINT`, and
nonzero-exit cleanup, and this supervisor must not preempt that window with an
immediate `SIGKILL`. Its signal handlers for `SIGTERM`/`SIGINT`/`SIGHUP`
therefore just absorb the signal and keep waiting for the command, which also
keeps its stdio pipes open so the orchestrator's `communicate()` cannot return
before the whole build is gone. Only the abrupt-parent-death path `SIGKILL`s,
because there is no orchestrator left to run the grace window.

A second, one-shot pipe carries the errno when the command cannot be executed
(`ENOENT`, `EACCES`, ...). Closing it without writing means "exec succeeded";
the orchestrator turns a written errno back into the same exception type and
exit code the old direct `Popen` produced.

Safety: the group is signalled only when this process is the leader of its own
group (`os.getpgid(0) == os.getpid()`), so it can never take out the
orchestrator's group or another concurrent release's group.

Entry point: `main(argv, parent_liveness_fd, exec_status_fd)`.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import threading


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _is_own_group_leader() -> bool:
    """Return True when this process leads its own process group."""
    try:
        return os.getpgid(0) == os.getpid()
    except OSError:
        return False


def _kill_own_group() -> None:
    """SIGKILL the isolated process group this supervisor leads.

    Guarded to the leader of its own group, so it can only ever target the
    isolated build tree this supervisor was created to own.
    """
    if not _is_own_group_leader():
        return
    try:
        os.killpg(os.getpid(), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _report_exec_error(status_fd: int, exc: OSError) -> None:
    err_no = exc.errno if exc.errno is not None else errno.EIO
    try:
        os.write(status_fd, str(err_no).encode("ascii"))
    except OSError:
        pass
    finally:
        _close_quietly(status_fd)


def main(argv: list[str], parent_liveness_fd: int, exec_status_fd: int) -> int:
    """Run `argv` in the supervisor's own group until it exits or the parent dies.

    Returns the command's exit status; when the command dies by a signal, the
    supervisor re-raises that signal on itself so the orchestrator observes the
    usual negative `Popen.returncode`.
    """
    if not argv:
        _close_quietly(exec_status_fd)
        return 2

    parent_dead = threading.Event()
    state: dict[str, subprocess.Popen[bytes] | None] = {"proc": None}

    def teardown() -> None:
        proc = state["proc"]
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        _kill_own_group()

    def watch_parent() -> None:
        try:
            while os.read(parent_liveness_fd, 4096):
                pass
        except OSError:
            pass
        # EOF (or a broken pipe) means the orchestrator is gone; there is no one
        # left to run a grace window, so tear the whole group down now.
        parent_dead.set()
        teardown()
        os._exit(1)

    def hold_signal(signum: int, _frame: object) -> None:
        # The orchestrator owns the SIGTERM -> grace -> SIGKILL sequence; absorb
        # the signal and keep waiting, so its grace window stays authoritative.
        pass

    watcher = threading.Thread(target=watch_parent, daemon=True)
    watcher.start()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, hold_signal)
        except (ValueError, OSError):
            pass

    try:
        proc = subprocess.Popen(argv)
    except OSError as exc:
        _report_exec_error(exec_status_fd, exc)
        return 127
    _close_quietly(exec_status_fd)
    state["proc"] = proc

    # Close the startup race: the watcher may have torn the group down before
    # this process was recorded, in which case the freshly forked command would
    # otherwise survive the already-delivered group signal.
    if parent_dead.is_set():
        teardown()
        os._exit(1)

    returncode = proc.wait()

    if returncode < 0:
        # Mirror death by signal so the orchestrator records the true wait status.
        sig = -returncode
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            # SIGKILL (and SIGSTOP) cannot have a handler; the default action
            # already applies, so killing ourselves below still mirrors -9.
            pass
        os.kill(os.getpid(), sig)
    return returncode
