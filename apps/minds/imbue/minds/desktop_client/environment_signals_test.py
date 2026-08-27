"""Unit tests for the laptop-side environment signals.

Both of the sleep tracker's clocks are injected, so a "sleep" here is simply two
heartbeats with a large wall gap between them -- no real waiting, and no
dependence on what the host machine's clocks did during the test. The
connectivity detector's endpoint checks are likewise injected, so no test of the
detector touches a socket at all. The exception is the last block, which drives
the production prober against listeners on loopback -- the socket handling is
what is under test there, and stubbing it would only restate it.
"""

import socket
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import ConnectivityFacet
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.desktop_client.environment_signals import SocketNetworkProber
from imbue.minds.desktop_client.environment_signals import SshEndpoint
from imbue.minds.desktop_client.environment_signals import _MAX_SAMPLED_WORKSPACE_SSH_ENDPOINTS
from imbue.minds.desktop_client.environment_signals import _address_attempt_seconds
from imbue.minds.desktop_client.testing import PUBLIC_SSH_ENDPOINTS
from imbue.minds.desktop_client.testing import STUB_CONNECTIVITY_HOSTS
from imbue.minds.desktop_client.testing import SideEffectingStubNetworkProber
from imbue.minds.desktop_client.testing import StubNetworkProber
from imbue.minds.desktop_client.testing import bring_stub_network_up
from imbue.minds.desktop_client.testing import build_connectivity_detector_over
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector
from imbue.minds.errors import MindError
from imbue.mngr.utils.polling import poll_until

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_GAP_THRESHOLD_SECONDS = 30.0


class _Clocks:
    """A manually-advanced pair of clocks: wall time, and a monotonic reading.

    Advancing both together is a process that was merely starved of CPU;
    advancing only the wall clock is a machine that slept, which is what freezes
    ``time.monotonic`` on macOS. The tracker records either as an interval, so
    tests advance both unless they are specifically about the difference.
    """

    def __init__(self, start: datetime) -> None:
        self._wall = start
        self._monotonic = 1000.0

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float, *, monotonic_seconds: float | None = None) -> None:
        self._wall += timedelta(seconds=seconds)
        self._monotonic += seconds if monotonic_seconds is None else monotonic_seconds


def _make_tracker(clocks: _Clocks) -> tuple[SleepTracker, list[datetime]]:
    tracker = SleepTracker(
        heartbeat_gap_threshold_seconds=_GAP_THRESHOLD_SECONDS,
        now_fn=clocks.wall,
        monotonic_fn=clocks.monotonic,
    )
    wakes: list[datetime] = []
    tracker.add_on_wake_callback(wakes.append)
    return tracker, wakes


def test_no_sleep_is_recorded_while_the_heartbeat_keeps_ticking() -> None:
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    for _ in range(10):
        tracker.record_heartbeat()
        clocks.advance(1.0)

    assert tracker.get_last_wake_at() is None
    assert not tracker.was_asleep_since(_T0)
    assert wakes == []


def test_a_gap_just_under_the_threshold_is_not_a_sleep() -> None:
    """Load, not sleep: the threshold exists so a starved loop is never mistaken for one."""
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    clocks.advance(_GAP_THRESHOLD_SECONDS - 0.5)
    tracker.record_heartbeat()

    assert tracker.get_last_wake_at() is None
    assert not tracker.was_asleep_since(_T0)
    assert wakes == []


def test_a_wall_gap_past_the_threshold_records_an_interval_and_fires_the_wake() -> None:
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    # A 15-minute lid-closed sleep: wall time ran, the monotonic clock did not.
    clocks.advance(900.0, monotonic_seconds=1.0)
    tracker.record_heartbeat()

    wake_at = _T0 + timedelta(seconds=900)
    assert tracker.get_last_wake_at() == wake_at
    assert tracker.was_asleep_since(_T0)
    assert wakes == [wake_at]


def test_a_gap_both_clocks_saw_still_records_an_interval() -> None:
    """A suspended process observed nothing either, so it suppresses the same way.

    The monotonic reading is a diagnostic label on the gap, never a condition on
    recording it: requiring the freeze would leave a SIGSTOP'd (or hypervisor-
    paused) minds convicting workspaces for seconds it never probed.
    """
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    clocks.advance(300.0)
    tracker.record_heartbeat()

    assert tracker.get_last_wake_at() == _T0 + timedelta(seconds=300)
    assert len(wakes) == 1


def test_the_wake_fires_once_per_gap_not_once_per_tick_after_it() -> None:
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    clocks.advance(900.0, monotonic_seconds=0.0)
    tracker.record_heartbeat()
    for _ in range(5):
        clocks.advance(1.0)
        tracker.record_heartbeat()

    assert len(wakes) == 1


def test_each_dark_wake_burst_reads_as_its_own_interval() -> None:
    """Powernap: short awake windows between sleeps, each with its own wake."""
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    for _ in range(3):
        clocks.advance(600.0, monotonic_seconds=0.0)
        tracker.record_heartbeat()
        # The burst itself: a few seconds of the loop actually running.
        for _ in range(3):
            clocks.advance(1.0)
            tracker.record_heartbeat()

    assert len(wakes) == 3
    assert tracker.get_last_wake_at() == wakes[-1]
    # The last burst is awake time, so a stretch measured inside it is not
    # suppressed -- only a stretch reaching back past the burst's own wake is.
    burst_start = wakes[-1]
    assert not tracker.was_asleep_since(burst_start)
    assert tracker.was_asleep_since(burst_start - timedelta(seconds=1))


