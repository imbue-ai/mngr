"""Unit tests for scoping the channel's frames and the SPA's URLs to one account per window."""

import pytest

from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.environment_signals import EnvironmentCondition
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.ui_account_scope import account_hub_route
from imbue.minds.desktop_client.ui_account_scope import account_id_from_hub_path
from imbue.minds.desktop_client.ui_account_scope import account_id_from_query
from imbue.minds.desktop_client.ui_account_scope import choose_switch_route
from imbue.minds.desktop_client.ui_account_scope import project_snapshot
from imbue.minds.desktop_client.ui_account_scope import resolve_window_account_id
from imbue.minds.desktop_client.ui_models import AccountPanelState
from imbue.minds.desktop_client.ui_models import NotificationKind
from imbue.minds.desktop_client.ui_models import ProviderPanelStatus
from imbue.minds.desktop_client.ui_models import RemoteTileState
from imbue.minds.desktop_client.ui_models import UiAccountProvidersEntry
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiEnvironmentMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiProviderEntry
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiSnapshot
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdatesMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability

ALICE = "user-alice"
BOB = "user-bob"
CAROL = "user-carol"


def _entry(agent_id: str, account_user_id: str = "") -> UiWorkspaceEntry:
    return UiWorkspaceEntry(id=agent_id, name=agent_id, accent="#112233", account_user_id=account_user_id)


def _notification(
    entry_id: str, workspace_agent_id: str, account_id: str = "", is_resolved: bool = False
) -> UiNotificationEntry:
    return UiNotificationEntry(
        id=entry_id,
        kind=NotificationKind.SYSTEM_EVENT,
        created_at="2026-10-25T00:00:00+00:00",
        is_resolved=is_resolved,
        outcome=None,
        title="t",
        body="b",
        request_id="",
        workspace_agent_id=workspace_agent_id,
        account_id=account_id,
        workspace_name="w",
        workspace_accent="#112233",
        service_name="",
    )


def _section(user_id: str, state: AccountPanelState = AccountPanelState.SIGNED_IN) -> UiAccountProvidersEntry:
    return UiAccountProvidersEntry(
        user_id=user_id,
        email=f"{user_id}@example.com" if user_id != LOCAL_PROFILE_USER_ID else "",
        state=state,
        is_connection_owner_eligible=False,
        imbue_cloud=None,
        connections=(),
    )


def _update() -> UiWorkspaceUpdate:
    return UiWorkspaceUpdate(
        availability=UpdateAvailability.OUT_OF_DATE,
        current_version="minds-v0.1.0",
        supported_version="minds-v0.2.0",
        activity=UpdateActivity.IDLE,
    )


def _install_snapshot() -> UiSnapshot:
    """An install with Alice's, Bob's, and the device's rows, plus one request and one update per workspace."""
    return UiSnapshot(
        workspaces=UiWorkspacesMessage(
            workspaces=(_entry("agent-a1", ALICE), _entry("agent-b1", BOB), _entry("agent-d1")),
            destroying_agent_ids=("agent-b1",),
            restorable_workspace_ids=("agent-a1", "agent-b1", "agent-d1"),
            remote_workspace_states={
                "agent-a1": RemoteTileState(state="locked"),
                "agent-b1": RemoteTileState(state=""),
            },
        ),
        accounts=UiAccountsMessage(account_id=LOCAL_PROFILE_USER_ID, has_accounts=True, accounts=()),
        providers=UiProvidersMessage(
            accounts=(_section(ALICE), _section(BOB), _section(LOCAL_PROFILE_USER_ID)),
            device_providers=(UiProviderEntry(name="docker", backend="docker", status=ProviderPanelStatus.OK),),
            last_event_at=None,
            last_full_snapshot_at=None,
        ),
        requests=UiRequestsMessage(
            count=3,
            request_ids=("req-a", "req-b", "req-unknown"),
            workspace_agent_ids=("agent-a1", "agent-b1"),
            workspace_agent_id_by_request_id={"req-a": "agent-a1", "req-b": "agent-b1"},
        ),
        notifications=UiNotificationsMessage(
            entries=(
                _notification("n-a", "agent-a1"),
                _notification("n-b", "agent-b1", is_resolved=True),
                _notification("n-alice-level", "", account_id=ALICE),
                _notification("n-unclaimed", "agent-zz"),
                _notification("n-install-wide", ""),
            ),
            unresolved_count=4,
        ),
        health=(
            UiHealthMessage(agent_id="agent-a1", status=AgentHealth.STUCK),
            UiHealthMessage(agent_id="agent-b1", status=AgentHealth.STUCK),
        ),
        discovery_health=UiDiscoveryHealthMessage(state=DiscoveryHealth.HEALTHY),
        workspace_updates=UiWorkspaceUpdatesMessage(
            updates={"agent-a1": _update(), "agent-b1": _update()}, update_window="2:00 AM-5:00 AM"
        ),
        environment=UiEnvironmentMessage(state=EnvironmentCondition.NONE),
    )


