"""Unit tests for mngr_usage.cli (agent-agnostic CLI + walk-by-convention discovery)."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pluggy
import pytest
from click.testing import CliRunner
from loguru import logger

from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr_usage.api import aggregate_process_cumulative
from imbue.mngr_usage.api import parse_events_from_content
from imbue.mngr_usage.api import parse_usage_events
from imbue.mngr_usage.cli import _build_render_model
from imbue.mngr_usage.cli import _flatten_primary_for_template
from imbue.mngr_usage.cli import _format_cost_line
from imbue.mngr_usage.cli import _format_duration
from imbue.mngr_usage.cli import _format_human_line
from imbue.mngr_usage.cli import _format_reset_phrase
from imbue.mngr_usage.cli import _format_session_detail_line
from imbue.mngr_usage.cli import _parse_optional_duration
from imbue.mngr_usage.cli import _session_mode_tag
from imbue.mngr_usage.cli import _write_source_section
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance
from imbue.mngr_usage.data_types import CostSnapshot
from imbue.mngr_usage.data_types import SessionCostRecord
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot


def aggregate_events_to_snapshots(
    events_by_source: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    since_seconds: int,
    now: int,
) -> list[UsageSnapshot]:
    """Test helper: build a snapshot per source via the process-cumulative strategy.

    These rendering tests all use the Claude source, whose production aggregation
    is ``aggregate_process_cumulative`` (also the dispatcher's fallback). Calling
    it directly keeps the rendering assertions focused; the reader-hook dispatch
    itself is covered by the per-plugin hookimpl tests and the integration tests
    in this file that invoke the CLI with a real plugin manager.
    """
    snapshots: list[UsageSnapshot] = []
    for source_name, agents_events in events_by_source.items():
        parsed_by_agent = {
            agent_id: parse_usage_events(events, source_name) for agent_id, events in agents_events.items()
        }
        snapshot = aggregate_process_cumulative(source_name, parsed_by_agent, since_seconds=since_seconds, now=now)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def _write_event(events_file: Path, event: dict[str, Any]) -> None:
    """Append a JSONL event line to ``events_file``, creating parents as needed."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _make_event(
    timestamp: str,
    used_percentage: float | None = 11.0,
    resets_at: int | None = 1778280000,
) -> dict:
    """Construct an event matching the writer's emitted shape.

    Includes a placeholder ``session_id`` since the reader contract
    requires every event to carry one (see ``_build_snapshot_for_source``).
    """
    return {
        "source": "claude/usage",
        "type": "cost_snapshot",
        "event_id": "evt-test123",
        "timestamp": timestamp,
        "session_id": "make-event-session",
        "rate_limits": {
            "five_hour": {"used_percentage": used_percentage, "resets_at": resets_at},
        },
    }


# =============================================================================
# Pure helpers
# =============================================================================


def test_parse_optional_duration_accepts_units() -> None:
    assert _parse_optional_duration("300") == 300
    assert _parse_optional_duration("60s") == 60
    assert _parse_optional_duration("5m") == 300
    assert _parse_optional_duration("2h") == 7200
    assert _parse_optional_duration("1d") == 86400
    assert _parse_optional_duration(None) is None
    assert _parse_optional_duration("") is None


def test_parse_optional_duration_rejects_bad_input() -> None:
    with pytest.raises(UserInputError):
        _parse_optional_duration("forever")


def test_format_duration_hits_each_branch() -> None:
    assert _format_duration(0) == "now"
    assert _format_duration(-1) == "now"
    assert _format_duration(45) == "45s"
    assert _format_duration(60) == "1m"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3600) == "1h"
    assert _format_duration(7325) == "2h 2m"
    assert _format_duration(86400) == "1d"
    assert _format_duration(360000) == "4d 4h"


def test_format_reset_phrase_handles_past_present_future() -> None:
    assert _format_reset_phrase(resets_at=1500, now=1000) == "resets in 8m 20s"
    assert _format_reset_phrase(resets_at=1000, now=1000) == "just reset"
    assert _format_reset_phrase(resets_at=970, now=1000) == "reset 30s ago"
    assert _format_reset_phrase(resets_at=400, now=1000) == "reset 10m ago"


def test_format_human_line_uses_past_tense_after_reset() -> None:
    snap = WindowSnapshot(used_percentage=11.0, resets_at=970)
    assert _format_human_line("5h", snap, now=1000) == "5h: 11% used, reset 30s ago"


def test_format_human_line_no_data_drops_reset_suffix() -> None:
    snap = WindowSnapshot(used_percentage=None, resets_at=1000)
    assert _format_human_line("5h", snap, now=1000) == "5h: no data"


def test_format_cost_line_returns_none_when_aggregate_has_no_cost() -> None:
    """Sessions exist but writer never emitted a USD cost field -> caller drops the line.

    Real call sites guarantee ``session_count >= 1`` and a real
    ``latest_event_at`` (gated in ``_write_source_section``); this test
    pins the ``total_cost_usd is None`` early-return that fires when the
    writer left out cost data despite having sessions of the mode.
    """
    assert (
        _format_cost_line(
            mode_label="api cost",
            mode_suffix="",
            aggregate_cost=CostSnapshot(),
            session_count=1,
            since_seconds=86400,
            latest_event_at=2000,
            now=2000,
        )
        is None
    )


def test_format_cost_line_single_session_shape_uses_age_phrase() -> None:
    """One session in the aggregate -> render the age phrase (not the session count).

    Both empty and non-empty ``mode_suffix`` are exercised here: subscription
    appends ` (imputed)` after the label; api appends nothing.
    """
    sub_line = _format_cost_line(
        mode_label="subscription cost",
        mode_suffix=" (imputed)",
        aggregate_cost=CostSnapshot(total_cost_usd=0.4275),
        session_count=1,
        since_seconds=86400,
        latest_event_at=1880,
        now=2000,
    )
    assert sub_line == "subscription cost (imputed): $0.43 (2m ago)"
    api_line = _format_cost_line(
        mode_label="api cost",
        mode_suffix="",
        aggregate_cost=CostSnapshot(total_cost_usd=1.23),
        session_count=1,
        since_seconds=86400,
        latest_event_at=2000,
        now=2000,
    )
    assert api_line == "api cost: $1.23 (just now)"


def test_format_cost_line_multi_session_shape_uses_since_suffix() -> None:
    """Multiple sessions -> render the session count + ``in last <since>`` suffix.

    Drops the age annotation because it would be ambiguous (which session?).
    """
    line = _format_cost_line(
        mode_label="api cost",
        mode_suffix="",
        aggregate_cost=CostSnapshot(total_cost_usd=5.43),
        session_count=3,
        since_seconds=86400,
        latest_event_at=2000,
        now=2000,
    )
    assert line == "api cost: $5.43 across 3 sessions in last 1d"


def _estimated_subscription_snapshot(provenance: CostProvenance) -> UsageSnapshot:
    return UsageSnapshot(
        source_name="codex",
        updated_at=1000,
        windows={},
        sessions=(
            SessionCostRecord(
                session_id="sub-token-session",
                cost=CostSnapshot(total_cost_usd=0.30),
                cost_mode=CostMode.SUBSCRIPTION,
                cost_provenance=provenance,
                first_event_at=950,
                last_event_at=1000,
            ),
        ),
        since_seconds=86400,
    )


