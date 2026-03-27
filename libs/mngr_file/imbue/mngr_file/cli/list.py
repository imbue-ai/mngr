import json
import shlex
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger
from tabulate import tabulate

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.group import file_group
from imbue.mngr_file.cli.target import compute_volume_path
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.cli.target import resolve_full_path
from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType
from imbue.mngr_file.data_types import PathRelativeTo

_DEFAULT_DISPLAY_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "file_type",
    "size",
    "modified",
)

_ALL_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "path",
    "file_type",
    "size",
    "modified",
    "permissions",
)

_HEADER_LABELS: Final[dict[str, str]] = {
    "name": "NAME",
    "path": "PATH",
    "file_type": "TYPE",
    "size": "SIZE",
    "modified": "MODIFIED",
    "permissions": "PERMISSIONS",
}

_FILE_TYPE_BY_STAT_CHAR: Final[dict[str, FileType]] = {
    "f": FileType.FILE,
    "d": FileType.DIRECTORY,
    "l": FileType.SYMLINK,
    "p": FileType.PIPE,
    "s": FileType.SOCKET,
    "b": FileType.BLOCK,
    "c": FileType.CHARACTER,
}

# Cross-platform Python script for listing files. Outputs tab-separated lines:
# name\tsize\tmodified_iso\ttype_char\tpermissions_str\tfull_path
# Works on both macOS and Linux since it uses Python's os/stat modules.
_LIST_SCRIPT: Final[str] = """
import os, stat, sys
from datetime import datetime, timezone

d = sys.argv[1]
is_recursive = sys.argv[2] == '1'

def emit(path):
    try:
        st = os.lstat(path)
    except OSError:
        return
    name = os.path.basename(path)
    if name == '.' or name == '':
        return
    mode = st.st_mode
    if stat.S_ISDIR(mode): tc = 'd'
    elif stat.S_ISLNK(mode): tc = 'l'
    elif stat.S_ISREG(mode): tc = 'f'
    elif stat.S_ISFIFO(mode): tc = 'p'
    elif stat.S_ISSOCK(mode): tc = 's'
    elif stat.S_ISBLK(mode): tc = 'b'
    elif stat.S_ISCHR(mode): tc = 'c'
    else: tc = '?'
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    perms = stat.filemode(mode)
    sys.stdout.write(f'{name}\\t{st.st_size}\\t{mtime}\\t{tc}\\t{perms}\\t{path}\\n')

if is_recursive:
    for root, dirs, files in os.walk(d):
        for name in dirs + files:
            emit(os.path.join(root, name))
else:
    for name in os.listdir(d):
        emit(os.path.join(d, name))
"""


class _FileListCliOptions(CommonCliOptions):
    """Options for the file list subcommand."""

    target: str
    path: str | None
    relative_to: str
    fields: str | None
    recursive: bool


@pure
def _parse_list_output_line(line: str) -> FileEntry | None:
    """Parse a single tab-separated line from the list script output."""
    parts = line.split("\t", 5)
    if len(parts) != 6:
        logger.trace("Skipping malformed list output line: {}", line)
        return None

    name, size_str, modified, type_char, permissions, full_path = parts

    if name == "." or name == "":
        return None

    file_type = _FILE_TYPE_BY_STAT_CHAR.get(type_char, FileType.OTHER)

    parsed_size: int | None = None
    if file_type != FileType.DIRECTORY:
        try:
            parsed_size = int(size_str)
        except ValueError:
            parsed_size = None

    return FileEntry(
        name=name,
        path=full_path,
        file_type=file_type,
        size=parsed_size,
        modified=modified,
        permissions=permissions,
    )


@pure
def parse_list_output(output: str) -> list[FileEntry]:
    """Parse the tab-separated output from the list script into FileEntry objects."""
    entries: list[FileEntry] = []
    for line in output.strip().splitlines():
        entry = _parse_list_output_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def list_files_on_host(
    host: OnlineHostInterface,
    directory: Path,
    is_recursive: bool,
) -> list[FileEntry]:
    """List files in a directory on a remote host using a cross-platform Python script."""
    quoted_dir = shlex.quote(str(directory))
    is_recursive_flag = "1" if is_recursive else "0"

    command = f"python3 -c {shlex.quote(_LIST_SCRIPT)} {quoted_dir} {is_recursive_flag}"

    with log_span("Listing files on host"):
        result = host.execute_command(command, timeout_seconds=30.0)

    if not result.success:
        raise MngrError(f"Failed to list files at {directory}: {result.stderr}")

    return parse_list_output(result.stdout)


def _list_volume_directory(
    volume: Volume,
    vol_path: str,
) -> list[FileEntry]:
    """List immediate children of a directory on a volume."""
    volume_files = volume.listdir(vol_path)

    entries: list[FileEntry] = []
    for vf in volume_files:
        name = vf.path.rsplit("/", 1)[-1] if "/" in vf.path else vf.path
        match vf.file_type:
            case VolumeFileType.FILE:
                file_type = FileType.FILE
            case VolumeFileType.DIRECTORY:
                file_type = FileType.DIRECTORY
            case _ as unreachable:
                assert_never(unreachable)

        size = vf.size if file_type != FileType.DIRECTORY else None
        modified = None
        if vf.mtime > 0:
            modified = datetime.fromtimestamp(vf.mtime, tz=timezone.utc).isoformat()

        entries.append(
            FileEntry(
                name=name,
                path=vf.path,
                file_type=file_type,
                size=size,
                modified=modified,
                permissions=None,
            )
        )

    return entries


