"""Unit tests for ``aggregate_session_cumulative`` -- the strategy for harnesses
whose ``session_id`` is its own cost/token counter (Codex, OpenCode, pi)."""

from __future__ import annotations

from typing import Any

import pytest

from imbue.mngr_usage.api import aggregate_session_cumulative
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance

# Far-future timestamps + a huge recency window so nothing is filtered by age.
_NOW = 2_800_000_000
_SINCE = 10**12


def _ts(second: int) -> str:
    return f"2056-01-01T00:00:{second:02d}.000000000Z"


def _event(
    session_id: str,
    *,
    second: int,
    cost: dict[str, Any] | None = None,
    tokens: dict[str, Any] | None = None,
    model: str | None = None,
    rate_limits: dict[str, Any] | None = None,
    cost_mode: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp": _ts(second),
        "session_id": session_id,
        "event_id": f"evt-{session_id}-{second}",
    }
    if cost is not None:
        event["cost"] = cost
    if tokens is not None:
        event["tokens"] = tokens
    if model is not None:
        event["model"] = model
    if rate_limits is not None:
        event["rate_limits"] = rate_limits
    if cost_mode is not None:
        event["cost_mode"] = cost_mode
    return event


def _aggregate(events: list[dict[str, Any]]):
    return aggregate_session_cumulative(
        "codex", {"agent-1": parse_usage_events(events, "codex")}, since_seconds=_SINCE, now=_NOW
    )


def test_reported_cost_is_used_verbatim_with_reported_provenance() -> None:
    snapshot = _aggregate([_event("s1", second=1, cost={"total_cost_usd": 4.25})])
    assert snapshot is not None
    record = snapshot.sessions[0]
    assert record.cost.total_cost_usd == pytest.approx(4.25)
    assert record.cost_provenance == CostProvenance.REPORTED
    assert record.cost_mode == CostMode.API_KEY


def test_token_only_event_is_estimated_via_pricing_and_carries_tokens() -> None:
    snapshot = _aggregate(
        [
            _event(
                "s1",
                second=1,
                tokens={"input": 2, "output": 7, "cache_read": 9133, "cache_creation": 21},
                model="anthropic/claude-opus-4-8",
            )
        ]
    )
    assert snapshot is not None
    record = snapshot.sessions[0]
    assert record.cost.total_cost_usd == pytest.approx(0.00488275)
    assert record.cost_provenance == CostProvenance.ESTIMATED
    assert record.tokens is not None and record.tokens.cache_read == 9133
    assert record.model == "anthropic/claude-opus-4-8"


def test_each_session_is_its_own_counter_no_cross_session_delta() -> None:
    # Two sessions with increasing cumulative cost (8.0 > 5.0, no drop). The
    # process-cumulative strategy would delta the second to 3.0; the
    # session-cumulative strategy must keep each session's own freshest reading.
    snapshot = _aggregate(
        [
            _event("session-a", second=1, cost={"total_cost_usd": 5.0}),
            _event("session-b", second=2, cost={"total_cost_usd": 8.0}),
        ]
    )
    assert snapshot is not None
    cost_by_session = {r.session_id: r.cost.total_cost_usd for r in snapshot.sessions}
    assert cost_by_session == {"session-a": pytest.approx(5.0), "session-b": pytest.approx(8.0)}
    # Both feed the api_cost aggregate, summing to the true 13.0 (not a delta'd 8.0).
    assert snapshot.api_cost.total_cost_usd == pytest.approx(13.0)


def test_freshest_reading_per_session_wins_across_multiple_events() -> None:
    snapshot = _aggregate(
        [
            _event("s1", second=1, cost={"total_cost_usd": 1.0}),
            _event("s1", second=3, cost={"total_cost_usd": 9.0}),
            _event("s1", second=2, cost={"total_cost_usd": 4.0}),
        ]
    )
    assert snapshot is not None
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].cost.total_cost_usd == pytest.approx(9.0)


def test_writer_cost_mode_hint_overrides_inference() -> None:
    snapshot = _aggregate([_event("s1", second=1, cost={"total_cost_usd": 1.0}, cost_mode="SUBSCRIPTION")])
    assert snapshot is not None
    assert snapshot.sessions[0].cost_mode == CostMode.SUBSCRIPTION


def test_rate_limits_presence_infers_subscription_and_surfaces_window() -> None:
    snapshot = _aggregate(
        [
            _event(
                "s1",
                second=1,
                cost={"total_cost_usd": 1.0},
                rate_limits={"five_hour": {"used_percentage": 40.0, "resets_at": 9_999_999_999}},
            )
        ]
    )
    assert snapshot is not None
    assert snapshot.sessions[0].cost_mode == CostMode.SUBSCRIPTION
    assert "five_hour" in snapshot.windows


def test_unpriced_model_yields_no_estimate_but_keeps_tokens() -> None:
    snapshot = _aggregate(
        [_event("s1", second=1, tokens={"input": 100, "output": 50}, model="openai/gpt-not-in-table")]
    )
    assert snapshot is not None
    record = snapshot.sessions[0]
    assert record.cost.total_cost_usd is None
    assert record.cost_provenance == CostProvenance.ESTIMATED
    assert record.tokens is not None and record.tokens.input == 100


def test_windows_only_session_surfaces_window_without_a_session_record() -> None:
    snapshot = _aggregate(
        [_event("s1", second=1, rate_limits={"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999}})]
    )
    assert snapshot is not None
    assert snapshot.sessions == ()
    assert "five_hour" in snapshot.windows