def test_subscription_cost_line_flags_estimated_dollars(capsys: pytest.CaptureFixture[str]) -> None:
    """A token-derived (ESTIMATED) subscription cost renders ``(imputed, estimated)``;
    a harness-REPORTED one keeps plain ``(imputed)``. Mirrors the api line's
    ``(estimated)`` flag and the JSON/CEL ``is_estimated`` surface."""
    estimated_model = _build_render_model(
        _estimated_subscription_snapshot(CostProvenance.ESTIMATED), stale_after=300, now=1000
    )
    _write_source_section(estimated_model, now=1000, header="[codex]", detail=False)
    assert "subscription cost (imputed, estimated): $0.30" in capsys.readouterr().out

    reported_model = _build_render_model(
        _estimated_subscription_snapshot(CostProvenance.REPORTED), stale_after=300, now=1000
    )
    _write_source_section(reported_model, now=1000, header="[codex]", detail=False)
    reported_out = capsys.readouterr().out
    assert "subscription cost (imputed): $0.30" in reported_out
    assert "estimated" not in reported_out


def test_session_mode_tag_maps_each_variant() -> None:
    """``_session_mode_tag`` exhaustively maps every ``CostMode`` to its
    short ``--detail`` tag. The mapping is part of the user-visible contract:
    ``SUBSCRIPTION`` -> ``"sub"``, ``API_KEY`` -> ``"api"``. Swapping or
    dropping either entry would silently mis-label sessions in mixed-mode
    breakdowns.
    """
    assert _session_mode_tag(CostMode.SUBSCRIPTION) == "sub"
    assert _session_mode_tag(CostMode.API_KEY) == "api"


def test_format_session_detail_line_returns_none_when_session_has_no_cost() -> None:
    """A session with no usable cost reading drops out of the ``--detail``
    breakdown -- the helper returns None so the caller skips writing the
    line entirely (matching ``_format_cost_line``'s None-drop contract).
    """
    session = SessionCostRecord(
        session_id="abcdef12-uuid-rest",
        cost=CostSnapshot(),
        cost_mode=CostMode.API_KEY,
        first_event_at=1000,
        last_event_at=1500,
    )
    assert _format_session_detail_line(session, now=2000) is None


def test_format_session_detail_line_renders_tag_truncated_id_and_age() -> None:
    """Rendered line matches the documented format:
    ``  [<tag>] <8-char id prefix>: $X.YY (<age phrase>)``. Session id longer
    than ``_SESSION_DETAIL_ID_PREFIX_LEN`` is truncated; the mode tag comes
    from ``_session_mode_tag``; cost is two-decimal USD.
    """
    session = SessionCostRecord(
        session_id="deadbeef-uuid-rest",
        cost=CostSnapshot(total_cost_usd=0.42),
        cost_mode=CostMode.SUBSCRIPTION,
        first_event_at=1800,
        last_event_at=1880,
    )
    line = _format_session_detail_line(session, now=2000)
    assert line == "  [sub] deadbeef: $0.42 (2m ago)"


# =============================================================================
# Event reading + snapshot building
# =============================================================================


def test_parse_events_from_content_returns_all_valid_lines() -> None:
    """Every well-formed JSON line is parsed into a typed ``UsageEvent``. A
    truncated trailing line is skipped with a warning rather than failing the
    whole parse."""
    content = (
        json.dumps(_make_event("2026-05-08T10:00:00.000000000Z"))
        + "\n"
        + json.dumps(_make_event("2026-05-08T11:00:00.000000000Z"))
        + "\n"
        + "{not valid json"
    )
    events = parse_events_from_content(content, "test")
    assert len(events) == 2
    # Both well-formed lines parse into typed events, in file order, with the
    # ISO timestamps converted to Unix seconds.
    assert events[0].timestamp_unix == int(datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc).timestamp())
    assert events[1].timestamp_unix == int(datetime(2026, 5, 8, 11, 0, tzinfo=timezone.utc).timestamp())


def test_parse_events_from_content_returns_empty_for_empty_or_garbage() -> None:
    assert parse_events_from_content("", "test") == []
    assert parse_events_from_content("\n\n", "test") == []
    assert parse_events_from_content("garbage\nstill garbage\n", "test") == []


# =============================================================================
# Aggregation pipeline
# =============================================================================


def _cost_event(
    timestamp_iso: str,
    *,
    session_id: str,
    cost_usd: float,
    rate_limits: dict | None = None,
) -> dict:
    """Construct a cost-bearing event for aggregation tests."""
    event = {
        "source": "claude/usage",
        "type": "cost_snapshot",
        "event_id": f"evt-{session_id}-{timestamp_iso}",
        "timestamp": timestamp_iso,
        "session_id": session_id,
        "cost": {"total_cost_usd": cost_usd, "total_duration_ms": 1000},
    }
    if rate_limits is not None:
        event["rate_limits"] = rate_limits
    return event


def test_aggregate_drops_source_with_no_renderable_content() -> None:
    """A source whose events have no timestamp field yields no snapshot."""
    events = [{"source": "claude/usage", "type": "cost_snapshot"}]
    snapshots = aggregate_events_to_snapshots({"claude": {"agent-x": events}}, since_seconds=86400, now=1_700_001_000)
    assert snapshots == []


def test_aggregate_keeps_freshest_windows_across_events() -> None:
    """Windows track an account-level counter; the freshest event's rate_limits wins."""
    events = [
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "timestamp": "2026-05-08T10:00:00.000000000Z",
            "session_id": "session-x",
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999}},
        },
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "timestamp": "2026-05-08T11:00:00.000000000Z",
            "session_id": "session-x",
            "rate_limits": {"five_hour": {"used_percentage": 50.0, "resets_at": 9_999_999_999}},
        },
    ]
    snapshots = aggregate_events_to_snapshots({"claude": {"agent-x": events}}, since_seconds=86400, now=2_000_000_000)
    assert len(snapshots) == 1
    assert snapshots[0].windows["five_hour"].used_percentage == 50.0


def test_aggregate_drops_events_without_session_id() -> None:
    """The reader requires session_id on every event. Events that lack it
    (writer bug or upstream-payload drift) are dropped entirely -- they
    don't contribute windows or cost -- and the reader emits a WARNING so
    the user notices the data loss instead of silently missing rows in
    ``mngr usage``. With the bundled Claude writer this case doesn't occur
    because Claude Code always emits session_id."""
    events = [
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-orphan",
            "timestamp": "2026-05-08T11:00:00.000000000Z",
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999}},
        }
    ]
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(msg.record["message"]), level="WARNING", format="{message}")
    try:
        snapshots = aggregate_events_to_snapshots(
            {"claude": {"agent-x": events}}, since_seconds=86400, now=2_000_000_000
        )
    finally:
        logger.remove(sink_id)
    # No session_id -> event dropped -> source has nothing renderable -> no snapshot.
    assert snapshots == []
    # And the user is warned about the dropped event so the data loss is visible.
    warning_text = " ".join(captured)
    assert "session_id" in warning_text
    assert "evt-orphan" in warning_text
    assert "claude" in warning_text


