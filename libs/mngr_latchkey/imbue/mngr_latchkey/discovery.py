"""Agent-lifecycle callbacks that wire the shared gateway into each agent.

Exposes two callables:

* :class:`LatchkeyDiscoveryHandler` -- on every agent discovery, ensures
  the shared ``latchkey gateway`` subprocess is up and makes it reachable
  on the agent's loopback ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. Agents
  whose host has an accessible outer host (the VPS -- e.g. imbue_cloud /
  vps_docker) get a VPS-resident gateway provisioned and reverse-tunneled
  into their container (see :func:`provision_remote_gateway`). All other
  SSH-reachable agents (local docker, modal, ssh) fall back to a reverse
  port-forward of the desktop-side gateway. Agents discovered without SSH
  info are expected to reach the gateway via whatever direct route exists.
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
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
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
from imbue.mngr_latchkey.remote_gateway import provision_remote_gateway


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
        """Worker-thread entry point. Always clears the pending flag in
        ``finally`` so a crash inside the setup doesn't permanently block
        subsequent fires for this agent.

        Agents whose host has an accessible outer host (the VPS -- e.g.
        imbue_cloud / vps_docker) get a VPS-resident gateway reverse-tunneled
        into their container. Everything else (local, modal, ssh, docker-over-tcp)
        falls back to the desktop-side gateway reverse tunnel.
        """
        try:
            if self._try_provision_remote_gateway(agent_id, host_id, ssh_info, provider_name):
                return
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=host_side_port,
                remote_port=AGENT_SIDE_LATCHKEY_PORT,
                # Tag the tunnel with the owning agent so the destruction
                # handler can ask the manager to drop it via
                # ``remove_reverse_tunnels_for_agent``. Without this the
                # tunnel registry leaks across destroyed agents and the
                # 30s health check loop spins paramiko transports against
                # ports that no longer exist.
                agent_id=str(agent_id),
            )
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.warning(
                "Failed to set up Latchkey reverse tunnel for agent {} (host-side port {}): {}",
                agent_id,
                host_side_port,
                e,
            )
        finally:
            with self._pending_lock:
                self._pending_remote_agents.discard(str(agent_id))

    def _try_provision_remote_gateway(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
    ) -> bool:
        """Stand up a VPS-resident gateway for the agent if its host has an outer host.

        Returns ``True`` when the agent is VPS-style (its provider exposes an
        outer host) and the VPS-resident gateway was provisioned, so the caller
        skips the desktop-side reverse tunnel. Returns ``False`` -- so the
        caller falls back to the desktop tunnel -- when the host has no outer
        host or provisioning fails. The container's ssh user/port come from the
        inner host's SSH info.
        """
        try:
            provider = get_provider_instance(ProviderInstanceName(provider_name), self.mngr_ctx)
            with provider.outer_host_for(host_id) as outer:
                if outer is None:
                    return False
                provision_remote_gateway(
                    outer,
                    host_id=host_id,
                    container_ssh_user=ssh_info.user,
                    container_ssh_port=ssh_info.port,
                )
            logger.info("Provisioned VPS-resident Latchkey gateway for agent {} on host {}", agent_id, host_id)
            return True
        except (RemoteGatewayError, MngrError, OSError, paramiko.SSHException) as e:
            logger.warning(
                "Failed to provision VPS-resident Latchkey gateway for agent {} on host {}; "
                "falling back to desktop tunnel: {}",
                agent_id,
                host_id,
                e,
            )
            return False


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
