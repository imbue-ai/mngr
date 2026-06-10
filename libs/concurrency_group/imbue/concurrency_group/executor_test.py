import threading
from threading import Event

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor


def _return_value(value: int) -> int:
    return value


def _raise_value_error() -> None:
    raise ValueError("test error")


def test_executor_submit_returns_future_with_result() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=4) as executor:
            future = executor.submit(_return_value, 42)
    assert future.result() == 42


def test_executor_submit_multiple_callables() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=4) as executor:
            futures = [executor.submit(_return_value, i) for i in range(10)]
    results = [f.result() for f in futures]
    assert results == list(range(10))


def test_executor_respects_max_workers() -> None:
    max_concurrent = 0
    current_concurrent = 0
    lock = threading.Lock()
    # Barrier synchronizes pairs of workers so they overlap, guaranteeing we observe concurrency.
    # If max_workers were not honored and only one worker ran at a time, the barrier would never be
    # satisfied and time out; we record that as a failure instead of swallowing it.
    barrier = threading.Barrier(2, timeout=5.0)
    barrier_breaks: list[threading.BrokenBarrierError] = []

    def _track_concurrency() -> None:
        nonlocal max_concurrent, current_concurrent
        with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        try:
            barrier.wait()
        except threading.BrokenBarrierError as e:
            barrier_breaks.append(e)
        with lock:
            current_concurrent -= 1

    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=2) as executor:
            for _ in range(6):
                executor.submit(_track_concurrency)

    # Two workers must actually run at the same time (not merely "at most two"): the barrier forces
    # pairs to overlap, so a regression that serialized work would leave max_concurrent at 1 and
    # break the barrier.
    assert barrier_breaks == []
    assert max_concurrent == 2


def test_executor_propagates_exceptions_via_future() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=4) as executor:
            future = executor.submit(_raise_value_error)

    with pytest.raises(ValueError, match="test error"):
        future.result()


def test_executor_exception_does_not_prevent_other_submissions() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=4) as executor:
            error_future = executor.submit(_raise_value_error)
            ok_future = executor.submit(_return_value, 99)

    assert ok_future.result() == 99
    with pytest.raises(ValueError, match="test error"):
        error_future.result()


def test_executor_waits_for_all_threads_on_exit() -> None:
    release = Event()

    def _wait_and_return(value: int) -> int:
        release.wait(timeout=5.0)
        return value

    with ConcurrencyGroup(name="outer") as cg:
        with ConcurrencyGroupExecutor(parent_cg=cg, name="test", max_workers=4) as executor:
            futures = [executor.submit(_wait_and_return, i) for i in range(5)]
            release.set()

    # After the executor context exits, all threads should be done
    assert all(f.done() for f in futures)
    assert sorted(f.result() for f in futures) == [0, 1, 2, 3, 4]
