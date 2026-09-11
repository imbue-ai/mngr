import base64
import sys
from pathlib import Path
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup

from imbue.imbue_common.logging import log_span
from imbue.mngr.cli.address_params import AGENT_OR_HOST_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.cli.target import resolve_full_path
from imbue.mngr_file.data_types import PathRelativeTo


class _FileGetCliOptions(CommonCliOptions):
    """Options for the file get subcommand."""

    target: AgentOrHostAddress
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
            write_json_line({"event": "file_read", **data})
        case OutputFormat.JSONL:
            emit_event("file_read", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
        case _ as unreachable:
            assert_never(unreachable)


def _emit_saved_result(
    file_path: Path,
    output_path: Path,
    size: int,
    output_opts: OutputOptions,
) -> None:
    """Report a read that was saved to a local file rather than written to stdout.

    Carries no ``content_base64``: the bytes are already on disk at
    ``output_path``, so repeating them would only inflate the event.
    """
    data = {
        "path": str(file_path),
        "output_path": str(output_path),
        "size": size,
    }
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"event": "file_read", **data})
        case OutputFormat.JSONL:
            emit_event("file_read", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Wrote {} bytes to {}", size, output_path)
        case _ as unreachable:
            assert_never(unreachable)


def _read_file(host: HostFileReadInterface, path: Path) -> bytes:
    """Read ``path``, reporting the two ordinary addressing mistakes as user-facing errors.

    Every readable host signals these the same way -- a local read, an SFTP read
    and a volume read all raise the builtin ``OSError`` subclasses -- so the
    translation belongs here rather than per backend.
    """
    try:
        return host.read_file(path)
    except FileNotFoundError as e:
        raise MngrError(f"No file at {path}. Use 'mngr file list' to see what is there.") from e
    except IsADirectoryError as e:
        raise MngrError(
            f"{path} is a directory, not a file. Use 'mngr rsync' to transfer a directory, "
            f"or 'mngr file list' to see what it holds."
        ) from e


@file_group.command(name="get")
@click.argument("target", type=AGENT_OR_HOST_ADDRESS)
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="file-get",
        command_class=_FileGetCliOptions,
    )

    relative_to = PathRelativeTo(opts.relative_to.upper())

    # Resolve target
    with log_span("Resolving file target"):
        resolved = resolve_file_target(
            target=opts.target,
            mngr_ctx=mngr_ctx,
            relative_to=relative_to,
        )

    # Read file through the unified readable-host interface (online or volume-backed).
    with log_span("Reading file"):
        full_path = resolve_full_path(resolved.base_path, opts.path)
        content = _read_file(resolved.host, full_path)
        display_path = full_path

    # Output
    if opts.output is not None:
        output_path = Path(opts.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
        _emit_saved_result(display_path, output_path, len(content), output_opts)
    else:
        _emit_get_result(display_path, content, output_opts)
