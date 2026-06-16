from __future__ import annotations

import time
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.exit_codes import EXIT_CODE_TIMEOUT
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.duration import parse_duration_to_seconds
from imbue.mngr_usage.api import WaitForUsageResult
from imbue.mngr_usage.api import derive_elapsed
from imbue.mngr_usage.api import gather_usage_snapshots
from imbue.mngr_usage.api import session_render_dict
from imbue.mngr_usage.api import wait_for_usage
from imbue.mngr_usage.api import window_has_data
from imbue.mngr_usage.api import window_render_dict
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostSnapshot
from imbue.mngr_usage.data_types import SessionCostRecord
from imbue.mngr_usage.data_types import TokenSnapshot
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention is documented in the ``mngr_usage.api`` module
# docstring; the CLI just calls it. ``mngr usage`` finds events by
# enumerating agents via ``list_agents`` and reading per-agent events via
# the events API -- this works uniformly for local and remote agents, and
# inherits ``mngr list``'s CEL filter machinery (``--include``,
# ``--exclude``, ``--provider``, ``--local``, ...).

_NO_DATA_HINT = (
    "No usage data yet -- check that a usage writer plugin is installed in the env "
    "running `mngr`, and that you've sent a prompt to an agent created after that "
    "plugin was installed. Destroyed agents still count toward usage by default; "
    "pass --no-preserved to limit the view to live agents."
)


class UsageCliOptions(CommonCliOptions, AgentFilterCliOptions):
    """Options for the `mngr usage` command.

    Inherits common output options (output_format, quiet, verbose, etc.) from
    ``CommonCliOptions`` and the agent-filter flags (``--include``,
    ``--exclude``, ``--local``, ``--running``, ``--project``, ``--label``,
    ...) from ``AgentFilterCliOptions`` so the same filtering vocabulary
    ``mngr list`` and ``mngr kanpan`` use applies here too.

    ``--verbose`` controls logging level (BUILD/DEBUG/TRACE) and is
    separate from ``--detail``, which controls the **display** breakdown:
    without ``--detail`` the human and JSON outputs are summary-only
    (aggregate cost + windows); with ``--detail`` they additionally
    surface per-session records. Splitting the two avoids conflating
    'log more' with 'show more'.
    """

    stale_after: str | None
    since: str | None
    detail: bool
    provider: tuple[str, ...]
    preserved: bool


@pure
def _parse_optional_duration(value: str | None) -> int | None:
    """Thin wrapper around ``parse_duration_to_seconds`` for optional CLI duration flags.

    Returns ``None`` when the flag wasn't supplied (or was empty); otherwise
    delegates so durations are accepted consistently with the rest of mngr
    (e.g. ``300``, ``5m``, ``2h``, ``1d``).
    """
    if value is None or not value.strip():
        return None
    return int(parse_duration_to_seconds(value))


# =============================================================================
# Rendering
# =============================================================================


@pure
def _format_duration(seconds: int) -> str:
    """Render seconds as a compact human duration: '1h 12m', '4d 3h', '45s'."""
    if seconds <= 0:
        return "now"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


@pure
def _format_reset_phrase(resets_at: int, now: int) -> str:
    """Render the reset timestamp in past or future tense."""
    delta = resets_at - now
    if delta > 0:
        return f"resets in {_format_duration(delta)}"
    if delta < 0:
        return f"reset {_format_duration(-delta)} ago"
    return "just reset"


@pure
def _format_usd(value: float) -> str:
    """Render a USD amount in a money-conventional way: ``$0.42``, ``$5.43``.

    Two decimals everywhere for visual stability; users who need more
    precision can drop to ``--format json`` or a format template, which
    carry the raw floats unchanged.
    """
    return f"${value:.2f}"


@pure
def _format_age_phrase(seconds: int) -> str:
    """Render ``seconds`` as a relative-time phrase: ``2m ago`` / ``just now``."""
    if seconds <= 1:
        return "just now"
    return f"{_format_duration(seconds)} ago"


@pure
def _format_human_line(window_label: str, window: WindowSnapshot, now: int) -> str:
    """Render one window's status as a single human-readable line."""
    if window.used_percentage is None:
        return f"{window_label}: no data"
    parts = [f"{window_label}: {window.used_percentage:.0f}% used,"]
    if window.resets_at is not None:
        parts.append(_format_reset_phrase(window.resets_at, now))
    else:
        parts.append("reset time unknown")
    return " ".join(parts)