def test_aggregate_groups_per_session_and_computes_session_contribution_delta() -> None:
    """Within one Claude Code process, ``SessionCostRecord.cost`` is the
    session's *own* contribution -- delta from the prior session's
    cumulative reading in the same process. Cost is process-cumulative
    upstream (the writer emits raw cumulative); the reader undoes that
    encoding so consumers see what each session actually cost.

    Walk:
      - abc sees 0.10 then 0.42 (latest cumulative for abc).
      - def sees 1.23. No cost drop between events, so all three are in
        one process. abc's contribution = 0.42 (delta from baseline 0).
        def's contribution = 1.23 - 0.42 = 0.81.
      - Aggregate (sum of contributions) = 0.42 + 0.81 = 1.23, which
        matches the final cumulative reading (true total spend).

    No rate_limits in any event -> mode classified as ``api_key`` (real
    billable spend), so the aggregate lands in ``api_cost`` rather than
    ``subscription_cost``.
    """
    events = [
        _cost_event("2026-05-08T10:00:00.000000000Z", session_id="abc", cost_usd=0.10),
        _cost_event("2026-05-08T10:30:00.000000000Z", session_id="abc", cost_usd=0.42),
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="def", cost_usd=1.23),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400 * 7,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.session_count == 2
    by_id = {s.session_id: s for s in snap.sessions}
    assert by_id["abc"].cost.total_cost_usd == pytest.approx(0.42)
    assert by_id["abc"].cost_mode == CostMode.API_KEY
    assert by_id["def"].cost.total_cost_usd == pytest.approx(0.81)
    assert by_id["def"].cost_mode == CostMode.API_KEY
    # Aggregate is the sum of contributions and recovers the final cumulative reading.
    assert snap.api_cost.total_cost_usd == pytest.approx(1.23)
    # No subscription sessions -> the subscription aggregate has no usable cost data.
    assert snap.subscription_cost.total_cost_usd is None
    assert snap.api_session_count == 2
    assert snap.subscription_session_count == 0


def test_aggregate_detects_process_boundary_via_cost_drop() -> None:
    """A downward step in cumulative cost between consecutive events from one
    agent signals a Claude Code process restart. Each process gets its own
    delta baseline so the second process's first session contribution starts
    from zero, not from the prior process's high-water mark.

    Walk: $5 then $0.30 from one agent. The drop is the process boundary;
    we should NOT compute the second session's spend as $0.30 - $5 (= -$4.70,
    nonsense). Instead each process's first session uses a zero baseline.
    Aggregate is $5 + $0.30 = $5.30, which is the true cross-process spend
    matching what a user would actually have been billed.
    """
    events = [
        _cost_event("2026-05-08T10:00:00.000000000Z", session_id="proc1-session", cost_usd=5.00),
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="proc2-session", cost_usd=0.30),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400 * 7,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.session_count == 2
    by_id = {s.session_id: s for s in snap.sessions}
    # Each session is the first (and only) in its own process; its contribution
    # is its full cumulative reading.
    assert by_id["proc1-session"].cost.total_cost_usd == pytest.approx(5.00)
    assert by_id["proc2-session"].cost.total_cost_usd == pytest.approx(0.30)
    # Aggregate is the cross-process sum -- the true total spend. No rate_limits
    # in either process -> both classified as api_key.
    assert snap.api_cost.total_cost_usd == pytest.approx(5.30)


def test_aggregate_isolates_process_boundary_detection_per_agent() -> None:
    """Cost-drop process-boundary detection runs per agent. If we merged
    streams across agents, every transition from agent-A's events to
    agent-B's events would look like a cost drop (their cost timelines are
    independent), spuriously splitting one agent's continuous process into
    fragments. This test plants two agents that would falsely trip the
    detector if streams were merged.
    """
    # Agent A: single process, cost grows 0.10 -> 0.50.
    agent_a_events = [
        _cost_event("2026-05-08T10:00:00.000000000Z", session_id="a-only-session", cost_usd=0.10),
        _cost_event("2026-05-08T10:30:00.000000000Z", session_id="a-only-session", cost_usd=0.50),
    ]
    # Agent B: single process, cost grows 0.05 -> 0.20. If merged with A,
    # the 0.50 -> 0.05 transition would look like a process boundary, but
    # really it's a different agent's independent timeline.
    agent_b_events = [
        _cost_event("2026-05-08T10:45:00.000000000Z", session_id="b-only-session", cost_usd=0.05),
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="b-only-session", cost_usd=0.20),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-a": agent_a_events, "agent-b": agent_b_events}},
        since_seconds=86400 * 7,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.session_count == 2
    by_id = {s.session_id: s for s in snap.sessions}
    # Each session is the only session in its agent's process; contribution = cumulative.
    assert by_id["a-only-session"].cost.total_cost_usd == pytest.approx(0.50)
    assert by_id["b-only-session"].cost.total_cost_usd == pytest.approx(0.20)
    # Aggregate sums across agents (both api_key mode since no rate_limits planted).
    assert snap.api_cost.total_cost_usd == pytest.approx(0.70)


def test_aggregate_filters_sessions_outside_recency_window() -> None:
    """Sessions whose last event is older than ``since_seconds`` are excluded."""
    base = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    fresh_ts = (base - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    stale_ts = (base - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    events = [
        _cost_event(fresh_ts, session_id="recent", cost_usd=0.42),
        _cost_event(stale_ts, session_id="old", cost_usd=99.99),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}}, since_seconds=86400, now=int(base.timestamp())
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    # The stale session is filtered out of `sessions`; the fresh one remains.
    assert snap.session_count == 1
    assert snap.sessions[0].session_id == "recent"
    # And the aggregate reflects only the in-window session (api_key mode since
    # no rate_limits planted).
    assert snap.api_cost.total_cost_usd == 0.42


def test_aggregate_sessions_sorted_newest_first() -> None:
    """``sessions`` is ordered by last_event_at descending so the newest is sessions[0]."""
    events = [
        _cost_event("2026-05-08T10:00:00.000000000Z", session_id="old", cost_usd=0.10),
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="mid", cost_usd=0.20),
        _cost_event("2026-05-08T12:00:00.000000000Z", session_id="new", cost_usd=0.30),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400 * 7,
        now=int(datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert [s.session_id for s in snapshots[0].sessions] == ["new", "mid", "old"]
    assert snapshots[0].sessions[0].session_id == "new"


def test_aggregate_returns_cost_only_snapshot_when_no_rate_limits() -> None:
    """API-key sessions emit cost without rate_limits; the snapshot is still
    built and the session is classified as ``api_key`` (real billable spend).
    """
    events = [
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="api-key-session", cost_usd=1.23),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.windows == {}
    assert snap.session_count == 1
    assert snap.sessions[0].cost.total_cost_usd == 1.23
    assert snap.sessions[0].cost_mode == CostMode.API_KEY
    # Real billable spend lands in api_cost; subscription_cost has no contributors.
    assert snap.api_cost.total_cost_usd == 1.23
    assert snap.subscription_cost.total_cost_usd is None


def test_aggregate_classifies_process_with_rate_limits_as_subscription() -> None:
    """A process is classified ``subscription`` when ANY of its events carry a
    non-empty rate_limits payload. Cost in that mode is **imputed** by Claude
    Code (subscription users don't actually pay per token), so the aggregate
    lands in ``subscription_cost`` and stays out of ``api_cost``.
    """
    rate_limits = {"five_hour": {"used_percentage": 11.0, "resets_at": 9_999_999_999}}
    events = [
        # First event: cost-only (no rate_limits yet -- typical for the brief window
        # before the first API response of a subscription session).
        _cost_event("2026-05-08T10:00:00.000000000Z", session_id="sub-sess", cost_usd=0.10),
        # Second event in the same process: rate_limits appears -> process is subscription.
        _cost_event("2026-05-08T10:30:00.000000000Z", session_id="sub-sess", cost_usd=0.42, rate_limits=rate_limits),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.session_count == 1
    assert snap.sessions[0].cost_mode == CostMode.SUBSCRIPTION
    # Imputed spend lands in subscription_cost; api_cost has no contributors.
    assert snap.subscription_cost.total_cost_usd == pytest.approx(0.42)
    assert snap.api_cost.total_cost_usd is None


def test_aggregate_classifies_different_processes_independently_when_auth_swaps() -> None:
    """Each Claude Code process is classified independently from its own events.
    A cost-drop boundary marks a new process; if the new process has no
    rate_limits while the old one did, the two get different ``cost_mode``
    tags and feed different aggregates.

    Scenario: subscription session (rate_limits present), user quits + relaunches
    Claude Code with ANTHROPIC_API_KEY (rate_limits gone, cost resets near zero).
    """
    rate_limits = {"five_hour": {"used_percentage": 50.0, "resets_at": 9_999_999_999}}
    events = [
        # Process 1: subscription (rate_limits present), cumulative cost climbs to $5.
        _cost_event(
            "2026-05-08T10:00:00.000000000Z", session_id="sub-process", cost_usd=5.00, rate_limits=rate_limits
        ),
        # Process 2: api_key (no rate_limits). Cost drop from 5.00 to 0.30 marks the
        # process boundary; this new process has no rate_limits anywhere.
        _cost_event("2026-05-08T11:00:00.000000000Z", session_id="api-process", cost_usd=0.30),
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    by_id = {s.session_id: s for s in snap.sessions}
    assert by_id["sub-process"].cost_mode == CostMode.SUBSCRIPTION
    assert by_id["api-process"].cost_mode == CostMode.API_KEY
    # The two aggregates remain distinct -- imputed subscription cost never gets
    # lumped with real api spend.
    assert snap.subscription_cost.total_cost_usd == pytest.approx(5.00)
    assert snap.api_cost.total_cost_usd == pytest.approx(0.30)
    assert snap.subscription_session_count == 1
    assert snap.api_session_count == 1


def test_aggregate_drops_event_with_non_string_session_id() -> None:
    """A non-string session_id is treated as absent (writer bug); the entire
    event is dropped -- including its rate_limits payload -- because the
    reader requires session_id to be a valid string on every event."""
    events = [
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "timestamp": "2026-05-08T11:00:00.000000000Z",
            "session_id": 12345,
            "cost": {"total_cost_usd": 5.0},
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999}},
        }
    ]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}},
        since_seconds=86400,
        now=int(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).timestamp()),
    )
    # No usable events for the source -> no snapshot.
    assert snapshots == []


