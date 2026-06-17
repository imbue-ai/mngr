"""Public reader API for ``mngr usage``.

Houses the snapshot pipeline (``gather_usage_snapshots``) and the polling
primitive (``wait_for_usage``) so they can be reused without going through
Click. The CLI command and the ``mngr usage wait`` subcommand both import
from here.

Architectural contract: ``mngr_usage`` is writer-agnostic. The reader
walks events files by path convention and treats window keys
(``five_hour``, ``seven_day``, ...) and source names (``claude``, ...) as
opaque strings supplied by whichever writer plugin produced them. The
only derived knowledge that lives in this module is *how* to compute
``elapsed_seconds`` / ``elapsed_percentage`` *given* a ``window_seconds``
the writer chose to emit -- that's pure arithmetic, not per-writer
knowledge.

Aggregation shape (per source):
- ``windows``: freshest event's rate_limits payload across all agents.
  Rate limits are an account-level counter so freshest-wins is the right
  reduction.
- ``sessions``: per-session own-contribution records across all agents,
  filtered to a recency window (``since_seconds``, default 24h). Each
  agent's stream is partitioned into Claude Code processes via cost-drop
  detection, and within each process every session_id gets a record
  whose ``cost`` is its delta from the prior session's cumulative reading.
  Each record is also tagged with a ``cost_mode``: ``SUBSCRIPTION`` if
  the Claude Code process emitted a non-empty ``rate_limits`` payload at
  any point (Claude.ai Pro/Max auth -- cost is imputed), otherwise
  ``API_KEY`` (direct ANTHROPIC_API_KEY -- cost is real). Aggregates
  on ``UsageSnapshot`` are split by mode (``subscription_cost`` vs
  ``api_cost``) so imputed estimates never get lumped together with
  billable spend.

The reader scans every event line in each agent's events file (not just
the last) so per-session aggregation across the recency window is
correct. Files are bounded in size in practice (<1MB after thousands of
renders), so the linear scan is cheap.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

import pluggy
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.events import discover_event_sources
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import build_cel_context
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostProvenance
from imbue.mngr_usage.data_types import CostSnapshot
from imbue.mngr_usage.data_types import EVENTS_DIR_NAME
from imbue.mngr_usage.data_types import EVENTS_JSONL_FILENAME
from imbue.mngr_usage.data_types import SessionCostRecord
from imbue.mngr_usage.data_types import TokenSnapshot
from imbue.mngr_usage.data_types import USAGE_DIR_NAME
from imbue.mngr_usage.data_types import UsageEvent
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot
from imbue.mngr_usage.data_types import add_optional
from imbue.mngr_usage.data_types import sub_optional
from imbue.mngr_usage.preservation import discover_preserved_agents
from imbue.mngr_usage.pricing import compute_cost

# Discovery convention: each agent's state dir holds usage events at
#   <agent_state_dir>/events/<source>/usage/events.jsonl
# This mirrors the common_transcript pattern used by ``mngr transcript``.
# The path segments themselves are declared once in ``data_types``.
_USAGE_SOURCE_SUFFIX = f"/{USAGE_DIR_NAME}"
_EVENTS_JSONL_FILENAME = EVENTS_JSONL_FILENAME


# =============================================================================
# Event parsing
# =============================================================================


@pure
def parse_events_from_content(content: str, source_for_warnings: str) -> list[UsageEvent]:
    """Parse a JSONL events file's content into typed ``UsageEvent``s.

    Each well-formed JSON object line is parsed (malformed lines skipped with a
    warning -- most commonly a writer mid-flight truncated trailing line) and then
    run through ``parse_usage_events``, which drops events lacking a parseable
    timestamp / session_id. ``source_for_warnings`` is included in the warning so
    the user can locate the offending events file.
    """
    raw_events: list[dict[str, Any]] = []
    for raw in content.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Skipping malformed event line in {}: {}", source_for_warnings, e)
            continue
        if isinstance(event, dict):
            raw_events.append(event)
    return parse_usage_events(raw_events, source_for_warnings)


@pure
def _parse_iso_timestamp(value: Any) -> int | None:
    """Convert an ISO 8601 ``timestamp`` field to a Unix timestamp, or None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


@pure
def _windows_from_event(event: dict[str, Any]) -> dict[str, WindowSnapshot]:
    """Reshape an event's ``rate_limits`` payload into UsageSnapshot windows.

    Window keys and their order are entirely up to the writer; we preserve
    JSONL insertion order. Per-window ``label`` (optional) is what the
    human renderer uses; missing labels fall back to the window key.

    Writer/reader versions are assumed to be lockstep (both live in the
    same monorepo). If a window dict has an unexpected field or a value
    that won't coerce to the typed field, pydantic raises and we drop the
    window with a debug log -- surfaces writer/reader drift rather than
    masking it.
    """
    rate_limits = event.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return {}
    windows: dict[str, WindowSnapshot] = {}
    for window_key, window_value in rate_limits.items():
        if not isinstance(window_value, dict):
            continue
        try:
            windows[str(window_key)] = WindowSnapshot.model_validate(window_value)
        except ValidationError as e:
            logger.debug("Skipping window {}: {}", window_key, e)
    return windows


@pure
def _cost_from_event(event: dict[str, Any]) -> CostSnapshot | None:
    """Reshape an event's ``cost`` payload into a CostSnapshot, or None if absent.

    Cost is writer-supplied and mirrors Claude Code's statusline cost shape.
    ``CostSnapshot`` inherits ``FrozenModel``'s ``extra="forbid"``, so any
    unknown field in the payload raises ``ValidationError``; we catch it and
    return None, dropping the whole cost block rather than just the unknown
    field. Writer/reader are assumed lockstep (same monorepo) -- surfaces
    drift rather than masking it, mirroring the stance ``_windows_from_event``
    takes for window dicts.

    A non-dict ``cost`` value is treated as "no cost data" rather than a
    hard error.
    """
    cost_payload = event.get("cost")
    if not isinstance(cost_payload, dict):
        return None
    try:
        return CostSnapshot.model_validate(cost_payload)
    except ValidationError as e:
        logger.debug("Skipping cost block: {}", e)
        return None


@pure
def _tokens_from_event(event: dict[str, Any]) -> TokenSnapshot | None:
    """Reshape an event's ``tokens`` payload into a TokenSnapshot, or None if absent.

    Mirrors ``_cost_from_event``: a non-dict ``tokens`` is "no token data", and an
    unexpected field raises under ``extra="forbid"`` and is dropped with a debug
    log (surfacing writer/reader drift rather than masking it).
    """
    tokens_payload = event.get("tokens")
    if not isinstance(tokens_payload, dict):
        return None
    try:
        return TokenSnapshot.model_validate(tokens_payload)
    except ValidationError as e:
        logger.debug("Skipping tokens block: {}", e)
        return None


