from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.utils.testing import generate_ssh_keypair_with_paramiko
from imbue.mngr.utils.testing import in_process_paramiko_sshd
from imbue.mngr.utils.testing import make_local_ssh_host_factory
from imbue.mngr_latchkey.discovery import is_transient_remote_wiring_error

# How long the client waits for sshd to answer authentication; the server never answers.
_CLIENT_AUTH_TIMEOUT_SECONDS = 0.5


def _ssh_keypairs(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a client key and a server host key, returning (client_private_key, host_private_key)."""
    client_key_dir = tmp_path / "client"
    client_key_dir.mkdir()
    server_key_dir = tmp_path / "server"
    server_key_dir.mkdir()
    client_private_key, _ = generate_ssh_keypair_with_paramiko(client_key_dir)
    host_private_key, _ = generate_ssh_keypair_with_paramiko(server_key_dir)
    return client_private_key, host_private_key


@pytest.mark.timeout(60)
def test_an_sshd_slow_to_answer_auth_is_classified_as_a_transient_wiring_failure(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """Pins the error shape real pyinfra and paramiko produce for an auth timeout, end to end.

    The outer host reports it as a ``HostAuthenticationError``, and paramiko's
    timeout is reachable behind it only through pyinfra's implicit exception
    context, so this fails if an upgrade of either library drops that link or
    rewords the timeout.
    """
    client_private_key, host_private_key = _ssh_keypairs(tmp_path)
    with in_process_paramiko_sshd(host_private_key, client_private_key, is_auth_held=True) as (port, _server):
        create_ssh_host = make_local_ssh_host_factory(
            port, host_private_key, client_private_key, temp_host_dir, temp_mngr_ctx, tmp_path
        )
        host = create_ssh_host("slow-auth")
        # pyinfra's connector copies the host data when it is built, so the override goes on its copy.
        connector_data = host.connector.host.connector.data
        connector_data["ssh_paramiko_connect_kwargs"] = {
            **connector_data["ssh_paramiko_connect_kwargs"],
            "auth_timeout": _CLIENT_AUTH_TIMEOUT_SECONDS,
        }

        with pytest.raises(HostAuthenticationError) as raised:
            host.connect()

    assert is_transient_remote_wiring_error(raised.value) is True


@pytest.mark.timeout(60)
def test_an_sshd_rejecting_the_key_is_classified_as_a_permanent_wiring_failure(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A key sshd actually refuses still reaches Sentry at once, through the same real libraries."""
    client_private_key, host_private_key = _ssh_keypairs(tmp_path)
    other_key_dir = tmp_path / "other"
    other_key_dir.mkdir()
    other_private_key, _ = generate_ssh_keypair_with_paramiko(other_key_dir)
    with in_process_paramiko_sshd(host_private_key, other_private_key, is_auth_held=False) as (port, _server):
        create_ssh_host = make_local_ssh_host_factory(
            port, host_private_key, client_private_key, temp_host_dir, temp_mngr_ctx, tmp_path
        )
        host = create_ssh_host("rejected-key")

        with pytest.raises(HostAuthenticationError) as raised:
            host.connect()

    assert is_transient_remote_wiring_error(raised.value) is False