def test_aggregate_returns_no_snapshot_when_only_sessions_outside_window_and_no_windows() -> None:
    """A source whose only events are out-of-window cost events and no rate_limits
    contributes nothing renderable -- the snapshot is dropped."""
    base = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    stale_ts = (base - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    events = [_cost_event(stale_ts, session_id="old", cost_usd=99.99)]
    snapshots = aggregate_events_to_snapshots(
        {"claude": {"agent-x": events}}, since_seconds=86400, now=int(base.timestamp())
    )
    assert snapshots == []


# =============================================================================
# Snapshot picking + render model
# =============================================================================


def test_render_model_marks_past_reset_as_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=999,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=900)},
    )
    model = _build_render_model(snapshot, stale_after=300, now=1000)
    # Age=1 (<300) so only the past-reset cause should fire.
    assert model.has_past_reset is True
    assert model.is_age_stale is False
    assert model.is_stale is True


def test_render_model_age_stale() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=500,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, stale_after=300, now=1000)
    # Reset is in the future so only the age cause should fire.
    assert model.is_age_stale is True
    assert model.has_past_reset is False
    assert model.is_stale is True


def test_render_model_fresh() -> None:
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=950,
        windows={"five_hour": WindowSnapshot(used_percentage=11.0, resets_at=2000)},
    )
    model = _build_render_model(snapshot, stale_after=300, now=1000)
    assert model.is_age_stale is False
    assert model.has_past_reset is False
    assert model.is_stale is False


def test_flatten_for_template_always_includes_per_mode_cost_keys() -> None:
    """``subscription_cost.*`` and ``api_cost.*`` template keys are always populated
    (empty string when absent) so format templates referencing them don't KeyError
    on snapshots that pre-date session/cost capture. The format-template surface
    intentionally doesn't expose per-session paths -- callers wanting that should
    use ``--format json`` and index ``sessions[]``."""
    snapshot_without_cost = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=1500)},
    )
    flat = _flatten_primary_for_template(
        _build_render_model(snapshot_without_cost, stale_after=300, now=1000), now=1000
    )
    assert flat["subscription_cost.total_cost_usd"] == ""
    assert flat["subscription_cost.total_duration_ms"] == ""
    assert flat["api_cost.total_cost_usd"] == ""
    assert flat["api_cost.total_duration_ms"] == ""
    assert flat["session_count"] == "0"
    assert flat["subscription_session_count"] == "0"
    assert flat["api_session_count"] == "0"
    # No combined `cost.*` key -- subscription and api stay distinct.
    assert "cost.total_cost_usd" not in flat
    # Per-session paths are intentionally not exposed in the format-template surface.
    assert "current_session.session_id" not in flat
    assert "sessions" not in flat


def test_flatten_for_template_populates_api_cost_when_session_is_api_mode() -> None:
    """A single api-key session populates ``api_cost.*`` and leaves ``subscription_cost.*`` empty."""
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={},
        sessions=(
            SessionCostRecord(
                session_id="uuid-abc",
                cost=CostSnapshot(total_cost_usd=0.42, total_duration_ms=12000),
                cost_mode=CostMode.API_KEY,
                first_event_at=900,
                last_event_at=950,
            ),
        ),
        since_seconds=86400,
    )
    flat = _flatten_primary_for_template(_build_render_model(snapshot, stale_after=300, now=1000), now=1000)
    # api_cost reflects the session's reading; subscription_cost stays empty.
    assert flat["api_cost.total_cost_usd"] == "0.42"
    assert flat["api_cost.total_duration_ms"] == "12000"
    assert flat["subscription_cost.total_cost_usd"] == ""
    assert flat["session_count"] == "1"
    assert flat["api_session_count"] == "1"
    assert flat["subscription_session_count"] == "0"


def test_flatten_for_template_aggregates_only_within_each_mode() -> None:
    """Per-mode aggregates only sum sessions of that mode. Two api sessions
    feed ``api_cost.*``; one subscription session feeds ``subscription_cost.*``;
    nothing is conflated.
    """
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=2000,
        windows={},
        sessions=(
            SessionCostRecord(
                session_id="sub-x",
                cost=CostSnapshot(total_cost_usd=0.10),
                cost_mode=CostMode.SUBSCRIPTION,
                first_event_at=1800,
                last_event_at=2000,
            ),
            SessionCostRecord(
                session_id="api-newer",
                cost=CostSnapshot(total_cost_usd=1.0),
                cost_mode=CostMode.API_KEY,
                first_event_at=1500,
                last_event_at=1900,
            ),
            SessionCostRecord(
                session_id="api-older",
                cost=CostSnapshot(total_cost_usd=0.42),
                cost_mode=CostMode.API_KEY,
                first_event_at=1000,
                last_event_at=1500,
            ),
        ),
        since_seconds=86400,
    )
    flat = _flatten_primary_for_template(_build_render_model(snapshot, stale_after=300, now=2000), now=2000)
    # api_cost sums only api_key sessions; subscription_cost only subscription.
    assert flat["api_cost.total_cost_usd"] == "1.42"
    assert flat["subscription_cost.total_cost_usd"] == "0.1"
    assert flat["session_count"] == "3"
    assert flat["api_session_count"] == "2"
    assert flat["subscription_session_count"] == "1"


