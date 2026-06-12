from enum import auto
from typing import Any
from typing import Literal

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


class SystemInterfaceBackendFailureReason(UpperCaseStrEnum):
    """Why a per-agent backend forward attempt failed.

    Surfaced by the plugin so a downstream consumer can decide how to
    react (e.g. drive a health tracker / recovery UI).

    - ``CONNECT_ERROR``: the plugin could not establish a connection to
      the backend (httpx.ConnectError / RemoteProtocolError before any
      response bytes, or a failure setting up the SSH tunnel to a remote
      backend -- e.g. when the agent's host has gone away).
    - ``SSE_EOF``: the backend dropped the response stream after some
      bytes had already been delivered. Despite the name (motivated by
      the SSE forwarding path that originally surfaced this), it also
      covers non-SSE mid-response read failures.
    - ``ERROR_RESPONSE``: the backend answered with a non-2xx HTTP status.
      ``status_code`` carries the code. The plugin forwards the response
      unchanged and does not interpret which codes matter -- the consumer
      decides whether (and how) to react to a given status.
    - ``UNRESOLVED``: the backend resolver had no entry for the agent.
    """

    CONNECT_ERROR = auto()
    SSE_EOF = auto()
    ERROR_RESPONSE = auto()
    UNRESOLVED = auto()


class BackendUrl(NonEmptyStr):
    """A resolved HTTP(S) backend URL the plugin should byte-forward to."""


class ProxyTarget(FrozenModel):
    """The resolved backend a request to ``<agent-id>.localhost`` should hit."""

    url: BackendUrl = Field(description="Backend URL")
    ssh_info: RemoteSSHInfo | None = Field(
        default=None,
        description="SSH info for tunneling; None for local agents",
    )


# -- Envelope payload schemas -----------------------------------------------


class LoginUrlPayload(FrozenModel):
    """Emitted once at startup with the freshly-minted login URL."""

    type: Literal["login_url"] = "login_url"
    url: str = Field(description="Full login URL with one-time code")


class ListeningPayload(FrozenModel):
    """Emitted once the FastAPI app is ready to accept connections."""

    type: Literal["listening"] = "listening"
    host: str = Field(description="Bind host")
    port: ForwardPort = Field(description="Bind port")


class ReverseTunnelEstablishedPayload(FrozenModel):
    """Emitted whenever a reverse tunnel is set up (or re-established)."""

    type: Literal["reverse_tunnel_established"] = "reverse_tunnel_established"
    agent_id: AgentId = Field(description="Agent the tunnel was set up for")
    remote_port: PositiveInt = Field(description="Port on the remote sshd that was bound")
    local_port: PositiveInt = Field(description="Local port the tunnel forwards to")
    ssh_host: str = Field(description="SSH host the reverse tunnel runs over")
    ssh_port: PositiveInt = Field(description="SSH port on ssh_host")


class SystemInterfaceBackendFailurePayload(FrozenModel):
    """Emitted when the plugin observes a per-agent backend failure.

    The plugin's role is observation only: it surfaces the kind of failure
    it saw (connection failure, mid-stream EOF, or a non-2xx response) so a
    downstream consumer can apply its own policy (e.g. a health tracker's
    HEALTHY -> STUCK transition).
    """

    type: Literal["system_interface_backend_failure"] = "system_interface_backend_failure"
    agent_id: AgentId = Field(description="Agent whose backend failed")
    reason: SystemInterfaceBackendFailureReason = Field(description="Why the forward attempt failed")
    status_code: int | None = Field(
        default=None,
        description="HTTP status code returned by the backend (set when reason is ERROR_RESPONSE; None otherwise)",
    )


class ResolverSnapshotPayload(FrozenModel):
    """Emitted on every resolver mutation: full per-agent service map.

    Carries the full ``{agent_id: {service_name: url}}`` map held by the
    plugin's ``ForwardResolver`` at the moment of mutation. A consumer can
    keep the latest copy in process state to mirror which services the
    plugin has resolved for a given agent (e.g. for diagnostics).

    The full map is sent on every change (no per-agent diff) so a consumer
    that connects late only needs the most recent envelope to be in sync.
    """

    type: Literal["resolver_snapshot"] = "resolver_snapshot"
    services_by_agent: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Full per-agent service map: {agent_id_str: {service_name: url}}",
    )


ForwardPayload = (
    LoginUrlPayload
    | ListeningPayload
    | ReverseTunnelEstablishedPayload
    | SystemInterfaceBackendFailurePayload
    | ResolverSnapshotPayload
)


class ForwardEnvelope(FrozenModel):
    """JSONL envelope written to the plugin's stdout stream.

    ``stream`` discriminates the kind of line: ``observe`` and ``event`` are
    raw passthrough lines from the spawned ``mngr`` subprocesses (the
    ``payload`` is the parsed JSON of that line). ``forward`` carries the
    plugin's own state events (``LoginUrlPayload`` / ``ListeningPayload`` /
    ``ReverseTunnelEstablishedPayload``).

    ``agent_id`` is omitted when the line is not agent-scoped (observe
    discovery snapshots, listening, login_url, etc.).
    """

    stream: Literal["observe", "event", "forward"] = Field(description="Source stream")
    agent_id: AgentId | None = Field(
        default=None,
        description="Agent the line is scoped to; omitted when not applicable",
    )
    payload: dict[str, Any] = Field(description="Raw decoded JSON payload")


# -- Forwarding strategy ----------------------------------------------------


class ForwardServiceStrategy(FrozenModel):
    """Resolve backend URLs by looking up a named service per agent."""

    service_name: str = Field(description="Name of the service to forward (e.g. 'system_interface')")


class ForwardPortStrategy(FrozenModel):
    """Forward to a fixed remote port on each agent's host (manual mode).

    Uses ``127.0.0.1:<remote_port>`` on the agent's host as the backend; for
    remote agents this is reached via an SSH ``direct-tcpip`` tunnel. Local
    agents are reached directly on ``127.0.0.1``.
    """

    remote_port: PositiveInt = Field(description="Fixed port on the agent's host to forward to")


ForwardStrategy = ForwardServiceStrategy | ForwardPortStrategy


# -- Per-snapshot result ----------------------------------------------------


class ForwardAgentSnapshot(FrozenModel):
    """One agent's row in a snapshot returned from ``mngr_list_snapshot``.

    Carries the same fields the observe-stream context exposes for CEL
    filtering (``agent.id`` / ``agent.name`` / ``agent.host_id`` /
    ``agent.provider_name`` / ``agent.labels``) so ``--agent-include`` /
    ``--agent-exclude`` evaluate identically in both observe and
    ``--no-observe`` modes.
    """

    agent_id: AgentId = Field(description="Agent ID")
    ssh_info: RemoteSSHInfo | None = Field(
        default=None,
        description="SSH info if the agent is on a remote host; None for local agents",
    )
    agent_name: str = Field(
        default="",
        description="Agent name from mngr list output, used for client-side CEL filtering",
    )
    host_id: str = Field(
        default="",
        description="Host ID from mngr list output, used for client-side CEL filtering",
    )
    provider_name: str = Field(
        default="",
        description="Provider name from mngr list output, used for client-side CEL filtering",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Labels copied from mngr list output, used for client-side CEL filtering",
    )


class ForwardListSnapshot(FrozenModel):
    """Result of running ``mngr list --format json`` once."""

    agents: tuple[ForwardAgentSnapshot, ...] = Field(
        default=(),
        description="All agents returned by mngr list (no filtering)",
    )