@pure
def _format_cost_line(
    *,
    mode_label: str,
    mode_suffix: str,
    aggregate_cost: CostSnapshot,
    session_count: int,
    since_seconds: int,
    latest_event_at: int,
    now: int,
) -> str | None:
    """Render one cost line under each source header for a single [[cost-mode]].

    Caller contract: only invoke when there's at least one session of
    this mode (``session_count >= 1``), so ``latest_event_at`` is always
    a real timestamp -- the freshest session's last event for this mode.
    ``_UsageRenderModel`` enforces this pairing by construction (its
    per-mode ``latest_*_event_at`` property is ``int | None`` and is
    ``None`` iff the mode has zero sessions); callers in
    ``_write_source_section`` gate on the timestamp being present.

    ``mode_label`` is the human-display tag for this mode's cost (e.g.
    ``"subscription cost"`` or ``"api cost"``); ``mode_suffix`` is
    appended verbatim to the label. Use ``""`` when the mode's semantics
    don't need a callout (api cost), and a leading-space parenthetical
    like ``" (imputed)"`` when they do (subscription cost). The
    ``in last <since>`` suffix is rendered through ``_format_duration``
    -- the default 24h plugin config thus prints ``in last 1d``. The
    combination yields lines like:

      subscription cost (imputed): $0.42 (2m ago)            (N == 1)
      subscription cost (imputed): $5.43 across 3 sessions in last 1d
      api cost: $0.42 (2m ago)
      api cost: $5.43 across 3 sessions in last 1d

    Two shapes:
    - One session: ``... $0.42 (2m ago)``. The aggregate is the only
      session, so we show its cost with an age annotation from
      ``latest_event_at``.
    - Multiple sessions: ``... $5.43 across N sessions in last <since>``.
      The age annotation would be ambiguous (which session?) so we drop
      it in favor of the breakdown.

    Returns None when this mode's aggregate has no ``total_cost_usd``
    (sessions exist but the writer never emitted a USD cost field) --
    the caller skips the line entirely.
    """
    total = aggregate_cost.total_cost_usd
    if total is None:
        return None
    prefix = f"{mode_label}{mode_suffix}"
    if session_count == 1:
        age_seconds = max(0, now - latest_event_at)
        return f"{prefix}: {_format_usd(total)} ({_format_age_phrase(age_seconds)})"
    return f"{prefix}: {_format_usd(total)} across {session_count} sessions in last {_format_duration(since_seconds)}"


_SESSION_DETAIL_ID_PREFIX_LEN = 8


@pure
def _session_mode_tag(cost_mode: CostMode) -> str:
    """Compact one-token tag for ``--detail`` per-session lines.

    Long-form labels live on the cost-line ("subscription cost (imputed)",
    "api cost"), so per-session lines just need a short tag that
    distinguishes them at a glance. Exhaustive ``match`` so adding a new
    ``CostMode`` variant is a static error rather than a runtime KeyError.
    """
    match cost_mode:
        case CostMode.SUBSCRIPTION:
            return "sub"
        case CostMode.API_KEY:
            return "api"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _format_session_detail_line(session: SessionCostRecord, now: int) -> str | None:
    """Render one indented per-session line for ``--detail`` view.

    Format: ``  [<tag>] abc12345: $0.42 (2m ago)`` where ``<tag>`` is
    ``sub`` for subscription sessions or ``api`` for api_key sessions.
    The tag keeps each line self-describing in mixed-mode breakdowns
    without bloating to the full mode label.

    Returns None when the session has no ``total_cost_usd`` (treat as no
    data; the breakdown for this session is dropped from the display).
    Session id is truncated to ``_SESSION_DETAIL_ID_PREFIX_LEN`` chars
    for visual compactness; full UUID stays in JSON's ``sessions[]``.
    """
    cost_usd = session.cost.total_cost_usd
    if cost_usd is None:
        return None
    age_seconds = max(0, now - session.last_event_at)
    tag = _session_mode_tag(session.cost_mode)
    return (
        f"  [{tag}] {session.session_id[:_SESSION_DETAIL_ID_PREFIX_LEN]}: "
        f"{_format_usd(cost_usd)} ({_format_age_phrase(age_seconds)})"
    )


@pure
def _window_to_template_values(window: WindowSnapshot, now: int) -> dict[str, str]:
    """Render a window's fields into string values keyed for format-template substitution."""
    seconds_until: str
    if window.resets_at is not None:
        seconds_until = str(max(0, window.resets_at - now))
    else:
        seconds_until = ""
    used_percentage = "" if window.used_percentage is None else f"{window.used_percentage:.2f}"
    elapsed_seconds, elapsed_percentage = derive_elapsed(window, now)
    return {
        "used_percentage": used_percentage,
        "resets_at": "" if window.resets_at is None else str(window.resets_at),
        "seconds_until_reset": seconds_until,
        "window_seconds": "" if window.window_seconds is None else str(window.window_seconds),
        "elapsed_seconds": "" if elapsed_seconds is None else str(elapsed_seconds),
        "elapsed_percentage": "" if elapsed_percentage is None else f"{elapsed_percentage:.2f}",
        "is_present": "true" if window_has_data(window) else "false",
    }


@pure
def _stringify_for_template(value: Any) -> str:
    """Render a single CEL/JSON value as a string for format-template substitution.

    Treats None as empty so a template like ``{api_cost.total_cost_usd}``
    doesn't render the literal ``None`` when the field is absent (e.g. for
    a subscription-only user whose api_cost aggregate is all-None).
    """
    return "" if value is None else str(value)


