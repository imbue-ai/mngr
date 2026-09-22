import json
from pathlib import Path
from uuid import uuid4

import click
import pluggy
import pytest
from click.testing import CliRunner
from click.testing import Result

from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import StoppedHost
from imbue.mngr_file.testing import StoppedHostFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import write_agent_record

_SUBCOMMANDS = pytest.mark.parametrize("subcommand", [file_get, file_put, file_list], ids=["get", "put", "list"])


def _invoke_on_name(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    subcommand: click.Command,
    target: str,
    name: str,
    extra_args: list[str],
) -> Result:
    """Invoke ``subcommand`` so that, were ``target`` resolved, it would act on the file ``name`` in the base."""
    path = "." if subcommand is file_list else name
    return cli_runner.invoke(subcommand, [target, path, *extra_args], input=b"replacement bytes", obj=plugin_manager)


def _files_named(root: Path, name: str) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob(name)}


@pytest.mark.witnesses("addressing.agent-target")
def test_agent_target_acts_on_the_host_that_agent_runs_on(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_host_dir: Path,
    stopped_host: StoppedHost,
    tmp_path: Path,
) -> None:
    name = f"marker-{uuid4().hex}.txt"
    (temp_host_dir / name).write_bytes(b"on the local host")
    (stopped_host.host_dir / name).write_bytes(b"on the stopped host")
    stopped_agent_name = f"stopped-agent-{uuid4().hex}"
    write_agent_record(stopped_host.host_dir, stopped_agent_name, tmp_path / "stopped_agent_work")

    local_result = cli_runner.invoke(
        file_get, [str(local_agent.name), name, "--relative-to", "host"], obj=plugin_manager
    )
    stopped_result = cli_runner.invoke(
        file_get, [stopped_agent_name, name, "--relative-to", "host"], obj=plugin_manager
    )

    assert local_result.exit_code == 0, local_result.output
    assert local_result.stdout_bytes == b"on the local host"
    assert stopped_result.exit_code == 0, stopped_result.output
    assert stopped_result.stdout_bytes == b"on the stopped host"


@pytest.mark.witnesses("addressing.agent-target")
def test_agent_target_offers_its_own_directories_as_bases(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    local_agent: AgentInterface,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    name = f"marker-{uuid4().hex}.txt"
    (local_agent.work_dir / name).write_bytes(b"in the work directory")
    (get_agent_state_dir_path(temp_host_dir, local_agent.id) / name).write_bytes(b"in the state directory")
    other_work_dir = tmp_path / "other_agent_work"
    other_work_dir.mkdir()
    other_agent = create_test_agent_state(local_host, other_work_dir, f"other-agent-{uuid4().hex}")
    (other_work_dir / name).write_bytes(b"in the other agent's work directory")
    (get_agent_state_dir_path(temp_host_dir, other_agent.id) / name).write_bytes(
        b"in the other agent's state directory"
    )

    work_result = cli_runner.invoke(
        file_get, [str(local_agent.name), name, "--relative-to", "work"], obj=plugin_manager
    )
    state_result = cli_runner.invoke(
        file_get, [str(local_agent.name), name, "--relative-to", "state"], obj=plugin_manager
    )

    assert work_result.exit_code == 0, work_result.output
    assert work_result.stdout_bytes == b"in the work directory"
    assert state_result.exit_code == 0, state_result.output
    assert state_result.stdout_bytes == b"in the state directory"


@pytest.mark.witnesses("addressing.host-target")
def test_host_target_acts_on_that_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    stopped_host: StoppedHost,
) -> None:
    name = f"marker-{uuid4().hex}.txt"
    (temp_host_dir / name).write_bytes(b"on the local host")
    (stopped_host.host_dir / name).write_bytes(b"on the stopped host")

    local_result = cli_runner.invoke(file_get, ["@localhost", name], obj=plugin_manager)
    stopped_result = cli_runner.invoke(file_get, [stopped_host.address, name], obj=plugin_manager)

    assert local_result.exit_code == 0, local_result.output
    assert local_result.stdout_bytes == b"on the local host"
    assert stopped_result.exit_code == 0, stopped_result.output
    assert stopped_result.stdout_bytes == b"on the stopped host"


