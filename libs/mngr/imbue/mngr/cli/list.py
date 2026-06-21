import re
import shutil
import string
import sys
from collections.abc import Sequence
from enum import Enum
from threading import Lock
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import BaseModel
from pydantic import PrivateAttr
from tabulate import tabulate

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.api.list import ErrorInfo
from imbue.mngr.api.list import ProviderErrorInfo
from imbue.mngr.api.list import build_agent_cel_context
from imbue.mngr.api.list import list_agents as api_list_agents
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.completion_install import write_managed_completion_scripts
from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.field_catalog import FieldContext
from imbue.mngr.cli.field_catalog import build_list_field_catalog
from imbue.mngr.cli.field_catalog import catalog_rows_as_dicts
from imbue.mngr.cli.field_catalog import render_catalog_help_markdown
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import add_agent_filter_options
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_topics import get_all_topics
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.output_helpers import write_stderr_line
from imbue.mngr.colors import ERROR_COLOR
from imbue.mngr.colors import RESET_COLOR
from imbue.mngr.colors import should_use_color
from imbue.mngr.config.agent_alias_registry import list_agent_aliases
from imbue.mngr.config.completion_writer import write_cli_completions_cache
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.cel_utils import compile_cel_sort_keys
from imbue.mngr.utils.cel_utils import evaluate_cel_sort_key
from imbue.mngr.utils.terminal import ANSI_DIM_GRAY
from imbue.mngr.utils.terminal import ANSI_ERASE_LINE
from imbue.mngr.utils.terminal import ANSI_RESET
from imbue.mngr.uv_tool import get_installed_plugin_package_names

_DEFAULT_HUMAN_DISPLAY_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "state",
    "host.name",
    "host.provider_name",
    "host.state",
    "labels.project",
)

# Custom header labels for fields that would otherwise generate ugly auto-generated headers.
# Fields not listed here use the default: field.upper().replace(".", " ")
_HEADER_LABELS: Final[dict[str, str]] = {
    "host.name": "HOST",
    "host.provider_name": "PROVIDER",
    "host.state": "HOST STATE",
    "host.tags": "HOST LABELS",
    "labels": "LABELS",
    "labels.project": "PROJECT",
    "host.ssh.host": "SSH HOST",
    "idle_timeout_seconds": "IDLE TIMEOUT",
    "activity_sources": "ACTIVITY",
}

# Aliases that let users reference fields by the same name in --fields/--format
# templates as they do in CEL filters and --sort. host.provider is the short form
# documented for CEL; the underlying attribute is host.provider_name.
_FIELD_ALIASES: Final[dict[str, str]] = {
    "host.provider": "host.provider_name",
    # `project` is the documented short form for the project label, mirroring
    # the `--project` filter flag; the underlying data lives in labels.project.
    "project": "labels.project",
}


@pure
def _resolve_field_alias(field: str) -> str:
    """Map a user-supplied field name to its canonical form for attribute/dict lookups."""
    return _FIELD_ALIASES.get(field, field)


@pure
def _is_streaming_eligible(is_sort_explicit: bool) -> bool:
    """Whether the general conditions for streaming mode are met.

    Streaming requires no explicit sort (needs all results before sorting). A limit is
    compatible with streaming -- it simply caps output at the first N agents to arrive,
    which is non-deterministic.
    """
    return not is_sort_explicit


@pure
def _should_use_streaming_mode(output_format: OutputFormat, is_sort_explicit: bool) -> bool:
    """Determine whether to use streaming mode for human list output."""
    return output_format == OutputFormat.HUMAN and _is_streaming_eligible(is_sort_explicit=is_sort_explicit)


class ListCliOptions(AgentFilterCliOptions, CommonCliOptions):
    """Options passed from the CLI to the list command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits filter options (include, exclude, running, ...) from AgentFilterCliOptions
    and common options (output_format, quiet, verbose, ...) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the list() function itself.
    """

    provider: tuple[str, ...]
    stdin: bool
    schema_view: bool
    fields: str | None
    header: tuple[str, ...]
    sort: str
    limit: int | None
    ids: bool
    addrs: bool
    on_error: str


