"""Agent-lifecycle callbacks that wire the shared gateway into each agent.

Exposes two callables:

* :class:`LatchkeyDiscoveryHandler` -- on every agent discovery, ensures
  the shared desktop ``latchkey gateway`` subprocess is up and makes it
  reachable on the agent's loopback ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``.
  Agents whose host *also* has an accessible outer host (the VPS -- e.g.
  imbue_cloud / vps_docker) additionally get a VPS-resident gateway
  provisioned and reverse-tunneled into their container on a distinct
  ``127.0.0.1:INNER_PORT`` (see :func:`provision_remote_gateway`), so they
  can reach both the desktop gateway and the VPS gateway at once. Agents
  discovered without SSH info are expected to reach the gateway via
  whatever direct route exists.
* :class:`LatchkeyDestructionHandler` -- on every agent destruction,
  tears down the reverse tunnel that belongs to that agent so the
  manager's health-check loop doesn't keep spinning paramiko transports
  against an SSH host that no longer exists.

Tunnel setup is dispatched onto a worker thread via the supplied
``ConcurrencyGroup`` so the discovery-stream reader thread is never
blocked on slow SSH I/O. Concurrent fires for the same agent are
coalesced via ``_pending_remote_agents``: the underlying
``setup_reverse_tunnel`` is already idempotent on
``(host:port, local_port)``, so a duplicate fire would do no harm,
but coalescing avoids spinning up a redundant worker just to find an
existing tunnel and exit.
"""

import os
import threading
from collections.abc import Callable
from pathlib import Path

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.events import FileSystemMovedEvent
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.remote_gateway import RemoteGatewayError
from imbue.mngr_latchkey.remote_gateway import local_credentials_path
from imbue.mngr_latchkey.remote_gateway import provision_remote_gateway
from imbue.mngr_latchkey.remote_gateway import sync_credentials
from imbue.mngr_latchkey.remote_gateway import sync_permissions
from imbue.mngr_latchkey.store import hosts_dir
from imbue.mngr_latchkey.store import permissions_path_for_host

# How long to wait for the watchdog observer to wind down on shutdown before
# giving up (it is a daemon thread, so the process can exit regardless).
_OBSERVER_STOP_TIMEOUT_SECONDS: float = 5.0


