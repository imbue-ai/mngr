"""Integration tests for the permission routes wired into ``app.py``.

Drives the FastAPI app via ``TestClient`` against a real catalog and a
fake ``LatchkeyPermissionGrantHandler`` so the routes are exercised
end-to-end without spawning any subprocesses.
"""

import uuid
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from pydantic import Field

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.permissions import GrantOutcome
from imbue.minds.desktop_client.latchkey.permissions import GrantResult
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog
from imbue.minds.desktop_client.latchkey.store import LatchkeyPermissionsConfig
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.request_events import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.mngr.primitives import AgentId

_OTHER_REQUEST_TYPE = "OTHER"


def _make_other_request_event(agent_id: str) -> RequestEvent:
    """Build a generic RequestEvent with a custom ``request_type`` for dispatcher tests."""
    return RequestEvent(
        timestamp=IsoTimestamp("2026-01-01T00:00:00.000000Z"),
        type=EventType("other_request"),
        event_id=EventId(f"evt-{uuid.uuid4().hex}"),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        agent_id=agent_id,
        request_type=_OTHER_REQUEST_TYPE,
    )


class _RecordingHandler(LatchkeyPermissionGrantHandler):
    """Subclass of ``LatchkeyPermissionGrantHandler`` that records calls instead of running them.

    Inheriting from the real handler keeps the ``app.state`` typing happy
    without polluting production code with a Protocol.
    """

    grant_outcome: GrantOutcome = Field(default=GrantOutcome.GRANTED)
    grant_message: str = Field(default="granted")
    grant_set_credentials_example: str | None = Field(default=None)
    deny_message: str = Field(default="denied")
    grant_calls: list[dict[str, object]] = Field(default_factory=list)
    deny_calls: list[dict[str, object]] = Field(default_factory=list)

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> GrantResult:
        self.grant_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "service_name": service_info.name,
                "granted_permissions": tuple(granted_permissions),
            }
        )
        # NEEDS_MANUAL_CREDENTIALS keeps the request pending and writes
        # no response event; the other outcomes resolve it.
        if self.grant_outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS:
            return GrantResult(
                outcome=self.grant_outcome,
                message=self.grant_message,
                response_event=None,
                set_credentials_example=self.grant_set_credentials_example,
            )
        status = RequestStatus.GRANTED if self.grant_outcome == GrantOutcome.GRANTED else RequestStatus.DENIED
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            service_name=service_info.name,
        )
        return GrantResult(
            outcome=self.grant_outcome,
            message=self.grant_message,
            response_event=response_event,
            set_credentials_example=None,
        )

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
    ) -> tuple[str, RequestResponseEvent]:
        self.deny_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "service_name": service_info.name,
            }
        )
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=RequestStatus.DENIED,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            service_name=service_info.name,
        )
        return self.deny_message, response_event


def _get_app_request_inbox(client: TestClient) -> RequestInbox:
    """Pull the live request inbox out of the FastAPI app behind a TestClient."""
    app = client.app
    assert isinstance(app, FastAPI)
    inbox = app.state.request_inbox
    assert isinstance(inbox, RequestInbox)
    return inbox


def _make_recording_handler(
    tmp_path: Path,
    grant_outcome: GrantOutcome = GrantOutcome.GRANTED,
    grant_message: str = "granted",
    grant_set_credentials_example: str | None = None,
) -> _RecordingHandler:
    """Build a ``_RecordingHandler`` with stub probes that won't be exercised in routing tests."""
    return _RecordingHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_binary="/nonexistent"),
        services_catalog=load_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary="/nonexistent"),
        grant_outcome=grant_outcome,
        grant_message=grant_message,
        grant_set_credentials_example=grant_set_credentials_example,
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: _RecordingHandler,
    inbox: RequestInbox,
) -> TestClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    paths = WorkspacePaths(data_dir=tmp_path)

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


