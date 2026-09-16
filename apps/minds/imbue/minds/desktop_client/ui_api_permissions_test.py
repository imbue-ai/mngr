"""Tests for the /ui/api per-workspace permissions routes (toggle tree, flips, degradation)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncRecord
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperator
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import build_permissions_test_catalog
from imbue.minds.desktop_client.latchkey.testing import leave_grant_on_this_computer
from imbue.minds.desktop_client.latchkey.testing import seed_connector_grant
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import create_accounts_permission_request
from imbue.minds.desktop_client.testing import create_file_sharing_permission_request
from imbue.minds.desktop_client.testing import create_predefined_permission_request
from imbue.minds.desktop_client.testing import create_workspace_permission_request
from imbue.minds.desktop_client.testing import write_fake_mngr_pair_script
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

_ACCOUNT: str = "alice@example.com"
# Generous: the stand-in mngr has to be spawned and answer before this elapses.
_START_TIMEOUT_SECONDS: float = 20.0
_WORKSPACE_NAME: str = "My Machine"
_SHARED_PATH_PERMISSION: str = "minds-file-server-read-/Users/me/notes"
# A ``latchkey-self`` name this screen does not own; every self-toggle write
# must leave it exactly where it was.
_BASELINE_PERMISSION: str = "minds-api-proxy-call-agent-123"
_AWS_CREDENTIALS = {"access-key-id": "AKIAEXAMPLE", "secret-access-key": "s3cret"}


class _UnreachableGatewayClient(FakeLatchkeyGatewayClient):
    """Gateway double whose reads fail the way an unreachable gateway does."""

    def get_permissions_config(self, permissions_file_path: Path) -> LatchkeyPermissionsConfig:
        raise LatchkeyGatewayClientError("gateway is down")


class _WorkspaceResolver(StaticBackendResolver):
    """Static resolver mapping agents to a fixed host and workspace name.

    ``host_by_agent`` overrides the shared host for the agents it names, which
    is what lets a test put two workspaces on two different hosts; agents it
    does not name keep ``fixed_host_id``.
    """

    fixed_host_id: HostId = Field(description="Host id reported for every known agent.")
    known_agent_ids: tuple[AgentId, ...] = Field(default=())
    name_by_agent: dict[str, str] = Field(default_factory=dict)
    host_by_agent: dict[str, str] = Field(default_factory=dict)

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.known_agent_ids:
            return None
        host_id = self.host_by_agent.get(str(agent_id), str(self.fixed_host_id))
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host_id)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.name_by_agent.get(str(agent_id))


def _build_handler(
    tmp_path: Path,
    latchkey: Latchkey,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=build_permissions_test_catalog(),
        mngr_message_sender=MngrMessageSender(
            mngr_caller=RecordingMngrCaller(),
            # These routes never send messages; an un-entered group satisfies
            # the required field.
            concurrency_group=ConcurrencyGroup(name="ui-api-permissions-test-unused"),
        ),
        gateway_client=gateway_client if gateway_client is not None else build_fake_gateway_client(),
        carry_grant_to_machine=leave_grant_on_this_computer,
    )


def _build_client(
    tmp_path: Path,
    latchkey: Latchkey,
    agent_ids: tuple[AgentId, ...],
    host_id: HostId,
    is_authenticated: bool = True,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
    inbox: StaticPendingRequests | None = None,
    has_handler: bool = True,
    host_by_agent: dict[str, str] | None = None,
    folder_sync_manager: FolderSyncManager | None = None,
    machine_operator: MachineOperator | None = None,
) -> FlaskClient:
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        fixed_host_id=host_id,
        known_agent_ids=agent_ids,
        name_by_agent={str(agent_id): _WORKSPACE_NAME for agent_id in agent_ids},
        host_by_agent=host_by_agent if host_by_agent is not None else {},
    )
    handlers = (_build_handler(tmp_path, latchkey, gateway_client),) if has_handler else ()
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=resolver,
        request_event_handlers=handlers,
        pending_requests=inbox,
        folder_sync_manager=folder_sync_manager,
        machine_operator=machine_operator,
    )
    return client


class _RecordingMachineOperator(MachineOperator):
    """A machine operator that records what it was told to carry, and carries nothing.

    The push to a workspace's machine is the half of a permissions write that
    nothing else here can see: this computer's copy looks right either way, and
    the difference only shows on the next read, when the machine's policy is
    adopted back over it.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    pushed_agent_ids: list[str] = Field(default_factory=list, description="Workspaces whose policy was pushed")
    refreshed_agent_ids: list[str] = Field(
        default_factory=list, description="Workspaces whose machine was read back, one SSH round trip each"
    )

    def push_permissions(self, workspace_agent_id: str) -> None:
        self.pushed_agent_ids.append(workspace_agent_id)

    def refresh(self, workspace_agent_id: str) -> None:
        self.refreshed_agent_ids.append(workspace_agent_id)


