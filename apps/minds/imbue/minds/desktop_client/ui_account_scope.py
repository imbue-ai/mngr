"""Scoping the `/ui/ws` frames and the SPA's URLs to one account per window.

Every window shows exactly one account: a SuperTokens user id, or ``local`` for
the not-signed-in device. The publisher derives the install's state once per
pass and projects it for each connected account with the functions here; the
bootstrap and the switch-account route resolve which account a URL belongs to
with the helpers at the bottom. Everything is pure, so the projection and the
URL rules are tested without a channel or an app.
"""

import re
from collections import Counter
from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Final
from urllib.parse import parse_qs

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.ui_models import AccountPanelState
from imbue.minds.desktop_client.ui_models import UiAccountProvidersEntry
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiSnapshot
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdatesMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage

# The hub routes carry the account in their first segment: /account/<account_id>/...
ACCOUNT_HUB_PATH_PREFIX: Final[str] = "/account/"
# The query parameter an overlay route carries so a reload keeps its window's account.
ACCOUNT_QUERY_PARAMETER: Final[str] = "account"
# A SuperTokens user id (a UUID) or the device's pseudo-account id.
_ACCOUNT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HUB_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/account/([^/?#]+)(?:/|$)")
# The SPA routes that name a workspace, whose account is the workspace's association.
_WORKSPACE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^/(?:workspace|popout|destroying)/((?:agent|host)-[a-f0-9]+)(?:/|$)|^/agents/((?:agent|host)-[a-f0-9]+)/recovery(?:/|$)",
    re.IGNORECASE,
)
_CREATING_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/creating/(create-attempt-[a-f0-9]+)(?:/|$)", re.IGNORECASE)
# The start flow runs on the device, whatever is signed in.
_DEVICE_PATHS: Final[frozenset[str]] = frozenset({"/start", "/consent"})


@pure
def is_valid_account_id(account_id: str) -> bool:
    return _ACCOUNT_ID_RE.match(account_id) is not None


@pure
def account_hub_route(account_id: str) -> str:
    """The home page of an account: ``/account/<account_id>/``."""
    return f"{ACCOUNT_HUB_PATH_PREFIX}{account_id}/"


@pure
def account_id_of_workspace_entry(entry: UiWorkspaceEntry) -> str:
    """The account a workspace row belongs to: its association, else the device."""
    return entry.account_user_id or LOCAL_PROFILE_USER_ID


class AccountWorkspaceScope(FrozenModel):
    """Which workspaces one account's windows hear about, from the install's workspace list."""

    account_id: str = Field(description="The account the scope is for")
    own_agent_ids: frozenset[str] = Field(description="The rows listed under the account")
    foreign_agent_ids: frozenset[str] = Field(description="The rows listed under some other account")

    def includes(self, agent_id: str) -> bool:
        """Whether a frame about ``agent_id`` reaches the account's windows.

        A workspace no row claims (one between discovery passes, or known only to
        a producer) is not evidence of another account's ownership, so its frames
        reach every window rather than none.
        """
        return agent_id not in self.foreign_agent_ids


@pure
def account_workspace_scope(workspaces: UiWorkspacesMessage, account_id: str) -> AccountWorkspaceScope:
    own: set[str] = set()
    foreign: set[str] = set()
    for entry in workspaces.workspaces:
        if account_id_of_workspace_entry(entry) == account_id:
            own.add(entry.id)
        else:
            foreign.add(entry.id)
    return AccountWorkspaceScope(
        account_id=account_id, own_agent_ids=frozenset(own), foreign_agent_ids=frozenset(foreign)
    )


@pure
def project_workspaces(message: UiWorkspacesMessage, account_id: str) -> UiWorkspacesMessage:
    """The account's rows alone, named as its, with every account's row count.

    The install-wide id lists stay, since restore and destroy span accounts.
    """
    kept = tuple(entry for entry in message.workspaces if account_id_of_workspace_entry(entry) == account_id)
    kept_ids = {entry.id for entry in kept}
    return message.model_copy_update(
        to_update(message.field_ref().account_id, account_id),
        to_update(message.field_ref().workspaces, kept),
        to_update(
            message.field_ref().workspace_count_by_account_id,
            dict(Counter(account_id_of_workspace_entry(entry) for entry in message.workspaces)),
        ),
        to_update(
            message.field_ref().remote_workspace_states,
            {agent_id: state for agent_id, state in message.remote_workspace_states.items() if agent_id in kept_ids},
        ),
    )


@pure
def project_accounts(message: UiAccountsMessage, account_id: str) -> UiAccountsMessage:
    return message.model_copy_update(to_update(message.field_ref().account_id, account_id))


@pure
def project_providers(message: UiProvidersMessage, account_id: str) -> UiProvidersMessage:
    """The account's own section and the device's, in that order; a signed-out account with no section gets an empty one."""
    own = tuple(section for section in message.accounts if section.user_id == account_id)
    device = tuple(section for section in message.accounts if section.user_id == LOCAL_PROFILE_USER_ID)
    if account_id == LOCAL_PROFILE_USER_ID:
        sections = device
    elif own:
        sections = own + device
    else:
        sections = (
            UiAccountProvidersEntry(
                user_id=account_id,
                email="",
                state=AccountPanelState.SIGNED_OUT,
                is_connection_owner_eligible=False,
                imbue_cloud=None,
                connections=(),
            ),
        ) + device
    return message.model_copy_update(to_update(message.field_ref().accounts, sections))


@pure
def project_requests(message: UiRequestsMessage, scope: AccountWorkspaceScope) -> UiRequestsMessage:
    """The pending requests of the account's workspaces; a request whose workspace is unnamed reaches every window."""
    by_request_id = message.workspace_agent_id_by_request_id
    kept_ids = tuple(
        request_id
        for request_id in message.request_ids
        if request_id not in by_request_id or scope.includes(by_request_id[request_id])
    )
    return UiRequestsMessage(
        count=len(kept_ids),
        request_ids=kept_ids,
        workspace_agent_ids=tuple(agent_id for agent_id in message.workspace_agent_ids if scope.includes(agent_id)),
        workspace_agent_id_by_request_id={
            request_id: agent_id for request_id, agent_id in by_request_id.items() if request_id in kept_ids
        },
    )


@pure
def project_notifications(message: UiNotificationsMessage, scope: AccountWorkspaceScope) -> UiNotificationsMessage:
    """The feed entries about the account's workspaces, plus its own account-level events."""
    kept = tuple(
        entry
        for entry in message.entries
        if (entry.workspace_agent_id != "" and scope.includes(entry.workspace_agent_id))
        or (entry.workspace_agent_id == "" and entry.account_id in ("", scope.account_id))
    )
    return message.model_copy_update(
        to_update(message.field_ref().entries, kept),
        to_update(message.field_ref().unresolved_count, sum(1 for entry in kept if not entry.is_resolved)),
    )


@pure
def project_health(health: tuple[UiHealthMessage, ...], scope: AccountWorkspaceScope) -> tuple[UiHealthMessage, ...]:
    return tuple(message for message in health if scope.includes(message.agent_id))


@pure
def project_workspace_updates(
    message: UiWorkspaceUpdatesMessage,
    scope: AccountWorkspaceScope,
    # The account's own resolved settings: its windows must see its update
    # window and auto-update choice, not another account's.
    update_window: str,
    is_auto_update_enabled: bool,
) -> UiWorkspaceUpdatesMessage:
    return message.model_copy_update(
        to_update(
            message.field_ref().updates,
            {agent_id: update for agent_id, update in message.updates.items() if scope.includes(agent_id)},
        ),
        to_update(message.field_ref().update_window, update_window),
        to_update(message.field_ref().is_auto_update_enabled, is_auto_update_enabled),
    )


@pure
def project_snapshot(
    snapshot: UiSnapshot,
    account_id: str,
    # The account's resolved update settings, for the updates frame's fields.
    update_window: str,
    is_auto_update_enabled: bool,
) -> UiSnapshot:
    """The install's snapshot as one account's window receives it."""
    scope = account_workspace_scope(snapshot.workspaces, account_id)
    return UiSnapshot(
        workspaces=project_workspaces(snapshot.workspaces, account_id),
        accounts=project_accounts(snapshot.accounts, account_id),
        providers=project_providers(snapshot.providers, account_id),
        requests=project_requests(snapshot.requests, scope),
        notifications=project_notifications(snapshot.notifications, scope),
        health=project_health(snapshot.health, scope),
        discovery_health=snapshot.discovery_health,
        workspace_updates=project_workspace_updates(
            snapshot.workspace_updates, scope, update_window, is_auto_update_enabled
        ),
        environment=snapshot.environment,
    )


@pure
def account_id_from_hub_path(path: str) -> str | None:
    """The account a hub route names (``/account/<id>/...``), or None for any other path."""
    match = _HUB_PATH_RE.match(path)
    if match is None:
        return None
    account_id = match.group(1)
    return account_id if is_valid_account_id(account_id) else None


@pure
def workspace_any_id_from_spa_path(path: str) -> str | None:
    """The workspace (either coordinate) a workspace-scoped SPA route names, or None."""
    match = _WORKSPACE_PATH_RE.match(path)
    if match is None:
        return None
    return match.group(1) or match.group(2)


@pure
def create_attempt_id_from_spa_path(path: str) -> str | None:
    match = _CREATING_PATH_RE.match(path)
    return match.group(1) if match is not None else None


@pure
def account_id_from_query(query: str) -> str | None:
    """The ``?account=`` an overlay route carries, when it names a valid account id."""
    values = parse_qs(query.lstrip("?")).get(ACCOUNT_QUERY_PARAMETER, [])
    return next((value for value in values if is_valid_account_id(value)), None)


def resolve_window_account_id(
    path: str,
    query: str,
    # The account whose windows list a workspace, by either workspace coordinate.
    resolve_workspace_account_id: Callable[[str], str],
    # The account a create attempt was submitted under (None for none, or an unknown attempt).
    resolve_create_attempt_account_id: Callable[[str], str | None],
    recent_account_ids: Sequence[str],
    # The account a bare route takes while no window has reported one yet.
    resolve_account_id_when_none_recorded: Callable[[], str],
) -> str:
    """The account a served URL belongs to, which seeds the window's account at first paint.

    The hub segment wins, then a named workspace's account, then a creation page's
    attempt, then the overlay's ``?account=``; the start flow is the device's, and
    anything else (``/`` included) takes the most recently used account, else
    ``resolve_account_id_when_none_recorded``'s.
    """
    hub_account_id = account_id_from_hub_path(path)
    if hub_account_id is not None:
        return hub_account_id
    workspace_any_id = workspace_any_id_from_spa_path(path)
    if workspace_any_id is not None:
        return resolve_workspace_account_id(workspace_any_id)
    create_attempt_id = create_attempt_id_from_spa_path(path)
    if create_attempt_id is not None:
        return resolve_create_attempt_account_id(create_attempt_id) or LOCAL_PROFILE_USER_ID
    query_account_id = account_id_from_query(query)
    if query_account_id is not None:
        return query_account_id
    if path.rstrip("/") in {device_path.rstrip("/") for device_path in _DEVICE_PATHS}:
        return LOCAL_PROFILE_USER_ID
    return recent_account_ids[0] if recent_account_ids else resolve_account_id_when_none_recorded()


@pure
def choose_switch_route(
    account_id: str,
    recent_workspace_ids: Sequence[str],
    enterable_workspace_ids: AbstractSet[str],
    workspace_ids_shown_elsewhere: AbstractSet[str],
) -> str:
    """Where a window switching to ``account_id`` lands: its most recent free workspace, else its hub.

    ``recent_workspace_ids`` is the account's workspaces in the order its windows
    last opened them; ``enterable_workspace_ids`` the live rows its windows list
    right now (a remote tile cannot be entered); ``workspace_ids_shown_elsewhere``
    what other connected windows currently show.
    """
    for workspace_id in recent_workspace_ids:
        if workspace_id in enterable_workspace_ids and workspace_id not in workspace_ids_shown_elsewhere:
            return f"/workspace/{workspace_id}"
    return account_hub_route(account_id)
