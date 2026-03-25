import base64
import sys
from pathlib import Path
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.imbue_common.logging import log_span
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat
from imbue.mng_file.cli.group import file_group
from imbue.mng_file.cli.target import compute_volume_path
from imbue.mng_file.cli.target import resolve_file_target
from imbue.mng_file.cli.target import resolve_full_path
from imbue.mng_file.data_types import PathRelativeTo


class _FileGetCliOptions(CommonCliOptions):
    """Options for the file get subcommand."""

    target: str
    path: str
    output: str | None
    relative_to: str


def _emit_get_result(
    file_path: Path,
    content: bytes,
    output_opts: OutputOptions,
) -> None:
    data = {
        "path": str(file_path),
        "size": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"event": "file_read", **data})
        case OutputFormat.JSONL:
            emit_event("file_read", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
        case _ as unreachable:
            assert_never(unreachable)


@file_group.command(name="get")
@click.argument("target")
@click.argument("path")
@optgroup.group("Output")
@optgroup.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write to a local file instead of stdout",
)
@optgroup.group("Path Resolution")
@optgroup.option(
    "--relative-to",
    type=click.Choice(["work", "state", "host"], case_sensitive=False),
    default="work",
    show_default=True,
    help="Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir)",
)
@add_common_options
@click.pass_context
def file_get(ctx: click.Context, **kwargs: Any) -> None:
    """Read a file from an agent or host.

    \b
    TARGET is the agent or host name/ID.
    PATH is the file path (absolute, or relative to --relative-to base).
    """
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="file-get",
        command_class=_FileGetCliOptions,
    )

    relative_to = PathRelativeTo(opts.relative_to.upper())

    # Resolve target
    with log_span("Resolving file target"):
        resolved = resolve_file_target(
            target_identifier=opts.target,
            mng_ctx=mng_ctx,
            relative_to=relative_to,
        )

    # Read file -- prefer online host, fall back to volume
    with log_span("Reading file"):
        if resolved.is_online:
            full_path = resolve_full_path(resolved.base_path, opts.path)
            content = resolved.host.read_file(full_path)
            display_path = full_path
        else:
            assert resolved.volume is not None
            vol_path = compute_volume_path(resolved.relative_to, resolved.agent_id, opts.path)
            content = resolved.volume.read_file(vol_path)
            display_path = Path(vol_path)

    # Output
    if opts.output is not None:
        output_path = Path(opts.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
    else:
        _emit_get_result(display_path, content, output_opts)
