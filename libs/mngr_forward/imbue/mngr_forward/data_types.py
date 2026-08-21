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
      the backend (httpx.ConnectError, or RemoteProtocolError / ReadError
      / a timeout before any response bytes, or a failure dialing the SSH
      host a remote backend lives on -- e.g. when the agent's host has
      gone away). It is the residual class: a failure that leaves the
      backend's own health unresolved, where it really may be at fault.
      The three reasons below were split out of it precisely because they
      are *not* that. The plugin's own refusal to dial host loopback for
      an agent with no tunnel is reported here too -- the one case that
      dialed nothing and still leaves the backend unresolved.
    - ``TUNNEL_SETUP_FAILED``: this device could not build its own end of
      the tunnel -- no known_hosts file to pin the host key against, no
      private key to authenticate with, a local socket it could not
      create or bind. Evidence about the machine running the plugin,
      not about the agent's backend, which was never dialed. Only
      failures raised against this device's own filesystem and socket
      table carry this reason; anything that failed against the host,
      mid-handshake included, stays ``CONNECT_ERROR`` -- including a key
      that is present but no longer accepted, which cannot be told from
      the host rejecting us without asking it.
    - ``POOL_EXHAUSTED``: the plugin's own connection pool ran out (e.g.
      leaked streaming responses), so the backend was never dialed and
      its health is unknown. Again evidence about the plugin, not the
      backend.
    - ``BACKEND_NOT_LISTENING``: the SSH tunnel to the agent's host was
      established and the host refused the ``direct-tcpip`` channel to
      the inner port -- the host is reachable and nothing is serving the
      backend port. Distinguishes a dead service inside a live container
      from a container that cannot be reached at all.
    - ``SSE_EOF``: the backend answered -- its response headers arrived
      -- and the body then failed, either dropped or stalled until the
      read budget ran out. Despite the name (motivated by the SSE
      forwarding path that originally surfaced this), it also covers a
      buffered response, where no body bytes need have been delivered at
      all. What it always says is that the request reached the backend
      and the backend replied, which is what separates it from
      ``CONNECT_ERROR``.
    - ``ERROR_RESPONSE``: the backend answered with a non-2xx HTTP status.
      ``status_code`` carries the code. The plugin forwards the response
      unchanged and does not interpret which codes matter -- the consumer
      decides whether (and how) to react to a given status.
    - ``UNRESOLVED``: the backend resolver had no entry for the agent.
    - ``STALLED``: the plugin has waited on this request for its
      stall-notice window without a response. The window starts when the
      request is handed to the backend client, so it covers waiting for a
      pooled slot and the dial -- the backend need not have accepted the
      connection -- but not the routing and (for a remote agent) SSH tunnel
      setup that precede it, which can add a comparable delay. Unlike every
      other reason here the request has *not* failed -- it is still in
      flight and may yet succeed. It is emitted purely so a consumer can
      start probing a backend that may be wedged; a consumer must not treat
      it as evidence that the request itself failed.
    """

    CONNECT_ERROR = auto()
    TUNNEL_SETUP_FAILED = auto()
    POOL_EXHAUSTED = auto()
    BACKEND_NOT_LISTENING = auto()
    SSE_EOF = auto()
    ERROR_RESPONSE = auto()
    UNRESOLVED = auto()
    STALLED = auto()


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
    """Emitted when the plugin observes something notable about a per-agent backend.

    The plugin's role is observation only: it surfaces what it saw so a
    downstream consumer can apply its own policy (e.g. a health tracker's
    HEALTHY -> STUCK transition). ``reason`` says what that was, and not all
    of them report a request that failed -- ``STALLED`` reports one still in
    flight.
    """

    type: Literal["system_interface_backend_failure"] = "system_interface_backend_failure"
    agent_id: AgentId = Field(description="Agent whose backend the observation is about")
    reason: SystemInterfaceBackendFailureReason = Field(
        description="What the plugin observed (see SystemInterfaceBackendFailureReason)"
    )
    status_code: int | None = Field(
        default=None,
        description="HTTP status code returned by the backend (set when reason is ERROR_RESPONSE; None otherwise)",
    )
    detail: str | None = Field(
        default=None,
        description=(
            "Description of the exception that produced the observation, for a consumer to show or log: "
            "its message, or its class name where it carries no message (httpx leaves ReadError and every "
            "TimeoutException empty). None wherever no exception backs the observation -- every "
            "ERROR_RESPONSE / UNRESOLVED / STALLED, and the CONNECT_ERROR the plugin raises itself when it "
            "refuses to dial host loopback for an agent with no SSH tunnel."
        ),
    )


ForwardPayload = (
    LoginUrlPayload | ListeningPayload | ReverseTunnelEstablishedPayload | SystemInterfaceBackendFailurePayload
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