class _LatchkeyStateChangeHandler(FrozenModel, FileSystemEventHandler):
    """watchdog handler that routes credential / per-host-permission file changes to sync callbacks.

    Frozen (and therefore hashable) because the watchdog observer stores
    scheduled handlers in a set; it is pure config + callbacks with no mutable
    state.

    Implements the ``dispatch`` method the watchdog observer calls for every
    filesystem event; it matches the changed path against the local credentials
    file and each currently-known remote host's permissions file and fires the
    corresponding callback. Unrelated paths (gateway logs, ``.tmp`` atomic-write
    siblings, unknown hosts) are ignored.
    """

    credentials_path: Path = Field(description="Absolute path of the local encrypted credentials file")
    plugin_data_dir: Path = Field(description="Plugin data dir under which per-host permissions files live")
    known_remote_host_ids: Callable[[], frozenset[str]] = Field(
        description="Returns the set of currently-known remote host ids (stringified)"
    )
    on_credentials_changed: Callable[[], None] = Field(description="Called when the credentials file changes")
    on_host_permissions_changed: Callable[[str], None] = Field(
        description="Called with a host id when that host's permissions file changes"
    )

    def dispatch(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # A move reports both src and dest; an atomic write (tmp -> rename)
        # surfaces the real file as the move dest, so consider both.
        changed_paths = {Path(os.fsdecode(event.src_path))}
        if isinstance(event, FileSystemMovedEvent):
            changed_paths.add(Path(os.fsdecode(event.dest_path)))
        if self.credentials_path in changed_paths:
            self.on_credentials_changed()
        for host_id_str in self.known_remote_host_ids():
            if permissions_path_for_host(self.plugin_data_dir, HostId(host_id_str)) in changed_paths:
                self.on_host_permissions_changed(host_id_str)


class LatchkeyDiscoveryHandler(MutableModel):
    """Discovery callback that ensures the shared Latchkey gateway is running and tunnels it in.

    Intended to be registered via ``MngrStreamManager.add_on_agent_discovered_callback``.

    For every discovered agent, ensures the shared ``latchkey gateway``
    subprocess is running on the desktop host. Agents that reach the
    desktop via SSH (containers, VMs, VPS) also get a reverse tunnel that
    exposes the host-side gateway on the agent's own
    ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. Agents discovered without SSH
    info (e.g. local-provider agents in tests, or any discovery that
    arrives before the host SSH event) skip the reverse-tunnel step and
    are expected to reach the gateway via whatever direct route already
    exists.
    """

    latchkey: Latchkey = Field(description="Latchkey wrapper that owns the shared gateway subprocess")
    tunnel_manager: SSHTunnelManager = Field(
        description="SSH tunnel manager used to reverse-forward the host-side gateway into remote agents"
    )
    concurrency_group: ConcurrencyGroup = Field(description="CG used to dispatch off-thread tunnel setups")
    mngr_ctx: MngrContext = Field(
        description="Mngr context used to open an agent's outer host (VPS) for the VPS-resident gateway path"
    )

    _pending_remote_agents: set[str] = PrivateAttr(default_factory=set)
    _pending_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # host_id -> provider_name for every genuinely-remote (VPS) host we have
    # provisioned a gateway on. Drives the remote-state sync loop.
    _remote_host_provider_by_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _remote_hosts_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __call__(self, agent_id: AgentId, host_id: HostId, ssh_info: RemoteSSHInfo | None, provider_name: str) -> None:
        try:
            host_side_port = self.latchkey.start_gateway(self.concurrency_group)
        except LatchkeyError as e:
            logger.warning("Failed to start shared Latchkey gateway for agent {}: {}", agent_id, e)
            return

        if ssh_info is None:
            # No SSH info for this agent (e.g. local-provider agent in tests,
            # or a discovery event that fired before the host SSH event); we
            # cannot set up a reverse tunnel, so just ensure the gateway is up
            # and let the agent reach it via whatever direct route exists.
            return

        agent_id_str = str(agent_id)
        with self._pending_lock:
            if agent_id_str in self._pending_remote_agents:
                logger.debug("Latchkey tunnel setup already in flight for agent {}; skipping duplicate fire", agent_id)
                return
            self._pending_remote_agents.add(agent_id_str)
        try:
            self.concurrency_group.start_new_thread(
                target=self._run_remote_setup,
                args=(agent_id, host_id, ssh_info, provider_name, host_side_port),
                name=f"latchkey-discovery-setup-{agent_id_str}",
                is_checked=False,
            )
        except (ConcurrencyExceptionGroup, InvalidConcurrencyGroupStateError, RuntimeError):
            # Roll back the pending flag so a later fire (after the CG
            # is healthy again) isn't permanently coalesced away.
            with self._pending_lock:
                self._pending_remote_agents.discard(agent_id_str)
            raise

    def _run_remote_setup(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
        host_side_port: int,
    ) -> None:
        """Worker-thread entry point that wires the gateway(s) into the agent.

        Every SSH-reachable agent gets the desktop-side gateway reverse-tunneled
        onto its ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` (this runs inline here
        since it is fast). Agents whose host *also* has an accessible outer host
        (the VPS -- e.g. imbue_cloud / vps_docker) additionally get a VPS-resident
        gateway provisioned and reverse-tunneled onto a distinct
        ``127.0.0.1:INNER_PORT``, so they can reach both gateways at once. That
        heavy provisioning is thrown onto its own fire-and-forget CG thread
        (which then owns clearing the pending flag); local agents clear it here.
        """
        is_pending_handed_off = False
        try:
            # Always: make the desktop-side gateway reachable on the agent's
            # ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. The ``agent_id`` tag lets the
            # destruction handler drop this tunnel via
            # ``remove_reverse_tunnels_for_agent``; without it the registry leaks
            # across destroyed agents and the 30s health-check loop spins
            # paramiko transports against ports that no longer exist.
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=host_side_port,
                remote_port=AGENT_SIDE_LATCHKEY_PORT,
                agent_id=str(agent_id),
            )
            # Additionally, for VPS agents: throw the (potentially minutes-long)
            # VPS-resident gateway provisioning onto its own CG thread and return
            # immediately. The CG's ObservableThread logs any uncaught failure at
            # error level, so a provisioning failure is never silently missed;
            # the thread is unchecked so a single agent's failure does not tear
            # down the shared supervisor.
            if self._host_has_outer_host(host_id, provider_name):
                self.concurrency_group.start_new_thread(
                    target=self._run_remote_gateway_provisioning,
                    args=(agent_id, host_id, ssh_info, provider_name),
                    name=f"latchkey-provision-{str(agent_id)}",
                    is_checked=False,
                )
                is_pending_handed_off = True
        except (
            SSHTunnelError,
            OSError,
            paramiko.SSHException,
            ConcurrencyExceptionGroup,
            InvalidConcurrencyGroupStateError,
            RuntimeError,
        ) as e:
            logger.warning(
                "Failed to set up Latchkey reachability for agent {} (host-side port {}): {}",
                agent_id,
                host_side_port,
                e,
            )
        finally:
            # The provisioning thread owns clearing the pending flag once the
            # heavy work finishes; otherwise (local agents, or a failure before
            # the provisioning thread was dispatched) clear it here.
            if not is_pending_handed_off:
                with self._pending_lock:
                    self._pending_remote_agents.discard(str(agent_id))

    def _host_has_outer_host(self, host_id: HostId, provider_name: str) -> bool:
        """Cheaply decide whether the agent's host has an accessible outer host (the VPS).

        Uses the connection-free ``outer_host_id_for``; any failure (unknown
        host, provider construction error) is treated as 'no outer' so the agent
        falls back to the desktop-side reverse tunnel.
        """
        try:
            provider = get_provider_instance(ProviderInstanceName(provider_name), self.mngr_ctx)
            return provider.outer_host_id_for(host_id) is not None
        except (MngrError, OSError) as e:
            logger.debug(
                "Could not determine outer host for host {} via provider {}; treating as non-VPS: {}",
                host_id,
                provider_name,
                e,
            )
            return False

    def _run_remote_gateway_provisioning(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
    ) -> None:
        """Fire-and-forget worker: stand up the VPS-resident gateway for a remote agent.

        Opens the agent's outer host and runs the full provisioning sequence on
        it. Exceptions are intentionally *not* swallowed: they propagate out of
        the thread target so the CG's ObservableThread logs them at error level
        (we never silently miss a provisioning failure). The pending flag is
        always cleared in ``finally`` so a later discovery fire retries.
        """
        try:
            provider = get_provider_instance(ProviderInstanceName(provider_name), self.mngr_ctx)
            with provider.outer_host_for(host_id) as outer:
                if outer is None:
                    # Raced: the outer host vanished between the cheap check and now.
                    logger.warning(
                        "Outer host for agent {} (host {}) vanished before provisioning; skipping",
                        agent_id,
                        host_id,
                    )
                    return
                if outer.is_local:
                    # The outer is this very machine (e.g. a local docker daemon),
                    # not a remote VPS -- nothing to provision and nothing to sync.
                    logger.debug(
                        "Outer host for agent {} (host {}) is local; skipping VPS gateway provisioning",
                        agent_id,
                        host_id,
                    )
                    return
                # Register the host so the remote-state watcher keeps its
                # credentials/permissions in sync from now on.
                with self._remote_hosts_lock:
                    self._remote_host_provider_by_id[str(host_id)] = provider_name
                provision_remote_gateway(
                    outer,
                    host_id=host_id,
                    container_ssh_user=ssh_info.user,
                    container_ssh_port=ssh_info.port,
                    latchkey_directory=self.latchkey.latchkey_directory,
                )
                # Initial sync for the freshly-provisioned host, reusing the
                # open outer connection: permissions first, then credentials.
                sync_permissions(outer, self.latchkey.latchkey_directory, host_id)
                sync_credentials(outer, self.latchkey.latchkey_directory)
            logger.info("Provisioned VPS-resident Latchkey gateway for agent {} on host {}", agent_id, host_id)
        finally:
            with self._pending_lock:
                self._pending_remote_agents.discard(str(agent_id))

    # -- Remote credential/permission sync ----------------------------------

    def start_remote_state_sync(self, concurrency_group: ConcurrencyGroup, shutdown_event: threading.Event) -> None:
        """Sync known remote hosts now, then watch for credential/permission changes.

        First syncs every currently-known remote host (permissions, then
        credentials -- order matters). Then starts a ``watchdog`` observer that
        pushes credentials to every known remote host whenever the local
        credentials file changes, and pushes a single host's permissions
        whenever that host's permissions file changes. (Newly-provisioned hosts
        get their initial sync inline in the provisioning path.)

        The observer's health is supervised on a *checked* CG strand: if it
        stops for any reason other than ``shutdown_event`` being set, that is a
        loud failure (the strand raises, the CG surfaces it, and the supervisor
        is signalled to shut down) rather than silently leaving remote agents
        with stale credentials/permissions. The observer is also stopped
        cleanly when ``shutdown_event`` is set.
        """
        self._sync_all_known_hosts()

        latchkey_directory = self.latchkey.latchkey_directory
        data_dir = self.latchkey.plugin_data_dir
        watched_hosts_dir = hosts_dir(data_dir)
        watched_hosts_dir.mkdir(parents=True, exist_ok=True)
        event_handler = _LatchkeyStateChangeHandler(
            credentials_path=local_credentials_path(latchkey_directory),
            plugin_data_dir=data_dir,
            known_remote_host_ids=self._known_remote_host_ids,
            on_credentials_changed=self._sync_credentials_to_all_known_hosts,
            on_host_permissions_changed=self._sync_permissions_to_host,
        )
        observer = Observer()
        # The credentials file sits at the latchkey-directory root; the per-host
        # permissions files live under the recursive hosts subtree.
        observer.schedule(event_handler, str(latchkey_directory), recursive=False)
        observer.schedule(event_handler, str(watched_hosts_dir), recursive=True)
        observer.daemon = True
        observer.start()
        # Stop the observer cleanly on shutdown (best-effort, so unchecked).
        concurrency_group.start_new_thread(
            target=self._stop_observer_on_shutdown,
            args=(observer, shutdown_event),
            name="latchkey-remote-state-watch-stopper",
            is_checked=False,
        )
        # Supervise the observer: an unexpected death is a loud, checked failure
        # that also wakes the supervisor so it tears down promptly.
        concurrency_group.start_new_thread(
            target=self._fail_loudly_if_observer_dies,
            args=(observer, shutdown_event),
            name="latchkey-remote-state-watch-sentinel",
            is_checked=True,
            on_failure=lambda _exception: shutdown_event.set(),
        )

    def _stop_observer_on_shutdown(self, observer: BaseObserver, shutdown_event: threading.Event) -> None:
        """Block until shutdown is signalled, then stop the watchdog observer."""
        shutdown_event.wait()
        observer.stop()
        observer.join(timeout=_OBSERVER_STOP_TIMEOUT_SECONDS)

    def _fail_loudly_if_observer_dies(self, observer: BaseObserver, shutdown_event: threading.Event) -> None:
        """Block until the observer stops; raise if it stopped for any reason other than shutdown.

        Run as a checked CG strand: a watchdog observer that dies mid-operation
        would otherwise leave remote agents silently un-synced, so we surface it
        loudly instead.
        """
        observer.join()
        if not shutdown_event.is_set():
            raise RemoteGatewayError(
                "Latchkey remote-state watcher (watchdog observer) stopped unexpectedly; remote agents' "
                "credentials and permissions are no longer being synced"
            )

    def _known_remote_host_ids(self) -> frozenset[str]:
        with self._remote_hosts_lock:
            return frozenset(self._remote_host_provider_by_id)

    def _sync_all_known_hosts(self) -> None:
        """Initial full sync (permissions then credentials) for every currently-known remote host."""
        with self._remote_hosts_lock:
            remote_hosts = dict(self._remote_host_provider_by_id)
        for host_id_str, provider_name in remote_hosts.items():
            self._sync_state_to_host(host_id_str, provider_name, do_permissions=True, do_credentials=True)

    def _sync_credentials_to_all_known_hosts(self) -> None:
        with self._remote_hosts_lock:
            remote_hosts = dict(self._remote_host_provider_by_id)
        for host_id_str, provider_name in remote_hosts.items():
            self._sync_state_to_host(host_id_str, provider_name, do_permissions=False, do_credentials=True)

    def _sync_permissions_to_host(self, host_id_str: str) -> None:
        with self._remote_hosts_lock:
            provider_name = self._remote_host_provider_by_id.get(host_id_str)
        if provider_name is None:
            return
        self._sync_state_to_host(host_id_str, provider_name, do_permissions=True, do_credentials=False)

    def _sync_state_to_host(
        self,
        host_id_str: str,
        provider_name: str,
        *,
        do_permissions: bool,
        do_credentials: bool,
    ) -> None:
        """Open the host's outer (VPS) and sync the requested state (permissions before credentials).

        A vanished host (``HostNotFoundError``) is dropped from the registry so
        we stop syncing it; other failures are logged and retried next pass.
        """
        host_id = HostId(host_id_str)
        try:
            provider = get_provider_instance(ProviderInstanceName(provider_name), self.mngr_ctx)
            with provider.outer_host_for(host_id) as outer:
                if outer is None or outer.is_local:
                    return
                # Order matters: permissions before credentials.
                if do_permissions:
                    sync_permissions(outer, self.latchkey.latchkey_directory, host_id)
                if do_credentials:
                    sync_credentials(outer, self.latchkey.latchkey_directory)
        except HostNotFoundError:
            with self._remote_hosts_lock:
                self._remote_host_provider_by_id.pop(host_id_str, None)
            logger.debug("Remote host {} no longer exists; dropped from latchkey sync", host_id_str)
        except (RemoteGatewayError, MngrError, OSError, paramiko.SSHException) as e:
            logger.warning("Failed to sync latchkey state to remote host {}: {}", host_id_str, e)


class LatchkeyDestructionHandler(FrozenModel):
    """Destruction callback that drops the destroyed agent's reverse tunnel.

    The Latchkey gateway is shared across all agents and must outlive any
    single agent, so we do not stop it here. But the per-agent reverse
    SSH tunnel set up by ``LatchkeyDiscoveryHandler`` does need to go
    away: otherwise ``SSHTunnelManager`` keeps the entry in its registry
    and the 30s health-check loop spins paramiko transports against an
    SSH host that no longer exists, pegging a CPU.
    """

    tunnel_manager: SSHTunnelManager = Field(
        description="Manager whose reverse tunnels for the destroyed agent must be torn down"
    )

    def __call__(self, agent_id: AgentId) -> None:
        removed = self.tunnel_manager.remove_reverse_tunnels_for_agent(str(agent_id))
        if removed:
            logger.debug("Removed {} reverse tunnel(s) for destroyed agent {}", removed, agent_id)
