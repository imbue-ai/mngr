"""Unit tests for :class:`WorkspacePermissionGrantHandler`."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

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

_HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]

# Verb permission names referenced by these tests (the catalog lives in the
# shared ``workspace_permissions.json``).
PERM_WORKSPACES_DESTROY: Final = "minds-workspaces-destroy"
PERM_WORKSPACES_READ: Final = "minds-workspaces-read"


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    concurrency_group: ConcurrencyGroup | None = None
    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, agent_id: AgentId, text: str) -> None:
        self.sent_messages.append((str(agent_id), text))


class _NamingBackendResolver(StaticBackendResolver):
    """Static resolver that maps agent ids to workspace names (for display)."""

    workspace_name_by_agent: dict[str, str] = Field(default_factory=dict)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.workspace_name_by_agent.get(str(agent_id))


def _build_gateway_client(handler: _HttpxHandler) -> LatchkeyGatewayClient:
    return LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
    )


def _make_handler(
    tmp_path: Path,
    gateway_handler: _HttpxHandler,
) -> tuple[WorkspacePermissionGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    return (
        WorkspacePermissionGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
        ),
        sender,
    )


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
    handler, _sender = _make_handler(tmp_path, lambda r: httpx.Response(204))
    assert handler.handles_request_type() == str(RequestType.WORKSPACE_PERMISSION)
    assert handler.kind_label() == "workspace access"


# -- render_request_detail_fragment --


def test_render_fragment_shows_verbs_rationale_and_target_choice(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path, lambda r: httpx.Response(204))
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="manage my sibling workspace",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    resolver = _NamingBackendResolver(
        url_by_agent_and_service={},
        workspace_name_by_agent={str(target): "Target WS"},
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=resolver,
        mngr_forward_origin="http://localhost:8421",
    )
    assert "manage my sibling workspace" in body
    assert PERM_WORKSPACES_DESTROY in body
    assert 'name="target_scope"' in body
    assert "Target WS" in body
    assert "All workspaces" in body
    assert "Approve" in body and "Deny" in body
    assert "<html" not in body


def test_render_fragment_without_target_omits_target_choice(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path, lambda r: httpx.Response(204))
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="create and list workspaces",
        permissions=(PERM_WORKSPACES_READ,),
        target_workspace_id=None,
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=_NamingBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    assert 'type="radio"' not in body
    assert 'name="target_scope" value="all"' in body


# -- apply_grant_request --


def test_grant_selected_sends_override_with_target(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_handler(tmp_path, _gateway_handler)
    requester = AgentId()
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(requester),
        rationale="destroy sibling",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(
        url_by_agent_and_service={},
        workspace_name_by_agent={str(target): "Target WS"},
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(
        f"/requests/{event.event_id}/grant",
        data={"permissions": PERM_WORKSPACES_DESTROY, "target_scope": "selected"},
    )
    assert response.status_code == 200, response.text
    assert response.get_json()["outcome"] == "GRANTED"
    # The gateway received an approve POST with the verbs + selected target.
    assert captured["method"] == "POST"
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.event_id}")
    assert json.loads(captured["content"]) == {
        "permissions": [PERM_WORKSPACES_DESTROY],
        "target_workspace_id": str(target),
    }
    # The grant message names the selected workspace, a response event is
    # written, and the requesting agent is notified.
    assert "Target WS" in response.get_json()["message"]
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1 and response_events[0].status == "GRANTED"
    assert sender.sent_messages and sender.sent_messages[0][0] == str(requester)


def test_grant_all_sends_override_with_null_target(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = _make_handler(tmp_path, _gateway_handler)
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="destroy anything",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(url_by_agent_and_service={})
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(
        f"/requests/{event.event_id}/grant",
        data={"permissions": PERM_WORKSPACES_DESTROY, "target_scope": "all"},
    )
    assert response.status_code == 200, response.text
    assert json.loads(captured["content"]) == {
        "permissions": [PERM_WORKSPACES_DESTROY],
        "target_workspace_id": None,
    }
    assert "all workspaces" in response.get_json()["message"]


def test_grant_rejects_empty_permissions(tmp_path: Path) -> None:
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc"})

    handler, sender = _make_handler(tmp_path, _gateway_handler)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="x",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(url_by_agent_and_service={})
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/grant", data={})
    assert response.status_code == 400
    assert gateway_called is False
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


def test_grant_returns_502_when_gateway_rejects(tmp_path: Path) -> None:
    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "boom"})

    handler, sender = _make_handler(tmp_path, _gateway_handler)
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="x",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(url_by_agent_and_service={})
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/grant", data={"permissions": PERM_WORKSPACES_DESTROY})
    assert response.status_code == 502
    assert "gateway" in response.get_json()["error"].lower()
    # The request stays pending: no response event, no agent notification.
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


# -- apply_deny_request --


def test_deny_calls_gateway_delete_writes_response_notifies(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    handler, sender = _make_handler(tmp_path, _gateway_handler)
    requester = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(requester),
        rationale="please",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(AgentId()),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(url_by_agent_and_service={})
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    assert captured["method"] == "DELETE"
    assert str(captured["path"]).endswith(f"/permission-requests/{event.event_id}")
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1 and response_events[0].status == "DENIED"
    assert sender.sent_messages and sender.sent_messages[0][0] == str(requester)


def test_inbox_detail_route_dispatches_to_handler(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path, lambda r: httpx.Response(204))
    target = AgentId()
    event = create_latchkey_workspace_permission_request_event(
        agent_id=str(AgentId()),
        rationale="r",
        permissions=(PERM_WORKSPACES_DESTROY,),
        target_workspace_id=str(target),
    )
    inbox = RequestInbox().add_request(event)
    resolver = _NamingBackendResolver(
        url_by_agent_and_service={},
        workspace_name_by_agent={str(target): "Target WS"},
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, resolver)

    response = client.get(f"/inbox/detail/{event.event_id}")
    assert response.status_code == 200
    assert PERM_WORKSPACES_DESTROY in response.text
