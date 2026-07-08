from __future__ import annotations

from enum import auto
from typing import overload

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import PluginConfig

# Discovery convention shared by the reader (``api``) and the destroy-time
# preservation (``preservation``): each agent's state dir holds usage events at
#   <agent_state_dir>/events/<source>/usage/events.jsonl
# Centralized here so the path structure is declared exactly once.
EVENTS_DIR_NAME = "events"
USAGE_DIR_NAME = "usage"
EVENTS_JSONL_FILENAME = "events.jsonl"


class CostMode(UpperCaseStrEnum):
    """How the writer's ``cost.total_cost_usd`` reading was computed.

    - ``SUBSCRIPTION``: the agent's Claude Code process was running under
      a Claude.ai Pro/Max subscription. Cost is **imputed** by Claude
      Code (what the same usage would have cost on the metered API); the
      user actually pays a flat subscription, so this number is
      informational rather than billable.
    - ``API_KEY``: the process was running with a direct
      ``ANTHROPIC_API_KEY``. Cost is the **real** API spend that will
      appear on the bill.

    The reader detects mode per Claude Code process: a process is
    classified ``SUBSCRIPTION`` if **any** event in it carries a
    non-empty ``rate_limits`` payload (rate_limits is emitted only for
    subscription auth, after the first API response); otherwise it's
    ``API_KEY``. Mixing the two aggregates would conflate imputed
    estimates with real spend, so ``UsageSnapshot`` keeps them in
    separate ``subscription_cost`` and ``api_cost`` fields.
    """

    SUBSCRIPTION = auto()
    API_KEY = auto()


class CostProvenance(UpperCaseStrEnum):
    """How a session's ``total_cost_usd`` was obtained.

    - ``REPORTED``: the harness supplied the dollar figure directly (Claude
      Code's statusline, OpenCode's per-message cost, pi's client-side cost).
    - ``ESTIMATED``: the reader derived it from token counts via the pricing
      table (a token-only source like Codex, or a provider where the harness
      reports no cost).

    Orthogonal to [[cost-mode]] (who pays / whether it's billable). The reader
    prefers a ``REPORTED`` figure and only falls back to ``ESTIMATED``.
    """

    REPORTED = auto()
    ESTIMATED = auto()


