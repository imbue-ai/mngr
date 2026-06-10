"""Integration tests for file get/put/list operations on localhost."""

import base64
import json
from pathlib import Path
from uuid import uuid4

import pluggy
from click.testing import CliRunner

from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import _volume_file_to_entry
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.data_types import FileEntry
from imbue.mngr_file.data_types import FileType
from imbue.mngr_file.data_types import PathRelativeTo


def _list_entries(host: object, directory: Path, *, recursive: bool) -> list[FileEntry]:
    assert isinstance(host, OnlineHostInterface)
    return [_volume_file_to_entry(vf) for vf in host.list_directory(directory, recursive=recursive)]


def test_list_files_on_localhost(temp_mngr_ctx: MngrContext) -> None:
    """A file and a directory created under the host dir appear in the listing with correct attributes."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )

    file_name = f"list-file-{uuid4().hex}.txt"
    dir_name = f"list-dir-{uuid4().hex}"
    file_content = b"listing test content"
    (resolved.base_path / file_name).write_bytes(file_content)
    (resolved.base_path / dir_name).mkdir()

    entries = _list_entries(resolved.host, resolved.base_path, recursive=False)
    entries_by_name = {e.name: e for e in entries}

    assert file_name in entries_by_name
    file_entry = entries_by_name[file_name]
    assert file_entry.file_type == FileType.FILE
    assert file_entry.size == len(file_content)

    assert dir_name in entries_by_name
    dir_entry = entries_by_name[dir_name]
    assert dir_entry.file_type == FileType.DIRECTORY
    assert dir_entry.size is None


def test_file_put_then_get_round_trips_content_via_cli(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Driving the put and get commands end-to-end round-trips file content through localhost."""
    content = b"end-to-end cli content 91273"
    file_name = f"e2e-cli-{uuid4().hex}.txt"

    put_result = cli_runner.invoke(
        file_put,
        ["@localhost", file_name, "--relative-to", "host", "--format", "json"],
        input=content,
        obj=plugin_manager,
    )
    assert put_result.exit_code == 0, put_result.output
    put_event = json.loads(put_result.output)
    assert put_event["event"] == "file_written"
    assert put_event["size"] == len(content)

    get_result = cli_runner.invoke(
        file_get,
        ["@localhost", file_name, "--relative-to", "host", "--format", "json"],
        obj=plugin_manager,
    )
    assert get_result.exit_code == 0, get_result.output
    get_event = json.loads(get_result.output)
    assert get_event["event"] == "file_read"
    assert get_event["size"] == len(content)
    assert base64.b64decode(get_event["content_base64"]) == content


def test_list_files_recursive_on_localhost(temp_mngr_ctx: MngrContext) -> None:
    """List files recursively on the local host dir."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )

    # Create a nested structure with unique names so the test is self-isolating.
    nested_dir = resolved.base_path / f"nested-dir-{uuid4().hex}"
    nested_dir.mkdir()
    nested_file = nested_dir / "nested-file.txt"
    nested_file.write_text("nested content")

    entries = _list_entries(resolved.host, resolved.base_path, recursive=True)
    names = {e.name for e in entries}
    assert nested_dir.name in names
    assert "nested-file.txt" in names
