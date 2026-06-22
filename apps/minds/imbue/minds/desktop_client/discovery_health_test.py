"""Unit tests for the discovery-pipeline health watchdog state machine.

The watchdog is driven with a fake clock (so the inter-rung waits and the
stall threshold are deterministic) and a fake producer remediator (so the
bounce/restart ladder can be asserted without a real supervisor). The
background loop that calls ``evaluate`` in production is exercised separately;
here we call ``evaluate`` / ``record_consumer_death`` directly.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import Field

from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.discovery_health import ProducerRemediator

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_STALL_SECONDS = 35.0
_RUNG_WAIT_SECONDS = 15.0


class _Clock:
    """A manually-advanced UTC clock used as the watchdog's ``now_fn``."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _FakeRemediator(ProducerRemediator):
    """Records ladder calls instead of touching a real supervisor."""

    calls: list[str] = Field(default_factory=list)

    def bounce(self) -> None:
        self.calls.append("bounce")

    def restart(self) -> None:
        self.calls.append("restart")


def _make_watchdog(
    clock: _Clock, remediator: _FakeRemediator
) -> tuple[DiscoveryHealthWatchdog, list[DiscoveryHealth]]:
    watchdog = DiscoveryHealthWatchdog(
        remediator=remediator,
        stall_threshold_seconds=_STALL_SECONDS,
        rung_wait_seconds=_RUNG_WAIT_SECONDS,
        now_fn=clock,
    )
    # On-change callbacks are no-arg (mirroring the resolver): record the tier
    # by re-reading it, which is what production consumers do.
    transitions: list[DiscoveryHealth] = []
    watchdog.add_on_change_callback(lambda: transitions.append(watchdog.get_health()))
    return watchdog, transitions


def test_fresh_snapshot_stays_healthy() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []
    assert transitions == []


def test_stall_enters_reconnecting_and_bounces_immediately() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    # Snapshot stamped at T0; now is T0 + 40s -> aged past the 35s threshold.
    clock.advance(40)
    watchdog.evaluate(_T0)

    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]
    assert transitions == [DiscoveryHealth.RECONNECTING]


def test_ladder_escalates_bounce_then_restart_then_blocked() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    clock.advance(40)
    # First stalled evaluate enters RECONNECTING and bounces.
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce"]

    # A second evaluate before the inter-rung wait elapses does nothing new.
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce"]

    # After the rung wait, the next rung is the heavier restart.
    clock.advance(_RUNG_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert remediator.calls == ["bounce", "restart"]
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING

    # After another rung wait with still no freshness, the watchdog gives up.
    clock.advance(_RUNG_WAIT_SECONDS)
    watchdog.evaluate(_T0)
    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert remediator.calls == ["bounce", "restart"]
    assert transitions == [DiscoveryHealth.RECONNECTING, DiscoveryHealth.BLOCKED]


def test_recovery_mid_ladder_returns_to_healthy_and_resets_ladder() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, transitions = _make_watchdog(clock, remediator)

    clock.advance(40)
    # Enter RECONNECTING and bounce.
    watchdog.evaluate(_T0)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING

    # A fresh snapshot (stamped at the current time) restores health and resets
    # the ladder bookkeeping.
    fresh = clock()
    watchdog.evaluate(fresh)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY

    # A subsequent stall starts the ladder over from the cheap bounce.
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

    # Force the terminal tier, then a stale evaluate must not move off it.
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


def test_cold_start_has_grace_then_stalls_when_no_first_snapshot() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # No snapshot has ever arrived. The first evaluate anchors the baseline and
    # is within the grace window, so it does not yet treat this as a stall.
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.HEALTHY
    assert remediator.calls == []

    # Past the grace window with still no first snapshot, the cold-start
    # backstop kicks the ladder.
    clock.advance(40)
    watchdog.evaluate(None)
    assert watchdog.get_health() is DiscoveryHealth.RECONNECTING
    assert remediator.calls == ["bounce"]


def test_cold_start_that_never_recovers_reaches_blocked() -> None:
    clock = _Clock(_T0)
    remediator = _FakeRemediator()
    watchdog, _transitions = _make_watchdog(clock, remediator)

    # Anchor the baseline (healthy), then never deliver a first snapshot: the
    # ladder runs to exhaustion and the watchdog blocks.
    watchdog.evaluate(None)
    clock.advance(40)
    watchdog.evaluate(None)
    clock.advance(_RUNG_WAIT_SECONDS)
    watchdog.evaluate(None)
    clock.advance(_RUNG_WAIT_SECONDS)
    watchdog.evaluate(None)

    assert watchdog.get_health() is DiscoveryHealth.BLOCKED
    assert remediator.calls == ["bounce", "restart"]
