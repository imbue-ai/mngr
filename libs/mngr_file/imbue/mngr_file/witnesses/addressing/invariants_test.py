import base64
import json
from pathlib import Path
from uuid import uuid4

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


def _arrange_addressing(
    case: str,
    local_agent: AgentInterface,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> tuple[str, list[str], str]:
    """Arrange one way of addressing a fresh directory: the target, the base options, and the directory's path."""
    directory = f"same-resolution-{uuid4().hex}"
    match case:
        case "agent-work-base":
            return str(local_agent.name), [], directory
        case "agent-state-base":
            return str(local_agent.name), ["--relative-to", "state"], directory
        case "agent-host-base":
            return str(local_agent.name), ["--relative-to", "host"], directory
        case "agent-absolute-path":
            return str(local_agent.name), [], str(tmp_path / directory)
        case "running-host":
            return "@localhost", [], directory
        case "host-absolute-path":
            return "@localhost", [], str(tmp_path / directory)
        case "stopped-host-absolute-path":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            return host.address, [], str(host.host_dir / directory)
        case "stopped-host":
            return stopped_host_factory.create(StoppedHostStorage.FILESYSTEM).address, [], directory
        case "stopped-service-host":
            return stopped_host_factory.create(StoppedHostStorage.SERVICE).address, [], directory
        case "stopped-agent-state-base" | "stopped-agent-host-base":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            agent_name = f"stopped-agent-{uuid4().hex}"
            write_agent_record(host.host_dir, agent_name, tmp_path / "stopped-work")
            base = "state" if case == "stopped-agent-state-base" else "host"
            return agent_name, ["--relative-to", base], directory
        case _:
            raise AssertionError(f"Unknown addressing case: {case}")


@pytest.mark.witnesses(
    "addressing.same-resolution-everywhere",
    partial="checks every base, both target kinds, absolute paths by both target kinds, and running and stopped "
    "machines, each with one path; cannot cover every target and path a user can give",
)
@pytest.mark.parametrize(
    "case",
    [
        "agent-work-base",
        "agent-state-base",
        "agent-host-base",
        "agent-absolute-path",
        "running-host",
        "host-absolute-path",
        "stopped-host",
        "stopped-host-absolute-path",
        "stopped-service-host",
        "stopped-agent-state-base",
        "stopped-agent-host-base",
    ],
)
def test_one_target_and_path_name_the_same_file_whether_written_read_or_listed(
    case: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> None:
    target, base_options, directory = _arrange_addressing(case, local_agent, stopped_host_factory, tmp_path)
    file_path = f"{directory}/witness.txt"
    content = f"same file {uuid4().hex}".encode()

    put_result = cli_runner.invoke(
        file_put, [target, file_path, *base_options, "--format", "json"], input=content, obj=plugin_manager
    )
    get_result = cli_runner.invoke(
        file_get, [target, file_path, *base_options, "--format", "json"], obj=plugin_manager
    )
    list_result = cli_runner.invoke(
        file_list, [target, directory, *base_options, "--format", "json"], obj=plugin_manager
    )

    assert put_result.exit_code == 0, put_result.output
    assert get_result.exit_code == 0, get_result.output
    assert list_result.exit_code == 0, list_result.output
    written_path = json.loads(put_result.stdout)["path"]
    read_event = json.loads(get_result.stdout)
    assert base64.b64decode(read_event["content_base64"]) == content
    assert read_event["path"] == written_path
    assert [entry["path"] for entry in json.loads(list_result.stdout)["files"]] == [written_path]


def _arrange_refused_addressing(
    case: str,
    local_agent: AgentInterface,
    local_host: Host,
    temp_work_dir: Path,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> tuple[str, list[str], str]:
    """Arrange a target, base options, and path whose resolution is refused."""
    relative_path = f"refused-{uuid4().hex}/witness.txt"
    match case:
        case "unknown-agent":
            return f"nobody-{uuid4().hex}", [], relative_path
        case "unknown-host":
            return f"@nowhere-{uuid4().hex}", [], relative_path
        case "ambiguous-agent":
            create_test_agent_state(local_host, temp_work_dir, str(local_agent.name))
            return str(local_agent.name), [], relative_path
        case "ambiguous-host":
            host_name = f"twin-{uuid4().hex}"
            stopped_host_factory.create(StoppedHostStorage.FILESYSTEM, host_name=host_name)
            stopped_host_factory.create(StoppedHostStorage.FILESYSTEM, host_name=host_name)
            return f"@{host_name}", [], relative_path
        case "state-base-for-host":
            return "@localhost", ["--relative-to", "state"], relative_path
        case "work-base-on-stopped-host":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            agent_name = f"stopped-agent-{uuid4().hex}"
            write_agent_record(host.host_dir, agent_name, tmp_path / "stopped-work")
            return agent_name, [], relative_path
        case "unreachable-storage":
            return stopped_host_factory.create(StoppedHostStorage.UNREACHABLE).address, [], relative_path
        case "absolute-path-outside-stopped-storage":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            return host.address, [], str(tmp_path / relative_path)
        case _:
            raise AssertionError(f"Unknown refusal case: {case}")


def _stated_refusal(result: Result) -> str:
    """What a command stated in refusing, without the usage line click heads a usage error with its own name."""
    _, marker, refusal = result.stderr.partition("Error: ")
    assert marker, result.stderr
    return refusal


@pytest.mark.witnesses(
    "addressing.same-resolution-everywhere",
    partial="checks one instance of each resolution refusal; cannot cover every target and path a user can give",
)
@pytest.mark.parametrize(
    "case",
    [
        "unknown-agent",
        "unknown-host",
        "ambiguous-agent",
        "ambiguous-host",
        "state-base-for-host",
        "work-base-on-stopped-host",
        "unreachable-storage",
        "absolute-path-outside-stopped-storage",
    ],
)
def test_a_resolution_refusal_is_the_same_refusal_for_put_get_and_list(
    case: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    local_host: Host,
    temp_work_dir: Path,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> None:
    target, base_options, path = _arrange_refused_addressing(
        case, local_agent, local_host, temp_work_dir, stopped_host_factory, tmp_path
    )

    put_result = cli_runner.invoke(file_put, [target, path, *base_options], input=b"refused", obj=plugin_manager)
    get_result = cli_runner.invoke(file_get, [target, path, *base_options], obj=plugin_manager)
    list_result = cli_runner.invoke(file_list, [target, path, *base_options], obj=plugin_manager)

    assert put_result.exit_code != 0, put_result.output
    assert get_result.exit_code == put_result.exit_code, get_result.output
    assert list_result.exit_code == put_result.exit_code, list_result.output
    assert _stated_refusal(get_result) == _stated_refusal(put_result)
    assert _stated_refusal(list_result) == _stated_refusal(put_result)


_LIFECYCLE_OPERATIONS = ["get", "put", "list", "get-missing-file", "put-with-mode", "list-recursive"]

_REFUSED_WHEREVER_SERVED = frozenset({"get-missing-file"})


def _invoke_operation_on_directory(
    operation: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    target: str,
    base_options: list[str],
    directory: str,
) -> Result:
    """Read, write, or list in ``directory``, which holds ``witness.txt``, in one of several ways."""
    match operation:
        case "get":
            return cli_runner.invoke(file_get, [target, f"{directory}/witness.txt", *base_options], obj=plugin_manager)
        case "get-missing-file":
            return cli_runner.invoke(file_get, [target, f"{directory}/absent.txt", *base_options], obj=plugin_manager)
        case "put":
            return cli_runner.invoke(
                file_put, [target, f"{directory}/written.txt", *base_options], input=b"new", obj=plugin_manager
            )
        case "put-with-mode":
            return cli_runner.invoke(
                file_put,
                [target, f"{directory}/written.txt", *base_options, "--mode", "0600"],
                input=b"new",
                obj=plugin_manager,
            )
        case "list":
            return cli_runner.invoke(file_list, [target, directory, *base_options], obj=plugin_manager)
        case "list-recursive":
            return cli_runner.invoke(file_list, [target, directory, *base_options, "--recursive"], obj=plugin_manager)
        case _:
            raise AssertionError(f"Unknown operation: {operation}")


def _arrange_lifecycle_directory(target_kind: str, host: StoppedHost, tmp_path: Path) -> tuple[str, list[str], str]:
    """Arrange a directory holding ``witness.txt`` on ``host``: the target, the base options, and the directory."""
    agent_name = f"lifecycle-agent-{uuid4().hex}"
    work_dir = tmp_path / "lifecycle-work"
    agent_id = write_agent_record(host.host_dir, agent_name, work_dir)
    match target_kind:
        case "host":
            target, base_options, base_dir = host.address, [], host.host_dir
        case "agent-state-base":
            target, base_options, base_dir = (
                agent_name,
                ["--relative-to", "state"],
                get_agent_state_dir_path(host.host_dir, agent_id),
            )
        case "agent-host-base":
            target, base_options, base_dir = agent_name, ["--relative-to", "host"], host.host_dir
        case "agent-work-base":
            target, base_options, base_dir = agent_name, [], work_dir
        case _:
            raise AssertionError(f"Unknown target kind: {target_kind}")
    directory = f"lifecycle-{uuid4().hex}"
    (base_dir / directory).mkdir(parents=True)
    (base_dir / directory / "witness.txt").write_bytes(b"lifecycle witness")
    return target, base_options, directory


@pytest.mark.witnesses(
    "addressing.never-changes-lifecycle",
    partial="checks reads, writes, and listings (with and without permissions or recursion, served and refused) "
    "by host target and by each agent base, each with one path; cannot cover every command, path, and option "
    "a user can give",
)
@pytest.mark.parametrize("operation", _LIFECYCLE_OPERATIONS)
@pytest.mark.parametrize(
    ("target_kind", "storage", "is_served"),
    [
        ("host", StoppedHostStorage.FILESYSTEM, True),
        ("host", StoppedHostStorage.SERVICE, True),
        ("host", StoppedHostStorage.UNREACHABLE, False),
        ("agent-state-base", StoppedHostStorage.FILESYSTEM, True),
        ("agent-state-base", StoppedHostStorage.SERVICE, True),
        ("agent-state-base", StoppedHostStorage.UNREACHABLE, False),
        ("agent-host-base", StoppedHostStorage.FILESYSTEM, True),
        ("agent-work-base", StoppedHostStorage.FILESYSTEM, False),
    ],
)
def test_addressing_a_stopped_host_leaves_it_stopped(
    target_kind: str,
    storage: StoppedHostStorage,
    is_served: bool,
    operation: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> None:
    host = stopped_host_factory.create(storage)
    target, base_options, directory = _arrange_lifecycle_directory(target_kind, host, tmp_path)

    result = _invoke_operation_on_directory(operation, cli_runner, plugin_manager, target, base_options, directory)

    is_expected_served = is_served and operation not in _REFUSED_WHEREVER_SERVED
    assert (result.exit_code == 0) == is_expected_served, result.output
    assert not host.is_running()


@pytest.mark.witnesses(
    "addressing.never-changes-lifecycle",
    partial="checks reads, writes, and listings (with and without permissions or recursion, served and refused) "
    "by host target and by each agent base, each with one path; cannot cover every command, path, and option "
    "a user can give",
)
@pytest.mark.parametrize("operation", _LIFECYCLE_OPERATIONS)
@pytest.mark.parametrize("target_kind", ["host", "agent-work-base", "agent-state-base", "agent-host-base"])
def test_addressing_a_running_host_leaves_it_running(
    target_kind: str,
    operation: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> None:
    host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
    host.start()
    target, base_options, directory = _arrange_lifecycle_directory(target_kind, host, tmp_path)

    result = _invoke_operation_on_directory(operation, cli_runner, plugin_manager, target, base_options, directory)

    assert (result.exit_code == 0) == (operation not in _REFUSED_WHEREVER_SERVED), result.output
    assert host.is_running()
