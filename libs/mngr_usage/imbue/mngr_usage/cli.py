from __future__ import annotations

import json
import time
from datetime import datetime
from threading import Lock
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.events import discover_event_sources
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.duration import parse_duration_to_seconds
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention: each agent's state dir holds rate-limits events at
#   <agent_state_dir>/events/<source>/rate_limits/events.jsonl
# This mirrors the common_transcript pattern (events/<source>/common_transcript/
# events.jsonl) used by `mngr transcript`. The <source> segment names the
# writer (a free-form identifier chosen by the writer plugin) and becomes the
# UsageSnapshot.source_name for the event. ``mngr usage`` finds events by
# enumerating agents via ``list_agents`` and reading per-agent events via the
# events API -- this works uniformly for local and remote agents, and inherits
# ``mngr list``'s CEL filter machinery (``--include``, ``--exclude``,
# ``--provider``, ``--local``, ...).
_RATE_LIMITS_SOURCE_SUFFIX = "/rate_limits"
_EVENTS_JSONL_FILENAME = "events.jsonl"

_NO_DATA_HINT = (
    "No usage data yet -- check that a usage writer plugin is installed in the env "
    "running `mngr`, and that you've sent a prompt to an agent that (a) was created "
    "after that plugin was installed and (b) is still alive."
)


class UsageCliOptions(CommonCliOptions, AgentFilterCliOptions):
    """Options for the `mngr usage` command.

    Inherits common output options (output_format, quiet, verbose, etc.) from
    ``CommonCliOptions`` and the agent-filter flags (``--include``,
    ``--exclude``, ``--local``, ``--running``, ``--project``, ``--label``,
    ...) from ``AgentFilterCliOptions`` so the same filtering vocabulary
    ``mngr list`` and ``mngr kanpan`` use applies here too.
    """

    max_age: str | None
    provider: tuple[str, ...]


@pure
def _parse_max_age(value: str | None) -> int | None:
    """Thin wrapper around ``parse_duration_to_seconds`` for the optional CLI flag.

    Returns ``None`` when the user didn't supply ``--max-age``; otherwise
    delegates to ``parse_duration_to_seconds`` so durations are accepted
    consistently with the rest of mngr.
    """
    if value is None or not value.strip():
        return None
    return int(parse_duration_to_seconds(value))


# =============================================================================
# Discovery + parsing
# =============================================================================


@pure
def _last_valid_event_from_content(content: str, source_for_warnings: str) -> dict[str, Any] | None:
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
            # most common case; skip it and try the previous one. We log at warning
            # level for visibility because corrupt earlier lines indicate something
            # worse and the user should know about it; expected truncation will
            # resolve on the next render.
            logger.warning("Skipping malformed event line in {}: {}", source_for_warnings, e)
            continue
        if isinstance(event, dict):
            return event
    return None


@pure
def _parse_iso_timestamp(value: Any) -> int | None:
    """Convert an ISO 8601 ``timestamp`` field to a Unix timestamp, or None on failure.

    Python 3.11+ ``datetime.fromisoformat`` accepts the trailing ``Z`` and
    9-digit fractional seconds the writer emits, so no normalization needed.
    """
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


@pure
def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@pure
def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@pure
def _coerce_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@pure
def _coerce_optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _windows_from_event(event: dict[str, Any]) -> dict[str, WindowSnapshot]:
    """Reshape an event's ``rate_limits`` payload into UsageSnapshot windows.

    The on-disk event shape (matching what the writer emits) is:

        {"source": "<source>/rate_limits", "type": "rate_limit_snapshot",
         "event_id": ..., "timestamp": ...,
         "rate_limits": {"<window_key>": {"used_percentage": 11, "resets_at": ...,
                                          "label": "5h"}, ...}}

    Window keys and their order are entirely up to the writer; we preserve
    JSONL insertion order. Per-window ``label`` (optional) is what the
    human renderer uses; missing labels fall back to the window key.
    Missing fields are coerced to None.
    """
    rate_limits = event.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return {}
    windows: dict[str, WindowSnapshot] = {}
    for window_key, window_value in rate_limits.items():
        if not isinstance(window_value, dict):
            continue
        windows[str(window_key)] = WindowSnapshot(
            used_percentage=_coerce_optional_float(window_value.get("used_percentage")),
            resets_at=_coerce_optional_int(window_value.get("resets_at")),
            label=_coerce_optional_str(window_value.get("label")),
            status=_coerce_optional_str(window_value.get("status")),
            is_using_overage=_coerce_optional_bool(window_value.get("is_using_overage")),
        )
    return windows


