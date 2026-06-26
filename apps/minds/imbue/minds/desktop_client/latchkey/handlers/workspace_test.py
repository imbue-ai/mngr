"""Unit tests for :class:`WorkspacePermissionGrantHandler`."""

import json
from pathlib import Path

import httpx
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.workspace import WorkspacePermissionGrantHandler
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_workspace_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_DESTROY
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_READ


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    concurrency_group: ConcurrencyGroup | None = None
    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, agent_id: AgentId, text: str) -> None:
        self.sent_messages.append((str(agent_id), text))


class _HostBackendResolver(StaticBackendResolver):
    """Static resolver that reports a real ``host-<hex>`` id for one agent."""

    agent_host_id: str = Field(description="Host id reported for the requesting agent.")
    workspace_name_by_agent: dict[str, str] = Field(default_factory=dict)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=self.agent_host_id)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.workspace_name_by_agent.get(str(agent_id))


def _build_gateway_client() -> LatchkeyGatewayClient:
    # The workspace handler only calls DELETE /permission-requests on the
    # gateway; return 204 for everything.
    return LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(lambda r: httpx.Response(204)),
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
    )


def _make_handler(tmp_path: Path) -> tuple[WorkspacePermissionGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    latchkey = Latchkey(latchkey_directory=tmp_path / "lk")
    return (
        WorkspacePermissionGrantHandler(
            data_dir=tmp_path,
            latchkey=latchkey,
            gateway_client=_build_gateway_client(),
            mngr_message_sender=sender,
        ),
        sender,
    )


def _plugin_data_dir(tmp_path: Path) -> Path:
    return Latchkey(latchkey_directory=tmp_path / "lk").plugin_data_dir


def _write_baseline_host_file(tmp_path: Path, host_id: HostId) -> Path:
    path = permissions_path_for_host(_plugin_data_dir(tmp_path), host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": [], "schemas": {}}))
    return path


def _build_authenticated_client(
    tmp_path: Path,
    handler: WorkspacePermissionGrantHandler,
    inbox: RequestInbox,
    backend_resolver: BackendResolverInterface,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    paths = WorkspacePaths(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        request_inbox=inbox,
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


# -- handles_request_type / labels --


def test_handler_claims_workspace_request_type(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    assert handler.handles_request_type() == str(RequestType.WORKSPACE_PERMISSION)
    assert handler.kind_label() == "workspace access"


# -- render_request_detail_fragment --


def test_render_fragment_shows_verbs_rationale_and_target_choice(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="manage my sibling workspace",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    resolver = _HostBackendResolver(
        url_by_agent_and_service={},
        agent_host_id=str(HostId()),
        workspace_name_by_agent={str(target): "Target WS"},
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=resolver,
        mngr_forward_origin="http://localhost:8421",
    )
    assert "manage my sibling workspace" in body
    assert PERM_WORKSPACES_DESTROY in body
    # The all-vs-selected choice is offered and names the target workspace.
    assert 'name="target_scope"' in body
    assert "Target WS" in body
    assert "All workspaces" in body
    assert "Approve" in body and "Deny" in body
    assert "<html" not in body


def test_render_fragment_without_target_omits_target_choice(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="create and list workspaces",
        permissions=(PERM_WORKSPACES_READ,),
        target_workspace_id=None,
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=_HostBackendResolver(url_by_agent_and_service={}, agent_host_id=str(HostId())),
        mngr_forward_origin="",
    )
    # No radio group; a hidden target_scope=all is carried instead.
    assert 'type="radio"' not in body
    assert 'name="target_scope" value="all"' in body


# -- apply_grant_request --


def test_grant_selected_accumulates_target_in_anyof(tmp_path: Path) -> None:
    handler, sender = _make_handler(tmp_path)
    requester = AgentId()
    target = AgentId()
    host_id = HostId()
    host_path = _write_baseline_host_file(tmp_path, host_id)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(requester),
        rationale="destroy sibling",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _HostBackendResolver(
        url_by_agent_and_service={},
        agent_host_id=str(host_id),
        workspace_name_by_agent={str(target): "Target WS"},
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(
        f"/requests/{event.event_id}/grant", data={"permissions": PERM_WORKSPACES_DESTROY, "target_scope": "selected"}
    )
    assert response.status_code == 200, response.text
    assert response.get_json()["outcome"] == "GRANTED"

    config = json.loads(host_path.read_text())
    assert {"minds-workspaces": [PERM_WORKSPACES_DESTROY]} in config["rules"]
    patterns = [e["pattern"] for e in config["schemas"][PERM_WORKSPACES_DESTROY]["properties"]["path"]["anyOf"]]
    assert any(str(target) in p for p in patterns)
    # A response event was written and the requesting agent was notified.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "GRANTED"
    assert sender.sent_messages and sender.sent_messages[0][0] == str(requester)


def test_grant_all_uses_wildcard_segment(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    requester = AgentId()
    target = AgentId()
    host_id = HostId()
    host_path = _write_baseline_host_file(tmp_path, host_id)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(requester),
        rationale="destroy anything",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _HostBackendResolver(url_by_agent_and_service={}, agent_host_id=str(host_id))
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(
        f"/requests/{event.event_id}/grant", data={"permissions": PERM_WORKSPACES_DESTROY, "target_scope": "all"}
    )
    assert response.status_code == 200, response.text

    config = json.loads(host_path.read_text())
    patterns = [e["pattern"] for e in config["schemas"][PERM_WORKSPACES_DESTROY]["properties"]["path"]["anyOf"]]
    # The all-workspaces grant pins the wildcard id segment, not a specific id.
    assert any("[^/]+" in p for p in patterns)
    assert not any(str(target) in p for p in patterns)


def test_grant_rejects_empty_permissions(tmp_path: Path) -> None:
    handler, sender = _make_handler(tmp_path)
    host_id = HostId()
    _write_baseline_host_file(tmp_path, host_id)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="x",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _HostBackendResolver(url_by_agent_and_service={}, agent_host_id=str(host_id))
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/grant", data={})
    assert response.status_code == 400
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


def test_grant_returns_503_when_host_unknown(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="x",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    # The plain StaticBackendResolver reports the placeholder "localhost" host,
    # which is not a valid HostId, so the host cannot be resolved.
    resolver = StaticBackendResolver(url_by_agent_and_service={str(event.agent_id): {}})
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/grant", data={"permissions": PERM_WORKSPACES_DESTROY})
    assert response.status_code == 503


# -- apply_deny_request --


def test_deny_writes_response_and_notifies(tmp_path: Path) -> None:
    handler, sender = _make_handler(tmp_path)
    requester = AgentId()
    host_id = HostId()
    host_path = _write_baseline_host_file(tmp_path, host_id)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(requester),
        rationale="please",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _HostBackendResolver(url_by_agent_and_service={}, agent_host_id=str(host_id))
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "DENIED"
    assert sender.sent_messages and sender.sent_messages[0][0] == str(requester)
    # Deny must not write any grant to the host permissions file.
    assert json.loads(host_path.read_text()) == {"rules": [], "schemas": {}}


def test_inbox_detail_route_dispatches_to_handler(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path)
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="r",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _HostBackendResolver(
        url_by_agent_and_service={},
        agent_host_id=str(HostId()),
        workspace_name_by_agent={str(target): "Target WS"},
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.get(f"/inbox/detail/{event.event_id}")
    assert response.status_code == 200
    assert PERM_WORKSPACES_DESTROY in response.text
