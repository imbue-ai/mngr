"""Typed wire models for the SPA's `/ui/ws` channel, bootstrap payload, and `/ui/api` responses.

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

The trailing section holds ``/ui/api`` request/response payloads rather than
channel frames. They live here because the generated TypeScript comes from
``UiWireSchema``, and because this module depends on nothing but pydantic --
the payload builders import it, never the other way round.
"""

from enum import auto
from typing import Annotated
from typing import Literal

from pydantic import Field
from pydantic import TypeAdapter

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.system_interface_health import AgentHealth

# Bumped on ANY breaking change to the models in this module. The server
# inlines it into the page bootstrap and sends it again in every connection's
# hello frame; the client compares the two and hard-reloads once on mismatch
# (picking up freshly served assets). That catches a server that upgraded
# while a window stayed open across a reconnect -- it cannot catch assets
# built for another version being served with a matching bootstrap, since
# both values come from the same live server.
UI_SCHEMA_VERSION: int = 6


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
    is_backend_unreachable: bool = Field(
        default=False,
        description="This machine's backend is unreachable, by the same verdict the recovery card renders",
    )
    is_device_cannot_connect: bool = Field(
        default=False,
        description="This device, not the machine, is what cannot connect -- again the card's own verdict",
    )
    provider_label: str = Field(
        default="", description="Friendly name of the provider hosting this workspace; empty when unknown"
    )
    # Whether a dead network on this device is even capable of explaining this
    # machine being unreachable. False for the on-device backends (local,
    # docker, lima), which answer over loopback with the wifi off -- the band
    # must not attribute their failures to the network, nor lead the user to a
    # card that withholds the restart that would fix them. Defaults True (and so
    # to today's behaviour) for the rows that carry no backend at all: create
    # attempts and machines hosted on another device.
    is_network_dependent: bool = Field(
        default=True, description="Whether reaching this machine requires this device to have a working network"
    )
    supports_shutdown: bool = Field(default=False, description="Whether minds can stop/start this workspace's host")
    liveness: str = Field(
        default="", description="RUNNING / STOPPED / STOPPING / STARTING / UNKNOWN when supports_shutdown, else empty"
    )
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
    """Pending-request inbox summary (badge count + exact id set)."""

    type: Literal["requests"] = "requests"
    count: int = Field(description="Number of displayable pending requests")
    request_ids: tuple[str, ...] = Field(description="Pending request event ids in deterministic order")


class NotificationOutcome(LowerCaseStrEnum):
    """How a feed entry's request resolved (the lowercase values are the wire strings).

    CLOSED is the feed's own outcome for a request that vanished without a
    recorded response (e.g. its workspace was destroyed); grant/deny
    responses only ever record APPROVED or DENIED.
    """

    APPROVED = auto()
    DENIED = auto()
    CLOSED = auto()


class UiNotificationEntry(FrozenModel):
    """One durable entry in the notification feed.

    Display fields are snapshotted at creation so a row still renders after
    the source request is gone (resolved, or its workspace destroyed).
    """

    id: str = Field(description="The request event id; unique per entry")
    kind: Literal["permission_request"] = Field(
        default="permission_request",
        description="What produced the entry; permission requests are the only kind today",
    )
    created_at: str = Field(
        description="ISO-8601 UTC timestamp of when the underlying request was filed "
        "(the request event's own timestamp, so ordering and relative times survive restarts)"
    )
    is_resolved: bool = Field(description="Whether the underlying request has been resolved")
    outcome: NotificationOutcome | None = Field(
        description="How the request resolved; None while unresolved, "
        "closed when it was auto-resolved because the request vanished (e.g. workspace destroyed)"
    )
    title: str = Field(description="Headline snapshotted at creation (matches the review dialog's)")
    body: str = Field(description="Secondary line snapshotted at creation; may be empty")
    request_id: str = Field(description="The originating request event id; opens the review flow while pending")
    workspace_agent_id: str = Field(description="Origin workspace's agent id; '' when unresolvable")
    workspace_name: str = Field(description="Origin workspace's display name, snapshotted at creation")
    workspace_accent: str = Field(description="Origin workspace's ``#rrggbb`` accent, snapshotted at creation")
    service_name: str = Field(description="Catalog service for the brand mark; '' when none")


