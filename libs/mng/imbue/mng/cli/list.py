import re
import shutil
import string
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from enum import Enum
from threading import Lock
from typing import Any
from typing import Final

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import BaseModel
from pydantic import PrivateAttr
from tabulate import tabulate

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.api.list import ErrorInfo
from imbue.mng.api.list import list_agents as api_list_agents
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import render_format_template
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.cli.watch_mode import run_watch_loop
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.completion_writer import write_cli_completions_cache
from imbue.mng.utils.terminal import ANSI_DIM_GRAY
from imbue.mng.utils.terminal import ANSI_ERASE_LINE
from imbue.mng.utils.terminal import ANSI_ERASE_TO_END
from imbue.mng.utils.terminal import ANSI_RESET
from imbue.mng.utils.terminal import StderrInterceptor
from imbue.mng.utils.terminal import ansi_cursor_up

_DEFAULT_HUMAN_DISPLAY_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "state",
    "host.name",
    "host.provider_name",
    "host.state",
    "labels",
)

# Custom header labels for fields that would otherwise generate ugly auto-generated headers.
# Fields not listed here use the default: field.upper().replace(".", " ")
_HEADER_LABELS: Final[dict[str, str]] = {
    "host.name": "HOST",
    "host.provider_name": "PROVIDER",
    "host.state": "HOST STATE",
    "host.tags": "TAGS",
    "labels": "LABELS",
    "host.ssh.host": "SSH HOST",
    "idle_timeout_seconds": "IDLE TIMEOUT",
    "activity_sources": "ACTIVITY",
}


@pure
def _is_streaming_eligible(
    is_watch: bool,
    is_sort_explicit: bool,
) -> bool:
    """Whether the general conditions for streaming mode are met.

    Streaming requires: no watch mode (needs repeated full fetches) and no explicit sort
    (needs all results before sorting). A limit is compatible with streaming -- it simply
    caps output at the first N agents to arrive, which is non-deterministic.
    """
    return not is_watch and not is_sort_explicit


@pure
def _should_use_streaming_mode(
    output_format: OutputFormat,
    is_watch: bool,
    is_sort_explicit: bool,
) -> bool:
    """Determine whether to use streaming mode for human list output."""
    return output_format == OutputFormat.HUMAN and _is_streaming_eligible(
        is_watch=is_watch, is_sort_explicit=is_sort_explicit
    )


