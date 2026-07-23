"""Typed contract for the chrome data pushed over ``/_chrome/events`` (SSE)
and seeded into page boot-state islands.

These models ARE the chrome data contract: the SSE route in ``app.py`` and the
page render handlers both build their payloads through them, so the boot
island and the SSE stream can never drift apart. The wire format is frozen --
each ``to_payload_dict()`` reproduces the exact key order and
present/absent/null semantics of the historical hand-built dicts
(``chrome_state_test.py`` pins this byte-for-byte).

The TypeScript mirror of these shapes lives in
``apps/minds/frontend/src/chrome_state.ts``; keep the two files in sync when
the contract changes.
"""

from enum import auto
from typing import Any
from typing import Literal

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure


class ChromeProviderStatus(LowerCaseStrEnum):
    """Panel status bucket for a provider entry (lowercase on the wire)."""

    OK = auto()
    ERROR = auto()
    DISABLED = auto()


class ChromeWorkspaceEntry(FrozenModel):
    """One workspace row in the ``workspaces`` SSE payload.

    Optional fields are ABSENT from the wire dict when unset (never null) --
    the ES5 consumers test presence with plain truthiness. Local entries may
    carry ``is_stale`` / ``supports_shutdown`` / ``liveness``; entries known
    only from synced records carry ``is_remote`` / ``location`` instead.
    """

    id: str = Field(description="The workspace's agent id")
    name: str = Field(description="Display name (workspace name, agent name, or the id as fallback)")
    accent: str = Field(description="The workspace accent as a ``#rrggbb`` CSS color")
    is_stale: str | None = Field(
        default=None, description='"true" when the workspace\'s provider had a discovery error; absent otherwise'
    )
    supports_shutdown: str | None = Field(
        default=None, description='"true" when the mind\'s host can stop/start (docker/lima); absent otherwise'
    )
    liveness: str | None = Field(
        default=None, description="RUNNING / STOPPED / UNKNOWN for shutdown-capable minds; absent otherwise"
    )
    provider: str | None = Field(
        default=None,
        description="Friendly compute-provider label (e.g. AWS) for the landing row chip; absent on remote rows",
    )
    is_remote: str | None = Field(
        default=None, description='"true" for workspaces known only from synced records; absent otherwise'
    )
    location: str | None = Field(
        default=None, description="Where a remote workspace lives (device label / provider kind); absent on local rows"
    )
    host_id: str | None = Field(
        default=None, description="The synced record's host id (the remove-record handle); remote rows only"
    )
    state_detail: str | None = Field(
        default=None, description="Detail for a remote tile's 'error' state (the tooltip text); absent otherwise"
    )
    account: str | None = Field(
        default=None, description="Owning account email when known; absent for account-less workspaces"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ChromeWorkspacesPayload(FrozenModel):
    """The ``workspaces`` SSE event.

    The connect-time snapshot additionally carries ``has_accounts`` and
    ``restorable_workspace_ids``; later diff-driven pushes omit both (absent,
    not null).
    """

    type: Literal["workspaces"] = Field(default="workspaces", description="SSE event discriminator")
    workspaces: tuple[ChromeWorkspaceEntry, ...] = Field(description="Every known workspace row, local then remote")
    destroying_agent_ids: tuple[str, ...] = Field(
        description="Agent ids in any in-flight / failed destroy state (so a vanished id is not treated as lost)"
    )
    destroying_status_by_agent_id: dict[str, str] = Field(
        description='agent_id -> "running" | "failed" for the same destroy records (the landing row chip)'
    )
    has_accounts: bool | None = Field(
        default=None, description="Whether any account is signed in; connect-time snapshot only, absent on updates"
    )
    restorable_workspace_ids: tuple[str, ...] | None = Field(
        default=None,
        description="Agent ids the shell may restore windows to; connect-time snapshot only, absent on updates",
    )
    remote_workspace_states: dict[str, str] = Field(
        description="agent_id -> derived access state for every remote tile (drift signal for the landing page)"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ChromeProviderEntry(FrozenModel):
    """One provider row in the ``providers_state`` SSE payload."""

    name: str = Field(description="Provider instance name")
    backend: str | None = Field(description="Provider backend name; null (present) for errored/disabled entries")
    status: ChromeProviderStatus = Field(description="Panel status bucket")
    is_enabled: bool = Field(description="Whether the provider is enabled in minds' settings")
    error_type: str | None = Field(
        default=None, description="Discovery error type name; only present when status is 'error'"
    )
    error_message: str | None = Field(
        default=None, description="Discovery error message; only present when status is 'error'"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        # ``backend`` serializes as null when None (the wire always carries the
        # key); the error fields are absent entirely on non-error entries.
        omitted: set[str] = set()
        if self.error_type is None:
            omitted.add("error_type")
        if self.error_message is None:
            omitted.add("error_message")
        return self.model_dump(mode="json", exclude=omitted)


class ChromeProvidersPayload(FrozenModel):
    """The ``providers_state`` SSE event (the landing page's providers panel)."""

    type: Literal["providers_state"] = Field(default="providers_state", description="SSE event discriminator")
    providers: tuple[ChromeProviderEntry, ...] = Field(description="Panel entries, alphabetical by name")
    last_event_at: str | None = Field(description="ISO timestamp of the last discovery event; null when unknown")
    last_full_snapshot_at: str | None = Field(
        description="ISO timestamp of the last full discovery snapshot; null when unknown"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "providers": [entry.to_payload_dict() for entry in self.providers],
            "last_event_at": self.last_event_at,
            "last_full_snapshot_at": self.last_full_snapshot_at,
        }


class ChromeRequestCard(FrozenModel):
    """One pending-request card in the inbox's left list."""

    id: str = Field(description="The request event id")
    kind_label: str = Field(description="Handler-provided request kind (e.g. 'permission request')")
    ws_name: str = Field(description="The requesting workspace's display name")
    display_name: str = Field(description="Handler-provided one-line summary of the request")
    accent: str = Field(description="The workspace accent hex (mirrors the homepage tile's color)")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ChromeRequestsPayload(FrozenModel):
    """The ``requests`` SSE event (pending permission-request inbox summary)."""

    type: Literal["requests"] = Field(default="requests", description="SSE event discriminator")
    count: int = Field(description="Number of displayable pending requests (the badge number)")
    request_ids: tuple[str, ...] = Field(
        description="Pending request event ids in deterministic order (the panel-refresh diff signal)"
    )
    cards: tuple[ChromeRequestCard, ...] = Field(
        description="Card summaries for the inbox's left list, most-recent-first (same order as request_ids)"
    )
    auto_open: bool = Field(description="Whether the shell should auto-open the panel on new requests")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ChromeSystemInterfaceStatusPayload(FrozenModel):
    """The ``system_interface_status`` SSE event (per-workspace health edge or re-assert)."""

    type: Literal["system_interface_status"] = Field(
        default="system_interface_status", description="SSE event discriminator"
    )
    agent_id: str = Field(description="The workspace whose system-interface health changed")
    status: str = Field(description="AgentHealth value (healthy / stuck / restarting / restart_failed)")
    error: str | None = Field(
        default=None, description="Failure reason; only present when status is restart_failed and a reason is known"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class LandingBootExtras(FrozenModel):
    """Landing-page-specific boot island data, a sibling of the chrome
    snapshot in the ``landing`` island key (everything row-level rides the
    chrome workspaces payload instead, so live pushes keep rows complete)."""

    mngr_forward_origin: str = Field(description="Bare origin of the mngr forward plugin (workspace links target it)")
    account_email: str = Field(description="The bottom-left launcher's account email; empty renders 'Log in'")
    extra_account_count: int = Field(description="How many further accounts are signed in (the '(+N)' suffix)")
    locked_account_emails: tuple[str, ...] = Field(
        description="Accounts whose synced secrets exist but whose key is absent here (the unlock banner)"
    )
    is_discovering: bool = Field(
        description="Initial discovery still running with no rows yet (the 'Discovering agents...' state)"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AssociateAccountPayload(FrozenModel):
    """One signed-in account offered by the associate-workspace prompt."""

    user_id: str = Field(description="The account's user id (the PATCH account_id value)")
    email: str = Field(description="The account's email address (the option label)")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SharingBootExtras(FrozenModel):
    """Sharing-editor boot island data (the ``sharing`` island key).

    Unlike the landing/inbox islands there is no ``chrome`` sibling: the
    editor's state comes from its own JSON status endpoint
    (``GET /api/v1/workspaces/<id>/sharing/<service>``), not the chrome SSE
    stream. Shared by the full /sharing page and the Electron sharing modal;
    ``is_modal`` selects the plain-text heading (nothing may navigate the
    overlay iframe) and the dismiss-style Cancel affordance."""

    agent_id: str = Field(description="The workspace agent id the editor manages sharing for")
    service_name: str = Field(description="The shared service (URL path segment + display code pill)")
    ws_name: str = Field(description="Workspace display name for the heading (falls back to the agent id)")
    account_email: str = Field(description="The bound account's email for the heading; empty hides it")
    initial_emails: tuple[str, ...] = Field(description="URL-proposed draft emails folded into the first load only")
    is_modal: bool = Field(description="True in the Electron overlay modal: plain-links heading + dismissing Cancel")
    mngr_forward_origin: str = Field(
        description="Bare origin of the mngr forward plugin for the page heading's workspace link; empty in the modal"
    )
    has_account: bool = Field(
        default=True,
        description="Whether the workspace has an associated account; False renders the associate prompt instead",
    )
    associate_accounts: tuple[AssociateAccountPayload, ...] = Field(
        default=(), description="Signed-in accounts offered by the associate prompt (when has_account is False)"
    )
    redirect_url: str = Field(
        default="", description="Where a successful association returns to; empty reloads in place"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class InboxBootExtras(FrozenModel):
    """Inbox-page-specific boot island data (the ``inbox`` sibling of the
    chrome snapshot)."""

    selected_id: str = Field(description="The initially-selected request id (empty for none)")
    keep_open: bool = Field(description="True only for an intentional whole-inbox open; false dismisses on resolution")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CreatingBootExtras(FrozenModel):
    """Creating-page boot island data (the ``creating`` island key).

    Like the sharing island there is no ``chrome`` sibling: the page's live
    state comes from the create-operation status endpoint
    (``GET /api/v1/workspaces/operations/create/<id>``) and its SSE log
    stream, not the chrome SSE stream."""

    agent_id: str = Field(
        description="The creation id (minds-internal in-flight handle) the status poll and log stream are keyed by"
    )
    status_text: str = Field(description="Server-resolved caption for the current creation status (first paint)")
    expected_duration_seconds: float = Field(
        description="Expected wall-clock creation duration; drives the time-based progress bar easing"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DestroyingBootExtras(FrozenModel):
    """Destroying-page boot island data (the ``destroying`` island key).

    No ``chrome`` sibling: the page's live state comes from the
    destroy-operation status endpoint
    (``GET /api/v1/workspaces/operations/destroy/<agent_id>``) and its SSE
    log stream, not the chrome SSE stream."""

    agent_id: str = Field(description="The workspace agent id being destroyed (keys the operation endpoints)")
    agent_name: str = Field(description="Workspace display name for the page heading")
    pid: int = Field(description="The destroy worker's pid, shown in the heading's helper line")
    status: str = Field(description="Initial server-computed operation status: running / failed / done")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ConsentBootExtras(FrozenModel):
    """Error-reporting consent page boot island data (the ``consent`` key).

    The two checkbox states seed the form; no ``chrome`` sibling (the page
    only POSTs /consent and reloads)."""

    report_unexpected_errors: bool = Field(description="Initial state of the report-unexpected-errors toggle")
    include_logs: bool = Field(description="Initial state of the include-logs toggle")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AuthErrorBootExtras(FrozenModel):
    """Authentication-failure page boot island data (the ``auth_error`` key)."""

    message: str = Field(description="The failure explanation shown under the heading")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SettingsBootExtras(FrozenModel):
    """App-level settings page boot island data (the ``settings`` island key).

    Shared by the full /settings page and the centered settings modal
    (``is_modal`` selects the card/backdrop chrome). The three overview
    tuples carry the ``model_dump(mode="json")`` payloads of the
    permission-overview models
    (:mod:`~imbue.minds.desktop_client.latchkey.permission_overview`:
    ``ServicePermissionOverview`` / ``WorkspaceFileSharingGrant`` /
    ``WorkspaceDelegationGrant``) -- those models are the source of the wire
    shape, mirrored as TypeScript interfaces in ``chrome_state.ts``. They are
    not imported here because this contract module must stay free of the
    latchkey dependency tree."""

    report_unexpected_errors: bool = Field(description="Initial state of the report-unexpected-errors toggle")
    include_error_logs: bool = Field(description="Initial state of the include-logs toggle")
    services_overview: tuple[dict[str, Any], ...] = Field(
        description="Serialized ServicePermissionOverview models (Connectors section)"
    )
    file_sharing_grants: tuple[dict[str, Any], ...] = Field(
        description="Serialized WorkspaceFileSharingGrant models (Local files section)"
    )
    workspace_delegation_grants: tuple[dict[str, Any], ...] = Field(
        description="Serialized WorkspaceDelegationGrant models (Workspaces section)"
    )
    permissions_unavailable: bool = Field(
        description="True when the latchkey gateway could not be reached to read grants"
    )
    is_master_password_set: bool = Field(
        description="Whether any signed-in account already has a non-empty sync master password"
    )
    is_modal: bool = Field(description="True for the centered modal variant (card/backdrop chrome, no back link)")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AccountEntryPayload(FrozenModel):
    """One signed-in account row in the ``accounts`` island."""

    user_id: str = Field(description="The account's user id (keys the set-default / logout actions)")
    email: str = Field(description="The account's email address")
    workspace_count: int = Field(description="How many workspaces the account owns")
    is_default: bool = Field(description="Whether this is the default account")
    is_enabled: bool = Field(
        description="Whether the account's provider block is enabled; False renders the 'Signed out' indicator"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AccountsBootExtras(FrozenModel):
    """Manage-accounts page boot island data (the ``accounts`` island key).

    Shared by the full /accounts page and the centered accounts modal."""

    accounts: tuple[AccountEntryPayload, ...] = Field(description="The signed-in accounts, in listing order")
    is_modal: bool = Field(description="True for the centered modal variant (card/backdrop chrome)")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "accounts": [account.to_payload_dict() for account in self.accounts],
            "is_modal": self.is_modal,
        }


class WorkspaceSettingsBootExtras(FrozenModel):
    """Workspace-settings page boot island data (the ``workspace_settings`` key)."""

    agent_id: str = Field(description="The workspace agent id (keys every settings API call)")
    ws_name: str = Field(description="Workspace display name (heading + rename seed)")
    current_color: str = Field(description="Stored workspace color hex (#rrggbb); pre-selects the picker")
    palette: dict[str, str] = Field(description="The pickable palette swatches as an ordered name -> hex map")
    is_stale: bool = Field(
        description="Provider-health flag; True disables rename/color (writes would not be observable)"
    )
    is_leased_imbue_cloud: bool = Field(
        description="True for hosts leased from Imbue Cloud: account association is fixed"
    )
    has_account: bool = Field(description="Whether any account is signed in (gates the Imbue Cloud backup provider)")
    current_account_email: str = Field(description="The associated account's email; empty when unassociated")
    associate_accounts: tuple[AssociateAccountPayload, ...] = Field(
        description="Signed-in accounts offered by the associate prompt (when unassociated)"
    )
    servers: tuple[str, ...] = Field(description="Discovered shareable servers, in listing order")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["associate_accounts"] = [account.to_payload_dict() for account in self.associate_accounts]
        return payload


class CreateFormBootExtras(FrozenModel):
    """Create-form page boot island data (the ``create`` island key).

    Carries the *effective* (preset-resolved) selections render_create_form
    computes, plus the option lists the selects render. The advanced selects
    are always the source of truth on submit; the preset cards just pre-fill
    them client-side."""

    git_url: str = Field(description="Repository URL/path seed (operator default or a re-rendered submission)")
    branch: str = Field(description="Branch seed (operator default or a re-rendered submission)")
    host_name: str = Field(description="Explicit workspace name seed; empty auto-names server-side")
    color: str = Field(description="Auto-chosen color hex carried in the create request (no visible picker)")
    launch_modes: tuple[str, ...] = Field(description="Compute provider option values, in enum order")
    selected_launch_mode: str = Field(description="The effective compute provider selection")
    ai_providers: tuple[str, ...] = Field(description="AI provider option values, in enum order")
    selected_ai_provider: str = Field(description="The effective AI provider selection")
    docker_runtimes: tuple[str, ...] = Field(description="Container runtime option values, in enum order")
    selected_docker_runtime: str = Field(description="The platform-default (or submitted) container runtime")
    backup_providers: tuple[str, ...] = Field(description="Backup provider option values, in enum order")
    selected_backup_provider: str = Field(description="The effective backup provider selection")
    backup_api_key_env: str = Field(description="Restic env seed; empty renders the annotated example")
    accounts: tuple[AssociateAccountPayload, ...] = Field(description="Signed-in accounts for the picker")
    default_account_id: str = Field(description="The pre-selected account id; empty selects 'No account'")
    anthropic_api_key: str = Field(description="API-key seed for a re-rendered submission")
    error_message: str = Field(description="Server-side validation error from a submitted form; empty hides it")
    region_options_by_launch_mode: dict[str, list[str]] = Field(
        description="Region options per compute provider (absent/empty hides the region select)"
    )
    region_selected_by_launch_mode: dict[str, str] = Field(description="The pre-selected region per compute provider")
    selected_preset: str = Field(description="Which preset card starts selected: remote / local")
    start_advanced: bool = Field(description="Open the advanced view on first paint (submit-error re-render)")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["accounts"] = [account.to_payload_dict() for account in self.accounts]
        return payload


class HelpBootExtras(FrozenModel):
    """Get-help modal boot island data (the ``help`` island key)."""

    include_logs_setting: bool = Field(
        description="Persistent include-logs preference; True always attaches logs and hides the one-off checkbox"
    )
    workspace_agent_id: str = Field(
        description="The workspace the help flow was opened from; empty on a general screen"
    )
    assist_available: bool = Field(
        description="Whether the have-an-agent-help option is offered (reachable/healthy workspace only)"
    )
    description: str = Field(
        description="Pre-filled report text; non-empty when an in-workspace /assist agent escalated its diagnosis"
    )
    is_agent_report: bool = Field(
        description="True for the agent-escalation flow: framed as the agent's submission, no mode choice"
    )
    workspace_name: str = Field(description="Workspace display name for the agent-report framing; best-effort")

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ChromeBootState(FrozenModel):
    """A connect-time snapshot of the chrome data, for page boot-state islands.

    Rendered into ``#minds-boot-state`` by ChromeShell so converted pages can
    mount synchronously with the same data the SSE stream would deliver on
    connect. Pages may bundle page-specific extras alongside this in their own
    island slice.
    """

    workspaces: ChromeWorkspacesPayload = Field(description="The connect-time workspaces snapshot")
    providers: ChromeProvidersPayload = Field(description="The connect-time providers panel snapshot")
    requests: ChromeRequestsPayload = Field(description="The connect-time requests summary")
    system_interface_statuses: tuple[ChromeSystemInterfaceStatusPayload, ...] = Field(
        description="Current per-workspace health states (the connect-time re-assert set)"
    )

    @pure
    def to_payload_dict(self) -> dict[str, Any]:
        return {
            "workspaces": self.workspaces.to_payload_dict(),
            "providers": self.providers.to_payload_dict(),
            "requests": self.requests.to_payload_dict(),
            "system_interface_statuses": [status.to_payload_dict() for status in self.system_interface_statuses],
        }