def test_a_stretch_reaching_back_into_a_sleep_is_disqualified() -> None:
    """Only a stretch entirely after the wake was entirely observed."""
    clocks = _Clocks(_T0)
    tracker, _wakes = _make_tracker(clocks)

    # One interval spanning [T0 + 10s, T0 + 310s], then a few seconds awake.
    clocks.advance(10.0)
    tracker.record_heartbeat()
    clocks.advance(300.0, monotonic_seconds=0.0)
    tracker.record_heartbeat()
    interval_end = _T0 + timedelta(seconds=310)
    clocks.advance(5.0)
    tracker.record_heartbeat()

    # Measured from before the sleep, or from inside it: seconds nobody watched.
    assert tracker.was_asleep_since(_T0)
    assert tracker.was_asleep_since(interval_end - timedelta(seconds=1))
    # Measured from the wake onwards: every second of it was observed.
    assert not tracker.was_asleep_since(interval_end)
    assert not tracker.was_asleep_since(interval_end + timedelta(seconds=1))


def test_a_window_is_disqualified_only_if_the_sleep_fell_inside_it() -> None:
    """An observation that is already over is judged by its own window, not by everything since.

    A discovery poll reports when it began and when it finished. One that
    finished before the lid closed and was merely consumed after the wake is
    evidence; ``was_asleep_since`` would throw it out, which is why the window
    form exists.
    """
    clocks = _Clocks(_T0)
    tracker, _wakes = _make_tracker(clocks)

    # One interval spanning [T0 + 10s, T0 + 310s].
    clocks.advance(10.0)
    tracker.record_heartbeat()
    clocks.advance(300.0, monotonic_seconds=0.0)
    tracker.record_heartbeat()
    sleep_start = _T0 + timedelta(seconds=10)
    sleep_end = _T0 + timedelta(seconds=310)

    # Closed before the sleep began: every second of it was watched.
    assert not tracker.was_asleep_during(_T0, sleep_start - timedelta(seconds=1))
    assert tracker.was_asleep_since(_T0), "the 'since' form cannot tell this window apart"
    # Straddling it, wholly inside it, or opened inside it: not evidence.
    assert tracker.was_asleep_during(_T0, sleep_end + timedelta(seconds=5))
    assert tracker.was_asleep_during(sleep_start + timedelta(seconds=1), sleep_end - timedelta(seconds=1))
    assert tracker.was_asleep_during(sleep_end - timedelta(seconds=1), sleep_end + timedelta(seconds=5))
    # Opened at the wake: every second of it was watched.
    assert not tracker.was_asleep_during(sleep_end, sleep_end + timedelta(seconds=5))
    # No sleep recorded at all answers no for any window.
    fresh_tracker, _ = _make_tracker(_Clocks(_T0))
    assert not fresh_tracker.was_asleep_during(_T0, sleep_end)


def test_a_backwards_clock_step_records_nothing_and_rebaselines() -> None:
    """An NTP correction is not evidence the process stopped running."""
    clocks = _Clocks(_T0)
    tracker, wakes = _make_tracker(clocks)

    tracker.record_heartbeat()
    clocks.advance(-3600.0)
    tracker.record_heartbeat()

    assert tracker.get_last_wake_at() is None
    assert wakes == []

    # The next real gap is measured against the corrected reading, not the
    # pre-correction one, so the step is not double-counted into it.
    clocks.advance(300.0, monotonic_seconds=0.0)
    tracker.record_heartbeat()

    assert wakes == [_T0 - timedelta(seconds=3600) + timedelta(seconds=300)]


def _raise_a_mind_error_for_the_wake(wake_at: datetime) -> None:
    """Stand-in for a wake consumer whose own work fails."""
    raise MindError(f"the wake at {wake_at.isoformat()} could not be consumed")


def test_a_failing_wake_callback_takes_neither_the_others_nor_the_loop_with_it() -> None:
    """The wake is established on the loops that convict, and one of them owns STUCK.

    ``record_heartbeat`` is called from the system-interface health probe loop
    and the discovery watchdog loop, not only from the heartbeat thread, so a
    callback escaping it kills a checked strand -- and in the first case the
    only thing that can ever take a machine out of STUCK. The families caught
    are wide for that reason, and a MindError is the one that would slip a fence
    written for RuntimeError, being a ClickException.
    """
    clocks = _Clocks(_T0)
    tracker, _wakes = _make_tracker(clocks)
    survivors: list[datetime] = []
    tracker.add_on_wake_callback(_raise_a_mind_error_for_the_wake)
    tracker.add_on_wake_callback(survivors.append)

    tracker.record_heartbeat()
    clocks.advance(300.0, monotonic_seconds=0.0)
    # Unfenced, this is the call that raises -- out of whichever loop's tick
    # closed the gap.
    tracker.record_heartbeat()

    assert survivors == [_T0 + timedelta(seconds=300)], "a callback that raised must not cost the ones after it"
    assert tracker.get_last_wake_at() == _T0 + timedelta(seconds=300), "nor the interval it was fired for"


def _make_detector(
    concurrency_group: ConcurrencyGroup, *, is_internet_up: bool = False, is_ssh_up: bool = False
) -> tuple[ConnectivityDetector, StubNetworkProber, list[int]]:
    """A detector over a stub network, with its recovery firings recorded."""
    detector, prober = build_stub_connectivity_detector(
        concurrency_group, is_internet_up=is_internet_up, is_ssh_up=is_ssh_up
    )
    recoveries: list[int] = []
    detector.add_on_recovery_callback(lambda: recoveries.append(1))
    return detector, prober, recoveries


def test_nothing_is_known_before_the_first_probe(root_concurrency_group: ConcurrencyGroup) -> None:
    """An unmeasured device must not read as blocked -- that would suppress on no evidence."""
    detector, prober, _recoveries = _make_detector(root_concurrency_group)

    reading = detector.get_reading()

    assert reading.internet is ConnectivityFacet.UNKNOWN
    assert reading.ssh is ConnectivityFacet.UNKNOWN
    assert reading.environment_block is EnvironmentBlock.NONE
    assert prober.probed_endpoints == []


