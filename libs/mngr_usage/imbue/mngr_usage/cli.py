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
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.duration import parse_duration_to_seconds
from imbue.mngr_usage.api import WaitForUsageResult
from imbue.mngr_usage.api import derive_elapsed
from imbue.mngr_usage.api import gather_usage_snapshots
from imbue.mngr_usage.api import wait_for_usage
from imbue.mngr_usage.api import window_render_dict
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention is documented on ``api.gather_usage_snapshots``;
# the CLI just calls it. ``mngr usage`` finds events by enumerating agents
# via ``list_agents`` and reading per-agent events via the events API --
# this works uniformly for local and remote agents, and inherits ``mngr
# list``'s CEL filter machinery (``--include``, ``--exclude``,
# ``--provider``, ``--local``, ...).

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
    elapsed_seconds, elapsed_percentage = derive_elapsed(window, now)
    return {
        "used_percentage": used_percentage,
        "resets_at": "" if window.resets_at is None else str(window.resets_at),
        "seconds_until_reset": seconds_until,
        "window_seconds": "" if window.window_seconds is None else str(window.window_seconds),
        "elapsed_seconds": "" if elapsed_seconds is None else str(elapsed_seconds),
        "elapsed_percentage": "" if elapsed_percentage is None else f"{elapsed_percentage:.2f}",
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


def _render_one_source_for_json(model: _UsageRenderModel, now: int) -> dict[str, Any]:
    """JSON shape for a single source's snapshot. Window order = writer's insertion order."""
    out: dict[str, Any] = {
        "source": model.source_name,
        "updated_at": model.snapshot_updated_at,
        "is_stale": model.is_stale,
    }
    for key, snap in model.windows.items():
        out[key] = window_render_dict(snap, now)
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
    flag_form = sorted(f"--{name.replace('_', '-')}" for name in explicit_param_names)
    subcommand = ctx.invoked_subcommand
    raise UserInputError(
        f"Options {', '.join(flag_form)} are not supported on `mngr usage` when a "
        f"subcommand is invoked (they would be silently ignored). Pass them after "
        f"`{subcommand}` instead: `mngr usage {subcommand} <options>`."
    )


@click.group(name="usage", invoke_without_command=True)
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

    When invoked without a subcommand, prints the current snapshot. Use
    ``mngr usage wait`` to block until a CEL predicate matches.
    """
    # Group-with-default-action: when a subcommand is invoked we hand off
    # entirely (no group-level reading or rendering). Without a subcommand
    # we render the snapshot, the existing ``mngr usage`` behavior.
    #
    # Reject group-level options when a subcommand is invoked, instead of
    # silently ignoring them. ``mngr usage --local wait --until X`` looks
    # like ``--local`` should scope the wait, but Click parses it as a
    # group option and our early-return drops it. The explicit error
    # tells the user to put flags after the subcommand.
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

    max_age_override = _parse_max_age(opts.max_age)
    effective_max_age = max_age_override if max_age_override is not None else plugin_config.max_age_seconds

    include_filters, exclude_filters = build_agent_filter_cel(opts, mngr_ctx.concurrency_group)
    provider_names = opts.provider if opts.provider else None
    snapshots = gather_usage_snapshots(
        mngr_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
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
    ``api.build_source_cel_context`` for the shape that's exposed.

    ``source_filters`` (from ``--source``) restricts which writer
    sources count for matching: ``--source claude`` only matches the
    ``claude`` source, ignoring anything else even if it would
    otherwise satisfy the predicate.
    """

    until_filters: tuple[str, ...]
    source_filters: tuple[str, ...]
    provider: tuple[str, ...]
    timeout: str | None
    interval: str


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
@optgroup.option(
    "--source",
    "source_filters",
    multiple=True,
    help="Only consider these writer sources (e.g. 'claude'). When omitted, any source "
    "may satisfy the predicate [repeatable].",
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
@add_agent_filter_options
@optgroup.option(
    "--provider",
    multiple=True,
    help="Restrict to agents from the given provider(s) (repeatable, e.g. --provider local).",
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

    include_filters, exclude_filters = build_agent_filter_cel(opts, mngr_ctx.concurrency_group)
    provider_names = opts.provider if opts.provider else None

    # ``compile_cel_filters`` raises MngrError on invalid CEL; let it bubble
    # so the user sees the exact bad expression rather than a generic timeout.
    until_programs, _unused_excludes = compile_cel_filters(opts.until_filters, exclude_filters=())

    timeout_seconds = parse_duration_to_seconds(opts.timeout) if opts.timeout is not None else None
    interval_seconds = parse_duration_to_seconds(opts.interval)

    emit_info(
        f"Waiting for usage predicate (poll {opts.interval}"
        f"{', timeout ' + opts.timeout if opts.timeout is not None else ''}"
        f"{', sources=' + ','.join(opts.source_filters) if opts.source_filters else ''}"
        ")",
        output_opts.output_format,
    )

    captured_output_format = output_opts.output_format
    try:
        result = wait_for_usage(
            poll_fn=lambda: gather_usage_snapshots(
                mngr_ctx,
                include_filters=include_filters,
                exclude_filters=exclude_filters,
                provider_names=provider_names,
            ),
            until_filters=until_programs,
            source_filter=opts.source_filters,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            on_tick=lambda snapshots, matched: _emit_wait_tick(snapshots, matched, captured_output_format),
        )
    except KeyboardInterrupt:
        logger.debug("Received keyboard interrupt")
        ctx.exit(EXIT_CODE_ERROR)
        return

    _output_wait_result(result, output_opts.output_format)
    if result.is_matched:
        ctx.exit(EXIT_CODE_SUCCESS)
    elif result.is_timed_out:
        ctx.exit(EXIT_CODE_TIMEOUT)
    else:
        ctx.exit(EXIT_CODE_ERROR)


def _emit_wait_tick(
    snapshots: list[UsageSnapshot],
    matched_source: str | None,
    output_format: OutputFormat,
) -> None:
    """Per-tick progress emission, modeled on ``mngr wait``'s state_change events.

    JSONL: one ``tick`` event per poll with a compact summary. Human: one
    line per tick (silent in JSON mode, which prefers a single final
    payload over noisy progress).
    """
    summary = {
        "matched_source": matched_source,
        "sources": [s.source_name for s in snapshots],
    }
    match output_format:
        case OutputFormat.JSONL:
            emit_event("tick", summary, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if matched_source is not None:
                write_human_line("[{}] matched", matched_source)
            elif snapshots:
                write_human_line("polled: {} (no match yet)", ", ".join(summary["sources"]))
            else:
                write_human_line("polled: no usage data yet")
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
            emit_final_json(payload)
        case OutputFormat.JSONL:
            emit_event("result", payload, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            # ``wait_for_usage`` returns from exactly two paths: match (is_matched=True,
            # is_timed_out=False) or timeout (is_matched=False, is_timed_out=True). The
            # is_matched/timed_out booleans are therefore exhaustive; treat anything
            # else as a programming error and raise so it doesn't silently disappear.
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
    synopsis="mngr usage wait --until CEL [--until CEL ...] [--source NAME ...] [--timeout DURATION] [--interval DURATION]",
    description="""Polls ``mngr usage`` snapshots until at least one source's CEL
context satisfies every ``--until`` expression. Composable with shell:

    mngr usage wait --until 'five_hour.used_percentage < 50 && five_hour.elapsed_percentage > 75' \\
      && mngr message my-agent "ok, kick off the next batch"

The CEL context per source mirrors one entry of ``mngr usage --format
json``'s ``sources`` array, with these derived fields per window:

- ``used_percentage``: from the writer.
- ``resets_at`` / ``seconds_until_reset``: when the window resets.
- ``window_seconds``: window duration (writer-provided; absent for
  variable-duration windows like Claude's overage).
- ``elapsed_seconds`` / ``elapsed_percentage``: derived from
  ``window_seconds`` and ``seconds_until_reset``; absent when
  ``window_seconds`` isn't emitted.

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
            "Restrict to Claude usage only",
            "mngr usage wait --source claude --until 'five_hour.used_percentage < 25'",
        ),
        (
            "Bail out after an hour",
            "mngr usage wait --until 'seven_day.used_percentage < 30' --timeout 1h",
        ),
        (
            "Tighter poll for short-window predicates",
            "mngr usage wait --until 'overage.is_using_overage == false' --interval 10s",
        ),
    ),
    see_also=(
        ("usage", "Show the current snapshot"),
        ("wait", "Wait on agent/host lifecycle state (unrelated)"),
    ),
).register()

add_pager_help_option(wait)
