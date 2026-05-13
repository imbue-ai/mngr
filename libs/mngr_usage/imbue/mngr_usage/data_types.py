from __future__ import annotations

from typing import overload

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import PluginConfig


class UsagePluginConfig(PluginConfig):
    """Configuration for the usage plugin."""

    max_age_seconds: int = Field(
        default=300,
        description="Snapshot freshness threshold in seconds. When the snapshot's "
        "updated_at is older than this, `mngr usage` prints a stale-snapshot warning. "
        "Reader-only -- this plugin doesn't capture data, it walks events files "
        "produced by writer plugins (one event per provisioned agent's render) and "
        "aggregates them per-source (rate-limit windows reduce freshest-wins; "
        "cost is grouped per session_id and filtered to a recency window).",
    )
    since_seconds: int = Field(
        default=86400,
        description="Recency window for per-session cost aggregation, in seconds. Sessions whose "
        "last event is older than this are excluded from the rendered total. Default is 24h, "
        "which matches the 'how much have I spent today' question; tighten with --since for "
        "right-now views, widen for longer-tail accounting.",
    )

    def merge_with(self, override: PluginConfig) -> UsagePluginConfig:
        """Merge with override config (FrozenModel-style)."""
        if not isinstance(override, UsagePluginConfig):
            return self
        return UsagePluginConfig(
            enabled=override.enabled if override.enabled is not None else self.enabled,
            max_age_seconds=override.max_age_seconds if override.max_age_seconds is not None else self.max_age_seconds,
            since_seconds=override.since_seconds if override.since_seconds is not None else self.since_seconds,
        )


class CostSnapshot(FrozenModel):
    """Session-level cost snapshot supplied by the writer.

    All fields optional and writer-driven; the reader treats them as
    typed-but-passive (no derived values are computed from cost). The
    field names match the Claude Code statusline shape so a writer can
    pass the payload through unchanged, but the model is writer-agnostic
    -- any usage source emitting session cost can populate the same
    fields, and CEL predicates like ``cost.total_cost_usd > 5.0`` apply
    uniformly across sources.

    Note: a ``CostSnapshot`` plays two roles in this model. At
    ``SessionCostRecord.cost`` it represents one session's latest reading.
    At ``UsageSnapshot.cost`` (a computed property) it's the sum of those
    per-session readings across the snapshot's sessions tuple. The same
    type works for both because each numeric field has well-defined sum
    semantics.
    """

    total_cost_usd: float | None = Field(default=None, description="Cumulative session cost in USD (writer-supplied).")
    total_duration_ms: int | None = Field(
        default=None, description="Total wall-clock duration of the session in milliseconds."
    )
    total_api_duration_ms: int | None = Field(
        default=None, description="Total time spent waiting for API responses in milliseconds."
    )
    total_lines_added: int | None = Field(default=None, description="Lines of code added during the session.")
    total_lines_removed: int | None = Field(default=None, description="Lines of code removed during the session.")


class WindowSnapshot(FrozenModel):
    """A single rate-limit window's snapshot state.

    The fields are intentionally generic: any writer that emits per-window
    usage percentages with reset timestamps fits, regardless of which API
    the percentages came from.

    ``label``, when present, is what the human renderer displays before the
    colon (e.g. ``"5h"``). When absent, the renderer falls back to the
    window's key as the label. Writers can use this to give compact display
    names while keeping keys identifier-safe so format templates work.

    ``window_seconds``, when present, declares the window's fixed duration
    in seconds. ``mngr usage`` uses it to derive ``elapsed_seconds`` /
    ``elapsed_percentage`` without baking per-window-class knowledge into
    the reader. Writers should emit it for fixed-duration windows and omit
    it for variable-duration ones (e.g. Claude's overage); when omitted the
    derived elapsed fields are reported as ``None``.

    ``status`` and ``is_using_overage`` are declared as optional fields
    defaulting to None; reserved for forward-compat without a schema bump.
    """

    used_percentage: float | None = Field(default=None)
    resets_at: int | None = Field(default=None, description="Unix timestamp when this window resets")
    window_seconds: int | None = Field(
        default=None,
        description="Window duration in seconds. When present (together with resets_at), enables "
        "the reader to derive elapsed_seconds / elapsed_percentage without baking per-window-class "
        "knowledge into mngr_usage. Writers emit this for fixed-duration windows (five_hour, "
        "seven_day, ...) and omit it for variable-duration windows (e.g. Claude's overage).",
    )
    label: str | None = Field(default=None, description="Human-display label; falls back to the window key.")
    status: str | None = Field(default=None)
    is_using_overage: bool | None = Field(default=None)