@click.command(name="list")
@add_agent_filter_options
# --provider and --stdin are intentionally NOT in add_agent_filter_options:
# --provider selects which providers to query (a fan-out control passed
# through to api_list_agents as provider_names, not a CEL filter on results),
# and --stdin reads refs from stdin which only makes sense for batch list.
@optgroup.option(
    "--provider",
    multiple=True,
    help="Show only agents using specified provider (repeatable)",
)
@optgroup.option(
    "--stdin",
    is_flag=True,
    help="Read agent and host IDs or names from stdin (one per line)",
)
@optgroup.group("Output Format")
@optgroup.option(
    "--ids",
    is_flag=True,
    help="Print only agent IDs, one per line",
)
@optgroup.option(
    "--addrs",
    is_flag=True,
    help="Print only agent addresses (name@host.provider), one per line",
)
@optgroup.option(
    "--schema",
    "schema_view",
    is_flag=True,
    default=False,
    help="List the fields referenceable in --include/--exclude, --sort, and --fields/--format "
    "(with their types and the contexts they work in), instead of listing agents.",
)
@optgroup.option(
    "--fields",
    help="Which fields to include (comma-separated)",
)
@optgroup.option(
    "--header",
    multiple=True,
    help="Override column header label (format: FIELD=LABEL, repeatable)",
)
@optgroup.option(
    "--sort",
    default="create_time",
    help="Sort by CEL expression(s) with optional direction, e.g. 'name asc, create_time desc'; enables sorted (non-streaming) output [default: create_time]",
)
@optgroup.option(
    "--limit",
    type=int,
    help="Limit number of results (applied after fetching from all providers)",
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
        ctx.exit(EXIT_CODE_ERROR)
    except ProviderUnavailableError as e:
        # Abort mode (the default): a single unreachable or unauthenticated provider
        # propagated out of discovery. Show its clean, attributable message and exit
        # with the granular provider-inaccessible code (same code used in continue mode
        # and by gc), rather than the generic exit code Click would use otherwise.
        e.show()
        ctx.exit(EXIT_CODE_PROVIDER_INACCESSIBLE)


def _refresh_completion_artifacts(**kwargs: Any) -> None:
    """Refresh the tab-completion cache and the managed completion script files.

    Run in the background from ``list`` so an upgraded mngr keeps the installed
    completion (which the rc shim sources) current without any manual steps.
    """
    write_cli_completions_cache(**kwargs)
    write_managed_completion_scripts()


def _list_impl(ctx: click.Context, **kwargs) -> None:
    """Implementation of list command (extracted for exception handling)."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="list",
        command_class=ListCliOptions,
        is_format_template_supported=True,
    )

    # --schema dumps the static field catalog and ignores every agent-selection
    # option, so reject those combinations rather than silently dropping them.
    if opts.schema_view:
        _reject_schema_conflicts(opts)
        _emit_field_schema(output_opts)
        return

    # Write the tab completion cache in the background so it doesn't block
    # the list output. The cache includes both static CLI structure and
    # dynamic values from the runtime context (agent types, templates, etc.).
    if ctx.parent is not None and isinstance(ctx.parent.command, click.Group):
        cli_group = ctx.parent.command
        # Include alias names so they are tab-completable for --type, even
        # though they are not distinct agent types.
        registered_agent_types = sorted(set(list_registered_agent_types()) | set(list_agent_aliases().keys()))
        topic_names = sorted(get_all_topics().keys())
        installed_plugin_packages = get_installed_plugin_package_names()
        mngr_ctx.concurrency_group.start_new_thread(
            target=_refresh_completion_artifacts,
            kwargs={
                "cli_group": cli_group,
                "mngr_ctx": mngr_ctx,
                "registered_agent_types": registered_agent_types,
                "topic_names": topic_names,
                "installed_plugin_packages": installed_plugin_packages,
            },
            name="completion-cache-writer",
            is_checked=False,
        )

    # Format template is now resolved by the common option parsing infrastructure
    # (via --format with a template string, e.g. --format '{name}\t{state}')
    format_template = output_opts.format_template

    # --ids / --addrs: shorthand for format templates that print one value per line
    is_shorthand_flag = opts.ids or opts.addrs
    if is_shorthand_flag:
        shorthand_name = "--ids" if opts.ids else "--addrs"
        if opts.ids and opts.addrs:
            raise click.UsageError("--ids and --addrs are mutually exclusive")
        format_source = ctx.get_parameter_source("output_format")
        is_format_explicit = format_source is not None and format_source != click.core.ParameterSource.DEFAULT
        if is_format_explicit:
            raise click.UsageError(f"{shorthand_name} cannot be combined with --format")

    match (opts.ids, opts.addrs):
        case (True, False):
            format_template = "{id}"
        case (False, True):
            format_template = "{name}@{host.name}.{host.provider_name}"
        case _:
            pass

    # Parse fields if provided
    fields = None
    if opts.fields:
        fields = [f.strip() for f in opts.fields.split(",") if f.strip()]

    # Parse custom header overrides (--header FIELD=LABEL)
    custom_headers: dict[str, str] | None = None
    if opts.header:
        custom_headers = {}
        for header_spec in opts.header:
            if "=" not in header_spec:
                raise click.BadParameter(
                    f"Header must be in FIELD=LABEL format, got: {header_spec}", param_hint="--header"
                )
            field_name, label = header_spec.split("=", 1)
            custom_headers[field_name.strip()] = label.strip()

    # Translate filter aliases (--running, --project, etc.) into CEL strings.
    include_filters_tuple, exclude_filters_tuple = build_agent_filter_cel(
        opts, mngr_ctx.concurrency_group, project_root=mngr_ctx.project_root
    )

    # --stdin: read agent/host refs from stdin and add as an OR'd include filter.
    # List-specific because kanpan and other commands don't take stdin input.
    if opts.stdin:
        stdin_refs = [line.strip() for line in sys.stdin if line.strip()]
        if stdin_refs:
            ref_filters = [
                f'(name == "{ref}" || id == "{ref}" || host.name == "{ref}" || host.id == "{ref}")'
                for ref in stdin_refs
            ]
            include_filters_tuple = (*include_filters_tuple, " || ".join(ref_filters))

    # --sort EXPR: CEL expression(s) with optional direction, e.g. "name asc, create_time desc"
    compiled_sort_keys = compile_cel_sort_keys(opts.sort)

    # --limit N: Limit number of results returned
    # NOTE: The limit is applied after fetching results. The full list is still retrieved
    # from providers and then sliced client-side. For large deployments, this means the
    # command may still take time proportional to the total number of agents.
    limit = opts.limit

    error_behavior = ErrorBehavior(opts.on_error.upper())

    provider_names = opts.provider if opts.provider else None

    # Dispatch to the appropriate output path
    if output_opts.output_format == OutputFormat.JSONL:
        _list_jsonl(
            ctx,
            mngr_ctx,
            include_filters_tuple,
            exclude_filters_tuple,
            provider_names,
            error_behavior,
            limit,
        )
        return

    # Determine if --sort was explicitly set by the user (vs using the default)
    sort_source = ctx.get_parameter_source("sort")
    is_sort_explicit = sort_source is not None and sort_source != click.core.ParameterSource.DEFAULT

    # Template output path: if --format is a template string, use streaming when possible, batch otherwise
    if format_template is not None:
        is_streaming_template = _is_streaming_eligible(is_sort_explicit=is_sort_explicit)
        if is_streaming_template:
            _list_streaming_template(
                ctx,
                mngr_ctx,
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
        output_opts.output_format, is_sort_explicit=is_sort_explicit
    ):
        display_fields = fields if fields is not None else list(_DEFAULT_HUMAN_DISPLAY_FIELDS)
        _list_streaming_human(
            ctx,
            mngr_ctx,
            include_filters_tuple,
            exclude_filters_tuple,
            provider_names,
            error_behavior,
            display_fields,
            limit,
            custom_headers=custom_headers,
        )
        return

    iteration_params = _ListIterationParams(
        mngr_ctx=mngr_ctx,
        output_opts=output_opts,
        include_filters=include_filters_tuple,
        exclude_filters=exclude_filters_tuple,
        provider_names=provider_names,
        error_behavior=error_behavior,
        compiled_sort_keys=compiled_sort_keys,
        limit=limit,
        fields=fields,
        format_template=format_template,
        custom_headers=custom_headers,
    )

    _run_list_iteration(iteration_params, ctx)


def _list_jsonl(
    ctx: click.Context,
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    error_behavior: ErrorBehavior,
    limit: int | None,
) -> None:
    """JSONL output path: stream agents as JSONL lines with optional limit."""
    limited_callback = _LimitedJsonlEmitter(limit=limit)

    result = api_list_agents(
        mngr_ctx=mngr_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=error_behavior,
        on_agent=limited_callback,
        on_error=_emit_jsonl_error,
        is_streaming=False,
    )
    # Errors were already streamed as structured JSONL records via _emit_jsonl_error;
    # exit non-zero (provider-inaccessible code when all errors are auth/unavailable).
    if result.errors:
        ctx.exit(_exit_code_for_list_errors(result.errors))


def _list_streaming_human(
    ctx: click.Context,
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    provider_names: tuple[str, ...] | None,
    error_behavior: ErrorBehavior,
    fields: list[str],
    limit: int | None,
    custom_headers: dict[str, str] | None = None,
) -> None:
    """Streaming human output path: display agents as each provider completes."""
    renderer = _StreamingHumanRenderer(
        fields=fields, is_tty=sys.stdout.isatty(), output=sys.stdout, limit=limit, custom_headers=custom_headers
    )

    renderer.start()
    try:
        result = api_list_agents(
            mngr_ctx=mngr_ctx,
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
        _emit_list_errors_human(result.errors)
        ctx.exit(_exit_code_for_list_errors(result.errors))


def _list_streaming_template(
    ctx: click.Context,
    mngr_ctx: MngrContext,
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
        mngr_ctx=mngr_ctx,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        provider_names=provider_names,
        error_behavior=error_behavior,
        on_agent=emitter,
        is_streaming=True,
    )

    if result.errors:
        _emit_list_errors_human(result.errors)
        ctx.exit(_exit_code_for_list_errors(result.errors))


class _LimitedJsonlEmitter(MutableModel):
    """Callable that emits JSONL output with an optional limit."""

    limit: int | None
    count: int = 0
    _lock: Lock = PrivateAttr(default_factory=Lock)

    def __call__(self, agent: AgentDetails) -> None:
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

    def __call__(self, agent: AgentDetails) -> None:
        line = _render_format_template(self.format_template, agent)
        with self._lock:
            if self.limit is not None and self._count >= self.limit:
                return
            self.output.write(line + "\n")
            self.output.flush()
            self._count += 1


# Minimum column widths for streaming output.
# These are the minimum data widths; actual column width is max(min_width, header_length).
_MIN_COLUMN_WIDTHS: Final[dict[str, int]] = {
    "name": 15,
    "host.name": 10,
    "host.provider_name": 10,
    "host.state": 10,
    "state": 10,
    "labels": 10,
    "labels.project": 10,
    "host.tags": 10,
}
_DEFAULT_MIN_COLUMN_WIDTH: Final[int] = 10
_MAX_COLUMN_WIDTHS: Final[dict[str, int]] = {}
_COLUMN_SEPARATOR: Final[str] = "  "
_TRUNCATION_SUFFIX: Final[str] = "... "


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

    When limit is set, stops displaying agents after the limit is reached. Results
    are non-deterministic since streaming does not sort.
    """

    fields: list[str]
    is_tty: bool
    output: Any
    limit: int | None = None
    custom_headers: dict[str, str] | None = None
    _lock: Lock = PrivateAttr(default_factory=Lock)
    _count: int = PrivateAttr(default=0)
    _is_header_written: bool = PrivateAttr(default=False)
    _column_widths: dict[str, int] = PrivateAttr(default_factory=dict)

    def start(self) -> None:
        """Compute column widths and write the initial status line (TTY only)."""
        terminal_width = shutil.get_terminal_size((120, 24)).columns
        self._column_widths = _compute_column_widths(self.fields, terminal_width, self.custom_headers)

        if self.is_tty:
            self.output.write(_format_status_line(0))
            self.output.flush()

    def __call__(self, agent: AgentDetails) -> None:
        """Handle a single agent result (on_agent callback)."""
        with self._lock:
            if self.limit is not None and self._count >= self.limit:
                return

            if self.is_tty:
                # Erase the current status line
                self.output.write(ANSI_ERASE_LINE)

            # Write header on first agent
            if not self._is_header_written:
                header_line = _format_streaming_header_row(self.fields, self._column_widths, self.custom_headers)
                self.output.write(header_line + "\n")
                self._is_header_written = True

            # Write the agent row (truncate only in TTY mode to prevent line wrapping)
            row_line = _format_streaming_agent_row(agent, self.fields, self._column_widths, is_truncate=self.is_tty)
            self.output.write(row_line + "\n")
            self._count += 1

            if self.is_tty:
                # Write updated status line
                self.output.write(_format_status_line(self._count))

            self.output.flush()

    def finish(self) -> None:
        """Clean up the status line after all providers have completed."""
        with self._lock:
            if self.is_tty:
                self.output.write(ANSI_ERASE_LINE)
                self.output.flush()

            if self._count == 0:
                write_human_line("No agents found")


@pure
def _get_header_label(field: str, custom_headers: dict[str, str] | None = None) -> str:
    """Get the display label for a column header."""
    if custom_headers and field in custom_headers:
        return custom_headers[field]
    canonical_field = _resolve_field_alias(field)
    if canonical_field in _HEADER_LABELS:
        return _HEADER_LABELS[canonical_field]
    return field.upper().replace(".", " ")


@pure
def _compute_column_widths(
    fields: Sequence[str], terminal_width: int, custom_headers: dict[str, str] | None = None
) -> dict[str, int]:
    """Compute column widths sized to the terminal, distributing extra space to expandable columns."""
    separator_total = len(_COLUMN_SEPARATOR) * max(len(fields) - 1, 0)

    # Start with minimum widths, ensuring each column is at least as wide as its header.
    # Column-width tables are keyed by canonical field names, so resolve aliases before lookup.
    width_by_field: dict[str, int] = {}
    for field in fields:
        canonical_field = _resolve_field_alias(field)
        min_data_width = _MIN_COLUMN_WIDTHS.get(canonical_field, _DEFAULT_MIN_COLUMN_WIDTH)
        header_width = len(_get_header_label(field, custom_headers))
        width_by_field[field] = max(min_data_width, header_width)

    min_total = sum(width_by_field.values()) + separator_total
    extra_space = max(terminal_width - min_total, 0)

    # Distribute extra space evenly across all columns, respecting max widths.
    # Process columns sorted by tightest max cap first so capped leftovers flow to less
    # constrained columns in a single pass.
    if fields and extra_space > 0:
        sorted_fields = sorted(fields, key=lambda f: _MAX_COLUMN_WIDTHS.get(_resolve_field_alias(f), float("inf")))
        remaining = extra_space
        for idx, field in enumerate(sorted_fields):
            fields_left = len(sorted_fields) - idx
            per_column = remaining // fields_left
            extra = 1 if (remaining % fields_left) > 0 else 0
            bonus = per_column + extra
            max_width = _MAX_COLUMN_WIDTHS.get(_resolve_field_alias(field))
            if max_width is not None and width_by_field[field] + bonus > max_width:
                bonus = max(max_width - width_by_field[field], 0)
            width_by_field[field] = width_by_field[field] + bonus
            remaining = remaining - bonus

    return width_by_field


@pure
def _format_streaming_header_row(
    fields: Sequence[str], column_widths: dict[str, int], custom_headers: dict[str, str] | None = None
) -> str:
    """Format the header row of streaming output with computed column widths."""
    parts: list[str] = []
    for field in fields:
        width = column_widths.get(field, _DEFAULT_MIN_COLUMN_WIDTH)
        value = _get_header_label(field, custom_headers)
        parts.append(value.ljust(width))
    return _COLUMN_SEPARATOR.join(parts)


@pure
def _truncate_to_width(value: str, width: int) -> str:
    """Truncate a value to fit within the given width, appending a suffix if truncated."""
    if len(value) <= width:
        return value.ljust(width)
    truncated_content_width = width - len(_TRUNCATION_SUFFIX)
    if truncated_content_width <= 0:
        return value[:width]
    return value[:truncated_content_width] + _TRUNCATION_SUFFIX


@pure
def _format_streaming_agent_row(
    agent: AgentDetails,
    fields: Sequence[str],
    column_widths: dict[str, int],
    # Truncate values that exceed column width (only needed for TTY to prevent line wrapping)
    is_truncate: bool = True,
) -> str:
    """Format a single agent as a streaming output row."""
    parts: list[str] = []
    for field in fields:
        width = column_widths.get(field, _DEFAULT_MIN_COLUMN_WIDTH)
        value = _get_field_value(agent, field)
        formatted = _truncate_to_width(value, width) if is_truncate else value.ljust(width)
        parts.append(formatted)
    return _COLUMN_SEPARATOR.join(parts)


class _ListIterationParams(BaseModel):
    """Parameters for a single list iteration."""

    model_config = {"arbitrary_types_allowed": True}

    mngr_ctx: MngrContext
    output_opts: OutputOptions
    include_filters: tuple[str, ...]
    exclude_filters: tuple[str, ...]
    provider_names: tuple[str, ...] | None
    error_behavior: ErrorBehavior
    # Compiled CEL sort keys: list of (program, is_descending) pairs
    compiled_sort_keys: list[tuple[Any, bool]]
    limit: int | None
    fields: list[str] | None
    format_template: str | None = None
    custom_headers: dict[str, str] | None = None


def _run_list_iteration(params: _ListIterationParams, ctx: click.Context) -> None:
    """Run a single list iteration."""
    result = api_list_agents(
        mngr_ctx=params.mngr_ctx,
        include_filters=params.include_filters,
        exclude_filters=params.exclude_filters,
        provider_names=params.provider_names,
        error_behavior=params.error_behavior,
        is_streaming=False,
    )

    # Apply sorting to results
    agents_to_display = _sort_agents_by_cel(result.agents, params.compiled_sort_keys)

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
            # Route through `_emit_json_output` so errors get pydantic-dumped to
            # JSON-friendly dicts; passing raw ErrorInfo objects to
            # `json.dumps` crashes with "Object of type ProviderErrorInfo is
            # not JSON serializable", which is what made `mngr list --on-error
            # continue --format json` hard-fail whenever discovery hit a
            # broken host.
            _emit_json_output([], result.errors)
        else:
            # JSONL is handled above with streaming, so this should be unreachable
            raise AssertionError(f"Unexpected output format: {params.output_opts.output_format}")
        _report_list_errors_and_exit(ctx, params.output_opts.output_format, result.errors)
        return

    # Template output takes precedence over format-based dispatch
    if params.format_template is not None:
        _emit_template_output(agents_to_display, params.format_template, output=sys.stdout)
    elif params.output_opts.output_format == OutputFormat.HUMAN:
        _emit_human_output(agents_to_display, params.fields, params.custom_headers)
    elif params.output_opts.output_format == OutputFormat.JSON:
        _emit_json_output(agents_to_display, result.errors)
    else:
        # JSONL is handled above with streaming, so this should be unreachable
        raise AssertionError(f"Unexpected output format: {params.output_opts.output_format}")

    _report_list_errors_and_exit(ctx, params.output_opts.output_format, result.errors)


def _report_list_errors_and_exit(
    ctx: click.Context,
    output_format: OutputFormat,
    errors: Sequence[ErrorInfo],
) -> None:
    """Report any listing errors and exit non-zero (no-op when there were none).

    JSON output already carries the errors in its structured ``errors`` array, so
    only non-JSON formats get the consistent human-readable error block on stderr.
    """
    if not errors:
        return
    if output_format != OutputFormat.JSON:
        _emit_list_errors_human(errors)
    ctx.exit(_exit_code_for_list_errors(errors))


def _exit_code_for_list_errors(errors: Sequence[ErrorInfo]) -> int:
    """Pick the process exit code for a completed listing.

    SUCCESS when there were no errors; the granular provider-inaccessible code
    when *every* error was a provider that could not be reached or authenticated;
    otherwise the generic error code.
    """
    if not errors:
        return EXIT_CODE_SUCCESS
    if all(error.is_provider_inaccessible for error in errors):
        return EXIT_CODE_PROVIDER_INACCESSIBLE
    return EXIT_CODE_ERROR


@pure
def _format_provider_error_line(error: ProviderErrorInfo) -> str:
    """Render one provider failure as a single consistent line.

    Shape: ``<provider>: <reason> — <remediation> (disable: mngr config set ...)``.
    Falls back to the full message when no concise reason was supplied.
    """
    # Keep it to a single glanceable line even if the underlying message is multi-line.
    full_reason = error.short_reason or error.message
    reason = full_reason.splitlines()[0] if full_reason else error.exception_type
    line = f"{error.provider_name}: {reason}"
    if error.short_remediation:
        line = f"{line} — {error.short_remediation}"
    return f"{line} (disable: mngr config set --scope user providers.{error.provider_name}.is_enabled false)"


@pure
def _format_list_error_line(error: ErrorInfo) -> str:
    """Render one listing error as a single line for the human-facing error block."""
    if isinstance(error, ProviderErrorInfo):
        return _format_provider_error_line(error)
    return error.message


@pure
def _render_list_errors_block(errors: Sequence[ErrorInfo], is_color_enabled: bool) -> str:
    """Render the consolidated error block, in bold red when color is enabled.

    Matches ``MngrError.show`` so the listing's end-of-output errors look the same as
    every other mngr error (red on a color-capable terminal, plain when piped/NO_COLOR).
    """
    lines = ["Errors:"]
    for error in errors:
        lines.append("  " + _format_list_error_line(error))
    block = "\n".join(lines)
    if is_color_enabled:
        return f"{ERROR_COLOR}{block}{RESET_COLOR}"
    return block


def _emit_list_errors_human(errors: Sequence[ErrorInfo]) -> None:
    """Print all listing errors to stderr in a single consistent block.

    Used by every non-JSON human-facing list path (table, template, --ids/--addrs)
    so unauthenticated or unreachable providers are reported the same way at the very
    end of the output, after all successfully listed agents. JSON/JSONL paths instead
    carry the same errors in their structured ``errors`` channel.
    """
    if not errors:
        return
    write_stderr_line(_render_list_errors_block(errors, should_use_color(sys.stderr)))


def _emit_json_output(agents: list[AgentDetails], errors: list[ErrorInfo]) -> None:
    """Emit JSON output with all agents."""
    agents_data = [agent.model_dump(mode="json") for agent in agents]
    errors_data = [error.model_dump(mode="json") for error in errors]
    output_data = {
        "agents": agents_data,
        "errors": errors_data,
    }
    write_json_line(output_data)


def _emit_jsonl_agent(agent: AgentDetails) -> None:
    """Emit a single agent as a JSONL line (streaming callback)."""
    agent_data = agent.model_dump(mode="json")
    write_json_line(agent_data)


def _emit_jsonl_error(error: ErrorInfo) -> None:
    """Emit a single error as a JSONL line (streaming callback)."""
    error_data = {"event": "error", **error.model_dump(mode="json")}
    write_json_line(error_data)


def _emit_human_output(
    agents: list[AgentDetails],
    fields: list[str] | None = None,
    custom_headers: dict[str, str] | None = None,
) -> None:
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
        headers.append(_get_header_label(field, custom_headers))

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


def _emit_template_output(agents: list[AgentDetails], template: str, output: Any) -> None:
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


class _CelSortKeyExtractor:
    """Extracts a sort key from an (agent, cel_context) pair for a single CEL expression."""

    program: Any
    is_descending: bool

    def __call__(self, pair: tuple[AgentDetails, dict[str, Any]]) -> tuple[int, str]:
        _, ctx = pair
        value = evaluate_cel_sort_key(self.program, ctx)
        if value is None:
            # For ascending: (1, "") puts None at end
            # For descending (reverse=True): (0, "") puts None at end
            return (1, "") if not self.is_descending else (0, "")
        return (0, str(value)) if not self.is_descending else (1, str(value))


def _sort_agents_by_cel(
    agents: list[AgentDetails],
    compiled_sort_keys: Sequence[tuple[Any, bool]],
) -> list[AgentDetails]:
    """Sort agents using compiled CEL sort key expressions.

    Supports multiple sort keys with per-key direction (asc/desc).
    Uses stable multi-pass sorting: sorts by each key in reverse order
    of significance so the most significant key dominates.
    """
    if not compiled_sort_keys or not agents:
        return agents

    # Precompute CEL contexts once for all agents
    cel_contexts = [build_agent_cel_context(agent) for agent in agents]

    # Pair agents with their precomputed contexts for sorting
    paired: list[tuple[AgentDetails, dict[str, Any]]] = list(zip(agents, cel_contexts, strict=True))

    # Sort by each key in reverse order of significance (stable sort preserves earlier orderings)
    for program, is_descending in reversed(compiled_sort_keys):
        extractor = _CelSortKeyExtractor()
        extractor.program = program
        extractor.is_descending = is_descending
        paired.sort(key=extractor, reverse=is_descending)

    return [agent for agent, _ in paired]


def _reject_schema_conflicts(opts: ListCliOptions) -> None:
    """Raise a UsageError if --schema is combined with any agent-selection option.

    The field catalog is static and independent of which agents exist, so any
    filter, fan-out, or per-agent output-shaping option is meaningless alongside
    it. Reject loudly so the user picks one rather than silently ignoring input.
    """
    conflicting = {
        "--include": bool(opts.include),
        "--exclude": bool(opts.exclude),
        "--running": opts.running,
        "--stopped": opts.stopped,
        "--archived": opts.archived,
        "--active": opts.active,
        "--local": opts.local,
        "--remote": opts.remote,
        "--project": bool(opts.project),
        "--label": bool(opts.label),
        "--host-label": bool(opts.host_label),
        "--provider": bool(opts.provider),
        "--stdin": opts.stdin,
        "--fields": opts.fields is not None,
        "--header": bool(opts.header),
        "--limit": opts.limit is not None,
        "--ids": opts.ids,
        "--addrs": opts.addrs,
    }
    used = [flag for flag, is_set in conflicting.items() if is_set]
    if used:
        raise click.UsageError(f"--schema lists fields and cannot be combined with: {', '.join(used)}")


def _emit_field_schema(output_opts: OutputOptions) -> None:
    """Emit the list field catalog in the requested output format."""
    rows = catalog_rows_as_dicts()
    if output_opts.format_template is not None:
        emit_format_template_lines(output_opts.format_template, rows)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"schema": rows})
        case OutputFormat.JSONL:
            write_json_line({"event": "list_schema", "schema": rows})
        case OutputFormat.HUMAN:
            write_human_line(
                "All fields are usable in --include/--exclude and --sort; "
                "(cel only) marks fields not usable in --fields/--format:"
            )
            for row in build_list_field_catalog():
                marker = "" if FieldContext.TEMPLATE in row.contexts else " (cel only)"
                write_human_line("  {} : {}{} - {}", row.key, row.type, marker, row.description)
        case _ as unreachable:
            assert_never(unreachable)


