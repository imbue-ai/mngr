"""Integration tests for the permission routes wired into ``app.py``.

Drives the FastAPI app via ``TestClient`` against a real catalog and a
fake ``LatchkeyPermissionGrantHandler`` so the routes are exercised
end-to-end without spawning any subprocesses.
"""

from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import Field

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog
from imbue.minds.desktop_client.latchkey.store import PermissionsConfig
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.mngr.primitives import AgentId


class _RecordingHandler(LatchkeyPermissionGrantHandler):
    """Subclass of ``LatchkeyPermissionGrantHandler`` that records calls instead of running them.

    Inheriting from the real handler keeps the ``app.state`` typing happy
    without polluting production code with a Protocol.
    """

    was_granted_outcome: bool = Field(default=True)
    grant_message: str = Field(default="granted")
    deny_message: str = Field(default="denied")
    grant_calls: list[dict[str, object]] = Field(default_factory=list)
    deny_calls: list[dict[str, object]] = Field(default_factory=list)

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> tuple[bool, str, RequestResponseEvent]:
        self.grant_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "service_name": service_info.name,
                "granted_permissions": tuple(granted_permissions),
            }
        )
        status = RequestStatus.GRANTED if self.was_granted_outcome else RequestStatus.DENIED
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            service_name=service_info.name,
        )
        return self.was_granted_outcome, self.grant_message, response_event

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
    was_granted_outcome: bool = True,
    grant_message: str = "granted",
) -> _RecordingHandler:
    """Build a ``_RecordingHandler`` with stub probes that won't be exercised in routing tests."""
    return _RecordingHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_binary="/nonexistent"),
        services_catalog=load_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary="/nonexistent"),
        was_granted_outcome=was_granted_outcome,
        grant_message=grant_message,
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
        permission_handler=handler,
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
    assert 'id="latchkey-approve-btn"' in body
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
        f"/requests/{request.event_id}/permission/grant",
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

    response = client.post(f"/requests/{request.event_id}/permission/grant", data={})

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
        was_granted_outcome=False,
        grant_message="Your sign-in flow did not finish. Reason: user cancelled.",
    )
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/permission/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 200
    payload = response.json()
    # No separate AUTH_FAILED status: a failed sign-in is reported as DENIED
    # with a distinct message so the agent can tell the user what happened.
    assert payload["outcome"] == "DENIED"
    assert "user cancelled" in payload["message"]


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

    response = client.post(f"/requests/{request.event_id}/permission/deny")

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
        f"/requests/{request.event_id}/permission/grant",
        data={"permissions": ["some-perm"]},
    )

    assert response.status_code == 400
    assert handler.grant_calls == []


def test_get_permission_request_page_pre_checks_existing_grants(tmp_path: Path) -> None:
    agent_id = AgentId()
    # Pre-populate permissions.json so the dialog should pre-check those.
    save_permissions(
        permissions_path_for_agent(tmp_path, agent_id),
        PermissionsConfig(rules=({"slack-api": ["slack-chat-read"]},)),
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
        f"/requests/{request.event_id}/permission/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 403
    assert handler.grant_calls == []