class _UsageRenderModel(FrozenModel):
    """Top-level view used both for JSON output and template rendering.

    Wraps the underlying ``UsageSnapshot`` and adds the CLI-only concerns
    (the two staleness flags and the ``now`` baseline for age phrases).
    Per-mode aggregation (``subscription_cost`` / ``api_cost`` /
    ``*_session_count`` / ``sessions``) is delegated to the snapshot --
    duplicating it here would risk drift with the canonical implementation.

    Two separate staleness flags so the warning emitter can pick the right
    text for each cause (avoiding "snapshot last updated now ago" when the
    snapshot itself just updated but a window already reset).
    """

    snapshot: UsageSnapshot
    now: int
    is_age_stale: bool
    has_past_reset: bool

    @property
    def source_name(self) -> str:
        return self.snapshot.source_name

    @property
    def snapshot_updated_at(self) -> int:
        return self.snapshot.updated_at

    @property
    def windows(self) -> dict[str, WindowSnapshot]:
        return self.snapshot.windows

    @property
    def sessions(self) -> tuple[SessionCostRecord, ...]:
        return self.snapshot.sessions

    @property
    def subscription_cost(self) -> CostSnapshot:
        return self.snapshot.subscription_cost

    @property
    def api_cost(self) -> CostSnapshot:
        return self.snapshot.api_cost

    @property
    def subscription_tokens(self) -> TokenSnapshot:
        return self.snapshot.subscription_tokens

    @property
    def api_tokens(self) -> TokenSnapshot:
        return self.snapshot.api_tokens

    @property
    def is_subscription_cost_estimated(self) -> bool:
        return self.snapshot.is_subscription_cost_estimated

    @property
    def is_api_cost_estimated(self) -> bool:
        return self.snapshot.is_api_cost_estimated

    @property
    def since_seconds(self) -> int:
        return self.snapshot.since_seconds

    @property
    def session_count(self) -> int:
        return self.snapshot.session_count

    @property
    def subscription_session_count(self) -> int:
        return self.snapshot.subscription_session_count

    @property
    def api_session_count(self) -> int:
        return self.snapshot.api_session_count

    @property
    def is_stale(self) -> bool:
        """Combined flag retained for JSON / format-template surfaces."""
        return self.is_age_stale or self.has_past_reset

    @property
    def latest_subscription_event_at(self) -> int | None:
        """Timestamp of the freshest subscription session's last event, or None."""
        subs = self.snapshot.subscription_sessions
        return subs[0].last_event_at if subs else None

    @property
    def latest_api_event_at(self) -> int | None:
        """Timestamp of the freshest api_key session's last event, or None."""
        apis = self.snapshot.api_sessions
        return apis[0].last_event_at if apis else None


def _build_render_model(snapshot: UsageSnapshot, stale_after: int, now: int) -> _UsageRenderModel:
    """Assemble the renderable view for a snapshot.

    Two staleness causes, tracked separately so the warning text matches:
    - ``is_age_stale``: snapshot updated_at is older than stale_after (no fresh
      event in a while).
    - ``has_past_reset``: any populated window's resets_at is in the past
      (the limit refreshed; cached used_percentage is from the prior window).
      The snapshot itself may be brand-new in this case.
    """
    return _UsageRenderModel(
        snapshot=snapshot,
        now=now,
        is_age_stale=(now - snapshot.updated_at) > stale_after,
        has_past_reset=any(snap.resets_at is not None and snap.resets_at < now for snap in snapshot.windows.values()),
    )


def _render_one_source_for_json(model: _UsageRenderModel, now: int, detail: bool) -> dict[str, Any]:
    """JSON shape for a single source's snapshot.

    Cost is split into ``subscription_cost`` (imputed under a Claude.ai
    subscription) and ``api_cost`` (real spend under a direct API key)
    so consumers don't have to sum imputed and real numbers. There is
    intentionally no combined ``cost`` field; predicates that want one
    or the other must say which (e.g. ``api_cost.total_cost_usd > 5``).

    With ``detail=True`` the full per-session breakdown is included as
    ``sessions[]`` (newest first, each carrying ``cost_mode``); without
    ``detail`` the key is omitted so the default JSON stays small.
    ``session_count`` is always present (total) along with
    ``subscription_session_count`` and ``api_session_count`` so consumers
    can tell at a glance how many sessions backed each aggregate.
    """
    out: dict[str, Any] = {
        "source": model.source_name,
        "updated_at": model.snapshot_updated_at,
        "is_stale": model.is_stale,
        "since_seconds": model.since_seconds,
        "session_count": model.session_count,
        "subscription_session_count": model.subscription_session_count,
        "api_session_count": model.api_session_count,
        "subscription_cost": {
            **model.subscription_cost.model_dump(),
            "is_estimated": model.is_subscription_cost_estimated,
        },
        "subscription_tokens": model.subscription_tokens.model_dump(),
        "api_cost": {
            **model.api_cost.model_dump(),
            "is_estimated": model.is_api_cost_estimated,
        },
        "api_tokens": model.api_tokens.model_dump(),
    }
    if detail:
        out["sessions"] = [session_render_dict(s, now) for s in model.sessions]
    for key, snap in model.windows.items():
        out[key] = window_render_dict(snap, now)
    return out


