"""Tests for the OVH TOFU host-key pinning helpers."""

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from imbue.mngr_ovh.bootstrap import pin_host_key_via_tofu
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

    def fake_connect(self: paramiko.SSHClient, **kwargs: Any) -> None:
        policy = self._policy  # noqa: SLF001 -- paramiko's internal but stable
        policy.missing_host_key(self, kwargs["hostname"], fake_server_key)

    with patch.object(paramiko.SSHClient, "connect", autospec=True, side_effect=fake_connect):
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
        with patch("imbue.mngr_ovh.bootstrap._TOFU_CONNECT_BACKOFF_SECONDS", 0.0):
            with pytest.raises(VpsProvisioningError, match="Could not SSH"):
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