def _recording_operator(tmp_path: Path, latchkey: Latchkey) -> _RecordingMachineOperator:
    """An operator whose machine access is never opened, because nothing here reaches one."""
    return _RecordingMachineOperator(
        access=MachineAccess(
            latchkey=latchkey,
            concurrency_group=ConcurrencyGroup(name="test-machine-access"),
            backend_resolver=MngrCliBackendResolver(),
        )
    )


def _latchkey(tmp_path: Path, accounts_by_service: dict[str, list[str]] | None = None) -> FakeAccountsLatchkey:
    return FakeAccountsLatchkey(
        latchkey_directory=tmp_path / "latchkey",
        latchkey_binary="/nonexistent",
        accounts_by_service=accounts_by_service if accounts_by_service is not None else {"slack": [_ACCOUNT]},
    )


def _slack_connection(payload: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in payload["connections"] if c["service_name"] == "slack")


def _slack_toggles(payload: dict[str, Any]) -> dict[str, bool]:
    """``permission -> is_granted`` across every group of the Slack scope panel."""
    scope_panel = _slack_connection(payload)["scopes"][0]
    return {
        toggle["permission"]: toggle["is_granted"] for group in scope_panel["groups"] for toggle in group["toggles"]
    }


# Every write route this module registers, with a body its own validator
# accepts, so one table can walk the guards they all share. Written as the
# whole sub-path under the workspace, because this module serves two resources
# -- the grants under ``permissions/`` and the three sync routes under
# ``folder-syncs/`` -- and both are registered by the same function, so both
# have to be guarded. A route registered without the prelude is exactly what
# this catches.
_WRITE_ROUTES: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "permissions/connector-toggle",
        {"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read", "enabled": True},
    ),
    ("permissions/self-toggle", {"permission": _SHARED_PATH_PERMISSION, "enabled": False}),
    ("permissions/connector-revoke-all", {"service_name": "slack", "account": _ACCOUNT}),
    ("permissions/connector-disconnect", {"service_name": "slack", "account": _ACCOUNT}),
    ("permissions/connect-credentials", {"service_name": "aws", "value_by_parameter_name": _AWS_CREDENTIALS}),
    ("folder-syncs/toggle", {"path": "~/notes", "enabled": True}),
    ("folder-syncs/discard-copy", {"path": "~/notes"}),
)


@pytest.mark.parametrize("path,body", _WRITE_ROUTES)
def test_writes_require_authentication(tmp_path: Path, path: str, body: dict[str, object]) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, is_authenticated=False)

    response = client.post(f"/ui/api/workspaces/{agent_id}/{path}", json=body)

    assert response.status_code == 401
    # Nothing reached latchkey either -- a 401 that still ran the write would be
    # the worse half of the same bug.
    assert latchkey.auth_set_calls == []
    assert latchkey.cleared_calls == []


def test_workspace_permissions_requires_authentication(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id, is_authenticated=False)

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 401


@pytest.mark.parametrize("path", [path for path, _ in _WRITE_ROUTES])
def test_writes_reject_a_body_that_is_not_a_json_object(tmp_path: Path, path: str) -> None:
    """The shared prelude's guard, which every per-route validator sits behind."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(f"/ui/api/workspaces/{agent_id}/{path}", json=["not", "an", "object"])

    assert response.status_code == 400
    assert json.loads(response.data) == {"error": "Invalid JSON body"}
    assert latchkey.auth_set_calls == []
    assert latchkey.cleared_calls == []


@pytest.mark.parametrize("path", [path for path, _ in _WRITE_ROUTES])
def test_writes_reject_a_body_missing_its_fields(tmp_path: Path, path: str) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(f"/ui/api/workspaces/{agent_id}/{path}", json={})

    assert response.status_code == 400
    assert latchkey.auth_set_calls == []
    assert latchkey.cleared_calls == []


def test_workspace_permissions_returns_the_full_toggle_tree(tmp_path: Path) -> None:
    """The payload carries every grantable permission as a toggle, marking the granted ones."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["permissions_unavailable"] is False
    assert payload["host_id"] == str(host_id)
    connection = _slack_connection(payload)
    assert connection["display_name"] == "Slack"
    assert connection["account"] == _ACCOUNT
    assert connection["is_connected"] is True
    assert connection["granted_count"] == 1
    assert connection["scopes"][0]["scope"] == "slack-api"
    # ``any`` is the catalog's injected detent catch-all; it always renders.
    assert _slack_toggles(payload) == {
        "any": False,
        "slack-read-all": False,
        "slack-write-all": False,
        "slack-chat-read": True,
        "slack-chat-write": False,
    }
    # GitHub has no signed-in account and no grants, so it is offered rather
    # than rendered as a connection.
    assert [entry["service_name"] for entry in payload["available_connections"]] == ["aws", "github"]
    assert payload["waiting_requests"] == []