def test_flatten_for_template_emits_only_present_windows() -> None:
    """Format-template flat dict reflects only the windows the writer actually
    emitted. Absent windows produce no template keys -- that's the writer's
    responsibility to populate, not mngr_usage's to synthesize."""
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=1500)},
    )
    model = _build_render_model(snapshot, stale_after=300, now=1000)
    flat = _flatten_primary_for_template(model, now=1000)
    assert flat["source"] == "claude"
    assert flat["five_hour.used_percentage"] == "42.00"
    assert flat["five_hour.resets_at"] == "1500"
    assert flat["five_hour.seconds_until_reset"] == "500"
    assert flat["five_hour.is_present"] == "true"
    # seven_day was not emitted by the writer, so no seven_day.* keys exist.
    assert "seven_day.is_present" not in flat
    assert "seven_day.used_percentage" not in flat


# =============================================================================
# CLI integration: plant events.jsonl files under the test's host_dir
# =============================================================================


@pytest.fixture
def cli_profile_dir(temp_host_dir: Path, temp_profile_dir: Path) -> Path:
    """Pin the CLI's auto-resolved profile_dir so writes via temp_host_dir reach the CLI."""
    config_path = temp_host_dir / ROOT_CONFIG_FILENAME
    config_path.write_text(f'profile = "{temp_profile_dir.name}"\n')
    return temp_profile_dir


@pytest.fixture
def cli_test_agent(local_host: Host, tmp_path: Path) -> AgentInterface:
    """Register a real local agent (not started) so ``list_agents`` finds it.

    Returns the registered agent; tests can plant events into its state dir
    at ``get_agent_state_dir_path(local_host.host_dir, agent.id) / "events" / ...``.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return local_host.create_agent_state(
        work_dir_path=work_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )


def _plant_event_for_agent(
    local_host: Host, agent: AgentInterface, event: dict[str, Any], source: str = "claude"
) -> None:
    """Plant an event into the agent's events file at the conventional path."""
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    events_file = state_dir / "events" / source / "usage" / "events.jsonl"
    _write_event(events_file, event)


@pytest.mark.tmux
def test_usage_command_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            # Timestamp in the future so the snapshot won't be stale-by-age in the test
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "human-format-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 9_999_999_999_999, "label": "5h"},
            },
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Writer emitted label="5h", so the line uses "5h:" rather than the literal key.
    assert "5h:" in result.output
    assert "73% used" in result.output


def _plant_preserved_usage_agent(local_host: Host, agent_id: AgentId, name: str, event: dict[str, Any]) -> None:
    """Plant a preserved (destroyed-agent) usage dir under <host_dir>/preserved/.

    Mirrors what the destroy-time preservation writes: a usage events file, the
    agent's data.json, and the host-metadata sidecar the reader keys on.
    """
    preserved_dir = local_host.host_dir / "preserved" / f"{name}--{agent_id}"
    preserved_dir.mkdir(parents=True, exist_ok=True)
    _write_event(preserved_dir / "events" / "claude" / "usage" / "events.jsonl", event)
    (preserved_dir / "data.json").write_text(
        json.dumps({"id": str(agent_id), "name": name, "type": "claude", "work_dir": "/tmp/w"})
    )
    (preserved_dir / "mngr_usage_meta.json").write_text(
        json.dumps({"provider_name": "local", "host_id": "host-" + "0" * 32, "host_name": "h"})
    )


def _parse_json_payload(output: str) -> dict[str, Any]:
    """Parse the single JSON object line from CLI output, skipping any leading log lines."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    raise AssertionError(f"No JSON object line in output: {output!r}")


def test_usage_command_includes_preserved_by_default_and_excludes_with_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_profile_dir: Path,
) -> None:
    """A destroyed agent's preserved usage shows by default; --no-preserved hides it.

    No live agent is registered, so the only data is the preserved dir -- the
    default run must surface it, and --no-preserved must drop it entirely.
    """
    _plant_preserved_usage_agent(
        local_host,
        AgentId.generate(),
        "gone-agent",
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-preserved",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "preserved-session",
            "rate_limits": {"five_hour": {"used_percentage": 42.0, "resets_at": 9_999_999_999_999}},
        },
    )

    default_result = cli_runner.invoke(
        usage, ["--format", "json", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert default_result.exit_code == 0, default_result.output
    default_payload = _parse_json_payload(default_result.output)
    assert [s["source"] for s in default_payload["sources"]] == ["claude"]
    assert default_payload["sources"][0]["five_hour"]["used_percentage"] == 42.0

    excluded_result = cli_runner.invoke(
        usage,
        ["--format", "json", "--stale-after", "300", "--no-preserved"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert excluded_result.exit_code == 0, excluded_result.output
    # No live agents and preserved excluded -> the no-data hint is logged ahead
    # of the JSON line; parse just the JSON object.
    assert _parse_json_payload(excluded_result.output)["sources"] == []


@pytest.mark.tmux
def test_usage_command_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "json-format-session",
            "rate_limits": {"five_hour": {"used_percentage": 12.3, "resets_at": 9_999_999_999_999}},
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["sources"][0]["source"] == "claude"
    assert payload["sources"][0]["five_hour"]["used_percentage"] == 12.3
    assert payload["sources"][0]["five_hour"]["is_present"] is True
    # No window_seconds emitted in this event, so derived elapsed_* fields are None.
    assert payload["sources"][0]["five_hour"]["window_seconds"] is None
    assert payload["sources"][0]["five_hour"]["elapsed_seconds"] is None
    assert payload["sources"][0]["five_hour"]["elapsed_percentage"] is None
    # seven_day was not emitted by the writer, so it doesn't appear in the JSON either.
    assert "seven_day" not in payload["sources"][0]


@pytest.mark.tmux
def test_usage_command_json_surfaces_elapsed_when_window_seconds_present(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """When the writer emits window_seconds, the JSON output exposes elapsed_seconds + elapsed_percentage.

    Anchors `resets_at` 5400s into the future of a 18000s window so 70% has elapsed,
    independent of when the test runs.
    """
    now_s = int(datetime.now(timezone.utc).timestamp())
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            # Use a fresh ISO timestamp so the snapshot isn't age-stale.
            "timestamp": datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
            "session_id": "elapsed-window-session",
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 12.3,
                    "resets_at": now_s + 5400,
                    "window_seconds": 18000,
                },
            },
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    five_hour = payload["sources"][0]["five_hour"]
    assert five_hour["window_seconds"] == 18000
    # Compute the expected elapsed off the CLI's own ``now`` (echoed in the JSON
    # payload) rather than a clock the test captured before invoking, so any
    # wall-clock drift between test setup and CLI invocation is cancelled out.
    cli_now = payload["now"]
    expected_elapsed = 18000 - (now_s + 5400 - cli_now)
    assert five_hour["elapsed_seconds"] == expected_elapsed
    assert five_hour["elapsed_percentage"] == expected_elapsed / 18000 * 100


@pytest.mark.tmux
def test_usage_command_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "format-template-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 88.0, "resets_at": 9_999_999_999_999},
                "seven_day": {"used_percentage": 44.0, "resets_at": 9_999_999_999_999},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["--format", "5h:{five_hour.used_percentage}/7d:{seven_day.used_percentage}", "--stale-after", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "5h:88.00/7d:44.00" in result.output


def test_usage_command_no_data_when_no_events(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """No agents on the host means no events files; render the no-data hint."""
    result = cli_runner.invoke(usage, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "No usage data yet" in result.output


def test_usage_command_format_template_with_no_events_emits_empty_stdout(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """`mngr usage --format '<template>'` with no agents emits no substituted line.

    The no-sources path under a format template returns without writing anything
    to stdout (no synthesized empty-substituted line, no no-data warning),
    letting a `--format '...'` consumer detect the no-data case by an empty
    output. The substituted template (anchored on a unique sentinel) must not
    appear, and the no-data warning must not fire either -- under format
    templates we keep stderr quiet so machine consumers see a clean empty
    stream.
    """
    result = cli_runner.invoke(
        usage,
        ["--format", "SENTINEL:{five_hour.used_percentage}"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # No substituted template line reached stdout (regression guard against
    # the previously-synthesized empty render model).
    assert "SENTINEL:" not in result.output
    # And the no-data warning is suppressed under format templates so the
    # caller's combined output is genuinely empty.
    assert "No usage data yet" not in result.output


@pytest.mark.tmux
def test_usage_command_picks_freshest_across_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """Two agents, two events, the most-recent timestamp wins."""
    work_dir_old = tmp_path / "work-old"
    work_dir_old.mkdir()
    agent_old = local_host.create_agent_state(
        work_dir_path=work_dir_old,
        options=CreateAgentOptions(
            name=AgentName("usage-test-old"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    work_dir_new = tmp_path / "work-new"
    work_dir_new.mkdir()
    agent_new = local_host.create_agent_state(
        work_dir_path=work_dir_new,
        options=CreateAgentOptions(
            name=AgentName("usage-test-new"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        agent_old,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-old",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "freshest-test-session-old",
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 9_999_999_999_999}},
        },
    )
    _plant_event_for_agent(
        local_host,
        agent_new,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-new",
            "timestamp": "2056-05-08T11:00:00.000000000Z",
            "session_id": "freshest-test-session-new",
            "rate_limits": {"five_hour": {"used_percentage": 99.0, "resets_at": 9_999_999_999_999}},
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    # Both events share source_name="claude" since they live under .../events/claude/...
    # The aggregator groups events from all agents under one source and keeps the
    # freshest event's rate_limits as the snapshot's windows, so we see exactly
    # one entry and its data is the newer event's.
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["source"] == "claude"
    assert payload["sources"][0]["five_hour"]["used_percentage"] == 99.0


@pytest.mark.tmux
def test_usage_command_uses_reset_specific_warning_when_window_just_reset(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """Regression: when a snapshot is fresh but a window already reset, the
    warning should call out the reset specifically (not say "snapshot last
    updated now ago"). The age-based warning fires only when the snapshot
    itself is stale by age."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-fresh",
            "timestamp": now_iso,
            "session_id": "reset-warning-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 37.0, "resets_at": 1000, "label": "5h"},
            },
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Age warning is gone (snapshot was just written).
    assert "snapshot last updated" not in result.output
    assert "now ago" not in result.output
    # Reset-specific warning fires instead.
    assert "a window already reset" in result.output


