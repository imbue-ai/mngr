"""Test helpers for running a paramiko-based SSH server with password auth.

Used by integration tests (test_password_auth.py) to verify SSHPasswordAuth
against the real SSH protocol without needing root (for setting system
passwords) or docker (for running a real sshd). Not suitable for production.
"""

import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import paramiko


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
def run_password_ssh_server(
    expected_password: str,
    expected_username: str,
) -> Generator[tuple[int, paramiko.PKey], None, None]:
    """Run an in-process paramiko SSH server on a random localhost port.

    Yields (port, host_key). The host_key can be used with write_known_hosts
    to build a known_hosts file for strict host key verification tests.
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


def write_known_hosts(path: Path, port: int, host_key: paramiko.PKey) -> Path:
    """Write a known_hosts entry for a localhost host on the given port."""
    entry = f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n"
    path.write_text(entry)
    return path
