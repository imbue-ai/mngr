import json
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import assert_never

import click
from click_option_group import optgroup
from tabulate import tabulate

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.address_params import AGENT_OR_HOST_ADDRESS
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.primitives import AgentOrHostAddress
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.group import file_group
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


class _FileListCliOptions(CommonCliOptions):
    """Options for the file list subcommand."""

    target: AgentOrHostAddress
    path: str | None
    relative_to: str
    fields: str | None
    recursive: bool


@pure
def _volume_file_to_entry(vf: VolumeFile) -> FileEntry:
    """Convert a ``VolumeFile`` (as returned by ``HostFileReadInterface.list_directory``) to a ``FileEntry``.

    ``file_type`` and ``permissions`` are passed through verbatim. A host
    (online, or local) classifies the full type set and surfaces a mode string;
    a bare volume-backed offline host only distinguishes FILE from DIRECTORY and
    leaves ``permissions`` None (rendered as ``-``).
    """
    name = vf.path.rsplit("/", 1)[-1] if "/" in vf.path else vf.path
    size = vf.size if vf.file_type != FileType.DIRECTORY else None
    modified = None
    if vf.mtime > 0:
        modified = datetime.fromtimestamp(vf.mtime, tz=timezone.utc).isoformat()

    return FileEntry(
        name=name,
        path=vf.path,
        file_type=vf.file_type,
        size=size,
        modified=modified,
        permissions=vf.permissions,
    )


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
def _entry_to_field_mapping(entry: FileEntry) -> dict[str, str]:
    """Render every attribute an entry carries, keyed by name, for a format template.

    A template addresses the whole attribute set rather than the displayed
    columns: which columns are shown is a separate choice, and a template that
    could only reach the shown ones would make the two options interfere.
    """
    return {field: _get_field_value(entry, field) for field in _ALL_FIELDS}


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
            write_json_line(
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
@click.argument("target", type=AGENT_OR_HOST_ADDRESS)
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
        is_format_template_supported=True,
    )

    relative_to = PathRelativeTo(opts.relative_to.upper())

    # Resolve target
    with log_span("Resolving file target"):
        resolved = resolve_file_target(
            target=opts.target,
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
    else:
        fields = _DEFAULT_DISPLAY_FIELDS

    # List files through the unified readable-host interface (online or volume-backed).
    with log_span("Listing files"):
        volume_files = resolved.host.list_directory(directory, recursive=opts.recursive)
        # list_directory answers a missing directory and an empty one identically, so
        # tell them apart before reporting "(empty)". The probe costs a round trip, so
        # it is only paid when the listing came back empty.
        if not volume_files and not resolved.host.path_exists(directory):
            raise MngrError(f"No directory at {directory}.")
    entries = [_volume_file_to_entry(vf) for vf in volume_files]

    if output_opts.format_template is not None:
        emit_format_template_lines(output_opts.format_template, [_entry_to_field_mapping(e) for e in entries])
    else:
        _emit_list_result(entries, fields, output_opts)