class UiNotificationsMessage(FrozenModel):
    """Full notification-feed state (always the complete feed, never a delta).

    Wire order IS display order: unresolved entries first, then resolved,
    each newest-first.
    """

    type: Literal["notifications"] = "notifications"
    entries: tuple[UiNotificationEntry, ...] = Field(description="Every feed entry, in display order")
    unresolved_count: int = Field(description="Number of unresolved entries")
    is_snapshot: bool = Field(
        default=False,
        description="Whether this frame is the connect-time replay of current state rather than a live edge",
    )


class UiHealthMessage(FrozenModel):
    """One workspace's system-interface health state."""

    type: Literal["health"] = "health"
    agent_id: str = Field(description="Workspace agent id")
    status: AgentHealth = Field(description="Current health classification")
    error: str | None = Field(default=None, description="Last restart error, present for RESTART_FAILED")
    is_restart_a_no_op: bool = Field(
        default=False,
        description=(
            "Whether this episode's dispatched start reported it booted nothing. A row that reads "
            "RESTART_FAILED with this set has no failed restart behind it -- the machine simply never "
            "answered -- so the badge says so rather than blaming a restart that never ran."
        ),
    )
    is_restart_start_only: bool | None = Field(
        default=None,
        description=(
            "Whether an in-flight restart skips the stop step, or None outside one. A full stop+start "
            "bounce only ever comes from the user's own click, so False is what makes 'Restarting' an "
            "honest claim; anything else may no-op against a machine that is already up. Carried here "
            "because the machines list has to make the same call the recovery card does, off the same "
            "evidence -- the recovery-info route already reports it, and two surfaces reading one "
            "episode must not describe it differently."
        ),
    )
    # A client that acts on transitions has to tell a replay from a live edge,
    # and position in the frame sequence is not something the wire format
    # promises.
    is_snapshot: bool = Field(
        default=False,
        description="Whether this frame is the connect-time replay of current state rather than a live edge",
    )


class UiDiscoveryHealthMessage(FrozenModel):
    """App-global discovery-pipeline health."""

    type: Literal["discovery_health"] = "discovery_health"
    state: DiscoveryHealth = Field(description="Pipeline health bucket")


class UiEnvironmentMessage(FrozenModel):
    """This device's own condition, as one app-global fact.

    App-global on purpose: the device cannot reach anything whether or not any
    machine has been convicted yet, and an app opened on a dead network has
    nothing to convict until the user clicks into a machine. One fact beside the
    discovery health lets the hub pages say it then, and the per-machine
    surfaces scope it themselves (a machine that runs on this device is
    reachable with the wifi off, so the condition explains nothing about it).
    """

    type: Literal["environment"] = "environment"
    state: EnvironmentBlock = Field(description="Device-level condition (offline / SSH-blocked network), or NONE")


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
    """This workspace's displayed view no longer matches what the machine would serve; rebuild it.

    Two producers: an in-workspace agent POSTing to
    ``/api/v1/agents/<id>/refresh`` after changing the workspace's own interface
    (see that route for why the agent has to ask), and the system-interface
    health tracker's recovery edge, for a machine that has just started
    answering again after serving every window a dead page.
    """

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
    has_focus: bool = Field(
        default=True,
        description="Whether this window currently has OS/browser focus (resent on every focus/blur)",
    )