class ListCliOptions(CommonCliOptions):
    """Options passed from the CLI to the list command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the list() function itself.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...]
    running: bool
    stopped: bool
    local: bool
    remote: bool
    provider: tuple[str, ...]
    project: tuple[str, ...]
    label: tuple[str, ...]
    tag: tuple[str, ...]
    stdin: bool
    fields: str | None
    sort: str
    sort_order: str
    limit: int | None
    watch: int | None
    on_error: str


@click.command(name="list")
@optgroup.group("Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include agents matching CEL expression (repeatable)",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching CEL expression (repeatable)",
)
@optgroup.option(
    "--running",
    is_flag=True,
    help="Show only running agents (alias for --include 'state == \"RUNNING\"')",
)
@optgroup.option(
    "--stopped",
    is_flag=True,
    help="Show only stopped agents (alias for --include 'state == \"STOPPED\"')",
)
@optgroup.option(
    "--local",
    is_flag=True,
    help="Show only local agents (alias for --include 'host.provider == \"local\"')",
)
@optgroup.option(
    "--remote",
    is_flag=True,
    help="Show only remote agents (alias for --exclude 'host.provider == \"local\"')",
)
@optgroup.option(
    "--provider",
    multiple=True,
    help="Show only agents using specified provider (repeatable)",
)
@optgroup.option(
    "--project",
    multiple=True,
    help="Show only agents with this project label (repeatable)",
)
@optgroup.option(
    "--label",
    multiple=True,
    help="Show only agents with this label (format: KEY=VALUE, repeatable) [experimental]",
)
@optgroup.option(
    "--tag",
    multiple=True,
    help="Show only agents on hosts with this tag (format: KEY=VALUE, repeatable)",
)
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read agent and host IDs or names from stdin (one per line)",
)
@optgroup.group("Output Format")
@optgroup.option(
    "--fields",
    help="Which fields to include (comma-separated)",
)
@optgroup.option(
    "--sort",
    default="create_time",
    help="Sort by field (supports nested fields like host.name); enables sorted (non-streaming) output [default: create_time]",
)
@optgroup.option(
    "--sort-order",
    type=click.Choice(["asc", "desc"], case_sensitive=False),
    default="asc",
    help="Sort order [default: asc]",
)
@optgroup.option(
    "--limit",
    type=int,
    help="Limit number of results (applied after fetching from all providers)",
)
@optgroup.group("Watch Mode")
@optgroup.option(
    "-w",
    "--watch",
    type=int,
    help="Continuously watch and update status at specified interval (seconds)",
)
@optgroup.group("Error Handling")
@optgroup.option(
    "--on-error",
    type=click.Choice(["abort", "continue"], case_sensitive=False),
    default="abort",
    help="What to do when errors occur: abort (stop immediately) or continue (keep going)",
)
@add_common_options
@click.pass_context
def list_command(ctx: click.Context, **kwargs) -> None:
    try:
        _list_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)

    if ctx.parent is not None and isinstance(ctx.parent.command, click.Group):
        write_cli_completions_cache(ctx.parent.command)


def _list_impl(ctx: click.Context, **kwargs) -> None:
    """Implementation of list command (extracted for exception handling)."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="list",
        command_class=ListCliOptions,
        is_format_template_supported=True,
    )

    # Format template is now resolved by the common option parsing infrastructure
    # (via --format with a template string, e.g. --format '{name}\t{state}')
    format_template = output_opts.format_template

    # Parse fields if provided
    fields = None
    if opts.fields:
        fields = [f.strip() for f in opts.fields.split(",") if f.strip()]

    # Build list of include filters
    include_filters = list(opts.include)

    # Handle stdin input by converting to CEL filters
    if opts.stdin:
        stdin_refs = [line.strip() for line in sys.stdin if line.strip()]
        if stdin_refs:
            # Create a CEL filter that matches any of the provided refs against
            # host.name, host.id, name, or id (using dot notation for nested fields)
            ref_filters = []
            for ref in stdin_refs:
                ref_filter = f'(name == "{ref}" || id == "{ref}" || host.name == "{ref}" || host.id == "{ref}")'
                ref_filters.append(ref_filter)
            # Combine all ref filters with OR
            combined_filter = " || ".join(ref_filters)
            include_filters.append(combined_filter)

    # --running: alias for --include 'state == "RUNNING"'
    # --stopped: alias for --include 'state == "STOPPED"'
    # --local: alias for --include 'host.provider == "local"'
    # --remote: alias for --exclude 'host.provider == "local"'
    if opts.running:
        include_filters.append(f'state == "{AgentLifecycleState.RUNNING.value}"')
    if opts.stopped:
        include_filters.append(f'state == "{AgentLifecycleState.STOPPED.value}"')
    if opts.local:
        include_filters.append('host.provider == "local"')

    # --project X: alias for --include 'labels.project == "X"'
    # Multiple values are OR'd together
    if opts.project:
        project_parts = [f'labels.project == "{p}"' for p in opts.project]
        include_filters.append(" || ".join(project_parts))

    # --label K=V: alias for --include 'labels.K == "V"'
    # Multiple values are OR'd together
    if opts.label:
        label_parts = []
        for label_spec in opts.label:
            if "=" not in label_spec:
                raise click.BadParameter(f"Label must be in KEY=VALUE format, got: {label_spec}", param_hint="--label")
            key, value = label_spec.split("=", 1)
            label_parts.append(f'labels.{key} == "{value}"')
        include_filters.append(" || ".join(label_parts))

    # --tag K=V: alias for --include 'host.tags.K == "V"'
    # Multiple values are OR'd together
    if opts.tag:
        tag_parts = []
        for tag_spec in opts.tag:
            if "=" not in tag_spec:
                raise click.BadParameter(f"Tag must be in KEY=VALUE format, got: {tag_spec}", param_hint="--tag")
            key, value = tag_spec.split("=", 1)
            tag_parts.append(f'host.tags.{key} == "{value}"')
        include_filters.append(" || ".join(tag_parts))

    # Build list of exclude filters
    exclude_filters = list(opts.exclude)
    if opts.remote:
        exclude_filters.append('host.provider == "local"')

    # --sort FIELD: Sort by any available field [default: create_time]
    # --sort-order ORDER: Sort order (asc, desc) [default: asc]
    sort_field = opts.sort
    sort_reverse = opts.sort_order.lower() == "desc"

    # --limit N: Limit number of results returned
    # NOTE: The limit is applied after fetching results. The full list is still retrieved
    # from providers and then sliced client-side. For large deployments, this means the
    # command may still take time proportional to the total number of agents.
    limit = opts.limit

    error_behavior = ErrorBehavior(opts.on_error.upper())

    include_filters_tuple = tuple(include_filters)
    exclude_filters_tuple = tuple(exclude_filters)
    provider_names = opts.provider if opts.provider else None

    # Dispatch to the appropriate output path
    if output_opts.output_format == OutputFormat.JSONL:
        _list_jsonl(
            ctx,
            mng_ctx,
            include_filters_tuple,
            exclude_filters_tuple,
            provider_names,
            error_behavior,
            limit,
            is_watch=bool(opts.watch),
        )
        return

    # Determine if --sort or --sort-order was explicitly set by the user (vs using the default)
    sort_source = ctx.get_parameter_source("sort")
    sort_order_source = ctx.get_parameter_source("sort_order")
    is_sort_explicit = (sort_source is not None and sort_source != click.core.ParameterSource.DEFAULT) or (
        sort_order_source is not None and sort_order_source != click.core.ParameterSource.DEFAULT
    )

    # Template output path: if --format is a template string, use streaming when possible, batch otherwise
    if format_template is not None:
        is_streaming_template = _is_streaming_eligible(is_watch=bool(opts.watch), is_sort_explicit=is_sort_explicit)
        if is_streaming_template:
            _list_streaming_template(
                ctx,
                mng_ctx,
                include_filters_tuple,
                exclude_filters_tuple,
                provider_names,
                error_behavior,
                format_template,
                limit,
            )
            return
        # Fall through to batch path with format_template set

    # Streaming mode trades sorted output for faster time-to-first-result: agents display
    # as each provider completes rather than waiting for all providers. Users who need sorted
    # output can pass --sort explicitly, which falls back to batch mode. When --limit is set,
    # streaming still works but produces non-deterministic results (whichever agents arrive first).
    if format_template is None and _should_use_streaming_mode(
        output_opts.output_format, is_watch=bool(opts.watch), is_sort_explicit=is_sort_explicit
    ):
        display_fields = fields if fields is not None else list(_DEFAULT_HUMAN_DISPLAY_FIELDS)
        _list_streaming_human(
            ctx,
            mng_ctx,
            include_filters_tuple,
            exclude_filters_tuple,
            provider_names,
            error_behavior,
            display_fields,
            limit,
        )
        return

    # Batch/watch path
    iteration_params = _ListIterationParams(
        mng_ctx=mng_ctx,
        output_opts=output_opts,
        include_filters=include_filters_tuple,
        exclude_filters=exclude_filters_tuple,
        provider_names=provider_names,
        error_behavior=error_behavior,
        sort_field=sort_field,
        sort_reverse=sort_reverse,
        limit=limit,
        fields=fields,
        format_template=format_template,
    )

    if opts.watch:
        try:
            run_watch_loop(
                iteration_fn=lambda: _run_list_iteration(iteration_params, ctx),
                interval_seconds=opts.watch,
                on_error_continue=True,
            )
        except KeyboardInterrupt:
            logger.info("\nWatch mode stopped")
            return
    else:
        _run_list_iteration(iteration_params, ctx)


