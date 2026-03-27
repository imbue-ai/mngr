import sys
from pathlib import Path
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.target import compute_volume_path
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.cli.target import resolve_full_path
from imbue.mngr_file.data_types import PathRelativeTo


class _FilePutCliOptions(CommonCliOptions):
    """Options for the file put subcommand."""

    target: str
    path: str
    input: str | None
    relative_to: str
    mode: str | None


def _emit_put_result(
    file_path: Path,
    size: int,
    output_opts: OutputOptions,
) -> None:
    data = {
        "path": str(file_path),
        "size": size,
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"event": "file_written", **data})
        case OutputFormat.JSONL:
            emit_event("file_written", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Wrote {} bytes to {}", size, file_path)
        case _ as unreachable:
            assert_never(unreachable)


@file_group.command(name="put")
@click.argument("target")
@click.argument("path")
@optgroup.group("Input")
@optgroup.option(
    "--input",
    "-i",
    "input",
    type=click.Path(exists=True),
    default=None,
    help="Read from a local file instead of stdin",
)
@optgroup.group("Path Resolution")
@optgroup.option(
    "--relative-to",
    type=click.Choice(["work", "state", "host"], case_sensitive=False),
    default="work",
    show_default=True,
    help="Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir)",
)
@optgroup.group("File Options")
@optgroup.option(
    "--mode",
    default=None,
    help="Set file permissions (e.g. '0644')",
)
@add_common_options
@click.pass_context
def file_put(ctx: click.Context, **kwargs: Any) -> None:
    """Write a file to an agent or host.

    \b
    TARGET is the agent or host name/ID.
    PATH is the destination file path (absolute, or relative to --relative-to base).

    Content is read from --input file or stdin.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="file-put",
        command_class=_FilePutCliOptions,
    )

    relative_to = PathRelativeTo(opts.relative_to.upper())

    # Resolve target
    with log_span("Resolving file target"):
        resolved = resolve_file_target(
            target_identifier=opts.target,
            mngr_ctx=mngr_ctx,
            relative_to=relative_to,
        )

    # Read content
    if opts.input is not None:
        content = Path(opts.input).read_bytes()
    elif sys.stdin.isatty():
        raise UserInputError(
            "No input provided. Either pipe data to stdin or use --input to specify a file.\n\n"
            "Examples:\n"
            "  echo 'hello' | mngr file put my-agent file.txt\n"
            "  mngr file put my-agent file.txt --input local-file.txt"
        )
    else:
        content = sys.stdin.buffer.read()

    # Write file -- prefer online host, fall back to volume
    with log_span("Writing file"):
        if resolved.is_online:
            full_path = resolve_full_path(resolved.base_path, opts.path)
            resolved.host.write_file(full_path, content, mode=opts.mode)
            display_path = full_path
        else:
            assert resolved.volume is not None
            if opts.mode is not None:
                logger.warning("--mode is not supported when writing via volume (host is offline); ignoring")
            vol_path = compute_volume_path(resolved.relative_to, resolved.agent_id, opts.path)
            resolved.volume.write_files({vol_path: content})
            display_path = Path(vol_path)

    _emit_put_result(display_path, len(content), output_opts)
