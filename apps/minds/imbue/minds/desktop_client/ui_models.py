"""Typed wire models for the SPA's `/ui/ws` channel and bootstrap payload.

Every frame on the channel is one of the message models below, discriminated by
its literal ``type`` field. The same models feed three consumers:

1. The WebSocket channel (`ui_channel.py`) serializes them per frame.
2. The bootstrap JSON inlined into the SPA index page (`ui_api.py`) embeds the
   connect-time snapshot so first paint needs zero extra round trips.
3. ``scripts/generate_ui_schema.py`` emits their JSON Schema, from which the
   frontend generates its TypeScript types -- these models ARE the wire
   contract, so any breaking change must bump ``UI_SCHEMA_VERSION``.

``app.py``'s row-derivation helpers still emit string-typed dict rows (a
holdover from the deleted ``/_chrome/events`` SSE wire format, which encoded
flags as ``"true"`` strings); its ``_ui_*_from_legacy_dict`` converters
translate those rows into these models, where booleans are real booleans.
"""

from enum import auto
from typing import Annotated
from typing import Literal

from pydantic import Field
from pydantic import TypeAdapter

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.system_interface_health import AgentHealth

# Bumped on ANY breaking change to the models in this module. The server
# inlines it into the page bootstrap and sends it again in every connection's
# hello frame; the client compares the two and hard-reloads once on mismatch
# (picking up freshly served assets). That catches a server that upgraded
# while a window stayed open across a reconnect -- it cannot catch assets
# built for another version being served with a matching bootstrap, since
# both values come from the same live server.
UI_SCHEMA_VERSION: int = 1


class UiWorkspaceEntry(FrozenModel):
    """One row of the workspace list (a live workspace, an in-flight create attempt, or a remote record).

    ``id`` is the workspace's ``agent_id``: the stable, singular identity of
    the workspace (the thing with a name that matters as a singleton).
    ``host_id`` is the logical machine currently running it (VM / container /
    remote host) and keys the content URLs (``/goto/<host-id>/`` and the
    ``host-<hex>.localhost`` origin family). They are mostly 1:1 today but
    diverge under future operations: "clone" mints a new agent_id AND a new
    host_id, while "move" mints a new host_id but preserves the agent_id (and
    retires the old host). UI state must therefore key on ``id`` and treat
    ``host_id`` as a swappable transport attribute.
    """

    id: str = Field(description="Workspace agent id (stable identity), or the create-attempt id for create rows")
    name: str = Field(description="Display name")
    accent: str = Field(description="#rrggbb accent color for chrome/sidebar rendering")
    host_id: str = Field(
        default="", description="Logical machine currently running the workspace; empty for rows without a live host"
    )
    is_stale: bool = Field(
        default=False, description="Provider's latest discovery poll errored; row retained but unverified"
    )
    supports_shutdown: bool = Field(default=False, description="Whether minds can stop/start this workspace's host")
    liveness: str = Field(default="", description="RUNNING / STOPPED / UNKNOWN when supports_shutdown, else empty")
    account: str = Field(default="", description="Owning account email, when known")
    create_attempt_state: str = Field(
        default="", description="creating / interrupted / failed for create-attempt rows; empty for real workspaces"
    )
    is_remote: bool = Field(default=False, description="Known only from synced records (hosted on another device)")
    location: str = Field(default="", description="Human label for where a remote workspace lives")


class UiHelloMessage(FrozenModel):
    """First frame on every connection: the server's schema version."""

    type: Literal["hello"] = "hello"
    schema_version: int = Field(description="UI_SCHEMA_VERSION the server was built with")


class UiWorkspacesMessage(FrozenModel):
    """Full workspace-list state (always the complete list, never a delta)."""

    type: Literal["workspaces"] = "workspaces"
    workspaces: tuple[UiWorkspaceEntry, ...] = Field(
        description="Every visible workspace/create/remote row, in display order"
    )
    destroying_agent_ids: tuple[str, ...] = Field(description="Agent ids with an in-flight or failed destroy")
    restorable_workspace_ids: tuple[str, ...] = Field(
        description="Agent ids AND host ids that window restore may target (both coordinates, see UiWorkspaceEntry)"
    )
    remote_workspace_states: dict[str, str] = Field(
        description="agent_id -> derived access state for remote tiles (empty string = plain remote)"
    )


