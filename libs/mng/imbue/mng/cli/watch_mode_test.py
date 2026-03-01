"""Tests for the watch mode utility."""

import time

import pytest

from imbue.mng.cli.watch_mode import run_watch_loop
from imbue.mng.errors import MngError


class _StopLoop(Exception):
    """Raised by test callbacks to break out of run_watch_loop."""


def test_run_watch_loop_runs_iteration_function() -> None:
    """run_watch_loop should call the iteration function."""
    call_count = [0]

    def iteration_fn() -> None:
        call_count[0] += 1
        if call_count[0] >= 2:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_watch_loop(iteration_fn, interval_seconds=0.05)

    # Should have called the function at least once before stopping
    assert call_count[0] >= 1


def test_run_watch_loop_waits_between_iterations() -> None:
    """run_watch_loop should wait for the specified interval between iterations."""
    timestamps: list[float] = []

    def iteration_fn() -> None:
        timestamps.append(time.monotonic())
        if len(timestamps) >= 2:
            raise _StopLoop()

    # Use interval_seconds=0.3 so we can verify the parameter actually controls wait duration.
    # Asserting >= 0.2 ensures the interval parameter is actually used (a zero-wait would fail).
    with pytest.raises(_StopLoop):
        run_watch_loop(iteration_fn, interval_seconds=0.3)

    # Should have at least 2 calls
    assert len(timestamps) >= 2
    gap = timestamps[1] - timestamps[0]
    assert gap >= 0.2, f"Expected gap >= 0.2s (interval=0.3), got {gap:.2f}s"


def test_run_watch_loop_continues_on_mng_error_by_default() -> None:
    """run_watch_loop should continue on MngError when on_error_continue is True."""
    call_count = [0]

    def iteration_fn() -> None:
        call_count[0] += 1
        if call_count[0] == 1:
            raise MngError("Test error")
        if call_count[0] >= 3:
            raise _StopLoop()

    with pytest.raises(_StopLoop):
        run_watch_loop(iteration_fn, interval_seconds=0.05, on_error_continue=True)

    # Should have continued past the error
    assert call_count[0] >= 2


def test_run_watch_loop_stops_on_mng_error_when_configured() -> None:
    """run_watch_loop should re-raise MngError when on_error_continue is False."""
    call_count = [0]

    def iteration_fn() -> None:
        call_count[0] += 1
        raise MngError("Test error")

    with pytest.raises(MngError, match="Test error"):
        run_watch_loop(iteration_fn, interval_seconds=0.05, on_error_continue=False)

    # Should have stopped after the first error
    assert call_count[0] == 1
