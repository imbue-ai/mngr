"""Unit tests for ``mngr_usage.api`` -- the reader pipeline + wait primitive.

Pipeline tests (``gather_usage_snapshots``, ``_RawEventsCollector``,
``aggregate_events_to_snapshots``) are covered end-to-end in
``cli_test.py`` through the planted-events fixtures. The tests here focus
on the bits the wait subcommand depends on directly: ``derive_elapsed``
arithmetic, ``window_render_dict`` / ``build_source_cel_context`` shape
(which is the CEL surface), and the ``wait_for_usage`` polling loop (driven
with an injected ``poll_fn`` and short real intervals).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_usage.api import build_source_cel_context
from imbue.mngr_usage.api import derive_elapsed
from imbue.mngr_usage.api import wait_for_usage
from imbue.mngr_usage.api import window_render_dict
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance
from imbue.mngr_usage.data_types import CostSnapshot
from imbue.mngr_usage.data_types import SessionCostRecord
from imbue.mngr_usage.data_types import TokenSnapshot
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# =============================================================================
# derive_elapsed
# =============================================================================


def test_derive_elapsed_returns_none_when_window_seconds_missing() -> None:
    """Without window_seconds the reader can't know how long the window is."""
    snap = WindowSnapshot(used_percentage=50.0, resets_at=2000, window_seconds=None)
    assert derive_elapsed(snap, now=1000) == (None, None)


def test_derive_elapsed_returns_none_when_resets_at_missing() -> None:
    """resets_at is the only way to anchor the elapsed computation."""
    snap = WindowSnapshot(used_percentage=50.0, resets_at=None, window_seconds=18000)
    assert derive_elapsed(snap, now=1000) == (None, None)


def test_derive_elapsed_returns_none_when_window_seconds_nonpositive() -> None:
    """A zero or negative window_seconds is unusable; the writer shouldn't emit it that way."""
    snap = WindowSnapshot(used_percentage=50.0, resets_at=2000, window_seconds=0)
    assert derive_elapsed(snap, now=1000) == (None, None)


def test_derive_elapsed_middle_of_window() -> None:
    """At t=1000 in an 18000s window that resets at 14400+1000=15400, ~20% elapsed."""
    snap = WindowSnapshot(used_percentage=50.0, resets_at=15400, window_seconds=18000)
    elapsed_seconds, elapsed_percentage = derive_elapsed(snap, now=1000)
    # 18000 (window) - 14400 (resets_at - now) = 3600 elapsed
    assert elapsed_seconds == 3600
    assert elapsed_percentage is not None
    assert abs(elapsed_percentage - 20.0) < 0.001


def test_derive_elapsed_clamps_to_zero_when_resets_at_in_future_past_window_end() -> None:
    """resets_at - now > window_seconds shouldn't produce negative elapsed (writer drift)."""
    snap = WindowSnapshot(used_percentage=0.0, resets_at=100000, window_seconds=18000)
    elapsed_seconds, elapsed_percentage = derive_elapsed(snap, now=1000)
    assert elapsed_seconds == 0
    assert elapsed_percentage == 0.0


def test_derive_elapsed_clamps_to_window_size_when_resets_at_in_past() -> None:
    """If the window already reset (resets_at < now), elapsed should max out at 100%, not 100+."""
    snap = WindowSnapshot(used_percentage=99.0, resets_at=500, window_seconds=18000)
    elapsed_seconds, elapsed_percentage = derive_elapsed(snap, now=1000)
    assert elapsed_seconds == 18000
    assert elapsed_percentage == 100.0


# =============================================================================
# window_render_dict / build_source_cel_context
# =============================================================================


def test_window_render_dict_surfaces_derived_fields_when_window_seconds_present() -> None:
    """The dict the wait CEL evaluates against must include the derived elapsed_* keys."""
    snap = WindowSnapshot(used_percentage=42.0, resets_at=15400, window_seconds=18000)
    rendered = window_render_dict(snap, now=1000)
    assert rendered["used_percentage"] == 42.0
    assert rendered["resets_at"] == 15400
    assert rendered["window_seconds"] == 18000
    assert rendered["seconds_until_reset"] == 14400
    assert rendered["elapsed_seconds"] == 3600
    assert rendered["elapsed_percentage"] is not None
    assert abs(rendered["elapsed_percentage"] - 20.0) < 0.001
    assert rendered["is_present"] is True