@pure
def _model_from_event(event: dict[str, Any]) -> str | None:
    """Extract the canonical ``<provider>/<model>`` string from the event, or None."""
    model = event.get("model")
    return model if isinstance(model, str) and model else None


@pure
def _cost_mode_hint_from_event(event: dict[str, Any]) -> CostMode | None:
    """Extract a writer-declared ``cost_mode`` hint, or None if absent / unrecognized."""
    hint = event.get("cost_mode")
    if not isinstance(hint, str):
        return None
    try:
        return CostMode(hint)
    except ValueError:
        logger.debug("Ignoring unrecognized cost_mode hint {!r}", hint)
        return None


@pure
def _session_id_from_event(event: dict[str, Any]) -> str | None:
    """Extract a session_id string from the event, or None if absent / unusable."""
    session_id = event.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


@pure
def _str_field_from_event(event: dict[str, Any], key: str) -> str | None:
    """Extract a non-empty string field from the event, or None if absent / unusable."""
    value = event.get(key)
    return value if isinstance(value, str) and value else None


@pure
def _parse_usage_event(raw: dict[str, Any], source_name: str) -> UsageEvent | None:
    """Parse one raw event dict into a ``UsageEvent``, or None to drop it.

    Folds in every per-field extractor. Two drop conditions:

    - an unparseable / absent ``timestamp`` drops the event SILENTLY (matches
      the old ``timed``-list skip -- malformed timestamps are common mid-write);
    - a missing / empty ``session_id`` drops the event with a WARNING (the
      bundled writers always emit it, so a miss is writer/reader drift).

    Malformed cost / tokens / window sub-blocks are individually dropped with a
    ``logger.debug`` inside their extractors rather than dropping the event.
    """
    timestamp_unix = _parse_iso_timestamp(raw.get("timestamp"))
    if timestamp_unix is None:
        return None
    session_id = _session_id_from_event(raw)
    if session_id is None:
        logger.warning(
            "Dropping event without session_id from source {} (event_id={})",
            source_name,
            raw.get("event_id"),
        )
        return None
    return UsageEvent(
        timestamp_unix=timestamp_unix,
        session_id=session_id,
        event_id=_str_field_from_event(raw, "event_id"),
        message_id=_str_field_from_event(raw, "message_id"),
        cost=_cost_from_event(raw),
        tokens=_tokens_from_event(raw),
        windows=_windows_from_event(raw),
        model=_model_from_event(raw),
        cost_mode_hint=_cost_mode_hint_from_event(raw),
    )


@pure
def parse_usage_events(raw_events: list[dict[str, Any]], source_name: str) -> list[UsageEvent]:
    """Parse + validate every raw event dict into a ``UsageEvent``, dropping the unparseable.

    The conversion entry point for callers/tests holding raw dicts (the reader
    boundary calls it via ``parse_events_from_content``). Does NOT sort -- the
    walker preamble (``_sorted_usage_events``) owns that.
    """
    return [event for raw in raw_events if (event := _parse_usage_event(raw, source_name)) is not None]


@pure
def _sorted_usage_events(events: list[UsageEvent]) -> list[UsageEvent]:
    """Sort already-parsed events by timestamp.

    Shared preamble for all three walkers; the parse happened upstream at the read
    boundary. The stable timestamp sort makes a single writer's append-only stream
    monotonic even if concurrent renders arrived out of file order.
    """
    return sorted(events, key=lambda event: event.timestamp_unix)


@pure
def _message_key_for_event(event: UsageEvent) -> str | None:
    """Dedup key for one message within a session: ``message_id``, else ``event_id``.

    A session-incremental writer emits one event per message update carrying a
    ``message_id``; the reader keeps the freshest event per message so re-fires of
    a streaming message (growing cost) collapse to its final reading. Falling back
    to ``event_id`` makes each event its own message when no ``message_id`` is set.
    """
    return event.message_id if event.message_id is not None else event.event_id


# =============================================================================
# Per-source aggregation
#
# Cost-field interpretation: writers emit cost cumulatively for the Claude
# Code process's lifetime -- ``cost.total_cost_usd`` keeps growing on every
# render until the user quits and relaunches (verified empirically; /clear
# does NOT reset it). The reader has to undo this cumulative encoding when
# we want "what did THIS session contribute".
#
# Within one Claude Code process, cost is monotonically non-decreasing
# across events. A downward step (cost_new < cost_prev) is the unambiguous
# signal of a Claude Code process boundary -- only a fresh process starts
# cost back near zero. We use this to partition each agent's event stream
# into processes; within each process we group by session_id and compute
# each session's "own contribution" as the delta from the prior session's
# final cumulative reading. Sum of session contributions across all
# processes is the true total spend.
#
# Why per-agent: each mngr Claude agent runs one Claude Code process at a
# time, with its own cost timeline. Detecting cost drops across agent
# boundaries would fire constantly (each agent's cost is independent), so
# we keep agent identity through to the aggregator and detect boundaries
# only within an agent's own stream.
# =============================================================================


class _AccumulatingSession(MutableModel):
    """Scratchpad for one session being built up within one Claude Code process.

    Tracks first/last event timestamps and the latest cumulative-cost reading
    from the writer. The ``latest_cumulative_cost`` here is the *cumulative*
    process-lifetime reading at this session's last event -- not the session's
    own contribution. The own-contribution delta is computed at process
    finalization (``_finalize_process``) when we have the prior session's
    cumulative baseline to subtract.
    """

    session_id: str
    latest_cumulative_cost: CostSnapshot
    first_event_at: int
    last_event_at: int


@pure
def _cost_delta(current: CostSnapshot, baseline: CostSnapshot) -> CostSnapshot:
    """Element-wise ``current - baseline`` clamped at zero per numeric field.

    See ``sub_optional`` (in ``data_types``) for per-field semantics; this is
    just the CostSnapshot-shaped wrapper used by ``_finalize_process`` to convert
    each session's cumulative reading into its own contribution within the Claude
    Code process.
    """
    return CostSnapshot(
        total_cost_usd=sub_optional(current.total_cost_usd, baseline.total_cost_usd),
        total_duration_ms=sub_optional(current.total_duration_ms, baseline.total_duration_ms),
        total_api_duration_ms=sub_optional(current.total_api_duration_ms, baseline.total_api_duration_ms),
        total_lines_added=sub_optional(current.total_lines_added, baseline.total_lines_added),
        total_lines_removed=sub_optional(current.total_lines_removed, baseline.total_lines_removed),
    )


