import hashlib
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_forward.relay import relay_data

_SHUTDOWN_POLL_SECONDS: Final[float] = 0.2

# Pending-connection backlog on each tunnel's Unix socket. Connections beyond
# this are refused, which a caller reads as an unreachable backend, so it needs
# headroom over the parallel requests a single page load makes.
_TUNNEL_LISTEN_BACKLOG: Final[int] = 8

_REVERSE_TUNNEL_HEALTH_CHECK_SECONDS: Final[float] = 30.0

# Per-tunnel backoff for the health-check repair loop. After each failed
# repair attempt the next attempt is scheduled at ``min(2 ** failures, cap)``
# seconds in the future. The retry continues forever (with the per-tunnel
# wait saturating at the 5-minute backoff ceiling) so a tunnel whose
# target is temporarily unreachable -- e.g. the user's laptop went offline
# overnight -- still recovers when the target comes back, instead of being
# permanently dropped.
_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS: Final[float] = 300.0

# Maximum AF_UNIX socket path length, conservative across macOS and Linux.
# macOS sun_path is 104 bytes, Linux is 108. Python's socket.bind rejects
# paths >= sizeof(sun_path) (it wants room for a NUL terminator), so the
# usable max is 103 on macOS and 107 on Linux. We use 103 to be portable.
_MAX_AF_UNIX_PATH_LENGTH: Final[int] = 103

# Interval (seconds) for SSH-level keepalives on every tunnel connection.
# Without keepalives an idle reverse tunnel can silently half-die: paramiko
# keeps the transport marked "active" and the remote sshd keeps the forwarded
# listener bound, so the remote port is orphaned across restarts (the next
# run's ``request_port_forward`` is then denied). Periodic keepalives keep the
# connection from idling out. Kept below the 30s health-check interval.
#
# They do NOT detect a dead peer, despite the name: ``set_keepalive`` never
# waits for a reply, so a peer that vanished mid-connection leaves the transport
# reporting ``is_active() == True`` for minutes. Detecting that is the job of
# the bounded channel open in ``_open_and_relay``.
_SSH_KEEPALIVE_INTERVAL_SECONDS: Final[int] = 15

# How long to wait for sshd's reply to a ``direct-tcpip`` channel open before
# calling the peer gone. paramiko defaults this to 3600s, so an open against a
# peer that silently went away waits an hour -- long enough that the tunnel is
# effectively dead for the rest of the session.
#
# Set to the proxy's own request timeout (``server._PROXY_TIMEOUT_SECONDS``):
# beyond that the request this open serves has already been abandoned, so
# waiting longer buys nothing and holds that request's pooled connection while
# it does. Deliberately no tighter, because hitting it closes the whole SSH
# connection out from under every other channel on it -- a merely loaded sshd
# must not pay that price.
#
# It bounds the wait, NOT the whole open: ``Transport.open_channel`` sends its
# request before starting the clock, and that send can block independently. A
# link that black-holes without ever erroring can therefore still stall an open
# past this.
_CHANNEL_OPEN_TIMEOUT_SECONDS: Final[float] = 30.0

