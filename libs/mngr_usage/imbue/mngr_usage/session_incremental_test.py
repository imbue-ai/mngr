"""Unit tests for ``aggregate_session_incremental`` -- the strategy for harnesses
that report cost/tokens per message (OpenCode, pi), summed per session."""

from __future__ import annotations

from typing import Any

import pytest

from imbue.mngr_usage.api import aggregate_session_incremental
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import CostProvenance

_NOW = 2_800_000_000
_SINCE = 10**12


def _ts(second: int) -> str:
    return f"2056-01-01T00:00:{second:02d}.000000000Z"


def _event(
    session_id: str,
    *,
    second: int,
    message_id: str | None = None,
    event_id: str | None = None,
    cost: dict[str, Any] | None = None,
    tokens: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp": _ts(second),
        "session_id": session_id,
        "event_id": event_id if event_id is not None else f"evt-{session_id}-{second}",
    }
    if message_id is not None:
        event["message_id"] = message_id
    if cost is not None:
        event["cost"] = cost
    if tokens is not None:
        event["tokens"] = tokens
    if model is not None:
        event["model"] = model
    return event


def _aggregate(events: list[dict[str, Any]]):
    return aggregate_session_incremental(
        "opencode", {"agent-1": parse_usage_events(events, "opencode")}, since_seconds=_SINCE, now=_NOW
    )


def test_session_total_is_the_sum_over_its_messages() -> None:
    snapshot = _aggregate(
        [
            _event("s1", second=1, message_id="m1", cost={"total_cost_usd": 1.0}),
            _event("s1", second=2, message_id="m2", cost={"total_cost_usd": 2.0}),
        ]
    )
    assert snapshot is not None
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].cost.total_cost_usd == pytest.approx(3.0)
    assert snapshot.sessions[0].cost_provenance == CostProvenance.REPORTED


def test_streaming_re_fires_of_one_message_collapse_to_its_freshest() -> None:
    # The same message_id updated twice (cost grows 1.0 -> 3.0) must contribute
    # 3.0, not 4.0 -- the freshest event per message wins, then messages sum.
    snapshot = _aggregate(
        [
            _event("s1", second=1, message_id="m1", cost={"total_cost_usd": 1.0}),
            _event("s1", second=2, message_id="m1", cost={"total_cost_usd": 3.0}),
            _event("s1", second=3, message_id="m2", cost={"total_cost_usd": 0.5}),
        ]
    )
    assert snapshot is not None
    assert snapshot.sessions[0].cost.total_cost_usd == pytest.approx(3.5)


def test_token_only_messages_are_estimated_and_summed() -> None:
    snapshot = _aggregate(
        [
            _event(
                "s1", second=1, message_id="m1", tokens={"input": 1000, "output": 0}, model="anthropic/claude-opus-4-8"
            ),
            _event(
                "s1", second=2, message_id="m2", tokens={"input": 0, "output": 1000}, model="anthropic/claude-opus-4-8"
            ),
        ]
    )
    assert snapshot is not None
    record = snapshot.sessions[0]
    # 1000 input * 5e-6 + 1000 output * 2.5e-5 = 0.005 + 0.025 = 0.030
    assert record.cost.total_cost_usd == pytest.approx(0.030)
    assert record.cost_provenance == CostProvenance.ESTIMATED
    assert record.tokens is not None
    assert record.tokens.input == 1000 and record.tokens.output == 1000


def test_distinct_sessions_are_independent() -> None:
    snapshot = _aggregate(
        [
            _event("sa", second=1, message_id="m1", cost={"total_cost_usd": 4.0}),
            _event("sb", second=2, message_id="m1", cost={"total_cost_usd": 9.0}),
        ]
    )
    assert snapshot is not None
    cost_by_session = {r.session_id: r.cost.total_cost_usd for r in snapshot.sessions}
    assert cost_by_session == {"sa": pytest.approx(4.0), "sb": pytest.approx(9.0)}


def test_events_without_message_id_fall_back_to_event_id_per_message() -> None:
    # No message_id -> each distinct event_id is its own message, so two events sum.
    snapshot = _aggregate(
        [
            _event("s1", second=1, event_id="e1", cost={"total_cost_usd": 1.5}),
            _event("s1", second=2, event_id="e2", cost={"total_cost_usd": 2.5}),
        ]
    )
    assert snapshot is not None
    assert snapshot.sessions[0].cost.total_cost_usd == pytest.approx(4.0)