def _flatten_primary_for_template(model: _UsageRenderModel, now: int) -> dict[str, str]:
    """Flatten the freshest source's render model for format-template substitution.

    The format-template surface is intentionally simple: it reflects the
    primary (freshest) source at top level. Window keys come from the
    writer; if a writer wants format-template support it must emit
    identifier-safe keys (Python's ``str.format`` parses them as
    identifiers). Multi-source consumers should use ``--format json``.

    Cost is exposed split by [[cost-mode]]: ``subscription_cost.*``
    (imputed) and ``api_cost.*`` (real). Each numeric field is always
    populated (empty string when absent) so templates referencing them
    don't KeyError on snapshots lacking that mode. For per-session
    breakdown, use ``--format json`` -- the format-template surface
    intentionally doesn't expose list-indexed paths.
    """
    flat: dict[str, str] = {
        "source": model.source_name,
        "now": str(now),
        "is_stale": str(model.is_stale).lower(),
        "updated_at": str(model.snapshot_updated_at),
        "since_seconds": str(model.since_seconds),
        "session_count": str(model.session_count),
        "subscription_session_count": str(model.subscription_session_count),
        "api_session_count": str(model.api_session_count),
    }
    for cost_field, cost_value in model.subscription_cost.model_dump().items():
        flat[f"subscription_cost.{cost_field}"] = _stringify_for_template(cost_value)
    for cost_field, cost_value in model.api_cost.model_dump().items():
        flat[f"api_cost.{cost_field}"] = _stringify_for_template(cost_value)
    for key, snap in model.windows.items():
        for sub_key, value in _window_to_template_values(snap, now).items():
            flat[f"{key}.{sub_key}"] = value
    return flat


def _write_source_section(model: _UsageRenderModel, now: int, header: str, detail: bool) -> bool:
    """Render one source's section: header, per-mode cost lines, optional per-session breakdown, window lines.

    Default layout (subscription + api both contribute -- rare; usually one mode)::

      [source]
      subscription cost (imputed): $0.42 (2m ago)               when N == 1
      subscription cost (imputed): $5.43 across N sessions in last <since>
      api cost: $0.42 (2m ago)
      api cost: $1.23 across N sessions in last <since>
      <window-label>: <pct>% used, <reset-phrase>       one per populated window

    A cost line is emitted per [[cost-mode]] only if that mode contributed
    cost in the window. So an API-key-only user sees a single ``api cost``
    line; a subscription-only user sees a single ``subscription cost
    (imputed)`` line; both render under the rare case where one agent
    swaps auth mode within the window.

    With ``detail=True`` and at least two sessions total (counting both
    modes together), indented per-session lines are inserted between the
    cost lines and the window lines (newest first, matching
    ``sessions[]`` order). For a single session the cost line already
    names that session's reading so the breakdown is suppressed to avoid
    duplication.

    Per-session lines are skipped individually when that session has no
    usable cost.

    Returns True if anything renderable was emitted -- this gates the
    catch-all "no usage data" hint and the per-source staleness warnings
    downstream. An API-key session that has cost but never gets
    rate_limits should count as data.
    """
    write_human_line(header)
    any_renderable = False
    # Subscription line first (imputed cost is informational; rendering it
    # before real spend keeps the eye-line "more important info further down").
    # Gate on the per-mode latest-event-at: it's None iff there are zero
    # sessions of this mode, in which case there's nothing to format.
    sub_latest = model.latest_subscription_event_at
    if sub_latest is not None:
        subscription_line = _format_cost_line(
            mode_label="subscription cost",
            # "imputed" already marks it informational; add "estimated" when the
            # dollars were token-derived rather than harness-reported (mirrors the
            # api line and the JSON/CEL is_estimated flag).
            mode_suffix=" (imputed, estimated)" if model.is_subscription_cost_estimated else " (imputed)",
            aggregate_cost=model.subscription_cost,
            session_count=model.subscription_session_count,
            since_seconds=model.since_seconds,
            latest_event_at=sub_latest,
            now=now,
        )
        if subscription_line is not None:
            write_human_line(subscription_line)
            any_renderable = True
    api_latest = model.latest_api_event_at
    if api_latest is not None:
        api_line = _format_cost_line(
            mode_label="api cost",
            # Flag token-derived dollars so a reader doesn't read an estimate as billed.
            mode_suffix=" (estimated)" if model.is_api_cost_estimated else "",
            aggregate_cost=model.api_cost,
            session_count=model.api_session_count,
            since_seconds=model.since_seconds,
            latest_event_at=api_latest,
            now=now,
        )
        if api_line is not None:
            write_human_line(api_line)
            any_renderable = True
    if detail and model.session_count > 1:
        for session in model.sessions:
            session_line = _format_session_detail_line(session, now)
            if session_line is not None:
                write_human_line(session_line)
    for key, snap in model.windows.items():
        if snap.used_percentage is None and snap.resets_at is None:
            continue
        write_human_line(_format_human_line(snap.label or key, snap, now))
        any_renderable = True
    return any_renderable


