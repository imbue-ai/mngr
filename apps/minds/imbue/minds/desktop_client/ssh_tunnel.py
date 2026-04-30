import hashlib
import ipaddress
import os
import select
import shlex
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

_BUFFER_SIZE: Final[int] = 65536

_SELECT_TIMEOUT_SECONDS: Final[float] = 1.0

_SHUTDOWN_POLL_SECONDS: Final[float] = 0.2

_SOCKET_POLL_SECONDS: Final[float] = 0.01

_REVERSE_TUNNEL_HEALTH_CHECK_SECONDS: Final[float] = 30.0

# Maximum AF_UNIX socket path length, conservative across macOS and Linux.
# macOS sun_path is 104 bytes, Linux is 108. Python's socket.bind rejects
# paths >= sizeof(sun_path) (it wants room for a NUL terminator), so the
# usable max is 103 on macOS and 107 on Linux. We use 103 to be portable.
_MAX_AF_UNIX_PATH_LENGTH: Final[int] = 103


class RemoteSSHInfo(FrozenModel):
    """SSH connection info for a remote agent host."""

    user: str = Field(description="SSH username (e.g. 'root')")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key file")


class SSHTunnelError(Exception):
    """Raised when an SSH tunnel operation fails."""

    ...


class LoopbackWithoutTunnelError(SSHTunnelError):
    """Raised when a backend URL points at loopback but no SSH tunnel is available.

    A registered loopback URL (e.g. ``http://localhost:8000`` for a Docker
    agent's workspace_server) is only safely reachable via an SSH tunnel into
    the agent's container. Dialing it directly would hit whatever process
    happens to be bound on the host's loopback interface, which is a
    different program than the agent's. Callers must surface this as a
    user-visible error rather than falling through to a host dial.
    """

    def __init__(self, agent_id: str, backend_url: str) -> None:
        self.agent_id = agent_id
        self.backend_url = backend_url
        super().__init__(
            f"no SSH tunnel available for agent {agent_id}; "
            f"refusing to dial host loopback at {backend_url}"
        )


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


class ReverseTunnelInfo(FrozenModel):
    """Metadata for an active reverse port forward."""

    ssh_info: RemoteSSHInfo = Field(description="SSH connection info for the remote host")
    local_port: int = Field(description="Local port being forwarded to the remote host")
    remote_port: int = Field(description="Port assigned on the remote host")
    requested_remote_port: int = Field(
        default=0,
        description=(
            "Remote port originally requested from the remote sshd. ``0`` means a dynamically "
            "assigned port (the default, used by the minds API tunnel); a fixed value is used "
            "by per-agent tunnels that need a well-known port inside the container (e.g. the "
            "Latchkey gateway on ``AGENT_SIDE_LATCHKEY_PORT``). The health check re-requests "
            "this same value when re-establishing a broken tunnel."
        ),
    )
    agent_state_dirs: list[str] = Field(
        description="$MNGR_AGENT_STATE_DIR paths on the remote host for all agents sharing this tunnel"
    )