def _get_field_value(agent: AgentDetails, field: str) -> str:
    """Extract a field value from an AgentDetails object and return as string.

    Supports nested fields like "host.name" and list slicing syntax like
    "host.snapshots[0]" or "host.snapshots[:3]".
    """
    # Resolve aliases first so a user-supplied alias (e.g. host.provider) maps to the
    # canonical attribute name (host.provider_name) before walking the agent object.
    field = _resolve_field_alias(field)
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
def _render_format_template(template: str, agent: AgentDetails) -> str:
    """Expand a str.format()-style template using agent field values.

    Pre-resolves field names via _get_field_value() (which supports nested
    attribute access and bracket notation on AgentDetails), then delegates
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
    one_line_description="List all agents managed by mngr",
    synopsis="mngr [list|ls] [--stdin] [--schema] [--ids] [--addrs] [--fields FIELDS] [--sort CEL] "
    "[--include CEL] [--exclude CEL] [--provider PROVIDER] [--running] [--stopped] [--archived] [--active] "
    "[--local] [--remote] [--project PROJECT] [--limit N] [--on-error MODE]",
    description="""Displays agents with their status, host information, and other metadata.
Supports filtering, sorting, and multiple output formats.""",
    aliases=("ls",),
    examples=(
        ("List all agents", "mngr list"),
        ("List only running agents", "mngr list --running"),
        ("List agents on Docker hosts", "mngr list --provider docker"),
        ("List agents for a project", "mngr list --project mngr"),
        ("List agents with a specific label", "mngr list --label env=prod"),
        ("List agents with a specific host label", "mngr list --host-label env=prod"),
        ("List agents as JSON", "mngr list --format json"),
        ("Filter with CEL expression", "mngr list --include 'name.contains(\"prod\")'"),
        ("Sort by name descending", "mngr list --sort 'name desc'"),
        ("Sort by multiple fields", "mngr list --sort 'state, name asc, create_time desc'"),
        ("Custom column header", "mngr list --fields name,labels.env --header labels.env=ENV"),
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
- `labels.project == "mngr"` - Match agents with a specific project label

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
- `has(labels.foo)` - Agents that have a `foo` label set
""",
        ),
        (
            "Available Fields",
            render_catalog_help_markdown(),
        ),
    ),
    see_also=(
        ("create", "Create a new agent"),
        ("connect", "Connect to an existing agent"),
        ("destroy", "Destroy agents"),
        ("multi_target", "Behavior when some agents cannot be accessed"),
        ("common", "Common CLI options for output format, logging, etc."),
    ),
).register()

# Add pager-enabled help option to the list command
add_pager_help_option(list_command)
