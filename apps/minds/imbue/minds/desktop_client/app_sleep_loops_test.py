"""Unit coverage for the ``app.py`` loops that produce and consume the sleep signal.

The heartbeat loop, whose ticks *are* the detector and which nothing else
tests; and the system-interface health probe loop, which must establish the
wake for itself before it ages any failure run against it. The discovery
watchdog's own version of that second concern lives in
``app_discovery_watchdog_loop_test.py`` -- the two loops share the hazard
because they share the shape: both were frozen by the same sleep and both are
released from it at the same instant.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _run_system_interface_health_probe_loop
from imbue.minds.desktop_client.app import start_sleep_heartbeat_loop
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import make_sleep_tracker
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until

# Short enough that a probe-failure run reaches it inside a test, long enough
# that the restarted run does not reach it again before the assertions run.
_FAST_THRESHOLD: float = 0.05


class _OneLaggedReading:
    """Reports its first reading fifteen minutes in the past, then real time.

    The lid closing before a loop's first tick and opening before its second,
    without the wait: the gap the loop measures between its own two readings is
    the sleep. Phrased as a property of the clock rather than something the test
    thread toggles, because the loop's ticks are not ordered against it.
    """

    def __init__(self) -> None:
        self._is_first_reading = True

    def __call__(self) -> datetime:
        if self._is_first_reading:
            self._is_first_reading = False
            return datetime.now(timezone.utc) - timedelta(seconds=900.0)
        return datetime.now(timezone.utc)


def test_the_heartbeat_loop_drives_the_tracker() -> None:
    """The whole sleep signal rests on this loop actually ticking.

    Every consumer reads a tracker that records nothing on its own, so a loop
    that never ran -- or that ran and never reached ``record_heartbeat`` --
    leaves each of them answering "no sleep known" forever, silently and
    exactly as they behave without the feature.
    """
    sleep_tracker = SleepTracker(now_fn=_OneLaggedReading())
    wakes: list[datetime] = []
    sleep_tracker.add_on_wake_callback(wakes.append)

    with ConcurrencyGroup(name="test-sleep-heartbeat-loop") as concurrency_group:
        start_sleep_heartbeat_loop(sleep_tracker, concurrency_group)
        assert poll_until(lambda: len(wakes) == 1, timeout=5.0, poll_interval=0.02), (
            "the heartbeat loop never recorded the gap between two of its own ticks"
        )
        concurrency_group.shutdown()

    assert sleep_tracker.get_last_wake_at() == wakes[0]


def test_the_probe_loop_establishes_the_wake_before_it_convicts_anything() -> None:
    """The first post-wake pass must not convict a run of the seconds nobody watched.

    This loop is the only authority on STUCK, and it is released from the sleep
    at the same instant as the heartbeat loop with no ordering between them. A
    pass that lands first reports every probe target as failing -- they really
    are unreachable for the moment -- and ages each failure against a monotonic
    clock that barely moved while the machine slept, so a run opened seconds
    before the lid closed convicts immediately. Nothing takes that back: a
    later heartbeat cannot reach a record that is no longer HEALTHY.
    """
    sleep_tracker, clock = make_sleep_tracker()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD, sleep_tracker=sleep_tracker)
    agent_id = AgentId.generate()
    # An agent discovery reports nothing for is the probe loop's no-I/O failure
    # path, which is all this needs: what is under test is when the loop reads
    # the wake, not what it learns from the network.
    resolver = MngrCliBackendResolver()

    # The state a running app is in when the lid closes: a heartbeat baseline,
    # and a probe-failure run old enough that the next failure would convict.
    clock.lag_seconds = 900.0
    sleep_tracker.record_heartbeat()
    clock.lag_seconds = 0.0
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    run_onset_before = tracker.get_failure_run_started_wall_at(agent_id)
    assert run_onset_before is not None
    assert poll_until(
        lambda: (datetime.now(timezone.utc) - run_onset_before).total_seconds() > _FAST_THRESHOLD,
        timeout=5.0,
        poll_interval=0.01,
    )

    with ConcurrencyGroup(name="test-health-probe-loop") as concurrency_group:
        concurrency_group.start_new_thread(
            target=_run_system_interface_health_probe_loop,
            args=(tracker, resolver, 1, "test-cookie", concurrency_group, sleep_tracker),
            name="test-system-interface-health-probe",
        )
        assert poll_until(
            lambda: tracker.get_health(agent_id) is AgentHealth.STUCK
            or tracker.get_failure_run_started_wall_at(agent_id) != run_onset_before,
            timeout=5.0,
            poll_interval=0.02,
        ), "the probe loop never reported a failure for the enrolled agent"
        concurrency_group.shutdown()

    # The onset, not the health: the restarted run is only _FAST_THRESHOLD long,
    # so the loop's next pass convicts it quite correctly, and asserting HEALTHY
    # would be asserting that this test finished inside one probe interval. A
    # conviction of the *pre-sleep* run is what must not have happened, and that
    # one leaves the onset exactly where it was -- the conviction path does not
    # move it.
    assert tracker.get_failure_run_started_wall_at(agent_id) != run_onset_before, (
        "the run that spans the sleep must be restarted at the wake, not convicted on it"
    )
    assert sleep_tracker.get_last_wake_at() is not None
