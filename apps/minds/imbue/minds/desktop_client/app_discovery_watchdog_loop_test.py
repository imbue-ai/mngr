"""Unit coverage for the background loop that drives the discovery-health watchdog.

The watchdog's own state machine is covered in ``discovery_health_test.py`` by
calling ``evaluate`` directly. What is covered here is the loop body: what it
reads, and when. That matters for exactly one thing -- a laptop sleep, which
stops this loop and the sleep heartbeat's loop together and releases them both
at the same instant on resume.
"""

from datetime import datetime
from datetime import timezone

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _run_discovery_health_watchdog_loop
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.desktop_client.testing import ManualClock
from imbue.minds.desktop_client.testing import RecordingProducerRemediator
from imbue.mngr.utils.polling import poll_until

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_STALL_SECONDS = 35.0


def test_the_watchdog_loop_sees_a_sleep_it_is_released_from_alongside_the_heartbeat() -> None:
    """A tick that reads a pre-sleep wake would SIGHUP a producer that never stalled.

    Both loops block on a wall-clock deadline that expires *during* the sleep,
    so on resume they become runnable together with no ordering between them. If
    this loop asked the sleep tracker for a wake the heartbeat had not recorded
    yet, it would age an hours-old event from the watchdog's start, call that a
    stall, and bounce the producer -- which is the thing the sleep signal was
    added to prevent.
    """
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog = DiscoveryHealthWatchdog(remediator=remediator, stall_threshold_seconds=_STALL_SECONDS, now_fn=clock)
    sleep_tracker = SleepTracker(now_fn=clock)
    resolver = MngrCliBackendResolver()
    resolver.record_discovery_event_received(_T0)

    # The state a running app is in when the lid closes: the heartbeat has a
    # baseline tick, and the watchdog has seen a healthy pipeline.
    sleep_tracker.record_heartbeat()
    watchdog.evaluate(_T0, None)

    # Ten minutes of nothing running at all, ending now.
    clock.advance(600.0)

    with ConcurrencyGroup(name="test-discovery-watchdog-loop") as concurrency_group:
        concurrency_group.start_new_thread(
            target=_run_discovery_health_watchdog_loop,
            args=(watchdog, resolver, concurrency_group, sleep_tracker),
            name="test-discovery-health-watchdog",
        )
        assert poll_until(lambda: sleep_tracker.get_last_wake_at() is not None, timeout=5.0, poll_interval=0.02), (
            "the loop must establish the wake itself rather than trust the heartbeat to have landed"
        )
        concurrency_group.shutdown()

    assert remediator.calls == []