# Threshold for reporting a channel open that did succeed. One open is a single
# round trip to sshd, so anything beyond this means the link or the remote sshd
# is degrading. This is the only trace of a link slow enough to ruin the page
# but never slow enough to trip the bound above.
#
# Reported at debug, not warning: the loading page re-polls once a second and
# each poll opens its own channel, so a degraded link would otherwise warn every
# second for as long as the page is open, all of it restating one fact.
_CHANNEL_OPEN_SLOW_THRESHOLD_SECONDS: Final[float] = 5.0


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
            "Remote port originally requested from the remote sshd. ``0`` means a dynamically "
            "assigned port (the default for the plugin's ``--reverse 0:<local>`` flag); a fixed "
            "value (e.g. ``AGENT_SIDE_LATCHKEY_PORT`` for the Latchkey gateway) is used when the "
            "caller wants a well-known port inside the container so an in-container env var that "
            "names the URL keeps working across tunnel re-establishments. The health check "
            "re-requests this same value when re-establishing a broken tunnel."
        ),
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Stringified ID of the agent that owns this tunnel, when known. Tagged by the "
            "caller of ``setup_reverse_tunnel`` (currently the Latchkey discovery handler) and "
            "read by ``remove_reverse_tunnels_for_agent`` so all tunnels belonging to a "
            "destroyed agent can be torn down together. ``None`` when the caller does not "
            "associate the tunnel with a specific agent (e.g. bare ``--reverse <r>:<l>`` "
            "specs from the plugin CLI)."
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
    """Manages SSH tunnels to remote agent backends via paramiko.

    Two flavours of tunnel are supported on the same manager:

    * **Forward (direct-tcpip)**: for each unique (SSH host, remote endpoint)
      pair, a Unix domain socket in a secure temporary directory forwards
      connections through an SSH direct-tcpip channel. Used by the
      ``mngr forward`` plugin's per-service forwarding so subdomain requests
      hit the right backend inside the remote agent. Created on demand by
      :meth:`get_tunnel_socket_path`.
    * **Reverse**: ``Transport.request_port_forward("127.0.0.1", remote_port)``
      asks the remote sshd to listen on a port and tunnel connections back
      to a local TCP port. Used by the ``mngr forward`` plugin's
      ``--reverse <remote>:<local>`` flag and by ``mngr latchkey forward`` to
      reverse-tunnel the host-side Latchkey gateway into every discovered
      agent. Created by :meth:`setup_reverse_tunnel` and kept healthy by an
      optional background thread started via
      :meth:`start_reverse_tunnel_health_check`.

    Reverse tunnels are keyed by ``(conn_key, local_port)`` so a single SSH
    host can host multiple concurrent tunnels for different purposes -- e.g.
    one for ``--reverse 8420:8420`` and one per agent for the Latchkey
    gateway. Optional :attr:`ReverseTunnelInfo.agent_id` tags let the
    Latchkey destruction path tear down all tunnels belonging to a
    destroyed agent via :meth:`remove_reverse_tunnels_for_agent`.

    The repair loop uses a per-tunnel exponential backoff (capped at
    ``_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS``) so a tunnel whose target is
    temporarily unreachable still recovers when the target comes back
    without hammering it on every 30s tick. Each successful repair fires
    the registered :meth:`add_on_tunnel_repaired_callback` callbacks so
    consumers (e.g. the plugin's ``ReverseTunnelHandler``) can emit a
    fresh envelope event with the possibly-new remote port.

    Forward-tunnel Unix sockets are created in a temporary directory with
    0o700 permissions (and 0o600 sockets), so the sockets are inaccessible
    to other users and same-user processes would need to discover the
    randomly generated directory path.
    """

    _tmpdir: tempfile.TemporaryDirectory[str] | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _connections: dict[str, paramiko.SSHClient] = PrivateAttr(default_factory=dict)
    _tunnel_socket_paths: dict[str, Path] = PrivateAttr(default_factory=dict)
    _tunnel_threads: dict[str, threading.Thread] = PrivateAttr(default_factory=dict)
    _shutdown_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    # Reverse tunnels are keyed by ``(conn_key, local_port)`` so that a single
    # SSH host can host multiple concurrent tunnels for different purposes --
    # e.g. one for a host application's API (``local_port == server_port``) and
    # one per agent for the Latchkey gateway (``local_port == per_agent_gateway_port``).
    _reverse_tunnels: dict[tuple[str, int], ReverseTunnelInfo] = PrivateAttr(default_factory=dict)
    _reverse_tunnel_setup_locks: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _health_check_thread: threading.Thread | None = PrivateAttr(default=None)
    _on_tunnel_repaired_callbacks: list[Callable[["ReverseTunnelInfo"], None]] = PrivateAttr(default_factory=list)
    # Failure bookkeeping for the per-tunnel exponential backoff used by the
    # health-check loop. Created lazily on first failure for a given tunnel
    # key and removed on success or when the tunnel itself is dropped.
    _failure_state: dict[tuple[str, int], _TunnelFailureState] = PrivateAttr(default_factory=dict)

    def _get_tmpdir(self) -> Path:
        """Get or create the secure temporary directory for Unix sockets.

        The directory is chmodded to 0o700 and contains only 0o600 sockets, so
        sharing /tmp with other users on the machine is safe.
        """
        if self._tmpdir is None:
            self._tmpdir = _create_short_path_tmpdir("mngr-forward-ssh-")
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

    def _invalidate_connection(self, conn_key: str, client: paramiko.SSHClient) -> None:
        """Drop ``client`` from the connection cache so the next request reconnects.

        The identity check makes concurrent invalidations idempotent: several
        connections can be in flight against the same dead transport and all
        time out, but only the first should close it. The rest must not tear
        down whatever replacement a later request has already established.
        """
        with self._lock:
            if self._connections.get(conn_key) is not client:
                return
            del self._connections[conn_key]
        # Closed outside the lock: closing a wedged transport can itself block,
        # and every other tunnel operation takes this lock.
        try:
            client.close()
        except (OSError, paramiko.SSHException) as e:
            logger.trace("Error closing invalidated SSH connection: {}", e)

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
        conn_key = f"{ssh_info.host}:{ssh_info.port}"
        tunnel_key = f"{conn_key}->{remote_host}:{remote_port}"

        with self._lock:
            existing_path = self._tunnel_socket_paths.get(tunnel_key)
            existing_thread = self._tunnel_threads.get(tunnel_key)
            if existing_path is not None and existing_thread is not None and existing_thread.is_alive():
                return existing_path

            client = self._get_or_create_connection(ssh_info)
            transport = _ssh_connection_transport(client)
            stop_event = threading.Event()
            on_transport_failure = _TransportFailureHandler(
                manager=self,
                conn_key=conn_key,
                client=client,
                stop_event=stop_event,
            )

            # Use a short hash of tunnel_key for the filename. Encoding the full
            # tunnel_key produces paths that can exceed AF_UNIX's 104-byte
            # sun_path limit on macOS, especially with long hostnames or IPv6
            # addresses. 12 hex chars (48 bits) is ample to avoid collisions
            # between tunnels within a single manager instance.
            tunnel_id = hashlib.blake2b(tunnel_key.encode(), digest_size=6).hexdigest()
            socket_path = self._get_tmpdir() / f"t-{tunnel_id}.sock"

            if socket_path.exists():
                socket_path.unlink()

            # Bound and listening before the path is handed back, so a caller
            # that connects immediately cannot land in the window between the
            # socket file appearing (at bind) and the listen that makes it
            # connectable -- which shows up as a spurious unreachable backend.
            server = _create_tunnel_listener(socket_path)
            thread = threading.Thread(
                target=_tunnel_accept_loop,
                args=(
                    server,
                    socket_path,
                    transport,
                    remote_host,
                    remote_port,
                    self._shutdown_event,
                    stop_event,
                    on_transport_failure,
                ),
                daemon=True,
                name=f"ssh-tunnel-{tunnel_key}",
            )
            thread.start()

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

        ``agent_id`` (optional) tags the resulting ``ReverseTunnelInfo`` with
        the owning agent's stringified ID so callers can later ask the manager
        to tear down all tunnels belonging to a destroyed agent via
        :meth:`remove_reverse_tunnels_for_agent`. Pass ``None`` (the default)
        when the tunnel is not associated with a specific agent.
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

        Called once per health-check iteration. Broken tunnels are
        re-established with the same originally-requested remote port (so
        any in-container env var naming the tunnel URL keeps pointing at a
        working endpoint). After a successful repair each registered
        ``on_tunnel_repaired`` callback is fired with the new
        ``ReverseTunnelInfo`` so consumers (e.g. the plugin's
        ``ReverseTunnelHandler``) can emit a fresh envelope event.

        Failed repair attempts back off exponentially (per tunnel, capped at
        ``_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS``) so the manager does not pay
        for a fresh paramiko handshake against a permanently-gone target on
        every 30s tick. The retry continues indefinitely so that a tunnel
        whose target is temporarily unreachable still recovers once the
        target comes back. A successful repair clears the backoff state.
        """
        with self._lock:
            tunnels = dict(self._reverse_tunnels)
            callbacks = list(self._on_tunnel_repaired_callbacks)

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
                # and back off from the cap instead of from zero. Drop any
                # lingering bookkeeping so this tunnel's next failure
                # starts a fresh schedule.
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
                with self._lock:
                    repaired_info = self._reverse_tunnels.get(tunnel_key)
                if repaired_info is not None:
                    for callback in callbacks:
                        try:
                            callback(repaired_info)
                        except (OSError, RuntimeError) as e:
                            logger.warning("Tunnel-repaired callback failed: {}", e)
            except (paramiko.SSHException, OSError, SSHTunnelError) as e:
                self._record_repair_failure(tunnel_key, conn_key, tunnel_info, e)

    def _record_repair_failure(
        self,
        tunnel_key: tuple[str, int],
        conn_key: str,
        tunnel_info: ReverseTunnelInfo,
        error: Exception,
    ) -> None:
        """Record backoff state after a failed repair (the retry itself is
        driven from :meth:`_check_and_repair_tunnels`).

        Split out for readability; the only caller is the ``except`` arm of
        the repair loop, which catches ``paramiko.SSHException``,
        ``OSError``, and our own ``SSHTunnelError``. The retry continues
        forever -- the backoff is capped at
        ``_REVERSE_TUNNEL_BACKOFF_CAP_SECONDS`` so a permanently-gone target
        only costs one paramiko handshake every five minutes, but a target
        that comes back online still gets repaired.

        Once the exponential schedule has reached the cap, the failure
        counter stops incrementing. Otherwise a tunnel that fails forever
        would compute an ever-growing ``2 ** failures`` per tick (e.g.
        ~30K-digit bigints after a year of one-failure-per-five-minutes)
        before clamping it back down to the cap, which is wasted work.
        """
        with self._lock:
            failure_state = self._failure_state.get(tunnel_key)
            if failure_state is None:
                failure_state = _TunnelFailureState()
                self._failure_state[tunnel_key] = failure_state
            # Stop incrementing once the exponential schedule has already
            # reached the cap; further increments would just keep
            # recomputing larger ``2 ** failures`` values that immediately
            # get clamped back down.
            if 2**failure_state.consecutive_failures < _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS:
                failure_state.consecutive_failures += 1
            backoff_seconds = min(float(2**failure_state.consecutive_failures), _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS)
            failure_state.next_attempt_at = time.monotonic() + backoff_seconds
            failures = failure_state.consecutive_failures

        logger.warning(
            "Failed to re-establish reverse tunnel to {} (local {}): {} (failure {}, backoff {:.0f}s)",
            conn_key,
            tunnel_info.local_port,
            error,
            failures,
            backoff_seconds,
        )

    def remove_reverse_tunnels_for_agent(self, agent_id: str) -> int:
        """Tear down every reverse tunnel associated with ``agent_id``.

        Cancels each matching port forward on the underlying SSH transport,
        drops the tunnel from the registry, and clears any backoff
        bookkeeping. If the SSH client for a given host has no remaining
        tunnels (forward or reverse) after removal, that client is closed
        too -- otherwise its paramiko transport thread would keep polling
        forever even though no live reverse tunnel uses it.

        Returns the number of tunnels removed. Safe to call when no tunnel
        for ``agent_id`` exists (returns ``0``).
        """
        with self._lock:
            keys = [tunnel_key for tunnel_key, info in self._reverse_tunnels.items() if info.agent_id == agent_id]
        return self._drop_tunnel_keys(tuple(keys))

    def _drop_tunnel_keys(self, tunnel_keys: tuple[tuple[str, int], ...]) -> int:
        """Internal shared cleanup used by :meth:`remove_reverse_tunnels_for_agent`.

        For each ``(conn_key, local_port)`` in ``tunnel_keys``: cancel its
        reverse port forward (best-effort -- the transport may already be
        dead), drop it from ``_reverse_tunnels``, drop its backoff entry.
        For each conn_key whose last *reverse* tunnel is being removed AND
        which has no active forward-tunnel thread, close and forget the SSH
        client so its transport thread exits. (Forward tunnels go through
        the same SSH client; we must not close it out from under a live
        forward tunnel, even if no reverse tunnel uses the host anymore.)
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
            # no further use for its SSH client *unless* a forward tunnel
            # is still using it. Pop it now so we can close it outside the
            # lock; the forward-tunnel-still-using-it check is below.
            affected_conn_keys = {tunnel_key[0] for tunnel_key, _ in removed_infos}
            still_in_use_conn_keys = {tunnel_key[0] for tunnel_key in self._reverse_tunnels}
            # Forward-tunnel keys have the shape ``"<host>:<port>-><rhost>:<rport>"``;
            # the conn_key prefix tells us if any forward tunnel currently
            # uses the connection.
            forward_in_use_conn_keys = {
                tunnel_path.split("->", 1)[0] for tunnel_path in self._tunnel_socket_paths if "->" in tunnel_path
            }
            orphaned_conn_keys = affected_conn_keys - still_in_use_conn_keys - forward_in_use_conn_keys
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

    def add_on_tunnel_repaired_callback(self, callback: "Callable[[ReverseTunnelInfo], None]") -> None:
        """Register a callback fired once per successful repair of a broken tunnel.

        Used by the plugin's ``ReverseTunnelHandler`` to re-emit a
        ``reverse_tunnel_established`` envelope (with a possibly-new remote
        port) so consumers can rewire any URL files they own.
        """
        with self._lock:
            self._on_tunnel_repaired_callbacks.append(callback)

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

        # Cancel reverse port forwards. Attempt the cancel unconditionally
        # (best-effort) instead of skipping it when the connection looks
        # inactive: a half-dead transport that paramiko has not yet noticed
        # would otherwise leave the remote sshd's forwarded listener bound,
        # orphaning the port across restarts. ``cancel_port_forward`` is itself
        # a no-op on a transport paramiko already considers dead, so this is
        # safe even when the connection is genuinely gone.
        for tunnel_key, tunnel_info in self._reverse_tunnels.items():
            conn_key, _local_port = tunnel_key
            client = self._connections.get(conn_key)
            if client is None:
                continue
            try:
                transport = client.get_transport()
                if transport is not None:
                    transport.cancel_port_forward("127.0.0.1", tunnel_info.remote_port)
            except (paramiko.SSHException, OSError) as e:
                logger.trace("Error cancelling reverse port forward: {}", e)
        self._reverse_tunnels.clear()
        self._failure_state.clear()

        # Emptied under the lock, because a relay thread whose channel open is
        # still in flight outlives the accept loops joined above and invalidates
        # against this dict. Closing happens outside the lock: closing a wedged
        # transport can itself block.
        with self._lock:
            clients = tuple(self._connections.values())
            self._connections.clear()
        for client in clients:
            try:
                client.close()
            except (OSError, paramiko.SSHException) as e:
                logger.trace("Error closing SSH connection during cleanup: {}", e)

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

    # Send periodic keepalives so an idle reverse tunnel is not reaped for
    # inactivity, which would leave the remote sshd's forwarded listener bound
    # and orphan the remote port across restarts. See
    # ``_SSH_KEEPALIVE_INTERVAL_SECONDS`` for why they do not detect a dead peer.
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(_SSH_KEEPALIVE_INTERVAL_SECONDS)

    return client


