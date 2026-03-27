import sys
from typing import Any

import click
from click_option_group import optgroup

from imbue.mngr.api.events import EventRecord
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import resolve_events_target
from imbue.mngr.api.events import stream_all_events
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.utils.cel_utils import compile_cel_filters


class EventsCliOptions(CommonCliOptions):
    """Options passed from the CLI to the events command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    target: str
    sources: tuple[str, ...]
    source: tuple[str, ...]
    filter: str | None
    follow: bool
    tail: int | None
    head: int | None


def _write_and_flush_stdout(content: str) -> None:
    """Write content to stdout and flush immediately for piped output."""
    sys.stdout.write(content)
    sys.stdout.flush()


@click.command(name="events")
@click.argument("target")
@click.argument("sources", nargs=-1)
@optgroup.group("Display")
@optgroup.option(
    "--follow/--no-follow",
    default=False,
    show_default=True,
    help="Continue running and print new events as they appear",
)
@optgroup.option(
    "--tail",
    type=click.IntRange(min=1),
    default=None,
    help="Print the last N events",
)
@optgroup.option(
    "--head",
    type=click.IntRange(min=1),
    default=None,
    help="Print the first N events",
)
@optgroup.group("Filtering")
@optgroup.option(
    "--source",
    multiple=True,
    help="Event source to include, relative to events/ (e.g. 'messages', 'logs/mngr'). Can be repeated.",
)
# FIXME: this should be consistent with the rest of the API (two repeatable args, --include and --exclude, that can be used together to build up complex filters)
@optgroup.option(
    "--filter",
    "filter",
    default=None,
    help="CEL expression to filter which events to include (e.g. 'source == \"messages\"')",
)
@add_common_options
@click.pass_context
def events(ctx: click.Context, **kwargs: Any) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="events",
        command_class=EventsCliOptions,
        is_format_template_supported=False,
    )

    # Validate mutually exclusive options
    if opts.head is not None and opts.tail is not None:
        raise UserInputError("Cannot specify both --head and --tail")

    if opts.follow and opts.head is not None:
        raise UserInputError("Cannot use --head with --follow")

    # Resolve the target (agent or host)
    target = resolve_events_target(
        identifier=opts.target,
        mngr_ctx=mngr_ctx,
    )

    # Merge positional source arguments and --source option values
    all_sources = tuple(sorted(set(opts.sources) | set(opts.source)))

    # Compile CEL filters
    cel_include_filters: list[Any] = []
    cel_exclude_filters: list[Any] = []
    if opts.filter is not None:
        cel_include_filters, cel_exclude_filters = compile_cel_filters(
            include_filters=[opts.filter],
            exclude_filters=[],
        )

    _stream_all_events_cli(target, opts, cel_include_filters, cel_exclude_filters, all_sources)


def _emit_event_record(event: EventRecord) -> None:
    """Emit a single event record to stdout as a JSONL line."""
    _write_and_flush_stdout(event.raw_line)
    if not event.raw_line.endswith("\n"):
        _write_and_flush_stdout("\n")


def _stream_all_events_cli(
    target: EventsTarget,
    opts: EventsCliOptions,
    cel_include_filters: list[Any],
    cel_exclude_filters: list[Any],
    source_filters: tuple[str, ...],
) -> None:
    """Stream all events from all sources as JSONL lines."""
    try:
        stream_all_events(
            target=target,
            on_event=_emit_event_record,
            cel_include_filters=cel_include_filters,
            cel_exclude_filters=cel_exclude_filters,
            tail_count=opts.tail,
            head_count=opts.head,
            is_follow=opts.follow,
            source_filters=source_filters,
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="events",
    one_line_description="View events from an agent or host",
    synopsis="mngr events TARGET [SOURCES...] [--source SOURCE] [--filter CEL] [--follow] [--tail N] [--head N]",
    arguments_description=(
        "- `TARGET`: Agent or host name/ID whose events to view\n"
        "- `SOURCES`: Event sources to include (optional; includes all sources if omitted). "
        "These are paths relative to the target's events/ directory (e.g. 'messages', 'logs/mngr')."
    ),
    description="""TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

Streams all events from all sources in date-sorted order. Use --source
or positional SOURCES arguments to restrict which event sources to include.
Use --filter to further restrict events via a CEL expression. Use --follow
to continuously stream new events.

In follow mode (--follow), the command polls for new events. When the host
is online, it reads files directly. When offline, it falls back to polling
the volume. The command handles online/offline transitions automatically.
Press Ctrl+C to stop.""",
    examples=(
        ("Stream all events for an agent", "mngr events my-agent"),
        ("Stream only message events", "mngr events my-agent messages"),
        ("Stream events from multiple sources", "mngr events my-agent messages logs/mngr"),
        ("Same thing using --source", "mngr events my-agent --source messages --source logs/mngr"),
        ("Filter within a source", "mngr events my-agent messages --filter 'data.role == \"user\"'"),
        ("View last 100 events", "mngr events my-agent --tail 100"),
        ("Follow all events in real-time", "mngr events my-agent --follow"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("exec", "Execute commands on an agent's host"),
    ),
).register()

# Add pager-enabled help option to the events command
add_pager_help_option(events)