def _snapshot_from_event(event: dict[str, Any], source_name: str) -> UsageSnapshot | None:
    """Reshape one events.jsonl line into a UsageSnapshot, or None if unusable."""
    timestamp = _parse_iso_timestamp(event.get("timestamp"))
    if timestamp is None:
        return None
    windows = _windows_from_event(event)
    if not windows:
        return None
    return UsageSnapshot(source_name=source_name, windows=windows, updated_at=timestamp)


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
        event = _last_valid_event_from_content(content, f"agent {agent.name} {source.source_path}")
        if event is None:
            continue
        source_name = source.source_path.removesuffix(_RATE_LIMITS_SOURCE_SUFFIX)
        snapshot = _snapshot_from_event(event, source_name)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


class _SnapshotCollector(MutableModel):
    """``list_agents`` on_agent callback that collects per-agent rate-limit snapshots.

    Class-based rather than a closure so it can hold its own lock without
    triggering the "no inline functions" ratchet (the callback runs from a
    streaming provider thread).
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


def _gather_snapshots(
    mngr_ctx: MngrContext,
    *,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
) -> list[UsageSnapshot]:
    """Enumerate matching agents via ``list_agents`` and collect their rate-limit snapshots.

    Inherits ``mngr list``'s CEL filtering, so e.g. ``mngr usage --local`` /
    ``--provider local`` / ``--project foo`` work without per-command glue.
    Errors from individual hosts are tolerated so a flaky remote provider
    doesn't crash the whole pass; this matches ``mngr list``'s
    ``CONTINUE`` behavior under stress (and is what users expect from a
    glanceable status command).
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
    return collector.snapshots


@pure
def _collapse_by_source(snapshots: list[UsageSnapshot]) -> list[UsageSnapshot]:
    """Reduce per-agent snapshots to one per ``source_name`` (the freshest).

    Multiple agents may write to the same source (e.g. several Claude agents
    all writing to ``events/claude/rate_limits/events.jsonl`` in their own
    state dirs). The user wants the most recent reading of each source, not
    a separate block per agent. The returned list's order is unspecified --
    callers re-sort freshest-first by ``(updated_at, source_name)`` anyway.
    """
    by_source: dict[str, UsageSnapshot] = {}
    for snap in snapshots:
        existing = by_source.get(snap.source_name)
        # ``existing`` was looked up under ``snap.source_name``, so the keys
        # match by construction -- compare on ``updated_at`` alone.
        if existing is None or snap.updated_at > existing.updated_at:
            by_source[snap.source_name] = snap
    return list(by_source.values())


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
def _window_to_template_values(window: WindowSnapshot, now: int) -> dict[str, str]:
    """Render a window's fields into string values keyed for format-template substitution."""
    seconds_until: str
    if window.resets_at is not None:
        seconds_until = str(max(0, window.resets_at - now))
    else:
        seconds_until = ""
    used_percentage = "" if window.used_percentage is None else f"{window.used_percentage:.2f}"
    return {
        "used_percentage": used_percentage,
        "resets_at": "" if window.resets_at is None else str(window.resets_at),
        "seconds_until_reset": seconds_until,
        "is_present": "true" if window.used_percentage is not None or window.resets_at is not None else "false",
    }


class _UsageRenderModel(FrozenModel):
    """Top-level view used both for JSON output and template rendering.

    Two separate staleness flags so the warning emitter can pick the right
    text for each cause (avoiding "snapshot last updated now ago" when the
    snapshot itself just updated but a window already reset).
    """

    source_name: str
    now: int
    is_age_stale: bool
    has_past_reset: bool
    snapshot_updated_at: int | None
    windows: dict[str, WindowSnapshot]

    @property
    def is_stale(self) -> bool:
        """Combined flag retained for JSON / format-template surfaces."""
        return self.is_age_stale or self.has_past_reset