def _list_jsonl(
    ctx: click.Context,
    mng_ctx: MngContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    error_behavior: ErrorBehavior,
    limit: int | None,
    is_watch: bool,
) -> None:
    """JSONL output path: stream agents as JSONL lines with optional limit."""
    if is_watch:
        logger.warning("Watch mode is not supported with JSONL format, running once")

    limited_callback = _LimitedJsonlEmitter(limit=limit)

    result = api_list_agents(
        mng_ctx=mng_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=error_behavior,
        on_agent=limited_callback,
        on_error=_emit_jsonl_error,
        is_streaming=False,
    )
    # Exit with non-zero code if there were errors (per error_handling.md spec)
    if result.errors:
        ctx.exit(1)


def _list_streaming_human(
    ctx: click.Context,
    mng_ctx: MngContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    error_behavior: ErrorBehavior,
    fields: list[str],
    limit: int | None,
) -> None:
    """Streaming human output path: display agents as each provider completes."""
    renderer = _StreamingHumanRenderer(fields=fields, is_tty=sys.stdout.isatty(), output=sys.stdout, limit=limit)

    # In TTY mode, intercept stderr so warnings (from loguru etc.) are routed
    # through the renderer and kept pinned at the bottom of the table, rather
    # than being interspersed with agent rows.
    interceptor = StderrInterceptor(callback=renderer.emit_warning, original_stderr=sys.stderr)
    with interceptor if renderer.is_tty else nullcontext():
        renderer.start()
        try:
            result = api_list_agents(
                mng_ctx=mng_ctx,
                include_filters=include_filters,
                exclude_filters=exclude_filters,
                provider_names=provider_names,
                error_behavior=error_behavior,
                on_agent=renderer,
                is_streaming=True,
            )
        finally:
            renderer.finish()

    if result.errors:
        for error in result.errors:
            logger.warning("{}: {}", error.exception_type, error.message)
        ctx.exit(1)


def _list_streaming_template(
    ctx: click.Context,
    mng_ctx: MngContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    error_behavior: ErrorBehavior,
    format_template: str,
    limit: int | None,
) -> None:
    """Streaming template output path: write one template-expanded line per agent."""
    emitter = _StreamingTemplateEmitter(format_template=format_template, output=sys.stdout, limit=limit)

    result = api_list_agents(
        mng_ctx=mng_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=error_behavior,
        on_agent=emitter,
        is_streaming=True,
    )

    if result.errors:
        for error in result.errors:
            logger.warning("{}: {}", error.exception_type, error.message)
        ctx.exit(1)


