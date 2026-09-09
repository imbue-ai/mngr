"""Fixtures for the tests in ``imbue/mngr/utils``."""

from collections.abc import Iterator

import pytest

from imbue.imbue_common.suspension import ClockReading
from imbue.mngr.utils.suspension_watchdog import SuspensionWatchdog


class PosableClock:
    """A pair of clocks the test advances by hand, so a suspension can be posed.

    Advancing ``wall_seconds`` alone is exactly what a suspension looks like:
    wall time passed that the monotonic clock did not see.
    """

    def __init__(self) -> None:
        self.wall_seconds = 1000.0
        self.monotonic_seconds = 500.0

    def __call__(self) -> ClockReading:
        return ClockReading(wall_seconds=self.wall_seconds, monotonic_seconds=self.monotonic_seconds)

    def pose_a_suspension(self, seconds: float) -> None:
        self.wall_seconds += seconds
        self.monotonic_seconds += 1.0

    def pass_time_awake(self, seconds: float) -> None:
        self.wall_seconds += seconds
        self.monotonic_seconds += seconds


@pytest.fixture
def watchdog_on_a_posable_clock() -> Iterator[tuple[SuspensionWatchdog, PosableClock]]:
    """A watchdog whose clock the test drives, shut down so its thread does not outlive the test."""
    clock = PosableClock()
    watchdog = SuspensionWatchdog(clock_fn=clock)
    try:
        yield watchdog, clock
    finally:
        watchdog.shutdown()
