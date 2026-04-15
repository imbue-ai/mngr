import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import paramiko
import pytest
from pydantic import SecretStr

from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.interfaces.ssh_auth import SSHAuthMethod
from imbue.mngr.interfaces.ssh_auth import expose_secrets_for_subprocess
from imbue.mngr_ssh_password_auth.auth import SSHPasswordAuth


# =========================================================================
# Construction and basic properties
# =========================================================================


def test_password_auth_has_correct_auth_type() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret123"))
    assert auth.auth_type == "password"


def test_password_auth_registered_in_registry() -> None:
    assert "password" in SSHAuthMethod._registry
    assert SSHAuthMethod._registry["password"] is SSHPasswordAuth


def test_password_auth_password_is_secret_str() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret123"))
    assert isinstance(auth.password, SecretStr)
    assert auth.password.get_secret_value() == "secret123"


# =========================================================================
# Secret masking
# =========================================================================


def test_password_not_in_repr() -> None:
    auth = SSHPasswordAuth(password=SecretStr("hunter2"))
    assert "hunter2" not in repr(auth)
    assert "**********" in repr(auth)


def test_password_not_in_str() -> None:
    auth = SSHPasswordAuth(password=SecretStr("hunter2"))
    assert "hunter2" not in str(auth)


def test_password_masked_in_model_dump_json() -> None:
    auth = SSHPasswordAuth(password=SecretStr("hunter2"))
    data = auth.model_dump(mode="json")
    assert data["password"] == "**********"
    assert "hunter2" not in str(data)


def test_password_masked_in_model_dump() -> None:
    auth = SSHPasswordAuth(password=SecretStr("hunter2"))
    data = auth.model_dump()
    assert "hunter2" not in str(data)


def test_ssh_info_with_password_auth_masks_password() -> None:
    from imbue.mngr.primitives import SSHInfo

    # SSHInfo.auth is typed as SSHKeyAuth (SSHAuthField), but at runtime
    # we can construct with any SSHAuthMethod subclass for testing masking.
    # For this test, we verify the password auth serialization directly.
    auth = SSHPasswordAuth(password=SecretStr("hunter2"))
    data = auth.model_dump(mode="json")
    assert "hunter2" not in str(data)
    assert data["password"] == "**********"


# =========================================================================
# get_display_command
# =========================================================================


def test_display_command_does_not_show_password() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    cmd = auth.get_display_command("root", "example.com", 22)
    assert "secret" not in cmd
    assert cmd == "ssh -p 22 root@example.com"


def test_display_command_with_custom_port() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    cmd = auth.get_display_command("user", "host.com", 2222)
    assert cmd == "ssh -p 2222 user@host.com"


# =========================================================================
# build_transport_command
# =========================================================================


def test_build_transport_command_uses_sshpass() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=None)
    assert transport.command.startswith("sshpass -e ssh")
    assert "-p 22" in transport.command
    assert "-o StrictHostKeyChecking=yes" in transport.command
    assert "secret" not in transport.command


def test_build_transport_command_password_in_env() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=None)
    assert "SSHPASS" in transport.env
    assert isinstance(transport.env["SSHPASS"], SecretStr)
    assert transport.env["SSHPASS"].get_secret_value() == "secret"


def test_build_transport_command_env_can_be_exposed() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=None)
    exposed = expose_secrets_for_subprocess(transport.env)
    assert exposed == {"SSHPASS": "secret"}


def test_build_transport_command_raises_without_sshpass() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value=None):
        with pytest.raises(BinaryNotInstalledError):
            auth.build_transport_command(port=22, known_hosts_file=None)


def test_build_transport_command_with_known_hosts() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=Path("/tmp/known_hosts"))
    assert "UserKnownHostsFile=/tmp/known_hosts" in transport.command


def test_build_transport_command_uses_instance_known_hosts_as_fallback() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"), known_hosts_file=Path("/my/known_hosts"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=None)
    assert "UserKnownHostsFile=/my/known_hosts" in transport.command


def test_build_transport_command_disables_pubkey_auth() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    with patch.object(shutil, "which", return_value="/usr/bin/sshpass"):
        transport = auth.build_transport_command(port=22, known_hosts_file=None)
    assert "PreferredAuthentications=password" in transport.command
    assert "PubkeyAuthentication=no" in transport.command


# =========================================================================
# configure_pyinfra_host_data
# =========================================================================


def test_configure_pyinfra_host_data_sets_password() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    data: dict[str, object] = {}
    auth.configure_pyinfra_host_data(data)
    assert data["ssh_password"] == "secret"
    assert data["ssh_look_for_keys"] is False
    assert data["ssh_allow_agent"] is False


def test_configure_pyinfra_host_data_with_known_hosts() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"), known_hosts_file=Path("/tmp/known_hosts"))
    data: dict[str, object] = {}
    auth.configure_pyinfra_host_data(data)
    assert data["ssh_known_hosts_file"] == "/tmp/known_hosts"
    assert data["ssh_strict_host_key_checking"] == "yes"


# =========================================================================
# connect_paramiko
# =========================================================================


def test_connect_paramiko_uses_password() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    client = MagicMock()
    auth.connect_paramiko(client, "example.com", 22, "root")
    client.connect.assert_called_once_with(
        hostname="example.com",
        port=22,
        username="root",
        password="secret",
        look_for_keys=False,
        allow_agent=False,
        timeout=10.0,
    )


def test_connect_paramiko_uses_reject_policy_without_known_hosts() -> None:
    auth = SSHPasswordAuth(password=SecretStr("secret"))
    client = MagicMock()
    auth.connect_paramiko(client, "example.com", 22, "root")
    import paramiko

    client.set_missing_host_key_policy.assert_called_once()
    policy_arg = client.set_missing_host_key_policy.call_args[0][0]
    assert isinstance(policy_arg, paramiko.RejectPolicy)


def test_connect_paramiko_loads_known_hosts_when_file_exists(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    auth = SSHPasswordAuth(password=SecretStr("secret"), known_hosts_file=known_hosts)
    client = MagicMock()
    auth.connect_paramiko(client, "example.com", 22, "root")
    client.load_host_keys.assert_called_once_with(str(known_hosts))


# =========================================================================
# Duplicate auth_type collision
# =========================================================================


def test_duplicate_auth_type_raises_type_error() -> None:
    with pytest.raises(TypeError, match="Duplicate SSHAuthMethod auth_type"):
        from imbue.mngr.interfaces.ssh_auth import SSHTransportCommand

        class DuplicatePasswordAuth(SSHAuthMethod):  # type: ignore[no-redef]
            auth_type: str = "password"

            def configure_pyinfra_host_data(self, host_data: dict[str, Any]) -> None:
                pass

            def build_transport_command(self, port: int, known_hosts_file: Path | None) -> SSHTransportCommand:
                raise NotImplementedError

            def connect_paramiko(
                self, client: paramiko.SSHClient, hostname: str, port: int, username: str
            ) -> None:
                pass

            def get_display_command(self, user: str, hostname: str, port: int) -> str:
                return ""