def _emit_output(
    snapshots_with_models: list[tuple[UsageSnapshot, _UsageRenderModel]],
    output_format: OutputFormat,
    format_template: str | None,
    now: int,
    since_seconds: int,
    detail: bool,
) -> None:
    """Write output for zero or more sources, freshest-first.

    The no-data hint is emitted as a logger.warning rather than a stdout line,
    so it (a) lands on stderr and doesn't pollute machine-readable output,
    (b) matches the existing stale-cache warning's channel, and (c) carries
    actionable info regardless of which output format the caller chose.

    The hint fires for two distinct no-data conditions:
    - Zero sources (no events files anywhere): fires under HUMAN and JSON/JSONL
      only. Format-template returns before the hint and produces no stdout
      output at all (and no stderr noise), letting a `--format '...'` consumer
      detect the no-data case by an empty stdout.
    - At least one source exists but no source produced any renderable section
      (no cost line, no populated window line): fires only under HUMAN, since
      the JSON/JSONL surfaces still emit a structured "empty enough" payload
      that downstream consumers can detect themselves.
    """
    if format_template is not None:
        # Format templates always reference the primary (freshest) source's
        # windows at top level for ergonomics; multi-source consumers should
        # use --format json. With no sources at all there's no data to
        # substitute; print nothing to stdout (no synthesized empty line)
        # and let the caller detect the no-data case by empty output.
        if not snapshots_with_models:
            return
        _, primary_model = snapshots_with_models[0]
        line = render_format_template(format_template, _flatten_primary_for_template(primary_model, now))
        write_human_line(line)
        return

    if not snapshots_with_models:
        logger.warning(_NO_DATA_HINT)

    match output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            payload: dict[str, Any] = {
                "now": now,
                "since_seconds": since_seconds,
                "sources": [_render_one_source_for_json(model, now, detail) for _, model in snapshots_with_models],
            }
            write_json_line(payload)
        case OutputFormat.HUMAN:
            if not snapshots_with_models:
                return
            any_renderable_anywhere = False
            # Two distinct stale reasons, tracked separately so the warning text matches the cause:
            #   age_stale_sources: snapshot file hasn't been refreshed in a while.
            #   reset_stale_sources: at least one window's resets_at is in the past, so the
            #     cached used_percentage is from the now-elapsed window. The snapshot may
            #     itself be brand-new (age 0); the staleness is in the data, not the file.
            age_stale_sources: list[tuple[str, int]] = []
            reset_stale_sources: list[str] = []
            for index, (_, model) in enumerate(snapshots_with_models):
                if index > 0:
                    write_human_line("")
                section_had_renderable = _write_source_section(model, now, f"[{model.source_name}]", detail)
                any_renderable_anywhere = any_renderable_anywhere or section_had_renderable
                if not section_had_renderable:
                    continue
                if model.is_age_stale:
                    age_stale_sources.append((model.source_name, max(0, now - model.snapshot_updated_at)))
                if model.has_past_reset:
                    reset_stale_sources.append(model.source_name)
            if not any_renderable_anywhere:
                logger.warning(_NO_DATA_HINT)
            for source_name, age_seconds in age_stale_sources:
                logger.warning(
                    "[{}] snapshot last updated {} ago",
                    source_name,
                    _format_duration(age_seconds),
                )
            for source_name in reset_stale_sources:
                logger.warning(
                    "[{}] a window already reset; the cached percentage is from the previous window",
                    source_name,
                )
        case _ as unreachable:
            assert_never(unreachable)


def _flag_form_for_param(ctx: click.Context, param_name: str) -> str:
    """Return the canonical ``--flag`` form for a click param name.

    Click's param ``name`` can diverge from the visible CLI switch (e.g.
    ``optgroup.option("--format", "output_format", ...)`` stores its value
    under ``output_format`` but the user types ``--format``). To produce
    accurate error messages we look up the actual long-form switch from
    the command's parameter list (``param.opts`` is declared on
    ``click.Parameter`` and present on both ``Option`` and ``Argument``);
    for params without a ``--``-form (i.e. positional arguments) we fall
    back to the hyphenated name.
    """
    for param in ctx.command.params:
        if param.name != param_name:
            continue
        long_opts = [opt for opt in param.opts if opt.startswith("--")]
        if long_opts:
            return long_opts[0]
        break
    return f"--{param_name.replace('_', '-')}"


def _reject_group_options_when_subcommand_invoked(ctx: click.Context) -> None:
    """Raise ``UserInputError`` if any group-level option was explicitly passed.

    Click parses ``mngr usage --local wait --until X`` as: ``--local`` on the
    group, ``--until`` on the subcommand. Our group early-returns on subcommand
    so ``--local`` would silently disappear, which is a UX trap (the user
    clearly meant for ``--local`` to scope the wait). Detect explicit
    command-line params via ``ctx.get_parameter_source`` and tell the user to
    move them after the subcommand.
    """
    explicit_param_names = [
        name for name in ctx.params if ctx.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE
    ]
    if not explicit_param_names:
        return
    flag_forms = sorted(_flag_form_for_param(ctx, name) for name in explicit_param_names)
    subcommand = ctx.invoked_subcommand
    raise UserInputError(
        f"Options {', '.join(flag_forms)} are not supported on `mngr usage` when a "
        f"subcommand is invoked (they would be silently ignored). Pass them after "
        f"`{subcommand}` instead: `mngr usage {subcommand} <options>`."
    )