class UsagePluginConfig(PluginConfig):
    """Configuration for the usage plugin."""

    stale_after_seconds: int = Field(
        default=300,
        description="Snapshot freshness threshold in seconds. When the snapshot's "
        "updated_at is older than this, `mngr usage` prints a stale-snapshot warning. "
        "Display warning only -- it does not change which events are aggregated. "
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
    preserve_on_destroy: bool = Field(
        default=True,
        description="Preserve each agent's usage events locally before its state directory is "
        "deleted on destroy, so destroyed agents' spend still counts in `mngr usage`. When enabled, "
        "any agent's events/<source>/usage directories (plus its data.json, for filtering) are copied "
        "to <local_host_dir>/preserved/<agent-name>--<agent-id>/, mirroring the agent's state-directory "
        "layout. For remote agents, files are pulled to the local machine so they survive host "
        "destruction. `mngr usage` reads these back by default (opt out with --no-preserved). "
        "Set to False to discard usage data on destroy.",
    )


class CostSnapshot(FrozenModel):
    """Session-level cost snapshot supplied by the writer.

    All fields optional and writer-driven; the reader treats them as
    typed-but-passive (no derived values are computed from cost). The
    field names match the Claude Code statusline shape so a writer can
    pass the payload through unchanged, but the model is writer-agnostic
    -- any usage source emitting session cost can populate the same
    fields, and CEL predicates like ``api_cost.total_cost_usd > 5.0``
    apply uniformly across sources.

    Note: a ``CostSnapshot`` plays two roles in this model. At
    ``SessionCostRecord.cost`` it represents one session's own-contribution
    delta (the cumulative reading at this session's last event minus the
    prior session's cumulative reading in the same Claude Code process).
    At ``UsageSnapshot.subscription_cost`` / ``UsageSnapshot.api_cost``
    (computed properties) it's the sum of per-session contributions
    across the sessions of one [[cost-mode]] in the recency window,
    recovering the true mode-scoped total spend. The same type works for
    both because each numeric field has well-defined sum semantics.
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


class TokenSnapshot(FrozenModel):
    """Session-level token counts supplied by a writer that reports usage in tokens.

    All fields optional and writer-driven, mirroring ``CostSnapshot``'s
    passive-but-typed stance. The buckets are **non-overlapping**: ``input`` is
    the non-cached input count, and ``cache_read`` / ``cache_creation`` are
    separate, so the dollar cost is exactly
    ``input*p_in + cache_read*p_cr + cache_creation*p_cw + output*p_out`` with no
    double-counting (see :func:`imbue.mngr_usage.pricing.compute_cost`). Writers
    whose source reports ``input`` inclusive of cache normalize it to the
    non-cached count before emitting. ``output`` includes any reasoning tokens
    (billed at the output rate).

    Like ``CostSnapshot``, a ``TokenSnapshot`` plays two roles: one session's own
    contribution at ``SessionCostRecord.tokens``, and the per-mode sum at
    ``UsageSnapshot``'s token aggregates. Each field sums cleanly, so the same
    type serves both.
    """

    input: int | None = Field(default=None, description="Non-cached input tokens (cache buckets are separate).")
    output: int | None = Field(default=None, description="Output tokens, inclusive of reasoning tokens.")
    cache_read: int | None = Field(default=None, description="Input tokens served from the prompt cache (read).")
    cache_creation: int | None = Field(
        default=None, description="Input tokens written to the prompt cache (creation/write)."
    )


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
    of the same [[cost-mode]] in a recency window recovers the true
    total spend for that mode, even when /clear has rotated session_id
    repeatedly within one process (cost is process-cumulative; /clear
    doesn't reset it).

    ``cost_mode`` carries the auth context: ``SUBSCRIPTION`` if the
    Claude Code process this session ran in was on a Claude.ai Pro/Max
    subscription (cost is imputed, not billed), or ``API_KEY`` if it
    was on a direct ``ANTHROPIC_API_KEY`` (cost is real). Consumers MUST
    keep these separated: see ``UsageSnapshot.subscription_cost`` and
    ``UsageSnapshot.api_cost`` for the aggregate split.

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
    cost_mode: CostMode = Field(
        description="``SUBSCRIPTION`` if the Claude Code process this session ran in had a "
        "rate_limits payload (Claude.ai Pro/Max subscription; cost is imputed), else ``API_KEY`` "
        "(direct ANTHROPIC_API_KEY; cost is real). Determines which aggregate this session feeds "
        "into on UsageSnapshot."
    )
    tokens: TokenSnapshot | None = Field(
        default=None,
        description="This session's own token contribution, when the source reports tokens; "
        "None for cost-only sources (e.g. Claude).",
    )
    model: str | None = Field(
        default=None,
        description="Canonical '<provider>/<model>' the tokens were billed against; drives "
        "token->cost derivation. None when the source reports cost directly or omits the model.",
    )
    cost_provenance: CostProvenance = Field(
        default=CostProvenance.REPORTED,
        description="Whether ``cost`` was reported by the harness (default) or estimated by the "
        "reader from ``tokens`` via the pricing table.",
    )
    first_event_at: int = Field(description="Unix timestamp of the earliest event seen for this session.")
    last_event_at: int = Field(description="Unix timestamp of the most recent event seen for this session.")


class UsageEvent(FrozenModel):
    """One raw event dict parsed and validated into the shape the walkers consume.

    Produced by ``_parse_usage_event`` (in ``api``); a parsed event always has a
    usable ``timestamp_unix`` and non-empty ``session_id`` (both are drop
    conditions in the parser). The walkers read every other field off this typed
    surface instead of re-doing ``event.get(...)`` + ``isinstance`` guards, which
    keeps the cross-strategy invariant intact: windows can only be read off a
    parsed event, so the session_id requirement filters windows exactly as it
    filters sessions.
    """

    timestamp_unix: int = Field(description="Event timestamp as a Unix timestamp.")
    session_id: str = Field(description="Writer-supplied session id (always non-empty on a parsed event).")
    event_id: str | None = Field(default=None, description="Per-event id, when the writer emitted one.")
    message_id: str | None = Field(default=None, description="Per-message id for session-incremental sources.")
    cost: CostSnapshot | None = Field(default=None, description="Parsed cost block, or None when absent/malformed.")
    tokens: TokenSnapshot | None = Field(
        default=None, description="Parsed tokens block, or None when absent/malformed."
    )
    windows: dict[str, WindowSnapshot] = Field(
        default_factory=dict, description="Parsed rate_limits payload (empty when absent)."
    )
    model: str | None = Field(default=None, description="Canonical '<provider>/<model>', or None.")
    cost_mode_hint: CostMode | None = Field(default=None, description="Writer-declared cost_mode hint, or None.")


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


@overload
def add_optional(left: int | None, right: int | None) -> int | None: ...


@overload
def add_optional(left: float | None, right: float | None) -> float | None: ...


@pure
def add_optional(left: int | float | None, right: int | float | None) -> int | float | None:
    """Add two optional numbers, treating None as 'missing' (None + None stays None).

    A present value on either side wins (a None on the other side counts as
    zero), matching ``_sum_optional``'s 'how much have I spent' stance where a
    single missing sub-field doesn't black-hole the whole sum.
    """
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


@overload
def sub_optional(current: int | None, baseline: int | None) -> int | None: ...


@overload
def sub_optional(current: float | None, baseline: float | None) -> float | None: ...


@pure
def sub_optional(current: int | float | None, baseline: int | float | None) -> int | float | None:
    """Single-field clamped subtraction for cost-delta computation.

    Treats a missing baseline as zero so the first reading in a series just
    gets its own value. A missing current field stays None (no delta to
    report). Negative deltas are clamped to zero defensively -- they shouldn't
    occur within a cumulative series but if a writer ever emits an out-of-order
    pair we'd rather show 0 than a misleading negative spend.

    Distinct from ``add_optional`` because subtraction clamps at zero and treats
    a None ``current`` as 'no delta', not as a zero operand.
    """
    if current is None:
        return None
    delta = current - (baseline if baseline is not None else 0)
    return delta if delta >= 0 else type(current)(0)


def _aggregate_cost(records: tuple[SessionCostRecord, ...]) -> CostSnapshot:
    """Sum each numeric field across ``records``'s ``cost`` snapshots.

    Field-wise ``_sum_optional`` so a single missing field on one session
    doesn't drag the whole aggregate to None. Returns an all-None
    ``CostSnapshot`` when ``records`` is empty -- ``model_dump`` of that
    still produces a dict with every key set to None, which keeps the JSON
    / template surfaces stable for consumers.
    """
    return CostSnapshot(
        total_cost_usd=_sum_optional([s.cost.total_cost_usd for s in records]),
        total_duration_ms=_sum_optional([s.cost.total_duration_ms for s in records]),
        total_api_duration_ms=_sum_optional([s.cost.total_api_duration_ms for s in records]),
        total_lines_added=_sum_optional([s.cost.total_lines_added for s in records]),
        total_lines_removed=_sum_optional([s.cost.total_lines_removed for s in records]),
    )


def _aggregate_tokens(records: tuple[SessionCostRecord, ...]) -> TokenSnapshot:
    """Sum each token field across ``records``'s ``tokens`` (a None record contributes nothing).

    All-None ``TokenSnapshot`` when no record carries tokens -- e.g. a cost-only
    source like Claude -- so the JSON / CEL surface stays shape-stable.
    """
    return TokenSnapshot(
        input=_sum_optional([s.tokens.input if s.tokens is not None else None for s in records]),
        output=_sum_optional([s.tokens.output if s.tokens is not None else None for s in records]),
        cache_read=_sum_optional([s.tokens.cache_read if s.tokens is not None else None for s in records]),
        cache_creation=_sum_optional([s.tokens.cache_creation if s.tokens is not None else None for s in records]),
    )


class UsageSnapshot(FrozenModel):
    """A complete usage snapshot derived from one writer's source.

    Aggregates events from every agent writing to this source (e.g. all
    Claude agents share ``source_name="claude"``). The data has two
    distinct shapes inside:

    - ``windows`` is the freshest rate-limit reading across all events for
      this source. Rate limits are an account-level counter (same across
      all agents for one Anthropic account), so freshest-wins is the right
      reduction.
    - ``sessions`` is a tuple of per-session own-contribution records.
      Each record's ``cost`` is the delta between this session's final
      cumulative reading and the prior session's final cumulative reading
      within the same Claude Code process (see ``SessionCostRecord``).
      Filtered to the recency window passed at gather time and ordered
      newest-first by ``last_event_at``. Records are keyed per (agent,
      process); the same ``session_id`` can appear more than once when
      /resume carries a session across a Claude Code process restart.

    Cost is split by [[cost-mode]]: ``subscription_cost`` aggregates
    sessions whose Claude Code process was on a Claude.ai subscription
    (imputed numbers), and ``api_cost`` aggregates sessions whose process
    was on a direct API key (real spend). Lumping the two together would
    conflate informational estimates with billable spend, so we never
    surface a single combined ``cost`` -- consumers should pick the mode
    they care about (or both, if comparing).

    Computed views (``subscription_cost``, ``api_cost``,
    ``subscription_session_count``, ``api_session_count``,
    ``session_count``) read off ``sessions`` and are recomputed each
    access -- cheap because the tuple is small.
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
        description="Per-session cost records within the recency window, ordered newest-first by last_event_at. "
        "Each record carries a ``cost_mode`` tag so subscription (imputed) and api_key (real) sessions stay "
        "distinguishable downstream.",
    )
    since_seconds: int = Field(
        default=0,
        description="Recency window used to filter sessions, in seconds. Echoed back so consumers can "
        "label aggregates with the same window they asked for (e.g. 'total in last 24h').",
    )

    @property
    def subscription_sessions(self) -> tuple[SessionCostRecord, ...]:
        """Sessions whose Claude Code process was on a subscription. Cost here is imputed by Claude Code."""
        return tuple(s for s in self.sessions if s.cost_mode == CostMode.SUBSCRIPTION)

    @property
    def api_sessions(self) -> tuple[SessionCostRecord, ...]:
        """Sessions whose Claude Code process was on a direct API key. Cost here is real billable spend."""
        return tuple(s for s in self.sessions if s.cost_mode == CostMode.API_KEY)

    @property
    def subscription_cost(self) -> CostSnapshot:
        """Aggregate cost across subscription-mode sessions only.

        Subscription cost is **imputed** by Claude Code (what the usage
        would have cost on the metered API). Users actually pay a flat
        subscription, so this is informational. Kept separate from
        ``api_cost`` so consumers don't conflate estimates with billable
        spend. All-None when there are no subscription sessions.
        """
        return _aggregate_cost(self.subscription_sessions)

    @property
    def api_cost(self) -> CostSnapshot:
        """Aggregate cost across api_key-mode sessions only.

        API-key cost is **real**: it tracks what the user's
        ``ANTHROPIC_API_KEY`` will actually be billed. Kept separate from
        ``subscription_cost`` so consumers don't accidentally sum imputed
        and real costs. All-None when there are no api_key sessions.
        """
        return _aggregate_cost(self.api_sessions)

    @property
    def subscription_tokens(self) -> TokenSnapshot:
        """Aggregate token counts across subscription-mode sessions (all-None for cost-only sources)."""
        return _aggregate_tokens(self.subscription_sessions)

    @property
    def api_tokens(self) -> TokenSnapshot:
        """Aggregate token counts across api_key-mode sessions (all-None for cost-only sources)."""
        return _aggregate_tokens(self.api_sessions)

    @property
    def is_subscription_cost_estimated(self) -> bool:
        """True if any subscription session's cost was reader-estimated from tokens (vs harness-reported)."""
        return any(s.cost_provenance == CostProvenance.ESTIMATED for s in self.subscription_sessions)

    @property
    def is_api_cost_estimated(self) -> bool:
        """True if any api_key session's cost was reader-estimated from tokens (vs harness-reported)."""
        return any(s.cost_provenance == CostProvenance.ESTIMATED for s in self.api_sessions)

    @property
    def session_count(self) -> int:
        """Total session count across both modes."""
        return len(self.sessions)

    @property
    def subscription_session_count(self) -> int:
        return len(self.subscription_sessions)

    @property
    def api_session_count(self) -> int:
        return len(self.api_sessions)