def test_workspace_permissions_carry_the_human_readable_copy(tmp_path: Path) -> None:
    """Rows ship the grouped, human-readable copy the pane renders, not raw schema names.

    The pane shows only ``label``/``description``; a schema name never reaches
    the user. Pinning them here also guards the wire mirrors in ``ui_models``:
    the copy is carried through a revalidated dump, so a field the engine
    renames or drops would otherwise surface as silently empty text.
    """
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    payload = json.loads(response.data)
    scope_panel = _slack_connection(payload)["scopes"][0]
    assert scope_panel["heading"] == "Slack"
    labels_by_permission = {
        toggle["permission"]: toggle["label"] for group in scope_panel["groups"] for toggle in group["toggles"]
    }
    assert labels_by_permission == {
        "any": "Everything (unrestricted)",
        "slack-read-all": "Read everything",
        "slack-write-all": "Change everything",
        "slack-chat-read": "Read chat",
        "slack-chat-write": "Manage chat",
    }
    # Full access leads and the catch-all trails, so the riskiest grant is last.
    headings = [group["heading"] for group in scope_panel["groups"]]
    assert headings[0] == "Full access"
    assert headings[-1] == "Extras"
    # Catalog descriptions ride along for the rows that have one.
    descriptions = {
        toggle["permission"]: toggle["description"] for group in scope_panel["groups"] for toggle in group["toggles"]
    }
    assert descriptions["slack-chat-read"] == "Get permalinks."


def test_connector_toggle_grants_a_permission_and_returns_the_refreshed_view(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-write", "enabled": True},
    )

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert _slack_toggles(payload) == {
        "any": False,
        "slack-read-all": False,
        "slack-write-all": False,
        "slack-chat-read": True,
        "slack-chat-write": True,
    }
    assert _slack_connection(payload)["granted_count"] == 2
    # The server wrote the rule's COMPLETE set (catalog order), never a diff.
    config = load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id))
    assert config.rules == ({account_scope_key("slack-api", _ACCOUNT): ["slack-chat-read", "slack-chat-write"]},)


def test_connector_toggle_off_of_the_last_permission_deletes_the_rule(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read", "enabled": False},
    )

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert _slack_connection(payload)["granted_count"] == 0
    assert all(is_granted is False for is_granted in _slack_toggles(payload).values())
    # An emptied set removes the rule rather than leaving an empty one behind.
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()


def test_connector_toggle_rejects_an_unknown_scope(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-toggle",
        json={"scope": "nope-api", "account": _ACCOUNT, "permission": "any", "enabled": True},
    )

    assert response.status_code == 400
    assert "nope-api" in json.loads(response.data)["error"]


def test_connector_toggle_rejects_a_body_without_enabled(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read"},
    )

    assert response.status_code == 400