@pytest.mark.witnesses("account-windows.one-account-per-window", partial="covers the channel projection only")
@pytest.mark.witnesses(
    "home-page.offers-accounts-with-workspaces",
    partial="covers the per-account row counts the window receives; the offer and the switch are the "
    "Mithril LandingPage's (frontend/src/views/pages/LandingPage.test.ts)",
)
def test_project_snapshot_keeps_one_accounts_rows_and_what_is_about_them() -> None:
    projected = project_snapshot(_install_snapshot(), ALICE)

    assert projected.workspaces.account_id == ALICE
    assert [entry.id for entry in projected.workspaces.workspaces] == ["agent-a1"]
    assert list(projected.workspaces.remote_workspace_states) == ["agent-a1"]
    # The install-wide id lists stay: restore and destroy span accounts.
    assert projected.workspaces.destroying_agent_ids == ("agent-b1",)
    # Every account's row count stays too, so a window with no rows can point at the accounts that have some.
    assert projected.workspaces.workspace_count_by_account_id == {ALICE: 1, BOB: 1, LOCAL_PROFILE_USER_ID: 1}
    assert projected.accounts.account_id == ALICE
    assert [section.user_id for section in projected.providers.accounts] == [ALICE, LOCAL_PROFILE_USER_ID]
    # Bob's request goes; the request whose workspace is unnamed reaches every window.
    assert projected.requests.request_ids == ("req-a", "req-unknown")
    assert projected.requests.count == 2
    assert projected.requests.workspace_agent_ids == ("agent-a1",)
    assert projected.requests.workspace_agent_id_by_request_id == {"req-a": "agent-a1"}
    # Alice's workspace's entry, her own account-level event, an entry about a
    # workspace no row claims, and one naming neither a workspace nor an account.
    assert [entry.id for entry in projected.notifications.entries] == [
        "n-a",
        "n-alice-level",
        "n-unclaimed",
        "n-install-wide",
    ]
    assert projected.notifications.unresolved_count == 4
    assert [message.agent_id for message in projected.health] == ["agent-a1"]
    assert list(projected.workspace_updates.updates) == ["agent-a1"]
    assert projected.workspace_updates.update_window == "2:00 AM-5:00 AM"


@pytest.mark.witnesses("account-windows.device-window-lists-unassociated")
def test_project_snapshot_for_the_device_lists_the_unassociated_rows_and_the_device_section_alone() -> None:
    projected = project_snapshot(_install_snapshot(), LOCAL_PROFILE_USER_ID)

    assert [entry.id for entry in projected.workspaces.workspaces] == ["agent-d1"]
    assert projected.workspaces.remote_workspace_states == {}
    assert [section.user_id for section in projected.providers.accounts] == [LOCAL_PROFILE_USER_ID]
    assert projected.requests.request_ids == ("req-unknown",)
    assert projected.requests.workspace_agent_id_by_request_id == {}
    assert [entry.id for entry in projected.notifications.entries] == ["n-unclaimed", "n-install-wide"]
    assert projected.health == ()


