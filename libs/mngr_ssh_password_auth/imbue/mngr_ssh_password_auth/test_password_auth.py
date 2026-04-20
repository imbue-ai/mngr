"""Integration tests for SSHPasswordAuth against a real paramiko SSH server.

These tests run a paramiko server in a background thread and exercise the
real SSH protocol end-to-end. Unlike the unit tests in auth_test.py which
mock paramiko entirely, these verify that SSHPasswordAuth.connect_paramiko
correctly negotiates with a real SSH server.
"""

import socket
from pathlib import Path

import paramiko
import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.ssh_auth import SSHConnectionError
from imbue.mngr_ssh_password_auth.auth import SSHPasswordAuth
from imbue.mngr_ssh_password_auth.testing import run_password_ssh_server
from imbue.mngr_ssh_password_auth.testing import write_known_hosts

_USER = "testuser"
_PASSWORD = "correct-horse-battery-staple"
_WRONG_PASSWORD = "wrong-password"


@pytest.mark.timeout(30)
def test_connect_paramiko_succeeds_with_correct_password(tmp_path: Path) -> None:
    """Real SSH protocol, correct password -> connection succeeds."""
    with run_password_ssh_server(_PASSWORD, _USER) as (port, host_key):
        known_hosts = write_known_hosts(tmp_path / "known_hosts", port, host_key)
        auth = SSHPasswordAuth(password=SecretStr(_PASSWORD), known_hosts_file=known_hosts)
        client = paramiko.SSHClient()
        try:
            auth.connect_paramiko(client, "127.0.0.1", port, _USER)
            transport = client.get_transport()
            assert transport is not None and transport.is_authenticated()
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_wraps_auth_failure_without_leaking_password(tmp_path: Path) -> None:
    """Wrong password -> SSHConnectionError. Real password must not appear in error."""
    with run_password_ssh_server(_PASSWORD, _USER) as (port, host_key):
        known_hosts = write_known_hosts(tmp_path / "known_hosts", port, host_key)
        auth = SSHPasswordAuth(password=SecretStr(_WRONG_PASSWORD), known_hosts_file=known_hosts)
        client = paramiko.SSHClient()
        try:
            with pytest.raises(SSHConnectionError) as exc_info:
                auth.connect_paramiko(client, "127.0.0.1", port, _USER)
            error_text = str(exc_info.value)
            assert _WRONG_PASSWORD not in error_text
            assert _PASSWORD not in error_text
            assert "password auth connection" in error_text
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_rejects_unknown_host_key_under_strict_mode(tmp_path: Path) -> None:
    """known_hosts file exists but does NOT contain server key -> RejectPolicy fires."""
    empty_known_hosts = tmp_path / "known_hosts"
    empty_known_hosts.write_text("")
    with run_password_ssh_server(_PASSWORD, _USER) as (port, _):
        auth = SSHPasswordAuth(password=SecretStr(_PASSWORD), known_hosts_file=empty_known_hosts)
        client = paramiko.SSHClient()
        try:
            with pytest.raises(SSHConnectionError):
                auth.connect_paramiko(client, "127.0.0.1", port, _USER)
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_accepts_any_host_when_no_known_hosts_file(tmp_path: Path) -> None:
    """No known_hosts file -> AutoAddPolicy fallback allows connection."""
    nonexistent = tmp_path / "does-not-exist"
    with run_password_ssh_server(_PASSWORD, _USER) as (port, _):
        auth = SSHPasswordAuth(password=SecretStr(_PASSWORD), known_hosts_file=nonexistent)
        client = paramiko.SSHClient()
        try:
            auth.connect_paramiko(client, "127.0.0.1", port, _USER)
            transport = client.get_transport()
            assert transport is not None and transport.is_authenticated()
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_password_never_appears_in_connection_error_traceback() -> None:
    """Connection to closed port raises SSHConnectionError without leaking password."""
    closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_sock.bind(("127.0.0.1", 0))
    closed_port = closed_sock.getsockname()[1]
    closed_sock.close()

    secret_password = "unique-secret-for-test-23fa9b"
    auth = SSHPasswordAuth(password=SecretStr(secret_password))
    client = paramiko.SSHClient()
    try:
        with pytest.raises(SSHConnectionError) as exc_info:
            auth.connect_paramiko(client, "127.0.0.1", closed_port, _USER)
        assert secret_password not in str(exc_info.value)
        cause = exc_info.value.__cause__
        assert cause is not None
        assert secret_password not in str(cause)
    finally:
        client.close()
