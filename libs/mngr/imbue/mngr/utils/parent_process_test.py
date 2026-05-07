import os
import threading
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import _PARENT_POLL_INTERVAL_SECONDS
from imbue.mngr.utils.parent_process import _read_grandparent_pid
from imbue.mngr.utils.parent_process import start_grandparent_death_watcher
from imbue.mngr.utils.parent_process import start_parent_death_watcher


def test_start_parent_death_watcher_starts_thread_in_concurrency_group() -> None:
    """Verify the watcher thread is started and is alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "parent-death-watcher"]
        assert len(threads) == 1
        assert threads[0].thread.is_alive()


def test_parent_death_watcher_does_not_fire_when_parent_alive() -> None:
    """Verify the watcher thread stays alive through a poll cycle when the parent is still alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "parent-death-watcher"]
        assert len(threads) == 1
        watcher_thread = threads[0].thread

        # Poll until the watcher has had time for at least one full poll cycle.
        # If the watcher incorrectly fired, the thread would exit after detecting
        # a (false) parent death.
        deadline = threading.Event()
        deadline.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS + 1.0)
        assert watcher_thread.is_alive(), "Watcher thread exited unexpectedly during poll cycle"


def test_read_grandparent_pid_returns_alive_grandparent() -> None:
    """The helper should return a positive, signalable PID when a grandparent exists.

    Pytest under xdist runs each test inside a worker that has a real parent
    and grandparent, so locally this always resolves. Some offload sandboxes
    run pytest directly under PID 1, leaving no grandparent; in that case the
    helper correctly returns ``None`` and the test skips.
    """
    grandparent_pid = _read_grandparent_pid()
    if grandparent_pid is None:
        pytest.skip("No resolvable grandparent in this process tree (e.g. offload sandbox)")
    assert grandparent_pid > 1
    os.kill(grandparent_pid, 0)


def test_start_grandparent_death_watcher_starts_thread_when_resolvable() -> None:
    """When a grandparent exists, the watcher thread is started and stays alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_grandparent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "grandparent-death-watcher"]
        # If the test runner has no resolvable grandparent (very unusual), the
        # watcher is a no-op; both shapes are valid.
        if _read_grandparent_pid() is None:
            assert threads == []
            return
        assert len(threads) == 1
        watcher_thread = threads[0].thread
        deadline = threading.Event()
        deadline.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS + 1.0)
        assert watcher_thread.is_alive(), "Grandparent watcher exited unexpectedly during poll cycle"
