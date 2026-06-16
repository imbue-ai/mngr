"""Unit tests for the polling module."""

import pytest

from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for


def test_poll_for_value_returns_value_immediately_when_producer_returns_non_none() -> None:
    value, poll_count, _ = poll_for_value(lambda: "found", timeout=1.0)

    assert value == "found"
    # A single call proves immediacy without coupling to wall-clock timing.
    assert poll_count == 1


def test_poll_for_value_returns_none_on_timeout() -> None:
    call_count = 0

    def producer() -> str | None:
        nonlocal call_count
        call_count += 1
        return None

    value, poll_count, _ = poll_for_value(producer, timeout=0.2, poll_interval=0.05)

    assert value is None
    # The loop runs a handful of times, then one final post-timeout check happens.
    # poll_count must equal the total producer invocations, and that total must include
    # the final check (i.e. exceed what the in-loop iterations alone produced).
    assert poll_count == call_count
    in_loop_calls = call_count - 1
    assert poll_count == in_loop_calls + 1
    assert in_loop_calls >= 1


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
    """poll_for_value should do one final check after timeout and return value if available.

    The producer only yields its value once the in-loop polling has already given up, so a
    successful return proves the value came from the single post-timeout check rather than
    from a regular loop iteration.
    """
    returns_value_after_loop_gives_up = False
    call_count = 0

    def producer() -> str | None:
        nonlocal call_count
        call_count += 1
        if returns_value_after_loop_gives_up:
            return "late-value"
        return None

    # With timeout==poll_interval the loop body runs exactly once (it sleeps, elapsed reaches
    # the timeout, and the `elapsed < timeout` guard then fails), so the value can only be
    # observed by the final post-timeout check.
    def producer_that_arms_after_loop() -> str | None:
        nonlocal returns_value_after_loop_gives_up
        result = producer()
        returns_value_after_loop_gives_up = True
        return result

    value, poll_count, _ = poll_for_value(producer_that_arms_after_loop, timeout=0.05, poll_interval=0.05)
    assert value == "late-value"
    # One in-loop call (returns None) plus the final post-timeout check that returns the value.
    assert poll_count == 2
    assert call_count == 2


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
    """poll_until should keep polling across several iterations until the condition turns true."""
    call_count = 0

    def condition() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= 3

    result = poll_until(condition, timeout=1.0, poll_interval=0.05)

    assert result is True
    # The condition was false twice and true on the third call.
    assert call_count == 3


def test_wait_for_returns_immediately_when_condition_true() -> None:
    """wait_for should return immediately, checking the condition exactly once, when it is already true."""
    call_count = 0

    def condition() -> bool:
        nonlocal call_count
        call_count += 1
        return True

    wait_for(condition, timeout=1.0)

    assert call_count == 1


def test_wait_for_raises_timeout_error_when_condition_never_true() -> None:
    """wait_for should raise TimeoutError when condition never becomes true."""
    with pytest.raises(TimeoutError, match="Condition not met"):
        wait_for(lambda: False, timeout=0.1, poll_interval=0.05, error_message="Condition not met")


def test_wait_for_custom_error_message() -> None:
    """wait_for should use custom error message."""
    with pytest.raises(TimeoutError, match="Custom error"):
        wait_for(lambda: False, timeout=0.1, poll_interval=0.05, error_message="Custom error")
