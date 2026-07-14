"""Unit tests for thread-local gevent Hub cleanup.

The regression here guards against the leak in gevent issue 1601: destroying a
per-thread hub without joining it first parks the hub greenlet mid-``LoopExit``,
whose traceback pins the worker's frames in a cycle the GC cannot break, so a
fresh Hub (and everything the task referenced) is stranded on every call.
"""

import gc

import gevent
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.thread_cleanup import mngr_executor


def _count_live_hubs() -> int:
    for _ in range(5):
        gc.collect()
    return sum(1 for obj in gc.get_objects() if type(obj).__name__ == "Hub")


def _run_one_discovery_like_poll() -> None:
    """Submit a task that touches gevent on a worker thread, as list_agents does."""
    with ConcurrencyGroup(name="thread_cleanup_test") as cg:
        with mngr_executor(parent_cg=cg, name="cleanup_probe", max_workers=4) as executor:
            future = executor.submit(lambda: gevent.sleep(0.001))
        future.result()


# The repeated full-heap gc.collect() passes are slow on a loaded machine (the
# observed failure mode is the suite-default 10s timeout, not the assertion),
# so this test carries its own generous timeout and a flaky retry on top.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_worker_hubs_do_not_accumulate_across_polls() -> None:
    """Each poll spins up worker threads that create gevent hubs; cleanup must
    free them so repeated polls do not strand a hub (and its retained object
    graph) per iteration."""
    # Warm up so any one-time hub allocation is already counted in the baseline.
    for _ in range(5):
        _run_one_discovery_like_poll()
    baseline_hubs = _count_live_hubs()

    iterations = 25
    for _ in range(iterations):
        _run_one_discovery_like_poll()

    growth = _count_live_hubs() - baseline_hubs
    # With the join-before-destroy fix, growth is ~0. Without it, growth scales
    # with the number of iterations (one stranded hub each). Allow a small
    # margin for worker threads that happen to be in flight.
    assert growth <= 5, f"gevent hubs accumulated across polls (grew by {growth} over {iterations} polls)"
