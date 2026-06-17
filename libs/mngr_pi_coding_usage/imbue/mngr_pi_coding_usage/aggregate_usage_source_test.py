"""Unit tests for the pi ``aggregate_usage_source`` reader hookimpl."""

from __future__ import annotations

from typing import Any

from imbue.mngr_pi_coding_usage.plugin import aggregate_usage_source
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance

_NOW = 2_800_000_000
_SINCE = 10**12


def _message_event(session_id: str, event_id: str, second: int, total_cost_usd: float) -> dict[str, Any]:
    return {
        "timestamp": f"2056-01-01T00:00:{second:02d}.000000000Z",
        "session_id": session_id,
        "event_id": event_id,
        "cost": {"total_cost_usd": total_cost_usd},
        "model": "anthropic/claude-opus-4-8",
    }


def test_pi_hookimpl_sums_per_message_as_reported() -> None:
    snapshot = aggregate_usage_source(
        source_name="pi-coding",
        agents_events={
            "agent-1": parse_usage_events(
                [
                    _message_event("s1", "evt-pi-usage-0", 1, 0.01),
                    _message_event("s1", "evt-pi-usage-1", 2, 0.02),
                ],
                "pi-coding",
            )
        },
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is not None
    assert snapshot.source_name == "pi-coding"
    record = snapshot.sessions[0]
    assert record.cost.total_cost_usd == 0.03
    assert record.cost_provenance == CostProvenance.REPORTED
    assert record.cost_mode == CostMode.API_KEY


def test_pi_hookimpl_declines_non_pi_sources() -> None:
    snapshot = aggregate_usage_source(
        source_name="opencode",
        agents_events={"agent-1": parse_usage_events([_message_event("s1", "evt-pi-usage-0", 1, 0.01)], "opencode")},
        since_seconds=_SINCE,
        now=_NOW,
    )
    assert snapshot is None