class UiAccountsMessage(FrozenModel):
    """Account-launcher identity (bottom-left launcher label + signed-in flag)."""

    type: Literal["accounts"] = "accounts"
    has_accounts: bool = Field(description="Whether any account is signed in")
    account_email: str = Field(description="Label email for the launcher, empty when signed out")
    extra_account_count: int = Field(description="How many additional signed-in accounts beyond the label one")


class ProviderPanelStatus(UpperCaseStrEnum):
    """Status bucket for a provider row in the providers panel."""

    OK = auto()
    ERROR = auto()
    DISABLED = auto()


class UiProviderEntry(FrozenModel):
    """One provider row in the providers panel."""

    name: str = Field(description="Provider instance name")
    backend: str | None = Field(description="Provider backend, None when errored/disabled")
    status: ProviderPanelStatus = Field(description="Panel status bucket")
    is_enabled: bool = Field(description="Whether minds' settings enable the provider")
    error_type: str | None = Field(default=None, description="Discovery error type for errored providers")
    error_message: str | None = Field(default=None, description="Discovery error message for errored providers")
    is_cloud_account: bool = Field(default=False, description="Bring-your-own-key account row (deletable)")
    workspace_count: int = Field(default=0, description="Active workspaces on this provider (for byok Delete gating)")


class UiProvidersMessage(FrozenModel):
    """Providers panel state."""

    type: Literal["providers"] = "providers"
    providers: tuple[UiProviderEntry, ...] = Field(description="Sorted provider entries")
    last_event_at: str | None = Field(description="ISO timestamp of the last discovery event, None before any")
    last_full_snapshot_at: str | None = Field(description="ISO timestamp of the last full discovery snapshot")


class UiRequestsMessage(FrozenModel):
    """Pending-request inbox summary (badge count + exact id set + auto-open policy)."""

    type: Literal["requests"] = "requests"
    count: int = Field(description="Number of displayable pending requests")
    request_ids: tuple[str, ...] = Field(description="Pending request event ids in deterministic order")
    auto_open: bool = Field(description="Whether the user allows the inbox to auto-open on new requests")


class UiHealthMessage(FrozenModel):
    """One workspace's system-interface health state."""

    type: Literal["health"] = "health"
    agent_id: str = Field(description="Workspace agent id")
    status: AgentHealth = Field(description="Current health classification")
    error: str | None = Field(default=None, description="Last restart error, present for RESTART_FAILED")


class UiDiscoveryHealthMessage(FrozenModel):
    """App-global discovery-pipeline health."""

    type: Literal["discovery_health"] = "discovery_health"
    state: DiscoveryHealth = Field(description="Pipeline health bucket")


class UiWorkspaceStoppedMessage(FrozenModel):
    """An in-app action stopped this workspace's host; open views must not observe (and restart) it."""

    type: Literal["workspace_stopped"] = "workspace_stopped"
    agent_id: str = Field(description="Stopped workspace's agent id")


class UiOpenHelpMessage(FrozenModel):
    """An in-workspace agent escalated a bug report; surface the help modal pre-filled."""

    type: Literal["open_help"] = "open_help"
    description: str = Field(description="The agent's diagnosis text")
    workspace_agent_id: str = Field(description="Reporting workspace's agent id")


class UiWorkspaceRefreshMessage(FrozenModel):
    """An in-workspace agent changed the workspace's own interface; rebuild the displayed view."""

    type: Literal["workspace_refresh"] = "workspace_refresh"
    agent_id: str = Field(description="Workspace agent id whose view is stale")