def test_self_toggle_flips_a_shared_path_and_preserves_unrelated_names(tmp_path: Path) -> None:
    """Local files / Other machines flips rewrite the whole rule but own only their own names."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
            schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
        ),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": False},
    )

    assert response.status_code == 200
    payload = json.loads(response.data)
    # Revoked paths leave the list entirely: this pane shows what is shared.
    assert payload["shared_paths"] == []
    assert load_permissions(permissions_path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": True},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["shared_paths"][0]["path"] == "/Users/me/notes"
    assert load_permissions(permissions_path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},)


def test_self_toggle_rejects_a_permission_the_screen_does_not_own(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=({SELF_SCOPE: [_BASELINE_PERMISSION]},), schemas={}),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/self-toggle",
        json={"permission": _BASELINE_PERMISSION, "enabled": False},
    )

    assert response.status_code == 400
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == (
        {SELF_SCOPE: [_BASELINE_PERMISSION]},
    )


def test_connector_revoke_all_drops_every_grant_for_the_account(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(
        latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read", "slack-chat-write")
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-revoke-all",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert _slack_connection(payload)["granted_count"] == 0
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()


def test_connector_revoke_all_leaves_other_workspaces_alone(tmp_path: Path) -> None:
    """Revoke all is scoped to this machine -- the mirror of the disconnect test below.

    This is the asymmetry the pane is built around, and it is the one this
    endpoint can get catastrophically wrong: wired to the cross-machine revoke
    it would silently strip grants from every other machine, which every other
    assertion in this file would still pass.
    """
    agent_id, other_agent_id = AgentId(), AgentId()
    host_id, other_host_id = HostId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    seed_connector_grant(latchkey.plugin_data_dir, other_host_id, "slack-api", _ACCOUNT, ("slack-chat-write",))
    other_rules = load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, other_host_id)).rules
    client = _build_client(
        tmp_path,
        latchkey,
        (agent_id, other_agent_id),
        host_id,
        host_by_agent={str(other_agent_id): str(other_host_id)},
    )

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-revoke-all",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, other_host_id)).rules == other_rules
    # And the account stays connected: revoking is not disconnecting.
    assert latchkey.cleared_calls == []
    assert "slack" in [entry["service_name"] for entry in json.loads(response.data)["connections"]]


def test_connector_revoke_all_rejects_an_unknown_service(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-revoke-all",
        json={"service_name": "nope", "account": _ACCOUNT},
    )

    assert response.status_code == 400


def test_connector_disconnect_clears_the_credential_and_drops_the_connection(tmp_path: Path) -> None:
    """Disconnecting clears the stored credential and takes the connection out of the view.

    Asserting on the RETURNED payload (rather than polling for it) is what fails
    if the cross-workspace strip is ever moved off the request thread: a
    backgrounded strip would answer with the connection still present, now
    merely disconnected, and the pane would be left pointing at it.
    """
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(
        latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read", "slack-chat-write")
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    assert latchkey.cleared_calls == [("slack", _ACCOUNT)]
    payload = json.loads(response.data)
    assert [entry["service_name"] for entry in payload["connections"]] == []
    # The service's last account is gone, so it is offered again -- reconnecting
    # is a fresh sign-in.
    assert "slack" in [entry["service_name"] for entry in payload["available_connections"]]
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()


def test_connector_disconnect_leaves_other_machines_grants_alone(tmp_path: Path) -> None:
    """Disconnecting is machine-scoped, because the credential it clears is.

    Every machine keeps its own credentials, so signing an account out here says
    nothing about the same account on another machine -- and stripping that
    machine's grants would revoke access that still has a credential behind it.
    """
    agent_id, other_agent_id = AgentId(), AgentId()
    host_id, other_host_id = HostId(), HostId()
    latchkey = _latchkey(tmp_path)
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    seed_connector_grant(latchkey.plugin_data_dir, other_host_id, "slack-api", _ACCOUNT, ("slack-chat-write",))
    client = _build_client(
        tmp_path,
        latchkey,
        (agent_id, other_agent_id),
        host_id,
        host_by_agent={str(other_agent_id): str(other_host_id)},
    )

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    # This machine's file is empty by the time the response lands: no polling,
    # because the strip is part of the request rather than a background thread.
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()
    other_rules = load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, other_host_id)).rules
    assert [permission for rule in other_rules for permission in rule.values()] == [["slack-chat-write"]]


def test_connector_disconnect_keeps_the_services_other_accounts(tmp_path: Path) -> None:
    other_account = "bob@example.com"
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path, accounts_by_service={"slack": [_ACCOUNT, other_account]})
    permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    signed_out_key, signed_out_granted, signed_out_schemas = build_account_grant(
        "slack-api", _ACCOUNT, ("slack-chat-read",)
    )
    kept_key, kept_granted, kept_schemas = build_account_grant("slack-api", other_account, ("slack-chat-write",))
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(
            rules=({signed_out_key: list(signed_out_granted)}, {kept_key: list(kept_granted)}),
            schemas={**signed_out_schemas, **kept_schemas},
        ),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    assert latchkey.cleared_calls == [("slack", _ACCOUNT)]
    payload = json.loads(response.data)
    assert [entry["account"] for entry in payload["connections"]] == [other_account]
    assert load_permissions(permissions_path).rules == (
        {account_scope_key("slack-api", other_account): ["slack-chat-write"]},
    )


def test_connector_disconnect_reports_a_refused_clear_as_502(tmp_path: Path) -> None:
    """A latchkey that will not clear is latchkey failing, not a bad request; nothing is stripped."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    latchkey.auth_clear_result = (False, "keychain is locked")
    seed_connector_grant(latchkey.plugin_data_dir, host_id, "slack-api", _ACCOUNT, ("slack-chat-read",))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 502
    assert "keychain is locked" in json.loads(response.data)["error"]
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == (
        {account_scope_key("slack-api", _ACCOUNT): ["slack-chat-read"]},
    )


def test_connector_disconnect_rejects_an_unknown_service_before_clearing_anything(tmp_path: Path) -> None:
    """The catalog check precedes the destructive half, so a bad name clears nothing."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "nope", "account": _ACCOUNT},
    )

    assert response.status_code == 400
    assert latchkey.cleared_calls == []


def test_connector_disconnect_rejects_a_malformed_body(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack"},
    )

    assert response.status_code == 400
    assert latchkey.cleared_calls == []


def test_connector_disconnect_reports_a_gateway_failure_as_502(tmp_path: Path) -> None:
    """The credential is gone and the strip failed: the user is told, rather than getting 'ok'."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(
        tmp_path,
        latchkey,
        (agent_id,),
        host_id,
        gateway_client=_UnreachableGatewayClient(),
    )

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-disconnect",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 502
    assert latchkey.cleared_calls == [("slack", _ACCOUNT)]