class _LimitedJsonlEmitter(MutableModel):
    """Callable that emits JSONL output with an optional limit."""

    limit: int | None
    count: int = 0
    _lock: Lock = PrivateAttr(default_factory=Lock)

    def __call__(self, agent: AgentInfo) -> None:
        with self._lock:
            if self.limit is not None and self.count >= self.limit:
                return
            _emit_jsonl_agent(agent)
            self.count += 1


class _StreamingTemplateEmitter(MutableModel):
    """Callable that writes one template-expanded line per agent."""

    format_template: str
    output: Any
    limit: int | None = None
    _lock: Lock = PrivateAttr(default_factory=Lock)
    _count: int = PrivateAttr(default=0)

    def __call__(self, agent: AgentInfo) -> None:
        line = _render_format_template(self.format_template, agent)
        with self._lock:
            if self.limit is not None and self._count >= self.limit:
                return
            self.output.write(line + "\n")
            self.output.flush()
            self._count += 1


# Minimum column widths for streaming output (left-justified, not truncated).
# These are the minimum data widths; actual column width is max(min_width, header_length).
_MIN_COLUMN_WIDTHS: Final[dict[str, int]] = {
    "name": 15,
    "host.name": 10,
    "host.provider_name": 10,
    "host.state": 10,
    "state": 10,
    "labels": 10,
    "host.tags": 10,
}
_DEFAULT_MIN_COLUMN_WIDTH: Final[int] = 10
# Columns that get extra space when the terminal is wider than the minimum
_EXPANDABLE_COLUMNS: Final[set[str]] = {"name", "labels"}
_MAX_COLUMN_WIDTHS: Final[dict[str, int]] = {}
_COLUMN_SEPARATOR: Final[str] = "  "


@pure
def _format_status_line(count: int) -> str:
    """Format the dim 'Searching...' status line with an optional count."""
    count_text = f" ({count} found)" if count > 0 else ""
    return f"{ANSI_DIM_GRAY}Searching...{count_text}{ANSI_RESET}"


class _StreamingHumanRenderer(MutableModel):
    """Thread-safe streaming renderer for human-readable list output.

    Writes table rows to stdout as agents arrive from the API. Uses an ANSI status
    line ("Searching...") that gets replaced by data rows on TTY outputs. On non-TTY
    outputs (piped), skips status lines and ANSI codes entirely.

    Warnings emitted during streaming (via emit_warning) are kept pinned at the
    bottom of the table output, above the status line. When new agent rows arrive,
    the warnings are moved down so they always remain at the bottom.

    When limit is set, stops displaying agents after the limit is reached. Results
    are non-deterministic since streaming does not sort.
    """

    fields: list[str]
    is_tty: bool
    output: Any
    limit: int | None = None
    _lock: Lock = PrivateAttr(default_factory=Lock)
    _count: int = PrivateAttr(default=0)
    _is_header_written: bool = PrivateAttr(default=False)
    _column_widths: dict[str, int] = PrivateAttr(default_factory=dict)
    _warning_texts: list[str] = PrivateAttr(default_factory=list)
    _warning_line_count: int = PrivateAttr(default=0)

    def start(self) -> None:
        """Compute column widths and write the initial status line (TTY only)."""
        terminal_width = shutil.get_terminal_size((120, 24)).columns
        self._column_widths = _compute_column_widths(self.fields, terminal_width)

        if self.is_tty:
            self.output.write(_format_status_line(0))
            self.output.flush()

    def emit_warning(self, text: str) -> None:
        """Write a warning, keeping it pinned below agent rows and above the status line."""
        with self._lock:
            if self.is_tty:
                # Erase the status line so the warning appears cleanly
                self.output.write(ANSI_ERASE_LINE)

            self.output.write(text)
            self._warning_texts.append(text)
            self._warning_line_count += text.count("\n")

            if self.is_tty:
                # Re-write the status line below the warning
                self.output.write(_format_status_line(self._count))

            self.output.flush()

    def __call__(self, agent: AgentInfo) -> None:
        """Handle a single agent result (on_agent callback)."""
        with self._lock:
            if self.limit is not None and self._count >= self.limit:
                return

            if self.is_tty:
                # Erase the current status line
                self.output.write(ANSI_ERASE_LINE)

                # If there are warnings below the agent rows, move cursor up
                # past them and erase to end of screen. The warnings will be
                # re-written after the new agent row so they stay at the bottom.
                if self._warning_line_count > 0:
                    self.output.write(ansi_cursor_up(self._warning_line_count))
                    self.output.write(ANSI_ERASE_TO_END)

            # Write header on first agent
            if not self._is_header_written:
                header_line = _format_streaming_header_row(self.fields, self._column_widths)
                self.output.write(header_line + "\n")
                self._is_header_written = True

            # Write the agent row
            row_line = _format_streaming_agent_row(agent, self.fields, self._column_widths)
            self.output.write(row_line + "\n")
            self._count += 1

            if self.is_tty:
                # Re-write warnings below the new agent row (only in TTY mode
                # where they were erased by cursor-up + erase-to-end above)
                for warning_text in self._warning_texts:
                    self.output.write(warning_text)

                # Write updated status line
                self.output.write(_format_status_line(self._count))

            self.output.flush()

    def finish(self) -> None:
        """Clean up the status line after all providers have completed."""
        with self._lock:
            if self.is_tty:
                # Erase the final status line (warnings remain visible at the bottom)
                self.output.write(ANSI_ERASE_LINE)
                self.output.flush()

            if self._count == 0:
                write_human_line("No agents found")


