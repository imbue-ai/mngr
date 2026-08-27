"""Abstract handler for a single permission-request kind.

The desktop client supports multiple kinds of pending requests (sharing,
latchkey-permission, ...). Each is rendered, granted, and denied through a
type-specific ``RequestEventHandler`` so the route layer can stay a thin
dispatcher: it authenticates, looks up the pending request by id, picks the
handler that claims the request's wire ``request_type``, and forwards the
rest of the work.

Adding a new request kind is now a matter of writing a new
``RequestEventHandler`` subclass and registering it with the desktop
client; no churn in ``app.py`` is required.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import Literal

from flask import Request
from flask import Response
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.ui_models import UiPermissionGrantGroup
from imbue.mngr_latchkey.credential_commands import CredentialCommandParameter


class UiManualCredentialsPrompt(FrozenModel):
    """The credential form shown for a service that cannot be signed in to through a browser."""

    parameters: tuple[CredentialCommandParameter, ...] = Field(
        description=(
            "One labeled input per value the service's credential command needs. Empty when Minds cannot "
            "work out what to ask for, in which case the dialog shows the message as an error and offers "
            "no Approve."
        ),
    )
    message: str = Field(description="Instruction (or, after a failed attempt, the reason) shown above the inputs")


class UiPermissionAccountChoice(FrozenModel):
    """One selectable account in the predefined-permission detail payload."""

    value: str = Field(description="Form value: latchkey account key ('' = unnamed default) or the new-account value")
    label: str = Field(description="User-facing account label")
    hint: str = Field(default="", description="Short qualifier shown next to the label (e.g. 'needs sign-in')")
    is_credential_setup_needed: bool = Field(
        default=False,
        description="Whether picking this account has to establish credentials before the grant can apply",
    )
    is_account_name_needed: bool = Field(
        default=False,
        description="Whether picking this account also requires the user to name it (manual-credentials services)",
    )


class UiWorkspaceVerbChoice(FrozenModel):
    """One grantable minds-workspaces verb in the workspace-permission detail payload."""

    permission: str = Field(description="Detent permission-schema name submitted by the form")
    display_name: str = Field(description="Human-readable label")
    description: str = Field(description="Plain-English summary of what the verb allows")
    is_targeted: bool = Field(description="Whether the verb is scoped to a target workspace id")


class UiPredefinedPermissionDetail(FrozenModel):
    """Inbox detail payload for a predefined (catalog-backed) permission request."""

    kind: Literal["predefined"] = "predefined"
    request_id: str = Field(description="Request event id (grant/deny routes key on it)")
    agent_id: str = Field(description="Requesting agent id")
    ws_name: str = Field(description="Workspace display name")
    rationale: str = Field(description="Agent's stated reason for the request")
    scope: str = Field(description="Detent scope schema (e.g. 'slack-api')")
    display_name: str = Field(description="Service display name for the dialog header")
    service_name: str = Field(
        description=(
            "Catalog service name whose brand mark leads the dialog header (e.g. 'slack'). Empty when the "
            "scope resolves to no catalog service, in which case the header draws its fallback glyph."
        )
    )
    permission_groups: tuple[UiPermissionGrantGroup, ...] = Field(
        description="Every grantable permission under the scope, grouped: full access first, the wildcard last"
    )
    checked_permissions: tuple[str, ...] = Field(description="Schemas to pre-check")
    account_choices: tuple[UiPermissionAccountChoice, ...] = Field(description="Accounts the grant can attach to")
    selected_account_value: str = Field(description="Preselected account choice value")
    new_account_value: str = Field(description="Form value of the sign-a-new-account-in choice")
    wildcard_permission: str = Field(description="The catch-all permission's submitted value (e.g. 'any')")
    will_open_browser: bool = Field(description="Whether approving is expected to pop a browser sign-in")
    manual_credentials: UiManualCredentialsPrompt | None = Field(
        description=(
            "The credential form to show while an account that needs credential setup is selected. None when "
            "the service signs in through a browser, so no form is ever needed."
        ),
    )


class UiFileSharingPermissionDetail(FrozenModel):
    """Inbox detail payload for a file-sharing permission request."""

    kind: Literal["file_sharing"] = "file_sharing"
    request_id: str = Field(description="Request event id")
    agent_id: str = Field(description="Requesting agent id")
    ws_name: str = Field(description="Workspace display name")
    rationale: str = Field(description="Agent's stated reason for the request")
    file_path: str = Field(description="Absolute path the agent asked for")
    access: str = Field(description="Requested access mode (READ or WRITE), verbatim")
    access_human_label: str = Field(description="Human rendering of the access mode ('read-only' / 'read & write')")
    allowed_roots: tuple[str, ...] = Field(description="Absolute WebDAV mount roots a shareable path must be under")
    home_dir: str = Field(description="Absolute home directory used to expand a leading '~'")


class UiWorkspacePermissionDetail(FrozenModel):
    """Inbox detail payload for a cross-workspace (minds-workspaces) permission request."""

    kind: Literal["workspace"] = "workspace"
    request_id: str = Field(description="Request event id")
    agent_id: str = Field(description="Requesting agent id")
    ws_name: str = Field(description="Workspace display name")
    rationale: str = Field(description="Agent's stated reason for the request")
    verbs: tuple[UiWorkspaceVerbChoice, ...] = Field(description="The grantable verbs, in dialog order")
    checked_permissions: tuple[str, ...] = Field(description="Verb schemas to pre-check")
    target_workspace_id: str | None = Field(description="Workspace the targeted verbs act on, when named")
    target_workspace_name: str | None = Field(description="Display name of the target workspace, when resolvable")
    show_target_choice: bool = Field(description="Whether to offer the all-vs-selected target radio")


class UiAccountsPermissionDetail(FrozenModel):
    """Inbox detail payload for an accounts permission request (all-or-nothing approve)."""

    kind: Literal["accounts"] = "accounts"
    request_id: str = Field(description="Request event id")
    agent_id: str = Field(description="Requesting agent id")
    ws_name: str = Field(description="Workspace display name")
    rationale: str = Field(description="Agent's stated reason for the request")


class UiUnknownScopeDetail(FrozenModel):
    """Deny-only detail for a predefined request whose scope is not in the catalog."""

    kind: Literal["unknown_scope"] = "unknown_scope"
    request_id: str = Field(description="Request event id")
    scope: str = Field(description="The unrecognized scope the agent asked for")


class UiUnsupportedDetail(FrozenModel):
    """Fallback detail when a handler is asked about an event type it does not own."""

    kind: Literal["unsupported"] = "unsupported"
    message: str = Field(description="Short explanation for the UI")


# The typed inbox-detail payload: exactly one shape per request kind (plus the
# unknown-scope and wrong-type fallbacks), discriminated by ``kind``.
RequestDetailPayload = (
    UiPredefinedPermissionDetail
    | UiFileSharingPermissionDetail
    | UiWorkspacePermissionDetail
    | UiAccountsPermissionDetail
    | UiUnknownScopeDetail
    | UiUnsupportedDetail
)


class RequestEventHandler(MutableModel, ABC):
    """Per-request-kind handler for the request inbox flow.

    Each implementation owns building the typed request detail payload,
    applying a grant, applying a deny, and providing the human-readable
    labels the inbox list uses to describe pending requests of its
    kind. The route layer guarantees that ``permission_request.request_type``
    matches ``handles_request_type()`` before calling any of the
    methods below, so subclasses may safely narrow ``permission_request.payload``
    to their concrete payload type.
    """

    @abstractmethod
    def handles_request_type(self) -> str:
        """Return the wire ``request_type`` this handler claims (e.g. ``"predefined"``)."""

    @abstractmethod
    def kind_label(self) -> str:
        """Short, lower-case label shown on inbox list cards (e.g. ``"sharing"``)."""

    @abstractmethod
    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        """Human-readable secondary label for the inbox list card.

        Typically the friendly service name (e.g. ``"Slack"`` rather than
        ``"slack"``). Falls back to whatever raw identifier the event
        carries when no nicer label is available.
        """

    @abstractmethod
    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        """Build the typed inbox-detail payload for the SPA's right pane.

        The SPA renders the dialog client-side and submits the same form
        fields to the grant/deny routes, so the values here must stay in
        lockstep with what :meth:`apply_grant_request` parses.
        """

    @abstractmethod
    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        """Apply a grant from ``POST /requests/{id}/grant`` and return the response.

        Implementations are responsible for parsing any form body, doing
        the underlying work (rewriting permission files, enabling
        sharing, ...), appending the corresponding response event to the
        inbox, and producing whatever response shape the originating UI
        expects (JSON for JS-driven dialogs, 303 redirects for plain
        form posts -- the route layer is agnostic).
        """

    @abstractmethod
    def apply_deny_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        """Apply a deny from ``POST /requests/{id}/deny`` and return the response.

        Same contract as :meth:`apply_grant_request`, minus the underlying
        grant work: the handler still appends the ``DENIED`` response
        event so the request stops appearing as pending.
        """


def find_handler_for_event(
    handlers: Sequence[RequestEventHandler],
    permission_request: StreamedPermissionRequest,
) -> RequestEventHandler | None:
    """Return the handler that claims ``permission_request.request_type``, or ``None``.

    There is at most one handler per request type by construction (the
    desktop client builds the tuple from a fixed set of handlers); if
    two ever claimed the same type, the first registered one wins.
    """
    for handler in handlers:
        if handler.handles_request_type() == permission_request.request_type:
            return handler
    return None