# Everything the server may send. The discriminator makes both pydantic
# validation and the generated TypeScript a tagged union.
UiServerMessage = Annotated[
    UiHelloMessage
    | UiWorkspacesMessage
    | UiAccountsMessage
    | UiProvidersMessage
    | UiRequestsMessage
    | UiNotificationsMessage
    | UiHealthMessage
    | UiDiscoveryHealthMessage
    | UiEnvironmentMessage
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
    notifications: UiNotificationsMessage = Field(description="Current notification feed")
    health: tuple[UiHealthMessage, ...] = Field(description="Per-workspace health states (only tracked workspaces)")
    discovery_health: UiDiscoveryHealthMessage = Field(description="Current discovery pipeline health")
    environment: UiEnvironmentMessage = Field(description="Current device-level connectivity condition")


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


# -- Per-workspace permissions payloads (GET/POST /ui/api/workspaces/<agent_id>/permissions) --
#
# Field-for-field mirrors of the ``WorkspacePermissionsView`` tree that
# ``latchkey/permission_toggles.py`` builds. They are mirrors rather than the
# engine models themselves so this module keeps its pydantic-only dependency
# set; ``ui_api_permissions`` converts by revalidating the engine model's dump,
# so a field added, renamed, or dropped upstream fails loudly instead of
# silently vanishing from the wire: ``extra=forbid`` rejects the first two,
# and no mirror field carries a default, so a dropped one fails as missing.


class UiPermissionToggle(FrozenModel):
    """One toggleable catalog permission and its current state on the workspace's host."""

    permission: str = Field(description="Detent permission schema name; the value posted back on a flip")
    label: str = Field(description="Row label derived from the permission name")
    description: str = Field(description="Plain-English summary; empty string when the catalog has none")
    is_granted: bool = Field(description="Whether the host's rule currently grants this permission")


class UiPermissionToggleGroup(FrozenModel):
    """A titled group of catalog toggles (``Full access``, ``Messages``, ...)."""

    heading: str = Field(description="Group heading, rendered as a section eyebrow")
    toggles: tuple[UiPermissionToggle, ...] = Field(description="Toggle rows in catalog order")


class UiPermissionScopePanel(FrozenModel):
    """The grouped toggles of one detent scope a service exposes."""

    scope: str = Field(description="Detent scope schema name; the rule-key half posted on a connector flip")
    heading: str = Field(description="Scope display name, shown as a divider when a service has several scopes")
    groups: tuple[UiPermissionToggleGroup, ...] = Field(description="Toggle groups, full access first, extras last")


class UiCredentialParameter(FrozenModel):
    """One value a service's credential command asks for, as an input in the pane."""

    name: str = Field(description="Placeholder name; the key the value is submitted under")
    label: str = Field(description="Label of the input the user types the value into")


class UiServiceSignIn(FrozenModel):
    """How the next account of a service is connected from Add connection.

    ``is_browser_supported`` picks the action: latchkey's browser sign-in, or
    the credential form built from ``credential_parameters``. No parameters and
    no browser flow means the service cannot be connected from here at all.
    """

    is_browser_supported: bool = Field(description="Whether connecting runs a browser sign-in")
    credential_parameters: tuple[UiCredentialParameter, ...] = Field(
        description="Inputs the credential form renders; empty for a browser sign-in and for an unusable command"
    )
    is_account_name_required: bool = Field(
        description="Whether the credential form also has to ask for a name for the new account"
    )


class UiPermissionConnection(FrozenModel):
    """One connection entry: a (service, account) pair with its toggle panels."""

    service_name: str = Field(description="Raw catalog service name; the revoke-all key")
    display_name: str = Field(description="Human-readable service label")
    account: str = Field(description="Latchkey account key ('' for the unnamed default); posted on a flip")
    account_label: str = Field(description="User-facing account label")
    is_connected: bool = Field(description="False for an account with grants but no stored credentials")
    show_account_label: bool = Field(description="Whether the nav entry needs the account label to disambiguate")
    granted_count: int = Field(description="Total permissions currently granted across the service's scopes")
    scopes: tuple[UiPermissionScopePanel, ...] = Field(description="One toggle panel per scope the service exposes")
    sign_in: UiServiceSignIn = Field(description="How connecting this service establishes its credentials")


class UiAvailableConnection(FrozenModel):
    """A catalog service with no signed-in account yet, offered by Add connection."""

    service_name: str = Field(description="Raw catalog service name")
    display_name: str = Field(description="Human-readable service label")
    sign_in: UiServiceSignIn = Field(description="How connecting this service establishes its credentials")


class UiSelfPermissionToggle(FrozenModel):
    """One ``latchkey-self`` toggle row (a shared path, or a cross-workspace verb)."""

    permission: str = Field(description="Full detent permission name; the value posted back on a flip")
    label: str = Field(description="Primary row label (the shared path, or the verb display name)")
    detail: str = Field(description="Secondary label (access level for paths, target for verbs); may be empty")
    description: str = Field(description="Plain-English summary shown under the label; may be empty")
    is_granted: bool = Field(description="Whether the ``latchkey-self`` rule currently carries the name")
    can_enable: bool = Field(description="False once the permission's schema is gone; the agent must re-request")


class UiWaitingPermissionRequest(FrozenModel):
    """One "Waiting on you" row: a pending permission request from this workspace's agents."""

    id: str = Field(description="Request event id; opens the review flow")
    title: str = Field(description="Headline matching the review dialog's (service or category name)")
    reason: str = Field(description="The agent's stated rationale; may be empty")
    service_name: str = Field(description="Catalog service whose brand mark leads the row; '' for non-service kinds")


class UiWorkspacePermissions(FrozenModel):
    """Everything the workspace Permissions pane renders, in one response.

    ``permissions_unavailable`` separates "could not load" from "nothing
    granted": when it is true every toggle collection is empty because the
    latchkey gateway (or the workspace's host) could not be reached, and the
    pane must show its notice instead of an empty state. ``waiting_requests``
    is served either way -- it comes from the local inbox, not the gateway.
    """

    host_id: str = Field(description="Host whose permissions file the toggles edit; '' when unavailable")
    connections: tuple[UiPermissionConnection, ...] = Field(description="Connected / granted (service, account) pairs")
    available_connections: tuple[UiAvailableConnection, ...] = Field(
        description="Catalog services with no account yet"
    )
    file_sharing_toggles: tuple[UiSelfPermissionToggle, ...] = Field(description="Local files (shared path) rows")
    workspace_toggles: tuple[UiSelfPermissionToggle, ...] = Field(description="Other machines (verb) rows")
    waiting_requests: tuple[UiWaitingPermissionRequest, ...] = Field(
        description="Pending permission requests from this workspace, oldest first"
    )
    permissions_unavailable: bool = Field(description="True when the permissions could not be loaded at all")


class UiConnectorToggleRequest(FrozenModel):
    """Body of POST /ui/api/workspaces/<agent_id>/permissions/connector-toggle."""

    scope: str = Field(description="Detent scope schema the permission belongs to")
    account: str = Field(description="Latchkey account key ('' for the unnamed default)")
    permission: str = Field(description="The single permission being flipped")
    enabled: bool = Field(description="The permission's new state")


class UiSelfToggleRequest(FrozenModel):
    """Body of POST /ui/api/workspaces/<agent_id>/permissions/self-toggle."""

    permission: str = Field(description="A ``minds-file-server-*`` or ``minds-workspaces-*`` permission name")
    enabled: bool = Field(description="The permission's new state")


class UiConnectorRevokeAllRequest(FrozenModel):
    """Body of POST /ui/api/workspaces/<agent_id>/permissions/connector-revoke-all."""

    service_name: str = Field(description="Catalog service whose grants are dropped for this workspace")
    account: str = Field(description="Latchkey account key ('' for the unnamed default)")


class UiConnectorDisconnectRequest(FrozenModel):
    """Body of POST /ui/api/workspaces/<agent_id>/permissions/connector-disconnect.

    Names the connection being disconnected, not the workspace it was
    disconnected from: clearing the credential is global, so the same body is
    sent whichever workspace's pane the button was pressed in, and the
    ``<agent_id>`` in the path only decides which workspace's refreshed view
    comes back. Deliberately NOT :class:`UiConnectorRevokeAllRequest` despite
    the identical fields -- that one drops this machine's grants and leaves the
    account connected, and the generated TypeScript name is what the call site
    reads.
    """

    service_name: str = Field(description="Catalog service the account is disconnected from, on every machine")
    account: str = Field(description="Latchkey account key ('' for the unnamed default)")


class UiConnectCredentialsRequest(FrozenModel):
    """Body of POST /ui/api/workspaces/<agent_id>/permissions/connect-credentials.

    Carries what the user typed into the credential form of a service latchkey
    cannot sign in to through a browser, so it must never be logged.
    """

    service_name: str = Field(description="Catalog service the credentials belong to")
    value_by_parameter_name: dict[str, str] = Field(
        description="Value typed for each parameter of the service's credential command, keyed by its name"
    )
    account_name: str = Field(
        default="",
        description="Name for the account the credentials create; ignored for a service's first account",
    )


# -- Grant-dialog permission rows (nested in the inbox detail payload) --
#
# The same classification the Permissions pane's toggles use, so one service
# reads the same way in the pane and in the dialog that grants it.


class UiPermissionGrantRow(FrozenModel):
    """One permission the grant dialog offers, with its display strings precomputed."""

    permission: str = Field(description="Detent permission schema name; the value the grant form submits")
    label: str = Field(description="Row label; the dialog never shows the schema name, not even for the wildcard")
    description: str = Field(description="Plain-English summary from the catalog; empty when it has none")
    is_wildcard: bool = Field(description="Whether this is the catch-all, exclusive with every specific permission")


class UiPermissionGrantGroup(FrozenModel):
    """A titled group of grant-dialog rows (``Full access``, ``Messages``, ...)."""

    heading: str = Field(description="Group heading, rendered as a section eyebrow")
    is_extras: bool = Field(description="Whether this is the trailing wildcard group, rendered behind a divider")
    rows: tuple[UiPermissionGrantRow, ...] = Field(description="Rows in catalog order")


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
    notifications: UiNotificationsMessage = Field(description="notifications frame")
    health: UiHealthMessage = Field(description="health frame")
    discovery_health: UiDiscoveryHealthMessage = Field(description="discovery_health frame")
    environment: UiEnvironmentMessage = Field(description="environment frame")
    workspace_stopped: UiWorkspaceStoppedMessage = Field(description="workspace_stopped frame")
    open_help: UiOpenHelpMessage = Field(description="open_help frame")
    workspace_refresh: UiWorkspaceRefreshMessage = Field(description="workspace_refresh frame")
    reload_ui: UiReloadMessage = Field(description="reload_ui frame")
    client_state: UiClientStateMessage = Field(description="client_state frame (client to server)")
    bootstrap: UiBootstrap = Field(description="bootstrap document")
    workspace_permissions: UiWorkspacePermissions = Field(description="workspace permissions payload")
    connector_toggle: UiConnectorToggleRequest = Field(description="connector-toggle request body")
    self_toggle: UiSelfToggleRequest = Field(description="self-toggle request body")
    connector_revoke_all: UiConnectorRevokeAllRequest = Field(description="connector-revoke-all request body")
    connector_disconnect: UiConnectorDisconnectRequest = Field(description="connector-disconnect request body")
    connect_credentials: UiConnectCredentialsRequest = Field(description="connect-credentials request body")
    permission_grant_group: UiPermissionGrantGroup = Field(description="one grant-dialog permission group")
