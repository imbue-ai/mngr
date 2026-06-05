"""Tests for the OVH TOFU host-key pinning + root-bootstrap helpers."""

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa

from imbue.mngr_ovh.bootstrap import _load_private_key
from imbue.mngr_ovh.bootstrap import bootstrap_root_authorized_keys_via_user
from imbue.mngr_ovh.bootstrap import install_required_outer_packages
from imbue.mngr_ovh.bootstrap import pin_host_key_via_tofu
from imbue.mngr_ovh.bootstrap import purge_qemu_packages
from imbue.mngr_ovh.bootstrap import verify_root_ssh
from imbue.mngr_ovh.bootstrap import wait_for_ssh_after_rebuild
from imbue.mngr_vps_docker.errors import VpsProvisioningError


def _make_private_key(tmp_path: Path) -> Path:
    """Create an Ed25519 keypair on disk and return the private-key path."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_path = tmp_path / "id_ed25519"
    private_path.write_bytes(pem)
    return private_path


def _make_paramiko_ed25519_pkey() -> paramiko.PKey:
    """Return a fresh paramiko Ed25519 PKey that we can hand to a MissingHostKeyPolicy."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    from io import StringIO

    return paramiko.Ed25519Key.from_private_key(StringIO(pem))


def test_pin_host_key_via_tofu_writes_known_hosts(tmp_path: Path) -> None:
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    fake_server_key = _make_paramiko_ed25519_pkey()

    fake_transport = MagicMock()
    fake_transport.get_remote_server_key.return_value = fake_server_key

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "get_transport", autospec=True, return_value=fake_transport),
    ):
        pinned = pin_host_key_via_tofu(
            hostname="vps-x.vps.ovh.us",
            port=22,
            ssh_user="root",
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
        )

    assert pinned.startswith("ssh-ed25519 ")
    assert "vps-x.vps.ovh.us" in known_hosts_path.read_text()
    assert pinned in known_hosts_path.read_text()


def test_pin_host_key_times_out_when_connect_keeps_failing(tmp_path: Path) -> None:
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        raise paramiko.SSHException("nope")

    with patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect):
        with patch("imbue.mngr_ovh.bootstrap._SSH_CONNECT_BACKOFF_SECONDS", 0.0):
            with pytest.raises(VpsProvisioningError, match="host-key TOFU"):
                pin_host_key_via_tofu(
                    hostname="vps-x.vps.ovh.us",
                    port=22,
                    ssh_user="root",
                    private_key_path=private_key_path,
                    known_hosts_path=known_hosts_path,
                    timeout_seconds=0.05,
                )


def test_wait_for_ssh_returns_when_socket_connects() -> None:
    with patch("socket.create_connection", autospec=True) as mock_conn:
        mock_conn.return_value = MagicMock(spec=socket.socket)
        wait_for_ssh_after_rebuild(hostname="vps-x", port=22, timeout_seconds=5.0)


def test_wait_for_ssh_times_out_when_socket_refuses() -> None:
    with patch("socket.create_connection", side_effect=OSError("ECONNREFUSED")):
        with pytest.raises(VpsProvisioningError, match="not reachable"):
            wait_for_ssh_after_rebuild(hostname="vps-x", port=22, timeout_seconds=0.05)