@click.group(name="usage", invoke_without_command=True)
@optgroup.group("Display")
@optgroup.option(
    "--stale-after",
    default=None,
    help="Warn when the snapshot file is older than this (e.g. '300', '5m', '2h'). Display warning "
    "only -- it does not change which events are aggregated (use --since for that). Default: from "
    "plugin config.",
)
@optgroup.option(
    "--detail",
    is_flag=True,
    default=False,
    help="Expand summary view: show per-session breakdown lines under each source's cost lines "
    "(human, tagged with `[sub]` or `[api]`), and include the `sessions[]` array under each "
    "source (JSON, each session carrying `cost_mode`). Default omits the per-session breakdown "
    "for terseness; the per-mode cost lines and window lines are unchanged.",
)
@add_agent_filter_options
@optgroup.option(
    "--provider",
    multiple=True,
    help="Show only agents from the given provider(s) (repeatable, e.g. --provider local)",
)
@optgroup.option(
    "--since",
    default=None,
    help="Recency window for per-session cost aggregation (e.g. '24h', '7d'). Sessions whose "
    "last event is older are dropped from `sessions[]` and from the per-mode aggregates "
    "(`subscription_cost.*` / `api_cost.*`) computed off them. Default: from plugin config (24h).",
)
@optgroup.option(
    "--preserved/--no-preserved",
    default=True,
    show_default=True,
    help="Include usage preserved from destroyed agents (under <local_host_dir>/preserved/). "
    "On by default so destroyed agents' spend still counts; pass --no-preserved to show only "
    "live agents. Preserved agents honor the same --provider/--project/--local/label filters.",
)
@add_common_options
@click.pass_context
def usage(ctx: click.Context, **kwargs: Any) -> None:
    """Show rolling-window usage / quota data captured by an agent's statusline.

    Enumerates agents via ``list_agents`` (same machinery, filters, and speed
    profile as ``mngr list``), reads each agent's ``events/<source>/
    usage/events.jsonl`` via the events API (so remote agents work the
    same as local), and renders one section per source: per-mode cost
    lines (``subscription cost (imputed)`` and/or ``api cost``, depending
    on which auth contexts the events came from), plus per-session
    breakdown when ``--detail`` is passed, followed by one line per
    populated rate-limit window.

    When invoked without a subcommand, prints the current snapshot. Use
    ``mngr usage wait`` to block until a CEL predicate matches.
    """
    if ctx.invoked_subcommand is not None:
        _reject_group_options_when_subcommand_invoked(ctx)
        return
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="usage",
        command_class=UsageCliOptions,
        is_format_template_supported=True,
    )
    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)

    stale_after_override = _parse_optional_duration(opts.stale_after)
    effective_stale_after = (
        stale_after_override if stale_after_override is not None else plugin_config.stale_after_seconds
    )
    since_override = _parse_optional_duration(opts.since)
    effective_since = since_override if since_override is not None else plugin_config.since_seconds

    include_filters, exclude_filters = build_agent_filter_cel(opts, mngr_ctx.concurrency_group)
    provider_names = opts.provider if opts.provider else None
    now = int(time.time())
    snapshots = gather_usage_snapshots(
        mngr_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        since_seconds=effective_since,
        now=now,
        include_preserved=opts.preserved,
    )

    # One render model per source (already collapsed in the aggregation pipeline),
    # sorted freshest-first. Tiebreak by source_name so the order is stable in tests.
    snapshots_with_models = sorted(
        ((s, _build_render_model(s, effective_stale_after, now)) for s in snapshots),
        key=lambda sm: (sm[0].updated_at, sm[0].source_name),
        reverse=True,
    )
    _emit_output(
        snapshots_with_models,
        output_opts.output_format,
        output_opts.format_template,
        now,
        effective_since,
        opts.detail,
    )


CommandHelpMetadata(
    key="usage",
    one_line_description="Show rolling-window usage / quota data from agent statusline events",
    synopsis="mngr usage [--stale-after DURATION] [--detail] [--since DURATION] [--no-preserved] [COMMAND]",
    description="""Reports rolling-window usage / quota data captured by an agent's
statusline.

Agent-agnostic and host-agnostic: enumerates matching agents via
``list_agents`` (same machinery and filter vocabulary as ``mngr list``) and
reads each agent's ``events/<source>/usage/events.jsonl`` via the
events API. Local and remote agents are read uniformly; the writer plugin
chooses the ``<source>`` segment.

Per-source aggregation:

- Rate-limit windows track an account-level counter, so the freshest reading
  across all agents wins.
- Cost is process-cumulative as emitted (Claude Code's ``total_cost_usd``
  grows across session boundaries and only resets when the Claude Code
  process itself is relaunched; ``/clear`` does NOT reset it). The reader
  detects process boundaries via cost-drop signals within each agent's
  event stream and stores each session's *own contribution* (delta from
  the prior session's cumulative reading in the same process). Records
  are summed across all (agent, process, session) tuples within the
  ``--since`` recency window (default 24h).
- Cost is split by auth mode: ``subscription_cost`` aggregates sessions
  whose Claude Code process was on a Claude.ai Pro/Max subscription
  (numbers are imputed by Claude Code), and ``api_cost`` aggregates
  sessions whose process was on a direct ANTHROPIC_API_KEY (numbers are
  real billable spend). Mode is detected per process from whether any
  event in it carried a ``rate_limits`` payload (subscription auth) or
  not (api-key auth). The two are never lumped into a single ``cost``
  field -- conflating imputed estimates with real spend would be
  misleading.
- The JSON output's ``sessions[]`` array is ordered newest-first; consumers
  that want a specific session's reading can index ``sessions[0]``. Each
  session carries a ``cost_mode`` field ("SUBSCRIPTION" or "API_KEY").""",
    examples=(
        ("Show current usage", "mngr usage"),
        ("Local agents only", "mngr usage --local"),
        ("Specific providers", "mngr usage --provider local --provider modal"),
        ("Aggregate cost across the last week", "mngr usage --since 7d"),
        ("Treat the snapshot as stale after 60s (warning only)", "mngr usage --stale-after 60"),
        ("Per-session breakdown (human + JSON, mode-tagged)", "mngr usage --detail"),
        ("Machine-readable output", "mngr usage --format json"),
        (
            "Custom format template (real API spend only)",
            "mngr usage --format '{api_cost.total_cost_usd} across {api_session_count} sessions'",
        ),
    ),
).register()