def test_window_render_dict_elapsed_fields_are_none_without_window_seconds() -> None:
    """Overage-style windows (no fixed duration) get the absent-elapsed surface."""
    snap = WindowSnapshot(used_percentage=5.0, resets_at=15400)
    rendered = window_render_dict(snap, now=1000)
    assert rendered["window_seconds"] is None
    assert rendered["elapsed_seconds"] is None
    assert rendered["elapsed_percentage"] is None
    # seconds_until_reset still works -- it only needs resets_at.
    assert rendered["seconds_until_reset"] == 14400


def test_build_source_cel_context_shape_matches_per_source_json() -> None:
    """The CEL context shape MUST match `mngr usage --format json` sources[i] (minus staleness).

    Users prototype predicates with `mngr usage --format json | jq .sources[0]`,
    so any divergence breaks the contract.
    """
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={
            "five_hour": WindowSnapshot(used_percentage=42.0, resets_at=15400, window_seconds=18000, label="5h"),
            "seven_day": WindowSnapshot(used_percentage=11.0, resets_at=600000, window_seconds=604800, label="7d"),
        },
    )
    ctx = build_source_cel_context(snapshot, now=1000)
    assert ctx["source"] == "claude"
    assert ctx["updated_at"] == 900
    assert ctx["five_hour"]["used_percentage"] == 42.0
    assert abs(ctx["five_hour"]["elapsed_percentage"] - 20.0) < 0.001
    assert ctx["seven_day"]["used_percentage"] == 11.0


def test_build_source_cel_context_exposes_per_mode_aggregates_and_sessions() -> None:
    """Cost is split by mode: ``subscription_cost.*`` (imputed) and
    ``api_cost.*`` (real billable spend). The per-session breakdown is
    available via ``sessions[]`` (newest-first); each session carries
    ``cost_mode`` so predicates can filter by mode without re-deriving.
    There is intentionally no combined ``cost`` field.
    """
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=2000,
        windows={},
        sessions=(
            SessionCostRecord(
                session_id="newer-sub",
                cost=CostSnapshot(total_cost_usd=0.42, total_duration_ms=12000),
                cost_mode=CostMode.SUBSCRIPTION,
                first_event_at=1500,
                last_event_at=2000,
            ),
            SessionCostRecord(
                session_id="older-api",
                cost=CostSnapshot(total_cost_usd=1.00, total_duration_ms=5000),
                cost_mode=CostMode.API_KEY,
                first_event_at=900,
                last_event_at=1100,
            ),
        ),
        since_seconds=86400,
    )
    ctx = build_source_cel_context(snapshot, now=2000)
    # Per-mode aggregates -- each only sums sessions of that mode.
    assert ctx["subscription_cost"]["total_cost_usd"] == pytest.approx(0.42)
    assert ctx["subscription_cost"]["total_duration_ms"] == 12000
    assert ctx["api_cost"]["total_cost_usd"] == pytest.approx(1.00)
    assert ctx["api_cost"]["total_duration_ms"] == 5000
    # Per-mode session counts; total session_count is the sum.
    assert ctx["subscription_session_count"] == 1
    assert ctx["api_session_count"] == 1
    assert ctx["session_count"] == 2
    assert ctx["since_seconds"] == 86400
    # No combined cost field: subscription and api cost stay distinct.
    assert "cost" not in ctx
    # sessions[] enumerates every session in the window, newest first, with mode tags.
    assert len(ctx["sessions"]) == 2
    assert ctx["sessions"][0]["session_id"] == "newer-sub"
    assert ctx["sessions"][0]["cost_mode"] == CostMode.SUBSCRIPTION
    assert ctx["sessions"][0]["cost"]["total_cost_usd"] == 0.42
    assert ctx["sessions"][1]["session_id"] == "older-api"
    assert ctx["sessions"][1]["cost_mode"] == CostMode.API_KEY


def test_build_source_cel_context_no_sessions_has_empty_list_and_all_none_aggregates() -> None:
    """When the snapshot has no sessions, ``sessions`` is an empty list and
    both per-mode aggregates have all-None numeric fields."""
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=15400, window_seconds=18000)},
    )
    ctx = build_source_cel_context(snapshot, now=1000)
    assert ctx["sessions"] == []
    assert ctx["session_count"] == 0
    assert ctx["subscription_session_count"] == 0
    assert ctx["api_session_count"] == 0
    # Per-mode aggregates over no sessions are all-None.
    assert ctx["subscription_cost"]["total_cost_usd"] is None
    assert ctx["api_cost"]["total_cost_usd"] is None


