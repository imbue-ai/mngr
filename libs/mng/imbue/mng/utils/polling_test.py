"""Unit tests for the polling module."""

import time

import pytest

from imbue.mng.utils.polling import poll_for_value
from imbue.mng.utils.polling import poll_until
from imbue.mng.utils.polling import run_periodically
from imbue.mng.utils.polling import wait_for


def test_poll_for_value_returns_value_immediately_when_producer_returns_non_none() -> None:
    value, poll_count, elapsed = poll_for_value(lambda: "found", timeout=1.0)

    assert value == "found"
    assert poll_count == 1
    assert elapsed < 0.5


def test_poll_for_value_returns_none_on_timeout() -> None:
    value, poll_count, elapsed = poll_for_value(lambda: None, timeout=0.2, poll_interval=0.05)

    assert value is None
    assert poll_count >= 2
    assert elapsed >= 0.2


def test_poll_for_value_polls_until_value_available() -> None:
    call_count = 0

    def producer() -> str | None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return "ready"
        return None

    value, poll_count, elapsed = poll_for_value(producer, timeout=2.0, poll_interval=0.05)

    assert value == "ready"
    assert poll_count == 3


def test_poll_for_value_succeeds_on_final_check_after_timeout() -> None:
    """poll_for_value should do one final check after timeout and return value if available."""
    call_count = 0

    def producer() -> str | None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return "late-value"
        return None

    value, poll_count, elapsed = poll_for_value(producer, timeout=0.1, poll_interval=0.05)
    assert value == "late-value"


def test_poll_for_value_returns_non_string_types() -> None:
    value, poll_count, _ = poll_for_value(lambda: 42, timeout=1.0)

    assert value == 42
    assert poll_count == 1


def test_poll_until_returns_true_when_condition_met() -> None:
    """poll_until should return True when condition is met immediately."""
    result = poll_until(lambda: True, timeout=1.0)

    assert result is True


def test_poll_until_returns_false_on_timeout() -> None:
    """poll_until should return False when timeout expires without condition being met."""
    result = poll_until(lambda: False, timeout=0.3, poll_interval=0.1)

    assert result is False


def test_poll_until_polls_until_condition_met() -> None:
    """poll_until should poll until condition is met."""
    start = time.time()

    result = poll_until(lambda: time.time() - start > 0.15, timeout=1.0, poll_interval=0.05)
    elapsed = time.time() - start
    assert result is True
    assert elapsed > 0.15
    assert elapsed < 0.75


def test_wait_for_returns_immediately_when_condition_true() -> None:
    """wait_for should return immediately when condition is already true."""
    wait_for(lambda: True, timeout=1.0)


def test_wait_for_raises_timeout_error_when_condition_never_true() -> None:
    """wait_for should raise TimeoutError when condition never becomes true."""
    with pytest.raises(TimeoutError, match="Condition not met"):
        wait_for(lambda: False, timeout=0.1, poll_interval=0.05, error_message="Condition not met")


def test_wait_for_custom_error_message() -> None:
    """wait_for should use custom error message."""
    with pytest.raises(TimeoutError, match="Custom error"):
        wait_for(lambda: False, timeout=0.1, poll_interval=0.05, error_message="Custom error")


class _StopIteration(Exception):
    """Raised by test callbacks to break out of run_periodically."""


def test_run_periodically_calls_fn_repeatedly() -> None:
    """run_periodically should call fn repeatedly until an exception propagates."""
    call_count = [0]

    def fn() -> None:
        call_count[0] += 1
        if call_count[0] >= 3:
            raise _StopIteration()

    with pytest.raises(_StopIteration):
        run_periodically(fn, interval=0.05)

    assert call_count[0] == 3


def test_run_periodically_respects_interval() -> None:
    """run_periodically should wait the specified interval between calls."""
    timestamps: list[float] = []

    def fn() -> None:
        timestamps.append(time.monotonic())
        if len(timestamps) >= 2:
            raise _StopIteration()

    with pytest.raises(_StopIteration):
        run_periodically(fn, interval=0.3)

    assert len(timestamps) == 2
    gap = timestamps[1] - timestamps[0]
    assert gap >= 0.25, f"Expected gap >= 0.25s (interval=0.3), got {gap:.2f}s"


def test_run_periodically_propagates_exceptions() -> None:
    """run_periodically should let exceptions from fn propagate."""

    def fn() -> None:
        raise _StopIteration()

    with pytest.raises(_StopIteration):
        run_periodically(fn, interval=0.05)