def test_a_working_network_reads_online_and_blocks_nothing(root_concurrency_group: ConcurrencyGroup) -> None:
    detector, prober, _recoveries = _make_detector(root_concurrency_group, is_internet_up=True, is_ssh_up=True)

    reading = detector.probe_now()

    assert reading.internet is ConnectivityFacet.ONLINE
    assert reading.ssh is ConnectivityFacet.ONLINE
    assert reading.environment_block is EnvironmentBlock.NONE
    # Both rounds ask their whole quorum at once, which is what buys the verdicts
    # that have to hear from every host -- so a good network costs three
    # connections in each rather than one. Sorted because the rounds ask on
    # threads of their own, so which host records itself first is a scheduling
    # accident rather than anything promised.
    assert sorted(call for call in prober.probed_endpoints if not call.startswith("ssh://")) == sorted(
        f"{host}:443" for host in STUB_CONNECTIVITY_HOSTS
    )
    assert sorted(call for call in prober.probed_endpoints if call.startswith("ssh://")) == sorted(
        f"ssh://{host}:22" for host in STUB_CONNECTIVITY_HOSTS
    )


def test_no_host_answering_reads_offline_and_leaves_ssh_untested(root_concurrency_group: ConcurrencyGroup) -> None:
    """With nothing reachable at all, port 22 was never actually tried."""
    detector, prober, _recoveries = _make_detector(root_concurrency_group)

    reading = detector.probe_now()

    assert reading.internet is ConnectivityFacet.OFFLINE
    assert reading.ssh is ConnectivityFacet.UNKNOWN
    assert reading.environment_block is EnvironmentBlock.OFFLINE
    assert not any(call.startswith("ssh://") for call in prober.probed_endpoints)