add_pager_help_option(usage)


# =============================================================================
# `mngr usage wait` subcommand
# =============================================================================


class UsageWaitCliOptions(CommonCliOptions, AgentFilterCliOptions):
    """Options for the ``mngr usage wait`` subcommand.

    Inherits the common output options and the standard agent-filter
    vocabulary (``--include``, ``--exclude``, ``--local``, ``--provider``,
    ``--project``, ``--label``, ...) so a wait can scope to the same
    agent set ``mngr usage`` would consider.

    ``until_filters`` (from ``--until``) is the predicate vocabulary --
    a list of CEL expressions, all of which must evaluate true against
    a single source's CEL context for the wait to succeed. See
    ``api.build_source_cel_context`` for the shape that's exposed. Users
    can scope matching to a specific writer via the top-level ``source``
    field in CEL (e.g. ``source == "claude" && five_hour.used_percentage
    < 50``).
    """

    until_filters: tuple[str, ...]
    provider: tuple[str, ...]
    timeout: str | None
    interval: str
    since: str | None
    preserved: bool


@usage.command("wait")
@optgroup.group("Predicate")
@optgroup.option(
    "--until",
    "until_filters",
    multiple=True,
    required=True,
    help="CEL expression that must evaluate true for some source to win the wait "
    "[repeatable, all must match]. The CEL context is the per-source dict from "
    "`mngr usage --format json` (see help description for shape).",
)
@optgroup.group("Wait options")
@optgroup.option(
    "--timeout",
    default=None,
    help="Maximum time to wait (e.g. '30s', '5m', '1h'). Default: wait forever.",
)
@optgroup.option(
    "--interval",
    default="30s",
    show_default=True,
    help="Poll interval (e.g. '15s', '1m'). The usage snapshot is rebuilt every interval. "
    "Default of 30s suits multi-hour windows; tighten for short-window predicates.",
)
@optgroup.option(
    "--since",
    default=None,
    help="Recency window for per-session cost aggregation (e.g. '24h', '7d'). Affects the "
    "per-session surfaces in the CEL context: `subscription_cost.*` / `api_cost.*` "
    "(per-mode aggregates), `sessions[]`, and the `*_session_count` fields. "
    "Default: from plugin config (24h).",
)
@add_agent_filter_options
@optgroup.option(
    "--provider",
    multiple=True,
    help="Restrict to agents from the given provider(s) (repeatable, e.g. --provider local).",
)
@optgroup.option(
    "--preserved/--no-preserved",
    default=True,
    show_default=True,
    help="Include usage preserved from destroyed agents when evaluating the predicate. "
    "On by default; pass --no-preserved to consider only live agents.",
)
@add_common_options
@click.pass_context
def wait(ctx: click.Context, **kwargs: Any) -> None:
    """Block until a usage snapshot's CEL context satisfies all --until filters.

    Polls ``gather_usage_snapshots`` every ``--interval`` and evaluates each
    source's CEL context (same shape as ``mngr usage --format json`` source
    entries) against every ``--until`` expression. The first source for
    which all predicates evaluate true wins; the command exits 0.

    Exit codes match ``mngr wait``:
      0 - A source matched all --until filters.
      1 - Error.
      2 - Timed out.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="usage wait",
        command_class=UsageWaitCliOptions,
        is_format_template_supported=False,
    )
    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)

    include_filters, exclude_filters = build_agent_filter_cel(opts, mngr_ctx.concurrency_group)
    provider_names = opts.provider if opts.provider else None

    until_programs, _unused_excludes = compile_cel_filters(opts.until_filters, exclude_filters=())

    timeout_seconds = parse_duration_to_seconds(opts.timeout) if opts.timeout is not None else None
    interval_seconds = parse_duration_to_seconds(opts.interval)
    since_override = _parse_optional_duration(opts.since)
    effective_since = since_override if since_override is not None else plugin_config.since_seconds

    emit_info(
        f"Waiting for usage predicate (poll {opts.interval}"
        f"{', timeout ' + opts.timeout if opts.timeout is not None else ''}"
        ")",
        output_opts.output_format,
    )

    try:
        result = wait_for_usage(
            poll_fn=lambda: gather_usage_snapshots(
                mngr_ctx,
                now=int(time.time()),
                include_filters=include_filters,
                exclude_filters=exclude_filters,
                provider_names=provider_names,
                since_seconds=effective_since,
                include_preserved=opts.preserved,
            ),
            until_filters=until_programs,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            now_fn=lambda: int(time.time()),
        )
    except KeyboardInterrupt:
        logger.debug("Received keyboard interrupt")
        ctx.exit(EXIT_CODE_ERROR)
        return

    if result.is_matched:
        _emit_match_state_change(result, output_opts.output_format)
    _output_wait_result(result, output_opts.output_format)
    if result.is_matched:
        ctx.exit(EXIT_CODE_SUCCESS)
    elif result.is_timed_out:
        ctx.exit(EXIT_CODE_TIMEOUT)
    else:
        ctx.exit(EXIT_CODE_ERROR)


def _emit_match_state_change(result: WaitForUsageResult, output_format: OutputFormat) -> None:
    """Emit the match transition in ``mngr_wait``'s ``state_change`` JSONL shape.

    The match is the only state change ``wait_for_usage`` produces: at most
    once per call, ``matched_source`` flips from None to a ``source_name``.
    Reusing ``mngr_wait``'s envelope means downstream JSONL consumers see one
    consistent shape across both wait commands.
    """
    match output_format:
        case OutputFormat.JSONL:
            emit_event(
                "state_change",
                {
                    "field": "matched_source",
                    "old_value": None,
                    "new_value": result.matched_source,
                    "elapsed_seconds": result.elapsed_seconds,
                },
                OutputFormat.JSONL,
            )
        case OutputFormat.HUMAN:
            write_human_line(
                "matched_source changed: None -> {} (after {:.1f}s)",
                result.matched_source,
                result.elapsed_seconds,
            )
        case OutputFormat.JSON:
            # JSON mode: silent until the final result payload.
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _output_wait_result(result: WaitForUsageResult, output_format: OutputFormat) -> None:
    """Render the final wait result. JSON/JSONL emit one final record; human writes a summary line."""
    payload = {
        "is_matched": result.is_matched,
        "is_timed_out": result.is_timed_out,
        "matched_source": result.matched_source,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "sources": [s.source_name for s in result.final_snapshots],
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(payload)
        case OutputFormat.JSONL:
            emit_event("result", payload, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if result.is_matched:
                write_human_line(
                    "Matched on source '{}' after {:.1f}s",
                    result.matched_source or "?",
                    result.elapsed_seconds,
                )
            elif result.is_timed_out:
                write_human_line("Timed out after {:.1f}s without match", result.elapsed_seconds)
            else:
                raise MngrError(
                    f"wait_for_usage returned both is_matched=False and is_timed_out=False "
                    f"(elapsed={result.elapsed_seconds:.2f}s)"
                )
        case _ as unreachable:
            assert_never(unreachable)


CommandHelpMetadata(
    key="usage.wait",
    one_line_description="Block until a usage snapshot matches a CEL predicate",
    synopsis="mngr usage wait --until CEL [--until CEL ...] [--timeout DURATION] [--interval DURATION] [--since DURATION] [--no-preserved]",
    description="""Polls ``mngr usage`` snapshots until at least one source's CEL