def _build_render_model(snapshot: UsageSnapshot, max_age: int, now: int) -> _UsageRenderModel:
    """Assemble the renderable view for a snapshot.

    Two staleness causes, tracked separately so the warning text matches:
    - ``is_age_stale``: snapshot updated_at is older than max_age (no fresh
      event in a while).
    - ``has_past_reset``: any populated window's resets_at is in the past
      (the limit refreshed; cached used_percentage is from the prior window).
      The snapshot itself may be brand-new in this case.
    """
    return _UsageRenderModel(
        source_name=snapshot.source_name,
        now=now,
        is_age_stale=(now - snapshot.updated_at) > max_age,
        has_past_reset=any(snap.resets_at is not None and snap.resets_at < now for snap in snapshot.windows.values()),
        snapshot_updated_at=snapshot.updated_at,
        windows=snapshot.windows,
    )


def _window_render_dict(snap: WindowSnapshot, now: int) -> dict[str, Any]:
    """Window's snapshot fields plus computed seconds_until_reset / is_present."""
    seconds_until_reset = None if snap.resets_at is None else max(0, snap.resets_at - now)
    return {
        **snap.model_dump(),
        "seconds_until_reset": seconds_until_reset,
        "is_present": snap.used_percentage is not None or snap.resets_at is not None,
    }


def _render_one_source_for_json(model: _UsageRenderModel, now: int) -> dict[str, Any]:
    """JSON shape for a single source's snapshot. Window order = writer's insertion order."""
    out: dict[str, Any] = {
        "source": model.source_name,
        "updated_at": model.snapshot_updated_at,
        "is_stale": model.is_stale,
    }
    for key, snap in model.windows.items():
        out[key] = _window_render_dict(snap, now)
    return out


def _flatten_primary_for_template(model: _UsageRenderModel, now: int) -> dict[str, str]:
    """Flatten the freshest source's render model for format-template substitution.

    The format-template surface is intentionally simple: it reflects the
    primary (freshest) source's windows at top level. Window keys come from
    the writer; if a writer wants format-template support it must emit
    identifier-safe keys (Python's ``str.format`` parses them as identifiers).
    Multi-source consumers should use ``--format json``.
    """
    flat: dict[str, str] = {
        "source": model.source_name,
        "now": str(now),
        "is_stale": str(model.is_stale).lower(),
        "updated_at": "" if model.snapshot_updated_at is None else str(model.snapshot_updated_at),
    }
    for key, snap in model.windows.items():
        for sub_key, value in _window_to_template_values(snap, now).items():
            flat[f"{key}.{sub_key}"] = value
    return flat


def _write_source_section(model: _UsageRenderModel, now: int, header: str) -> bool:
    """Render one source's window lines (always preceded by a ``[source]`` header).

    Window order is the writer's insertion order. The line label is the
    window's ``label`` field if present, else the literal key.
    Returns True if any window with a percentage was rendered (drives the
    catch-all hint downstream).
    """
    write_human_line(header)
    any_with_percentage = False
    for key, snap in model.windows.items():
        if snap.used_percentage is None and snap.resets_at is None:
            continue
        write_human_line(_format_human_line(snap.label or key, snap, now))
        if snap.used_percentage is not None:
            any_with_percentage = True
    return any_with_percentage


