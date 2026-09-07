"""Agent-lifecycle callbacks that wire the shared gateway into each agent.

Exposes two callables:

* :class:`LatchkeyDiscoveryHandler` -- on every agent discovery, ensures
  the shared desktop ``latchkey gateway`` subprocess is up and exposes exactly
  one gateway on the agent's ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. Local
  workspaces receive the desktop gateway directly. Remote workspaces receive
  the VPS gateway, while a separate desktop-to-VPS tunnel lets its forwarding
  extension reach Minds-owned endpoints on the desktop. A workspace whose
  gateway location cannot be resolved yet receives *neither*: guessing the
  desktop gateway would half-work while exposing it to a workspace that is not
  entitled to it (see ``_warn_unresolved_gateway_route``).
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

import threading

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.loader import load_config
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.remote.credentials import RemoteLatchkeyDirectory
from imbue.mngr_latchkey.remote.provisioning import DESKTOP_GATEWAY_VPS_PORT
from imbue.mngr_latchkey.remote.provisioning import provision_remote_gateway
from imbue.mngr_latchkey.remote.provisioning import sync_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host


class _GatewayRoute(FrozenModel):
    """Successfully-resolved gateway route for one host."""

    outer_ssh_info: RemoteSSHInfo | None = Field(
        description="Remote outer-host SSH endpoint, or None when the workspace uses the desktop gateway directly"
    )


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

    An agent whose host discovery reports as not-running (stopped, paused,
    crashed, ...) instead has its reverse tunnel torn down and skips gateway
    provisioning, since its container sshd and docker target are gone until it
    restarts; the shared desktop gateway is still ensured up (it is shared
    across all agents). A ``None`` host state is treated as unknown and stays
    on the normal path.

    ``UNAUTHENTICATED`` is handled separately: the host is up and its container
    is probably serving the workspace fine, but the outer sshd rejected our key,
    so provisioning has no usable door. That is also skipped, but with a warning
    (once per host) -- otherwise the only symptom is every latchkey call from
    that host's agents failing while the workspace looks perfectly healthy.
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
    # host_ids with a provisioning pass currently in flight, so multiple agents
    # sharing one outer host coalesce onto a single (host-scoped) provisioning
    # run instead of racing concurrent passes against the same VPS/container.
    # Guarded by ``_remote_hosts_lock``, held only for the brief check-and-set
    # (never across the provisioning I/O).
    _provisioning_hosts: set[str] = PrivateAttr(default_factory=set)
    # host_ids whose VPS-resident gateway has been provisioned successfully this
    # supervisor lifetime. Provisioning is expensive (multiple SSH round-trips)
    # and the discovery stream re-emits the full agent set on every cycle, so we
    # skip re-provisioning an already-provisioned host rather than re-running it
    # every cycle. The desktop reads and edits a machine's credentials and
    # policy on demand, so nothing else here depends on the record; a supervisor
    # restart clears this and re-provisions. A failed pass is *not* recorded
    # here, so it retries on the next cycle.
    _provisioned_hosts: set[str] = PrivateAttr(default_factory=set)
    _remote_hosts_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Successful route resolutions are static for a host and can be reused on
    # every discovery cycle. Failures are deliberately absent so the next cycle
    # retries once a late lease/provider record becomes visible.
    _gateway_route_by_host_id: dict[str, _GatewayRoute] = PrivateAttr(default_factory=dict)
    # host_ids already warned about an unresolvable gateway route, so the warning
    # is emitted once per host rather than on every discovery cycle.
    _unresolved_route_hosts: set[str] = PrivateAttr(default_factory=set)
    # Same, for hosts whose outer sshd rejected this machine's key.
    _unauthenticated_hosts: set[str] = PrivateAttr(default_factory=set)

    def __call__(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
        host_state: HostState | None,
    ) -> None:
        try:
            host_side_port = self.latchkey.start_gateway(self.concurrency_group)
        except LatchkeyError as e:
            logger.opt(exception=e).error("Failed to start shared Latchkey gateway for agent {}: {}", agent_id, e)
            return

        # UNAUTHENTICATED is not "the machine is down" -- the host is reachable
        # and its container is very likely serving the workspace normally; what
        # failed is our key on the *outer* sshd, which is the only door
        # provisioning can use. So the skip below is right, but it must not be
        # silent: nothing else reports it, and the user just sees every latchkey
        # call from their agents fail with connection-refused while the
        # workspace itself looks healthy.
        if host_state is HostState.UNAUTHENTICATED:
            self._warn_unauthenticated_host(host_id)
            self._tear_down_stopped_agent(agent_id, host_id)
            return

        # A host that discovery reports as explicitly not-running (stopped,
        # paused, crashed, ...) has no live container sshd or docker daemon
        # target to act on, so tear down any reverse tunnel we opened while it
        # was running and skip both the desktop-gateway tunnel and the
        # VPS-resident gateway provisioning (whose ``docker exec`` would fail
        # against a stopped container). A ``None`` state is "unknown / not
        # applicable" (e.g. the local provider, or a discovery event that
        # arrives before the host snapshot), so it stays on the normal path.
        # When the host returns to RUNNING, discovery re-fires and everything
        # is re-established.
        if host_state is not None and host_state is not HostState.RUNNING:
            self._tear_down_stopped_agent(agent_id, host_id)
            return

        if host_state is HostState.RUNNING:
            # Outer auth demonstrably works again (for imbue_cloud, RUNNING is
            # only reachable by running the listing script over outer SSH), so
            # the next rejection is a new episode and warns afresh rather than
            # being deduplicated against the last one.
            with self._remote_hosts_lock:
                self._unauthenticated_hosts.discard(str(host_id))

        if ssh_info is None:
            # No SSH info for this agent (e.g. local-provider agent in tests,
            # or a discovery event that fired before the host SSH event); we
            # cannot set up a reverse tunnel, so just ensure the gateway is up
            # and let the agent reach it via whatever direct route exists.
            return

        instance_key_str = str(AgentInstanceKey.build(agent_id, host_id))
        with self._pending_lock:
            if instance_key_str in self._pending_remote_agents:
                # Latchkey tunnel setup already in flight; skipping duplicate fire.
                return
            self._pending_remote_agents.add(instance_key_str)
        try:
            self.concurrency_group.start_new_thread(
                target=self._run_remote_setup,
                args=(agent_id, host_id, ssh_info, provider_name, host_side_port),
                name=f"latchkey-discovery-setup-{instance_key_str}",
                is_checked=False,
            )
        except (ConcurrencyExceptionGroup, InvalidConcurrencyGroupStateError, RuntimeError):
            # Roll back the pending flag so a later fire (after the CG
            # is healthy again) isn't permanently coalesced away.
            with self._pending_lock:
                self._pending_remote_agents.discard(instance_key_str)
            raise

    def _run_remote_setup(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
        host_side_port: int,
    ) -> None:
        """Worker-thread entry point that chooses and wires one workspace gateway."""
        instance_key = AgentInstanceKey.build(agent_id, host_id)
        is_pending_handed_off = False
        try:
            route = self._resolve_gateway_route(host_id, provider_name)
            if route is None:
                # Unresolved (not cached, so the next cycle retries). We wire
                # *nothing*: an unresolved route means we do not know which
                # gateway this workspace belongs to, and guessing the desktop one
                # is not a safe guess -- see
                # ``_warn_unresolved_gateway_route`` for why.
                self._warn_unresolved_gateway_route(host_id, provider_name)
            elif route.outer_ssh_info is None:
                # Confirmed desktop workspace (the provider has no outer host,
                # or its outer is this very machine): the normal local path.
                self._setup_desktop_gateway_reachability(agent_id, host_id, ssh_info, host_side_port)
            else:
                self._remove_stale_desktop_to_container_tunnel(agent_id, host_id, ssh_info, host_side_port)
                self._setup_desktop_gateway_reachability_on_vps(
                    agent_id,
                    host_id,
                    route.outer_ssh_info,
                    host_side_port,
                )
                is_pending_handed_off = self._maybe_dispatch_remote_gateway_provisioning(
                    agent_id, host_id, ssh_info, provider_name
                )
        finally:
            # The provisioning thread owns clearing the pending flag once the
            # heavy work finishes; otherwise (local agents, or provisioning was
            # not dispatched) clear it here.
            if not is_pending_handed_off:
                with self._pending_lock:
                    self._pending_remote_agents.discard(str(instance_key))

    def _remove_stale_desktop_to_container_tunnel(
        self, agent_id: AgentId, host_id: HostId, ssh_info: RemoteSSHInfo, host_side_port: int
    ) -> None:
        """Drop the desktop->container tunnel an earlier cycle may have opened for a VPS host.

        A cycle that wired this host to the desktop gateway (e.g. before
        a provider reload revealed its outer host) left a tunnel holding 1989
        in the container, where the VPS->container tunnel must bind. Removal is
        keyed by the container endpoint rather than the agent tag: the
        desktop->VPS tunnel set up right after this carries the same agent tag,
        so an agent-keyed removal tore it down and re-dialed it on every
        discovery cycle. In the steady state (no stale tunnel) this is a no-op.
        """
        if self.tunnel_manager.remove_reverse_tunnel(ssh_info, host_side_port):
            logger.debug(
                "Removed a stale desktop->container latchkey tunnel for agent {} on host {}",
                agent_id,
                host_id,
            )

    def _tear_down_stopped_agent(self, agent_id: AgentId, host_id: HostId) -> None:
        """Drop a stopped agent's reverse tunnel and mark its host for re-provisioning.

        The per-agent reverse tunnel points at the container's sshd, which is
        down while the host is stopped; removing it stops the tunnel manager's
        health-check loop from indefinitely re-dialing a dead endpoint.
        Forgetting the host's ``_provisioned_hosts`` marker means a later restart
        re-runs the idempotent VPS-resident gateway provisioning, since a stopped
        container may be recreated before it comes back.
        """
        removed_tunnel_count = self.tunnel_manager.remove_reverse_tunnels_for_agent(
            AgentInstanceKey.build(agent_id, host_id)
        )
        if removed_tunnel_count:
            logger.debug("Removed {} reverse tunnel(s) for stopped agent {}", removed_tunnel_count, agent_id)
        with self._remote_hosts_lock:
            self._provisioned_hosts.discard(str(host_id))

    def _setup_desktop_gateway_reachability(
        self, agent_id: AgentId, host_id: HostId, ssh_info: RemoteSSHInfo, host_side_port: int
    ) -> None:
        """Reverse-tunnel the desktop-side gateway onto the agent's ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``.

        The instance tag (``<agent_id>@<host_id>``; agent ids are unique per
        host, not globally) lets the destruction handler drop this tunnel via
        ``remove_reverse_tunnels_for_agent``; without it the registry leaks
        across destroyed agents and the 30s health-check loop spins paramiko
        transports against ports that no longer exist. Failures are logged
        rather than raised so they never prevent the independent VPS-resident
        gateway provisioning path.
        """
        try:
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=host_side_port,
                remote_port=AGENT_SIDE_LATCHKEY_PORT,
                agent_id=AgentInstanceKey.build(agent_id, host_id),
            )
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.opt(exception=e).error(
                "Failed to set up desktop-side Latchkey reachability for agent {} (host-side port {}): {}",
                agent_id,
                host_side_port,
                e,
            )

    def _setup_desktop_gateway_reachability_on_vps(
        self,
        agent_id: AgentId,
        host_id: HostId,
        outer_ssh_info: RemoteSSHInfo,
        host_side_port: int,
    ) -> None:
        """Expose the desktop gateway on the VPS loopback for the proxy extension.

        The VPS currently has a one-to-one relationship with its workspace and
        main agent. Tagging the tunnel with that agent instance preserves the
        normal destruction behavior: stopping or destroying the agent tears
        down the now-unused desktop-to-VPS tunnel.
        """
        try:
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=outer_ssh_info,
                local_port=host_side_port,
                remote_port=DESKTOP_GATEWAY_VPS_PORT,
                agent_id=AgentInstanceKey.build(agent_id, host_id),
            )
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.opt(exception=e).error(
                "Failed to expose the desktop Latchkey gateway on VPS port {} for host {}: {}",
                DESKTOP_GATEWAY_VPS_PORT,
                host_id,
                e,
            )

    def _maybe_dispatch_remote_gateway_provisioning(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
    ) -> bool:
        """Dispatch VPS-resident gateway provisioning for agents whose host has an outer host.

        Returns ``True`` when the (potentially minutes-long) provisioning was
        handed off to its own fire-and-forget CG thread -- which then owns
        clearing the pending flag. Returns ``False`` for non-VPS agents and when
        the dispatch itself fails (logged so a later discovery fire retries).
        The thread is unchecked so a single agent's provisioning failure does
        not tear down the shared supervisor; the CG's ObservableThread logs any
        uncaught failure at error level so it is never silently missed.

        Independent of the desktop-to-VPS extension tunnel: a failure there
        does not prevent provisioning the VPS gateway for third-party calls.
        """
        host_id_str = str(host_id)
        with self._remote_hosts_lock:
            if host_id_str in self._provisioned_hosts:
                # Already provisioned this host this supervisor lifetime; skip the
                # expensive idempotent re-run that every discovery cycle would
                # otherwise trigger. A supervisor restart re-provisions.
                logger.trace(
                    "VPS-resident gateway already provisioned for host {} this session; "
                    "skipping re-provision for agent {}",
                    host_id,
                    agent_id,
                )
                return False
            if host_id_str in self._provisioning_hosts:
                # A provisioning pass for this host is already in flight. The
                # work is host-scoped (one container, one gateway, one tunnel),
                # so a second pass for another agent on the same host would be
                # redundant and would race the first on the same VPS files;
                # coalesce it away. A later discovery fire re-runs once the
                # in-flight pass clears the flag.
                logger.trace(
                    "VPS-resident gateway provisioning already in flight for host {}; coalescing agent {}",
                    host_id,
                    agent_id,
                )
                return False
            self._provisioning_hosts.add(host_id_str)
        try:
            self.concurrency_group.start_new_thread(
                target=self._run_remote_gateway_provisioning,
                args=(agent_id, host_id, ssh_info, provider_name),
                name=f"latchkey-provision-{str(agent_id)}",
                is_checked=False,
            )
        except (ConcurrencyExceptionGroup, InvalidConcurrencyGroupStateError, RuntimeError) as e:
            # The thread that would clear the in-flight flag never started, so
            # clear it here -- otherwise this host's provisioning would be
            # coalesced away forever.
            with self._remote_hosts_lock:
                self._provisioning_hosts.discard(host_id_str)
            logger.opt(exception=e).error(
                "Failed to dispatch VPS-resident Latchkey gateway provisioning for agent {}: {}",
                agent_id,
                e,
            )
            return False
        return True

    def _resolve_gateway_route(self, host_id: HostId, provider_name: str) -> _GatewayRoute | None:
        """Resolve whether the host uses the desktop or VPS gateway.

        Returns ``None`` when the answer is not knowable *yet*; that is never
        cached, so the next discovery cycle retries. Only answers derived from an
        opened outer host are cached, since those are static for the host: the
        provider has no outer at all (modal, local, ssh, docker-over-tcp), its
        outer is this very machine, or it is a genuinely remote VPS.

        Deliberately *not* keyed off the cheap ``outer_host_id_for`` pre-check:
        that returns ``None`` both for "this provider has no outer" and for "the
        outer is not known yet" (``mngr_vps`` returns ``None`` while the host
        record has no VPS IP -- routine in the first minutes of a create).
        Caching that as "desktop" pinned a VPS workspace to the desktop gateway
        for the rest of the supervisor's lifetime, so its VPS gateway was never
        provisioned at all. A provider whose outer is not resolvable yet raises
        (``HostNotFoundError``) from ``outer_host_for`` instead, which lands in
        the retryable branch below.
        """
        host_id_str = str(host_id)
        with self._remote_hosts_lock:
            cached = self._gateway_route_by_host_id.get(host_id_str)
        if cached is not None:
            return cached

        try:
            provider = self._provider_for_route(provider_name)
            try:
                route = self._resolve_route_via_provider(provider, host_id)
            except HostNotFoundError:
                # This supervisor holds one long-lived provider instance, and
                # some providers cache their whole host/lease listing on it with
                # no expiry (e.g. imbue_cloud's ``_leased_hosts_cache``). A host
                # leased *after* that listing was taken is then permanently
                # invisible: every new host would fall back to the desktop
                # gateway (and never get a VPS gateway) until the app restarts.
                # So treat "not found" as "our listing may be older than this
                # host" and look again on fresh data.
                logger.debug(
                    "Host {} not in provider {}'s cached listing; refreshing it and retrying",
                    host_id,
                    provider_name,
                )
                provider.reset_caches()
                route = self._resolve_route_via_provider(provider, host_id)
            if route is None:
                return None
        except (MngrError, OSError) as e:
            logger.debug(
                "Could not resolve latchkey gateway route for host {} via provider {}: {}",
                host_id,
                provider_name,
                e,
            )
            return None

        with self._remote_hosts_lock:
            self._gateway_route_by_host_id[host_id_str] = route
            self._unresolved_route_hosts.discard(host_id_str)
        return route

    def _provider_for_route(self, provider_name: str) -> ProviderInstanceInterface:
        """Return the provider instance route resolution should ask (a seam for tests)."""
        return get_provider_instance(ProviderInstanceName(provider_name), self.mngr_ctx)

    def _resolve_route_via_provider(
        self, provider: ProviderInstanceInterface, host_id: HostId
    ) -> _GatewayRoute | None:
        """Resolve the route from an opened outer host, or ``None`` if it has no SSH endpoint."""
        with provider.outer_host_for(host_id) as outer:
            if outer is None or outer.is_local:
                return _GatewayRoute(outer_ssh_info=None)
            connection_info = outer.get_ssh_connection_info()
            if connection_info is None:
                return None
            user, hostname, port, key_path = connection_info
            return _GatewayRoute(
                outer_ssh_info=RemoteSSHInfo(
                    user=user,
                    host=hostname,
                    port=port,
                    key_path=key_path,
                    known_hosts_path=outer.get_ssh_known_hosts_path(),
                )
            )

    def reload_provider_config(self) -> None:
        """Re-read the provider set from settings and forget cached route resolutions.

        The supervisor loads its :class:`MngrContext` once, at startup, and every
        route resolution goes through ``get_provider_instance`` against that
        snapshot. A provider instance the desktop client registers *later* (the
        user adds a cloud or imbue_cloud account mid-session) is therefore
        invisible here: resolution fails for every agent on it, the workspace is
        served by the desktop gateway, and its VPS gateway is never provisioned
        at all -- until the whole app is restarted. The desktop client already
        SIGHUPs this supervisor on every provider-set change to bounce the ``mngr
        observe`` child; this brings the supervisor's own view along with it.

        Only the provider mapping is taken from the freshly-loaded config. Every
        other setting stays as resolved at startup, because ``--setting``
        overrides are applied *after* ``load_config`` and would otherwise be
        silently dropped here. A failed reload leaves the current provider set in
        place: a stale provider set still serves every workspace that was already
        resolvable.
        """
        providers = self._load_provider_instance_configs()
        if providers is None:
            return
        config = self.mngr_ctx.config.model_copy_update(
            to_update(self.mngr_ctx.config.field_ref().providers, providers)
        )
        # A fresh context object also invalidates ``get_provider_instance``'s
        # cache, which is keyed by ``(name, id(mngr_ctx))``.
        self.mngr_ctx = self.mngr_ctx.model_copy_update(to_update(self.mngr_ctx.field_ref().config, config))
        with self._remote_hosts_lock:
            # Routes resolved against the previous provider set may have been
            # decided by a provider that has since been (re)configured.
            self._gateway_route_by_host_id.clear()
        logger.info("Reloaded the latchkey provider set ({} provider instance(s))", len(providers))

    def _load_provider_instance_configs(self) -> dict[ProviderInstanceName, ProviderInstanceConfig] | None:
        """Read the current provider-instance blocks from settings, or ``None`` on failure."""
        try:
            reloaded = load_config(self.mngr_ctx.pm, self.mngr_ctx.concurrency_group)
        except (MngrError, OSError) as e:
            logger.opt(exception=e).warning(
                "Could not reload the latchkey provider set; keeping the one loaded at startup: {}", e
            )
            return None
        return dict(reloaded.config.providers)

    def _warn_unauthenticated_host(self, host_id: HostId) -> None:
        """Surface a host whose outer sshd rejected our key, once per host, loudly.

        Provisioning reaches the workspace's gateway over the outer host, so an
        outer key rejection means nothing can be wired: the in-container gateway
        port stays unbound and every latchkey call from that host's agents fails
        with connection-refused. Falling back to the desktop gateway is not an
        option for the same reasons spelled out in
        ``_warn_unresolved_gateway_route``.

        This is worth its own warning because the failure is otherwise invisible:
        the container's sshd is untouched, so the workspace keeps loading and
        chatting normally and nothing connects the dead latchkey calls to the
        host's SSH state.
        """
        host_id_str = str(host_id)
        with self._remote_hosts_lock:
            is_first = host_id_str not in self._unauthenticated_hosts
            self._unauthenticated_hosts.add(host_id_str)
        message = (
            "Host {} rejected this machine's SSH key (host state UNAUTHENTICATED), so its latchkey "
            "gateway cannot be provisioned: its in-container gateway port stays closed and latchkey "
            "calls from its agents will fail with connection-refused. The workspace itself is "
            "unaffected (it reaches the container over a different sshd), so nothing else will report "
            "this. The host's outer authorized_keys has most likely lost this machine's key."
        )
        if is_first:
            logger.warning(message, host_id)
        else:
            logger.debug(message, host_id)

    def _warn_unresolved_gateway_route(self, host_id: HostId, provider_name: str) -> None:
        """Surface an unresolvable gateway route once per host, loudly.

        Nothing is wired for such a host: its in-container gateway port stays
        unbound, so its latchkey calls fail with connection-refused until a later
        cycle resolves the route. That is deliberately worse-looking than
        tunnelling the desktop gateway in as a guess, which is what this used to
        do, because for a VPS-backed workspace that guess is actively harmful:

        * It half-works, and therefore hides the problem. Requests that are not
          permission-checked (the ``/latchkey/`` RPC) succeed, while every
          third-party call and extension route is denied -- the workspace has no
          permissions-override JWT (its policy lives on the VPS), so the desktop
          gateway evaluates it against its deny-all default file.
        * It exposes the desktop gateway to the workspace. ``/latchkey/`` is
          gated by the shared password alone, so the workspace can enumerate the
          user's services, accounts and credential status, and start auth flows
          on the user's own machine.
        * It squats the container's ``AGENT_SIDE_LATCHKEY_PORT``, which the
          VPS->container tunnel has to bind, so provisioning then has to tear
          down a tunnel we opened ourselves.

        Resolution normally succeeds on the first try for desktop hosts
        (their providers answer without network I/O), so "unresolved" almost
        always means the provider lookup itself failed -- i.e. we know nothing
        about this host, and wiring nothing is the honest response.

        Warn on the first occurrence per host and drop to debug afterwards, so a
        persistent problem is visible without flooding the log on every cycle.
        """
        host_id_str = str(host_id)
        with self._remote_hosts_lock:
            is_first = host_id_str not in self._unresolved_route_hosts
            self._unresolved_route_hosts.add(host_id_str)
        message = (
            "Could not resolve the latchkey gateway route for host {} via provider {}; "
            "leaving its latchkey gateway unwired until this resolves (its in-container "
            "gateway port stays closed, so latchkey calls will fail there). Check that "
            "provider {} is configured in the settings this supervisor loaded."
        )
        if is_first:
            logger.warning(message, host_id, provider_name, provider_name)
        else:
            logger.debug(message, host_id, provider_name, provider_name)

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
                    logger.info(
                        "Outer host for agent {} (host {}) vanished before provisioning; skipping",
                        agent_id,
                        host_id,
                    )
                    return
                if outer.is_local:
                    # The outer is this very machine (e.g. a local docker daemon),
                    # not a remote VPS -- nothing to provision and nothing to sync.
                    logger.trace(
                        "Outer host for agent {} (host {}) is local; skipping VPS gateway provisioning",
                        agent_id,
                        host_id,
                    )
                    return
                # The reverse tunnel runs *on the outer host*, so it needs the
                # port the container's sshd is published on from the outer host's
                # own loopback -- not ``ssh_info.port``, which is how a remote
                # client reaches the container (a box-forwarded port for slices).
                # Providers whose topology splits publish from connect surface the
                # loopback port here; otherwise the two coincide and we fall back.
                loopback_ssh_port = provider.get_container_loopback_ssh_port(host_id)
                container_ssh_port = loopback_ssh_port if loopback_ssh_port is not None else ssh_info.port
                desktop_permissions_override = self.latchkey.create_permissions_override_jwt(
                    permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
                )
                provision_remote_gateway(
                    outer,
                    host_id=host_id,
                    container_ssh_user=ssh_info.user,
                    container_ssh_port=container_ssh_port,
                    latchkey_directory=self.latchkey.latchkey_directory,
                    gateway_password=self.latchkey.derive_gateway_password(),
                    desktop_permissions_override=desktop_permissions_override,
                )
                # Seed (or adopt) the machine's policy while its outer host is
                # still open. A gateway with no permissions file at all is an
                # allow-all gateway, so this is the one thing that cannot wait
                # for a user to open the workspace's Permissions tab.
                sync_permissions(
                    outer,
                    self.latchkey.latchkey_directory,
                    host_id,
                    RemoteLatchkeyDirectory(host=outer).resolve(),
                )
            logger.info("Provisioned VPS-resident Latchkey gateway for agent {} on host {}", agent_id, host_id)
            # Record success so later discovery cycles skip the expensive re-run.
            # Only reached when provisioning completed without raising (a failure
            # propagates past here, leaving the host eligible for retry).
            with self._remote_hosts_lock:
                self._provisioned_hosts.add(str(host_id))
        finally:
            # Release the per-host in-flight guard, and clear the per-agent
            # pending flag. (A failed pass leaves the host out of
            # ``_provisioned_hosts``, so a later discovery fire retries it.)
            with self._remote_hosts_lock:
                self._provisioning_hosts.discard(str(host_id))
            with self._pending_lock:
                self._pending_remote_agents.discard(str(AgentInstanceKey.build(agent_id, host_id)))


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

    def __call__(self, agent_id: AgentId, host_id: HostId) -> None:
        removed = self.tunnel_manager.remove_reverse_tunnels_for_agent(AgentInstanceKey.build(agent_id, host_id))
        if removed:
            logger.debug("Removed {} reverse tunnel(s) for destroyed agent {} on host {}", removed, agent_id, host_id)
