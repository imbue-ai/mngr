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
- ``sessions``: per-(session_id) latest cost reading across all agents,
  filtered to a recency window (``since_seconds``, default 24h).
  Cost is per-session, so we keep one record per session_id.

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
from threading import Lock
from typing import Any

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
from imbue.mngr_usage.data_types import CostSnapshot
from imbue.mngr_usage.data_types import SessionCostRecord
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention: each agent's state dir holds usage events at
#   <agent_state_dir>/events/<source>/usage/events.jsonl
# This mirrors the common_transcript pattern used by ``mngr transcript``.
_USAGE_SOURCE_SUFFIX = "/usage"
_EVENTS_JSONL_FILENAME = "events.jsonl"


# =============================================================================
# Event parsing
# =============================================================================


@pure
def parse_events_from_content(content: str, source_for_warnings: str) -> list[dict[str, Any]]:
    """Return every well-formed JSON object from a JSONL events file's content.

    Skips malformed lines with a warning (most commonly a writer mid-flight
    truncated trailing line); ``source_for_warnings`` is included in the
    warning so the user can locate the offending events file.
    """
    events: list[dict[str, Any]] = []
    for raw in content.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Skipping malformed event line in {}: {}", source_for_warnings, e)
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


@pure
def _parse_iso_timestamp(value: Any) -> int | None:
    """Convert an ISO 8601 ``timestamp`` field to a Unix timestamp, or None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


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


def _cost_from_event(event: dict[str, Any]) -> CostSnapshot | None:
    """Reshape an event's ``cost`` payload into a CostSnapshot, or None if absent.

    Cost is writer-supplied and mirrors Claude Code's statusline cost shape;
    unknown fields are dropped by pydantic's default behavior. A non-dict
    ``cost`` value is treated as "no cost data" rather than a hard error,
    matching the lenient stance we take for windows.
    """
    cost_payload = event.get("cost")
    if not isinstance(cost_payload, dict):
        return None
    try:
        return CostSnapshot.model_validate(cost_payload)
    except ValidationError as e:
        logger.debug("Skipping cost block: {}", e)
        return None


def _session_id_from_event(event: dict[str, Any]) -> str | None:
    """Extract a session_id string from the event, or None if absent / unusable."""
    session_id = event.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


# =============================================================================
# Per-source aggregation
# =============================================================================


class _SessionAccumulator(MutableModel):
    """Per-session running state during aggregation.

    Tracks first/last event timestamps and the most recent cost reading.
    Mutable on purpose: the aggregator updates one of these per event, then
    freezes them into ``SessionCostRecord`` instances at the end. Using a
    mutable scratchpad here avoids constructing N immutable copies as we
    walk a long events file.
    """

    session_id: str
    cost: CostSnapshot
    first_event_at: int
    last_event_at: int

    def to_record(self) -> SessionCostRecord:
        return SessionCostRecord(
            session_id=self.session_id,
            cost=self.cost,
            first_event_at=self.first_event_at,
            last_event_at=self.last_event_at,
        )


def _build_snapshot_for_source(
    source_name: str,
    events: list[dict[str, Any]],
    *,
    since_seconds: int,
    now: int,
) -> UsageSnapshot | None:
    """Aggregate one source's parsed events into a UsageSnapshot.

    Single pass over the events list (caller already parsed JSON):
    - Windows: keep the freshest event's rate_limits payload (the rate-limit
      counter is account-level, so freshest-wins is the right reduction
      across agents that share an account).
    - Sessions: build a SessionCostRecord per session_id, updating its
      ``cost`` to whichever event has the latest timestamp for that
      session and tracking ``first_event_at`` / ``last_event_at``.
      Filtered to ``last_event_at >= now - since_seconds`` and sorted
      newest-first.

    Returns None when no event contributes anything renderable (no
    parseable timestamps, no windows, no cost-bearing sessions in the
    window).
    """
    cutoff = now - since_seconds
    session_accumulators: dict[str, _SessionAccumulator] = {}
    freshest_windows_timestamp = -1
    freshest_windows: dict[str, WindowSnapshot] = {}
    max_timestamp = -1

    for event in events:
        timestamp = _parse_iso_timestamp(event.get("timestamp"))
        if timestamp is None:
            continue
        if timestamp > max_timestamp:
            max_timestamp = timestamp

        windows = _windows_from_event(event)
        if windows and timestamp > freshest_windows_timestamp:
            freshest_windows_timestamp = timestamp
            freshest_windows = windows

        session_id = _session_id_from_event(event)
        cost = _cost_from_event(event)
        if session_id is None or cost is None:
            continue

        existing = session_accumulators.get(session_id)
        if existing is None:
            session_accumulators[session_id] = _SessionAccumulator(
                session_id=session_id,
                cost=cost,
                first_event_at=timestamp,
                last_event_at=timestamp,
            )
            continue
        # Cost reading wins by timestamp; first/last bracket all observed events for this session.
        if timestamp >= existing.last_event_at:
            existing.cost = cost
            existing.last_event_at = timestamp
        if timestamp < existing.first_event_at:
            existing.first_event_at = timestamp

    if max_timestamp < 0:
        return None

    in_window = [a for a in session_accumulators.values() if a.last_event_at >= cutoff]
    in_window.sort(key=lambda a: a.last_event_at, reverse=True)
    sessions = tuple(a.to_record() for a in in_window)

    if not freshest_windows and not sessions:
        return None

    return UsageSnapshot(
        source_name=source_name,
        updated_at=max_timestamp,
        windows=freshest_windows,
        sessions=sessions,
        since_seconds=since_seconds,
    )


@pure
def aggregate_events_to_snapshots(
    events_by_source: dict[str, list[dict[str, Any]]],
    *,
    since_seconds: int,
    now: int,
) -> list[UsageSnapshot]:
    """Build a UsageSnapshot per source from already-parsed events."""
    snapshots: list[UsageSnapshot] = []
    for source_name, events in events_by_source.items():
        snapshot = _build_snapshot_for_source(source_name, events, since_seconds=since_seconds, now=now)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


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


def session_render_dict(session: SessionCostRecord, now: int) -> dict[str, Any]:
    """Render one session record as a dict for JSON / CEL surfaces."""
    return {
        "session_id": session.session_id,
        "cost": session.cost.model_dump(),
        "first_event_at": session.first_event_at,
        "last_event_at": session.last_event_at,
        "age_seconds": max(0, now - session.last_event_at),
    }


def empty_current_session_dict() -> dict[str, Any]:
    """Default current_session shape when no sessions are present.

    Keeps the CEL/JSON surface stable: ``current_session.cost.total_cost_usd``
    is always a queryable path, even when the source has no recent sessions.
    """
    return {
        "session_id": None,
        "cost": CostSnapshot().model_dump(),
        "first_event_at": None,
        "last_event_at": None,
        "age_seconds": None,
    }


def build_source_cel_context(snapshot: UsageSnapshot, now: int) -> dict[str, Any]:
    """Build the per-source dict that ``mngr usage wait``'s ``--until`` CEL filters evaluate against.

    Shape matches one entry of ``mngr usage --format json``'s ``sources``
    array minus the snapshot-level staleness flags (those are CLI ergonomics,
    not predicate ergonomics). Users can prototype a predicate with
    ``mngr usage --format json | jq .sources[0]`` and paste the same field
    paths into ``--until``.

    Top-level ``cost`` is the **aggregate** across the snapshot's sessions
    (sum of each numeric field), so predicates like ``cost.total_cost_usd
    > 5.0`` mean 'I've spent more than $5 across recent sessions'. To
    predicate on the most recent session specifically, use the
    ``current_session.*`` paths (e.g. ``current_session.cost.total_cost_usd``).
    ``current_session`` is always a dict -- it has None-valued fields when
    no sessions are present -- so CEL paths don't need to guard for absence.
    """
    current_session = snapshot.current_session
    ctx: dict[str, Any] = {
        "source": snapshot.source_name,
        "updated_at": snapshot.updated_at,
        "since_seconds": snapshot.since_seconds,
        "cost": snapshot.cost.model_dump(),
        "session_count": snapshot.session_count,
        "current_session": (
            session_render_dict(current_session, now) if current_session is not None else empty_current_session_dict()
        ),
        "sessions": [session_render_dict(s, now) for s in snapshot.sessions],
    }
    for key, window in snapshot.windows.items():
        ctx[key] = window_render_dict(window, now)
    return ctx


# =============================================================================
# Per-agent reading
# =============================================================================


def _events_per_source_for_agent(mngr_ctx: MngrContext, agent: AgentDetails) -> dict[str, list[dict[str, Any]]]:
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
    by_source: dict[str, list[dict[str, Any]]] = {}
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
    """``list_agents`` on_agent callback that collects raw events grouped by source_name.

    The ``_lock`` here is required, not defensive. ``list_agents(is_streaming=True)``
    fans out across an executor with ``max_workers=32`` per provider AND a nested
    ``max_workers=32`` executor per host within each provider (see ``mngr.api.list``'s
    ``_list_agents_streaming`` and ``_construct_discover_and_emit_for_provider``),
    and ``_collect_and_emit_details_for_host`` invokes ``on_agent`` *outside* the
    results_lock. Multiple host threads can therefore enter ``__call__`` concurrently;
    without the lock, the ``dict.setdefault().extend`` would race.

    Class-based rather than a closure so it can hold its own lock without
    triggering the "no inline functions" ratchet.
    """

    mngr_ctx: MngrContext
    events_by_source: dict[str, list[dict[str, Any]]] = {}
    _lock: Lock = PrivateAttr(default_factory=Lock)

    model_config = {"arbitrary_types_allowed": True}

    def __call__(self, agent: AgentDetails) -> None:
        per_source = _events_per_source_for_agent(self.mngr_ctx, agent)
        if not per_source:
            return
        with self._lock:
            for source_name, events in per_source.items():
                self.events_by_source.setdefault(source_name, []).extend(events)


def gather_usage_snapshots(
    mngr_ctx: MngrContext,
    *,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    provider_names: tuple[str, ...] | None = None,
    since_seconds: int = 86400,
    now: int | None = None,
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
    """
    if now is None:
        now = int(time.time())
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
    return aggregate_events_to_snapshots(collector.events_by_source, since_seconds=since_seconds, now=now)


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
    now_fn: Callable[[], int] = lambda: int(time.time()),
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> WaitForUsageResult:
    """Poll until any source's CEL context satisfies all ``until_filters``, or timeout.

    Per tick: ``poll_fn()`` returns the snapshot list. For each source,
    build a CEL context and evaluate against ``until_filters``. First
    passing source wins. If no match and not timed out, sleep
    ``interval_seconds`` and try again.

    Clock and sleep are injected so tests can run the loop fast without
    real time elapsing. The default callable for ``now_fn`` reads
    wall-clock; ``monotonic_fn`` is used only for the timeout/elapsed
    measurement so a wall-clock skew during the wait doesn't confuse
    timeout accounting.
    """
    start = monotonic_fn()
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
                elapsed_seconds=monotonic_fn() - start,
                final_snapshots=tuple(last_snapshots),
            )
        elapsed = monotonic_fn() - start
        if timeout_seconds is not None and elapsed >= timeout_seconds:
            is_waiting = False
        else:
            sleep_fn(interval_seconds)
    return WaitForUsageResult(
        is_matched=False,
        is_timed_out=True,
        matched_source=None,
        elapsed_seconds=monotonic_fn() - start,
        final_snapshots=tuple(last_snapshots),
    )
