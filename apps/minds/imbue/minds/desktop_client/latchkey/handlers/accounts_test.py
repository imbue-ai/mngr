"""Unit tests for :class:`AccountsPermissionGrantHandler`."""

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
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.chrome_state import AccountsPermissionDetail
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.handlers.accounts import AccountsPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_accounts_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId

_HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    # This double overrides ``send`` entirely and never dispatches to a
    # concurrency group, so relax the base class's required field.
    concurrency_group: ConcurrencyGroup | None = None
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


def _make_accounts_handler(
    tmp_path: Path,
    gateway_handler: _HttpxHandler,
) -> tuple[AccountsPermissionGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    return (
        AccountsPermissionGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
        ),
        sender,
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: AccountsPermissionGrantHandler,
    inbox: RequestInbox,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    backend_resolver: BackendResolverInterface = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
        request_inbox=inbox,
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def test_handler_claims_accounts_request_type(tmp_path: Path) -> None:
    handler, _sender = _make_accounts_handler(tmp_path, lambda _req: httpx.Response(200))
    assert handler.handles_request_type() == str(RequestType.ACCOUNTS_PERMISSION)


def test_detail_payload_carries_rationale(tmp_path: Path) -> None:
    handler, _sender = _make_accounts_handler(tmp_path, lambda _req: httpx.Response(200))
    agent_id = str(AgentId())
    event = create_latchkey_accounts_permission_request_event(
        agent_id=agent_id,
        rationale="needs to find the right account",
    )
    payload = handler.build_request_detail_payload(event, StaticBackendResolver(url_by_agent_and_service={}))
    # All-or-nothing grant: the accounts detail carries no per-permission
    # choice (the component enables Approve on first paint).
    assert isinstance(payload, AccountsPermissionDetail)
    assert payload.rationale == "needs to find the right account"
    assert payload.agent_id == agent_id
    assert payload.request_id == str(event.event_id)


def test_grant_calls_gateway_approve_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_accounts_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_accounts_permission_request_event(agent_id=str(agent_id), rationale="need data")
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"

    # Gateway received the approve POST with NO override body (the effect is fixed).
    assert captured["method"] == "POST"
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.event_id}")
    assert captured["content"] == b""

    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "GRANTED"
    assert response_events[0].request_event_id == str(event.event_id)
    assert sender.sent_messages == [(str(agent_id), response.get_json()["message"])]


def test_deny_deletes_request_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200)

    handler, sender = _make_accounts_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_accounts_permission_request_event(agent_id=str(agent_id), rationale="need data")
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    assert captured["method"] == "DELETE"

    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "DENIED"
    assert sender.sent_messages == [(str(agent_id), response.get_json()["message"])]