class UiReloadMessage(FrozenModel):
    """Ask every client to hard-reload (e.g. new hashed assets after an update)."""

    type: Literal["reload_ui"] = "reload_ui"


class UiClientStateMessage(FrozenModel):
    """Client -> server registration: which window this is and what it is viewing."""

    type: Literal["client_state"] = "client_state"
    client_id: str = Field(description="Stable per-window id chosen by the client")
    route: str = Field(description="Current SPA route path")
    workspace_agent_id: str | None = Field(default=None, description="Displayed workspace's agent id, if any")


# Everything the server may send. The discriminator makes both pydantic
# validation and the generated TypeScript a tagged union.
UiServerMessage = Annotated[
    UiHelloMessage
    | UiWorkspacesMessage
    | UiAccountsMessage
    | UiProvidersMessage
    | UiRequestsMessage
    | UiHealthMessage
    | UiDiscoveryHealthMessage
    | UiWorkspaceStoppedMessage
    | UiOpenHelpMessage
    | UiWorkspaceRefreshMessage
    | UiReloadMessage,
    Field(discriminator="type"),
]

UI_CLIENT_MESSAGE_ADAPTER: TypeAdapter[UiClientStateMessage] = TypeAdapter(UiClientStateMessage)


class UiSnapshot(FrozenModel):
    """The complete connect-time state: one message of each snapshot type.

    Built by the publisher and used verbatim by both the WS connect sequence
    and the bootstrap JSON, so the two can never drift.
    """

    workspaces: UiWorkspacesMessage = Field(description="Current workspace list")
    accounts: UiAccountsMessage = Field(description="Current account-launcher identity")
    providers: UiProvidersMessage = Field(description="Current providers panel state")
    requests: UiRequestsMessage = Field(description="Current inbox summary")
    health: tuple[UiHealthMessage, ...] = Field(description="Per-workspace health states (only tracked workspaces)")
    discovery_health: UiDiscoveryHealthMessage = Field(description="Current discovery pipeline health")


class UiBootstrapSeed(FrozenModel):
    """First-paint seed values that are not part of publisher state."""

    accent: str = Field(description="Initial accent color (avoids neutral->accent pop-in)")
    is_mac: bool = Field(description="Whether the client platform is macOS (traffic-light padding etc.)")
    mngr_forward_origin: str = Field(description="Bare origin of the mngr forward plugin for /goto/ URLs")


class UiBootstrap(FrozenModel):
    """The ``window.__MINDS_BOOTSTRAP__`` document inlined into the SPA index page."""

    seed: UiBootstrapSeed = Field(description="First-paint seed values")
    schema_version: int = Field(description="UI_SCHEMA_VERSION the serving code was built with")
    snapshot: UiSnapshot = Field(description="Connect-time state snapshot")


class UiWireSchema(FrozenModel):
    """Container whose JSON Schema is the single generated artifact for the frontend.

    Never instantiated; exists so ``model_json_schema()`` hoists every wire
    model into one ``$defs`` table for ``scripts/generate_ui_schema.py``.
    """

    hello: UiHelloMessage = Field(description="hello frame")
    workspaces: UiWorkspacesMessage = Field(description="workspaces frame")
    accounts: UiAccountsMessage = Field(description="accounts frame")
    providers: UiProvidersMessage = Field(description="providers frame")
    requests: UiRequestsMessage = Field(description="requests frame")
    health: UiHealthMessage = Field(description="health frame")
    discovery_health: UiDiscoveryHealthMessage = Field(description="discovery_health frame")
    workspace_stopped: UiWorkspaceStoppedMessage = Field(description="workspace_stopped frame")
    open_help: UiOpenHelpMessage = Field(description="open_help frame")
    workspace_refresh: UiWorkspaceRefreshMessage = Field(description="workspace_refresh frame")
    reload_ui: UiReloadMessage = Field(description="reload_ui frame")
    client_state: UiClientStateMessage = Field(description="client_state frame (client to server)")
    bootstrap: UiBootstrap = Field(description="bootstrap document")