def test_every_ssh_endpoint_failing_on_a_working_internet_reads_ssh_blocked(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The user's browser works, so they must be told about the port, not about being offline."""
    detector, prober, _recoveries = _make_detector(root_concurrency_group, is_internet_up=True)

    reading = detector.probe_now()

    assert reading.internet is ConnectivityFacet.ONLINE
    assert reading.ssh is ConnectivityFacet.OFFLINE
    assert reading.environment_block is EnvironmentBlock.SSH_BLOCKED
    # Every endpoint has to fail: one site being down is not the network. Sorted
    # because the round asks them on threads of its own, so which one records
    # itself first is a scheduling accident rather than anything promised.
    assert sorted(call for call in prober.probed_endpoints if call.startswith("ssh://")) == sorted(
        f"ssh://{host}:22" for host in STUB_CONNECTIVITY_HOSTS
    )


def test_one_reachable_ssh_endpoint_clears_the_ssh_verdict(root_concurrency_group: ConcurrencyGroup) -> None:
    """A single site blocking us, or being down, is not this network blocking port 22."""
    detector, prober, _recoveries = _make_detector(root_concurrency_group, is_internet_up=True)
    prober.ssh_endpoints = {SshEndpoint(host="gamma.example", port=22)}

    reading = detector.probe_now()

    assert reading.ssh is ConnectivityFacet.ONLINE
    assert reading.environment_block is EnvironmentBlock.NONE


def test_a_wake_blanks_the_reading_without_claiming_a_recovery(root_concurrency_group: ConcurrencyGroup) -> None:
    """The laptop may have woken on another network; the old reading describes neither."""
    detector, _prober, recoveries = _make_detector(root_concurrency_group)
    detector.probe_now()
    assert detector.get_reading().environment_block is EnvironmentBlock.OFFLINE

    detector.invalidate_after_wake(_T0)

    assert detector.get_reading().internet is ConnectivityFacet.UNKNOWN
    assert detector.get_reading().environment_block is EnvironmentBlock.NONE
    # Nothing has been observed to come back, so nothing owed may fire yet.
    assert recoveries == []


def test_recovery_fires_once_when_a_probe_finds_the_device_reachable_again(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    detector, prober, recoveries = _make_detector(root_concurrency_group)

    detector.probe_now()
    assert recoveries == []

    bring_stub_network_up(prober)
    detector.probe_now()
    detector.probe_now()

    assert recoveries == [1]


def test_recovery_still_fires_for_a_probe_taken_after_a_wake_blanked_the_reading(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The wake clears the reading, not the fact that something is owed.

    Keyed on the blanked reading instead, a laptop that slept while offline and
    woke up connected would strand every withheld restart until the next outage.
    """
    detector, prober, recoveries = _make_detector(root_concurrency_group)
    detector.probe_now()

    detector.invalidate_after_wake(_T0)
    bring_stub_network_up(prober)
    detector.probe_now()

    assert recoveries == [1]


def test_each_move_of_the_condition_notifies_the_surfaces_that_render_it(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Once per move, in either direction, and not at all for a probe that re-reads the same one.

    The surfaces have no other source: the device's condition is read off this
    detector alone, so a device that goes dark before anything has been
    convicted is announced through here or nowhere. Firing on every probe
    instead would rewrite the strip under the user every poll interval.
    """
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)
    changes: list[EnvironmentBlock] = []
    detector.add_on_change_callback(lambda: changes.append(detector.get_reading().environment_block))

    detector.probe_now()
    detector.probe_now()
    # HTTPS back while SSH stays dead: a different condition, not a better one.
    prober.reachable_hosts = set(STUB_CONNECTIVITY_HOSTS)
    detector.probe_now()
    bring_stub_network_up(prober)
    detector.probe_now()

    assert changes == [EnvironmentBlock.OFFLINE, EnvironmentBlock.SSH_BLOCKED, EnvironmentBlock.NONE]


def test_a_wake_that_drops_a_bad_reading_tells_the_surfaces_showing_it(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """They are rendering a condition the wake just made unmeasured, and must stop.

    A wake with nothing to drop says nothing: the condition did not move, and
    announcing it would rewrite the strip for a state that never changed.
    """
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)
    changes: list[EnvironmentBlock] = []
    detector.add_on_change_callback(lambda: changes.append(detector.get_reading().environment_block))
    detector.probe_now()

    detector.invalidate_after_wake(_T0)
    detector.invalidate_after_wake(_T0)

    assert changes == [EnvironmentBlock.OFFLINE, EnvironmentBlock.NONE]


def test_the_background_loop_probes_only_while_a_bad_reading_is_outstanding() -> None:
    """Steady state is silence: a device on a working network generates no probes."""
    with ConcurrencyGroup(name="test-connectivity") as concurrency_group:
        detector, prober, recoveries = _make_detector(concurrency_group)
        # Enter the watching state the way production does: something asked, and
        # the answer was bad.
        detector.probe_now()

        concurrency_group.start_new_thread(
            target=detector.run_background_loop, args=(concurrency_group,), name="test-connectivity-loop"
        )
        assert poll_until(
            lambda: len(prober.probed_endpoints) > len(STUB_CONNECTIVITY_HOSTS), timeout=5.0, poll_interval=0.02
        ), "a bad reading must be re-probed until it clears"
        bring_stub_network_up(prober)
        assert poll_until(lambda: recoveries == [1], timeout=5.0, poll_interval=0.02), (
            "the loop must notice the network coming back"
        )
        calls_at_recovery = len(prober.probed_endpoints)
        # Several poll intervals with nothing outstanding.
        threading.Event().wait(timeout=0.5)
        calls_after_idling = len(prober.probed_endpoints)
        concurrency_group.shutdown()

    assert calls_after_idling == calls_at_recovery, "a good reading must stop the probing entirely"


def _fail_the_walk_behind_the_endpoints() -> None:
    """Everything a probe reaches that is not the socket, failing.

    The walk over discovery behind ``workspace_ssh_endpoints_fn``, and the SSH
    round's threads, which the parent group refuses to start once it is
    shutting down.
    """
    raise MindError("the walk behind this probe's endpoints failed")


def test_a_probe_that_raises_does_not_take_the_loop_that_would_clear_the_reading() -> None:
    """The loop survives a failed probe, because nothing else can end the outage.

    This thread is the only thing that ever observes the network coming back.
    Losing it while a bad reading is outstanding leaves the watch latched on with
    nothing to lift it: every owed restart and every held view refresh would wait
    for a recovery no one is left to see.
    """
    prober = SideEffectingStubNetworkProber(on_first_question=_fail_the_walk_behind_the_endpoints, is_armed=False)

    with ConcurrencyGroup(name="test-connectivity-raising-probe") as concurrency_group:
        detector = build_connectivity_detector_over(prober, concurrency_group)
        recoveries: list[int] = []
        detector.add_on_recovery_callback(lambda: recoveries.append(1))
        detector.probe_now()
        assert detector.get_reading().environment_block is EnvironmentBlock.OFFLINE

        concurrency_group.start_new_thread(
            target=detector.run_background_loop, args=(concurrency_group,), name="test-connectivity-loop"
        )
        prober.is_armed = True
        assert poll_until(lambda: not prober.is_armed, timeout=5.0, poll_interval=0.02), (
            "the loop must reach the probe that raises"
        )
        bring_stub_network_up(prober)
        assert poll_until(lambda: recoveries == [1], timeout=5.0, poll_interval=0.02), (
            "and must still be running to see the network come back"
        )
        concurrency_group.shutdown()


def test_a_probe_opens_no_connections_once_the_app_is_going_down(root_concurrency_group: ConcurrencyGroup) -> None:
    """The drain joins every thread against one budget, and this one blocks in socket timeouts.

    On the network this detector exists for, a round was measured at 9.25s and
    the loop is probing most of the time -- so a probe that kept going would
    hold the quit and end it with a strand-timeout. And it must not store what
    it half-measured: the endpoints it never reached read as "nothing answered",
    which is a dead network recorded on the way out, with the callbacks that
    render one.
    """
    shutdown_event = threading.Event()
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, shutdown_event=shutdown_event)
    reading_before = detector.probe_now()
    endpoints_before = list(prober.probed_endpoints)
    shutdown_event.set()

    reading = detector.probe_now()

    assert prober.probed_endpoints == endpoints_before, "no connection may be opened once the app is going down"
    assert reading == reading_before, "and the reading in force must survive the round that measured nothing"


class _ProberThatRendezvouses(StubNetworkProber):
    """Holds every question of one round at a barrier until the whole round has arrived.

    A round asked one endpoint at a time can never fill the barrier, so it trips
    instead of releasing -- which makes "these ran together" an outcome rather
    than a stopwatch reading. The subclass says which round is held.
    """

    rendezvous: Callable[[], None] = Field(description="Waits for the rest of the round to arrive")


class _ProberThatRendezvousesOnEverySshQuestion(_ProberThatRendezvouses):
    def is_ssh_server(self, host: str, port: int) -> bool:
        self.rendezvous()
        return super().is_ssh_server(host, port)


class _ProberThatRendezvousesOnEveryInternetQuestion(_ProberThatRendezvouses):
    def is_reachable(self, host: str, port: int) -> bool:
        self.rendezvous()
        return super().is_reachable(host, port)


# Ceiling on "the whole round has arrived": a concurrent round fills the barrier
# as fast as three threads can be scheduled, so this only bounds a failing run.
# Kept well inside the suite's own ``--timeout=10`` per-test budget, so a round
# that went serial again fails on the assertion that says so rather than on
# pytest's opaque timeout.
_RENDEZVOUS_WAIT_SECONDS: Final[float] = 2.0


@pytest.mark.parametrize(
    ("prober_type", "is_ssh_round"),
    [(_ProberThatRendezvousesOnEverySshQuestion, True), (_ProberThatRendezvousesOnEveryInternetQuestion, False)],
    ids=["ssh", "internet"],
)
def test_a_round_asks_every_one_of_its_endpoints_at_once(
    prober_type: type[_ProberThatRendezvouses], is_ssh_round: bool
) -> None:
    """A round's endpoints are dialled concurrently, not one after another.

    Both rounds' expensive answer is the negative one, and it has to hear from
    every endpoint: serially that is the sum of every connection budget. The SSH
    round is the one a stuck machine's dispatch waits on, and one such round was
    measured at 9.25s; the internet round is the term that declares this device
    offline, and was the largest single term in the probe's worst case, which
    has to fit inside the concurrency group's exit budget when a quit lands
    mid-round. A first answer still settles the facet either way; what changed
    is that the rest were already being asked.
    """
    endpoint_count = len(STUB_CONNECTIVITY_HOSTS)
    barrier = threading.Barrier(endpoint_count, timeout=_RENDEZVOUS_WAIT_SECONDS)

    def rendezvous() -> None:
        # A serial round leaves the barrier one short, so the first arrival's
        # timeout breaks it. Swallowed here so the round still finishes and the
        # assertion below is what reports it, rather than an exception escaping
        # a probe thread.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass

    prober = prober_type(
        reachable_hosts=set(STUB_CONNECTIVITY_HOSTS),
        ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS),
        rendezvous=rendezvous,
    )

    with ConcurrencyGroup(name="test-concurrent-round") as cg:
        detector = build_connectivity_detector_over(prober, cg)
        reading = detector.probe_now()

    assert not barrier.broken, "the round did not run its endpoints together"
    assert reading.internet is ConnectivityFacet.ONLINE
    assert reading.ssh is ConnectivityFacet.ONLINE
    questions = [call for call in prober.probed_endpoints if call.startswith("ssh://") is is_ssh_round]
    assert len(questions) == endpoint_count


