"""Agent-discovery callback that wires the shared gateway into each agent.

For every agent reported by an mngr discovery stream, this handler:

1. Ensures the shared ``latchkey gateway`` subprocess is up on the
   desktop host.
2. For agents reachable only via SSH (containers, VMs, VPS), opens a
   reverse port-forward so the agent's loopback
   ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` reaches the host-side
   gateway. DEV agents already run on the bare host and need no tunnel.

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
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_latchkey.ssh_tunnel import SSHTunnelError
from imbue.mngr_latchkey.ssh_tunnel import SSHTunnelManager


class LatchkeyDiscoveryHandler(MutableModel):
    """Discovery callback that ensures the shared Latchkey gateway is running and tunnels it in.

    Intended to be registered via ``MngrStreamManager.add_on_agent_discovered_callback``.

    For every discovered agent, ensures the shared ``latchkey gateway``
    subprocess is running on the desktop host. Agents that reach the
    desktop via SSH (containers, VMs, VPS) also get a reverse tunnel that
    exposes the host-side gateway on the agent's own
    ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT``. DEV-mode agents run on the
    bare host and need no tunnel; their ``LATCHKEY_GATEWAY`` env var
    points directly at the dynamic host port.
    """

    latchkey: Latchkey = Field(description="Latchkey wrapper that owns the shared gateway subprocess")
    tunnel_manager: SSHTunnelManager = Field(
        description="SSH tunnel manager used to reverse-forward the host-side gateway into remote agents"
    )
    concurrency_group: ConcurrencyGroup = Field(description="CG used to dispatch off-thread tunnel setups")

    _pending_remote_agents: set[str] = PrivateAttr(default_factory=set)
    _pending_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __call__(self, agent_id: AgentId, ssh_info: RemoteSSHInfo | None, provider_name: str) -> None:
        del provider_name
        try:
            info = self.latchkey.ensure_gateway_started()
        except LatchkeyError as e:
            logger.warning("Failed to start shared Latchkey gateway for agent {}: {}", agent_id, e)
            return

        if ssh_info is None:
            # DEV-mode agent runs on the bare host; it reaches the gateway
            # directly on its dynamic host port, so no tunnel is needed.
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
                args=(agent_id, ssh_info, info.port),
                name=f"latchkey-discovery-setup-{agent_id_str}",
                is_checked=False,
            )
        except (ConcurrencyExceptionGroup, InvalidConcurrencyGroupStateError, RuntimeError):
            # Roll back the pending flag so a later fire (after the CG
            # is healthy again) isn't permanently coalesced away.
            with self._pending_lock:
                self._pending_remote_agents.discard(agent_id_str)
            raise

    def _run_remote_setup(self, agent_id: AgentId, ssh_info: RemoteSSHInfo, host_side_port: int) -> None:
        """Worker-thread entry point. Always clears the pending flag in
        ``finally`` so a crash inside the SSH tunnel setup doesn't
        permanently block subsequent fires for this agent.
        """
        try:
            self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=host_side_port,
                remote_port=AGENT_SIDE_LATCHKEY_PORT,
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