def test_workspace_permissions_carry_how_each_service_is_connected(tmp_path: Path) -> None:
    """Each offered service says whether connecting signs in or asks for credentials."""
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    payload = json.loads(response.data)
    sign_in_by_service = {entry["service_name"]: entry["sign_in"] for entry in payload["available_connections"]}
    assert sign_in_by_service["github"]["is_browser_supported"] is True
    assert sign_in_by_service["github"]["credential_parameters"] == []
    aws_sign_in = sign_in_by_service["aws"]
    assert aws_sign_in["is_browser_supported"] is False
    assert aws_sign_in["credential_parameters"] == [
        {"name": "access-key-id", "label": "Access key id"},
        {"name": "secret-access-key", "label": "Secret access key"},
    ]
    # AWS has no account yet, so the first one is latchkey's unnamed default.
    assert aws_sign_in["is_account_name_required"] is False
    # A connected service carries the same answer, for the account after this one.
    assert _slack_connection(payload)["sign_in"]["is_browser_supported"] is True


def test_connect_credentials_stores_them_and_returns_the_new_connection(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-credentials",
        json={"service_name": "aws", "value_by_parameter_name": _AWS_CREDENTIALS, "account_name": ""},
    )

    assert response.status_code == 200
    assert latchkey.auth_set_calls == [
        ("aws", ("--account", "", "auth", "set-nocurl", "aws", "AKIAEXAMPLE", "s3cret")),
    ]
    payload = json.loads(response.data)
    # The refreshed view carries the new connection, with nothing granted on it.
    aws = next(entry for entry in payload["connections"] if entry["service_name"] == "aws")
    assert aws["account"] == ""
    assert aws["is_connected"] is True
    assert aws["granted_count"] == 0
    assert [entry["service_name"] for entry in payload["available_connections"]] == ["github"]


