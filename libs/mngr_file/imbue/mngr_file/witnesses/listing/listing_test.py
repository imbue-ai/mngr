import json
import os
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_file.cli.list import file_list

# tabulate's plain format separates columns by at least two spaces, while a
# rendered size such as "5 B" holds a single one.
_COLUMN_SEPARATOR = re.compile(r"\s{2,}")


@pytest.mark.witnesses("listing.base-directory-default")
def test_listing_with_no_path_lists_the_base_directory(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_work_dir: Path,
) -> None:
    member = temp_work_dir / f"member-{uuid4().hex}.txt"
    member.write_bytes(b"in the work directory")

    result = cli_runner.invoke(file_list, [str(local_agent.name), "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert {entry["path"] for entry in json.loads(result.stdout)["files"]} == {str(member)}


@pytest.mark.witnesses("listing.named-directory")
def test_listing_with_a_relative_path_lists_that_directory(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_work_dir: Path,
) -> None:
    (temp_work_dir / f"beside-{uuid4().hex}.txt").write_bytes(b"in the base directory")
    named = temp_work_dir / f"named-{uuid4().hex}"
    named.mkdir()
    member = named / "member.txt"
    member.write_bytes(b"in the named directory")

    result = cli_runner.invoke(file_list, [str(local_agent.name), named.name, "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert {entry["path"] for entry in json.loads(result.stdout)["files"]} == {str(member)}


@pytest.mark.witnesses("listing.shallow-by-default")
def test_listing_without_descent_reports_one_level(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"shallow-{uuid4().hex}"
    subdirectory = directory / "subdirectory"
    subdirectory.mkdir(parents=True)
    top_file = directory / "top.txt"
    top_file.write_bytes(b"top")
    nested_file = subdirectory / "nested.txt"
    nested_file.write_bytes(b"nested")

    result = cli_runner.invoke(file_list, ["@localhost", directory.name, "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    reported_paths = {entry["path"] for entry in json.loads(result.stdout)["files"]}
    assert reported_paths >= {str(top_file), str(subdirectory)}
    assert str(nested_file) not in reported_paths


@pytest.mark.witnesses("listing.absolute-entry-paths")
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="only the entries of a successful listing addressed by a relative path; no other subcommand or refusal",
)
def test_listing_entry_carries_its_absolute_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"absolute-{uuid4().hex}"
    directory.mkdir()
    file_name = "member.txt"
    (directory / file_name).write_bytes(b"addressable")

    result = cli_runner.invoke(file_list, ["@localhost", directory.name, "--format", "json"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    entries_by_name = {entry["name"]: entry for entry in json.loads(result.stdout)["files"]}
    assert entries_by_name[file_name]["path"] == str(temp_host_dir / directory.name / file_name)


@pytest.mark.witnesses("listing.default-columns")
def test_human_listing_displays_name_kind_size_and_modified_by_default(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"columns-{uuid4().hex}"
    directory.mkdir()
    member = directory / "notes.txt"
    member.write_bytes(b"12345")
    modified_at = 1600000000
    os.utime(member, (modified_at, modified_at))

    result = cli_runner.invoke(file_list, ["@localhost", directory.name], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    _header, row = result.stdout.strip().splitlines()
    displayed = set(_COLUMN_SEPARATOR.split(row.strip()))
    modified_text = datetime.fromtimestamp(modified_at, tz=timezone.utc).isoformat()
    assert displayed >= {"notes.txt", "file", "5 B", modified_text}


@pytest.mark.witnesses("listing.chosen-columns")
def test_human_listing_displays_only_the_chosen_attributes(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"chosen-{uuid4().hex}"
    directory.mkdir()
    member = directory / "notes.txt"
    member.write_bytes(b"12345")
    # Pinned so the expected permissions do not depend on the ambient umask.
    member.chmod(0o640)

    result = cli_runner.invoke(
        file_list, ["@localhost", directory.name, "--fields", "name,permissions"], obj=plugin_manager
    )

    assert result.exit_code == 0, result.output
    _header, row = result.stdout.strip().splitlines()
    assert _COLUMN_SEPARATOR.split(row.strip()) == ["notes.txt", "-rw-r-----"]


@pytest.mark.witnesses("listing.unknown-column")
def test_choosing_an_attribute_no_entry_carries_is_a_usage_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(file_list, ["@localhost", "--fields", "name,colour"], obj=plugin_manager)

    assert result.exit_code == 2, result.output
    assert all(field in result.stderr for field in ("name", "path", "file_type", "size", "modified", "permissions"))