def test_project_snapshot_for_a_signed_out_account_with_no_section_synthesizes_an_empty_one() -> None:
    projected = project_snapshot(_install_snapshot(), "user-gone")

    (own, device) = projected.providers.accounts
    assert (own.user_id, own.state, own.connections) == ("user-gone", AccountPanelState.SIGNED_OUT, ())
    assert device.user_id == LOCAL_PROFILE_USER_ID
    assert projected.workspaces.workspaces == ()


def test_hub_path_and_query_parsing() -> None:
    assert account_id_from_hub_path(f"/account/{ALICE}/") == ALICE
    assert account_id_from_hub_path(f"/account/{ALICE}/create") == ALICE
    assert account_id_from_hub_path("/account/local") == "local"
    assert account_id_from_hub_path("/accounts") is None
    assert account_id_from_hub_path("/account/../etc") is None
    assert account_id_from_query(f"?workspace=agent-a1&account={ALICE}") == ALICE
    assert account_id_from_query("account=") is None
    assert account_hub_route(ALICE) == f"/account/{ALICE}/"


def test_resolve_window_account_id_follows_the_hub_the_workspace_the_attempt_the_query_then_the_recent_list() -> None:
    def workspace_owner(any_id: str) -> str:
        return {"agent-a1": ALICE, "host-a1": ALICE}.get(any_id, LOCAL_PROFILE_USER_ID)

    def attempt_owner(attempt_id: str) -> str | None:
        return BOB if attempt_id == "create-attempt-0a1b2c3d" else None

    def resolve(path: str, query: str = "") -> str:
        return resolve_window_account_id(
            path,
            query,
            resolve_workspace_account_id=workspace_owner,
            resolve_create_attempt_account_id=attempt_owner,
            recent_account_ids=[BOB, ALICE],
            resolve_account_id_when_none_recorded=lambda: CAROL,
        )

    assert resolve(f"/account/{ALICE}/create") == ALICE
    assert resolve("/workspace/agent-a1") == ALICE
    assert resolve("/workspace/host-a1/options") == ALICE
    assert resolve("/popout/agent-a1/win-0a1b") == ALICE
    assert resolve("/agents/agent-a1/recovery") == ALICE
    assert resolve("/creating/create-attempt-0a1b2c3d") == BOB
    assert resolve("/creating/create-attempt-ffffffff") == LOCAL_PROFILE_USER_ID
    assert resolve("/settings", f"account={ALICE}") == ALICE
    assert resolve("/start") == LOCAL_PROFILE_USER_ID
    # A bare route takes the most recently used account, and the stand-in when none was used yet.
    assert resolve("/") == BOB
    assert resolve("/accounts") == BOB
    assert (
        resolve_window_account_id(
            "/",
            "",
            resolve_workspace_account_id=workspace_owner,
            resolve_create_attempt_account_id=attempt_owner,
            recent_account_ids=[],
            resolve_account_id_when_none_recorded=lambda: CAROL,
        )
        == CAROL
    )


@pytest.mark.witnesses(
    "account-windows.switch-skips-a-workspace-another-window-shows", partial="covers the landing rule alone"
)
@pytest.mark.witnesses("account-windows.switch-falls-back-to-the-home-page", partial="covers the landing rule alone")
def test_choose_switch_route_lands_on_the_most_recent_free_workspace_else_the_hub() -> None:
    enterable = frozenset({"agent-1", "agent-2", "agent-3"})

    # The most recent workspace is shown elsewhere; the next one is free.
    assert (
        choose_switch_route(ALICE, ["agent-1", "agent-2"], enterable, frozenset({"agent-1"})) == "/workspace/agent-2"
    )
    # A recent workspace that is gone (or a remote tile) is skipped.
    assert choose_switch_route(ALICE, ["agent-gone", "agent-3"], enterable, frozenset()) == "/workspace/agent-3"
    # Nothing free: the hub.
    assert choose_switch_route(ALICE, ["agent-1"], enterable, frozenset({"agent-1"})) == f"/account/{ALICE}/"
    assert choose_switch_route(ALICE, [], enterable, frozenset()) == f"/account/{ALICE}/"