def _create_short_path_tmpdir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """Create a temporary directory short enough to hold AF_UNIX socket paths.

    On macOS, $TMPDIR is a long per-user path under /var/folders/... that can
    push a socket path over ``_MAX_AF_UNIX_PATH_LENGTH`` on its own, so /tmp is
    used directly on Darwin.
    """
    base_dir = "/tmp" if sys.platform == "darwin" else None
    return tempfile.TemporaryDirectory(prefix=prefix, dir=base_dir)


def _create_tunnel_listener(socket_path: Path) -> socket.socket:
    """Create, bind, and listen on the tunnel's Unix domain socket.

    Done on the caller's thread rather than inside the accept loop so that the
    socket is connectable the moment the path is handed out, and so a bind
    failure raises here instead of surfacing on a background thread.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
    except OSError as e:
        server.close()
        raise SSHTunnelError(f"Could not bind tunnel socket {socket_path}: {e}") from e
    try:
        os.chmod(str(socket_path), 0o600)
        server.listen(_TUNNEL_LISTEN_BACKLOG)
        server.settimeout(_SHUTDOWN_POLL_SECONDS)
    except OSError as e:
        server.close()
        # The bind above created the socket file, and closing the socket does
        # not remove it. Only this arm may unlink: a path that failed to bind
        # is one we do not own.
        socket_path.unlink(missing_ok=True)
        raise SSHTunnelError(f"Could not listen on tunnel socket {socket_path}: {e}") from e
    return server


class _TransportFailureHandler(FrozenModel):
    """Retires one tunnel and the SSH connection under it, when that connection stops answering.

    Retiring the loop is the half that triggers the rebuild: the next
    :meth:`SSHTunnelManager.get_tunnel_socket_path` finds a dead thread, and by
    then :meth:`SSHTunnelManager._get_or_create_connection` finds no cached
    client either, so both are established fresh.
    """

    # ``threading.Event`` is not pydantic-native, and ``FrozenModel`` disallows
    # arbitrary types by default.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    manager: "SSHTunnelManager" = Field(description="Manager whose connection cache the dead client is dropped from")
    conn_key: str = Field(description="``host:port`` key of the connection being retired")
    client: paramiko.SSHClient = Field(description="The specific client to drop; a replacement is left alone")
    stop_event: threading.Event = Field(description="Per-tunnel flag that retires this tunnel's accept loop")

    def __call__(self) -> None:
        self.manager._invalidate_connection(self.conn_key, self.client)
        self.stop_event.set()


def _is_transport_unusable(transport: paramiko.Transport, open_seconds: float) -> bool:
    """Whether a channel open that just failed means the SSH connection itself is unusable.

    The exception paramiko raises cannot answer this:
    ``Transport.saved_exception`` is a single slot shared by every in-flight
    open and reading it clears it, so out of a burst of refusals only the first
    waiter to wake sees the ``ChannelException``. The rest get a bare
    ``SSHException``, the same type a genuine transport failure raises.

    The transport answers it. sshd answering, refusal included, leaves the
    transport active and comes back within a single round trip. A peer that
    vanished either takes the transport down with it, or leaves it reporting
    active while the open runs out ``_CHANNEL_OPEN_TIMEOUT_SECONDS``.

    ``open_seconds`` must be the larger of the monotonic and wall-clock elapsed,
    because paramiko times the open on the wall clock. A machine that suspends
    with an open in flight resumes with the wall clock advanced by the whole
    sleep, so paramiko times out at once while the monotonic elapsed is still
    near zero.
    """
    return not transport.is_active() or open_seconds >= _CHANNEL_OPEN_TIMEOUT_SECONDS


def _open_and_relay(
    client_sock: socket.socket,
    transport: paramiko.Transport,
    remote_host: str,
    remote_port: int,
    on_transport_failure: Callable[[], None],
) -> None:
    """Open a direct-tcpip channel for one accepted connection, then relay it.

    Runs per connection rather than on the accept loop, because opening the
    channel is the step that blocks when the transport has silently died: doing
    it inline would queue every later connection behind the stuck one.

    A failed open is either the target port not listening -- a workspace still
    booting refuses every open until its service comes up -- or the SSH
    connection underneath having stopped answering. The two call for opposite
    responses, and :func:`_is_transport_unusable` is what tells them apart: only
    the second retires the connection via ``on_transport_failure``.

    ``EOFError`` is caught alongside paramiko's own exceptions because paramiko
    re-raises a bare one from an open that was in flight when the peer closed;
    it is neither an ``SSHException`` nor an ``OSError``.
    """
    started_at = time.monotonic()
    started_at_wall = time.time()
    try:
        channel = transport.open_channel(
            "direct-tcpip",
            (remote_host, remote_port),
            ("127.0.0.1", 0),
            timeout=_CHANNEL_OPEN_TIMEOUT_SECONDS,
        )
    except (paramiko.SSHException, EOFError, OSError) as e:
        client_sock.close()
        open_seconds = max(time.monotonic() - started_at, time.time() - started_at_wall)
        if not _is_transport_unusable(transport, open_seconds):
            logger.debug("Failed to open an SSH channel to {}:{}: {}", remote_host, remote_port, e)
            return
        logger.warning(
            "Failed to open an SSH channel to {}:{} at the transport level; dropped the connection so "
            "the next request reconnects: {}",
            remote_host,
            remote_port,
            e,
        )
        on_transport_failure()
        return

    # Leave a trace of a degrading link before it starts tripping the hard bound.
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > _CHANNEL_OPEN_SLOW_THRESHOLD_SECONDS:
        logger.debug(
            "Opened an SSH channel to {}:{} in {:.1f}s, far longer than a single round trip should take",
            remote_host,
            remote_port,
            elapsed_seconds,
        )

    relay_data(client_sock, channel)


def _tunnel_accept_loop(
    server: socket.socket,
    sock_path: Path,
    transport: paramiko.Transport,
    remote_host: str,
    remote_port: int,
    shutdown_event: threading.Event,
    stop_event: threading.Event,
    on_transport_failure: Callable[[], None],
) -> None:
    """Accept connections on an already-listening Unix socket and forward them via SSH.

    Takes ownership of ``server`` (built by :func:`_create_tunnel_listener`) and
    closes and unlinks it on exit. Each accepted connection is handed to its own
    thread, which opens the direct-tcpip channel and relays it.
    ``shutdown_event`` retires every tunnel (manager cleanup); ``stop_event``
    retires just this one, which is how a transport failure gets the tunnel
    rebuilt on the next request.
    """
    try:
        while not shutdown_event.is_set() and not stop_event.is_set():
            try:
                client_sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("Accept loop socket error, stopping tunnel: {}", e)
                break

            threading.Thread(
                target=_open_and_relay,
                args=(client_sock, transport, remote_host, remote_port, on_transport_failure),
                daemon=True,
                name=f"ssh-relay-{remote_host}:{remote_port}",
            ).start()
    finally:
        server.close()
        try:
            os.unlink(str(sock_path))
        except OSError as e:
            logger.trace("Error unlinking tunnel socket: {}", e)


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
            target=relay_data,
            args=(local_sock, channel),
            daemon=True,
            name=f"reverse-relay-127.0.0.1:{self.local_port}",
        ).start()


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