@pytest.mark.witnesses("addressing.host-target")
@pytest.mark.parametrize("base_args", [[], ["--relative-to", "host"]], ids=["no-base", "host"])
def test_host_target_resolves_a_relative_path_against_the_host_directory(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
    base_args: list[str],
) -> None:
    name = f"written-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_put, ["@localhost", name, "--format", "json", *base_args], input=b"new bytes", obj=plugin_manager
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["path"] == str(temp_host_dir / name)
    assert sorted(tmp_path.rglob(name)) == [temp_host_dir / name]


@pytest.mark.witnesses("addressing.host-target")
@pytest.mark.parametrize("relative_to", ["work", "state"])
def test_host_target_never_resolves_a_relative_path_against_an_agent_directory(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_host_dir: Path,
    tmp_path: Path,
    relative_to: str,
) -> None:
    name = f"written-{uuid4().hex}.txt"

    result = cli_runner.invoke(
        file_put,
        ["@localhost", name, "--format", "json", "--relative-to", relative_to],
        input=b"new bytes",
        obj=plugin_manager,
    )

    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    is_written = result.exit_code == 0
    if is_written:
        assert json.loads(result.stdout)["path"] == str(temp_host_dir / name)
    assert sorted(tmp_path.rglob(name)) == ([temp_host_dir / name] if is_written else [])
    assert not (Path.cwd() / name).exists()


@pytest.mark.witnesses("addressing.unresolvable-target")
@_SUBCOMMANDS
@pytest.mark.parametrize("target_prefix", ["", "@"], ids=["agent", "host"])
def test_target_matching_nothing_is_refused_naming_it(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_host_dir: Path,
    stopped_host: StoppedHost,
    tmp_path: Path,
    subcommand: click.Command,
    target_prefix: str,
) -> None:
    name = f"marker-{uuid4().hex}.txt"
    for root in (local_agent.work_dir, temp_host_dir, stopped_host.host_dir):
        (root / name).write_bytes(b"existing marker bytes")
    unmatched_name = f"no-such-target-{uuid4().hex}"
    files_before = _files_named(tmp_path, name)

    result = _invoke_on_name(
        cli_runner, plugin_manager, subcommand, f"{target_prefix}{unmatched_name}", name, ["--relative-to", "host"]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert unmatched_name in result.stderr
    assert "existing marker bytes" not in result.stdout
    assert name not in result.stdout
    assert _files_named(tmp_path, name) == files_before


@pytest.mark.witnesses("addressing.ambiguous-target")
@_SUBCOMMANDS
def test_agent_name_matching_two_agents_is_refused_as_ambiguous(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    tmp_path: Path,
    subcommand: click.Command,
) -> None:
    shared_name = f"twin-agent-{uuid4().hex}"
    name = f"marker-{uuid4().hex}.txt"
    for work_dir in (tmp_path / "first_work", tmp_path / "second_work"):
        work_dir.mkdir()
        (work_dir / name).write_bytes(b"existing marker bytes")
        create_test_agent_state(local_host, work_dir, shared_name)
    files_before = _files_named(tmp_path, name)

    result = _invoke_on_name(cli_runner, plugin_manager, subcommand, shared_name, name, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert "multiple" in result.stderr.lower()
    assert "existing marker bytes" not in result.stdout
    assert name not in result.stdout
    assert _files_named(tmp_path, name) == files_before


@pytest.mark.witnesses("addressing.ambiguous-target")
@_SUBCOMMANDS
def test_host_name_matching_two_hosts_is_refused_as_ambiguous(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
    subcommand: click.Command,
) -> None:
    shared_name = f"twin-host-{uuid4().hex}"
    name = f"marker-{uuid4().hex}.txt"
    for _ in range(2):
        host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM, host_name=shared_name)
        (host.host_dir / name).write_bytes(b"existing marker bytes")
    files_before = _files_named(tmp_path, name)

    result = _invoke_on_name(cli_runner, plugin_manager, subcommand, f"@{shared_name}", name, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit), result.exception
    assert "multiple" in result.stderr.lower()
    assert "existing marker bytes" not in result.stdout
    assert name not in result.stdout
    assert _files_named(tmp_path, name) == files_before