@pure
def _get_header_label(field: str) -> str:
    """Get the display label for a column header."""
    if field in _HEADER_LABELS:
        return _HEADER_LABELS[field]
    return field.upper().replace(".", " ")


@pure
def _compute_column_widths(fields: Sequence[str], terminal_width: int) -> dict[str, int]:
    """Compute column widths sized to the terminal, distributing extra space to expandable columns."""
    separator_total = len(_COLUMN_SEPARATOR) * max(len(fields) - 1, 0)

    # Start with minimum widths, ensuring each column is at least as wide as its header
    width_by_field: dict[str, int] = {}
    for field in fields:
        min_data_width = _MIN_COLUMN_WIDTHS.get(field, _DEFAULT_MIN_COLUMN_WIDTH)
        header_width = len(_get_header_label(field))
        width_by_field[field] = max(min_data_width, header_width)

    min_total = sum(width_by_field.values()) + separator_total
    extra_space = max(terminal_width - min_total, 0)

    # Distribute extra space to expandable columns, respecting max widths.
    # Process columns sorted by tightest max cap first so capped leftovers flow to less
    # constrained columns in a single pass.
    expandable_in_fields = [f for f in fields if f in _EXPANDABLE_COLUMNS]
    if expandable_in_fields and extra_space > 0:
        sorted_expandable = sorted(expandable_in_fields, key=lambda f: _MAX_COLUMN_WIDTHS.get(f, float("inf")))
        remaining = extra_space
        for idx, field in enumerate(sorted_expandable):
            fields_left = len(sorted_expandable) - idx
            per_column = remaining // fields_left
            extra = 1 if (remaining % fields_left) > 0 else 0
            bonus = per_column + extra
            max_width = _MAX_COLUMN_WIDTHS.get(field)
            if max_width is not None and width_by_field[field] + bonus > max_width:
                bonus = max(max_width - width_by_field[field], 0)
            width_by_field[field] = width_by_field[field] + bonus
            remaining = remaining - bonus

    return width_by_field


@pure
def _format_streaming_header_row(fields: Sequence[str], column_widths: dict[str, int]) -> str:
    """Format the header row of streaming output with computed column widths."""
    parts: list[str] = []
    for field in fields:
        width = column_widths.get(field, _DEFAULT_MIN_COLUMN_WIDTH)
        value = _get_header_label(field)
        parts.append(value.ljust(width))
    return _COLUMN_SEPARATOR.join(parts)


@pure
def _format_streaming_agent_row(agent: AgentInfo, fields: Sequence[str], column_widths: dict[str, int]) -> str:
    """Format a single agent as a streaming output row."""
    parts: list[str] = []
    for field in fields:
        width = column_widths.get(field, _DEFAULT_MIN_COLUMN_WIDTH)
        value = _get_field_value(agent, field)
        # Values are padded but intentionally not truncated: full values are preferred
        # over truncated ones, so columns may appear ragged when values exceed the width.
        parts.append(value.ljust(width))
    return _COLUMN_SEPARATOR.join(parts)


class _ListIterationParams(BaseModel):
    """Parameters for a single list iteration, used for watch mode."""

    model_config = {"arbitrary_types_allowed": True}

    mng_ctx: MngContext
    output_opts: OutputOptions
    include_filters: tuple[str, ...]
    exclude_filters: tuple[str, ...]
    provider_names: tuple[str, ...] | None
    error_behavior: ErrorBehavior
    sort_field: str
    sort_reverse: bool
    limit: int | None
    fields: list[str] | None
    format_template: str | None = None