@pytest.mark.tmux
def test_usage_wait_matches_when_predicate_already_true(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """End-to-end: planted snapshot already satisfies the predicate -> exit 0 on first poll."""
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "wait-matches-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 12.0, "resets_at": 9_999_999_999_999, "window_seconds": 18000},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "five_hour.used_percentage < 50", "--interval", "1s", "--timeout", "5s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "Matched on source" in result.output or "matched" in result.output.lower()


@pytest.mark.tmux
def test_usage_wait_times_out_when_predicate_never_satisfied(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """End-to-end: predicate always false -> exit 2 (timeout) after --timeout passes."""
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "wait-timeout-session",
            "rate_limits": {
                "five_hour": {"used_percentage": 90.0, "resets_at": 9_999_999_999_999, "window_seconds": 18000},
            },
        },
    )
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "five_hour.used_percentage < 50", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # Exit code 2 == EXIT_CODE_TIMEOUT from mngr.cli.exit_codes; matches `mngr wait`.
    assert result.exit_code == 2, result.output
    assert "Timed out" in result.output


def test_usage_wait_rejects_group_level_options_when_subcommand_invoked(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Group-level options like `--local` placed before the subcommand are silently
    ignored by Click's early-return. We surface a UserInputError instead so the user
    sees their flag is in the wrong position."""
    result = cli_runner.invoke(
        usage,
        ["--local", "wait", "--until", "true", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    # The error names the offending flag and the corrective placement.
    assert "--local" in result.output
    assert "wait" in result.output


def test_usage_wait_rejection_uses_visible_flag_name_for_renamed_params(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """The error message must use the user-visible CLI flag (e.g. ``--format``),
    not the underlying click param name (``output_format``). Otherwise the
    suggestion sends the user looking for a flag that doesn't exist."""
    result = cli_runner.invoke(
        usage,
        ["--format", "json", "wait", "--until", "true", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "--format" in result.output
    # The internal param name must NOT leak into the message.
    assert "--output-format" not in result.output


def test_usage_wait_accepts_subcommand_level_options(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Sanity: putting the same flag after the subcommand is the supported form
    and reaches the wait body (here it times out since no matching agent exists)."""
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "true", "--local", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # `--until 'true'` would normally match instantly, but with no agents present
    # there are no snapshots to evaluate against, so the wait times out (exit 2).
    assert result.exit_code in (0, 2), result.output


def test_usage_wait_rejects_invalid_cel(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    cli_profile_dir: Path,
) -> None:
    """Invalid CEL must fail fast with a clear error rather than time out."""
    result = cli_runner.invoke(
        usage,
        ["wait", "--until", "this is not a valid cel expression {[", "--interval", "1s", "--timeout", "1s"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # MngrError bubbles up as a non-zero exit; the user-visible signal is the
    # "Invalid include filter" message.
    assert result.exit_code != 0
    assert "Invalid" in result.output or "invalid" in result.output.lower()


@pytest.mark.tmux
def test_usage_wait_applies_since_to_cost_aggregate(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """`mngr usage wait --since` tightens the recency window applied to each
    poll's cost aggregation, mirroring `mngr usage --since`. A predicate that
    succeeds iff a stale high-cost session is dropped from `api_cost` proves
    the flag reached `gather_usage_snapshots` (it would time out otherwise).
    """
    base = datetime.now(timezone.utc)
    now_iso = base.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    stale_iso = (base - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-stale",
            "timestamp": stale_iso,
            "session_id": "wait-since-stale-session",
            "cost": {"total_cost_usd": 99.0},
        },
    )
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-fresh",
            "timestamp": now_iso,
            "session_id": "wait-since-fresh-session",
            "cost": {"total_cost_usd": 0.50},
        },
    )
    # With `--since 1h` only the fresh $0.50 session contributes to `api_cost`,
    # so `api_cost.total_cost_usd < 1.0` matches on the first poll. Without
    # `--since 1h` the default 24h window would include the stale $99 session
    # and the predicate would be false.
    result = cli_runner.invoke(
        usage,
        [
            "wait",
            "--until",
            "api_cost.total_cost_usd < 1.0",
            "--since",
            "1h",
            "--interval",
            "1s",
            "--timeout",
            "5s",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "Matched on source" in result.output or "matched" in result.output.lower()


@pytest.mark.tmux
def test_usage_command_renders_subscription_cost_line_for_subscription_user(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """Subscription users get rate_limits + cost. The presence of rate_limits
    classifies the process as subscription, and the human output renders the
    cost on a 'subscription cost (imputed)' line (calling out that the value
    is imputed by Claude Code, not real billable spend). No 'api cost' line
    appears since no api-key session contributed.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": now_iso,
            "session_id": "abc12345-uuid-rest",
            "cost": {"total_cost_usd": 0.4275, "total_duration_ms": 12000},
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 9_999_999_999_999, "label": "5h"},
            },
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Subscription cost line with the imputed callout; 2-decimal money format.
    assert "subscription cost (imputed): $0.43" in result.output
    # No api-key line since no api-key session contributed.
    assert "api cost:" not in result.output
    # The window line still renders alongside.
    assert "5h:" in result.output
    # The cost line must appear between the [source] header and the window line.
    cost_idx = result.output.index("subscription cost (imputed): $0.43")
    assert result.output.index("[claude]") < cost_idx < result.output.index("5h:")


@pytest.mark.tmux
def test_usage_command_renders_api_cost_line_for_api_key_user(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """API-key sessions emit cost but never rate_limits. The absence of
    rate_limits classifies the process as api_key, so the human output
    renders an 'api cost' line (real billable spend, no 'imputed' callout).
    No subscription line appears.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": now_iso,
            "session_id": "deadbeef-uuid-rest",
            "cost": {"total_cost_usd": 1.23},
            "rate_limits": None,
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "api cost: $1.23" in result.output
    # No subscription line should appear -- the user is on a direct API key.
    assert "subscription cost" not in result.output
    # The "No usage data yet" hint must not fire -- cost IS data.
    assert "No usage data yet" not in result.output


@pytest.mark.tmux
def test_usage_command_json_default_is_summary_only(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """Default JSON output is summary-only: per-mode aggregates
    (``subscription_cost`` and ``api_cost``), ``session_count`` (total) plus
    per-mode counts, and the windows. ``sessions[]`` is omitted unless
    ``--detail`` is passed, keeping the common-case payload small. There is
    no combined ``cost`` key -- subscription and api cost stay distinct.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    # rate_limits present alongside cost -> classified as subscription.
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": now_iso,
            "session_id": "uuid-abc",
            "cost": {"total_cost_usd": 0.42, "total_duration_ms": 12000, "total_api_duration_ms": 8000},
            "rate_limits": {"five_hour": {"used_percentage": 12.3, "resets_at": 9_999_999_999_999}},
        },
    )
    result = cli_runner.invoke(
        usage, ["--format", "json", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    source = payload["sources"][0]
    # Subscription aggregate carries the session's reading; api aggregate is all-None.
    assert source["subscription_cost"]["total_cost_usd"] == 0.42
    assert source["subscription_cost"]["total_duration_ms"] == 12000
    assert source["subscription_cost"]["total_api_duration_ms"] == 8000
    # Fields the writer didn't supply are still present in the dict with None values.
    assert source["subscription_cost"]["total_lines_added"] is None
    assert source["api_cost"]["total_cost_usd"] is None
    # Counts are split by mode; total session_count is the sum.
    assert source["session_count"] == 1
    assert source["subscription_session_count"] == 1
    assert source["api_session_count"] == 0
    # No combined `cost` key -- callers must pick a mode explicitly.
    assert "cost" not in source
    # Summary-only by default: no per-session breakdown unless --detail is set.
    assert "sessions" not in source
    assert "current_session" not in source


@pytest.mark.tmux
def test_usage_command_detail_flag_includes_sessions_in_json(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """``--detail`` adds ``sessions[]`` (newest-first) to each source in the JSON output."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-1",
            "timestamp": now_iso,
            "session_id": "uuid-abc",
            "cost": {"total_cost_usd": 0.42},
        },
    )
    result = cli_runner.invoke(
        usage,
        ["--format", "json", "--stale-after", "300", "--detail"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    source = payload["sources"][0]
    assert source["session_count"] == 1
    assert len(source["sessions"]) == 1
    assert source["sessions"][0]["session_id"] == "uuid-abc"
    assert source["sessions"][0]["cost"]["total_cost_usd"] == 0.42
    # cost_mode is exposed so consumers can filter sessions[] by auth context.
    # No rate_limits planted -> classified as api_key.
    assert source["sessions"][0]["cost_mode"] == CostMode.API_KEY


@pytest.mark.tmux
def test_usage_command_aggregates_cost_across_agents_in_same_source(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """Two Claude agents on different api-key sessions both contribute to
    ``claude``'s aggregate api spend. With ``--detail`` the JSON output exposes
    both session records under ``sessions[]`` and ``api_cost.total_cost_usd``
    sums across them. No rate_limits in either event -> both api_key mode.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    agent_a_dir = tmp_path / "agent-a"
    agent_a_dir.mkdir()
    agent_a = local_host.create_agent_state(
        work_dir_path=agent_a_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-agg-a"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    agent_b_dir = tmp_path / "agent-b"
    agent_b_dir.mkdir()
    agent_b = local_host.create_agent_state(
        work_dir_path=agent_b_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-agg-b"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        agent_a,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-a",
            "timestamp": now_iso,
            "session_id": "session-aaaa-uuid",
            "cost": {"total_cost_usd": 0.50},
        },
    )
    _plant_event_for_agent(
        local_host,
        agent_b,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-b",
            "timestamp": now_iso,
            "session_id": "session-bbbb-uuid",
            "cost": {"total_cost_usd": 1.20},
        },
    )
    result = cli_runner.invoke(
        usage,
        ["--format", "json", "--stale-after", "300", "--detail"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    source = payload["sources"][0]
    assert source["session_count"] == 2
    assert source["api_session_count"] == 2
    assert source["subscription_session_count"] == 0
    # api_cost is the sum across all api-mode sessions in the recency window;
    # subscription_cost has no contributors.
    assert source["api_cost"]["total_cost_usd"] == pytest.approx(1.70)
    assert source["subscription_cost"]["total_cost_usd"] is None
    # With --detail, both session_ids appear under sessions[].
    session_ids = {s["session_id"] for s in source["sessions"]}
    assert session_ids == {"session-aaaa-uuid", "session-bbbb-uuid"}
    # Both sessions are api_key mode.
    assert all(s["cost_mode"] == CostMode.API_KEY for s in source["sessions"])


@pytest.mark.tmux
def test_usage_command_emits_aggregate_api_cost_line_with_multiple_sessions(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """With multiple api-key sessions in the recency window, the human output
    shows a single 'api cost: $X.YY across N sessions in last <since>' line
    -- no per-session ids in the default view (those are --detail-only).
    Both events lack rate_limits, so both classify as api_key mode.
    """
    base = datetime.now(timezone.utc)
    now_iso = base.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    earlier_iso = (base - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-earlier",
            "timestamp": earlier_iso,
            "session_id": "olderseessionid",
            "cost": {"total_cost_usd": 1.00},
        },
    )
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-now",
            "timestamp": now_iso,
            "session_id": "currentsession",
            "cost": {"total_cost_usd": 0.30},
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # With multiple api-mode sessions, the api line shows the aggregate with a session count.
    assert "api cost: $1.30 across 2 sessions" in result.output
    # No subscription line should appear -- neither event had rate_limits.
    assert "subscription cost" not in result.output
    # No per-session id appears in the default human view -- those are --detail-only.
    # 8-char truncation of the planted session ids: "currentsession" -> "currents",
    # "olderseessionid" -> "oldersee". Asserting the exact truncated prefixes
    # so a regression that surfaces either session line is caught.
    assert "currents" not in result.output
    assert "oldersee" not in result.output


@pytest.mark.tmux
def test_usage_command_detail_flag_emits_per_session_lines_in_human_output(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """``--detail`` adds indented per-session lines between the cost line and the
    window lines, newest-first. Suppressed when there's only one session (the
    cost line already names that session's reading)."""
    base = datetime.now(timezone.utc)
    now_iso = base.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    earlier_iso = (base - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-earlier",
            "timestamp": earlier_iso,
            "session_id": "olderseessionid",
            "cost": {"total_cost_usd": 1.00},
        },
    )
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-now",
            "timestamp": now_iso,
            "session_id": "currentsession",
            "cost": {"total_cost_usd": 0.30},
        },
    )
    result = cli_runner.invoke(usage, ["--detail", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # api cost line still shows the aggregate.
    assert "api cost: $1.30 across 2 sessions" in result.output, result.output
    # Per-session lines appear (newest-first, indented) with [api] tags. 8-char
    # prefixes: "currentsession" -> "currents"; "olderseessionid" -> "oldersee".
    cost_idx = result.output.index("api cost: $1.30")
    current_idx = result.output.index("[api] currents:")
    older_idx = result.output.index("[api] oldersee:")
    assert cost_idx < current_idx < older_idx, result.output


@pytest.mark.tmux
def test_usage_command_detail_flag_emits_sub_tag_for_subscription_sessions(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """``--detail`` per-session lines tag subscription sessions with ``[sub]``.

    The ``_session_mode_tag`` mapping (``SUBSCRIPTION -> "sub"``,
    ``API_KEY -> "api"``) is part of the user-visible contract: it's how
    users distinguish auth contexts in a mixed-mode breakdown. The
    api-tag side is covered elsewhere; this guards the subscription
    branch against a silent regression that would swap or drop the
    SUBSCRIPTION entry.
    """
    base = datetime.now(timezone.utc)
    now_iso = base.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    earlier_iso = (base - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    rate_limits = {"five_hour": {"used_percentage": 30.0, "resets_at": 9_999_999_999_999, "label": "5h"}}
    # Both events carry rate_limits -> both processes are classified as
    # subscription. Cost-drop boundary between them is fine; the mode
    # classification of each process is independent and both land in
    # SUBSCRIPTION here.
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-earlier-sub",
            "timestamp": earlier_iso,
            "session_id": "oldersubsession",
            "cost": {"total_cost_usd": 1.00},
            "rate_limits": rate_limits,
        },
    )
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-now-sub",
            "timestamp": now_iso,
            "session_id": "currentsubsess",
            "cost": {"total_cost_usd": 0.30},
            "rate_limits": rate_limits,
        },
    )
    result = cli_runner.invoke(usage, ["--detail", "--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Subscription aggregate line appears (imputed callout intact).
    assert "subscription cost (imputed): $1.30 across 2 sessions" in result.output, result.output
    # Per-session lines render with the [sub] tag (8-char truncated session ids).
    cost_idx = result.output.index("subscription cost (imputed): $1.30")
    current_idx = result.output.index("[sub] currents")
    older_idx = result.output.index("[sub] oldersub")
    assert cost_idx < current_idx < older_idx, result.output
    # No api cost line and no [api] tags -- this is a single-mode run.
    assert "api cost:" not in result.output
    assert "[api]" not in result.output


@pytest.mark.tmux
def test_usage_command_renders_both_cost_lines_when_both_modes_contribute(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """When one agent contributes subscription cost and another contributes api
    cost in the same recency window, both lines render. Subscription line
    appears first (imputed info is contextual), then the api line (real
    billable spend). The two aggregates stay distinct -- never lumped.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    sub_dir = tmp_path / "agent-sub"
    sub_dir.mkdir()
    sub_agent = local_host.create_agent_state(
        work_dir_path=sub_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-sub"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    api_dir = tmp_path / "agent-api"
    api_dir.mkdir()
    api_agent = local_host.create_agent_state(
        work_dir_path=api_dir,
        options=CreateAgentOptions(
            name=AgentName("usage-api"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        sub_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-sub",
            "timestamp": now_iso,
            "session_id": "subscription-session-uuid",
            "cost": {"total_cost_usd": 0.50},
            "rate_limits": {"five_hour": {"used_percentage": 30.0, "resets_at": 9_999_999_999_999, "label": "5h"}},
        },
    )
    _plant_event_for_agent(
        local_host,
        api_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-api",
            "timestamp": now_iso,
            "session_id": "api-key-session-uuid",
            "cost": {"total_cost_usd": 1.25},
        },
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Both cost lines render.
    assert "subscription cost (imputed): $0.50" in result.output
    assert "api cost: $1.25" in result.output
    # Subscription line appears before the api line.
    sub_idx = result.output.index("subscription cost (imputed): $0.50")
    api_idx = result.output.index("api cost: $1.25")
    assert sub_idx < api_idx, result.output


@pytest.mark.tmux
def test_usage_command_excludes_stale_session_via_since(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    cli_test_agent: AgentInterface,
    cli_profile_dir: Path,
) -> None:
    """--since tightens the recency window; a session whose last event is older
    than --since is excluded from the aggregate."""
    base = datetime.now(timezone.utc)
    now_iso = base.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    stale_iso = (base - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-stale",
            "timestamp": stale_iso,
            "session_id": "should-be-excluded",
            "cost": {"total_cost_usd": 99.0},
        },
    )
    _plant_event_for_agent(
        local_host,
        cli_test_agent,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-fresh",
            "timestamp": now_iso,
            "session_id": "in-window",
            "cost": {"total_cost_usd": 0.50},
        },
    )
    # --since 1h drops the 3h-old session.
    result = cli_runner.invoke(
        usage,
        ["--format", "json", "--stale-after", "300", "--since", "1h"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    source = payload["sources"][0]
    assert source["session_count"] == 1
    # No rate_limits planted -> api_key mode.
    assert source["api_cost"]["total_cost_usd"] == pytest.approx(0.50)
    assert source["subscription_cost"]["total_cost_usd"] is None


@pytest.mark.tmux
def test_usage_command_human_format_multi_source(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    cli_profile_dir: Path,
) -> None:
    """When two distinct sources contribute, render each as its own [source] section."""
    work_dir_a = tmp_path / "work-a"
    work_dir_a.mkdir()
    agent_a = local_host.create_agent_state(
        work_dir_path=work_dir_a,
        options=CreateAgentOptions(
            name=AgentName("usage-test-claude"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    work_dir_b = tmp_path / "work-b"
    work_dir_b.mkdir()
    agent_b = local_host.create_agent_state(
        work_dir_path=work_dir_b,
        options=CreateAgentOptions(
            name=AgentName("usage-test-opencode"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 9999"),
        ),
    )
    _plant_event_for_agent(
        local_host,
        agent_a,
        {
            "source": "claude/usage",
            "type": "cost_snapshot",
            "event_id": "evt-claude",
            "timestamp": "2056-05-08T10:00:00.000000000Z",
            "session_id": "multi-source-claude-session",
            "rate_limits": {"five_hour": {"used_percentage": 11.0, "resets_at": 9_999_999_999_999}},
        },
        source="claude",
    )
    _plant_event_for_agent(
        local_host,
        agent_b,
        {
            "source": "opencode/usage",
            "type": "cost_snapshot",
            "event_id": "evt-opencode",
            "timestamp": "2056-05-08T11:00:00.000000000Z",
            "session_id": "multi-source-opencode-session",
            "rate_limits": {"five_hour": {"used_percentage": 22.0, "resets_at": 9_999_999_999_999}},
        },
        source="opencode",
    )
    result = cli_runner.invoke(usage, ["--stale-after", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Both source headers present
    assert "[claude]" in result.output
    assert "[opencode]" in result.output
    # Both percentages rendered (somewhere)
    assert "11% used" in result.output
    assert "22% used" in result.output
    # Freshest first: opencode's section should appear before claude's
    assert result.output.index("[opencode]") < result.output.index("[claude]")
