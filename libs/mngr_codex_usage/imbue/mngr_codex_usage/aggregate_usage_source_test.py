"""Unit tests for the Codex ``aggregate_usage_source`` reader hookimpl."""

from __future__ import annotations

from typing import Any

import pytest

from imbue.mngr_codex_usage.plugin import aggregate_usage_source
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance

_NOW = 2_800_000_000
_SINCE = 10**12


def _codex_event(
    session_id: str, event_id: str, second: int, tokens: dict[str, Any], rate_limits: dict[str, Any] | None = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp": f"2056-01-01T00:00:{second:02d}.000000000Z",
        "session_id": session_id,
        "event_id": event_id,
        "cost": None,
        "tokens": tokens,
        "model": "openai/gpt-5.2-codex",
        "cost_mode": "SUBSCRIPTION" if rate_limits is not None else "API_KEY",
    }
    if rate_limits is not None:
        event["rate_limits"] = rate_limits
    return event


def test_codex_hookimpl_takes_freshest_cumulative_and_estimates_cost() -> None:
    # token_count is cumulative; the freshest reading is the session total, and
    # cost is estimated from tokens (Codex reports no dollar cost).
    snapshot = aggregate_usage_source(
        source_name="codex",
        agents_events={
            "agent-1": parse_usage_events(
                [
                    _codex_event(
                        "s1", "line-1-usage", 1, {"input": 100, "output": 0, "cache_read": 0, "cache_creation": None}
                    ),
                    _codex_event(
                        "s1",
                        "line-2-usage",
                        2,
                        {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_creation": None},
                    ),
                ],
                "codex",
            )
        },
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is not None
    record = snapshot.sessions[0]
    # Freshest = 1,000,000 input * gpt-5.2-codex input price 1.75e-6 = 1.75.
    assert record.cost.total_cost_usd == pytest.approx(1.75)
    assert record.cost_provenance == CostProvenance.ESTIMATED
    assert record.cost_mode == CostMode.API_KEY


def test_codex_hookimpl_surfaces_windows_and_subscription_mode() -> None:
    snapshot = aggregate_usage_source(
        source_name="codex",
        agents_events={
            "agent-1": parse_usage_events(
                [
                    _codex_event(
                        "s1",
                        "line-1-usage",
                        1,
                        {"input": 100, "output": 50, "cache_read": 200, "cache_creation": None},
                        rate_limits={
                            "five_hour": {
                                "used_percentage": 23.0,
                                "resets_at": 9_999_999_999,
                                "window_seconds": 18000,
                            },
                            "seven_day": {
                                "used_percentage": 27.0,
                                "resets_at": 9_999_999_999,
                                "window_seconds": 604800,
                            },
                        },
                    )
                ],
                "codex",
            )
        },
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is not None
    assert snapshot.sessions[0].cost_mode == CostMode.SUBSCRIPTION
    assert "five_hour" in snapshot.windows and "seven_day" in snapshot.windows


def test_codex_hookimpl_declines_non_codex_sources() -> None:
    snapshot = aggregate_usage_source(
        source_name="opencode",
        agents_events={
            "agent-1": parse_usage_events(
                [_codex_event("s1", "line-1-usage", 1, {"input": 1, "output": 1})], "opencode"
            )
        },
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is None
