import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.events import EventRecord
from imbue.mng.api.events import EventsTarget
from imbue.mng.api.events import apply_head_or_tail
from imbue.mng.api.events import follow_event_file
from imbue.mng.api.events import read_event_content
from imbue.mng.api.events import resolve_events_target
from imbue.mng.api.events import stream_all_events
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.cel_utils import compile_cel_filters


class EventsCliOptions(CommonCliOptions):
    """Options passed from the CLI to the events command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    target: str
    event_filename: str | None
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
@click.argument("event_filename", required=False, default=None)
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
    help="Print the last N events (or lines when viewing a specific file)",
)
@optgroup.option(
    "--head",
    type=click.IntRange(min=1),
    default=None,
    help="Print the first N events (or lines when viewing a specific file)",
)
@optgroup.group("Filtering")
@optgroup.option(
    "--filter",
    "filter",
    default=None,
    help="CEL expression to filter which events to include (e.g. 'source == \"messages\"')",
)
@add_common_options
@click.pass_context
def events(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
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

    if opts.filter is not None and opts.event_filename is not None:
        raise UserInputError("Cannot use --filter with a specific event file name")

    # Resolve the target (agent or host)
    target = resolve_events_target(
        identifier=opts.target,
        mng_ctx=mng_ctx,
    )

    # If a specific event file is given, view that file directly
    if opts.event_filename is not None:
        _handle_specific_file(target, opts, output_opts)
        return

    # Stream all events from all sources
    cel_include_filters: list[Any] = []
    cel_exclude_filters: list[Any] = []
    if opts.filter is not None:
        cel_include_filters, cel_exclude_filters = compile_cel_filters(
            include_filters=[opts.filter],
            exclude_filters=[],
        )

    _stream_all_events_cli(target, opts, cel_include_filters, cel_exclude_filters)


def _handle_specific_file(
    target: EventsTarget,
    opts: EventsCliOptions,
    output_opts: OutputOptions,
) -> None:
    """View a specific event file by name."""
    assert opts.event_filename is not None

    if opts.follow:
        # Follow mode: poll and print new content
        logger.info("Following event file '{}' for {} (Ctrl+C to stop)", opts.event_filename, target.display_name)
        try:
            follow_event_file(
                target=target,
                event_file_name=opts.event_filename,
                on_new_content=_write_and_flush_stdout,
                tail_count=opts.tail,
            )
        except KeyboardInterrupt:
            # Clean exit on Ctrl+C
            sys.stdout.write("\n")
            sys.stdout.flush()
        return

    # Read and display the event file
    try:
        content = read_event_content(target, opts.event_filename)
    except (MngError, OSError) as e:
        raise MngError(f"Failed to read event file '{opts.event_filename}': {e}") from e

    filtered_content = apply_head_or_tail(content, head_count=opts.head, tail_count=opts.tail)
    _emit_event_content(filtered_content, opts.event_filename, output_opts)


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
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _emit_event_content(
    content: str,
    event_file_name: str,
    output_opts: OutputOptions,
) -> None:
    """Emit event content in the appropriate format."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            sys.stdout.write(content)
            if content and not content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_final_json(
                {
                    "event_file": event_file_name,
                    "content": content,
                }
            )
        case _ as unreachable:
            assert_never(unreachable)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="events",
    one_line_description="View events from an agent or host",
    synopsis="mng events TARGET [EVENT_FILE] [--filter CEL] [--follow] [--tail N] [--head N]",
    arguments_description=(
        "- `TARGET`: Agent or host name/ID whose events to view\n"
        "- `EVENT_FILE`: Name of a specific event file to view (optional; streams all events if omitted)"
    ),
    description="""TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

If EVENT_FILE is not specified, streams all events from all sources in
date-sorted order. Use --filter to restrict which events are included
via a CEL expression. Use --follow to continuously stream new events.

If EVENT_FILE is specified, prints its contents directly.

In follow mode (--follow), the command polls for new events. When the host
is online, it reads files directly. When offline, it falls back to polling
the volume. The command handles online/offline transitions automatically.
Press Ctrl+C to stop.""",
    examples=(
        ("Stream all events for an agent", "mng events my-agent"),
        ("Stream only message events", "mng events my-agent --filter 'source == \"messages\"'"),
        ("View last 100 events", "mng events my-agent --tail 100"),
        ("Follow all events in real-time", "mng events my-agent --follow"),
        ("View a specific event file", "mng events my-agent messages/events.jsonl"),
        ("Follow a specific event file", "mng events my-agent messages/events.jsonl --follow"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("exec", "Execute commands on an agent's host"),
    ),
).register()

# Add pager-enabled help option to the events command
add_pager_help_option(events)
