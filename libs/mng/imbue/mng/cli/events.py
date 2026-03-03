import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mng.api.events import EventFileEntry
from imbue.mng.api.events import apply_head_or_tail
from imbue.mng.api.events import follow_event_file
from imbue.mng.api.events import list_event_files
from imbue.mng.api.events import read_event_content
from imbue.mng.api.events import resolve_events_target
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_format_template_lines
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import OutputFormat


class EventsCliOptions(CommonCliOptions):
    """Options passed from the CLI to the events command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    target: str
    event_filename: str | None
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
    help="Continue running and print new messages as they appear",
)
@optgroup.option(
    "--tail",
    type=click.IntRange(min=1),
    default=None,
    help="Print the last N lines of the event file",
)
@optgroup.option(
    "--head",
    type=click.IntRange(min=1),
    default=None,
    help="Print the first N lines of the event file",
)
@add_common_options
@click.pass_context
def events(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="events",
        command_class=EventsCliOptions,
        is_format_template_supported=True,
    )

    # Validate mutually exclusive options
    if opts.head is not None and opts.tail is not None:
        raise UserInputError("Cannot specify both --head and --tail")

    if opts.follow and opts.head is not None:
        raise UserInputError("Cannot use --head with --follow")

    # Resolve the target (agent or host)
    target = resolve_events_target(
        identifier=opts.target,
        mng_ctx=mng_ctx,
    )

    # If no event file specified, list available event files
    if opts.event_filename is None:
        event_files = list_event_files(target)
        _emit_event_file_list(event_files, target.display_name, output_opts)
        return

    # Format templates only apply to file listing, not to viewing file content
    if output_opts.format_template is not None:
        raise UserInputError(
            "Format template strings are only supported when listing event files (without a filename argument). "
            "Use --format human, --format json, or --format jsonl when viewing event content."
        )

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


def _emit_event_file_list(
    event_files: list[EventFileEntry],
    display_name: str,
    output_opts: OutputOptions,
) -> None:
    """Emit the list of available event files."""
    if output_opts.format_template is not None:
        items = [{"name": ef.name, "size": str(ef.size)} for ef in event_files]
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            if not event_files:
                write_human_line("No event files found for {}", display_name)
            else:
                write_human_line("Event files for {}:", display_name)
                for event_file in event_files:
                    write_human_line("  {} ({} bytes)", event_file.name, event_file.size)
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_final_json(
                {
                    "target": display_name,
                    "event_files": [{"name": ef.name, "size": ef.size} for ef in event_files],
                }
            )
        case _ as unreachable:
            assert_never(unreachable)


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
    one_line_description="View event files from an agent or host [experimental]",
    synopsis="mng events TARGET [EVENT_FILE] [--follow] [--tail N] [--head N]",
    arguments_description=(
        "- `TARGET`: Agent or host name/ID whose events to view\n"
        "- `EVENT_FILE`: Name of the event file to view (optional; lists files if omitted)"
    ),
    description="""TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

If EVENT_FILE is not specified, lists all available event files.
If EVENT_FILE is specified, prints its contents.

In follow mode (--follow), the command uses tail -f for real-time
streaming when the host is online (locally or via SSH). When the host
is offline, it falls back to polling the volume for new content.
Press Ctrl+C to stop.

When listing files, supports custom format templates via --format. Available fields: name, size.""",
    examples=(
        ("List available event files for an agent", "mng events my-agent"),
        ("View a specific event file", "mng events my-agent output.log"),
        ("View the last 50 lines", "mng events my-agent output.log --tail 50"),
        ("Follow an event file", "mng events my-agent output.log --follow"),
        ("List files with custom format template", "mng events my-agent --format '{name}\\t{size}'"),
    ),
    see_also=(
        ("list", "List available agents"),
        ("exec", "Execute commands on an agent's host"),
    ),
).register()

# Add pager-enabled help option to the events command
add_pager_help_option(events)