class SessionCostRecord(FrozenModel):
    """One session's cost contribution and observation window.

    ``cost`` is this session's **own** contribution -- the delta between
    its final cumulative reading from the writer and the prior session's
    final cumulative reading in the same Claude Code process. The first
    session in a process has its full cumulative reading as ``cost``.
    Summing ``cost`` across all sessions (in all processes, all agents)
    in a recency window recovers the true total spend, even when /clear
    has rotated session_id repeatedly within one process (cost is
    process-cumulative; /clear doesn't reset it).

    ``first_event_at`` / ``last_event_at`` bracket the timestamps of
    events seen for this session and let consumers tell how recent /
    long-running the session is.
    """

    session_id: str = Field(description="Writer-supplied session UUID.")
    cost: CostSnapshot = Field(
        description="This session's own contribution. Delta from the prior session's cumulative "
        "reading in the same Claude Code process; equals the full cumulative reading for the "
        "first session in a process.",
    )
    first_event_at: int = Field(description="Unix timestamp of the earliest event seen for this session.")
    last_event_at: int = Field(description="Unix timestamp of the most recent event seen for this session.")


@overload
def _sum_optional(values: list[int | None]) -> int | None: ...


@overload
def _sum_optional(values: list[float | None]) -> float | None: ...


def _sum_optional(values: list[int | None] | list[float | None]) -> int | float | None:
    """Sum non-None values; return None when all inputs are None.

    Treats None as 'missing data' rather than zero. If any value is present
    we sum the present ones (a None doesn't drag the total to None) -- this
    matches the user-facing 'how much have I spent' question, where one
    session missing a sub-field shouldn't black-hole the whole aggregate.

    Overloaded for ``int`` and ``float`` separately so the aggregate's field
    types match the per-session field types in ``CostSnapshot`` (durations /
    line counts stay int; USD stays float). The runtime implementation is
    type-agnostic; only the static-type surface bifurcates.
    """
    present = [v for v in values if v is not None]
    return sum(present) if present else None


class UsageSnapshot(FrozenModel):
    """A complete usage snapshot derived from one writer's source.

    Aggregates events from every agent writing to this source (e.g. all
    Claude agents share ``source_name="claude"``). The data has two
    distinct shapes inside:

    - ``windows`` is the freshest rate-limit reading across all events for
      this source. Rate limits are an account-level counter (same across
      all agents for one Anthropic account), so freshest-wins is the right
      reduction.
    - ``sessions`` is the per-(session_id) latest cost reading across all
      events for this source, filtered to the recency window passed at
      gather time. Cost is per-session, so we keep one record per session.
      Ordered newest-first by ``last_event_at``.

    Computed views (``cost``, ``session_count``) read off ``sessions`` and
    are recomputed each access -- cheap because the tuple is small.
    """

    source_name: str = Field(description="Writer-chosen source identifier")
    updated_at: int = Field(
        description="Unix timestamp of the freshest event seen for this source. Drives the staleness warning."
    )
    windows: dict[str, WindowSnapshot] = Field(
        default_factory=dict,
        description="Per-window state, keyed by writer-chosen window names (insertion-order preserved). "
        "Reflects the freshest event's rate_limits payload for this source.",
    )
    sessions: tuple[SessionCostRecord, ...] = Field(
        default=(),
        description="Per-session cost records within the recency window, ordered newest-first by last_event_at.",
    )
    since_seconds: int = Field(
        default=0,
        description="Recency window used to filter sessions, in seconds. Echoed back so consumers can "
        "label aggregates with the same window they asked for (e.g. 'total in last 24h').",
    )

    @property
    def cost(self) -> CostSnapshot:
        """Aggregate cost across every session in ``sessions``.

        Sum of each session's own contribution (each ``SessionCostRecord.cost``
        is already a delta against the prior session in the same Claude Code
        process), so this aggregate is the true total spend across all
        sessions in all processes in the recency window. All-None when
        ``sessions`` is empty. CEL predicates like ``cost.total_cost_usd >
        5.0`` work both for per-session and aggregate contexts (same numeric
        semantics, different scope).
        """
        return CostSnapshot(
            total_cost_usd=_sum_optional([s.cost.total_cost_usd for s in self.sessions]),
            total_duration_ms=_sum_optional([s.cost.total_duration_ms for s in self.sessions]),
            total_api_duration_ms=_sum_optional([s.cost.total_api_duration_ms for s in self.sessions]),
            total_lines_added=_sum_optional([s.cost.total_lines_added for s in self.sessions]),
            total_lines_removed=_sum_optional([s.cost.total_lines_removed for s in self.sessions]),
        )

    @property
    def session_count(self) -> int:
        return len(self.sessions)
