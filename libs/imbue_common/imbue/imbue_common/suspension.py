"""Detecting that the machine this process runs on was suspended.

A sleep kills every TCP connection the process holds, but the socket (and a
paramiko transport on it) keeps reporting itself alive until a write finally
makes the kernel give up -- ~48s at best, never for an idle socket. So anything
holding a connection across a sleep has to ask whether one happened.

The two clocks answer it: ``time.monotonic()`` stops while the machine is
suspended (Darwin's ``mach_absolute_time``, Linux's ``CLOCK_MONOTONIC``) and
``time.time()`` does not, so the difference between their elapsed spans is time
the machine was not running. On Darwin this is a CPython choice rather than a
platform guarantee -- the OS's own ``clock_gettime(CLOCK_MONOTONIC)`` keeps
counting through a sleep -- so a CPython reimplementation would silently disable
this detection.

No background thread or history is needed: stamp with :func:`read_clocks`, ask
later with :func:`was_suspended_since`. That is what makes it usable from a
short-lived CLI process as well as a daemon, unlike minds' ``SleepTracker``,
which needs a heartbeat loop to answer *when* the process came back.
"""

import time
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# How far the wall clock must outrun the monotonic clock before the gap counts as
# a suspension. Non-atomic sampling does not reach seconds, and an NTP step big
# enough to clear this means the clock was already badly wrong; the shortest
# sleep a lid produces is well over it.
_DEFAULT_SUSPENSION_THRESHOLD_SECONDS: Final[float] = 5.0


class ClockReading(FrozenModel):
    """Both clocks sampled together. Only the difference between two readings from the same process means anything."""

    wall_seconds: float = Field(description="``time.time()`` at the sample -- advances while suspended")
    monotonic_seconds: float = Field(description="``time.monotonic()`` at the sample -- does not")


def read_clocks() -> ClockReading:
    """Sample both clocks now, for a later :func:`was_suspended_since` to compare against."""
    return ClockReading(wall_seconds=time.time(), monotonic_seconds=time.monotonic())


def was_suspended_since(
    reading: ClockReading,
    *,
    threshold_seconds: float = _DEFAULT_SUSPENSION_THRESHOLD_SECONDS,
    now: ClockReading | None = None,
) -> bool:
    """Whether the machine was suspended between ``reading`` and ``now`` (sampled if omitted).

    Errors are one-sided: a wall clock stepped backwards or a CPU-starved
    process (both clocks advance together) reports no suspension.
    """
    current = read_clocks() if now is None else now
    wall_elapsed = current.wall_seconds - reading.wall_seconds
    monotonic_elapsed = current.monotonic_seconds - reading.monotonic_seconds
    return wall_elapsed - monotonic_elapsed >= threshold_seconds