def _run_list_iteration(params: _ListIterationParams, ctx: click.Context) -> None:
    """Run a single list iteration."""
    result = api_list_agents(
        mng_ctx=params.mng_ctx,
        include_filters=params.include_filters,
        exclude_filters=params.exclude_filters,
        provider_names=params.provider_names,
        error_behavior=params.error_behavior,
        is_streaming=False,
    )

    if result.errors:
        for error in result.errors:
            logger.warning("{}: {}", error.exception_type, error.message)

    # Apply sorting to results
    agents_to_display = _sort_agents(result.agents, params.sort_field, params.sort_reverse)

    # Apply limit to results (after sorting)
    if params.limit is not None:
        agents_to_display = agents_to_display[: params.limit]

    if not agents_to_display:
        if params.format_template is not None:
            # Template mode: silent empty output (consistent with scripting use)
            pass
        elif params.output_opts.output_format == OutputFormat.HUMAN:
            write_human_line("No agents found")
        elif params.output_opts.output_format == OutputFormat.JSON:
            emit_final_json({"agents": [], "errors": result.errors})
        else:
            # JSONL is handled above with streaming, so this should be unreachable
            raise AssertionError(f"Unexpected output format: {params.output_opts.output_format}")
        # Exit with non-zero code if there were errors (per error_handling.md spec)
        if result.errors:
            ctx.exit(1)
        return

    # Template output takes precedence over format-based dispatch
    if params.format_template is not None:
        _emit_template_output(agents_to_display, params.format_template, output=sys.stdout)
    elif params.output_opts.output_format == OutputFormat.HUMAN:
        _emit_human_output(agents_to_display, params.fields)
    elif params.output_opts.output_format == OutputFormat.JSON:
        _emit_json_output(agents_to_display, result.errors)
    else:
        # JSONL is handled above with streaming, so this should be unreachable
        raise AssertionError(f"Unexpected output format: {params.output_opts.output_format}")

    # Exit with non-zero code if there were errors (per error_handling.md spec)
    if result.errors:
        ctx.exit(1)


def _emit_json_output(agents: list[AgentInfo], errors: list[ErrorInfo]) -> None:
    """Emit JSON output with all agents."""
    agents_data = [agent.model_dump(mode="json") for agent in agents]
    errors_data = [error.model_dump(mode="json") for error in errors]
    output_data = {
        "agents": agents_data,
        "errors": errors_data,
    }
    emit_final_json(output_data)


def _emit_jsonl_agent(agent: AgentInfo) -> None:
    """Emit a single agent as a JSONL line (streaming callback)."""
    agent_data = agent.model_dump(mode="json")
    emit_final_json(agent_data)


def _emit_jsonl_error(error: ErrorInfo) -> None:
    """Emit a single error as a JSONL line (streaming callback)."""
    error_data = {"event": "error", **error.model_dump(mode="json")}
    emit_final_json(error_data)


def _emit_human_output(agents: list[AgentInfo], fields: list[str] | None = None) -> None:
    """Emit human-readable table output with optional field selection."""
    if not agents:
        return

    # Default fields if none specified
    if fields is None:
        fields = list(_DEFAULT_HUMAN_DISPLAY_FIELDS)

    # Build table data dynamically based on requested fields
    headers = []
    rows = []

    # Generate headers
    for field in fields:
        headers.append(_get_header_label(field))

    # Generate rows
    for agent in agents:
        row = []
        for field in fields:
            value = _get_field_value(agent, field)
            row.append(value)
        rows.append(row)

    # Generate table
    table = tabulate(rows, headers=headers, tablefmt="plain")
    write_human_line("\n" + table)


def _emit_template_output(agents: list[AgentInfo], template: str, output: Any) -> None:
    """Emit template-formatted output, one line per agent."""
    for agent in agents:
        line = _render_format_template(template, agent)
        output.write(line + "\n")
    output.flush()


def _parse_slice_spec(spec: str) -> int | slice | None:
    """Parse a bracket slice specification like '0', '-1', ':3', '1:3', or '1:'.

    Returns an int for single index, slice object for ranges, or None if invalid.
    """
    spec = spec.strip()

    try:
        # Check if it's a slice (contains ':')
        if ":" in spec:
            parts = spec.split(":")
            if len(parts) == 2:
                start_str, stop_str = parts
                start = int(start_str) if start_str else None
                stop = int(stop_str) if stop_str else None
                return slice(start, stop)
            elif len(parts) == 3:
                start_str, stop_str, step_str = parts
                start = int(start_str) if start_str else None
                stop = int(stop_str) if stop_str else None
                step = int(step_str) if step_str else None
                return slice(start, stop, step)
            else:
                # Invalid slice format (too many colons)
                return None
        else:
            # Simple index
            return int(spec)
    except ValueError:
        # Could not parse integers in the spec
        return None


def _format_value_as_string(value: Any) -> str:
    """Convert a value to string representation for display."""
    if value is None:
        return ""
    elif isinstance(value, dict):
        if not value:
            return ""
        return ", ".join(f"{k}={v}" for k, v in value.items())
    elif isinstance(value, Enum):
        return str(value.value)
    elif hasattr(value, "name") and hasattr(value, "id"):
        # For objects like SnapshotInfo that have both name and id, prefer name
        return str(value.name)
    elif isinstance(value, (tuple, list)) and not isinstance(value, str):
        return ", ".join(_format_value_as_string(item) for item in value)
    elif isinstance(value, str):
        return value
    else:
        return str(value)


