"""Unit tests for the discovery-pipeline health watchdog state machine.

The watchdog is driven with a fake clock (so the backoff waits and the stall
threshold are deterministic) and a fake producer remediator (so the
bounce/restart remediations can be asserted without a
real supervisor). The background loop that calls ``evaluate`` in production is
exercised separately; here we call ``evaluate`` / ``record_consumer_death``
directly.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.testing import ManualClock
from imbue.minds.desktop_client.testing import RecordingProducerRemediator

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_STALL_SECONDS = 35.0
_REMEDIATION_WAIT_SECONDS = 15.0
_MAX_BACKOFF_SECONDS = 300.0


def _make_watchdog(
    clock: ManualClock, remediator: RecordingProducerRemediator
) -> tuple[DiscoveryHealthWatchdog, list[DiscoveryHealth]]:
    watchdog = DiscoveryHealthWatchdog(
        remediator=remediator,
        stall_threshold_seconds=_STALL_SECONDS,
        remediation_wait_seconds=_REMEDIATION_WAIT_SECONDS,
        max_remediation_backoff_seconds=_MAX_BACKOFF_SECONDS,
        now_fn=clock,
    )
    # On-change callbacks are no-arg (mirroring the resolver): record the tier
    # by re-reading it, which is what production consumers do.
    transitions: list[DiscoveryHealth] = []
    watchdog.add_on_change_callback(lambda: transitions.append(watchdog.get_health()))
    return watchdog, transitions


def test_fresh_event_stays_healthy() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []
    assert transitions == []


def test_fresh_event_stays_healthy_even_when_full_snapshot_is_stale() -> None:
    # The stall signal is the last discovery *event*, not the last full
    # snapshot: a producer still emitting incremental events (so ``last_event_at``
    # keeps advancing) is alive and must not be remediated, even if it has not
    # completed a full re-poll for a while. The loop passes ``last_event_at``, so
    # a fresh event here stands in for "events flowing, full snapshot stale".
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    clock.advance(120)
    # The event is fresh (now), regardless of how old the full snapshot is.
    watchdog.evaluate(clock())

    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []


def test_stall_enters_reconnecting_and_bounces_immediately() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the later stall ages from event time
    # rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    # Last event at T0; now is T0 + 40s -> aged past the 35s threshold.
    clock.advance(40)
    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_remediation_bounces_then_restarts_on_growing_backoff_forever() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the later stall ages from event time
    # rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    clock.advance(40)
    # First stalled evaluate enters RECONNECTING and bounces.
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce"]

    # A second evaluate before the first backoff (15s) elapses does nothing new.
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce"]

    # After the 15s base wait, the first restart fires.
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart"]

    # The next restart waits twice as long (30s): 15s later is not yet due.
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart"]

    # A further 15s (30s total since the last restart) crosses the doubled wait.
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart", "restart"]

    # It never gives up: stays RECONNECTING, never BLOCKED.
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_failed_restart_does_not_block_and_keeps_retrying() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator(fail_restart=True)
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the later stall ages from event time
    # rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    clock.advance(40)
    # First stalled evaluate enters RECONNECTING and bounces.
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce"]

    # The restart runs and raises. A failed restart is just another "did not
    # help" -- the watchdog stays RECONNECTING and keeps backing off, it does
    # NOT escalate to terminal BLOCKED.
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart"]
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING

    # The next backoff (30s) still drives another restart attempt.
    clock.advance(2 * _REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart", "restart"]
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_recovery_mid_remediation_returns_to_healthy_and_resets() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the later stall ages from event time
    # rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    clock.advance(40)
    # Enter RECONNECTING and bounce.
    watchdog.evaluate(_T0)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING

    # A fresh event (stamped at the current time) restores health and resets the
    # remediation bookkeeping (bounce + backoff counters).
    fresh = clock()
    watchdog.evaluate(fresh)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY

    # A subsequent stall starts remediation over from the cheap bounce.
    clock.advance(40)
    watchdog.evaluate(fresh)
    assert remediator.calls == ["bounce", "bounce"]
    assert transitions == [
        DiscoveryHealth.RECONNECTING,
        DiscoveryHealth.HEALTHY,
        DiscoveryHealth.RECONNECTING,
    ]


def test_consumer_death_blocks_immediately_without_remediation() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.record_consumer_death()

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert remediator.calls == []
    assert transitions == [DiscoveryHealth.BLOCKED]


def test_consumer_death_is_idempotent() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.record_consumer_death()
    watchdog.record_consumer_death()

    assert transitions == [DiscoveryHealth.BLOCKED]


def test_blocked_is_terminal_and_evaluate_is_a_no_op() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Force the terminal tier (consumer death), then a stale evaluate must not
    # move off it or run any remediation.
    watchdog.record_consumer_death()
    clock.advance(120)
    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert remediator.calls == []
    assert transitions == [DiscoveryHealth.BLOCKED]


def test_consumer_death_during_reconnecting_escalates_to_blocked() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the later stall ages from event time
    # rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    clock.advance(40)
    # Enter RECONNECTING (+ bounce), then a consumer death escalates to BLOCKED.
    watchdog.evaluate(_T0)
    watchdog.record_consumer_death()

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert transitions == [DiscoveryHealth.RECONNECTING, DiscoveryHealth.BLOCKED]


def test_cold_start_has_grace_then_stalls_when_no_first_event() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # No event has ever arrived. The first evaluate anchors the baseline and is
    # within the grace window, so it does not yet treat this as a stall.
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # Past the grace window with still no first event, the cold-start backstop
    # kicks off remediation.
    clock.advance(40)
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]


def test_replayed_pre_watchdog_events_get_cold_start_grace_instead_of_immediate_stall() -> None:
    # At startup the consumer replays the previous session's discovery file,
    # whose snapshots fold in with their original (hours-old) timestamps. A
    # tick sampled mid-replay must not read that as a stall -- the resulting
    # bounce once SIGHUP'd the still-booting supervisor to death. Timestamps
    # predating the watchdog age from its start, like the no-event cold start.
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    stale_replayed = _T0 - timedelta(hours=3)
    watchdog.evaluate(stale_replayed)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # Still within the grace window: the stale timestamp alone never stalls it.
    clock.advance(20)
    watchdog.evaluate(stale_replayed)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # Past the grace window with still nothing from this session, the
    # cold-start backstop takes over and remediation begins.
    clock.advance(20)
    watchdog.evaluate(stale_replayed)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_cold_start_that_never_recovers_keeps_retrying_without_blocking() -> None:
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # Anchor the baseline (healthy), then never deliver a first event: the
    # watchdog bounces, then restarts on backoff, and keeps going -- it never
    # reaches a terminal BLOCKED (only consumer death does that).
    watchdog.evaluate(None)
    clock.advance(40)
    # bounce
    watchdog.evaluate(None)
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    # first restart, after the 15s base wait
    watchdog.evaluate(None)
    clock.advance(2 * _REMEDIATION_WAIT_SECONDS)
    # second restart, after the doubled 30s wait
    watchdog.evaluate(None)

    assert remediator.calls == ["bounce", "restart", "restart"]
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING


def test_backoff_holds_at_cap_without_overflow_after_many_restarts() -> None:
    # The watchdog retries forever, so the restart counter grows without bound.
    # The backoff must hold at the cap and never overflow ``2.0 ** count`` (which
    # raises OverflowError at count 1024 -- it is uncaught and would kill the
    # watchdog thread). Drive the counter well past that threshold and assert the
    # backoff stays at the cap and a stalled evaluate keeps remediating cleanly.
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # Anchor a same-session event at T0 so the stalled evaluate below ages from
    # event time rather than being treated as a pre-watchdog replay artifact.
    watchdog.evaluate(_T0)

    watchdog._restart_count = 5000
    watchdog._bounce_attempted = True
    watchdog._last_remediation_at = clock()

    # The current backoff is the cap, computed without overflowing the power.
    assert watchdog._current_backoff_seconds() == _MAX_BACKOFF_SECONDS

    # Once the cap elapses, a still-stalled evaluate fires another restart rather
    # than raising; the counter advances and the backoff still holds at the cap.
    clock.advance(_MAX_BACKOFF_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["restart"]
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert watchdog._current_backoff_seconds() == _MAX_BACKOFF_SECONDS


def test_a_long_sleep_is_not_read_as_a_stalled_producer() -> None:
    """The first tick after the lid opens must not SIGHUP a producer that never stalled.

    Both loops were frozen for the sleep, so the producer's silence was never
    observed -- and the event it emitted before the machine went down describes
    a world nobody has looked at since.
    """
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.evaluate(_T0)

    # A ten-minute sleep, ending now: far past the stall threshold, with the
    # last event stamped before it began.
    clock.advance(600)
    woke_at = clock()
    watchdog.evaluate(_T0, woke_at)

    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []
    assert transitions == []


def test_a_producer_that_is_still_dead_after_the_wake_grace_is_remediated() -> None:
    """The wake buys a fresh grace period, not an exemption."""
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    watchdog.evaluate(_T0)
    clock.advance(600)
    woke_at = clock()
    watchdog.evaluate(_T0, woke_at)

    # A full stall threshold has now elapsed since the wake with nothing new
    # from the producer, which is a stall this session actually watched.
    clock.advance(_STALL_SECONDS + 5)
    watchdog.evaluate(_T0, woke_at)

    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]


def test_a_wake_restarts_the_remediation_episode_at_the_cheap_bounce() -> None:
    """A backoff whose waits elapsed while nothing was running has measured nothing.

    Without this the first post-wake stall would resume mid-escalation and issue
    a full supervisor restart -- re-provisioning every managed host -- for a
    producer that has not been given one cheap re-kick since the machine woke.
    """
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # A genuine stall that escalates past the bounce into the restart backoff.
    watchdog.evaluate(_T0)
    clock.advance(40)
    watchdog.evaluate(_T0)
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart"]

    # The machine sleeps, then wakes; the producer is still silent afterwards.
    clock.advance(600)
    woke_at = clock()
    watchdog.evaluate(_T0, woke_at)
    clock.advance(_STALL_SECONDS + 5)
    watchdog.evaluate(_T0, woke_at)

    assert remediator.calls == ["bounce", "restart", "bounce"]


def test_the_same_wake_reported_every_tick_resets_the_episode_only_once() -> None:
    """The loop re-reads the last wake each tick; only a newer one is an event."""
    clock = ManualClock(_T0)
    remediator = RecordingProducerRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    watchdog.evaluate(_T0)
    clock.advance(600)
    woke_at = clock()

    # Past the wake's own grace, the producer's continued silence escalates
    # normally even though every tick keeps reporting the same wake.
    clock.advance(_STALL_SECONDS + 5)
    watchdog.evaluate(_T0, woke_at)
    clock.advance(_REMEDIATION_WAIT_SECONDS)
    watchdog.evaluate(_T0, woke_at)

    assert remediator.calls == ["bounce", "restart"]
