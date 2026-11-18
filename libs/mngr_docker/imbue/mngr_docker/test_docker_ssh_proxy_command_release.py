"""Integration tests for releasing the ProxyCommand behind a docker-py ``ssh://`` client."""

import shlex
from pathlib import Path
from typing import Final
from uuid import uuid4

import docker
import docker.constants
import paramiko
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import assert_runs_leave_no_descriptors_or_child_processes_behind
from imbue.mngr.utils.testing import in_process_paramiko_sshd
from imbue.mngr.utils.testing import make_client_and_host_ssh_keys
from imbue.mngr.utils.testing import write_ssh_config_reaching_port_through_proxy_command
from imbue.mngr_docker.config import DockerProviderConfig
from imbue.mngr_docker.instance import DockerProviderInstance
from imbue.mngr_docker.instance import _close_docker_client_releasing_its_ssh_proxy_command

_RUN_COUNT: Final[int] = 5


def _write_user_ssh_config(ssh_config_text: str) -> Path:
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "config").write_text(ssh_config_text)
    return ssh_dir


@pytest.mark.parametrize(
    "proxy_command",
    [
        pytest.param(shlex.join(["sh", "-c", "printf 'SSH-1.0-incompatible\\r\\n'"]), id="proxy-exits"),
        pytest.param(
            shlex.join(["sh", "-c", "printf 'SSH-1.0-incompatible\\r\\n'; exec sleep 52917"]), id="proxy-stays-up"
        ),
    ],
)
@pytest.mark.timeout(60)
def test_failed_docker_client_builds_over_ssh_leave_no_proxy_command_pipes_or_children_while_their_errors_are_held(
    temp_mngr_ctx: MngrContext, proxy_command: str
) -> None:
    """A failed ``ssh://`` client build releases its ProxyCommand even while the error it raised is still referenced."""
    host_alias = f"docker-daemon-host-{uuid4().hex}"
    _write_user_ssh_config(f"Host {host_alias}\n  ProxyCommand {proxy_command}\n")
    held_errors: list[ProviderUnavailableError] = []

    def build_once() -> None:
        provider = DockerProviderInstance(
            name=ProviderInstanceName("test-docker-ssh-build-fails"),
            host_dir=Path("/mngr"),
            mngr_ctx=temp_mngr_ctx,
            config=DockerProviderConfig(host=f"ssh://{host_alias}", isolate_host_volumes=False),
        )
        with pytest.raises(ProviderUnavailableError) as excinfo:
            _ = provider._docker_client
        held_errors.append(excinfo.value)

    assert_runs_leave_no_descriptors_or_child_processes_behind(build_once, _RUN_COUNT)

    assert len(held_errors) == _RUN_COUNT + 1
    assert all(isinstance(error.__cause__, paramiko.SSHException) for error in held_errors)


@pytest.mark.timeout(60)
def test_closing_a_docker_client_connected_over_ssh_leaves_no_proxy_command_pipes_or_children(tmp_path: Path) -> None:
    """Closing a docker-py client whose ``ssh://`` connection runs through a ProxyCommand frees the proxy's pipes and child."""
    host_alias = f"docker-daemon-host-{uuid4().hex}"
    client_private_key, host_private_key = make_client_and_host_ssh_keys(tmp_path)
    host_key = paramiko.PKey.from_path(host_private_key)

    with in_process_paramiko_sshd(
        host_private_key, client_private_key, is_auth_held=False, dropped_connection_count=0
    ) as (port, _server):
        ssh_config_path = write_ssh_config_reaching_port_through_proxy_command(
            tmp_path, host_alias, port, client_private_key
        )
        ssh_dir = _write_user_ssh_config(ssh_config_path.read_text())
        (ssh_dir / "known_hosts").write_text(f"{host_alias} {host_key.get_name()} {host_key.get_base64()}\n")

        def connect_and_close() -> None:
            # With the API version given, docker-py does not ask the daemon for one, so building the client only connects.
            client = docker.DockerClient(
                base_url=f"ssh://{host_alias}", version=docker.constants.DEFAULT_DOCKER_API_VERSION
            )
            _close_docker_client_releasing_its_ssh_proxy_command(client)

        assert_runs_leave_no_descriptors_or_child_processes_behind(connect_and_close, _RUN_COUNT)