# Pattern to match a field part with optional bracket notation
# Matches: "fieldname", "fieldname[0]", "fieldname[-1]", "fieldname[:3]", "fieldname[1:3]", etc.
_BRACKET_PATTERN = re.compile(r"^([^\[]+)(?:\[([^\]]+)\])?$")


def _get_sortable_value(agent: AgentInfo, field: str) -> Any:
    """Extract a field value from an AgentInfo object for sorting.

    Returns the raw value (not string-formatted) for proper sorting behavior.
    Supports nested fields like "host.name".
    """
    # Handle nested fields (e.g., "host.name")
    # Also supports dict key access for plugin fields (e.g., "host.plugin.aws.iam_user")
    parts = field.split(".")
    value: Any = agent

    try:
        for part in parts:
            # Strip any bracket notation for sorting (use base field only)
            base_part = part.split("[")[0]
            if hasattr(value, base_part):
                value = getattr(value, base_part)
            elif isinstance(value, dict) and base_part in value:
                value = value[base_part]
            else:
                return None
        return value
    except (AttributeError, KeyError):
        return None


class _AgentSortKey:
    """Callable class for sorting agents by a field (avoids inline function definitions)."""

    sort_field: str

    def __call__(self, agent: AgentInfo) -> tuple[int, Any]:
        value = _get_sortable_value(agent, self.sort_field)
        if value is None:
            return (1, "")
        if hasattr(value, "value"):
            value = value.value
        return (0, str(value))


def _sort_agents(agents: list[AgentInfo], sort_field: str, reverse: bool) -> list[AgentInfo]:
    """Sort a list of agents by the specified field."""
    key = _AgentSortKey()
    key.sort_field = sort_field
    return sorted(agents, key=key, reverse=reverse)


def _get_field_value(agent: AgentInfo, field: str) -> str:
    """Extract a field value from an AgentInfo object and return as string.

    Supports nested fields like "host.name" and list slicing syntax like
    "host.snapshots[0]" or "host.snapshots[:3]".
    """
    # Handle nested fields (e.g., "host.name") with optional bracket notation
    # Also supports dict key access for plugin fields (e.g., "host.plugin.aws.iam_user")
    parts = field.split(".")
    value: Any = agent

    try:
        for part in parts:
            # Parse the part for bracket notation
            match = _BRACKET_PATTERN.match(part)
            if not match:
                return ""

            field_name = match.group(1)
            # bracket_spec may be None if no brackets present in the part
            bracket_spec = match.group(2)

            # Get the field value: try object attribute first, then dict key
            if hasattr(value, field_name):
                value = getattr(value, field_name)
            elif isinstance(value, dict) and field_name in value:
                value = value[field_name]
            else:
                return ""

            # Apply bracket indexing/slicing if present
            if bracket_spec is not None:
                if not isinstance(value, (list, tuple, Sequence)) or isinstance(value, str):
                    return ""

                index_or_slice = _parse_slice_spec(bracket_spec)
                if index_or_slice is None:
                    return ""

                try:
                    value = value[index_or_slice]
                except (IndexError, ValueError):
                    # IndexError: out of bounds index
                    # ValueError: slice step cannot be zero
                    return ""

                # If the result is a list (from slicing), format each element
                if isinstance(value, (list, tuple)) and not isinstance(value, str):
                    return ", ".join(_format_value_as_string(item) for item in value)

        return _format_value_as_string(value)
    except (AttributeError, KeyError):
        return ""


