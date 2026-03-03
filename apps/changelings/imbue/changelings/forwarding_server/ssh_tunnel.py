import os
import select
import socket
import tempfile
import threading
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

_BUFFER_SIZE: Final[int] = 65536

_SELECT_TIMEOUT_SECONDS: Final[float] = 1.0

_ACCEPT_TIMEOUT_SECONDS: Final[float] = 1.0

_SOCKET_POLL_SECONDS: Final[float] = 0.01


class RemoteSSHInfo(FrozenModel):
    """SSH connection info for a remote agent host, parsed from mng list --json."""

    user: str = Field(description="SSH username (e.g. 'root')")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key file")


class SSHTunnelError(Exception):
    """Raised when an SSH tunnel operation fails."""

    ...


def _ssh_connection_is_active(client: paramiko.SSHClient) -> bool:
    """Check whether the SSH client's transport is active."""
    transport = client.get_transport()
    return transport is not None and transport.is_active()


def _ssh_connection_transport(client: paramiko.SSHClient) -> paramiko.Transport:
    """Get the SSH client's transport, raising if not active."""
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise SSHTunnelError("SSH transport is not active")
    return transport


class SSHTunnelManager(MutableModel):
    """Manages SSH tunnels to remote agent backends via paramiko.

    For each unique SSH host, maintains a paramiko SSHClient connection.
    For each unique (SSH host, remote endpoint) pair, creates a Unix domain
    socket in a secure temporary directory that forwards connections through
    SSH direct-tcpip channels.

    The Unix sockets are created in a temporary directory with 0o700 permissions.
    Other users cannot access the sockets, and same-user processes would need
    to discover the randomly generated directory path.
    """

    _tmpdir: tempfile.TemporaryDirectory[str] | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _connections: dict[str, paramiko.SSHClient] = PrivateAttr(default_factory=dict)
    _tunnel_socket_paths: dict[str, Path] = PrivateAttr(default_factory=dict)
    _tunnel_threads: dict[str, threading.Thread] = PrivateAttr(default_factory=dict)
    _shutdown_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def _get_tmpdir(self) -> Path:
        """Get or create the secure temporary directory for Unix sockets."""
        if self._tmpdir is None:
            self._tmpdir = tempfile.TemporaryDirectory(prefix="changeling-ssh-")
            os.chmod(self._tmpdir.name, 0o700)
        return Path(self._tmpdir.name)

    def _get_or_create_connection(self, ssh_info: RemoteSSHInfo) -> paramiko.SSHClient:
        """Get or create an SSH connection to the given host.

        Reuses existing active connections. Creates a new connection if none
        exists or the existing one has become inactive.
        """
        conn_key = f"{ssh_info.host}:{ssh_info.port}"
        existing = self._connections.get(conn_key)
        if existing is not None and _ssh_connection_is_active(existing):
            return existing

        if existing is not None:
            try:
                existing.close()
            except (OSError, paramiko.SSHException) as e:
                logger.trace("Error closing stale SSH connection: {}", e)

        logger.debug("Establishing SSH connection to {}:{}", ssh_info.host, ssh_info.port)
        client = _create_ssh_client(ssh_info)
        self._connections[conn_key] = client
        return client

    def get_tunnel_socket_path(
        self,
        ssh_info: RemoteSSHInfo,
        remote_host: str,
        remote_port: int,
    ) -> Path:
        """Get or create a Unix socket that tunnels to the given remote endpoint.

        Returns the path to a Unix domain socket. Connecting to this socket
        will forward traffic through an SSH tunnel to (remote_host, remote_port)
        on the remote host identified by ssh_info.
        """
        tunnel_key = f"{ssh_info.host}:{ssh_info.port}->{remote_host}:{remote_port}"

        with self._lock:
            existing_path = self._tunnel_socket_paths.get(tunnel_key)
            existing_thread = self._tunnel_threads.get(tunnel_key)
            if existing_path is not None and existing_thread is not None and existing_thread.is_alive():
                return existing_path

            client = self._get_or_create_connection(ssh_info)
            transport = _ssh_connection_transport(client)
            socket_path = self._get_tmpdir() / f"tunnel-{tunnel_key.replace(':', '-').replace('>', '')}.sock"

            if socket_path.exists():
                socket_path.unlink()

            thread = threading.Thread(
                target=_tunnel_accept_loop,
                args=(socket_path, transport, remote_host, remote_port, self._shutdown_event),
                daemon=True,
                name=f"ssh-tunnel-{tunnel_key}",
            )
            thread.start()

            _wait_for_socket(socket_path)

            self._tunnel_socket_paths[tunnel_key] = socket_path
            self._tunnel_threads[tunnel_key] = thread
            return socket_path

    def cleanup(self) -> None:
        """Shut down all tunnels and SSH connections."""
        self._shutdown_event.set()

        for thread in self._tunnel_threads.values():
            thread.join(timeout=5.0)

        for client in self._connections.values():
            try:
                client.close()
            except (OSError, paramiko.SSHException) as e:
                logger.trace("Error closing SSH connection during cleanup: {}", e)

        self._connections.clear()
        self._tunnel_socket_paths.clear()
        self._tunnel_threads.clear()

        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except OSError as e:
                logger.trace("Error cleaning up tunnel tmpdir: {}", e)
            self._tmpdir = None


