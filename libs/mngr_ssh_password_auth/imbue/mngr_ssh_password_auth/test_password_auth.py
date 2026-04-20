"""Integration tests for SSHPasswordAuth against a real paramiko SSH server.

These tests run a paramiko `ServerInterface` in a background thread and exercise
the real SSH protocol end-to-end, including password authentication, host key
verification, and error handling. Unlike the unit tests in auth_test.py which
mock paramiko entirely, these tests verify that SSHPasswordAuth.connect_paramiko
correctly negotiates with a real SSH server.
"""

import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import paramiko
import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.ssh_auth import SSHConnectionError
from imbue.mngr_ssh_password_auth.auth import SSHPasswordAuth

_TEST_USERNAME = "testuser"
_TEST_PASSWORD = "correct-horse-battery-staple"
_WRONG_PASSWORD = "wrong-password"


class _PasswordAuthServer(paramiko.ServerInterface):
    """Paramiko server stub that accepts one specific username + password."""

    def __init__(self, expected_username: str, expected_password: str) -> None:
        self._expected_username = expected_username
        self._expected_password = expected_password

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self._expected_username and password == self._expected_password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


@contextmanager
def _run_ssh_server(
    expected_password: str,
    expected_username: str = _TEST_USERNAME,
) -> Generator[tuple[int, paramiko.PKey], None, None]:
    """Run a paramiko SSH server on a random localhost port.

    Yields (port, host_key). The host_key can be used to build a known_hosts
    file so the client can verify the server under RejectPolicy.
    """
    host_key = paramiko.RSAKey.generate(2048)

    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(1)
    listen_sock.settimeout(0.5)
    port = listen_sock.getsockname()[1]

    stop_event = threading.Event()
    transports: list[paramiko.Transport] = []

    def serve() -> None:
        while not stop_event.is_set():
            try:
                client_sock, _ = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client_sock)
            transports.append(transport)
            transport.add_server_key(host_key)
            server = _PasswordAuthServer(expected_username, expected_password)
            try:
                transport.start_server(server=server)
            except paramiko.SSHException:
                transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        yield port, host_key
    finally:
        stop_event.set()
        for t in transports:
            t.close()
        listen_sock.close()
        thread.join(timeout=5.0)


def _write_known_hosts(tmp_path: Path, port: int, host_key: paramiko.PKey) -> Path:
    """Write a known_hosts file with the server's public key."""
    known_hosts = tmp_path / "known_hosts"
    # Format: [127.0.0.1]:PORT keytype base64key
    entry = f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n"
    known_hosts.write_text(entry)
    return known_hosts


@pytest.mark.timeout(30)
def test_connect_paramiko_succeeds_with_correct_password(tmp_path: Path) -> None:
    """End-to-end: real SSH protocol, correct password -> connection succeeds."""
    with _run_ssh_server(_TEST_PASSWORD) as (port, host_key):
        known_hosts = _write_known_hosts(tmp_path, port, host_key)
        auth = SSHPasswordAuth(password=SecretStr(_TEST_PASSWORD), known_hosts_file=known_hosts)
        client = paramiko.SSHClient()
        try:
            auth.connect_paramiko(client, "127.0.0.1", port, _TEST_USERNAME)
            assert client.get_transport() is not None
            assert client.get_transport().is_authenticated()
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_wraps_auth_failure_without_leaking_password(tmp_path: Path) -> None:
    """Wrong password -> SSHConnectionError. Real password must not appear in error."""
    with _run_ssh_server(_TEST_PASSWORD) as (port, host_key):
        known_hosts = _write_known_hosts(tmp_path, port, host_key)
        auth = SSHPasswordAuth(password=SecretStr(_WRONG_PASSWORD), known_hosts_file=known_hosts)
        client = paramiko.SSHClient()
        try:
            with pytest.raises(SSHConnectionError) as exc_info:
                auth.connect_paramiko(client, "127.0.0.1", port, _TEST_USERNAME)
            error_text = str(exc_info.value)
            assert _WRONG_PASSWORD not in error_text
            assert _TEST_PASSWORD not in error_text
            assert "password auth connection" in error_text
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_rejects_unknown_host_key_under_strict_mode(tmp_path: Path) -> None:
    """known_hosts file exists but does NOT contain server key -> RejectPolicy fires."""
    empty_known_hosts = tmp_path / "known_hosts"
    empty_known_hosts.write_text("")
    with _run_ssh_server(_TEST_PASSWORD) as (port, _):
        auth = SSHPasswordAuth(password=SecretStr(_TEST_PASSWORD), known_hosts_file=empty_known_hosts)
        client = paramiko.SSHClient()
        try:
            with pytest.raises(SSHConnectionError):
                auth.connect_paramiko(client, "127.0.0.1", port, _TEST_USERNAME)
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_connect_paramiko_accepts_any_host_when_no_known_hosts_file(tmp_path: Path) -> None:
    """No known_hosts file -> AutoAddPolicy fallback allows connection."""
    nonexistent = tmp_path / "does-not-exist"
    with _run_ssh_server(_TEST_PASSWORD) as (port, _):
        auth = SSHPasswordAuth(password=SecretStr(_TEST_PASSWORD), known_hosts_file=nonexistent)
        client = paramiko.SSHClient()
        try:
            auth.connect_paramiko(client, "127.0.0.1", port, _TEST_USERNAME)
            assert client.get_transport() is not None
            assert client.get_transport().is_authenticated()
        finally:
            client.close()


@pytest.mark.timeout(30)
def test_password_never_appears_in_connection_error_traceback() -> None:
    """Connection to non-listening port raises SSHConnectionError without leaking password."""
    # Find a closed port by binding+closing
    closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_sock.bind(("127.0.0.1", 0))
    closed_port = closed_sock.getsockname()[1]
    closed_sock.close()

    secret_password = "unique-secret-for-test-23fa9b"
    auth = SSHPasswordAuth(password=SecretStr(secret_password))
    client = paramiko.SSHClient()
    try:
        with pytest.raises(SSHConnectionError) as exc_info:
            auth.connect_paramiko(client, "127.0.0.1", closed_port, _TEST_USERNAME)
        assert secret_password not in str(exc_info.value)
        # Also not in the chained cause's message
        cause = exc_info.value.__cause__
        assert cause is not None
        assert secret_password not in str(cause)
    finally:
        client.close()