class _ProberThatQuitsPartWayThroughTheSshFacet(StubNetworkProber):
    """Runs ``on_first_ssh_question`` once the SSH facet has been entered.

    Where a quit actually lands, most of the time: the SSH facet is the long
    half of the round, and it is the half whose unasked endpoints would be
    recorded as refusals.
    """

    on_first_ssh_question: Callable[[], None] = Field(description="Run after the first SSH endpoint answers")
    _has_run: bool = PrivateAttr(default=False)

    def is_ssh_server(self, host: str, port: int) -> bool:
        is_up = super().is_ssh_server(host, port)
        if not self._has_run:
            self._has_run = True
            self.on_first_ssh_question()
        return is_up


def test_a_probe_cut_short_mid_round_does_not_record_the_half_it_never_measured(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The endpoints a shutdown skipped read as "nothing answered", which is a verdict.

    A quit landing inside the SSH facet leaves the rest of the quorum unasked,
    and an unasked endpoint is indistinguishable from a refused one -- so
    storing the round would write SSH_BLOCKED on the way out the door and fire
    the change callbacks that put "this network blocks SSH" on screen for
    however long the window outlives the quit. The internet facet already
    settled by then, so the guard that catches this is the one *after* the SSH
    facet, not the one before it.
    """
    shutdown_event = threading.Event()
    prober = _ProberThatQuitsPartWayThroughTheSshFacet(
        reachable_hosts=set(STUB_CONNECTIVITY_HOSTS),
        ssh_endpoints=set(),
        on_first_ssh_question=shutdown_event.set,
    )
    detector = build_connectivity_detector_over(prober, root_concurrency_group, shutdown_event=shutdown_event)
    changes: list[EnvironmentBlock] = []
    detector.add_on_change_callback(lambda: changes.append(detector.get_reading().environment_block))

    reading = detector.probe_now()

    ssh_questions = [endpoint for endpoint in prober.probed_endpoints if endpoint.startswith("ssh://")]
    assert len(ssh_questions) == 1, "the shutdown must land inside the SSH facet, not before it"
    assert reading.internet is ConnectivityFacet.UNKNOWN, "the round measured nothing that may be kept"
    assert detector.get_reading().environment_block is EnvironmentBlock.NONE
    assert changes == [], "and nothing may be announced for a condition that was never measured"


def test_a_burst_of_gates_shares_one_measurement_of_the_same_network(root_concurrency_group: ConcurrencyGroup) -> None:
    """A dropped network wedges every remote machine at once, and they all ask together.

    Without the shared reading each gate queues behind the last and re-measures
    the same dead network, putting tens of seconds between the first machine's
    verdict and the last's for no new information.
    """
    detector, prober, _recoveries = _make_detector(root_concurrency_group)

    first = detector.probe_now()
    probes_after_first = len(prober.probed_endpoints)
    second = detector.probe_now(max_reuse_age_seconds=60.0)

    assert second == first
    assert len(prober.probed_endpoints) == probes_after_first


def test_a_reading_older_than_the_caller_will_accept_is_taken_again(root_concurrency_group: ConcurrencyGroup) -> None:
    """The shared reading is a window, not a cache: past it a gate measures for itself.

    Without the expiry every gate would decide on an arbitrarily old reading --
    convicting or exonerating a network that is no longer in front of the laptop.
    """
    clocks = _Clocks(_T0)
    prober = StubNetworkProber()
    detector = build_connectivity_detector_over(prober, root_concurrency_group, now_fn=clocks.wall)

    detector.probe_now()
    probes_after_first = len(prober.probed_endpoints)

    clocks.advance(61.0)
    detector.probe_now(max_reuse_age_seconds=60.0)
    probes_after_expiry = len(prober.probed_endpoints)

    assert probes_after_expiry > probes_after_first

    # Zero is the default, and is what the background loop relies on to ever
    # observe the network coming back.
    detector.probe_now()

    assert len(prober.probed_endpoints) > probes_after_expiry


# The endpoint an imbue_cloud machine is actually reached on: a port the box
# forwarded, nowhere near 22.
_WORKSPACE_ENDPOINT = SshEndpoint(host="box.example", port=22131)


def test_a_reachable_workspace_endpoint_settles_the_ssh_facet_without_the_public_quorum(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The endpoints minds dials are the only ones whose answer is the question.

    A network that blocks port 22 in particular -- an ordinary anti-tunnelling
    policy -- says nothing about the high port an imbue_cloud machine's host
    answers on. Asking :22 alone would strand a working setup behind an
    incompatible-network verdict.
    """
    detector, prober = build_stub_connectivity_detector(
        root_concurrency_group, is_internet_up=True, is_ssh_up=False, workspace_ssh_endpoints=(_WORKSPACE_ENDPOINT,)
    )
    prober.ssh_endpoints = {_WORKSPACE_ENDPOINT}

    reading = detector.probe_now()

    assert reading.ssh is ConnectivityFacet.ONLINE
    assert reading.environment_block is EnvironmentBlock.NONE
    # Settled by minds' own endpoint; the public hosts were never asked on SSH.
    assert [call for call in prober.probed_endpoints if call.startswith("ssh://")] == [
        f"ssh://{_WORKSPACE_ENDPOINT.host}:22131"
    ]


def test_the_public_quorum_keeps_dead_machines_from_being_blamed_on_the_network(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Every machine failing has two explanations, and only one of them is the network.

    With public SSH still answering, SSH leaves this device fine, so the
    machines are unreachable for reasons of their own -- and the recovery paths
    must go on treating that as a machine problem, which is what it is.
    """
    detector, _prober = build_stub_connectivity_detector(
        root_concurrency_group, is_internet_up=True, is_ssh_up=True, workspace_ssh_endpoints=(_WORKSPACE_ENDPOINT,)
    )

    reading = detector.probe_now()

    assert reading.ssh is ConnectivityFacet.ONLINE
    assert reading.environment_block is EnvironmentBlock.NONE


def test_a_network_that_blocks_ssh_outright_is_still_reported(root_concurrency_group: ConcurrencyGroup) -> None:
    """The motivating case: public wifi where nothing SSH gets out at all."""
    detector, _prober = build_stub_connectivity_detector(
        root_concurrency_group, is_internet_up=True, is_ssh_up=False, workspace_ssh_endpoints=(_WORKSPACE_ENDPOINT,)
    )

    reading = detector.probe_now()

    assert reading.ssh is ConnectivityFacet.OFFLINE
    assert reading.environment_block is EnvironmentBlock.SSH_BLOCKED


def test_only_a_bounded_sample_of_this_device_s_endpoints_is_probed(root_concurrency_group: ConcurrencyGroup) -> None:
    """One answering endpoint settles it, so the cap only bounds the all-failing case."""
    endpoints = tuple(
        SshEndpoint(host=f"box{index}.example", port=22000 + index)
        for index in range(_MAX_SAMPLED_WORKSPACE_SSH_ENDPOINTS * 3)
    )
    detector, prober = build_stub_connectivity_detector(
        root_concurrency_group, is_internet_up=True, is_ssh_up=True, workspace_ssh_endpoints=endpoints
    )

    detector.probe_now()

    workspace_probes = [call for call in prober.probed_endpoints if call.startswith("ssh://box")]
    assert len(workspace_probes) == _MAX_SAMPLED_WORKSPACE_SSH_ENDPOINTS


def test_endpoints_shared_by_several_machines_are_probed_once(root_concurrency_group: ConcurrencyGroup) -> None:
    """The agents on one host share its endpoint; probing it repeatedly measures nothing extra."""
    detector, prober = build_stub_connectivity_detector(
        root_concurrency_group,
        is_internet_up=True,
        is_ssh_up=True,
        workspace_ssh_endpoints=(_WORKSPACE_ENDPOINT, _WORKSPACE_ENDPOINT, _WORKSPACE_ENDPOINT),
    )

    detector.probe_now()

    assert [call for call in prober.probed_endpoints if call.startswith("ssh://box")] == [
        f"ssh://{_WORKSPACE_ENDPOINT.host}:22131"
    ]


def _no_wake() -> None:
    """Placeholder for :class:`_WakingProber` before the detector it wakes exists."""


def _build_waking_detector(
    concurrency_group: ConcurrencyGroup, *, is_network_up: bool
) -> tuple[ConnectivityDetector, SideEffectingStubNetworkProber]:
    """A detector whose next armed probe is interrupted by a wake.

    The lid closing mid-probe, without the timing: the probe thread is frozen
    with its connections already made, the heartbeat thread records the gap on
    resume, and only then does the probe finish and try to store what it found.
    Built disarmed, since each test arms the probe it wants interrupted.
    """
    prober = SideEffectingStubNetworkProber(
        on_first_question=_no_wake,
        is_armed=False,
        reachable_hosts=set(STUB_CONNECTIVITY_HOSTS) if is_network_up else set(),
        ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS) if is_network_up else set(),
    )
    detector = build_connectivity_detector_over(prober, concurrency_group)
    prober.on_first_question = lambda: detector.invalidate_after_wake(_T0)
    return detector, prober


@pytest.mark.witnesses(
    "no-verdict-on-unobserved-time",
    partial="witnesses the device's own connectivity reading only",
)
def test_a_reading_measured_across_a_wake_is_dropped_rather_than_stored(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The network it measured is the one the laptop went to sleep on.

    Stored, it would undo the invalidation the wake just made -- and it carries
    a post-wake timestamp, so it would look fresh enough for a gate arriving in
    the next couple of seconds to reuse.
    """
    detector, prober = _build_waking_detector(root_concurrency_group, is_network_up=True)
    prober.is_armed = True

    detector.probe_now()

    reading = detector.get_reading()
    assert reading.internet is ConnectivityFacet.UNKNOWN
    assert reading.ssh is ConnectivityFacet.UNKNOWN
    assert reading.observed_at is None


@pytest.mark.parametrize("is_network_up", [True, False])
def test_a_probe_interrupted_by_a_wake_answers_its_caller_unknown(
    is_network_up: bool, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The caller must not act on what the detector itself refused to adopt.

    The gate reads the block straight off this return value, and it is the only
    reader whose decision it drives. A dropped OFFLINE handed back would have it
    withhold a start and record it as owed -- against a detector that stored
    nothing, so is watching for nothing, so never releases it, and against a
    machine that is already STUCK and cannot fire the edge a second time. A
    dropped ONLINE handed back would dispatch over a network nothing has looked
    at. UNKNOWN is the honest answer and the safe one: it suppresses nothing, so
    the gate does what it would do with no detector wired at all.
    """
    detector, prober = _build_waking_detector(root_concurrency_group, is_network_up=is_network_up)
    prober.is_armed = True

    reading = detector.probe_now()

    assert reading.internet is ConnectivityFacet.UNKNOWN
    assert reading.ssh is ConnectivityFacet.UNKNOWN
    assert reading.observed_at is None
    assert reading.environment_block is EnvironmentBlock.NONE
    assert reading == detector.get_reading()


def test_a_good_reading_measured_across_a_wake_does_not_claim_a_recovery(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Otherwise every owed restart is drained onto a network nothing has measured.

    The network really did come back -- before the lid closed. Firing the
    bad -> good edge on that dispatches every withheld start at once, and each
    one fails if the laptop woke somewhere with no network at all.
    """
    detector, prober = _build_waking_detector(root_concurrency_group, is_network_up=False)
    recoveries: list[int] = []
    detector.add_on_recovery_callback(lambda: recoveries.append(1))

    # A bad reading first, so there is something outstanding to recover from.
    detector.probe_now()
    assert detector.get_reading().environment_block is EnvironmentBlock.OFFLINE

    bring_stub_network_up(prober)
    prober.is_armed = True
    detector.probe_now()

    assert recoveries == []
    assert detector.get_reading().observed_at is None

    # The next probe, taken entirely on this side of the wake, is the one that
    # settles it -- and the detector is still watching, so it does get taken.
    detector.probe_now()

    assert recoveries == [1]


def _raise_a_mind_error() -> None:
    """Stand-in for a restart the dispatch could not claim."""
    raise MindError("the restart could not be claimed")


def test_a_failing_recovery_callback_takes_neither_the_others_nor_the_watcher_with_it(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The recovery callback dispatches restarts, and a restart can raise a MindError.

    That is a plain ClickException rather than a RuntimeError, so it escaped the
    fence -- and escaping, it kills the loop that is the only thing able to
    observe the network coming back, leaving every owed restart stranded for the
    life of the process.
    """
    detector, prober, recoveries = _make_detector(root_concurrency_group)
    survivors: list[int] = []
    detector.add_on_recovery_callback(_raise_a_mind_error)
    detector.add_on_recovery_callback(lambda: survivors.append(1))

    detector.probe_now()
    bring_stub_network_up(prober)
    detector.probe_now()

    assert recoveries == [1]
    assert survivors == [1]


# -- SocketNetworkProber, against real loopback listeners --
#
# The one part of this module that speaks to a socket, and the part every
# reading a user is ever shown comes out of. Driven against in-process
# listeners on 127.0.0.1 rather than a stub: what is under test *is* the socket
# handling, and a stub of it would only restate the implementation.

_SSH_BANNER: Final[bytes] = b"SSH-2.0-OpenSSH_9.6\r\n"
# Long enough that a per-recv budget would show up as seven of these, short
# enough that the whole file stays fast.
_PROBE_BUDGET_SECONDS: Final[float] = 0.5


@contextmanager
def _loopback_listener(
    chunks: tuple[bytes, ...], chunk_delay_seconds: float = 0.0, bind_host: str = "127.0.0.1"
) -> Iterator[int]:
    """Serve ``chunks`` to every connection on a loopback port, yielding the port.

    Each connection is handled on its own thread and then held open until
    teardown: a peer that hangs up is a different answer from one that goes
    quiet, and the quiet one is what the banner read's deadline exists for.

    ``bind_host`` is the loopback address to listen on, for the one test that
    needs the listener on a particular one of ``localhost``'s addresses.
    """
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.bind((bind_host, 0))
    listener.listen(8)
    port = int(listener.getsockname()[1])
    is_stopping = threading.Event()

    def handle(connection: socket.socket) -> None:
        with connection:
            for chunk in chunks:
                if chunk_delay_seconds > 0.0 and is_stopping.wait(timeout=chunk_delay_seconds):
                    return
                try:
                    connection.sendall(chunk)
                except OSError:
                    return
            is_stopping.wait(timeout=_PROBE_BUDGET_SECONDS * 20.0)

    def accept_forever() -> None:
        while not is_stopping.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    accepting = threading.Thread(target=accept_forever, daemon=True)
    accepting.start()
    try:
        yield port
    finally:
        is_stopping.set()
        # Shut down before closing: the accept thread cannot see is_stopping
        # until an accept returns, and on Linux closing a descriptor another
        # thread is blocked in accept() on does not return it -- so the join
        # below would run its whole timeout and leave the thread blocked for the
        # life of the worker. Shutting down does return it, through the
        # ``except OSError`` in accept_forever. macOS rejects a shutdown of a
        # listening socket with ENOTCONN, which is exactly the platform where
        # close() already returns the accept, so that answer is the no-op it
        # looks like rather than a failure to swallow.
        try:
            listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        listener.close()
        accepting.join(timeout=5.0)


def _closed_loopback_port() -> int:
    """A port nothing is listening on: bound to learn the number, then released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_real_ssh_banner_reads_as_an_ssh_server() -> None:
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)

    with _loopback_listener((_SSH_BANNER,)) as port:
        assert prober.is_reachable("127.0.0.1", port)
        assert prober.is_ssh_server("127.0.0.1", port)


def test_a_banner_arriving_one_byte_at_a_time_still_reads_as_an_ssh_server() -> None:
    """A short recv is not an answer, and treating it as one blames a working endpoint.

    The read also has to fit in one budget rather than one per recv: the prefix
    is seven bytes, and a peer dribbling just inside a per-recv timeout would
    otherwise hold the detector's probe lock -- and every gate queued on it --
    for seven times as long. A filtering middlebox, which is exactly what this
    facet exists to catch, is the likeliest thing to trickle.
    """
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)
    trickle = tuple(_SSH_BANNER[index : index + 1] for index in range(len(_SSH_BANNER)))

    with _loopback_listener(trickle, chunk_delay_seconds=_PROBE_BUDGET_SECONDS / 20.0) as port:
        started_at = time.monotonic()
        assert prober.is_ssh_server("127.0.0.1", port)
        # Comfortably under the seven budgets a per-recv timeout would spend,
        # with enough slack that a loaded machine cannot fail it on scheduling.
        assert time.monotonic() - started_at < _PROBE_BUDGET_SECONDS * 4.0


def test_a_port_that_answers_with_something_other_than_ssh_is_not_an_ssh_server() -> None:
    """The motivating case, and the whole reason the banner is read rather than the connect counted.

    A captive portal or filtering middlebox accepts the connection happily and
    then serves something that is not SSH. A connect alone reports that as a
    working SSH path -- so the port answering and the port speaking SSH have to
    come back as different answers about the same endpoint.
    """
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)

    with _loopback_listener((b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",)) as port:
        assert prober.is_reachable("127.0.0.1", port)
        assert not prober.is_ssh_server("127.0.0.1", port)


def test_a_port_that_accepts_and_says_nothing_is_not_an_ssh_server() -> None:
    """And gives up inside its budget rather than waiting on a peer that never speaks."""
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)

    with _loopback_listener(()) as port:
        started_at = time.monotonic()
        assert not prober.is_ssh_server("127.0.0.1", port)
        elapsed_seconds = time.monotonic() - started_at

    assert _PROBE_BUDGET_SECONDS <= elapsed_seconds < _PROBE_BUDGET_SECONDS * 6.0


def test_a_hostname_the_resolver_refuses_answers_neither_question() -> None:
    """A name that cannot even be encoded is this endpoint not answering, not an error.

    The endpoints come from discovery, so the prober is not handed constants:
    an over-long label makes getaddrinfo's idna codec raise a ValueError rather
    than an OSError, and letting that out would kill the detector's loop thread
    -- leaving the watch latched on with nothing left to lift it, and every
    owed start stranded behind it.
    """
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)
    unencodable_host = "a" * 70 + ".example.com"

    assert not prober.is_reachable(unencodable_host, 443)
    assert not prober.is_ssh_server(unencodable_host, 22)


def test_a_port_nothing_is_listening_on_answers_neither_question() -> None:
    """A refused connection is the answer, not an error: both methods report it as a failure."""
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)
    port = _closed_loopback_port()

    assert not prober.is_reachable("127.0.0.1", port)
    assert not prober.is_ssh_server("127.0.0.1", port)