@pure
def _render_format_template(template: str, agent: AgentInfo) -> str:
    """Expand a str.format()-style template using agent field values.

    Pre-resolves field names via _get_field_value() (which supports nested
    attribute access and bracket notation on AgentInfo), then delegates
    template expansion to the shared render_format_template helper.
    """
    # Pre-resolve all referenced field names using the agent-specific field resolver
    field_values: dict[str, str] = {}
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            field_values[field_name] = _get_field_value(agent, field_name)
    return render_format_template(template, field_values)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="list",
    one_line_description="List all agents managed by mng",
    synopsis="mng [list|ls] [OPTIONS]",
    description="""Displays agents with their status, host information, and other metadata.
Supports filtering, sorting, and multiple output formats.""",
    aliases=("ls",),
    examples=(
        ("List all agents", "mng list"),
        ("List only running agents", "mng list --running"),
        ("List agents on Docker hosts", "mng list --provider docker"),
        ("List agents for a project", "mng list --project mng"),
        ("List agents with a specific label", "mng list --label env=prod"),
        ("List agents with a specific host tag", "mng list --tag env=prod"),
        ("List agents as JSON", "mng list --format json"),
        ("Filter with CEL expression", "mng list --include 'name.contains(\"prod\")'"),
    ),
    additional_sections=(
        (
            "CEL Filter Examples",
            """CEL (Common Expression Language) filters allow powerful, expressive filtering of agents.
All agent fields from the "Available Fields" section can be used in filter expressions.

**Simple equality filters:**
- `name == "my-agent"` - Match agent by exact name
- `state == "RUNNING"` - Match running agents
- `host.provider == "docker"` - Match agents on Docker hosts
- `type == "claude"` - Match agents of type "claude"
- `labels.project == "mng"` - Match agents with a specific project label

**Compound expressions:**
- `state == "RUNNING" && host.provider == "modal"` - Running agents on Modal
- `state == "STOPPED" || state == "FAILED"` - Stopped or failed agents
- `host.provider == "docker" && name.startsWith("test-")` - Docker agents with names starting with "test-"

**String operations:**
- `name.contains("prod")` - Agent names containing "prod"
- `name.startsWith("staging-")` - Agent names starting with "staging-"
- `name.endsWith("-dev")` - Agent names ending with "-dev"

**Numeric comparisons:**
- `runtime_seconds > 3600` - Agents running for more than an hour
- `idle_seconds < 300` - Agents active in the last 5 minutes
- `host.resource.memory_gb >= 8` - Agents on hosts with 8GB+ memory
- `host.uptime_seconds > 86400` - Agents on hosts running for more than a day

**Existence checks:**
- `has(url)` - Agents that have a URL set
- `has(host.ssh)` - Agents on remote hosts with SSH access
""",
        ),
        (
            "Available Fields",
            """**Agent fields** (same syntax for `--fields` and CEL filters):
- `name` - Agent name
- `id` - Agent ID
- `type` - Agent type (claude, codex, etc.)
- `command` - The command used to start the agent
- `url` - URL where the agent can be accessed (if reported)
- `work_dir` - Working directory for this agent
- `create_time` - Creation timestamp
- `start_time` - Timestamp for when the agent was last started
- `runtime_seconds` - How long the agent has been running
- `user_activity_time` - Timestamp of the last user activity
- `agent_activity_time` - Timestamp of the last agent activity
- `idle_seconds` - How long since the agent was active
- `idle_mode` - Idle detection mode
- `idle_timeout_seconds` - Idle timeout before host stops
- `activity_sources` - Activity sources used for idle detection
- `start_on_boot` - Whether the agent is set to start on host boot
- `state` - Agent lifecycle state (RUNNING, STOPPED, WAITING, REPLACED, DONE)
- `labels` - Agent labels (key-value pairs, e.g., project=mng)
- `labels.$KEY` - Specific label value (e.g., `labels.project`)
- `plugin.$PLUGIN_NAME.*` - Plugin-defined fields (e.g., `plugin.chat_history.messages`)

**Host fields** (dot notation for both `--fields` and CEL filters):
- `host.name` - Host name
- `host.id` - Host ID
- `host.provider_name` - Host provider (local, docker, modal, etc.) (in CEL filters, use `host.provider`)
- `host.state` - Current host state (RUNNING, STOPPED, BUILDING, etc.)
- `host.image` - Host image (Docker image name, Modal image ID, etc.)
- `host.tags` - Metadata tags for the host
- `host.ssh_activity_time` - Timestamp of the last SSH connection to the host
- `host.boot_time` - When the host was last started
- `host.uptime_seconds` - How long the host has been running
- `host.resource` - Resource limits for the host
  - `host.resource.cpu.count` - Number of CPUs
  - `host.resource.cpu.frequency_ghz` - CPU frequency in GHz
  - `host.resource.memory_gb` - Memory in GB
  - `host.resource.disk_gb` - Disk space in GB
  - `host.resource.gpu.count` - Number of GPUs
  - `host.resource.gpu.model` - GPU model name
  - `host.resource.gpu.memory_gb` - GPU memory in GB
- `host.ssh` - SSH access details (remote hosts only)
  - `host.ssh.command` - Full SSH command to connect
  - `host.ssh.host` - SSH hostname
  - `host.ssh.port` - SSH port
  - `host.ssh.user` - SSH username
  - `host.ssh.key_path` - Path to SSH private key
- `host.snapshots` - List of available snapshots
- `host.is_locked` - Whether the host is currently locked for an operation
- `host.locked_time` - When the host was locked
- `host.plugin.$PLUGIN_NAME.*` - Host plugin fields (e.g., `host.plugin.aws.iam_user`)

**Notes:**
- You can use Python-style list slicing for list fields (e.g., `host.snapshots[0]` for the first snapshot, `host.snapshots[:3]` for the first 3)
""",
        ),
        (
            "Related Documentation",
            """- [Multi-target Options](../generic/multi_target.md) - Behavior when some agents cannot be accessed
- [Common Options](../generic/common.md) - Common CLI options for output format, logging, etc.""",
        ),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("connect", "Connect to an existing agent"),
        ("destroy", "Destroy agents"),
    ),
).register()

# Add pager-enabled help option to the list command
add_pager_help_option(list_command)