def test_connect_credentials_reports_a_refused_credential_as_400(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    latchkey.auth_set_result = (
        False,
        "Error: that does not look like an AWS access key ID\nExample: latchkey auth set-nocurl aws <id> <key>",
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-credentials",
        json={"service_name": "aws", "value_by_parameter_name": _AWS_CREDENTIALS},
    )

    assert response.status_code == 400
    # The service's own explanation is kept; its usage lines are not.
    assert json.loads(response.data) == {
        "error": "AWS rejected those credentials: that does not look like an AWS access key ID"
    }


def test_connect_credentials_rejects_a_browser_service(tmp_path: Path) -> None:
    """Slack is connected by signing in, so this route is not the way to add it."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-credentials",
        json={"service_name": "slack", "value_by_parameter_name": {"token": "t"}},
    )

    assert response.status_code == 400
    assert latchkey.auth_set_calls == []


def test_connect_credentials_rejects_an_unknown_service(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-credentials",
        json={"service_name": "not-a-service", "value_by_parameter_name": {}},
    )

    assert response.status_code == 400
    assert "Unknown service" in json.loads(response.data)["error"]


def test_connect_credentials_rejects_a_malformed_body(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-credentials",
        json={"service_name": "aws"},
    )

    assert response.status_code == 400


def test_workspace_permissions_degrades_when_the_gateway_is_unreachable(tmp_path: Path) -> None:
    """An unreachable gateway answers 200 with the unavailable flag, not a 500."""
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(
        tmp_path,
        _latchkey(tmp_path),
        (agent_id,),
        host_id,
        gateway_client=_UnreachableGatewayClient(),
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["permissions_unavailable"] is True
    assert payload["host_id"] == ""
    assert payload["connections"] == []
    assert payload["available_connections"] == []
    assert payload["shared_paths"] == []
    assert payload["workspace_toggles"] == []


def test_connector_toggle_reports_a_gateway_failure_as_502(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(
        tmp_path,
        _latchkey(tmp_path),
        (agent_id,),
        host_id,
        gateway_client=_UnreachableGatewayClient(),
    )

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read", "enabled": True},
    )

    assert response.status_code == 502


def test_workspace_permissions_is_unavailable_without_a_permission_handler(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id, has_handler=False)

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    assert json.loads(response.data)["permissions_unavailable"] is True


def test_writes_are_rejected_with_503_without_a_permission_handler(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id, has_handler=False)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": False},
    )

    assert response.status_code == 503


def test_workspace_permissions_lists_waiting_requests_oldest_first(tmp_path: Path) -> None:
    """Pending requests from this workspace's agents lead with the longest-blocked one."""
    agent_id, sibling_agent_id, host_id = AgentId(), AgentId(), HostId()
    older = create_predefined_permission_request(
        agent_id=str(sibling_agent_id),
        scope="slack-api",
        rationale="post the standup summary",
    )
    newer = create_file_sharing_permission_request(
        agent_id=str(sibling_agent_id),
        path="/Users/me/notes",
        access="READ",
        rationale="read the design notes",
    )
    # ``pending`` is display order (newest first), matching the gateway view.
    inbox = StaticPendingRequests(pending=(newer, older))
    client = _build_client(
        tmp_path,
        _latchkey(tmp_path),
        (agent_id, sibling_agent_id),
        host_id,
        inbox=inbox,
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    waiting = json.loads(response.data)["waiting_requests"]
    assert [(row["id"], row["title"], row["service_name"]) for row in waiting] == [
        (older.request_id, "Slack", "slack"),
        (newer.request_id, "Local files", ""),
    ]
    assert waiting[0]["reason"] == "post the standup summary"


@pytest.mark.parametrize(
    "make_event,expected_title,expected_service_name",
    [
        pytest.param(
            lambda agent_id: create_predefined_permission_request(
                agent_id=agent_id, scope="not-in-the-catalog", rationale="why"
            ),
            "not-in-the-catalog",
            "",
            id="predefined-outside-the-catalog",
        ),
        pytest.param(
            lambda agent_id: create_workspace_permission_request(agent_id=agent_id, rationale="why"),
            "Other machines",
            "",
            id="cross-workspace",
        ),
        pytest.param(
            lambda agent_id: create_accounts_permission_request(agent_id=agent_id, rationale="why"),
            "Device accounts",
            "",
            id="device-accounts",
        ),
    ],
)
def test_waiting_requests_title_every_kind_the_strip_can_show(
    tmp_path: Path,
    make_event: Callable[[str], StreamedPermissionRequest],
    expected_title: str,
    expected_service_name: str,
) -> None:
    """Each row's headline is the name the review dialog uses, never a raw schema name.

    The strip is the only place several of these kinds are named, so a title
    that regressed to the scope string would reach the user unannounced.
    """
    agent_id, sibling_agent_id, host_id = AgentId(), AgentId(), HostId()
    event = make_event(str(sibling_agent_id))
    client = _build_client(
        tmp_path,
        _latchkey(tmp_path),
        (agent_id, sibling_agent_id),
        host_id,
        inbox=StaticPendingRequests(pending=(event,)),
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    waiting = json.loads(response.data)["waiting_requests"]
    assert [(row["title"], row["service_name"], row["reason"]) for row in waiting] == [
        (expected_title, expected_service_name, "why")
    ]


def test_waiting_requests_exclude_other_workspaces(tmp_path: Path) -> None:
    agent_id, other_agent_id, host_id = AgentId(), AgentId(), HostId()
    other_request = create_file_sharing_permission_request(
        agent_id=str(other_agent_id),
        path="/Users/me/elsewhere",
        access="READ",
        rationale="not this machine",
    )
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        fixed_host_id=host_id,
        known_agent_ids=(agent_id, other_agent_id),
        name_by_agent={str(agent_id): _WORKSPACE_NAME, str(other_agent_id): "Other Machine"},
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        request_event_handlers=(_build_handler(tmp_path, _latchkey(tmp_path)),),
        pending_requests=StaticPendingRequests(pending=(other_request,)),
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    assert json.loads(response.data)["waiting_requests"] == []


def test_workspace_permissions_rejects_a_malformed_workspace_id(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.get("/ui/api/workspaces/not-an-agent-id/permissions")

    assert response.status_code == 404


def _seed_shared_path_grant(tmp_path: Path, latchkey: FakeAccountsLatchkey, host_id: HostId, path: str) -> None:
    """Give the machine one granted shared path, so its row is in the payload."""
    permission = f"minds-file-server-read-{path}"
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=({SELF_SCOPE: [permission]},), schemas={permission: {"type": "object"}}),
    )


def _folder_sync_manager(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> FolderSyncManager:
    """A manager whose ``mngr`` is the test stand-in, rooted at ``tmp_path``."""
    return FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id="host-0f0e0d0c0b0a09080706050403020100",
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        store=FolderSyncStore(records_dir=tmp_path / "folder_syncs"),
    )


def _shared_path_row(payload: dict[str, Any], path: str) -> dict[str, Any]:
    return next(row for row in payload["shared_paths"] if row["path"] == path)


def test_sync_is_reported_unsupported_when_the_build_cannot_run_one(tmp_path: Path) -> None:
    """The pane has to tell "cannot sync here" apart from "not synced"."""
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/permissions").data)

    assert payload["is_sync_supported"] is False


def test_turning_sync_on_answers_with_the_row_carrying_its_sync(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    shared = tmp_path / "notes"
    shared.mkdir()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    _seed_shared_path_grant(tmp_path, latchkey, host_id, str(shared))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(shared), "enabled": True, "conflict": "WORKSPACE"},
    )

    assert response.status_code == 200
    # The row appears with the response; bringing the sync up is asynchronous,
    # so how far along it is by the time this is read is a race the stand-in
    # mngr -- which answers instantly -- can and does win. That it is one of
    # the states a sync on its way up can be in is the part that is true every
    # time; that it has not finished yet is not.
    assert _shared_path_row(json.loads(response.data), str(shared))["sync"]["state"] in (
        "STARTING",
        "SYNCING",
        "SYNCED",
    )

    manager.wait_until_started(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)
    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/permissions").data)
    assert payload["is_sync_supported"] is True
    row = _shared_path_row(payload, str(shared))
    assert row["sync"]["state"] == "SYNCED"
    # Derived from the grant, which is read-only here, not chosen separately.
    assert row["sync"]["direction"] == "TO_WORKSPACE"
    assert row["sync"]["workspace_path"] == f"~/synced_folders/host-0f0e0d0c0b0a09080706050403020100{shared}"
    assert row["path_label"] == "~/notes"
    manager.stop_all()


def test_turning_sync_off_clears_it_from_the_row(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    shared = tmp_path / "notes"
    shared.mkdir()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    _seed_shared_path_grant(tmp_path, latchkey, host_id, str(shared))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager)
    client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(shared), "enabled": True},
    )
    manager.wait_until_started(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(shared), "enabled": False},
    )

    assert response.status_code == 200
    # The click records where the folder should end up and returns, so the answer
    # reports the destination rather than waiting for the move to finish.
    assert _shared_path_row(json.loads(response.data), str(shared))["sync"]["activity"] == "INACTIVE"
    assert manager.wait_until_settled(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)