def _emit_output(
    snapshots_with_models: list[tuple[UsageSnapshot, _UsageRenderModel]],
    output_format: OutputFormat,
    format_template: str | None,
    now: int,
) -> None:
    """Write output for zero or more sources, freshest-first.

    The no-data hint is emitted as a logger.warning rather than a stdout line,
    so it (a) lands on stderr and doesn't pollute machine-readable output,
    (b) matches the existing stale-cache warning's channel, and (c) carries
    actionable info regardless of which output format the caller chose.
    Two no-data cases trigger it: zero sources (no events files anywhere)
    and "every source's events lack used_percentage" (renders as percentage-
    less window lines, but still actionable for the user).
    """
    if format_template is not None:
        # Format templates always reference the primary (freshest) source's
        # windows at top level for ergonomics; multi-source consumers should
        # use --format json. With no sources at all, all fields are empty.
        if not snapshots_with_models:
            empty = _UsageRenderModel(
                source_name="",
                now=now,
                is_age_stale=True,
                has_past_reset=False,
                snapshot_updated_at=None,
                windows={},
            )
            line = render_format_template(format_template, _flatten_primary_for_template(empty, now))
        else:
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
                "sources": [_render_one_source_for_json(model, now) for _, model in snapshots_with_models],
            }
            emit_final_json(payload)
        case OutputFormat.HUMAN:
            if not snapshots_with_models:
                return
            any_with_percentage_anywhere = False
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
                section_had_percentage = _write_source_section(model, now, f"[{model.source_name}]")
                any_with_percentage_anywhere = any_with_percentage_anywhere or section_had_percentage
                if not section_had_percentage or model.snapshot_updated_at is None:
                    continue
                if model.is_age_stale:
                    age_stale_sources.append((model.source_name, max(0, now - model.snapshot_updated_at)))
                if model.has_past_reset:
                    reset_stale_sources.append(model.source_name)
            if not any_with_percentage_anywhere:
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


@click.command("usage")
@click.option(
    "--max-age",
    default=None,
    help="Stale-warning threshold (e.g. '300', '5m', '2h'). Default: from plugin config.",
)
@add_agent_filter_options
@optgroup.option(
    "--provider",
    multiple=True,
    help="Show only agents from the given provider(s) (repeatable, e.g. --provider local)",
)
@add_common_options
@click.pass_context
def usage(ctx: click.Context, **kwargs: Any) -> None:
    """Show rolling-window usage / quota data captured by an agent's statusline.

    Enumerates agents via ``list_agents`` (same machinery, filters, and speed
    profile as ``mngr list``), reads each agent's ``events/<source>/
    rate_limits/events.jsonl`` via the events API (so remote agents work the
    same as local), and renders the freshest snapshot per source.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="usage",
        command_class=UsageCliOptions,
        is_format_template_supported=True,
    )
    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)

    max_age_override = _parse_max_age(opts.max_age)
    effective_max_age = max_age_override if max_age_override is not None else plugin_config.max_age_seconds

    include_filters, exclude_filters = build_agent_filter_cel(opts, mngr_ctx.concurrency_group)
    provider_names = opts.provider if opts.provider else None
    snapshots = _collapse_by_source(
        _gather_snapshots(
            mngr_ctx,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            provider_names=provider_names,
        )
    )
    now = int(time.time())

    # One render model per source (freshest snapshot for that source), sorted
    # freshest-first across sources. Tiebreak by source_name so the order is
    # stable in tests.
    snapshots_with_models = sorted(
        ((s, _build_render_model(s, effective_max_age, now)) for s in snapshots),
        key=lambda sm: (sm[0].updated_at, sm[0].source_name),
        reverse=True,
    )
    _emit_output(snapshots_with_models, output_opts.output_format, output_opts.format_template, now)


CommandHelpMetadata(
    key="usage",
    one_line_description="Show rolling-window usage / quota data from agent statusline events",
    synopsis="mngr usage [OPTIONS]",
    description="""Reports rolling-window usage / quota data captured by an agent's
statusline.

Agent-agnostic and host-agnostic: enumerates matching agents via
``list_agents`` (same machinery and filter vocabulary as ``mngr list``) and
reads each agent's ``events/<source>/rate_limits/events.jsonl`` via the
events API. Local and remote agents are read uniformly; the writer plugin
chooses the ``<source>`` segment.""",
    examples=(
        ("Show current usage", "mngr usage"),
        ("Local agents only", "mngr usage --local"),
        ("Specific providers", "mngr usage --provider local --provider modal"),
        ("Treat the snapshot as stale after 60s (warning only)", "mngr usage --max-age 60"),
        ("Machine-readable output", "mngr usage --format json"),
        ("Custom format template", "mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'"),
    ),
).register()

add_pager_help_option(usage)