@pure
def _finalize_process(
    ordered_sessions: list[_AccumulatingSession],
    *,
    cost_mode: CostMode,
) -> list[SessionCostRecord]:
    """Convert one process's accumulated sessions into SessionCostRecord instances.

    Walks ``ordered_sessions`` in temporal order. For each session, the
    record's ``cost`` is the delta between its latest cumulative reading
    and the prior session's latest cumulative reading in the same process
    (baseline = all-None for the first session). This is the session's
    own contribution -- summing ``cost`` across all sessions in this
    process recovers the process's total spend.

    Every record from this process gets the same ``cost_mode`` (auth
    context doesn't change mid-process; a /quit + relaunch under a
    different auth is a new process and gets classified independently).
    """
    records: list[SessionCostRecord] = []
    # All-None baseline (zero-equivalent under sub_optional) for the first session in the process.
    baseline = CostSnapshot()
    for accum in ordered_sessions:
        records.append(
            SessionCostRecord(
                session_id=accum.session_id,
                cost=_cost_delta(accum.latest_cumulative_cost, baseline),
                cost_mode=cost_mode,
                first_event_at=accum.first_event_at,
                last_event_at=accum.last_event_at,
            )
        )
        baseline = accum.latest_cumulative_cost
    return records


class _AgentWalkResult(FrozenModel):
    """Per-agent reduction yielded by ``_walk_agent_events``."""

    max_timestamp: int
    freshest_windows_timestamp: int
    freshest_windows: dict[str, WindowSnapshot]
    session_records: tuple[SessionCostRecord, ...]


@pure
def _classify_process(has_rate_limits: bool) -> CostMode:
    """Map per-process rate_limits presence to a ``CostMode``.

    ``rate_limits`` is emitted only by Claude.ai Pro/Max subscription
    auth (after the first API response of the session). If any event in
    the process carried a non-empty rate_limits payload, the process
    was on subscription auth and its cost is imputed. Otherwise it was
    on a direct API key and its cost is real billable spend.

    Edge case: a subscription process that quits before any API response
    has neither rate_limits nor cost-bearing events, so it produces no
    session records and the misclassification is moot. Once an API
    response lands, rate_limits appears and the process is classified
    correctly thereafter.
    """
    return CostMode.SUBSCRIPTION if has_rate_limits else CostMode.API_KEY


@pure
def _walk_agent_events(events: list[UsageEvent], source_name: str) -> _AgentWalkResult:
    """Walk one agent's events in time order, producing all the per-agent reductions.

    Combines four concerns that all gate on the same "is this event valid"
    check (parseable timestamp + non-empty session_id, enforced by the parser):
      1. Track the freshest rate_limits payload observed (windows).
      2. Track the max event timestamp (for snapshot freshness / staleness).
      3. Partition events into Claude Code processes via cost-drop detection,
         and per process group by session_id with own-contribution deltas.
      4. Classify each process as ``subscription`` or ``api_key`` based on
         whether any of its events carried a non-empty rate_limits payload.

    Events lacking a parseable timestamp or ``session_id`` were already dropped
    upstream at the read boundary by ``parse_usage_events`` (silently / with a
    WARNING respectively), so every ``UsageEvent`` here is valid. Because windows
    are only ever read off a parsed event, the session_id requirement filters
    windows the same way it filters sessions: a malformed event can't contribute
    its windows while having its cost / sessions dropped, which would be a silent
    partial-data trap.
    """
    timed = _sorted_usage_events(events)

    max_timestamp = -1
    freshest_windows_timestamp = -1
    freshest_windows: dict[str, WindowSnapshot] = {}

    finalized: list[SessionCostRecord] = []
    sessions_in_process: dict[str, _AccumulatingSession] = {}
    ordered_in_process: list[_AccumulatingSession] = []
    prev_cumulative_cost_usd: float | None = None
    # Per-process rate_limits presence. Any event in the current process
    # carrying a non-empty windows dict flips this to True; the process is
    # then classified as ``subscription`` at finalization time. Reset on
    # every process boundary so each Claude Code process gets its own
    # auth-mode classification independently.
    current_process_has_rate_limits = False

    for event in timed:
        timestamp = event.timestamp_unix
        session_id = event.session_id

        if timestamp > max_timestamp:
            max_timestamp = timestamp

        windows = event.windows
        if windows and timestamp > freshest_windows_timestamp:
            freshest_windows_timestamp = timestamp
            freshest_windows = windows

        cost = event.cost

        # Process-boundary detection: a cost-bearing event whose cumulative
        # total_cost_usd is strictly below the prior cost-bearing event's is
        # the unambiguous signal of a Claude Code process restart. Cost is
        # monotonic non-decreasing within a process; only relaunches reset
        # it. We don't gate on session_id changing too because a fresh
        # process always rotates session_id anyway -- the cost-drop test is
        # strictly stronger.
        if cost is not None and cost.total_cost_usd is not None:
            if prev_cumulative_cost_usd is not None and cost.total_cost_usd < prev_cumulative_cost_usd:
                finalized.extend(
                    _finalize_process(
                        ordered_in_process,
                        cost_mode=_classify_process(current_process_has_rate_limits),
                    )
                )
                sessions_in_process = {}
                ordered_in_process = []
                current_process_has_rate_limits = False
            prev_cumulative_cost_usd = cost.total_cost_usd

        # Mark the (new or continuing) process as subscription if THIS event
        # carries a rate_limits payload. Done after the boundary check so a
        # subscription event that itself triggered a process restart is
        # attributed to the *new* process (its rate_limits belongs there).
        if windows:
            current_process_has_rate_limits = True

        if cost is None:
            # Rate-limits-only event (no cost). Doesn't contribute to per-
            # session aggregation; windows + mode classification were
            # tracked above.
            continue

        existing = sessions_in_process.get(session_id)
        if existing is None:
            accum = _AccumulatingSession(
                session_id=session_id,
                latest_cumulative_cost=cost,
                first_event_at=timestamp,
                last_event_at=timestamp,
            )
            sessions_in_process[session_id] = accum
            ordered_in_process.append(accum)
            continue
        # Same session within the current process: refresh latest cost reading
        # iff this event is at-or-after the previous; widen the first/last
        # window to bracket all observed events for this session.
        if timestamp >= existing.last_event_at:
            existing.latest_cumulative_cost = cost
            existing.last_event_at = timestamp
        if timestamp < existing.first_event_at:
            existing.first_event_at = timestamp

    # End of agent's stream: finalize the final (still-open) process.
    finalized.extend(
        _finalize_process(
            ordered_in_process,
            cost_mode=_classify_process(current_process_has_rate_limits),
        )
    )

    return _AgentWalkResult(
        max_timestamp=max_timestamp,
        freshest_windows_timestamp=freshest_windows_timestamp,
        freshest_windows=freshest_windows,
        session_records=tuple(finalized),
    )