def test_syncing_a_folder_that_is_not_there_is_refused(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A sanity check, not a perimeter.

    The route does not confine a sync to the folders a share may use. That
    check was there and has gone: the caller it would most need to police is
    ``restore_all``, reading a file that sits beside this app's signing key and
    latchkey credentials, so it stopped nobody while implying a boundary that
    does not exist. What is left is what the next step needs.
    """
    agent_id, host_id = AgentId(), HostId()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id, folder_sync_manager=manager)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(tmp_path / "not-there"), "enabled": True},
    )

    assert response.status_code == 400
    assert "nothing at" in json.loads(response.data)["error"]
    assert not (tmp_path / "argv.json").exists()


def test_sharing_a_new_path_files_the_grant_against_the_workspaces_own_file(tmp_path: Path) -> None:
    """Not Minds' own permissions file -- a file-sharing rule there wedges the gateway."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    shared = tmp_path / "pictures"
    shared.mkdir()
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/shared-path",
        json={"path": str(shared), "access": "WRITE"},
    )

    assert response.status_code == 200
    assert _shared_path_row(json.loads(response.data), str(shared))["access"] == "WRITE"
    granted = [name for rule in load_permissions(permissions_path).rules for name in rule.get(SELF_SCOPE, [])]
    assert f"minds-file-server-write-{shared}" in granted


def test_sharing_a_path_hands_the_grant_to_the_workspaces_own_machine(tmp_path: Path) -> None:
    """Otherwise the next read adopts the machine's policy back over it and the row vanishes.

    The gateway splices a new grant into this computer's copy only. A remote
    workspace's machine enforces its own, and opening the pane reads that back
    over this one -- so a grant the machine never hears about survives exactly
    until the user looks at it again.
    """
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    operator = _recording_operator(tmp_path, latchkey)
    shared = tmp_path / "pictures"
    shared.mkdir()
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, machine_operator=operator)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/shared-path",
        json={"path": str(shared), "access": "READ"},
    )

    assert response.status_code == 200
    # READ is what the Add buttons send, and it is the case that used to be
    # missed: its only other write is a revoke of a grant that was never there,
    # and a no-op flip returns without pushing anything.
    assert operator.pushed_agent_ids == [str(agent_id)]


def test_the_folder_sync_poll_never_touches_the_workspaces_machine(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The pane polls this every two seconds; a machine read there costs a round trip per poll.

    Everything it answers with is already in this process, so the operator --
    which is what reaches the machine -- must be left alone entirely.
    """
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    operator = _recording_operator(tmp_path, latchkey)
    shared = tmp_path / "notes"
    shared.mkdir()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    _seed_shared_path_grant(tmp_path, latchkey, host_id, str(shared))
    client = _build_client(
        tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager, machine_operator=operator
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/folder-syncs")

    assert response.status_code == 200
    assert "rows" in json.loads(response.data)
    assert operator.refreshed_agent_ids == []


def test_the_folder_sync_poll_reports_a_running_sync(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """What the poll exists for: the state a sync reached after the pane was drawn."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    shared = tmp_path / "notes"
    shared.mkdir()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    _seed_shared_path_grant(tmp_path, latchkey, host_id, str(shared))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager)
    client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(shared), "enabled": True},
    )
    manager.wait_until_started(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)

    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/folder-syncs").data)

    row = next(entry for entry in payload["rows"] if entry["path"] == str(shared))
    assert row["sync"]["state"] == "SYNCED"
    manager.stop_all()