def test_build_source_cel_context_raises_when_window_key_collides_with_reserved_field() -> None:
    """A writer-chosen window key that shadows a reserved source-level field must error, not clobber."""
    snapshot = UsageSnapshot(
        source_name="claude",
        updated_at=900,
        windows={"api_cost": WindowSnapshot(used_percentage=42.0, resets_at=15400, window_seconds=18000)},
    )
    with pytest.raises(MngrError, match="api_cost"):
        build_source_cel_context(snapshot, now=1000)


# =============================================================================
# wait_for_usage
# =============================================================================


def _make_snapshot(source: str, used: float, resets_at: int, window_seconds: int = 18000) -> UsageSnapshot:
    return UsageSnapshot(
        source_name=source,
        updated_at=900,
        windows={
            "five_hour": WindowSnapshot(used_percentage=used, resets_at=resets_at, window_seconds=window_seconds),
        },
    )


def _compile_until(filters: Sequence[str]) -> list[object]:
    compiled, _ = compile_cel_filters(filters, exclude_filters=())
    return compiled


def test_wait_for_usage_matches_on_first_poll_when_predicate_already_true() -> None:
    """Predicate true on tick 1 -> exit immediately with is_matched=True."""
    snapshots = [_make_snapshot("claude", used=10.0, resets_at=2000)]
    result = wait_for_usage(
        poll_fn=lambda: snapshots,
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=None,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is True
    assert result.is_timed_out is False
    assert result.matched_source == "claude"
    assert len(result.final_snapshots) == 1


def test_wait_for_usage_polls_until_predicate_flips_true() -> None:
    """Polling loop: predicate false on tick 1, true on tick 2 -> matches on tick 2."""
    call_count = [0]

    def poll_fn() -> list[UsageSnapshot]:
        call_count[0] += 1
        # First poll: used at 80%; second poll: dropped to 10% (e.g. window just reset).
        used = 80.0 if call_count[0] == 1 else 10.0
        return [_make_snapshot("claude", used=used, resets_at=2000)]

    result = wait_for_usage(
        poll_fn=poll_fn,
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=None,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is True
    assert call_count[0] == 2


def test_wait_for_usage_times_out_when_predicate_never_matches() -> None:
    """Timeout: predicate stays false; loop exits with is_timed_out=True after timeout_seconds."""
    snapshots = [_make_snapshot("claude", used=80.0, resets_at=2000)]
    result = wait_for_usage(
        poll_fn=lambda: snapshots,
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=0.02,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is False
    assert result.is_timed_out is True
    assert result.matched_source is None


def test_wait_for_usage_source_predicate_in_cel_excludes_non_matching_sources() -> None:
    """Users scope to one source via CEL: ``source == "claude"``. An opencode
    snapshot that would otherwise satisfy the numeric predicate is ignored."""
    snapshots = [_make_snapshot("opencode", used=10.0, resets_at=2000)]
    result = wait_for_usage(
        poll_fn=lambda: snapshots,
        until_filters=_compile_until(['source == "claude" && five_hour.used_percentage < 50']),
        timeout_seconds=0.02,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is False
    assert result.is_timed_out is True


def test_wait_for_usage_multi_source_any_match_wins() -> None:
    """Two sources, only one matches -> wait succeeds, matched_source identifies which."""
    snapshots = [
        _make_snapshot("opencode", used=80.0, resets_at=2000),
        _make_snapshot("claude", used=10.0, resets_at=2000),
    ]
    result = wait_for_usage(
        poll_fn=lambda: snapshots,
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=None,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is True
    # claude is the matching one; opencode (80%) fails the predicate.
    assert result.matched_source == "claude"


def test_wait_for_usage_no_snapshots_keeps_polling_until_timeout() -> None:
    """No data yet -> not a match; loop times out rather than crashes."""
    result = wait_for_usage(
        poll_fn=lambda: [],
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=0.02,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is False
    assert result.is_timed_out is True
    assert result.matched_source is None
    assert result.final_snapshots == ()


def test_wait_for_usage_handles_poll_error_and_keeps_trying() -> None:
    """Transient host error during poll -> warn and retry; doesn't kill the wait."""
    call_count = [0]

    def poll_fn() -> list[UsageSnapshot]:
        call_count[0] += 1
        if call_count[0] == 1:
            raise MngrError("flaky host")
        return [_make_snapshot("claude", used=10.0, resets_at=2000)]

    result = wait_for_usage(
        poll_fn=poll_fn,
        until_filters=_compile_until(["five_hour.used_percentage < 50"]),
        timeout_seconds=None,
        interval_seconds=0.001,
        now_fn=lambda: 1000,
    )
    assert result.is_matched is True
    # First attempt raised, second succeeded.
    assert call_count[0] == 2


def test_wait_for_usage_elapsed_percentage_predicate() -> None:
    """User's canonical use case: '75% elapsed AND <50% used'.

    Verifies the derived field flows through CEL correctly. At now=14500
    with a 5h window resetting at 15400, elapsed_seconds = 17100 (95% elapsed),
    used_percentage = 40 (< 50), so the predicate matches.
    """
    snapshots = [_make_snapshot("claude", used=40.0, resets_at=15400, window_seconds=18000)]
    result = wait_for_usage(
        poll_fn=lambda: snapshots,
        until_filters=_compile_until(["five_hour.elapsed_percentage > 75.0 && five_hour.used_percentage < 50.0"]),
        timeout_seconds=None,
        interval_seconds=0.001,
        now_fn=lambda: 14500,
    )
    assert result.is_matched is True
    assert result.matched_source == "claude"


# =============================================================================
# Token aggregates + estimated-cost flag surface (mirror-cost-split)
# =============================================================================


def _api_session(
    session_id: str,
    *,
    total_cost_usd: float,
    tokens: TokenSnapshot,
    provenance: CostProvenance,
    last_event_at: int,
) -> SessionCostRecord:
    return SessionCostRecord(
        session_id=session_id,
        cost=CostSnapshot(total_cost_usd=total_cost_usd),
        cost_mode=CostMode.API_KEY,
        tokens=tokens,
        model="anthropic/claude-opus-4-8",
        cost_provenance=provenance,
        first_event_at=last_event_at,
        last_event_at=last_event_at,
    )


def test_cel_context_exposes_token_aggregates_and_estimated_flag() -> None:
    reported = _api_session(
        "r",
        total_cost_usd=1.0,
        tokens=TokenSnapshot(input=100, output=50),
        provenance=CostProvenance.REPORTED,
        last_event_at=1000,
    )
    estimated = _api_session(
        "e",
        total_cost_usd=2.0,
        tokens=TokenSnapshot(input=200, output=100),
        provenance=CostProvenance.ESTIMATED,
        last_event_at=1001,
    )
    snapshot = UsageSnapshot(source_name="codex", updated_at=1001, sessions=(estimated, reported))
    ctx = build_source_cel_context(snapshot, now=2000)

    assert ctx["api_cost"]["total_cost_usd"] == 3.0
    # Any estimated session in the mode flags the whole aggregate as estimated.
    assert ctx["api_cost"]["is_estimated"] is True
    assert ctx["api_tokens"]["input"] == 300 and ctx["api_tokens"]["output"] == 150
    # Newest-first; the estimated session is the fresher one.
    assert ctx["sessions"][0]["cost_provenance"] == CostProvenance.ESTIMATED
    assert ctx["sessions"][0]["tokens"]["input"] == 200
    assert ctx["sessions"][0]["model"] == "anthropic/claude-opus-4-8"


def test_all_reported_sessions_are_not_flagged_estimated() -> None:
    reported = _api_session(
        "r",
        total_cost_usd=5.0,
        tokens=TokenSnapshot(input=10, output=10),
        provenance=CostProvenance.REPORTED,
        last_event_at=1000,
    )
    snapshot = UsageSnapshot(source_name="opencode", updated_at=1000, sessions=(reported,))
    assert snapshot.is_api_cost_estimated is False
    assert build_source_cel_context(snapshot, now=2000)["api_cost"]["is_estimated"] is False


def test_cost_only_source_has_all_none_token_aggregate() -> None:
    # A Claude-style cost-only session (no tokens) -> token aggregate is all-None.
    cost_only = SessionCostRecord(
        session_id="c",
        cost=CostSnapshot(total_cost_usd=1.0),
        cost_mode=CostMode.API_KEY,
        first_event_at=1000,
        last_event_at=1000,
    )
    snapshot = UsageSnapshot(source_name="claude", updated_at=1000, sessions=(cost_only,))
    assert snapshot.api_tokens.input is None
    assert build_source_cel_context(snapshot, now=2000)["api_tokens"]["input"] is None
