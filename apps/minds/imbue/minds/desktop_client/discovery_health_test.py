"""Unit tests for the discovery-pipeline health watchdog state machine.

The watchdog is driven with a fake clock (so the backoff waits and the stall
threshold are deterministic) and a fake producer remediator (so the
bounce/restart remediations and the liveness probe can be asserted without a
real supervisor). The background loop that calls ``evaluate`` in production is
exercised separately; here we call ``evaluate`` / ``record_consumer_death``
directly.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import Field

from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.discovery_health import ProducerRemediator
from imbue.mngr_latchkey.core import LatchkeyError

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_STALL_SECONDS = 35.0
_REMEDIATION_WAIT_SECONDS = 15.0
_MAX_BACKOFF_SECONDS = 300.0


class _Clock:
    """A manually-advanced UTC clock used as the watchdog's ``now_fn``."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _FakeRemediator(ProducerRemediator):
    """Records remediation calls instead of touching a real supervisor.

    ``alive`` is what :meth:`is_alive` reports (a dead supervisor is a stall the
    watchdog acts on at once). ``fail_restart``, when True, makes ``restart``
    raise after recording the call -- mirroring a real supervisor restart that
    fails, which the watchdog must treat as "did not help" (retry), not give up.
    """

    calls: list[str] = Field(default_factory=list)
    alive: bool = Field(default=True)
    fail_restart: bool = Field(default=False)

    def bounce(self) -> None:
        self.calls.append("bounce")

    def restart(self) -> None:
        self.calls.append("restart")
        if self.fail_restart:
            raise LatchkeyError("simulated supervisor restart failure")

    def is_alive(self) -> bool:
        return self.alive


def _make_watchdog(
    clock: _Clock, remediator: _FakeRemediator
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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    clock.advance(120)
    # The event is fresh (now), regardless of how old the full snapshot is.
    watchdog.evaluate(clock())

    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []


def test_stall_enters_reconnecting_and_bounces_immediately() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Last event at T0; now is T0 + 40s -> aged past the 35s threshold.
    clock.advance(40)
    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_dead_supervisor_triggers_immediate_remediation_once_seen_alive() -> None:
    # Once the supervisor has been seen up, a later crash is a stall regardless
    # of event freshness: the watchdog remediates at once rather than waiting out
    # the 35s threshold.
    clock = _Clock(_T0)
    remediator = _FakeRemediator(alive=True)
    watchdog, transitions = _make_watchdog(clock, remediator)

    # First tick: supervisor up + fresh event -> healthy, and "seen alive" latches.
    watchdog.evaluate(_T0)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # Supervisor crashes; even with a still-fresh event, remediate immediately.
    remediator.alive = False
    watchdog.evaluate(clock())
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_dead_supervisor_during_startup_defers_to_freshness_grace() -> None:
    # During startup the supervisor is still being brought up on a background
    # thread, so is_alive() can read False before its on-disk record is written.
    # The watchdog must NOT treat that as an immediate stall (which would race
    # the startup thread and risk a duplicate spawn): until the supervisor is
    # first seen up, only the freshness timer governs.
    clock = _Clock(_T0)
    remediator = _FakeRemediator(alive=False)
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # No event yet and the supervisor not yet up, but within the cold-start
    # grace: stays healthy, no remediation despite is_alive() being False.
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # It comes up before the grace expires: the latch arms and all stays calm.
    remediator.alive = True
    clock.advance(5)
    watchdog.evaluate(clock())
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []


def test_supervisor_that_never_comes_up_still_remediates_via_freshness_grace() -> None:
    # Even if the supervisor is never seen alive (so the liveness trigger stays
    # suppressed), a producer delivering no events past the cold-start grace is
    # still remediated -- by the freshness backstop, not the liveness trigger.
    clock = _Clock(_T0)
    remediator = _FakeRemediator(alive=False)
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # Anchor the baseline, within grace.
    watchdog.evaluate(None)
    assert remediator.calls == []
    clock.advance(40)
    # Past grace -> the freshness backstop kicks off remediation.
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]


def test_remediation_bounces_then_restarts_on_growing_backoff_forever() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

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
    clock = _Clock(_T0)
    remediator = _FakeRemediator(fail_restart=True)
    watchdog, transitions = _make_watchdog(clock, remediator)

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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.record_consumer_death()

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert remediator.calls == []
    assert transitions == [DiscoveryHealth.BLOCKED]


def test_consumer_death_is_idempotent() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.record_consumer_death()
    watchdog.record_consumer_death()

    assert transitions == [DiscoveryHealth.BLOCKED]


def test_blocked_is_terminal_and_evaluate_is_a_no_op() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    clock.advance(40)
    # Enter RECONNECTING (+ bounce), then a consumer death escalates to BLOCKED.
    watchdog.evaluate(_T0)
    watchdog.record_consumer_death()

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert transitions == [DiscoveryHealth.RECONNECTING, DiscoveryHealth.BLOCKED]


def test_cold_start_has_grace_then_stalls_when_no_first_event() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
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


def test_cold_start_that_never_recovers_keeps_retrying_without_blocking() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
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
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

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
