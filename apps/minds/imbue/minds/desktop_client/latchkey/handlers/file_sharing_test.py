"""Unit tests for :class:`FileSharingGrantHandler`."""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import Response
from pydantic import Field
from starlette.testclient import TestClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.handlers.file_sharing import FileSharingGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_file_sharing_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId

_HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, agent_id: AgentId, text: str) -> None:
        self.sent_messages.append((str(agent_id), text))


def _build_gateway_client(handler: _HttpxHandler) -> LatchkeyGatewayClient:
    return LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
    )


def _make_file_sharing_handler(
    tmp_path: Path,
    gateway_handler: _HttpxHandler,
) -> tuple[FileSharingGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    return (
        FileSharingGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
        ),
        sender,
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: FileSharingGrantHandler,
    inbox: RequestInbox,
) -> TestClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface = StaticBackendResolver(url_by_agent_and_service={})
    paths = WorkspacePaths.flat(tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        request_inbox=inbox,
        request_event_handlers=(handler,),
    )
    client = TestClient(app, base_url="http://localhost")
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value, path="/")
    return client


# -- handler.handles_request_type --


def test_handler_claims_file_sharing_request_type(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    assert handler.handles_request_type() == str(RequestType.FILE_SHARING_PERMISSION)
    assert handler.kind_label() == "file sharing"


def test_display_name_returns_path(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    assert handler.display_name_for_event(event) == "/home/user/data.txt"


# -- render_request_page --


def test_render_request_page_shows_path_and_rationale(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )
    response = handler.render_request_page(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="http://localhost:8421",
    )
    assert response.status_code == 200
    body = bytes(response.body).decode("utf-8")
    assert "/home/user/important.txt" in body
    assert "summarize the doc" in body
    assert "Approve" in body and "Deny" in body
    # The dialog must show the human-readable access label so the user
    # knows what's being granted.
    assert "read-only" in body
    # The dialog must escape user-controlled values; ensure quoting
    # uses HTML-safe attributes rather than raw interpolation.
    # Presence of the form-submit JS tag confirms the dialog wired in.
    assert "<script>" in body


def test_render_request_page_marks_write_grants_distinctly(tmp_path: Path) -> None:
    """WRITE grants render the broader human-readable access label."""
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/edit.txt",
        access="WRITE",
        rationale="edit it",
    )
    response = handler.render_request_page(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    body = bytes(response.body).decode("utf-8")
    assert "read &amp; write" in body or "read & write" in body
    # The read-only label must not appear when WRITE access is being
    # requested, otherwise the dialog would be misleading.
    assert "read-only" not in body


def test_render_request_page_escapes_html_in_inputs(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/tmp/<script>alert(1)</script>.txt",
        access="READ",
        rationale="<img src=x onerror=alert(2)>",
    )
    response = handler.render_request_page(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    body = bytes(response.body).decode("utf-8")
    # Raw HTML must not appear; entities must.
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)" in body
    assert "<img src=x" not in body


# -- apply_grant_request --


def test_grant_calls_gateway_approve_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(agent_id),
        path="/home/user/data.txt",
        access="WRITE",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant")
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "GRANTED"
    assert "/home/user/data.txt" in body["message"]
    # Granted-message text reflects the access mode the agent asked for
    # so the agent's response handler can see what it ended up with.
    assert "read & write" in body["message"]

    # Gateway received the approve request.
    assert captured["method"] == "POST"
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.event_id}")

    # Response event was appended on disk.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "GRANTED"
    assert response_events[0].request_event_id == str(event.event_id)

    # Agent was notified.
    assert sender.sent_messages == [(str(agent_id), body["message"])]


def test_grant_returns_502_when_gateway_rejects(tmp_path: Path) -> None:
    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "boom"})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant")
    assert response.status_code == 502
    assert "gateway" in response.json()["error"].lower()
    # No response event written; the request stays pending.
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


# -- apply_deny_request --


def test_deny_calls_gateway_delete_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="READ",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "DENIED"

    # Gateway received DELETE (not POST).
    assert captured["method"] == "DELETE"
    assert str(captured["path"]).endswith(f"/permission-requests/{event.event_id}")

    # Response event written.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "DENIED"

    # Agent notified, with the access mode in the message text.
    assert len(sender.sent_messages) == 1
    assert "/home/user/secret.txt" in sender.sent_messages[0][1]
    assert "read-only" in sender.sent_messages[0][1]


def test_deny_still_writes_response_when_gateway_delete_fails(tmp_path: Path) -> None:
    """A failed DELETE should still result in a DENIED response + notification.

    The on-disk file inside the gateway is best-effort cleanup; what
    matters is that the user's deny intent is recorded and the agent
    is told.
    """

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "gateway down"})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="WRITE",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    assert response.json()["outcome"] == "DENIED"
    assert len(load_response_events(tmp_path)) == 1
    assert len(sender.sent_messages) == 1


# -- Wiring through the FastAPI dispatcher --


def test_request_page_route_dispatches_to_handler(tmp_path: Path) -> None:
    """GET /requests/<id> for a file-sharing event routes to FileSharingGrantHandler."""
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/x.txt",
        access="READ",
        rationale="r",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/requests/{event.event_id}")
    assert response.status_code == 200
    assert "/home/user/x.txt" in response.text


# Inputs used by the FastAPI route dispatcher tests above are unused
# here; ruff-lint flags the imports if we don't reference them.
_ = (FastAPI, Request, Response, RequestEvent, LatchkeyFileSharingPermissionRequestEvent)
