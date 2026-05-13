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
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention: each agent's state dir holds rate-limits events at
#   <agent_state_dir>/events/<source>/rate_limits/events.jsonl
# This mirrors the common_transcript pattern used by ``mngr transcript``.
_RATE_LIMITS_SOURCE_SUFFIX = "/rate_limits"
_EVENTS_JSONL_FILENAME = "events.jsonl"


# =============================================================================
# Event parsing
# =============================================================================


@pure
def last_valid_event_from_content(content: str, source_for_warnings: str) -> dict[str, Any] | None:
    """Return the last well-formed JSON object from a JSONL events file's content.

    Walks lines from the end; tolerates a truncated trailing line by skipping
    it and trying the previous one. Returns None if no valid line exists.
    ``source_for_warnings`` is included in any malformed-line warning so the
    user can locate the offending events file.
    """
    for line in reversed([raw for raw in content.splitlines() if raw.strip()]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            # A truncated trailing line (writer mid-flight, no newline yet) is the
            # most common case; skip it and try the previous one. Earlier corrupt
            # lines indicate something worse so we warn for visibility.
            logger.warning("Skipping malformed event line in {}: {}", source_for_warnings, e)
            continue
        if isinstance(event, dict):
            return event
    return None


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


def snapshot_from_event(event: dict[str, Any], source_name: str) -> UsageSnapshot | None:
    """Reshape one events.jsonl line into a UsageSnapshot, or None if unusable.

    A snapshot is built whenever the event contributes *any* renderable
    content -- a window, a cost block, or a session_id. The "no windows
    means unusable" rule that older versions of this reader applied has
    been relaxed: cost-only events (typical for API-key auth, where
    Claude Code never emits rate_limits) still produce a snapshot so
    cost tracking works for those users.
    """
    timestamp = _parse_iso_timestamp(event.get("timestamp"))
    if timestamp is None:
        return None
    windows = _windows_from_event(event)
    cost = _cost_from_event(event)
    session_id = _session_id_from_event(event)
    if not windows and cost is None and session_id is None:
        return None
    return UsageSnapshot(
        source_name=source_name,
        windows=windows,
        updated_at=timestamp,
        session_id=session_id,
        cost=cost,
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
    # ``seconds_until_reset`` is non-negative, so ``window_seconds -
    # seconds_until_reset`` is bounded above by ``window_seconds`` -- only the
    # ``max(0, ...)`` lower-clamp does real work here.
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


def build_source_cel_context(snapshot: UsageSnapshot, now: int) -> dict[str, Any]:
    """Build the per-source dict that ``mngr usage wait``'s ``--until`` CEL filters evaluate against.

    Shape matches one entry of ``mngr usage --format json``'s ``sources``
    array minus the snapshot-level staleness flags (those are CLI ergonomics,
    not predicate ergonomics). Users can prototype a predicate with
    ``mngr usage --format json | jq .sources[0]`` and paste the same field
    paths into ``--until``.

    ``cost`` is always present as a dict (with all-None fields when the
    writer didn't supply cost) so predicates like ``cost.total_cost_usd >
    5.0`` don't have to guard against the field's absence.
    """
    ctx: dict[str, Any] = {
        "source": snapshot.source_name,
        "updated_at": snapshot.updated_at,
        "session_id": snapshot.session_id,
        "cost": (snapshot.cost if snapshot.cost is not None else CostSnapshot()).model_dump(),
    }
    for key, window in snapshot.windows.items():
        ctx[key] = window_render_dict(window, now)
    return ctx


# =============================================================================
# Per-agent reading
# =============================================================================


def _snapshots_for_agent(mngr_ctx: MngrContext, agent: AgentDetails) -> list[UsageSnapshot]:
    """Read all rate-limit snapshots from one agent's events directory.

    Builds an ``EventsTarget`` for the agent (works for local + remote +
    volume-backed hosts), discovers source dirs under ``events/``, and for
    each source matching ``<source>/rate_limits`` reads the last event and
    converts to a ``UsageSnapshot``. Returns ``[]`` if the host has no
    events access, has no rate_limits source, or all events fail to parse.
    """
    target = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent.id,
        agent_name=str(agent.name),
        host_id=agent.host.id,
        provider_name=agent.host.provider_name,
    )
    if target is None:
        return []
    try:
        sources = discover_event_sources(target)
    except MngrError as e:
        logger.debug("Could not discover events for agent {}: {}", agent.name, e)
        return []
    snapshots: list[UsageSnapshot] = []
    for source in sources:
        if not source.source_path.endswith(_RATE_LIMITS_SOURCE_SUFFIX) or not source.is_current_file_present:
            continue
        try:
            content = read_event_content(target, f"{source.source_path}/{_EVENTS_JSONL_FILENAME}")
        except (MngrError, FileNotFoundError) as e:
            logger.debug("Could not read {} for agent {}: {}", source.source_path, agent.name, e)
            continue
        event = last_valid_event_from_content(content, f"agent {agent.name} {source.source_path}")
        if event is None:
            continue
        source_name = source.source_path.removesuffix(_RATE_LIMITS_SOURCE_SUFFIX)
        snapshot = snapshot_from_event(event, source_name)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


class _SnapshotCollector(MutableModel):
    """``list_agents`` on_agent callback that collects per-agent rate-limit snapshots.

    The ``_lock`` here is required, not defensive. ``list_agents(is_streaming=True)``
    fans out across an executor with ``max_workers=32`` per provider AND a nested
    ``max_workers=32`` executor per host within each provider (see ``mngr.api.list``'s
    ``_list_agents_streaming`` and ``_construct_discover_and_emit_for_provider``),
    and ``_collect_and_emit_details_for_host`` invokes ``on_agent`` *outside* the
    results_lock. Multiple host threads can therefore enter ``__call__`` concurrently;
    without the lock, the ``list.extend`` would race.

    Class-based rather than a closure so it can hold its own lock without
    triggering the "no inline functions" ratchet.
    """

    mngr_ctx: MngrContext
    snapshots: list[UsageSnapshot] = []
    _lock: Lock = PrivateAttr(default_factory=Lock)

    model_config = {"arbitrary_types_allowed": True}

    def __call__(self, agent: AgentDetails) -> None:
        agent_snapshots = _snapshots_for_agent(self.mngr_ctx, agent)
        if not agent_snapshots:
            return
        with self._lock:
            self.snapshots.extend(agent_snapshots)


@pure
def collapse_by_source(snapshots: list[UsageSnapshot]) -> list[UsageSnapshot]:
    """Reduce per-agent snapshots to one per ``source_name`` (the freshest).

    Multiple agents may write to the same source (e.g. several Claude agents
    all writing to ``events/claude/rate_limits/events.jsonl`` in their own
    state dirs). Returns the freshest reading per source; order is
    unspecified -- callers re-sort as needed.
    """
    by_source: dict[str, UsageSnapshot] = {}
    for snap in snapshots:
        existing = by_source.get(snap.source_name)
        if existing is None or snap.updated_at > existing.updated_at:
            by_source[snap.source_name] = snap
    return list(by_source.values())


def gather_usage_snapshots(
    mngr_ctx: MngrContext,
    *,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    provider_names: tuple[str, ...] | None = None,
) -> list[UsageSnapshot]:
    """Enumerate matching agents via ``list_agents`` and collect rate-limit snapshots, freshest-per-source.

    Inherits ``mngr list``'s CEL filtering, so e.g. ``--local`` /
    ``--provider local`` / ``--project foo`` work without per-command glue.
    Errors from individual hosts are tolerated so a flaky remote provider
    doesn't crash the whole pass.
    """
    collector = _SnapshotCollector(mngr_ctx=mngr_ctx)
    list_agents(
        mngr_ctx=mngr_ctx,
        is_streaming=True,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=ErrorBehavior.CONTINUE,
        on_agent=collector,
    )
    return collapse_by_source(collector.snapshots)


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

    Per tick: ``poll_fn()`` returns the (already collapsed-by-source)
    snapshot list. For each source, build a CEL context and evaluate
    against ``until_filters``. First passing source wins. If no match
    and not timed out, sleep ``interval_seconds`` and try again.

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
