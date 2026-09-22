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
from imbue.mngr_file.cli.target import is_directory
from imbue.mngr_file.cli.target import parse_relative_to
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.cli.target import resolve_full_path


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
            write_human_line("Wrote {} bytes from {} to {}", size, file_path, output_path)
        case _ as unreachable:
            assert_never(unreachable)


def _no_file_error(path: Path) -> MngrError:
    return MngrError(f"No file at {path}. Use 'mngr file list' to see what is there.")


def _directory_error(path: Path) -> MngrError:
    return MngrError(
        f"{path} is a directory, not a file. Use 'mngr rsync' to transfer a directory, "
        f"or 'mngr file list' to see what it holds."
    )


def _read_file(host: HostFileReadInterface, path: Path, host_dir: Path) -> bytes:
    """Read ``path``, reporting the two ordinary addressing mistakes as user-facing errors.

    A read of a missing path or a directory fails in the terms of whatever reaches
    the machine -- a filesystem raises ``OSError``, a storage service such as a
    Modal volume raises errors of its own -- so the path is classified through the
    host interface before it is read.
    """
    if not host.path_exists(path):
        raise _no_file_error(path)
    if is_directory(host, path, host_dir):
        raise _directory_error(path)
    try:
        return host.read_file(path)
    except IsADirectoryError as e:
        # A listing classifies a symlink to a directory as a link, so only the read reveals it.
        raise _directory_error(path) from e


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

    relative_to = parse_relative_to(opts.target, opts.relative_to)

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
        content = _read_file(resolved.host, full_path, resolved.host_dir)
        display_path = full_path

    # Output
    if opts.output is not None:
        output_path = Path(opts.output).absolute()
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)
        except IsADirectoryError as e:
            raise MngrError(f"Cannot save to {output_path}: it is a directory.") from e
        except (NotADirectoryError, FileExistsError) as e:
            # Creating the parents of a path fails this way when one of them is a file.
            raise MngrError(f"Cannot save to {output_path}: a directory leading to it is a file.") from e
        _emit_saved_result(display_path, output_path, len(content), output_opts)
    else:
        _emit_get_result(display_path, content, output_opts)
