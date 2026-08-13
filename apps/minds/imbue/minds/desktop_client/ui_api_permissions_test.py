"""Tests for the /ui/api per-workspace permissions routes (toggle tree, flips, degradation)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import build_permissions_test_catalog
from imbue.minds.desktop_client.latchkey.testing import seed_connector_grant
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_latchkey_accounts_permission_request_event
from imbue.minds.desktop_client.request_events import create_latchkey_file_sharing_permission_request_event
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import create_latchkey_workspace_permission_request_event
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
    )


def _build_client(
    tmp_path: Path,
    latchkey: Latchkey,
    agent_ids: tuple[AgentId, ...],
    host_id: HostId,
    is_authenticated: bool = True,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
    inbox: RequestInbox | None = None,
    has_handler: bool = True,
    host_by_agent: dict[str, str] | None = None,
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
        request_inbox=inbox,
    )
    return client


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


# Every route of this area, with a body its own validator accepts, so one table
# can walk the guards they all share. Kept beside the routes rather than in each
# test, since a route registered without the prelude is exactly what this
# catches.
_WRITE_ROUTES: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "connector-toggle",
        {"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read", "enabled": True},
    ),
    ("self-toggle", {"permission": _SHARED_PATH_PERMISSION, "enabled": False}),
    ("connector-revoke-all", {"service_name": "slack", "account": _ACCOUNT}),
    ("connector-disconnect", {"service_name": "slack", "account": _ACCOUNT}),
    ("connect-credentials", {"service_name": "aws", "value_by_parameter_name": _AWS_CREDENTIALS}),
)


@pytest.mark.parametrize("path,body", _WRITE_ROUTES)
def test_writes_require_authentication(tmp_path: Path, path: str, body: dict[str, object]) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id, is_authenticated=False)

    response = client.post(f"/ui/api/workspaces/{agent_id}/permissions/{path}", json=body)

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

    response = client.post(f"/ui/api/workspaces/{agent_id}/permissions/{path}", json=["not", "an", "object"])

    assert response.status_code == 400
    assert json.loads(response.data) == {"error": "Invalid JSON body"}
    assert latchkey.auth_set_calls == []
    assert latchkey.cleared_calls == []


@pytest.mark.parametrize("path", [path for path, _ in _WRITE_ROUTES])
def test_writes_reject_a_body_missing_its_fields(tmp_path: Path, path: str) -> None:
    agent_id, host_id = AgentId(), HostId()
    latchkey = _latchkey(tmp_path)
    client = _build_client(tmp_path, latchkey, (agent_id,), host_id)

    response = client.post(f"/ui/api/workspaces/{agent_id}/permissions/{path}", json={})

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
    assert [(t["permission"], t["is_granted"], t["can_enable"]) for t in payload["file_sharing_toggles"]] == [
        (_SHARED_PATH_PERMISSION, False, True)
    ]
    assert load_permissions(permissions_path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    response = client.post(
        f"/ui/api/workspaces/{agent_id}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": True},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["file_sharing_toggles"][0]["is_granted"] is True
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


def test_connector_disconnect_strips_grants_on_every_workspace(tmp_path: Path) -> None:
    """Disconnecting is not machine-scoped: every workspace holding the account loses its grants."""
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
    # Both files are empty by the time the response lands: no polling, because
    # the strip is part of the request rather than a background thread.
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, host_id)).rules == ()
    assert load_permissions(permissions_path_for_host(latchkey.plugin_data_dir, other_host_id)).rules == ()


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
    assert payload["file_sharing_toggles"] == []
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
    older = create_latchkey_predefined_permission_request_event(
        agent_id=str(sibling_agent_id),
        scope="slack-api",
        rationale="post the standup summary",
    )
    newer = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(sibling_agent_id),
        path="/Users/me/notes",
        access="READ",
        rationale="read the design notes",
    )
    inbox = RequestInbox().add_request(older).add_request(newer)
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
        (str(older.event_id), "Slack", "slack"),
        (str(newer.event_id), "Local files", ""),
    ]
    assert waiting[0]["reason"] == "post the standup summary"


@pytest.mark.parametrize(
    "make_event,expected_title,expected_service_name",
    [
        pytest.param(
            lambda agent_id: create_latchkey_predefined_permission_request_event(
                agent_id=agent_id, scope="not-in-the-catalog", rationale="why"
            ),
            "not-in-the-catalog",
            "",
            id="predefined-outside-the-catalog",
        ),
        pytest.param(
            lambda agent_id: create_latchkey_workspace_permission_request_event(agent_id=agent_id, rationale="why"),
            "Other machines",
            "",
            id="cross-workspace",
        ),
        pytest.param(
            lambda agent_id: create_latchkey_accounts_permission_request_event(agent_id=agent_id, rationale="why"),
            "Device accounts",
            "",
            id="device-accounts",
        ),
    ],
)
def test_waiting_requests_title_every_kind_the_strip_can_show(
    tmp_path: Path,
    make_event: Callable[[str], RequestEvent],
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
        inbox=RequestInbox().add_request(event),
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    waiting = json.loads(response.data)["waiting_requests"]
    assert [(row["title"], row["service_name"], row["reason"]) for row in waiting] == [
        (expected_title, expected_service_name, "why")
    ]


def test_waiting_requests_exclude_other_workspaces(tmp_path: Path) -> None:
    agent_id, other_agent_id, host_id = AgentId(), AgentId(), HostId()
    other_request = create_latchkey_file_sharing_permission_request_event(
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
        request_inbox=RequestInbox().add_request(other_request),
    )

    response = client.get(f"/ui/api/workspaces/{agent_id}/permissions")

    assert response.status_code == 200
    assert json.loads(response.data)["waiting_requests"] == []


def test_workspace_permissions_rejects_a_malformed_workspace_id(tmp_path: Path) -> None:
    agent_id, host_id = AgentId(), HostId()
    client = _build_client(tmp_path, _latchkey(tmp_path), (agent_id,), host_id)

    response = client.get("/ui/api/workspaces/not-an-agent-id/permissions")

    assert response.status_code == 404
