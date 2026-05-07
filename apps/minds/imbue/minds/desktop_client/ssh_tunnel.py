import select
import socket
import threading
from pathlib import Path
from typing import Final

import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

_BUFFER_SIZE: Final[int] = 65536

_SELECT_TIMEOUT_SECONDS: Final[float] = 1.0

_REVERSE_TUNNEL_HEALTH_CHECK_SECONDS: Final[float] = 30.0


class RemoteSSHInfo(FrozenModel):
    """SSH connection info for a remote agent host."""

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


class ReverseTunnelInfo(FrozenModel):
    """Metadata for an active reverse port forward."""

    ssh_info: RemoteSSHInfo = Field(description="SSH connection info for the remote host")
    local_port: int = Field(description="Local port being forwarded to the remote host")
    remote_port: int = Field(description="Port assigned on the remote host")
    requested_remote_port: int = Field(
        default=0,
        description=(
            "Remote port originally requested from the remote sshd. The Latchkey gateway uses "
            "``AGENT_SIDE_LATCHKEY_PORT`` (a fixed value) so the in-container env var "
            "``LATCHKEY_GATEWAY=http://127.0.0.1:<fixed-port>`` keeps working across tunnel "
            "re-establishments. The health check re-requests this same value when re-establishing "
            "a broken tunnel."
        ),
    )


class SSHTunnelManager(MutableModel):
    """Manages SSH reverse-port-forward tunnels for the surviving Latchkey path.

    For each unique SSH host, maintains a paramiko SSHClient connection.
    Reverse port forwards are keyed by ``(conn_key, local_port)`` so a single
    SSH host can carry multiple concurrent tunnels (e.g. per-agent Latchkey
    gateways on a fixed in-container port). Reverse tunnels are health-checked
    every ~30s and re-established if broken.

    Forward (direct-tcpip) tunnels used to live here too -- those moved to
    the ``mngr_forward`` plugin's own SSHTunnelManager in Phase 2 and are no
    longer needed in minds.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _connections: dict[str, paramiko.SSHClient] = PrivateAttr(default_factory=dict)
    _shutdown_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _reverse_tunnels: dict[tuple[str, int], ReverseTunnelInfo] = PrivateAttr(default_factory=dict)
    _reverse_tunnel_setup_locks: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _health_check_thread: threading.Thread | None = PrivateAttr(default=None)

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
        remote_port: int = 0,
    ) -> int:
        """Set up a reverse port forward so the remote host can reach the local server.

        Asks the remote sshd to listen on ``remote_port`` (0 = dynamically
        assigned, the default) and forward connections back to
        ``127.0.0.1:local_port`` on the local machine. Returns the port the
        remote sshd actually bound (equal to ``remote_port`` when it is
        non-zero, or the dynamically assigned port when it is 0).

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
                        return existing.remote_port

                client = self._get_or_create_connection(ssh_info)
                transport = _ssh_connection_transport(client)

            # Register a per-forward handler so paramiko dispatches each
            # inbound channel to the correct local port, preserving the
            # ``(server_addr, server_port)`` routing info. The default
            # ``handler=None`` path puts every channel on a single transport-
            # wide queue keyed only by arrival order, which silently cross-
            # routes connections when multiple reverse tunnels share one
            # transport (e.g. multiple Latchkey gateway tunnels to the same
            # agent host).
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
            )
            with self._lock:
                self._reverse_tunnels[tunnel_key] = tunnel_info

            return assigned_remote_port

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
        with the same originally-requested remote port (so the in-container env
        var that names the gateway URL keeps pointing at a working endpoint).
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
                new_remote_port = self.setup_reverse_tunnel(
                    ssh_info=tunnel_info.ssh_info,
                    local_port=tunnel_info.local_port,
                    remote_port=tunnel_info.requested_remote_port,
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
        """Cancel all reverse port forwards and close SSH connections."""
        self._shutdown_event.set()

        # Wait for health check thread
        if self._health_check_thread is not None:
            self._health_check_thread.join(timeout=5.0)
            self._health_check_thread = None

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


def open_ssh_client(ssh_info: RemoteSSHInfo) -> paramiko.SSHClient:
    """Open a paramiko SSH client to the given host using the cached known_hosts.

    Public wrapper around the internal ``_create_ssh_client`` helper. Used
    by ``forward_cli.MindsApiUrlWriter`` to write ``minds_api_url`` on
    remote agent hosts without depending on a private symbol.
    """
    return _create_ssh_client(ssh_info)


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
