"""Unit tests for the SSH suspension watchdog."""

import threading
import weakref

from imbue.mngr.utils.conftest import PosableClock
from imbue.mngr.utils.suspension_watchdog import SuspensionWatchdog


class _FakeTransport:
    """Stands in for a paramiko Transport: reports liveness and records its close."""

    def __init__(self, is_active: bool = True) -> None:
        self._is_active = is_active
        self.close_count = 0

    def is_active(self) -> bool:
        return self._is_active

    def getpeername(self) -> tuple[str, int]:
        return ("192.0.2.1", 22)

    def close(self) -> None:
        self.close_count += 1
        self._is_active = False

    def revive(self) -> None:
        """Report active again, so a later sweep would close it if it were still watched."""
        self._is_active = True


def test_a_transport_that_outlived_a_suspension_is_closed(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """Closing the transport is the only thing that reaches a reader blocked on a dead connection."""
    watchdog, clock = watchdog_on_a_posable_clock
    transport = _FakeTransport()
    watchdog.register(transport)  # ty: ignore[invalid-argument-type]
    clock.pose_a_suspension(730.0)

    assert watchdog.close_transports_that_outlived_a_suspension() == 1
    assert transport.close_count == 1


def test_a_transport_established_after_the_last_suspension_is_left_alone(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """A connection the machine never slept under is the one the command still needs."""
    watchdog, clock = watchdog_on_a_posable_clock
    transport = _FakeTransport()
    watchdog.register(transport)  # ty: ignore[invalid-argument-type]
    clock.pass_time_awake(730.0)

    assert watchdog.close_transports_that_outlived_a_suspension() == 0
    assert transport.close_count == 0


def test_a_closed_transport_stops_being_watched(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """Otherwise a long-running consumer accumulates every connection it ever made."""
    watchdog, clock = watchdog_on_a_posable_clock
    transport = _FakeTransport(is_active=False)
    watchdog.register(transport)  # ty: ignore[invalid-argument-type]
    clock.pose_a_suspension(730.0)

    assert watchdog.close_transports_that_outlived_a_suspension() == 0
    assert transport.close_count == 0

    # Dropped, not merely skipped: a transport that came back to life would be
    # closed by the next sweep if it were still on the books.
    transport.revive()
    clock.pose_a_suspension(730.0)

    assert watchdog.close_transports_that_outlived_a_suspension() == 0
    assert transport.close_count == 0


def test_watching_a_transport_never_keeps_it_alive(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """The registry holds weak references, so a dropped transport is collectable."""
    watchdog, _clock = watchdog_on_a_posable_clock
    transport = _FakeTransport()
    watchdog.register(transport)  # ty: ignore[invalid-argument-type]
    reference = weakref.ref(transport)

    del transport

    assert reference() is None
    assert watchdog.close_transports_that_outlived_a_suspension() == 0


def test_each_transport_is_judged_against_its_own_establishment(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """One reconnected after the wake must survive the sweep that retires its predecessor."""
    watchdog, clock = watchdog_on_a_posable_clock
    before_the_sleep = _FakeTransport()
    after_the_wake = _FakeTransport()
    watchdog.register(before_the_sleep)  # ty: ignore[invalid-argument-type]
    clock.pose_a_suspension(730.0)
    watchdog.register(after_the_wake)  # ty: ignore[invalid-argument-type]

    assert watchdog.close_transports_that_outlived_a_suspension() == 1
    assert before_the_sleep.close_count == 1
    assert after_the_wake.close_count == 0


def _count_watchdog_threads() -> int:
    """Watchdog threads in the process, by name; callers compare against a baseline."""
    return sum(1 for thread in threading.enumerate() if thread.name == "ssh-suspension-watchdog")


def test_registering_starts_one_background_thread_that_shutdown_stops() -> None:
    """A command that opens several connections must not pay a thread for each."""
    watchdog = SuspensionWatchdog(check_interval_seconds=0.01)
    transports = [_FakeTransport() for _ in range(3)]
    threads_before = _count_watchdog_threads()

    for transport in transports:
        watchdog.register(transport)  # ty: ignore[invalid-argument-type]

    assert _count_watchdog_threads() == threads_before + 1

    watchdog.shutdown()

    assert _count_watchdog_threads() == threads_before


def test_a_local_connector_with_no_transport_registers_nothing(
    watchdog_on_a_posable_clock: tuple[SuspensionWatchdog, PosableClock],
) -> None:
    """The chokepoint hands over whatever the connector has, which for local hosts is None."""
    watchdog, clock = watchdog_on_a_posable_clock
    threads_before = _count_watchdog_threads()

    watchdog.register(None)
    clock.pose_a_suspension(730.0)

    assert watchdog.close_transports_that_outlived_a_suspension() == 0
    assert _count_watchdog_threads() == threads_before
