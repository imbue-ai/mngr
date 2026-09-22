"""Integration tests for file get/put/list operations on localhost."""

import base64
import json
import re
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.cli.target import resolve_file_target
from imbue.mngr_file.data_types import PathRelativeTo
from imbue.mngr_file.testing import AddressedMachineFactory
from imbue.mngr_file.testing import StoppedHostStorage

_ADDRESSED_MACHINES = pytest.mark.parametrize(
    "stopped_storage",
    [None, StoppedHostStorage.FILESYSTEM, StoppedHostStorage.SERVICE],
    ids=["running-local-host", "stopped-host-filesystem", "stopped-host-service"],
)


@pytest.mark.witnesses("listing.directories-have-no-size")
def test_file_list_reports_a_file_size_and_no_directory_size(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """A file's entry carries its size in bytes, while a directory's entry carries none."""
    directory = temp_host_dir / f"sizes-{uuid4().hex}"
    directory.mkdir()
    file_content = b"listing test content"
    (directory / "file.txt").write_bytes(file_content)
    (directory / "subdirectory").mkdir()

    result = cli_runner.invoke(file_list, ["@localhost", directory.name, "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    entries_by_name = {entry["name"]: entry for entry in json.loads(result.stdout)["files"]}
    assert entries_by_name["file.txt"]["size"] == len(file_content)
    assert entries_by_name["subdirectory"]["size"] is None


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


@pytest.mark.witnesses("listing.recursive-descent")
def test_file_list_recursive_descends_into_subdirectories(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """A recursive listing reports the members of subdirectories alongside the top level."""
    directory = temp_host_dir / f"recursive-{uuid4().hex}"
    subdirectory = directory / "subdirectory"
    subdirectory.mkdir(parents=True)
    top_file = directory / "top.txt"
    top_file.write_bytes(b"top")
    nested_file = subdirectory / "nested.txt"
    nested_file.write_bytes(b"nested")

    result = cli_runner.invoke(
        file_list, ["@localhost", directory.name, "--recursive", "--format", "json"], obj=plugin_manager
    )

    assert result.exit_code == 0, result.output
    reported_paths = {entry["path"] for entry in json.loads(result.stdout)["files"]}
    assert reported_paths >= {str(top_file), str(subdirectory), str(nested_file)}


@pytest.mark.witnesses("transfer.no-file-at-path")
@pytest.mark.witnesses(
    "clean-refusals",
    partial="only get of a missing file, on the running local host and on stopped hosts reached through storage",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="only the refusal of get for a missing file, on the running local host and on stopped hosts reached through storage",
)
@_ADDRESSED_MACHINES
def test_file_get_reports_a_missing_file_as_a_user_facing_error(
    stopped_storage: StoppedHostStorage | None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    """Reading a path that does not exist is a clean error naming the path, on every kind of machine."""
    machine = addressed_machine_factory.create(stopped_storage)
    missing_name = f"missing-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_get,
        [machine.address, missing_name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert str(machine.host_dir / missing_name) in result.stderr
    assert "mngr file list" in result.stderr


@pytest.mark.witnesses("transfer.path-is-a-directory")
@pytest.mark.witnesses(
    "clean-refusals",
    partial="only get of a directory, on the running local host and on stopped hosts reached through storage",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="only the refusal of get for a directory, on the running local host and on stopped hosts reached through storage",
)
@_ADDRESSED_MACHINES
def test_file_get_reports_a_directory_as_a_user_facing_error(
    stopped_storage: StoppedHostStorage | None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    """Pointing get at a directory is a clean error that sends the user to rsync or list, on every kind of machine."""
    machine = addressed_machine_factory.create(stopped_storage)
    directory = machine.host_dir / f"a-directory-{uuid4().hex}"
    directory.mkdir()

    result = cli_runner.invoke(
        file_get,
        [machine.address, directory.name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert str(directory) in result.stderr
    assert "is a directory" in result.stderr
    assert "mngr rsync" in result.stderr
    assert "mngr file list" in result.stderr


@pytest.mark.witnesses("listing.missing-directory")
@pytest.mark.witnesses(
    "clean-refusals",
    partial="only a listing of a missing directory; no other subcommand, target, or refusal condition",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="only the refusal of a listing addressed by a relative path; no other subcommand or successful report",
)
@pytest.mark.parametrize(
    "storage",
    [None, StoppedHostStorage.FILESYSTEM, StoppedHostStorage.SERVICE],
    ids=["running", "stopped-filesystem", "stopped-service"],
)
def test_file_list_reports_a_missing_directory_as_a_user_facing_error(
    storage: StoppedHostStorage | None,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    """A directory that does not exist is refused, unlike an empty one, whether or not its host is running."""
    machine = addressed_machine_factory.create(storage)
    empty_name = f"empty-dir-{uuid4().hex}"
    (machine.host_dir / empty_name).mkdir()
    missing_name = f"missing-dir-{uuid4().hex}"

    empty_result = cli_runner.invoke(file_list, [machine.address, empty_name], obj=plugin_manager)
    result = cli_runner.invoke(file_list, [machine.address, missing_name], obj=plugin_manager)

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code != 0, result.output
    assert str(machine.host_dir / missing_name) in result.stderr
    assert result.exit_code != empty_result.exit_code, empty_result.output


@pytest.mark.witnesses("listing.empty-directory")
def test_file_list_still_reports_an_existing_empty_directory_as_empty(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """A directory that exists but holds nothing stays a successful empty listing."""
    empty_name = f"empty-dir-{uuid4().hex}"
    (temp_host_dir / empty_name).mkdir()

    result = cli_runner.invoke(file_list, ["@localhost", empty_name, "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["files"] == []


@pytest.mark.witnesses(
    "transfer.saved-to-local-file", partial="only machine-readable output; human output is witnessed separately"
)
@pytest.mark.witnesses("transfer.local-parents-created")
@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_file_get_with_output_emits_an_event_without_the_content(
    output_format: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Saving to a local file still reports what happened, minus the redundant content."""
    content = b"saved to a local file"
    remote_name = f"saved-{uuid4().hex}.txt"
    (temp_host_dir / remote_name).write_bytes(content)
    local_path = tmp_path / "nested" / "deeper" / "local.txt"

    result = cli_runner.invoke(
        file_get,
        ["@localhost", remote_name, "--relative-to", "host", "--output", str(local_path), "--format", output_format],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    assert local_path.parent.is_dir()
    assert local_path.read_bytes() == content
    event = json.loads(result.stdout)
    assert event["size"] == len(content)
    assert event["path"] == str(temp_host_dir / remote_name)
    assert event["output_path"] == str(local_path)
    assert content not in result.stdout_bytes
    assert base64.b64encode(content) not in result.stdout_bytes


@pytest.mark.witnesses(
    "transfer.saved-to-local-file", partial="only human output; machine-readable output is witnessed separately"
)
@pytest.mark.witnesses("transfer.local-parents-created")
def test_file_get_with_output_reports_the_write_in_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Human format reports the size, the path read, and the local path written, without the content."""
    content = b"human mode"
    remote_name = f"saved-human-{uuid4().hex}.txt"
    remote_path = temp_host_dir / remote_name
    remote_path.write_bytes(content)
    local_path = tmp_path / "nested" / "deeper" / "local.txt"

    result = cli_runner.invoke(
        file_get,
        ["@localhost", remote_name, "--relative-to", "host", "--output", str(local_path), "--format", "human"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    assert local_path.parent.is_dir()
    assert local_path.read_bytes() == content
    assert str(remote_path) in result.stdout
    assert str(local_path) in result.stdout
    report_without_paths = result.stdout.replace(str(remote_path), "").replace(str(local_path), "")
    assert re.search(rf"\b{len(content)}\b", report_without_paths), result.stdout
    assert content not in result.stdout_bytes


@pytest.mark.witnesses("listing.template-per-entry")
@pytest.mark.witnesses(
    "named-output-formats",
    partial="only a template over a one-entry listing; no other format, subcommand, or multi-record outcome",
)
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
        ["@localhost", directory.name, "--relative-to", "host", "--format", "{name}|{size}"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["one.txt|5 B"]


@pytest.mark.witnesses("listing.template-reaches-every-attribute")
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


@pytest.mark.witnesses("transfer.write-rendered-through-a-template")
@pytest.mark.witnesses(
    "named-output-formats",
    partial="verified only for put's template over its single record; list's templates, "
    "field rendering shared across subcommands, and the refusal of templates by get are not exercised",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="verified only for a templated put to the running local host with a relative path; "
    "refusals and other subcommands are not exercised",
)
def test_file_put_renders_a_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    """put describes its outcome with records, so it takes a template like list does."""
    file_name = f"put-tmpl-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_put,
        ["@localhost", file_name, "--relative-to", "host", "--format", "{path}|{size}"],
        input=b"0123456789",
        obj=plugin_manager,
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stderr
    path_field, size_field = lines[0].split("|")
    assert path_field == str(temp_host_dir / file_name)
    assert size_field.split()[0] == "10"
