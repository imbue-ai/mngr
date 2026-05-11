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


class WorkspaceBackendFailureReason(UpperCaseStrEnum):
    """Why a per-agent backend forward attempt failed.

    Surfaced by the plugin so the minds-side health tracker can decide
    whether to tick the agent toward STUCK.

    - ``CONNECT_ERROR``: the plugin could not establish a connection to
      the backend (httpx.ConnectError / RemoteProtocolError before any
      response bytes).
    - ``SSE_EOF``: the backend dropped the response stream after some
      bytes had already been delivered. Despite the name (motivated by
      the SSE forwarding path that originally surfaced this), it also
      covers non-SSE mid-response read failures.
    - ``FIVEXX_RESPONSE``: the backend returned a 502/503/504. Other 5xx
      codes (e.g. application-layer 500s) are *not* tagged as failures.
    - ``UNRESOLVED``: the backend resolver had no entry for the agent.
    """

    CONNECT_ERROR = auto()
    SSE_EOF = auto()
    FIVEXX_RESPONSE = auto()
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


class WorkspaceBackendFailurePayload(FrozenModel):
    """Emitted when the plugin observes a per-agent backend failure.

    The plugin's role is observation only: it surfaces the kind of failure
    it saw (connect error, mid-SSE EOF, 5xx response) so the minds-side
    ``WorkspaceServerHealthTracker`` can apply policy (e.g. 5s
    HEALTHY -> STUCK transition).
    """

    type: Literal["workspace_backend_failure"] = "workspace_backend_failure"
    agent_id: AgentId = Field(description="Agent whose backend failed")
    reason: WorkspaceBackendFailureReason = Field(description="Why the forward attempt failed")
    status_code: int | None = Field(
        default=None,
        description="HTTP status code returned by the backend (only set when reason is FIVEXX_RESPONSE)",
    )


ForwardPayload = (
    LoginUrlPayload | ListeningPayload | ReverseTunnelEstablishedPayload | WorkspaceBackendFailurePayload
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
