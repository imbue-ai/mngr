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
from imbue.mngr_file.cli.list import file_list
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


def test_file_get_reports_a_missing_file_as_a_user_facing_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Reading a path that does not exist is a clean error, not an unhandled FileNotFoundError."""
    missing_name = f"missing-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_get,
        ["@localhost", missing_name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, FileNotFoundError), result.exception
    assert "Traceback" not in result.output
    assert missing_name in result.output
    assert "no file" in result.output.lower()


def test_file_get_reports_a_directory_as_a_user_facing_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Pointing get at a directory is a clean error, not an unhandled IsADirectoryError."""
    directory = tmp_path / "a-directory"
    directory.mkdir()

    result = cli_runner.invoke(
        file_get,
        ["@localhost", str(directory)],
        obj=plugin_manager,
    )

    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, IsADirectoryError), result.exception
    assert "Traceback" not in result.output
    assert "directory" in result.output.lower()
    # A user reaching for a whole directory wants to transfer it, which is rsync's job.
    assert "rsync" in result.output


def test_file_list_reports_a_missing_directory_as_a_user_facing_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """A directory that does not exist is an error, not an empty listing."""
    missing_name = f"missing-dir-{uuid4().hex}"

    result = cli_runner.invoke(
        file_list,
        ["@localhost", missing_name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert missing_name in result.output
    assert "(empty)" not in result.output


def test_file_list_still_reports_an_existing_empty_directory_as_empty(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A directory that exists but holds nothing stays a successful empty listing."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    empty_name = f"empty-dir-{uuid4().hex}"
    (resolved.base_path / empty_name).mkdir()

    result = cli_runner.invoke(
        file_list,
        ["@localhost", empty_name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert "(empty)" in result.output


def test_file_get_with_output_emits_an_event_without_the_content(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Saving to a local file still reports what happened, minus the redundant content."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    content = b"saved to a local file"
    remote_name = f"saved-{uuid4().hex}.txt"
    (resolved.base_path / remote_name).write_bytes(content)
    local_path = tmp_path / "nested" / "local.txt"

    result = cli_runner.invoke(
        file_get,
        ["@localhost", remote_name, "--relative-to", "host", "--output", str(local_path), "--format", "json"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert local_path.read_bytes() == content
    event = json.loads(result.output)
    assert event["event"] == "file_read"
    assert event["size"] == len(content)
    assert event["output_path"] == str(local_path)
    assert event["path"] == str(resolved.base_path / remote_name)
    assert "content_base64" not in event


def test_file_get_with_output_reports_the_write_in_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Human format announces the local write the same way put announces a remote one."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    content = b"human mode"
    remote_name = f"saved-human-{uuid4().hex}.txt"
    (resolved.base_path / remote_name).write_bytes(content)
    local_path = tmp_path / "local.txt"

    result = cli_runner.invoke(
        file_get,
        ["@localhost", remote_name, "--relative-to", "host", "--output", str(local_path)],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert local_path.read_bytes() == content
    assert str(len(content)) in result.output
    assert str(local_path) in result.output


def test_file_list_renders_a_format_template_per_entry(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """list accepts a format template, as the other record-emitting mngr commands do."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    directory = resolved.base_path / f"tmpl-{uuid4().hex}"
    directory.mkdir()
    (directory / "one.txt").write_bytes(b"12345")

    result = cli_runner.invoke(
        file_list,
        ["@localhost", directory.name, "--relative-to", "host", "--format", "{name}|{size}|{file_type}"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "one.txt|5 B|file"


def test_file_list_format_template_can_use_every_attribute(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Every attribute an entry carries is addressable from a template, not just the default columns."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    directory = resolved.base_path / f"tmpl-all-{uuid4().hex}"
    directory.mkdir()
    entry = directory / "two.txt"
    entry.write_bytes(b"xy")
    # Set the mode explicitly: what a fresh file gets otherwise depends on the
    # ambient umask, which differs between a developer's machine and CI.
    entry.chmod(0o640)

    result = cli_runner.invoke(
        file_list,
        ["@localhost", directory.name, "--relative-to", "host", "--format", "{path}::{permissions}"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    # permissions is absent from the default display, so reaching it proves the
    # template addresses the whole attribute set.
    assert result.output.strip() == f"{entry}::-rw-r-----"


def test_file_put_renders_a_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """put describes its outcome with records, so it takes a template like list does."""
    resolved = resolve_file_target(
        target=parse_agent_or_host_address("@localhost"),
        mngr_ctx=temp_mngr_ctx,
        relative_to=PathRelativeTo.HOST,
    )
    file_name = f"put-tmpl-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_put,
        ["@localhost", file_name, "--relative-to", "host", "--format", "{path}|{size}"],
        input=b"0123456789",
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{resolved.base_path / file_name}|10 B"
    assert (resolved.base_path / file_name).read_bytes() == b"0123456789"
