"""Unit tests for the Claude ``aggregate_usage_source`` reader hookimpl."""

from __future__ import annotations

from typing import Any

from imbue.mngr_claude_usage.plugin import aggregate_usage_source
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance

_NOW = 2_800_000_000
_SINCE = 10**12


def _claude_event(session_id: str, second: int, total_cost_usd: float) -> dict[str, Any]:
    return {
        "timestamp": f"2056-01-01T00:00:{second:02d}.000000000Z",
        "session_id": session_id,
        "event_id": f"evt-{session_id}-{second}",
        "cost": {"total_cost_usd": total_cost_usd},
    }


def test_claude_hookimpl_aggregates_the_claude_source_as_reported() -> None:
    snapshot = aggregate_usage_source(
        source_name="claude",
        agents_events={"agent-1": parse_usage_events([_claude_event("s1", 1, 2.5)], "claude")},
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is not None
    assert snapshot.source_name == "claude"
    record = snapshot.sessions[0]
    assert record.cost.total_cost_usd == 2.5
    assert record.cost_provenance == CostProvenance.REPORTED
    assert record.cost_mode == CostMode.API_KEY


def test_claude_hookimpl_declines_non_claude_sources() -> None:
    # Returning None lets the firstresult hook fall through to another plugin
    # (or the dispatcher's process-cumulative fallback).
    snapshot = aggregate_usage_source(
        source_name="codex",
        agents_events={"agent-1": parse_usage_events([_claude_event("s1", 1, 2.5)], "codex")},
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is None