context satisfies every ``--until`` expression. Composable with shell:

    mngr usage wait --until 'five_hour.used_percentage < 50 && five_hour.elapsed_percentage > 75' \\
      && mngr message my-agent "ok, kick off the next batch"

The CEL context per source mirrors one entry of ``mngr usage --format
json``'s ``sources`` array. Window fields (under each window key, e.g.
``five_hour``):

- ``used_percentage``: from the writer.
- ``resets_at`` / ``seconds_until_reset``: when the window resets.
- ``window_seconds``: window duration (writer-provided; absent for
  variable-duration windows like Claude's overage).
- ``elapsed_seconds`` / ``elapsed_percentage``: derived from
  ``window_seconds`` and ``seconds_until_reset``; absent when
  ``window_seconds`` isn't emitted.

Source-level fields:

- ``subscription_cost.total_cost_usd`` / ``subscription_cost.total_duration_ms`` / ... :
  aggregate across the recency window of sessions whose Claude Code process
  was on a Claude.ai Pro/Max subscription. Cost is **imputed** by Claude Code
  (what the usage would have cost on the metered API) and is informational --
  the user actually pays a flat subscription. Never lumped with ``api_cost``.
- ``api_cost.total_cost_usd`` / ``api_cost.total_duration_ms`` / ... :
  aggregate across the recency window of sessions whose Claude Code process
  was on a direct ANTHROPIC_API_KEY. Cost is **real** billable spend.
- ``subscription_session_count`` / ``api_session_count``: number of sessions
  in each mode contributing to the corresponding aggregate. ``session_count``
  is the total across both modes.
- ``sessions``: list of session-cost records, newest-first. Each entry
  carries a ``cost_mode`` ("SUBSCRIPTION" or "API_KEY") tag.

Exit codes:
  0 - A source matched all --until filters.
  1 - Error (invalid CEL, interrupt).
  2 - Timed out.""",
    examples=(
        (
            "Wait for 75% of the 5h window to elapse while at most 50% of the limit is used",
            "mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50'",
        ),
        (
            "Restrict to Claude usage only (via CEL)",
            "mngr usage wait --until 'source == \"claude\" && five_hour.used_percentage < 25'",
        ),
        (
            "Bail out after an hour",
            "mngr usage wait --until 'seven_day.used_percentage < 30' --timeout 1h",
        ),
        (
            "Tighter poll for short-window predicates",
            "mngr usage wait --until 'overage.is_using_overage == false' --interval 10s",
        ),
        (
            "Wait until cumulative real API spend over the last 24h crosses $20",
            "mngr usage wait --until 'api_cost.total_cost_usd > 20.0'",
        ),
        (
            "Cap real spend in the last week (subscription cost is imputed and ignored here)",
            "mngr usage wait --until 'api_cost.total_cost_usd > 100.0' --since 7d",
        ),
    ),
    see_also=(
        ("usage", "Show the current snapshot"),
        ("wait", "Wait on agent/host lifecycle state (unrelated)"),
    ),
).register()

add_pager_help_option(wait)
