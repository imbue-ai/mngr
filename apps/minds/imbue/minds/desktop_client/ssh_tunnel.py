import select
import socket
import threading
import time
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

# Per-tunnel backoff for the health-check repair loop. After each failed
# repair attempt the next attempt is scheduled at ``min(2 ** failures, cap)``
# seconds in the future; after ``_REVERSE_TUNNEL_MAX_REPAIR_FAILURES``
# consecutive failures the tunnel is dropped entirely so the manager does
# not keep paying for new SSH handshakes against a permanently-gone target.
_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS: Final[float] = 300.0
_REVERSE_TUNNEL_MAX_REPAIR_FAILURES: Final[int] = 10


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
    agent_id: str | None = Field(
        default=None,
        description=(
            "Stringified ID of the agent that owns this tunnel, when known. Tagged by the caller "
            "of ``setup_reverse_tunnel`` (currently ``LatchkeyDiscoveryHandler``) and read by "
            "``remove_reverse_tunnels_for_agent`` (currently invoked from "
            "``LatchkeyDestructionHandler``) so all tunnels belonging to a destroyed agent can be "
            "torn down together. ``None`` when the caller does not associate the tunnel with a "
            "specific agent."
        ),
    )


class _TunnelFailureState(MutableModel):
    """Per-tunnel backoff bookkeeping for the health-check repair loop.

    Held by ``SSHTunnelManager._failure_state`` keyed by the same
    ``(conn_key, local_port)`` tuple as ``_reverse_tunnels``. Tunnels with
    ``consecutive_failures == 0`` (the steady-state "healthy" case) need not
    appear here at all; entries are created on first failure and removed on
    successful repair or when the tunnel itself is dropped.
    """

    consecutive_failures: int = Field(
        default=0,
        description="Number of consecutive failed repair attempts since the last success",
    )
    next_attempt_at: float = Field(
        default=0.0,
        description=(
            "Earliest ``time.monotonic()`` value at which the health check should retry this "
            "tunnel. Tunnels whose ``next_attempt_at`` is in the future are skipped this tick."
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
    # Failure bookkeeping for the per-tunnel exponential backoff used by the
    # health-check loop. Created lazily on first failure for a given tunnel
    # key and removed on success or when the tunnel itself is dropped.
    _failure_state: dict[tuple[str, int], _TunnelFailureState] = PrivateAttr(default_factory=dict)

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
        agent_id: str | None = None,
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

        ``agent_id`` (optional) tags the resulting ``ReverseTunnelInfo`` with the
        owning agent's stringified ID so callers can later ask the manager to
        tear down all tunnels belonging to a destroyed agent via
        ``remove_reverse_tunnels_for_agent``. Pass ``None`` (the default) when
        the tunnel is not associated with a specific agent.
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
                agent_id=agent_id,
            )
            with self._lock:
                self._reverse_tunnels[tunnel_key] = tunnel_info
                # Successful setup clears any prior failure bookkeeping so the
                # next health-check tick treats the tunnel as healthy.
                self._failure_state.pop(tunnel_key, None)

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

        Failed repair attempts back off exponentially (per tunnel, capped at
        ``_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS``) so the manager does not pay
        for a fresh paramiko handshake against a permanently-gone target on
        every 30s tick. After ``_REVERSE_TUNNEL_MAX_REPAIR_FAILURES`` straight
        failures the tunnel is dropped entirely; a successful repair clears
        the backoff state.
        """
        with self._lock:
            tunnels = dict(self._reverse_tunnels)

        now = time.monotonic()
        for tunnel_key, tunnel_info in tunnels.items():
            conn_key, _local_port = tunnel_key
            with self._lock:
                client = self._connections.get(conn_key)
                failure_state = self._failure_state.get(tunnel_key)

            is_alive = client is not None and _ssh_connection_is_active(client)
            if is_alive:
                # Underlying SSH connection is alive again. The most likely
                # path here is that a *sibling* tunnel sharing the same
                # conn_key got repaired in this very loop (or earlier),
                # which recreated the SSH client. ``setup_reverse_tunnel``
                # clears failure_state only for the specific tunnel_key it
                # just set up, so siblings observing is_alive=True would
                # otherwise carry stale failure_state into the next break
                # and could hit ``_REVERSE_TUNNEL_MAX_REPAIR_FAILURES``
                # prematurely. Drop any lingering bookkeeping so this
                # tunnel's next failure starts a fresh schedule.
                if failure_state is not None:
                    with self._lock:
                        self._failure_state.pop(tunnel_key, None)
                continue

            if failure_state is not None and failure_state.next_attempt_at > now:
                # Still inside the backoff window from the last failure;
                # skip this tick to avoid hammering a dead target.
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
                    agent_id=tunnel_info.agent_id,
                )
                logger.info(
                    "Reverse tunnel re-established to {} (local {}) on remote port {}",
                    conn_key,
                    tunnel_info.local_port,
                    new_remote_port,
                )
            except (paramiko.SSHException, OSError, SSHTunnelError) as e:
                self._record_repair_failure(tunnel_key, conn_key, tunnel_info, e)

    def _record_repair_failure(
        self,
        tunnel_key: tuple[str, int],
        conn_key: str,
        tunnel_info: ReverseTunnelInfo,
        error: Exception,
    ) -> None:
        """Update backoff bookkeeping after a failed repair, dropping the tunnel after N strikes.

        Split out of ``_check_and_repair_tunnels`` for readability; the only
        caller is the ``except`` arm of the repair loop, which catches
        ``paramiko.SSHException``, ``OSError``, and our own ``SSHTunnelError``.
        """
        with self._lock:
            failure_state = self._failure_state.get(tunnel_key)
            if failure_state is None:
                failure_state = _TunnelFailureState()
                self._failure_state[tunnel_key] = failure_state
            failure_state.consecutive_failures += 1
            backoff_seconds = min(
                float(2**failure_state.consecutive_failures),
                _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS,
            )
            failure_state.next_attempt_at = time.monotonic() + backoff_seconds
            failures = failure_state.consecutive_failures

        logger.warning(
            "Failed to re-establish reverse tunnel to {} (local {}): {} (failure {}/{}, backoff {:.0f}s)",
            conn_key,
            tunnel_info.local_port,
            error,
            failures,
            _REVERSE_TUNNEL_MAX_REPAIR_FAILURES,
            backoff_seconds,
        )

        if failures >= _REVERSE_TUNNEL_MAX_REPAIR_FAILURES:
            logger.warning(
                "Dropping reverse tunnel to {} (local {}) after {} consecutive "
                "failed repair attempts; further repair attempts would just keep "
                "spinning a paramiko transport against a gone target.",
                conn_key,
                tunnel_info.local_port,
                failures,
            )
            self._drop_tunnel_keys((tunnel_key,))

    def _reverse_tunnel_health_check_loop(self) -> None:
        """Periodically check reverse tunnels and re-establish broken ones."""
        while not self._shutdown_event.wait(timeout=_REVERSE_TUNNEL_HEALTH_CHECK_SECONDS):
            self._check_and_repair_tunnels()

    def remove_reverse_tunnels_for_agent(self, agent_id: str) -> int:
        """Tear down every reverse tunnel associated with ``agent_id``.

        Cancels each matching port forward on the underlying SSH transport,
        drops the tunnel from the registry, and clears any backoff
        bookkeeping. If the SSH client for a given host has no remaining
        tunnels after removal, that client is closed too -- otherwise its
        paramiko transport thread would keep polling forever even though no
        live tunnel uses it.

        Returns the number of tunnels removed. Safe to call when no tunnel
        for ``agent_id`` exists (returns ``0``).
        """
        with self._lock:
            keys = [tunnel_key for tunnel_key, info in self._reverse_tunnels.items() if info.agent_id == agent_id]
        return self._drop_tunnel_keys(tuple(keys))

    def _drop_tunnel_keys(self, tunnel_keys: tuple[tuple[str, int], ...]) -> int:
        """Internal shared cleanup used by ``remove_reverse_tunnels_for_agent`` and the backoff drop path.

        For each ``(conn_key, local_port)`` in ``tunnel_keys``: cancel its
        reverse port forward (best-effort -- the transport may already be
        dead), drop it from ``_reverse_tunnels``, drop its backoff entry.
        For each conn_key whose last tunnel is being removed, close and
        forget the SSH client so its transport thread exits.
        """
        if not tunnel_keys:
            return 0

        with self._lock:
            removed_infos: list[tuple[tuple[str, int], ReverseTunnelInfo]] = []
            for tunnel_key in tunnel_keys:
                info = self._reverse_tunnels.pop(tunnel_key, None)
                self._failure_state.pop(tunnel_key, None)
                if info is not None:
                    removed_infos.append((tunnel_key, info))
            # A conn_key whose every remaining tunnel was just removed has
            # no further use for its SSH client; pop it now so we can close
            # it outside the lock.
            affected_conn_keys = {tunnel_key[0] for tunnel_key, _ in removed_infos}
            still_in_use_conn_keys = {tunnel_key[0] for tunnel_key in self._reverse_tunnels}
            orphaned_conn_keys = affected_conn_keys - still_in_use_conn_keys
            orphaned_clients: dict[str, paramiko.SSHClient] = {}
            for conn_key in orphaned_conn_keys:
                client = self._connections.pop(conn_key, None)
                if client is not None:
                    orphaned_clients[conn_key] = client
            # Snapshot remaining clients for shared-host tunnel cancellation
            # so we don't reach back into ``_connections`` after dropping the
            # lock (another thread could mutate the dict in the meantime).
            remaining_clients: dict[str, paramiko.SSHClient] = {
                conn_key: client for conn_key, client in self._connections.items() if conn_key in affected_conn_keys
            }

        for tunnel_key, info in removed_infos:
            conn_key, _local_port = tunnel_key
            client = orphaned_clients.get(conn_key) or remaining_clients.get(conn_key)
            # Best-effort cancel -- if the transport is already dead this is
            # a no-op. We still want to ask paramiko nicely first so an alive
            # remote sshd actually frees the bound port.
            if client is not None:
                try:
                    transport = client.get_transport()
                    if transport is not None and transport.is_active():
                        transport.cancel_port_forward("127.0.0.1", info.remote_port)
                except (paramiko.SSHException, OSError) as e:
                    logger.trace("Error cancelling reverse port forward during removal: {}", e)

        for client in orphaned_clients.values():
            try:
                client.close()
            except (OSError, paramiko.SSHException) as e:
                logger.trace("Error closing orphaned SSH connection during removal: {}", e)

        return len(removed_infos)

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
        self._failure_state.clear()

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