def test_a_host_is_reached_on_whichever_of_its_addresses_answers() -> None:
    """The budget is spent per endpoint, so the walk over its addresses is ours to get right.

    ``socket.create_connection`` does that walk itself, and giving it up is what
    stops one multi-homed host from spending the budget once per address. What
    it would take with it, done wrong, is the fallback that makes a host
    reachable at all when the address its resolver hands back first is not the
    one answering -- an ordinary shape for minds' own endpoints on any machine
    with both address families configured.

    The listener is bound to the address ``localhost`` resolves to *last*, so
    reaching it means the walk got past the one that comes first.
    """
    loopback_addresses = tuple(str(info[4][0]) for info in socket.getaddrinfo("localhost", 0, 0, socket.SOCK_STREAM))
    if len(set(loopback_addresses)) < 2:
        pytest.skip("localhost resolves to one address here, so there is no fallback to exercise")
    prober = SocketNetworkProber(timeout_seconds=_PROBE_BUDGET_SECONDS)

    with _loopback_listener((_SSH_BANNER,), bind_host=loopback_addresses[-1]) as port:
        assert prober.is_reachable("localhost", port)
        assert prober.is_ssh_server("localhost", port)


@pytest.mark.parametrize("address_count", [1, 2, 3, 6])
def test_no_address_can_spend_the_budget_the_ones_behind_it_need(address_count: int) -> None:
    """The fallback the walk exists for is the address that goes silent, not the one that refuses.

    A refused address costs nothing, so the walk reaches the next one whatever
    the budget rule is -- which is all a local listener can stage. What no
    listener can stage is an address that drops the SYN, and that is the case
    the walk is for: a routable IPv6 address on a network that blackholes IPv6
    takes every second it is given. So the schedule is walked here with every
    address silent, which is the worst the rule has to hold under.

    Six addresses because that is ``bitbucket.org``, one of the quorum hosts
    this dials: three AAAA records ahead of three A records, which on a network
    that carries no IPv6 is five silent addresses in front of the one that
    answers.
    """
    budget_seconds = 1.5
    remaining_seconds = budget_seconds

    for index in range(address_count):
        attempt_seconds = _address_attempt_seconds(remaining_seconds, address_count - index)
        assert attempt_seconds > 0.0, "every address must be left something to dial on, or the walk stops short"
        # The silent case: this address spends the whole of what it was given.
        remaining_seconds -= attempt_seconds

    assert remaining_seconds >= 0.0, "and the whole walk must stay inside the endpoint's budget"