def list_files_on_volume(
    volume: Volume,
    vol_path: str,
    is_recursive: bool,
) -> list[FileEntry]:
    """List files in a directory using a Volume interface."""
    with log_span("Listing files on volume"):
        entries = _list_volume_directory(volume, vol_path)

    if not is_recursive:
        return entries

    # Recurse into subdirectories via BFS
    directories_to_visit = deque(e.path for e in entries if e.file_type == FileType.DIRECTORY)
    while directories_to_visit:
        subdir_path = directories_to_visit.popleft()
        sub_entries = _list_volume_directory(volume, subdir_path)
        entries.extend(sub_entries)
        directories_to_visit.extend(e.path for e in sub_entries if e.file_type == FileType.DIRECTORY)

    return entries


@pure
def _get_field_value(entry: FileEntry, field: str) -> str:
    """Extract a display value from a FileEntry for the given field name."""
    match field:
        case "name":
            return entry.name
        case "path":
            return entry.path
        case "file_type":
            return entry.file_type.value.lower()
        case "size":
            if entry.size is None:
                return "-"
            return format_size(entry.size)
        case "modified":
            return entry.modified if entry.modified is not None else "-"
        case "permissions":
            return entry.permissions if entry.permissions is not None else "-"
        case _:
            return ""


@pure
def _entry_to_field_mapping(entry: FileEntry, fields: Sequence[str]) -> dict[str, str]:
    """Convert a FileEntry to a mapping of field name -> display value."""
    return {field: _get_field_value(entry, field) for field in fields}


@pure
def _entry_to_json_dict(entry: FileEntry) -> dict[str, Any]:
    """Convert a FileEntry to a JSON-serializable dict with raw values."""
    return {
        "name": entry.name,
        "path": entry.path,
        "file_type": entry.file_type.value.lower(),
        "size": entry.size,
        "modified": entry.modified,
        "permissions": entry.permissions,
    }


def _emit_list_result(
    entries: list[FileEntry],
    fields: tuple[str, ...],
    output_opts: OutputOptions,
) -> None:
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            if not entries:
                write_human_line("(empty)")
                return
            headers = [_HEADER_LABELS.get(f, f.upper()) for f in fields]
            rows = [[_get_field_value(entry, f) for f in fields] for entry in entries]
            table = tabulate(rows, headers=headers, tablefmt="plain")
            write_human_line(table)
        case OutputFormat.JSON:
            emit_final_json(
                {
                    "count": len(entries),
                    "files": [_entry_to_json_dict(e) for e in entries],
                }
            )
        case OutputFormat.JSONL:
            for entry in entries:
                data = _entry_to_json_dict(entry)
                write_human_line(json.dumps(data))
        case _ as unreachable:
            assert_never(unreachable)


@file_group.command(name="list")
@click.argument("target")
@click.argument("path", required=False, default=None)
@optgroup.group("Path Resolution")
@optgroup.option(
    "--relative-to",
    type=click.Choice(["work", "state", "host"], case_sensitive=False),
    default="work",
    show_default=True,
    help="Base directory for relative paths (agent targets only): work (work_dir), state (agent state dir), host (host dir)",
)
@optgroup.group("Output Format")
@optgroup.option(
    "--fields",
    default=None,
    help="Comma-separated list of fields to display: name, path, file_type, size, modified, permissions",
)
@optgroup.group("Options")
@optgroup.option(
    "--recursive",
    "-R",
    is_flag=True,
    default=False,
    help="List files recursively",
)
@add_common_options
@click.pass_context
def file_list(ctx: click.Context, **kwargs: Any) -> None:
    """List files on an agent or host.

    \b
    TARGET is the agent or host name/ID.
    PATH is the directory to list (defaults to the base directory).
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="file-list",
        command_class=_FileListCliOptions,
    )

    relative_to = PathRelativeTo(opts.relative_to.upper())

    # Resolve target
    with log_span("Resolving file target"):
        resolved = resolve_file_target(
            target_identifier=opts.target,
            mngr_ctx=mngr_ctx,
            relative_to=relative_to,
        )

    # Determine directory to list
    if opts.path is not None:
        directory = resolve_full_path(resolved.base_path, opts.path)
    else:
        directory = resolved.base_path

    # Determine fields
    if opts.fields is not None:
        fields = tuple(f.strip() for f in opts.fields.split(","))
        invalid_fields = [f for f in fields if f not in _ALL_FIELDS]
        if invalid_fields:
            valid_list = ", ".join(_ALL_FIELDS)
            raise click.BadParameter(
                f"Unknown field(s): {', '.join(invalid_fields)}. Valid fields: {valid_list}",
                param_hint="--fields",
            )
    elif output_opts.format_template is not None:
        fields = _ALL_FIELDS
    else:
        fields = _DEFAULT_DISPLAY_FIELDS

    # List files -- prefer online host, fall back to volume
    if resolved.is_online:
        entries = list_files_on_host(
            host=resolved.host,
            directory=directory,
            is_recursive=opts.recursive,
        )
    else:
        assert resolved.volume is not None
        vol_path = compute_volume_path(resolved.relative_to, resolved.agent_id, opts.path)
        entries = list_files_on_volume(
            volume=resolved.volume,
            vol_path=vol_path,
            is_recursive=opts.recursive,
        )

    # Output
    if output_opts.format_template is not None:
        for entry in entries:
            field_mapping = _entry_to_field_mapping(entry, _ALL_FIELDS)
            line = render_format_template(output_opts.format_template, field_mapping)
            write_human_line(line)
    else:
        _emit_list_result(entries, fields, output_opts)
