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
