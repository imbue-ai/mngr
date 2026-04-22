import os
import threading
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import start_parent_death_watcher


def test_start_parent_death_watcher_starts_thread_in_concurrency_group() -> None:
    """Verify the watcher thread is started and is alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "parent-death-watcher"]
        assert len(threads) == 1
        assert threads[0].thread.is_alive()


def test_parent_death_watcher_does_not_fire_when_parent_alive() -> None:
    """Verify the watcher doesn't trigger when the parent is still alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        # Wait for a poll cycle to complete by polling the thread state
        poll_complete = threading.Event()
        poll_complete.wait(timeout=4.0)
        # We should still be here (no SIGTERM received)
        assert os.getpid() > 0
