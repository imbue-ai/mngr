from pathlib import Path
from uuid import uuid4

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import StoppedHostFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import write_agent_record


@pytest.mark.parametrize(
    ("base_request", "expected_content"),
    [
        pytest.param(
            (),
            b"in the work directory",
            id="no-base-requested",
            marks=pytest.mark.witnesses("addressing.work-dir-default"),
        ),
        pytest.param(
            ("--relative-to", "state"),
            b"in the state directory",
            id="state-base",
            marks=pytest.mark.witnesses("addressing.state-dir-base"),
        ),
        pytest.param(
            ("--relative-to", "host"),
            b"in the host directory",
            id="host-base",
            marks=pytest.mark.witnesses("addressing.host-dir-base"),
        ),
    ],
)
def test_a_relative_path_against_an_agent_resolves_against_the_requested_base(
    base_request: tuple[str, ...],
    expected_content: bytes,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host_factory: StoppedHostFactory,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """The same relative path exists under every base and in the host directory of a host the agent does not run on."""
    agent_host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
    agent_host.start()
    work_dir = tmp_path / "agent_work_dir"
    work_dir.mkdir()
    agent_name = f"resolution-agent-{uuid4().hex}"
    agent_id = write_agent_record(agent_host.host_dir, agent_name, work_dir)
    relative_path = f"resolution-{uuid4().hex}.txt"
    (work_dir / relative_path).write_bytes(b"in the work directory")
    (get_agent_state_dir_path(agent_host.host_dir, agent_id) / relative_path).write_bytes(b"in the state directory")
    (agent_host.host_dir / relative_path).write_bytes(b"in the host directory")
    (temp_host_dir / relative_path).write_bytes(b"in another host's host directory")

    result = cli_runner.invoke(
        file_get,
        [agent_name, relative_path, *base_request],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == expected_content


@pytest.mark.witnesses("addressing.host-target-base")
def test_a_relative_path_against_a_host_resolves_against_its_host_directory(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host_factory: StoppedHostFactory,
    temp_host_dir: Path,
) -> None:
    """The same relative path also exists in the host directory of a host other than the target."""
    target_host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
    target_host.start()
    relative_path = f"resolution-{uuid4().hex}.txt"
    (target_host.host_dir / relative_path).write_bytes(b"in the host directory")
    (temp_host_dir / relative_path).write_bytes(b"in another host's host directory")

    result = cli_runner.invoke(file_get, [target_host.address, relative_path], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == b"in the host directory"


@pytest.mark.witnesses("addressing.absolute-path-wins")
@pytest.mark.parametrize(
    ("is_agent_target", "base_requests"),
    [
        pytest.param(True, ((), ("--relative-to", "state"), ("--relative-to", "host")), id="agent"),
        pytest.param(False, ((), ("--relative-to", "host")), id="host"),
    ],
)
def test_an_absolute_path_names_its_file_regardless_of_the_base(
    is_agent_target: bool,
    base_requests: tuple[tuple[str, ...], ...],
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """Every base the target admits holds a decoy under the absolute path's own components, which only a consulted base could surface."""
    absolute_path = tmp_path / f"outside-every-base-{uuid4().hex}" / "witness.txt"
    absolute_path.parent.mkdir()
    absolute_path.write_bytes(b"outside every base")
    for base in (local_agent.work_dir, get_agent_state_dir_path(temp_host_dir, local_agent.id), temp_host_dir):
        decoy = base / absolute_path.relative_to(absolute_path.anchor)
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_bytes(b"decoy under a base directory")
    target = str(local_agent.name) if is_agent_target else "@localhost"

    results = [
        cli_runner.invoke(file_get, [target, str(absolute_path), *base_request], obj=plugin_manager)
        for base_request in base_requests
    ]

    assert [result.exit_code for result in results] == [0] * len(base_requests), [r.output for r in results]
    assert [result.stdout_bytes for result in results] == [b"outside every base"] * len(base_requests)


@pytest.mark.witnesses("addressing.state-base-needs-an-agent")
@pytest.mark.parametrize("subcommand", [file_get, file_put, file_list], ids=["get", "put", "list"])
def test_requesting_the_state_directory_for_a_host_target_is_a_usage_error(
    subcommand: click.Command,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(
        subcommand,
        ["@localhost", f"resolution-{uuid4().hex}.txt", "--relative-to", "state"],
        input=b"content offered to put",
        obj=plugin_manager,
    )

    assert result.exit_code == 2, result.output
    assert "only valid for agent targets" in result.stderr
