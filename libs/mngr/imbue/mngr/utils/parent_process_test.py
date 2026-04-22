import threading
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import _PARENT_POLL_INTERVAL_SECONDS
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