def _create_ssh_client(ssh_info: RemoteSSHInfo) -> paramiko.SSHClient:
    """Create a paramiko SSH connection to the given host.

    Uses the known_hosts file from the same directory as the SSH key (this is
    where mng stores it for each provider). Falls back to AutoAddPolicy if
    no known_hosts file is found.
    """
    client = paramiko.SSHClient()

    known_hosts_path = ssh_info.key_path.parent / "known_hosts"
    if known_hosts_path.exists():
        client.load_host_keys(str(known_hosts_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        logger.warning("No known_hosts file at {}, using AutoAddPolicy", known_hosts_path)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=ssh_info.host,
        port=ssh_info.port,
        username=ssh_info.user,
        key_filename=str(ssh_info.key_path),
        timeout=10.0,
    )

    return client


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    """Wait for a Unix domain socket file to appear.

    Raises SSHTunnelError if the socket does not appear within the timeout.
    Uses threading.Event.wait for polling instead of time.sleep.
    """
    poll_event = threading.Event()
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not deadline.is_set():
            if socket_path.exists():
                return
            poll_event.wait(timeout=_SOCKET_POLL_SECONDS)
    finally:
        timer.cancel()
    raise SSHTunnelError(f"SSH tunnel socket did not appear within {timeout}s at {socket_path}")


def _tunnel_accept_loop(
    sock_path: Path,
    transport: paramiko.Transport,
    remote_host: str,
    remote_port: int,
    shutdown_event: threading.Event,
) -> None:
    """Accept connections on a Unix domain socket and forward them via SSH.

    For each accepted connection, opens a paramiko direct-tcpip channel to
    (remote_host, remote_port) on the remote SSH host, then relays data
    bidirectionally between the local socket and the SSH channel.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        os.chmod(str(sock_path), 0o600)
        server.listen(8)
        server.settimeout(_ACCEPT_TIMEOUT_SECONDS)

        while not shutdown_event.is_set():
            try:
                client_sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("Accept loop socket error, stopping tunnel: {}", e)
                break

            try:
                channel = transport.open_channel(
                    "direct-tcpip",
                    (remote_host, remote_port),
                    ("127.0.0.1", 0),
                )
            except (paramiko.SSHException, OSError) as e:
                logger.warning("Failed to open SSH channel to {}:{}: {}", remote_host, remote_port, e)
                client_sock.close()
                continue

            threading.Thread(
                target=_relay_data,
                args=(client_sock, channel),
                daemon=True,
                name=f"ssh-relay-{remote_host}:{remote_port}",
            ).start()
    finally:
        server.close()
        try:
            os.unlink(str(sock_path))
        except OSError as e:
            logger.trace("Error unlinking tunnel socket: {}", e)


def _relay_step(sock: socket.socket, channel: paramiko.Channel) -> bool:
    """Perform one relay step: transfer available data between sock and channel.

    Returns True to continue relaying, False when either end has closed.
    """
    r, _, _ = select.select([sock, channel], [], [], _SELECT_TIMEOUT_SECONDS)

    if sock in r:
        data = sock.recv(_BUFFER_SIZE)
        if not data:
            return False
        channel.sendall(data)

    if channel in r:
        if channel.recv_ready():
            data = channel.recv(_BUFFER_SIZE)
            if not data:
                return False
            sock.sendall(data)

    return True


def _relay_data(sock: socket.socket, channel: paramiko.Channel) -> None:
    """Relay data bidirectionally between a local socket and a paramiko channel.

    Uses select() to multiplex reads from both ends. Terminates when either
    end closes or an error occurs.
    """
    try:
        while _relay_step(sock, channel):
            pass
    except (OSError, EOFError, paramiko.SSHException) as e:
        logger.trace("SSH tunnel relay ended: {}", e)
    finally:
        try:
            channel.close()
        except (OSError, paramiko.SSHException) as e:
            logger.trace("Error closing SSH channel in relay: {}", e)
        try:
            sock.close()
        except OSError as e:
            logger.trace("Error closing socket in relay: {}", e)


def parse_url_host_port(url: str) -> tuple[str, int]:
    """Extract host and port from a URL.

    Returns (host, port) tuple. Defaults port to 80 for http:// and 443
    for https:// if not specified in the URL.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    return host, port
