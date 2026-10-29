"""Run a command in its own process group, and kill the whole group on timeout (#390).

A worker that spawns children (the fixture build does) must not leave them
running after the run's budget is spent, so each command starts in its own
session; on timeout the group gets SIGTERM, a short grace period, then SIGKILL,
and is reaped before the caller goes on.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

GRACE_S = 1.0


def _killpg(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def run_group(argv: list[str], *, timeout: float | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(argv, capture_output=True, text=True)` in a new session; `TimeoutExpired` after the group is dead."""
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, **kwargs  # type: ignore[arg-type]
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        _killpg(process.pid, signal.SIGKILL)  # whatever ignored SIGTERM, children included
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
        raise
    except BaseException:
        _killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)