def aggregate_process_cumulative(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate a **process-cumulative** source (Claude) into a UsageSnapshot.

    For this strategy each agent's cost is one counter that spans multiple
    ``session_id``s (a ``/clear`` rotates the session id without resetting cost),
    so per-agent walks (``_walk_agent_events``) partition the stream into
    processes via cost-drop detection and compute each session's contribution as
    a delta from the prior session's reading within the process. The outer
    combine here reduces per-agent results across agents: freshest-wins for
    windows, max for timestamp, union for session records. Sessions are then
    filtered to the recency window (``last_event_at >= now - since_seconds``) and
    sorted newest-first.

    This is the default strategy a writer plugin's reader hookimpl reuses when
    its harness shares Claude's cumulative model, and the fallback the dispatcher
    uses for an unclaimed source.

    Returns None when no event contributes anything renderable (no
    parseable timestamps, no windows, no cost-bearing sessions in the
    window).
    """
    return _combine_agent_walks(source_name, agents_events, _walk_agent_events, since_seconds=since_seconds, now=now)


@pure
def _resolve_session_cost_and_provenance(
    cost: CostSnapshot | None,
    tokens: TokenSnapshot | None,
    model: str | None,
    *,
    source_name: str,
    session_id: str,
) -> tuple[CostSnapshot, CostProvenance]:
    """Pick a session's dollar figure: prefer a harness-reported cost, else estimate from tokens.

    1. A reported ``total_cost_usd`` wins (provenance ``REPORTED``).
    2. Else, with ``tokens`` + a priced ``model``, derive it (``ESTIMATED``).
    3. Else leave the cost unpriced (all-None ``total_cost_usd``); ``ESTIMATED``
       when tokens are present (we wanted to estimate but couldn't -- WARNING),
       ``REPORTED`` when there's simply nothing to price.
    """
    if cost is not None and cost.total_cost_usd is not None:
        return cost, CostProvenance.REPORTED
    if tokens is not None and model is not None:
        derived = compute_cost(model, tokens)
        if derived is not None:
            return CostSnapshot(total_cost_usd=derived), CostProvenance.ESTIMATED
        logger.warning(
            "Missing pricing for model {!r} (source {}, session {}); cost left unestimated",
            model,
            source_name,
            session_id,
        )
        return CostSnapshot(), CostProvenance.ESTIMATED
    if tokens is not None:
        logger.warning(
            "Session {} (source {}) reports tokens but no model; cannot estimate cost",
            session_id,
            source_name,
        )
        return CostSnapshot(), CostProvenance.ESTIMATED
    return (cost if cost is not None else CostSnapshot()), CostProvenance.REPORTED


class _SessionCumulativeAccumulator(MutableModel):
    """Scratchpad for one session under session-cumulative aggregation.

    Each ``session_id`` is its own counter, so the freshest event's cost/tokens
    is the session's whole contribution -- no cross-session delta is taken.
    """

    session_id: str
    freshest_cost: CostSnapshot | None
    freshest_tokens: TokenSnapshot | None
    model: str | None
    cost_mode_hint: CostMode | None
    has_rate_limits: bool
    first_event_at: int
    last_event_at: int


@pure
def _walk_agent_events_session_cumulative(events: list[UsageEvent], source_name: str) -> _AgentWalkResult:
    """Walk one agent's events treating each ``session_id`` as its own cumulative counter.

    Unlike ``_walk_agent_events`` (process-cumulative), there is no process
    partitioning or cross-session delta: the freshest event per session carries
    that session's whole cumulative reading. Tracks freshest windows and max
    timestamp the same way, and resolves each session's dollar cost + provenance
    (reported vs token-estimated) and mode (writer hint -> rate_limits -> API_KEY)
    at the end.
    """
    timed = _sorted_usage_events(events)

    max_timestamp = -1
    freshest_windows_timestamp = -1
    freshest_windows: dict[str, WindowSnapshot] = {}
    accumulator_by_session: dict[str, _SessionCumulativeAccumulator] = {}
    session_order: list[str] = []

    for event in timed:
        timestamp = event.timestamp_unix
        session_id = event.session_id

        if timestamp > max_timestamp:
            max_timestamp = timestamp
        windows = event.windows
        if windows and timestamp > freshest_windows_timestamp:
            freshest_windows_timestamp = timestamp
            freshest_windows = windows

        cost = event.cost
        tokens = event.tokens
        model = event.model
        hint = event.cost_mode_hint

        accumulator = accumulator_by_session.get(session_id)
        if accumulator is None:
            accumulator_by_session[session_id] = _SessionCumulativeAccumulator(
                session_id=session_id,
                freshest_cost=cost,
                freshest_tokens=tokens,
                model=model,
                cost_mode_hint=hint,
                has_rate_limits=bool(windows),
                first_event_at=timestamp,
                last_event_at=timestamp,
            )
            session_order.append(session_id)
            continue

        # Refresh the freshest reading from at-or-after events; widen the bracket
        # and accumulate the (sticky) rate-limits-seen flag from any event.
        if timestamp >= accumulator.last_event_at:
            if cost is not None:
                accumulator.freshest_cost = cost
            if tokens is not None:
                accumulator.freshest_tokens = tokens
            if model is not None:
                accumulator.model = model
            if hint is not None:
                accumulator.cost_mode_hint = hint
            accumulator.last_event_at = timestamp
        if timestamp < accumulator.first_event_at:
            accumulator.first_event_at = timestamp
        if windows:
            accumulator.has_rate_limits = True

    records: list[SessionCostRecord] = []
    for session_id in session_order:
        accumulator = accumulator_by_session[session_id]
        if accumulator.freshest_cost is None and accumulator.freshest_tokens is None:
            # A windows-only session (no cost and no tokens) contributes no record;
            # its windows + max timestamp were already folded in above.
            continue
        resolved_cost, provenance = _resolve_session_cost_and_provenance(
            accumulator.freshest_cost,
            accumulator.freshest_tokens,
            accumulator.model,
            source_name=source_name,
            session_id=session_id,
        )
        mode = (
            accumulator.cost_mode_hint
            if accumulator.cost_mode_hint is not None
            else (CostMode.SUBSCRIPTION if accumulator.has_rate_limits else CostMode.API_KEY)
        )
        records.append(
            SessionCostRecord(
                session_id=session_id,
                cost=resolved_cost,
                cost_mode=mode,
                tokens=accumulator.freshest_tokens,
                model=accumulator.model,
                cost_provenance=provenance,
                first_event_at=accumulator.first_event_at,
                last_event_at=accumulator.last_event_at,
            )
        )

    return _AgentWalkResult(
        max_timestamp=max_timestamp,
        freshest_windows_timestamp=freshest_windows_timestamp,
        freshest_windows=freshest_windows,
        session_records=tuple(records),
    )


def aggregate_session_cumulative(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate a **session-cumulative** source (Codex) into a UsageSnapshot.

    Each ``session_id`` is its own counter and each event carries the session's
    cumulative-to-date reading, so the freshest reading per session is its whole
    contribution -- no process partitioning, no cross-session delta. Cost is
    preferred from the harness and otherwise estimated from tokens. Same outer
    combine, recency filter, and window handling as the process-cumulative
    strategy.
    """
    return _combine_agent_walks(
        source_name, agents_events, _walk_agent_events_session_cumulative, since_seconds=since_seconds, now=now
    )


@pure
def _sum_token_snapshots(left: TokenSnapshot | None, right: TokenSnapshot | None) -> TokenSnapshot | None:
    """Field-wise sum of two optional TokenSnapshots; None when both are None."""
    if left is None:
        return right
    if right is None:
        return left
    return TokenSnapshot(
        input=add_optional(left.input, right.input),
        output=add_optional(left.output, right.output),
        cache_read=add_optional(left.cache_read, right.cache_read),
        cache_creation=add_optional(left.cache_creation, right.cache_creation),
    )


class _IncrementalSessionAccumulator(MutableModel):
    """Per-session scratchpad for session-incremental aggregation.

    Each ``session_id`` accumulates **per-message** contributions: the freshest
    event per message key is kept (so streaming re-fires collapse), and the
    session total is the sum across messages.
    """

    session_id: str
    # message key -> the freshest parsed event seen for that message
    freshest_event_by_message: dict[str, UsageEvent]
    model: str | None
    model_timestamp: int
    cost_mode_hint: CostMode | None
    has_rate_limits: bool
    first_event_at: int
    last_event_at: int


@pure
def _walk_agent_events_session_incremental(events: list[UsageEvent], source_name: str) -> _AgentWalkResult:
    """Walk one agent's events summing **per-message** contributions within each session.

    For harnesses that report cost/tokens per assistant message (OpenCode, pi):
    each ``session_id``'s total is the sum over its messages, where each message's
    contribution is the freshest event seen for that message key (collapsing
    streaming re-fires). Per-message cost is preferred-reported / else
    token-estimated; the session is ``ESTIMATED`` if any message was estimated.
    Windows and max timestamp are tracked as in the other strategies.
    """
    timed = _sorted_usage_events(events)

    max_timestamp = -1
    freshest_windows_timestamp = -1
    freshest_windows: dict[str, WindowSnapshot] = {}
    accumulator_by_session: dict[str, _IncrementalSessionAccumulator] = {}
    session_order: list[str] = []

    for event in timed:
        timestamp = event.timestamp_unix
        session_id = event.session_id
        message_key = _message_key_for_event(event)
        if message_key is None:
            continue

        if timestamp > max_timestamp:
            max_timestamp = timestamp
        windows = event.windows
        if windows and timestamp > freshest_windows_timestamp:
            freshest_windows_timestamp = timestamp
            freshest_windows = windows

        model = event.model
        hint = event.cost_mode_hint

        accumulator = accumulator_by_session.get(session_id)
        if accumulator is None:
            accumulator = _IncrementalSessionAccumulator(
                session_id=session_id,
                freshest_event_by_message={},
                model=None,
                model_timestamp=-1,
                cost_mode_hint=None,
                has_rate_limits=False,
                first_event_at=timestamp,
                last_event_at=timestamp,
            )
            accumulator_by_session[session_id] = accumulator
            session_order.append(session_id)

        existing = accumulator.freshest_event_by_message.get(message_key)
        if existing is None or timestamp >= existing.timestamp_unix:
            accumulator.freshest_event_by_message[message_key] = event
        accumulator.first_event_at = min(accumulator.first_event_at, timestamp)
        accumulator.last_event_at = max(accumulator.last_event_at, timestamp)
        if windows:
            accumulator.has_rate_limits = True
        if hint is not None:
            accumulator.cost_mode_hint = hint
        if model is not None and timestamp >= accumulator.model_timestamp:
            accumulator.model = model
            accumulator.model_timestamp = timestamp

    records: list[SessionCostRecord] = []
    for session_id in session_order:
        accumulator = accumulator_by_session[session_id]
        total_cost_usd: float | None = None
        summed_tokens: TokenSnapshot | None = None
        is_any_estimated = False
        for message_event in accumulator.freshest_event_by_message.values():
            cost = message_event.cost
            tokens = message_event.tokens
            model = message_event.model
            resolved_cost, provenance = _resolve_session_cost_and_provenance(
                cost, tokens, model, source_name=source_name, session_id=session_id
            )
            if resolved_cost.total_cost_usd is not None:
                total_cost_usd = (total_cost_usd or 0.0) + resolved_cost.total_cost_usd
            if provenance == CostProvenance.ESTIMATED:
                is_any_estimated = True
            summed_tokens = _sum_token_snapshots(summed_tokens, tokens)

        if total_cost_usd is None and summed_tokens is None:
            # Every message for this session was windows-only (no cost, no tokens);
            # its windows + timestamps were already folded in -- emit no record.
            continue
        records.append(
            SessionCostRecord(
                session_id=session_id,
                cost=CostSnapshot(total_cost_usd=total_cost_usd),
                cost_mode=(
                    accumulator.cost_mode_hint
                    if accumulator.cost_mode_hint is not None
                    else (CostMode.SUBSCRIPTION if accumulator.has_rate_limits else CostMode.API_KEY)
                ),
                tokens=summed_tokens,
                model=accumulator.model,
                cost_provenance=(CostProvenance.ESTIMATED if is_any_estimated else CostProvenance.REPORTED),
                first_event_at=accumulator.first_event_at,
                last_event_at=accumulator.last_event_at,
            )
        )

    return _AgentWalkResult(
        max_timestamp=max_timestamp,
        freshest_windows_timestamp=freshest_windows_timestamp,
        freshest_windows=freshest_windows,
        session_records=tuple(records),
    )


def aggregate_session_incremental(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate a **session-incremental** source (OpenCode, pi) into a UsageSnapshot.

    Each event reports one message's own cost/tokens (not a cumulative total), so
    a session's contribution is the sum over its messages -- the freshest event
    per message key, summed. Restart-proof: events are append-only and
    self-contained, so the reader recomputes the total from the log each time.
    """
    return _combine_agent_walks(
        source_name, agents_events, _walk_agent_events_session_incremental, since_seconds=since_seconds, now=now
    )


def _combine_agent_walks(
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    walk_agent_events: Callable[[list[UsageEvent], str], _AgentWalkResult],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Reduce per-agent walk results into one source snapshot (shared by both strategies).

    Freshest-wins for windows, max for timestamp, union for session records;
    sessions are then filtered to the recency window and sorted newest-first.
    Returns None when nothing renderable was found.
    """
    cutoff = now - since_seconds
    freshest_windows_timestamp = -1
    freshest_windows: dict[str, WindowSnapshot] = {}
    max_timestamp = -1
    all_sessions: list[SessionCostRecord] = []

    for _agent_id, events in agents_events.items():
        agent_result = walk_agent_events(events, source_name)
        if agent_result.max_timestamp > max_timestamp:
            max_timestamp = agent_result.max_timestamp
        if agent_result.freshest_windows_timestamp > freshest_windows_timestamp:
            freshest_windows_timestamp = agent_result.freshest_windows_timestamp
            freshest_windows = agent_result.freshest_windows
        all_sessions.extend(agent_result.session_records)

    if max_timestamp < 0:
        return None

    in_window = [r for r in all_sessions if r.last_event_at >= cutoff]
    in_window.sort(key=lambda r: r.last_event_at, reverse=True)
    sessions = tuple(in_window)

    if not freshest_windows and not sessions:
        return None

    return UsageSnapshot(
        source_name=source_name,
        updated_at=max_timestamp,
        windows=freshest_windows,
        sessions=sessions,
        since_seconds=since_seconds,
    )


# =============================================================================
# Derived window fields
# =============================================================================


@pure
def window_has_data(window: WindowSnapshot) -> bool:
    """Return True if the window has any writer-supplied content worth rendering.

    A window is considered "present" once the writer has populated either a
    usage percentage or a reset timestamp; both being None means the writer
    emitted a window key without content yet. Centralized here so the JSON
    surface (``window_render_dict``) and the format-template surface
    (``_window_to_template_values``) stay in lockstep.
    """
    return window.used_percentage is not None or window.resets_at is not None


@pure
def derive_elapsed(window: WindowSnapshot, now: int) -> tuple[int | None, float | None]:
    """Compute ``(elapsed_seconds, elapsed_percentage)`` for a window, when derivable.

    Requires both ``window_seconds`` (window class info, from the writer) and
    ``resets_at`` (event-specific). Without ``window_seconds`` the reader has
    no way to know how long the window is, so both derived values are None.
    Clamped to ``[0, window_seconds]`` so a slightly-stale ``resets_at`` that
    leaks past the boundary doesn't produce negative or >100% values.
    """
    if window.window_seconds is None or window.window_seconds <= 0 or window.resets_at is None:
        return None, None
    seconds_until_reset = max(0, window.resets_at - now)
    elapsed_seconds = max(0, window.window_seconds - seconds_until_reset)
    elapsed_percentage = elapsed_seconds / window.window_seconds * 100
    return elapsed_seconds, elapsed_percentage


@pure
def window_render_dict(snap: WindowSnapshot, now: int) -> dict[str, Any]:
    """Window's snapshot fields plus computed seconds_until_reset / elapsed_* / is_present.

    The ``elapsed_seconds`` / ``elapsed_percentage`` fields are derived from
    ``window_seconds`` (when the writer emits it). They're the cleanest way
    to express predicates like "75% of the window has elapsed" without
    callers needing to know that ``five_hour`` == 18000s.
    """
    seconds_until_reset = None if snap.resets_at is None else max(0, snap.resets_at - now)
    elapsed_seconds, elapsed_percentage = derive_elapsed(snap, now)
    return {
        **snap.model_dump(),
        "seconds_until_reset": seconds_until_reset,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_percentage": elapsed_percentage,
        "is_present": window_has_data(snap),
    }


@pure
def session_render_dict(session: SessionCostRecord, now: int) -> dict[str, Any]:
    """Render one session record as a dict for JSON / CEL surfaces.

    Includes ``cost_mode`` so consumers can group / filter sessions by
    auth context (subscription vs api_key) without re-deriving it.
    """
    return {
        "session_id": session.session_id,
        "cost": session.cost.model_dump(),
        "cost_mode": session.cost_mode,
        "cost_provenance": session.cost_provenance,
        "model": session.model,
        "tokens": session.tokens.model_dump() if session.tokens is not None else None,
        "first_event_at": session.first_event_at,
        "last_event_at": session.last_event_at,
        "age_seconds": max(0, now - session.last_event_at),
    }


@pure
def build_source_cel_context(snapshot: UsageSnapshot, now: int) -> dict[str, Any]:
    """Build the per-source dict that ``mngr usage wait``'s ``--until`` CEL filters evaluate against.

    Shape matches one entry of ``mngr usage --format json --detail``'s
    ``sources`` array minus the snapshot-level staleness flags (those are
    CLI ergonomics, not predicate ergonomics). ``--detail`` is the variant
    that mirrors the CEL context: the default JSON omits ``sessions[]`` for
    payload size, but the CEL context always exposes it so predicates like
    ``sessions[0].cost.total_cost_usd > 5`` work. Users can prototype a
    predicate with ``mngr usage --format json --detail | jq .sources[0]``
    and paste the same field paths into ``--until``.

    Cost is split by [[cost-mode]] -- ``subscription_cost`` (imputed by
    Claude Code under a Pro/Max subscription) and ``api_cost`` (real
    spend under a direct ANTHROPIC_API_KEY). There is intentionally no
    combined ``cost`` field: summing imputed and real numbers would be
    misleading. Pick the mode you actually care about, e.g.
    ``api_cost.total_cost_usd > 5.0`` (alert when real billable spend
    crosses $5) or ``subscription_cost.total_cost_usd > 50.0`` (estimate
    you've gotten >$50 of value out of your subscription this window).
    """
    ctx: dict[str, Any] = {
        "source": snapshot.source_name,
        "updated_at": snapshot.updated_at,
        "since_seconds": snapshot.since_seconds,
        # ``is_estimated`` rides inside each cost block: true when any contributing
        # session's dollars were reader-derived from tokens (vs harness-reported).
        "subscription_cost": {
            **snapshot.subscription_cost.model_dump(),
            "is_estimated": snapshot.is_subscription_cost_estimated,
        },
        "subscription_tokens": snapshot.subscription_tokens.model_dump(),
        "subscription_session_count": snapshot.subscription_session_count,
        "api_cost": {
            **snapshot.api_cost.model_dump(),
            "is_estimated": snapshot.is_api_cost_estimated,
        },
        "api_tokens": snapshot.api_tokens.model_dump(),
        "api_session_count": snapshot.api_session_count,
        "session_count": snapshot.session_count,
        "sessions": [session_render_dict(s, now) for s in snapshot.sessions],
    }
    # Window keys are writer-chosen and share the top-level namespace with the
    # reserved source-level fields above. A window key that collides with one
    # would silently clobber it, so reject the collision loudly instead.
    reserved_keys = set(ctx)
    for key, window in snapshot.windows.items():
        if key in reserved_keys:
            raise MngrError(
                f"Window key {key!r} from source {snapshot.source_name!r} collides with a reserved "
                f"source-level CEL field; rename the window key in the writer."
            )
        ctx[key] = window_render_dict(window, now)
    return ctx


# =============================================================================
# Per-agent reading
# =============================================================================


def _events_per_source_for_agent(mngr_ctx: MngrContext, agent: AgentDetails) -> dict[str, list[UsageEvent]]:
    """Read all usage events from one agent's events directory, grouped by source_name.

    Builds an ``EventsTarget`` for the agent (works for local + remote +
    volume-backed hosts), discovers source dirs under ``events/``, and for
    each source matching ``<source>/usage`` parses every event line.
    Returns ``{}`` if the host has no events access, has no usage source,
    or all events fail to parse.
    """
    target = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent.id,
        agent_name=str(agent.name),
        host_id=agent.host.id,
        provider_name=agent.host.provider_name,
    )
    if target is None:
        return {}
    try:
        sources = discover_event_sources(target)
    except MngrError as e:
        logger.debug("Could not discover events for agent {}: {}", agent.name, e)
        return {}
    by_source: dict[str, list[UsageEvent]] = {}
    for source in sources:
        if not source.source_path.endswith(_USAGE_SOURCE_SUFFIX) or not source.is_current_file_present:
            continue
        try:
            content = read_event_content(target, f"{source.source_path}/{_EVENTS_JSONL_FILENAME}")
        except (MngrError, FileNotFoundError) as e:
            logger.debug("Could not read {} for agent {}: {}", source.source_path, agent.name, e)
            continue
        events = parse_events_from_content(content, f"agent {agent.name} {source.source_path}")
        if not events:
            continue
        source_name = source.source_path.removesuffix(_USAGE_SOURCE_SUFFIX)
        by_source.setdefault(source_name, []).extend(events)
    return by_source


class _RawEventsCollector(MutableModel):
    """``list_agents`` on_agent callback that collects raw events grouped by source then agent.

    The two-level grouping (source_name -> agent_id -> events) preserves
    per-agent boundaries so the aggregator can detect Claude Code process
    restarts via cost-drop signals within a single agent's stream. If we
    merged events across agents under one source-keyed list, every
    agent-to-agent transition in the merged stream would falsely register
    as a cost drop (each agent's cost timeline is independent).

    The ``_lock`` here is required, not defensive. ``list_agents(is_streaming=True)``
    fans out across an executor with ``max_workers=32`` per provider AND a nested
    ``max_workers=32`` executor per host within each provider (see ``mngr.api.list``'s
    ``_list_agents_streaming`` and ``_construct_discover_and_emit_for_provider``),
    and ``_collect_and_emit_details_for_host`` invokes ``on_agent`` *outside* the
    results_lock. Multiple host threads can therefore enter ``__call__`` concurrently;
    without the lock, the nested ``setdefault().setdefault().extend`` would race.

    Class-based rather than a closure so it can hold its own lock without
    triggering the "no inline functions" ratchet.
    """

    mngr_ctx: MngrContext
    events_by_source: dict[str, dict[str, list[UsageEvent]]] = {}
    _lock: Lock = PrivateAttr(default_factory=Lock)

    model_config = {"arbitrary_types_allowed": True}

    def __call__(self, agent: AgentDetails) -> None:
        per_source = _events_per_source_for_agent(self.mngr_ctx, agent)
        if not per_source:
            return
        agent_id = str(agent.id)
        with self._lock:
            for source_name, events in per_source.items():
                self.events_by_source.setdefault(source_name, {}).setdefault(agent_id, []).extend(events)


def _preserved_events_per_source(preserved_dir: Path) -> dict[str, list[UsageEvent]]:
    """Read a destroyed agent's preserved usage events, grouped by source_name.

    The preserved layout mirrors the live state dir: usage events live at
    ``<preserved_dir>/events/<source>/usage/events.jsonl``. Preserved files are
    always local, so they're read straight off local disk. Returns ``{}`` when
    the dir holds no usage events.
    """
    events_root = preserved_dir / EVENTS_DIR_NAME
    by_source: dict[str, list[UsageEvent]] = {}
    if not events_root.is_dir():
        return by_source
    for source_dir in sorted(events_root.iterdir()):
        events_file = source_dir / USAGE_DIR_NAME / EVENTS_JSONL_FILENAME
        if not events_file.is_file():
            continue
        try:
            content = events_file.read_text()
        except OSError as e:
            logger.debug("Could not read preserved usage events {}: {}", events_file, e)
            continue
        events = parse_events_from_content(content, f"preserved {source_dir.name}")
        if events:
            by_source[source_dir.name] = events
    return by_source


def _merge_preserved_events(
    mngr_ctx: MngrContext,
    events_by_source: dict[str, dict[str, list[UsageEvent]]],
    *,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
) -> None:
    """Fold destroyed agents' preserved usage events into ``events_by_source`` in place.

    Each preserved agent is filtered with the same provider + CEL predicates as
    live agents (via its preserved data.json). An agent whose id already
    contributed live events is skipped, so a (hypothetical) still-live agent
    that also has a stale preserved copy is never double-counted.
    """
    live_agent_ids = {agent_id for agents in events_by_source.values() for agent_id in agents}
    refs = discover_preserved_agents(
        mngr_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
    )
    for ref in refs:
        if ref.agent_id in live_agent_ids:
            continue
        for source_name, events in _preserved_events_per_source(ref.preserved_dir).items():
            events_by_source.setdefault(source_name, {}).setdefault(ref.agent_id, []).extend(events)


def _dispatch_source_aggregation(
    pm: pluggy.PluginManager,
    source_name: str,
    agents_events: dict[str, list[UsageEvent]],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate one source via the reader hook, falling back to process-cumulative.

    A usage plugin's ``aggregate_usage_source`` hookimpl claims its own source
    (the hook is firstresult); a source no plugin claims aggregates with the
    process-cumulative default. The hookspec itself is contributed by
    ``mngr_usage``'s ``register_hookspecs`` (processed at plugin-load time in both
    production and the test plugin-manager fixture), so it is always present.
    """
    snapshot = pm.hook.aggregate_usage_source(
        source_name=source_name, agents_events=agents_events, since_seconds=since_seconds, now=now
    )
    if snapshot is not None:
        return snapshot
    return aggregate_process_cumulative(source_name, agents_events, since_seconds=since_seconds, now=now)


def _aggregate_via_dispatch(
    pm: pluggy.PluginManager,
    events_by_source: dict[str, dict[str, list[UsageEvent]]],
    *,
    since_seconds: int,
    now: int,
) -> list[UsageSnapshot]:
    """Aggregate every source through the reader-hook dispatch (plugin reader or fallback)."""
    snapshots: list[UsageSnapshot] = []
    for source_name, agents_events in events_by_source.items():
        snapshot = _dispatch_source_aggregation(pm, source_name, agents_events, since_seconds=since_seconds, now=now)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def gather_usage_snapshots(
    mngr_ctx: MngrContext,
    *,
    now: int,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    since_seconds: int,
    include_preserved: bool,
) -> list[UsageSnapshot]:
    """Enumerate matching agents, collect raw events, aggregate per source.

    Inherits ``mngr list``'s CEL filtering, so e.g. ``--local`` /
    ``--provider local`` / ``--project foo`` work without per-command glue.
    Errors from individual hosts are tolerated so a flaky remote provider
    doesn't crash the whole pass.

    ``since_seconds`` is the recency window for per-session aggregation;
    sessions whose last event is older than that are excluded from the
    snapshot's ``sessions`` tuple. The freshest rate-limit windows are
    kept regardless of session recency, since rate limits track the
    underlying account quota's current state.

    When ``include_preserved`` is True, usage events preserved
    from destroyed agents (under ``<local_host_dir>/preserved/``) are folded in
    too, so destroyed agents' spend still counts. They are filtered by the same
    provider / CEL predicates as live agents via their preserved data.json.
    """
    collector = _RawEventsCollector(mngr_ctx=mngr_ctx)
    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=True,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=ErrorBehavior.CONTINUE,
        on_agent=collector,
    )
    if include_preserved:
        _merge_preserved_events(
            mngr_ctx,
            collector.events_by_source,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            provider_names=provider_names,
        )
    return _aggregate_via_dispatch(mngr_ctx.pm, collector.events_by_source, since_seconds=since_seconds, now=now)


# =============================================================================
# Wait primitive
# =============================================================================


class WaitForUsageResult(FrozenModel):
    """Outcome of a ``wait_for_usage`` call.

    Mirrors ``mngr_wait.data_types.WaitResult`` in spirit: a flat record the
    CLI renders directly. ``matched_source`` is the ``source_name`` of the
    first snapshot whose CEL context satisfied all ``--until`` filters; None
    when timed out.
    """

    is_matched: bool = Field(description="True iff all --until filters matched for some source")
    is_timed_out: bool = Field(description="True iff the wait reached --timeout without matching")
    matched_source: str | None = Field(default=None, description="The source_name that matched, when matched")
    elapsed_seconds: float = Field(description="Wall-clock seconds spent in the wait loop")
    final_snapshots: tuple[UsageSnapshot, ...] = Field(
        default=(),
        description="Snapshot list from the last successful poll (whether matching or not). Empty when no poll succeeded.",
    )


def _match_first_source(
    snapshots: Sequence[UsageSnapshot],
    until_filters: Sequence[Any],
    now: int,
) -> str | None:
    """Return the ``source_name`` of the first snapshot whose CEL context satisfies all ``until_filters``.

    Users who want to restrict matching to a specific writer can encode that
    directly in the CEL predicate via the top-level ``source`` field, e.g.
    ``--until 'source == "claude" && five_hour.used_percentage < 50'``.
    """
    for snapshot in snapshots:
        raw_ctx = build_source_cel_context(snapshot, now)
        cel_ctx = build_cel_context(raw_ctx)
        passed = apply_compiled_cel_filters(
            cel_context=cel_ctx,
            include_filters=until_filters,
            exclude_filters=(),
            error_context_description=f"usage source '{snapshot.source_name}'",
        )
        if passed:
            return snapshot.source_name
    return None


def wait_for_usage(
    *,
    poll_fn: Callable[[], list[UsageSnapshot]],
    until_filters: Sequence[Any],
    timeout_seconds: float | None,
    interval_seconds: float,
    now_fn: Callable[[], int],
) -> WaitForUsageResult:
    """Poll until any source's CEL context satisfies all ``until_filters``, or timeout.

    Per tick: ``poll_fn()`` returns the snapshot list. For each source, build a CEL
    context and evaluate against ``until_filters``. First passing source wins. If no
    match and not timed out, sleep ``interval_seconds`` and try again.

    ``timeout_seconds=None`` waits indefinitely. This is a background/daemon wait,
    expected to run until the usage predicate flips -- the deliberate exception to
    the "every wait must set a timeout" rule. That is why this loop owns a real
    ``time.sleep`` (the one sanctioned sleep in this package -- see
    ``test_ratchets``) rather than going through the shared ``poll_until``, which
    requires an explicit timeout on purpose.

    ``now_fn`` supplies the wall-clock seconds fed into each CEL context (so
    time-based filters can be evaluated deterministically in tests). ``time.monotonic``
    drives the timeout/elapsed accounting so a wall-clock skew mid-wait can't confuse it.
    """
    start = time.monotonic()
    last_snapshots: list[UsageSnapshot] = []
    is_waiting = True
    while is_waiting:
        try:
            last_snapshots = poll_fn()
        except (MngrError, OSError) as e:
            # A flaky host shouldn't kill the wait. Polling errors get logged
            # and we try again on the next interval; last_snapshots keeps
            # whatever the previous successful poll produced (or stays
            # empty if no poll has ever succeeded).
            logger.warning("Usage poll failed (will retry): {}", e)
        matched = _match_first_source(last_snapshots, until_filters, now_fn())
        if matched is not None:
            return WaitForUsageResult(
                is_matched=True,
                is_timed_out=False,
                matched_source=matched,
                elapsed_seconds=time.monotonic() - start,
                final_snapshots=tuple(last_snapshots),
            )
        if timeout_seconds is not None and time.monotonic() - start >= timeout_seconds:
            is_waiting = False
        else:
            # Daemon wait: this real sleep is the deliberate per-package exception.
            time.sleep(interval_seconds)
    return WaitForUsageResult(
        is_matched=False,
        is_timed_out=True,
        matched_source=None,
        elapsed_seconds=time.monotonic() - start,
        final_snapshots=tuple(last_snapshots),
    )