class SSHTunnelManager(MutableModel):
    """Manages SSH tunnels to remote agent backends via paramiko.

    For each unique SSH host, maintains a paramiko SSHClient connection.
    For each unique (SSH host, remote endpoint) pair, creates a Unix domain
    socket in a secure temporary directory that forwards connections through
    SSH direct-tcpip channels.

    Also supports reverse port forwarding so that remote agents can reach
    the local minds server. Reverse tunnels are health-checked periodically
    and re-established if broken.

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
    # Reverse tunnels are keyed by ``(conn_key, local_port)`` so that a single
    # SSH host can host multiple concurrent tunnels for different purposes --
    # e.g. one for the minds API (``local_port == server_port``) and one per
    # agent for the Latchkey gateway (``local_port == per_agent_gateway_port``).
    _reverse_tunnels: dict[tuple[str, int], ReverseTunnelInfo] = PrivateAttr(default_factory=dict)
    _reverse_tunnel_setup_locks: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _health_check_thread: threading.Thread | None = PrivateAttr(default=None)

    def _get_tmpdir(self) -> Path:
        """Get or create the secure temporary directory for Unix sockets.

        On macOS, $TMPDIR is a long per-user path under /var/folders/... that
        can push AF_UNIX socket paths over the 104-byte sun_path limit. We use
        /tmp directly on Darwin to keep socket paths short. The directory is
        chmodded to 0o700 and contains only 0o600 sockets, so sharing /tmp with
        other users on the machine is safe.
        """
        if self._tmpdir is None:
            base_dir = "/tmp" if sys.platform == "darwin" else None
            self._tmpdir = tempfile.TemporaryDirectory(prefix="minds-ssh-", dir=base_dir)
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
            # Use a short hash of tunnel_key for the filename. Encoding the full
            # tunnel_key produces paths that can exceed AF_UNIX's 104-byte
            # sun_path limit on macOS, especially with long hostnames or IPv6
            # addresses. 12 hex chars (48 bits) is ample to avoid collisions
            # between tunnels within a single manager instance.
            tunnel_id = hashlib.blake2b(tunnel_key.encode(), digest_size=6).hexdigest()
            socket_path = self._get_tmpdir() / f"t-{tunnel_id}.sock"

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

    def _get_reverse_tunnel_setup_lock(self, conn_key: str) -> threading.Lock:
        """Get or create a per-host setup lock for reverse tunnels."""
        with self._lock:
            if conn_key not in self._reverse_tunnel_setup_locks:
                self._reverse_tunnel_setup_locks[conn_key] = threading.Lock()
            return self._reverse_tunnel_setup_locks[conn_key]

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        agent_state_dir: str | None = None,
        remote_port: int = 0,
    ) -> int:
        """Set up a reverse port forward so the remote host can reach the local server.

        Asks the remote sshd to listen on ``remote_port`` (0 = dynamically
        assigned, the default) and forward connections back to
        ``127.0.0.1:local_port`` on the local machine. Returns the port the
        remote sshd actually bound (equal to ``remote_port`` when it is
        non-zero, or the dynamically assigned port when it is 0).

        Pass ``agent_state_dir`` for tunnels whose remote URL should be written
        into a per-agent state directory (minds API); leave it as ``None`` for
        tunnels that deliver their endpoint via a constant URL injected at
        ``mngr create`` time (Latchkey gateway).

        Reuses an existing tunnel identified by the ``(conn_key, local_port)``
        key so that multiple callers targeting the same local service share a
        single tunnel. Different ``local_port``s on the same SSH host produce
        independent tunnels.

        Concurrent calls for the same host are serialized via a per-host lock to
        prevent establishing duplicate reverse tunnels.
        """
        conn_key = f"{ssh_info.host}:{ssh_info.port}"
        tunnel_key = (conn_key, local_port)
        host_lock = self._get_reverse_tunnel_setup_lock(conn_key)

        with host_lock:
            with self._lock:
                # Check if a reverse tunnel already exists for this (host, local_port)
                existing = self._reverse_tunnels.get(tunnel_key)
                if existing is not None:
                    # Verify the transport is still alive
                    client = self._connections.get(conn_key)
                    if client is not None and _ssh_connection_is_active(client):
                        # Register this agent's state dir if not already tracked
                        if agent_state_dir is not None and agent_state_dir not in existing.agent_state_dirs:
                            self._reverse_tunnels[tunnel_key] = existing.model_copy_update(
                                to_update(
                                    existing.field_ref().agent_state_dirs,
                                    existing.agent_state_dirs + [agent_state_dir],
                                )
                            )
                        return existing.remote_port

                client = self._get_or_create_connection(ssh_info)
                transport = _ssh_connection_transport(client)

            # Register a per-forward handler so paramiko dispatches each
            # inbound channel to the correct local port, preserving the
            # ``(server_addr, server_port)`` routing info. The default
            # ``handler=None`` path puts every channel on a single transport-
            # wide queue keyed only by arrival order, which silently cross-
            # routes connections when multiple reverse tunnels share one
            # transport (e.g. the minds API tunnel and a Latchkey gateway
            # tunnel to the same agent host).
            handler = _ForwardedTunnelHandler(local_port=local_port, shutdown_event=self._shutdown_event)
            assigned_remote_port = transport.request_port_forward("127.0.0.1", remote_port, handler=handler)
            logger.info(
                "Reverse tunnel established: remote 127.0.0.1:{} -> local 127.0.0.1:{}",
                assigned_remote_port,
                local_port,
            )

            tunnel_info = ReverseTunnelInfo(
                ssh_info=ssh_info,
                local_port=local_port,
                remote_port=assigned_remote_port,
                requested_remote_port=remote_port,
                agent_state_dirs=[agent_state_dir] if agent_state_dir is not None else [],
            )
            with self._lock:
                self._reverse_tunnels[tunnel_key] = tunnel_info

            return assigned_remote_port

    def write_api_url_to_remote(
        self,
        ssh_info: RemoteSSHInfo,
        agent_state_dir: str,
        url: str,
    ) -> None:
        """Write the minds API URL to a file on the remote host via SSH."""
        with self._lock:
            client = self._get_or_create_connection(ssh_info)

        shell_dir = _shell_quote_remote_path(agent_state_dir)
        quoted_url = shlex.quote(url)
        command = f"mkdir -p {shell_dir} && printf '%s' {quoted_url} > {shell_dir}/minds_api_url"
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=10.0)
            _stdin.close()
            try:
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    error_output = stderr.read().decode().strip()
                    logger.warning(
                        "Failed to write API URL to remote {}: exit={}, stderr={}",
                        ssh_info.host,
                        exit_status,
                        error_output,
                    )
            finally:
                stdout.channel.close()
                stdout.close()
                stderr.close()
        except (paramiko.SSHException, OSError) as e:
            logger.warning("Failed to write API URL to remote {}: {}", ssh_info.host, e)

    @staticmethod
    def write_api_url_to_local(
        agent_state_dir: Path,
        url: str,
    ) -> None:
        """Write the minds API URL to a file on the local filesystem."""
        agent_state_dir.mkdir(parents=True, exist_ok=True)
        url_file = agent_state_dir / "minds_api_url"
        url_file.write_text(url)

    def start_reverse_tunnel_health_check(self) -> None:
        """Start a background thread that checks reverse tunnels every 30 seconds."""
        if self._health_check_thread is not None:
            return
        self._health_check_thread = threading.Thread(
            target=self._reverse_tunnel_health_check_loop,
            daemon=True,
            name="reverse-tunnel-health-check",
        )
        self._health_check_thread.start()

    def _check_and_repair_tunnels(self) -> None:
        """Check all reverse tunnels and re-establish any that are broken.

        Called once per health-check iteration. Broken tunnels are re-established
        with the same originally-requested remote port, and URL files on the
        remote hosts are updated with the new remote port for tunnels that
        track agent state dirs.
        """
        with self._lock:
            tunnels = dict(self._reverse_tunnels)

        for tunnel_key, tunnel_info in tunnels.items():
            conn_key, _local_port = tunnel_key
            with self._lock:
                client = self._connections.get(conn_key)

            is_alive = client is not None and _ssh_connection_is_active(client)
            if is_alive:
                continue

            logger.info(
                "Reverse tunnel to {} (local {}) is broken, re-establishing...",
                conn_key,
                tunnel_info.local_port,
            )
            try:
                first_dir: str | None = tunnel_info.agent_state_dirs[0] if tunnel_info.agent_state_dirs else None
                new_remote_port = self.setup_reverse_tunnel(
                    ssh_info=tunnel_info.ssh_info,
                    local_port=tunnel_info.local_port,
                    agent_state_dir=first_dir,
                    remote_port=tunnel_info.requested_remote_port,
                )
                # Re-register remaining agent state dirs so they are tracked
                # in the new tunnel's ReverseTunnelInfo (setup_reverse_tunnel
                # appends dirs to an existing active tunnel without creating a new one).
                for extra_dir in tunnel_info.agent_state_dirs[1:]:
                    self.setup_reverse_tunnel(
                        ssh_info=tunnel_info.ssh_info,
                        local_port=tunnel_info.local_port,
                        agent_state_dir=extra_dir,
                        remote_port=tunnel_info.requested_remote_port,
                    )
                # Update the URL file for all agents sharing this tunnel (no-op
                # for tunnels with no tracked dirs, e.g. the Latchkey gateway).
                new_url = f"http://127.0.0.1:{new_remote_port}"
                for agent_state_dir in tunnel_info.agent_state_dirs:
                    self.write_api_url_to_remote(
                        ssh_info=tunnel_info.ssh_info,
                        agent_state_dir=agent_state_dir,
                        url=new_url,
                    )
                logger.info(
                    "Reverse tunnel re-established to {} (local {}) on remote port {}",
                    conn_key,
                    tunnel_info.local_port,
                    new_remote_port,
                )
            except (paramiko.SSHException, OSError, SSHTunnelError) as e:
                logger.warning(
                    "Failed to re-establish reverse tunnel to {} (local {}): {}",
                    conn_key,
                    tunnel_info.local_port,
                    e,
                )

    def _reverse_tunnel_health_check_loop(self) -> None:
        """Periodically check reverse tunnels and re-establish broken ones."""
        while not self._shutdown_event.wait(timeout=_REVERSE_TUNNEL_HEALTH_CHECK_SECONDS):
            self._check_and_repair_tunnels()

    def cleanup(self) -> None:
        """Shut down all tunnels (forward and reverse) and SSH connections."""
        self._shutdown_event.set()

        # Wait for health check thread
        if self._health_check_thread is not None:
            self._health_check_thread.join(timeout=5.0)
            self._health_check_thread = None

        for thread in self._tunnel_threads.values():
            thread.join(timeout=5.0)

        # Cancel reverse port forwards
        for tunnel_key, tunnel_info in self._reverse_tunnels.items():
            conn_key, _local_port = tunnel_key
            client = self._connections.get(conn_key)
            if client is not None and _ssh_connection_is_active(client):
                try:
                    transport = client.get_transport()
                    if transport is not None:
                        transport.cancel_port_forward("127.0.0.1", tunnel_info.remote_port)
                except (paramiko.SSHException, OSError) as e:
                    logger.trace("Error cancelling reverse port forward: {}", e)
        self._reverse_tunnels.clear()

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
    where mngr stores it for each provider). Falls back to AutoAddPolicy if
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
        server.settimeout(_SHUTDOWN_POLL_SECONDS)

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
                if not transport.is_active():
                    logger.warning("SSH transport is dead, stopping tunnel accept loop")
                    break
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


class _ForwardedTunnelHandler(FrozenModel):
    """Per-forward port-forward handler that relays inbound channels to ``127.0.0.1:local_port``.

    Registered with paramiko via ``Transport.request_port_forward(..., handler=self)``.
    Paramiko invokes ``__call__`` on its own dispatch thread for every inbound
    connection to the specific reverse-forwarded port this handler is registered
    against. Using a per-forward handler is load-bearing: it is the only way
    paramiko preserves the ``(server_addr, server_port)`` routing info. The
    default queue-based ``Transport.accept()`` path discards that info, which
    silently cross-routes connections when multiple reverse tunnels share a
    single transport.

    Keeping ``__call__`` short is important: paramiko runs it on the transport's
    internal dispatch thread, so any slow work would back up the transport.
    We only do a non-blocking local ``connect()`` against loopback and hand
    off to a dedicated relay thread.
    """

    # ``threading.Event`` is not pydantic-native; opt into arbitrary types for
    # this handler specifically. The parent ``FrozenModel`` disallows them by
    # default.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    local_port: int = Field(description="127.0.0.1 TCP port inbound channels are relayed to")
    shutdown_event: threading.Event = Field(
        description="Shared shutdown flag; when set, newly arrived channels are closed without relaying"
    )

    def __call__(
        self,
        channel: paramiko.Channel,
        _origin_addr: tuple[str, int],
        _server_addr: tuple[str, int],
    ) -> None:
        if self.shutdown_event.is_set():
            try:
                channel.close()
            except (paramiko.SSHException, OSError) as e:
                logger.trace("Error closing channel during shutdown: {}", e)
            return
        local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            local_sock.connect(("127.0.0.1", self.local_port))
        except OSError as e:
            logger.warning("Failed to connect to local port {} for reverse tunnel: {}", self.local_port, e)
            local_sock.close()
            try:
                channel.close()
            except (paramiko.SSHException, OSError) as close_err:
                logger.trace("Error closing channel after failed local connect: {}", close_err)
            return
        threading.Thread(
            target=_relay_data,
            args=(local_sock, channel),
            daemon=True,
            name=f"reverse-relay-127.0.0.1:{self.local_port}",
        ).start()


def _shell_quote_remote_path(path: str) -> str:
    """Produce a shell-safe argument for a remote path, preserving tilde expansion.

    shlex.quote wraps strings in single quotes, which prevents tilde expansion on
    the remote shell. Paths starting with '~/' are rewritten to use '$HOME/'
    in a double-quoted string so the remote shell expands the variable correctly.
    The remainder of the path after '~/' is the agent ID (UUID format: alphanumeric
    and hyphens), which is safe to embed in a double-quoted shell string.
    """
    if path == "~" or path.startswith("~/"):
        rest = path[1:]
        return f'"$HOME{rest}"'
    return shlex.quote(path)


def parse_url_host_port(url: str) -> tuple[str, int]:
    """Extract host and port from a URL.

    Returns (host, port) tuple. Defaults port to 80 for http:// and 443
    for https:// if not specified in the URL.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    # Normalize localhost to 127.0.0.1 to avoid IPv6 resolution issues.
    # SSH channels don't do dual-stack fallback like curl, so if the remote
    # resolves localhost to ::1 but the server only listens on 127.0.0.1,
    # the channel open fails.
    if host == "localhost":
        host = "127.0.0.1"
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    return host, port


def is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is a loopback address (``127.0.0.0/8`` or ``::1``).

    Used by the desktop client's proxy to decide whether a registered backend
    URL is only safely reachable through an SSH tunnel: a loopback URL points
    at the agent container's interface, not the host's, so dialing it
    directly from the host would hit an unrelated process. Hostnames that
    are not IP literals (``example.com``, ``ws-backend``, etc.) are not
    loopback even if they happen to resolve there in some environments --
    only literal loopback URLs are gated.
    """
    host, _ = parse_url_host_port(url)
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
