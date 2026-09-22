"""Witnesses for addressing/stopped-hosts.feature: reaching a host that is not running."""

import json
from pathlib import Path
from uuid import uuid4

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import StoppedHost
from imbue.mngr_file.testing import StoppedHostFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import write_agent_record

_REACHABLE_STORAGE_KINDS = (StoppedHostStorage.FILESYSTEM, StoppedHostStorage.SERVICE)

_SUBCOMMANDS = (file_get, file_put, file_list)


@pytest.fixture(params=_REACHABLE_STORAGE_KINDS, ids=lambda storage: storage.lower())
def reachable_stopped_host(request: pytest.FixtureRequest, stopped_host_factory: StoppedHostFactory) -> StoppedHost:
    """A stopped host whose persisted storage can be reached, once per kind of reachable storage."""
    return stopped_host_factory.create(request.param)


@pytest.mark.witnesses("addressing.stopped-host-reads")
@pytest.mark.witnesses(
    "addressing.never-changes-lifecycle",
    partial="checks only a successful read of a host-directory file on a stopped host with reachable storage; "
    "not writes, listings, refusals, or agent targets",
)
def test_file_under_host_dir_is_read_while_host_is_stopped(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    reachable_stopped_host: StoppedHost,
) -> None:
    content = f"stopped host read {uuid4().hex}".encode()
    file_name = f"read-{uuid4().hex}.txt"
    (reachable_stopped_host.host_dir / file_name).write_bytes(content)

    result = cli_runner.invoke(
        file_get,
        [reachable_stopped_host.address, file_name, "--relative-to", "host"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout_bytes == content
    assert not reachable_stopped_host.is_running()


@pytest.mark.witnesses("addressing.stopped-host-writes")
def test_file_written_to_stopped_host_is_there_when_it_runs_again(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    reachable_stopped_host: StoppedHost,
) -> None:
    content = f"stopped host write {uuid4().hex}".encode()
    file_name = f"write-{uuid4().hex}.txt"

    put_result = cli_runner.invoke(
        file_put,
        [reachable_stopped_host.address, file_name, "--relative-to", "host", "--format", "json"],
        input=content,
        obj=plugin_manager,
    )

    assert put_result.exit_code == 0, put_result.stderr
    assert json.loads(put_result.stdout)["event"] == "file_written"

    read_while_stopped = cli_runner.invoke(
        file_get,
        [reachable_stopped_host.address, file_name, "--relative-to", "host"],
        obj=plugin_manager,
    )
    assert read_while_stopped.stdout_bytes == content

    reachable_stopped_host.start()
    read_while_running = cli_runner.invoke(
        file_get,
        [reachable_stopped_host.address, file_name, "--relative-to", "host"],
        obj=plugin_manager,
    )
    assert read_while_running.stdout_bytes == content


@pytest.mark.parametrize("subcommand", _SUBCOMMANDS, ids=lambda command: command.name)
@pytest.mark.witnesses("addressing.work-dir-unreachable-when-stopped")
def test_work_dir_of_agent_on_stopped_host_is_refused(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host: StoppedHost,
    tmp_path: Path,
    subcommand: click.Command,
) -> None:
    agent_name = f"stopped-agent-{uuid4().hex}"
    write_agent_record(stopped_host.host_dir, agent_name, tmp_path / "work")

    result = cli_runner.invoke(
        subcommand,
        [agent_name, f"relative-{uuid4().hex}.txt"],
        input=b"content offered to put",
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "is offline" in result.stderr.lower()
    assert "--relative-to state" in result.stderr
    assert "--relative-to host" in result.stderr


@pytest.mark.parametrize("subcommand", _SUBCOMMANDS, ids=lambda command: command.name)
@pytest.mark.witnesses("addressing.no-persisted-storage")
def test_stopped_host_with_unreachable_storage_is_refused(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    unreachable_stopped_host: StoppedHost,
    subcommand: click.Command,
) -> None:
    result = cli_runner.invoke(
        subcommand,
        [unreachable_stopped_host.address, f"unreachable-{uuid4().hex}.txt", "--relative-to", "host"],
        input=b"content offered to put",
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "is offline" in result.stderr.lower()
    assert "cannot access files" in result.stderr.lower()


@pytest.mark.witnesses("addressing.mode-not-applied-when-stopped")
def test_requested_mode_is_reported_as_not_applied_on_stopped_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    reachable_stopped_host: StoppedHost,
) -> None:
    content = f"stopped host mode {uuid4().hex}".encode()
    file_name = f"mode-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_put,
        [reachable_stopped_host.address, file_name, "--relative-to", "host", "--mode", "0600"],
        input=content,
        obj=plugin_manager,
    )

    assert (reachable_stopped_host.host_dir / file_name).read_bytes() == content
    assert "mode is not settable" in result.stderr


@pytest.mark.witnesses("addressing.reduced-detail-when-stopped")
def test_listing_from_stopped_host_reports_only_file_or_directory_and_no_permissions(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    reachable_stopped_host: StoppedHost,
) -> None:
    directory_name = f"listed-{uuid4().hex}"
    directory = reachable_stopped_host.host_dir / directory_name
    directory.mkdir()
    (directory / "plain.txt").write_bytes(b"plain")
    (directory / "nested").mkdir()
    # A live host would report this entry as a symlink; persisted storage cannot tell.
    (directory / "link.txt").symlink_to(directory / "plain.txt")

    result = cli_runner.invoke(
        file_list,
        [reachable_stopped_host.address, directory_name, "--relative-to", "host", "--format", "json"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    entries = json.loads(result.stdout)["files"]
    assert len(entries) == 3
    assert {entry["file_type"] for entry in entries} <= {"file", "directory"}
    assert all(entry["permissions"] is None for entry in entries)
