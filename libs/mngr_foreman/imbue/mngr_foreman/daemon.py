"""Detach ``mngr foreman`` into a background daemon process.

``mngr foreman -d`` does not fork the running (already multi-threaded) process --
forking after threads exist is a deadlock hazard, and the running process holds a
parent-death watcher that would SIGTERM the child the moment the launcher exits
(``start_parent_death_watcher`` polls ``getppid()``). Instead the launcher
re-execs ``mngr foreman`` as a fresh, fully detached child: its own session
(``start_new_session`` -> ``setsid``, so no controlling tty), stdin from
``/dev/null``, and stdout+stderr redirected to a log file. A single environment
marker (``MNGR_FOREMAN_DAEMONIZED``) tells that child to (a) run the server in the
foreground rather than re-daemonizing, and (b) skip the parent-death watcher --
a daemon is meant to outlive its launcher.

This module holds only the OS-level primitives (process liveness, pid-file
read/write, detached spawn) so they can be unit-tested without a running server.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

_DAEMON_ENV_MARKER: Final[str] = "MNGR_FOREMAN_DAEMONIZED"


def is_daemon_child() -> bool:
    """True when this process was spawned as the detached foreman daemon."""
    return os.environ.get(_DAEMON_ENV_MARKER) == "1"


def is_process_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists.

    Uses the signal-0 probe: ``os.kill(pid, 0)`` sends no signal but still
    performs the existence + permission check. ``ProcessLookupError`` means no
    such process; ``PermissionError`` means it exists but is owned by another
    user (still "alive" for our guard's purposes).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_running_daemon_pid(pid_file: Path) -> int | None:
    """Return the PID recorded in ``pid_file`` iff that process is alive.

    A missing file, unparseable contents, or a stale (dead) PID all read as
    "not running" -- a leftover pid-file from a crashed server must never block a
    fresh start.
    """
    try:
        text = pid_file.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if is_process_alive(pid) else None


def write_pid_file(pid_file: Path, pid: int) -> None:
    """Write ``pid`` to ``pid_file`` (creating parent dirs), one line, trailing newline."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{pid}\n")


def spawn_detached_foreman(mngr_binary: str, forwarded_args: Sequence[str], log_file: Path) -> int:
    """Re-exec ``mngr foreman`` fully detached and return the child PID.

    ``forwarded_args`` are the original ``mngr`` sub-args (``sys.argv[1:]``,
    e.g. ``("foreman", "-d", "--port", "8700")``); they are replayed verbatim.
    The ``MNGR_FOREMAN_DAEMONIZED`` marker makes the child ignore ``-d`` (run in
    the foreground) rather than daemonizing again, so replaying the exact args --
    including ``-d`` -- is safe and needs no arg surgery.

    stdout+stderr are appended to ``log_file`` (restarts keep one history);
    ``start_new_session=True`` puts the child in its own session with no
    controlling terminal, so it survives the launcher's exit and the terminal
    closing. The server never opens ``/dev/tty``, so this single detach is
    sufficient (no second fork needed to prevent tty reacquisition).
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    child_env[_DAEMON_ENV_MARKER] = "1"
    # A detached daemon must NOT be tracked by a ConcurrencyGroup: the group reaps
    # its processes on exit, which is exactly what we are avoiding here (the server
    # is meant to outlive this command). So this is a deliberate direct-Popen use.
    with open(os.devnull, "rb") as devnull, open(log_file, "ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            [mngr_binary, *forwarded_args],
            stdin=devnull,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_env,
            close_fds=True,
        )
    return proc.pid