def test_narrowing_access_to_read_drops_the_wider_grant(tmp_path: Path) -> None:
    """Otherwise the agent keeps the write it was just told it no longer has."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    shared = tmp_path / "notes"
    shared.mkdir()
    write_name = f"minds-file-server-write-{shared}"
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(rules=({SELF_SCOPE: [write_name]},), schemas={write_name: {"type": "object"}}),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/shared-path",
        json={"path": str(shared), "access": "READ"},
    )

    assert response.status_code == 200
    assert _shared_path_row(json.loads(response.data), str(shared))["access"] == "READ"
    granted = [name for rule in load_permissions(permissions_path).rules for name in rule.get(SELF_SCOPE, [])]
    assert write_name not in granted


def test_removing_a_shared_path_drops_every_access_mode(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    read_name = "minds-file-server-read-/Users/me/notes"
    write_name = "minds-file-server-write-/Users/me/notes"
    save_permissions(
        permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, read_name, write_name]},),
            schemas={read_name: {"type": "object"}, write_name: {"type": "object"}},
        ),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/shared-path-remove",
        json={"path": "/Users/me/notes"},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["shared_paths"] == []
    # The unrelated baseline name is untouched, as with every other write here.
    assert load_permissions(permissions_path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)


def test_a_path_held_at_both_access_modes_is_one_row_showing_the_wider(tmp_path: Path) -> None:
    """WRITE already implies READ, so two grants are still one thing the user shared."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    read_name = "minds-file-server-read-/Users/me/notes"
    write_name = "minds-file-server-write-/Users/me/notes"
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [read_name, write_name]},),
            schemas={read_name: {"type": "object"}, write_name: {"type": "object"}},
        ),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/permissions").data)

    assert len(payload["shared_paths"]) == 1
    assert payload["shared_paths"][0]["access"] == "WRITE"


def test_a_folder_inside_a_synced_one_is_offered_with_the_reason_it_cannot_sync(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """End to end: the manager's refusal has to reach the row, not just exist."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    outer = tmp_path / "work"
    inner = outer / "subfolder"
    inner.mkdir(parents=True)
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    permissions = tuple(f"minds-file-server-read-{path}" for path in (str(outer), str(inner)))
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: list(permissions)},),
            schemas={permission: {"type": "object"} for permission in permissions},
        ),
    )
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager)
    client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(outer), "enabled": True},
    )
    manager.wait_until_started(str(agent_id), str(outer), _START_TIMEOUT_SECONDS)

    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/permissions").data)

    inner_row = _shared_path_row(payload, str(inner))
    assert "one folder is inside the other" in inner_row["sync_unavailable_reason"]

    # And still when the inner folder has a record of its own from an earlier
    # session -- a copy it was told to remove, say. Having been synced before
    # says nothing about whether it may be synced now.
    assert manager.store is not None
    manager.store.remember(
        FolderSyncRecord(
            agent_id=str(agent_id),
            local_path=str(inner),
            direction=FolderSyncDirection.TO_WORKSPACE,
            conflict=FolderSyncConflict.NEWER,
            device_id=manager.device_id,
            activity=FolderSyncActivity.DISCARDED,
        )
    )
    payload = json.loads(client.get(f"/ui/api/workspaces/{agent_id}/permissions").data)
    assert "one folder is inside the other" in _shared_path_row(payload, str(inner))["sync_unavailable_reason"]
    # And the folder that is syncing is still offered.
    assert _shared_path_row(payload, str(outer))["sync_unavailable_reason"] == ""
    manager.stop_all()


def test_removing_a_shared_path_stops_any_sync_on_it_and_deletes_the_copy(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A sync outliving its grant would keep copying files the agent may no longer ask for.

    The copy goes with it rather than being set aside: with the path gone there
    is no row left that could ever offer to delete it.
    """
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    shared = tmp_path / "notes"
    shared.mkdir()
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    _seed_shared_path_grant(tmp_path, latchkey, host_id, str(shared))
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, folder_sync_manager=manager)
    client.post(
        f"/ui/api/workspaces/{agent_id}/folder-syncs/toggle",
        json={"path": str(shared), "enabled": True},
    )
    manager.wait_until_started(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)
    assert manager.status_for_path(str(agent_id), str(shared)) is not None

    client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/shared-path-remove",
        json={"path": str(shared)},
    )

    assert manager.wait_until_settled(str(agent_id), str(shared), _START_TIMEOUT_SECONDS)
    assert manager.desired_activity_for(str(agent_id), str(shared)) == FolderSyncActivity.DISCARDED


def test_connect_browser_signs_in_and_answers_with_the_refreshed_pane(tmp_path: Path) -> None:
    """Add connection's sign-in belongs to the machine whose pane asked for it."""
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path, accounts_by_service={})
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-browser",
        json={"service_name": "slack"},
    )

    assert response.status_code == 200
    assert latchkey.added_account_calls == ["slack"]
    payload = json.loads(response.data)
    assert [entry["service_name"] for entry in payload["connections"]] == ["slack"]


def test_connect_browser_reports_a_sign_in_that_did_not_complete(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path, accounts_by_service={})
    latchkey.add_account_result = (False, "the browser was closed")
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-browser",
        json={"service_name": "slack"},
    )

    assert response.status_code == 502
    assert "the browser was closed" in json.loads(response.data)["error"]


def test_connect_browser_rejects_an_unknown_service(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/connect-browser",
        json={"service_name": "not-a-service"},
    )

    assert response.status_code == 400
    assert latchkey.added_account_calls == []
