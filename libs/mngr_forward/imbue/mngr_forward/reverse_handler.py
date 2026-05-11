"""Per-agent reverse-tunnel setup driven by ``--reverse <remote>:<local>``.

For every agent discovered with SSH info, opens each configured reverse
tunnel pair and emits a ``forward.reverse_tunnel_established`` envelope. The
underlying ``SSHTunnelManager`` health-checks tunnels every ~30s and
re-establishes any that go stale; the repair callback re-emits the same
envelope (with a possibly-different remote port if sshd reassigned one).
"""

import threading
from collections.abc import Sequence

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.data_types import ReverseTunnelEstablishedPayload
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import ReverseTunnelInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager


class ReverseTunnelHandler(MutableModel):
    """``on_agent_discovered`` callback that maintains per-agent reverse tunnels."""

    tunnel_manager: SSHTunnelManager = Field(frozen=True, description="Underlying SSH tunnel manager")
    envelope_writer: EnvelopeWriter = Field(frozen=True, description="Where envelope events are emitted")
    specs: tuple[ReverseTunnelSpec, ...] = Field(
        frozen=True,
        description="One reverse tunnel pair per --reverse <remote>:<local>",
    )

    # Tracks which agents requested each plugin-managed tunnel so the repair
    # callback can re-emit one envelope per agent. Key is the
    # ``(ssh_host, ssh_port, local_port)`` triple, cast to plain ``int``s so
    # equality works against ``ReverseTunnelInfo.{ssh_info.host,
    # ssh_info.port, local_port}``.
    _agents_by_tunnel_key: dict[tuple[str, int, int], set[AgentId]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def model_post_init(self, __context: object) -> None:
        if self.specs:
            self.tunnel_manager.add_on_tunnel_repaired_callback(self._on_tunnel_repaired)

    def __call__(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        del provider_name
        if ssh_info is None or not self.specs:
            return
        for spec in self.specs:
            self._setup_one(agent_id, ssh_info, spec)

    def setup_for_snapshot(
        self,
        agents_with_ssh: Sequence[tuple[AgentId, RemoteSSHInfo]],
    ) -> None:
        """Set up reverse tunnels for a fixed snapshot of agents (--no-observe mode)."""
        if not self.specs:
            return
        for agent_id, ssh_info in agents_with_ssh:
            for spec in self.specs:
                self._setup_one(agent_id, ssh_info, spec)

    def _setup_one(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo,
        spec: ReverseTunnelSpec,
    ) -> None:
        try:
            assigned_remote_port = self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=spec.local_port,
                remote_port=spec.remote_port,
            )
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.warning(
                "Failed to set up reverse tunnel for agent {} ({}:{}): {}",
                agent_id,
                spec.remote_port,
                spec.local_port,
                e,
            )
            return
        self._track_agent(agent_id=agent_id, ssh_info=ssh_info, local_port=spec.local_port)
        self.envelope_writer.emit_reverse_tunnel_established(
            ReverseTunnelEstablishedPayload(
                agent_id=agent_id,
                remote_port=PositiveInt(assigned_remote_port),
                local_port=spec.local_port,
                ssh_host=ssh_info.host,
                ssh_port=PositiveInt(ssh_info.port),
            )
        )

    def _track_agent(self, agent_id: AgentId, ssh_info: RemoteSSHInfo, local_port: PositiveInt) -> None:
        key = (ssh_info.host, ssh_info.port, int(local_port))
        with self._lock:
            self._agents_by_tunnel_key.setdefault(key, set()).add(agent_id)

    def _on_tunnel_repaired(self, info: ReverseTunnelInfo) -> None:
        # Fan out one envelope per agent that requested this tunnel via
        # ``__call__`` / ``setup_for_snapshot``.
        agents = self._lookup_agents_for_tunnel(info)
        if agents:
            for agent_id in agents:
                self._emit_repaired(agent_id, info)
            return
        logger.debug(
            "Reverse tunnel repaired (host={}, local={}, remote={}); no agents tracked",
            info.ssh_info.host,
            info.local_port,
            info.remote_port,
        )

    def _lookup_agents_for_tunnel(self, info: ReverseTunnelInfo) -> tuple[AgentId, ...]:
        key = (info.ssh_info.host, info.ssh_info.port, info.local_port)
        with self._lock:
            agents = self._agents_by_tunnel_key.get(key, set())
            # Sort for stable emit order across repairs (set order is not
            # stable across runs).
            return tuple(sorted(agents))

    def _emit_repaired(self, agent_id: AgentId, info: ReverseTunnelInfo) -> None:
        self.envelope_writer.emit_reverse_tunnel_established(
            ReverseTunnelEstablishedPayload(
                agent_id=agent_id,
                remote_port=PositiveInt(info.remote_port),
                local_port=PositiveInt(info.local_port),
                ssh_host=info.ssh_info.host,
                ssh_port=PositiveInt(info.ssh_info.port),
            )
        )