def test_get_permission_request_page_renders_dialog_with_default_checks(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="I need to read the team channel to summarize today's discussion.",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/requests/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    assert "Slack" in body
    assert "I need to read" in body
    # Default-checked permissions appear with checked attribute.
    assert 'value="slack-read-all"' in body
    assert "checked" in body
    # Approve must be disabled in initial markup.
    assert 'id="permissions-approve-btn"' in body
    assert "disabled" in body


def test_post_permission_grant_calls_handler_and_resolves_inbox(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all", "slack-write-all"]},
    )

    assert response.status_code == 200
    assert response.json() == {"outcome": "GRANTED", "message": "granted"}
    assert len(handler.grant_calls) == 1
    call = handler.grant_calls[0]
    assert call["service_name"] == "slack"
    assert call["granted_permissions"] == ("slack-read-all", "slack-write-all")
    # The request must no longer appear as pending after grant.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 0


def test_post_permission_grant_rejects_empty_permissions(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{request.event_id}/grant", data={})

    assert response.status_code == 400
    assert handler.grant_calls == []
    # The request must remain pending so the user can try again.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


def test_post_permission_grant_with_failed_signin_returns_denied_outcome(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(
        tmp_path,
        grant_outcome=GrantOutcome.DENIED,
        grant_message="Your sign-in flow did not finish. Reason: user cancelled.",
    )
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 200
    payload = response.json()
    # No separate AUTH_FAILED status: a failed sign-in is reported as DENIED
    # with a distinct message so the agent can tell the user what happened.
    assert payload["outcome"] == "DENIED"
    assert "user cancelled" in payload["message"]


def test_post_permission_grant_with_manual_credentials_keeps_request_pending(tmp_path: Path) -> None:
    """NEEDS_MANUAL_CREDENTIALS must echo the example command and not resolve the inbox."""
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    expected_example = 'latchkey auth set slack -H "Authorization: Bearer xoxb-..."'
    handler = _make_recording_handler(
        tmp_path,
        grant_outcome=GrantOutcome.NEEDS_MANUAL_CREDENTIALS,
        grant_message="Slack does not support browser sign-in.",
        grant_set_credentials_example=expected_example,
    )
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert payload["set_credentials_example"] == expected_example
    # The request must remain pending so the user can click Approve again
    # after running the suggested command.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


def test_post_permission_deny_calls_handler_and_resolves_inbox(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{request.event_id}/deny")

    assert response.status_code == 200
    assert response.json() == {"outcome": "DENIED"}
    assert len(handler.deny_calls) == 1
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 0


def test_post_permission_grant_unknown_service_returns_400(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="not-a-real-service",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["some-perm"]},
    )

    assert response.status_code == 400
    assert handler.grant_calls == []


def test_get_permission_request_page_pre_checks_existing_grants(tmp_path: Path) -> None:
    agent_id = AgentId()
    # Pre-populate latchkey_permissions.json so the dialog should pre-check those.
    save_permissions(
        permissions_path_for_agent(tmp_path, agent_id),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-chat-read"]},)),
    )
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/requests/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    # The previously-granted permission appears checked.
    chat_read_idx = body.find('value="slack-chat-read"')
    assert chat_read_idx != -1
    # Find the surrounding <input ...> tag and assert it has 'checked'.
    tag_start = body.rfind("<input", 0, chat_read_idx)
    tag_end = body.find(">", chat_read_idx)
    assert "checked" in body[tag_start:tag_end]


def test_unauthenticated_grant_post_returns_403(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_permission_request_event(
        agent_id=str(agent_id),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)
    # Drop the cookie to simulate an unauthenticated request.
    client.cookies.clear()

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 403
    assert handler.grant_calls == []


# -- Dispatch by request type --


class _StubOtherHandler(RequestEventHandler):
    """Records the request events it is asked to grant or deny.

    Used to verify the unified ``/requests/{id}/{grant,deny}`` dispatcher
    forwards to the handler whose ``handles_request_type`` matches the
    event, without exercising any real handler side effects.
    """

    grant_event_ids: list[str] = Field(default_factory=list)
    deny_event_ids: list[str] = Field(default_factory=list)

    def handles_request_type(self) -> str:
        return _OTHER_REQUEST_TYPE

    def kind_label(self) -> str:
        return "other"

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        return ""

    def render_request_page(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> Response:
        return Response(content="ok", status_code=200)

    async def apply_grant_request(self, request: Request, req_event: RequestEvent) -> Response:
        self.grant_event_ids.append(str(req_event.event_id))
        return Response(content="granted", status_code=200)

    async def apply_deny_request(self, request: Request, req_event: RequestEvent) -> Response:
        self.deny_event_ids.append(str(req_event.event_id))
        return Response(content="denied", status_code=200)


def _build_authenticated_client_with_handlers(
    tmp_path: Path,
    handlers: tuple[RequestEventHandler, ...],
    inbox: RequestInbox,
) -> TestClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    paths = WorkspacePaths(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        request_inbox=inbox,
        request_event_handlers=handlers,
    )
    client = TestClient(app, base_url="http://localhost")
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value, path="/")
    return client


def test_dispatcher_routes_grant_to_handler_matching_request_type(tmp_path: Path) -> None:
    """Two handlers registered; only the one whose handles_request_type matches must be called."""
    other_request = _make_other_request_event(agent_id=str(AgentId()))
    permission_request = create_latchkey_permission_request_event(
        agent_id=str(AgentId()),
        service_name="slack",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(other_request).add_request(permission_request)
    other_handler = _StubOtherHandler()
    permission_handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client_with_handlers(
        tmp_path,
        handlers=(other_handler, permission_handler),
        inbox=inbox,
    )

    # Granting an OTHER event must hit the other handler only.
    other_response = client.post(f"/requests/{other_request.event_id}/grant")
    assert other_response.status_code == 200
    assert other_handler.grant_event_ids == [str(other_request.event_id)]
    assert permission_handler.grant_calls == []

    # Granting a LATCHKEY_PERMISSION event must hit the permission handler only.
    perm_response = client.post(
        f"/requests/{permission_request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
    )
    assert perm_response.status_code == 200
    assert other_handler.grant_event_ids == [str(other_request.event_id)]
    assert len(permission_handler.grant_calls) == 1


def test_dispatcher_returns_400_when_no_handler_claims_request_type(tmp_path: Path) -> None:
    """A request whose type no registered handler claims must produce a 400, not a 500."""
    other_request = _make_other_request_event(agent_id=str(AgentId()))
    inbox = RequestInbox().add_request(other_request)
    # Only the latchkey-permission handler is registered, so the OTHER
    # request has nowhere to go.
    permission_handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client_with_handlers(
        tmp_path,
        handlers=(permission_handler,),
        inbox=inbox,
    )

    response = client.post(f"/requests/{other_request.event_id}/grant")
    assert response.status_code == 400
    assert permission_handler.grant_calls == []