def _write_rsa_private_key(tmp_path: Path, key_name: str = "id_rsa") -> Path:
    """Write an RSA private key in the TraditionalOpenSSL PEM format.

    Matches what ``ssh_utils.generate_ssh_keypair`` produces for the base
    ``VpsDockerProvider``, so the regression test for Bug 4 reflects the
    real on-disk format the OVH provider receives.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_path = tmp_path / key_name
    private_path.write_bytes(pem)
    return private_path


def test_load_private_key_accepts_ed25519(tmp_path: Path) -> None:
    """Ed25519 OpenSSH-format keys must load (the original code path)."""
    key_path = _make_private_key(tmp_path)
    loaded = _load_private_key(key_path)
    assert isinstance(loaded, paramiko.Ed25519Key)


def test_load_private_key_accepts_rsa(tmp_path: Path) -> None:
    """Bug 4: RSA keys produced by ``ssh_utils.generate_ssh_keypair`` must load.

    Pre-fix, ``pin_host_key_via_tofu`` hardcoded
    ``paramiko.Ed25519Key.from_private_key_file``, which raised
    ``SSHException("encountered RSA key, expected OPENSSH key")`` against
    the RSA keys the base ``VpsDockerProvider`` actually produces.
    """
    key_path = _write_rsa_private_key(tmp_path)
    loaded = _load_private_key(key_path)
    assert isinstance(loaded, paramiko.RSAKey)


def test_load_private_key_raises_for_garbage(tmp_path: Path) -> None:
    """A non-SSH file should produce a clear ``VpsProvisioningError``."""
    bad_path = tmp_path / "not_a_key"
    bad_path.write_bytes(b"this is not an SSH private key\n")
    with pytest.raises(VpsProvisioningError, match="Could not parse SSH private key"):
        _load_private_key(bad_path)


def _stub_paramiko_exec(stdout: str = "", stderr: str = "", exit_status: int = 0) -> tuple[Any, Any, Any]:
    """Build the (stdin, stdout, stderr) triple that paramiko.exec_command returns."""
    stdin_mock = MagicMock()
    stdout_mock = MagicMock()
    stdout_mock.channel.recv_exit_status.return_value = exit_status
    stdout_mock.read.return_value = stdout.encode()
    stderr_mock = MagicMock()
    stderr_mock.read.return_value = stderr.encode()
    return stdin_mock, stdout_mock, stderr_mock


def test_bootstrap_root_runs_sudo_install_copy(tmp_path: Path) -> None:
    """Successful path runs the sudo install + copy and returns without error."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x.vps.ovh.us ssh-ed25519 AAAA\n")
    exec_commands: list[str] = []

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        exec_commands.append(command)
        return _stub_paramiko_exec(stdout="", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        bootstrap_root_authorized_keys_via_user(
            hostname="vps-x.vps.ovh.us",
            port=22,
            bootstrap_user="debian",
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
        )

    # The bootstrap command must shell out via sudo and copy to /root/.ssh.
    assert len(exec_commands) == 1
    cmd = exec_commands[0]
    assert "sudo" in cmd
    assert "/root/.ssh/authorized_keys" in cmd
    assert "~/.ssh/authorized_keys" in cmd


def test_bootstrap_root_raises_when_sudo_fails(tmp_path: Path) -> None:
    """Non-zero exit from the sudo step surfaces as VpsProvisioningError."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        return _stub_paramiko_exec(stdout="", stderr="sudo: a password is required", exit_status=1)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        with pytest.raises(VpsProvisioningError, match="copy authorized_keys"):
            bootstrap_root_authorized_keys_via_user(
                hostname="vps-x",
                port=22,
                bootstrap_user="debian",
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=5.0,
            )


def test_bootstrap_root_times_out_when_connect_fails(tmp_path: Path) -> None:
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    with (
        patch.object(
            paramiko.SSHClient,
            "connect",
            autospec=True,
            side_effect=paramiko.SSHException("auth"),
        ),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch("imbue.mngr_ovh.bootstrap._SSH_CONNECT_BACKOFF_SECONDS", 0.0),
    ):
        with pytest.raises(VpsProvisioningError, match="bootstrap root SSH"):
            bootstrap_root_authorized_keys_via_user(
                hostname="vps-x",
                port=22,
                bootstrap_user="debian",
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=0.05,
            )


def test_verify_root_ssh_smoke_test_fail_raises(tmp_path: Path) -> None:
    """A non-zero exit from the smoke-test command surfaces as VpsProvisioningError.

    Covers the case where SSH-as-root connects (key auth works) but
    something in the remote command fails -- e.g. a misconfigured shell
    or sudo policy that blocks ``whoami``.
    """
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        return _stub_paramiko_exec(stdout="not-root", stderr="", exit_status=1)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        with pytest.raises(VpsProvisioningError, match="root smoke-test"):
            verify_root_ssh(
                hostname="vps-x",
                port=22,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=5.0,
            )


def test_verify_root_ssh_returns_when_root_smoke_test_succeeds(tmp_path: Path) -> None:
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")
    seen_users: list[str | None] = []

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        seen_users.append(kwargs.get("username"))

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        return _stub_paramiko_exec(stdout="OK", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        verify_root_ssh(
            hostname="vps-x",
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
        )

    # The verification connects as root, not as the bootstrap user.
    assert seen_users == ["root"]


def test_verify_root_ssh_raises_when_root_login_fails(tmp_path: Path) -> None:
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    with (
        patch.object(
            paramiko.SSHClient,
            "connect",
            autospec=True,
            side_effect=paramiko.SSHException("publickey"),
        ),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch("imbue.mngr_ovh.bootstrap._SSH_CONNECT_BACKOFF_SECONDS", 0.0),
    ):
        with pytest.raises(VpsProvisioningError, match="post-bootstrap verification"):
            verify_root_ssh(
                hostname="vps-x",
                port=22,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=0.05,
            )


def test_install_required_outer_packages_runs_apt_install(tmp_path: Path) -> None:
    """Successful path runs ``apt-get update && apt-get install -y <pkgs>`` as root."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")
    exec_commands: list[str] = []
    seen_users: list[str | None] = []

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        seen_users.append(kwargs.get("username"))

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        exec_commands.append(command)
        return _stub_paramiko_exec(stdout="", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        install_required_outer_packages(
            hostname="vps-x",
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
            packages=("rsync",),
        )

    # Must connect as root (not the bootstrap user) because the previous
    # step already moved authorized_keys into /root/.
    assert seen_users == ["root"]
    # One exec covers update + install in a single set -e pipeline so a
    # failed update aborts before install rather than masking the cause.
    assert len(exec_commands) == 1
    cmd = exec_commands[0]
    assert "apt-get update" in cmd
    assert "apt-get install -y rsync" in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd
    assert "set -e" in cmd


def test_install_required_outer_packages_with_multiple_packages(tmp_path: Path) -> None:
    """Multiple packages land in a single apt-get install command."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")
    exec_commands: list[str] = []

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        exec_commands.append(command)
        return _stub_paramiko_exec(stdout="", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        install_required_outer_packages(
            hostname="vps-x",
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
            packages=("rsync", "build-essential"),
        )

    assert exec_commands[0].endswith("apt-get install -y rsync build-essential")


def test_install_required_outer_packages_with_empty_packages_is_noop(tmp_path: Path) -> None:
    """Empty package tuple skips SSH entirely -- nothing to install."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")
    exec_commands: list[str] = []
    connect_calls: list[Any] = []

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        connect_calls.append(kwargs)

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        exec_commands.append(command)
        return _stub_paramiko_exec(stdout="", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        install_required_outer_packages(
            hostname="vps-x",
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
            packages=(),
        )

    assert connect_calls == []
    assert exec_commands == []


def test_install_required_outer_packages_raises_when_apt_fails(tmp_path: Path) -> None:
    """A non-zero apt exit surfaces as VpsProvisioningError with the package list in the failure label."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        return _stub_paramiko_exec(stdout="", stderr="E: Unable to locate package rsync", exit_status=100)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        with pytest.raises(VpsProvisioningError, match="apt-get install rsync"):
            install_required_outer_packages(
                hostname="vps-x",
                port=22,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=5.0,
                packages=("rsync",),
            )


def test_purge_qemu_packages_runs_detect_then_purge_as_root(tmp_path: Path) -> None:
    """Successful path runs the detect-then-purge command as root."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")
    exec_commands: list[str] = []
    seen_users: list[str | None] = []

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        seen_users.append(kwargs.get("username"))

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        exec_commands.append(command)
        return _stub_paramiko_exec(stdout="", stderr="", exit_status=0)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        purge_qemu_packages(
            hostname="vps-x",
            port=22,
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
            timeout_seconds=5.0,
        )

    # The purge connects as root (authorized_keys already in /root) and gates
    # the destructive apt-get purge behind the dpkg detection probe so a
    # qemu-free image is a clean no-op rather than an apt glob error.
    assert seen_users == ["root"]
    assert len(exec_commands) == 1
    cmd = exec_commands[0]
    assert "dpkg -l | grep -q qemu" in cmd
    assert "apt-get purge --auto-remove -y 'qemu*'" in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd


def test_purge_qemu_packages_raises_when_purge_fails(tmp_path: Path) -> None:
    """A non-zero exit from the purge surfaces as VpsProvisioningError so provisioning aborts."""
    private_key_path = _make_private_key(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"
    known_hosts_path.write_text("vps-x ssh-ed25519 AAAA\n")

    def fake_exec(self: paramiko.SSHClient, command: str, **_kwargs: Any) -> Any:
        return _stub_paramiko_exec(stdout="", stderr="E: dpkg was interrupted", exit_status=100)

    with (
        patch.object(paramiko.SSHClient, "connect", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "load_host_keys", autospec=True, return_value=None),
        patch.object(paramiko.SSHClient, "exec_command", autospec=True, side_effect=fake_exec),
        patch.object(paramiko.SSHClient, "close", autospec=True, return_value=None),
    ):
        with pytest.raises(VpsProvisioningError, match="apt-get purge qemu"):
            purge_qemu_packages(
                hostname="vps-x",
                port=22,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                timeout_seconds=5.0,
            )
