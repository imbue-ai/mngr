import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def poll_for_value(
    producer: Callable[[], T | None],
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> tuple[T | None, int, float]:
    """Poll until a producer returns a non-None value or timeout expires.

    Returns (value, poll_count, elapsed_seconds):
    - value: The first non-None value returned by the producer, or None if timeout occurred
    - poll_count: Number of times the producer was called
    - elapsed_seconds: Total time spent polling
    """
    start_time = time.time()
    poll_count = 0
    elapsed = 0.0
    while elapsed < timeout:
        poll_count += 1
        result = producer()
        if result is not None:
            return result, poll_count, time.time() - start_time
        time.sleep(poll_interval)
        elapsed = time.time() - start_time
    # One final check after timeout in case value became available during last sleep
    poll_count += 1
    result = producer()
    if result is not None:
        return result, poll_count, time.time() - start_time
    return None, poll_count, time.time() - start_time


def poll_until(
    condition: Callable[[], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> bool:
    """Poll until a condition becomes true or timeout expires.

    Returns True if the condition was met, False if timeout occurred.
    """
    value, _, _ = poll_for_value(
        lambda: True if condition() else None,
        timeout,
        poll_interval,
    )
    return value is not None


def wait_for(
    condition: Callable[[], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.1,
    error_message: str = "Condition not met within timeout",
) -> None:
    """Wait for a condition to become true, polling at regular intervals.

    This is a general-purpose polling utility for production code.
    Raises TimeoutError if the condition is not met within the timeout period.
    """
    if not poll_until(condition, timeout, poll_interval):
        raise TimeoutError(error_message)


def run_periodically(
    fn: Callable[[], None],
    interval: float,
) -> None:
    """Run a function repeatedly at a fixed interval until interrupted.

    Calls fn(), waits interval seconds, calls fn() again, and so on indefinitely.
    Terminates only when an exception (typically KeyboardInterrupt) propagates from fn.

    Internally uses poll_until with a never-satisfied condition -- the poll_interval
    controls the execution interval, and the condition performs the actual work as a
    side effect. This is intentionally "misusing" the polling semantics; run_periodically
    exists so that callers don't have to do this themselves.
    """
    # The condition calls fn() for its side effect and always returns False,
    # so poll_until keeps looping at the given interval.
    poll_until(
        condition=lambda: (fn(), False)[1],
        timeout=100 * 365 * 24 * 3600.0,
        poll_interval=interval,
    )
