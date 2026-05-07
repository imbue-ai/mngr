import os
import signal
import threading
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState

_PARENT_POLL_INTERVAL_SECONDS: Final[float] = 3.0


def _poll_parent_pid_until_changed(
    original_ppid: int,
    stop_event: threading.Event,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Poll parent PID and send SIGTERM if it changes."""
    while not stop_event.is_set():
        stop_event.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS)
        if stop_event.is_set():
            break
        if concurrency_group.state != ConcurrencyGroupState.ACTIVE:
            break
        current_ppid = os.getppid()
        if current_ppid != original_ppid:
            logger.info(
                "Parent process died (was PID {}, now reparented to PID {}), sending SIGTERM",
                original_ppid,
                current_ppid,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            break


def start_parent_death_watcher(concurrency_group: ConcurrencyGroup) -> None:
    """Start a daemon thread that exits the process when its parent dies.

    Records the current parent PID and polls every ~3 seconds. If the parent PID
    changes (e.g. reparented to PID 1 because the parent exited), sends SIGTERM
    to the current process. This triggers the same clean shutdown path as Ctrl+C.
    """
    original_ppid = os.getppid()
    logger.debug("Parent death watcher started (parent PID={})", original_ppid)

    stop_event = threading.Event()

    concurrency_group.start_new_thread(
        target=_poll_parent_pid_until_changed,
        args=(original_ppid, stop_event, concurrency_group),
        daemon=True,
        name="parent-death-watcher",
        is_checked=False,
    )


def _read_grandparent_pid() -> int | None:
    """Return the grandparent process's PID by reading ``/proc/<ppid>/status``.

    Returns ``None`` when the parent's status can't be read (e.g. the parent
    already exited, or we're on a platform without /proc) or when the parent
    has no real grandparent (PPid == 1, i.e. already orphaned to init).
    """
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return None
    try:
        with open(f"/proc/{parent_pid}/status") as status_file:
            for line in status_file:
                if line.startswith("PPid:"):
                    grandparent_pid = int(line.split()[1])
                    return grandparent_pid if grandparent_pid > 1 else None
    except OSError:
        return None
    return None


def _poll_grandparent_until_dead(
    grandparent_pid: int,
    stop_event: threading.Event,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Poll the grandparent's PID and SIGTERM ourselves when it stops existing."""
    while not stop_event.is_set():
        stop_event.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS)
        if stop_event.is_set():
            break
        if concurrency_group.state != ConcurrencyGroupState.ACTIVE:
            break
        try:
            os.kill(grandparent_pid, 0)
        except ProcessLookupError:
            logger.info(
                "Grandparent process (PID {}) no longer exists, sending SIGTERM",
                grandparent_pid,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            break
        except PermissionError:
            # PID belongs to a process we can't signal (e.g. reused by another
            # user). Treat as still alive; bailing on PermissionError would
            # cause spurious early shutdowns on PID reuse boundaries.
            continue


def start_grandparent_death_watcher(concurrency_group: ConcurrencyGroup) -> None:
    """Start a daemon thread that exits the process when its *grandparent* dies.

    Use this for processes spawned via a thin wrapper (e.g. ``minds forward``
    spawned by Electron via ``uv run ...``): the immediate parent is the
    wrapper, which Electron's death does not bring down on its own. Without
    this watcher, an Electron crash leaves the python process running
    indefinitely, taking ``mngr observe`` / ``mngr events`` along with it
    (since their own parent-death watchers see a still-live python parent).

    Records the grandparent PID at startup, polls ``os.kill(pid, 0)`` every
    ~3 seconds, and SIGTERMs the current process the first time the
    grandparent disappears. No-op when no grandparent can be resolved
    (parent is init, or ``/proc`` is unavailable).
    """
    grandparent_pid = _read_grandparent_pid()
    if grandparent_pid is None:
        logger.debug("Grandparent death watcher: no resolvable grandparent; skipping")
        return
    logger.debug("Grandparent death watcher started (grandparent PID={})", grandparent_pid)

    stop_event = threading.Event()

    concurrency_group.start_new_thread(
        target=_poll_grandparent_until_dead,
        args=(grandparent_pid, stop_event, concurrency_group),
        daemon=True,
        name="grandparent-death-watcher",
        is_checked=False,
    )
