"""Integration tests for the permission routes wired into ``app.py``.

Drives the Flask app via the test client against a real catalog and a
fake ``LatchkeyPermissionGrantHandler`` so the routes are exercised
end-to-end without spawning any subprocesses.
"""

import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from flask import Request
from flask import Response
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import _build_requests_payload
from imbue.minds.desktop_client.app import _displayable_pending_requests
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantOutcome
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantResult
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.predefined import ManualCredentialSubmission
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiManualCredentialsPrompt
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.credential_commands import CredentialCommandParameter
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.testing import FakeLatchkey

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

    Inheriting from the real handler keeps the ``request_event_handlers``
    typing happy without polluting production code with a Protocol.
    """

    grant_outcome: GrantOutcome = Field(default=GrantOutcome.GRANTED)
    grant_message: str = Field(default="granted")
    grant_manual_credentials: UiManualCredentialsPrompt | None = Field(default=None)
    deny_message: str = Field(default="denied")
    grant_calls: list[dict[str, object]] = Field(default_factory=list)
    deny_calls: list[dict[str, object]] = Field(default_factory=list)

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        host_id: HostId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
        account_choice: str,
        manual_credentials: ManualCredentialSubmission,
    ) -> GrantResult:
        self.grant_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "host_id": str(host_id),
                "scope": service_info.scope,
                "granted_permissions": tuple(granted_permissions),
                "account_choice": account_choice,
                "manual_credentials": manual_credentials,
            }
        )
        # NEEDS_MANUAL_CREDENTIALS and FAILED keep the request pending and
        # write no response event; the other outcomes resolve it.
        if self.grant_outcome in (GrantOutcome.NEEDS_MANUAL_CREDENTIALS, GrantOutcome.FAILED):
            return GrantResult(
                outcome=self.grant_outcome,
                message=self.grant_message,
                response_event=None,
                manual_credentials=self.grant_manual_credentials,
            )
        status = RequestStatus.GRANTED if self.grant_outcome == GrantOutcome.GRANTED else RequestStatus.DENIED
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            scope=service_info.scope,
        )
        return GrantResult(
            outcome=self.grant_outcome,
            message=self.grant_message,
            response_event=response_event,
            manual_credentials=None,
        )

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        scope: str,
        display_name: str,
    ) -> tuple[str, RequestResponseEvent]:
        self.deny_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "scope": scope,
                "display_name": display_name,
            }
        )
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=RequestStatus.DENIED,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            scope=scope,
        )
        return self.deny_message, response_event


def _get_app_request_inbox(client: FlaskClient) -> RequestInbox:
    """Pull the live request inbox out of the Flask app behind a test client."""
    inbox = get_state(client.application).request_inbox
    assert isinstance(inbox, RequestInbox)
    return inbox


_TEST_SERVICES_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "description": "Any interaction with the Slack API.",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all"},
                {"name": "slack-chat-read"},
            ],
        },
    ],
    "github": [
        {
            "scope": "github-rest-api",
            "display_name": "GitHub",
            "permissions": [{"name": "github-read-all"}],
        },
    ],
}


# The account the stub latchkey reports as signed in, so the dialog has a
# concrete account to preselect and pre-check grants against.
_TEST_ACCOUNT: str = "alice@example.com"


def _make_stub_latchkey(tmp_path: Path) -> FakeLatchkey:
    """Return a non-spawning ``Latchkey`` reporting one valid signed-in account."""
    latchkey = FakeLatchkey(latchkey_directory=tmp_path)
    latchkey.configure(
        service_info=LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID,
            accounts=(ServiceAccountCredential(account=_TEST_ACCOUNT, credential_status=CredentialStatus.VALID),),
            auth_options=frozenset({LATCHKEY_AUTH_OPTION_BROWSER}),
            set_credentials_example=None,
        ),
    )
    return latchkey


def _make_recording_handler(
    tmp_path: Path,
    grant_outcome: GrantOutcome = GrantOutcome.GRANTED,
    grant_message: str = "granted",
    grant_manual_credentials: UiManualCredentialsPrompt | None = None,
) -> _RecordingHandler:
    """Build a ``_RecordingHandler`` with stub probes that won't be exercised in routing tests."""
    gateway_client = build_fake_gateway_client()
    return _RecordingHandler(
        data_dir=tmp_path,
        latchkey=_make_stub_latchkey(tmp_path),
        services_catalog=ServicesCatalog.from_catalog_payload(_TEST_SERVICES_CATALOG_PAYLOAD),
        mngr_message_sender=MngrMessageSender(
            mngr_caller=RecordingMngrCaller(),
            # ``_RecordingHandler`` overrides grant/deny, so the sender is never
            # used; an un-entered group satisfies the required field.
            concurrency_group=ConcurrencyGroup(name="permission-routes-test-unused"),
        ),
        gateway_client=gateway_client,
        grant_outcome=grant_outcome,
        grant_message=grant_message,
        grant_manual_credentials=grant_manual_credentials,
    )


class _HostKnownStaticResolver(StaticBackendResolver):
    """``StaticBackendResolver`` that reports a configurable ``host_id`` for every agent.

    Latchkey permissions are stored per-host (see
    :func:`permissions_path_for_host`), so the route layer maps the
    incoming agent_id to a host_id via the backend resolver before
    applying a grant. The default ``StaticBackendResolver`` reports
    ``host_id="localhost"`` which isn't a valid :class:`HostId`; this
    subclass lets tests pretend the resolver has seen the agent and
    knows its host so the grant POST does not 503.
    """

    fixed_host_id: HostId = Field(description="Host id the resolver reports for every agent.")
    known_agent_ids: tuple[AgentId, ...] = Field(
        default=(),
        description="Agents the resolver claims to know; others still return None.",
    )

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.known_agent_ids:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=str(self.fixed_host_id))

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        # A name per known agent so the inbox attributes each
        # pending request to its workspace (via the shared workspace name).
        if agent_id not in self.known_agent_ids:
            return None
        return f"ws-{agent_id}"


def _build_authenticated_client(
    tmp_path: Path,
    handler: _RecordingHandler,
    inbox: RequestInbox,
    agent_id: AgentId | None = None,
    host_id: HostId | None = None,
) -> FlaskClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface
    if agent_id is not None:
        backend_resolver = _HostKnownStaticResolver(
            url_by_agent_and_service={},
            fixed_host_id=host_id or HostId(),
            known_agent_ids=(agent_id,),
        )
    else:
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
    client = app.test_client()
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)
    return client


def test_requests_payload_excludes_unresolvable_hosts(tmp_path: Path) -> None:
    """The SSE badge payload counts only requests whose host is resolvable.

    The badge count and the rendered cards are driven off the same filter,
    so a request from a since-stopped machine neither inflates the badge
    nor appears in the panel.
    """
    known_agent = AgentId()
    stopped_agent = AgentId()
    visible_request = create_latchkey_predefined_permission_request_event(
        agent_id=str(known_agent),
        scope="slack-api",
        rationale="visible",
    )
    hidden_request = create_latchkey_predefined_permission_request_event(
        agent_id=str(stopped_agent),
        scope="slack-api",
        rationale="hidden",
    )
    inbox = RequestInbox().add_request(visible_request).add_request(hidden_request)
    backend_resolver = _HostKnownStaticResolver(
        url_by_agent_and_service={},
        fixed_host_id=HostId(),
        known_agent_ids=(known_agent,),
    )

    displayable = _displayable_pending_requests(inbox, backend_resolver)
    payload = _build_requests_payload(inbox, backend_resolver)

    assert [str(req.event_id) for req in displayable] == [str(visible_request.event_id)]
    assert payload["count"] == 1
    assert payload["request_ids"] == [str(visible_request.event_id)]


def test_post_permission_grant_calls_handler_and_resolves_inbox(tmp_path: Path) -> None:
    agent_id = AgentId()
    host_id = HostId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id, host_id=host_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all", "slack-write-all"], "account": _TEST_ACCOUNT},
    )

    assert response.status_code == 200
    assert response.get_json() == {"outcome": "GRANTED", "message": "granted"}
    assert len(handler.grant_calls) == 1
    call = handler.grant_calls[0]
    assert call["scope"] == "slack-api"
    assert call["granted_permissions"] == ("slack-read-all", "slack-write-all")
    # The route resolved the agent to its host via the backend resolver
    # and threaded that host_id into the grant call so the handler
    # writes to ``permissions_path_for_host``.
    assert call["host_id"] == str(host_id)
    # The request must no longer appear as pending after grant.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 0


def test_post_permission_grant_rejects_empty_permissions(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
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


def test_post_permission_grant_with_failed_signin_keeps_request_pending(tmp_path: Path) -> None:
    """A failed sign-in is reported as FAILED and must not auto-deny the request."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(
        tmp_path,
        grant_outcome=GrantOutcome.FAILED,
        grant_message="Sign-in to Slack did not complete. Reason: user cancelled.",
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
    )

    assert response.status_code == 200
    payload = response.get_json()
    # FAILED is a distinct outcome from DENIED: the approval failed but the
    # request is not resolved, so the agent's message carries the reason.
    assert payload["outcome"] == "FAILED"
    assert "user cancelled" in payload["message"]
    # The request must remain pending so the user can click Approve again.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


def test_post_permission_grant_with_manual_credentials_keeps_request_pending(tmp_path: Path) -> None:
    """NEEDS_MANUAL_CREDENTIALS must return the credential form and not resolve the inbox."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    expected_prompt = UiManualCredentialsPrompt(
        parameters=(CredentialCommandParameter(name="token", label="Token"),),
        message="Slack does not support browser sign-in",
    )
    handler = _make_recording_handler(
        tmp_path,
        grant_outcome=GrantOutcome.NEEDS_MANUAL_CREDENTIALS,
        grant_message="Slack does not support browser sign-in.",
        grant_manual_credentials=expected_prompt,
    )
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert payload["manual_credentials"] == {
        "parameters": [{"name": "token", "label": "Token"}],
        "message": "Slack does not support browser sign-in",
    }
    # The request must remain pending so the user can fill the form in and
    # click Approve again.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


def test_post_permission_grant_forwards_the_submitted_credential_form(tmp_path: Path) -> None:
    """The values typed into the credential form must reach the grant flow."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={
            "permissions": ["slack-read-all"],
            "account": _TEST_ACCOUNT,
            "manual_credentials": json.dumps({"token": "xoxb-9137"}),
            "account_name": "work",
        },
    )

    assert response.status_code == 200
    assert handler.grant_calls[0]["manual_credentials"] == ManualCredentialSubmission(
        value_by_parameter_name={"token": "xoxb-9137"},
        account_name="work",
    )


def test_post_permission_grant_rejects_a_malformed_credential_form(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={
            "permissions": ["slack-read-all"],
            "account": _TEST_ACCOUNT,
            "manual_credentials": json.dumps(["xoxb-9137"]),
        },
    )

    assert response.status_code == 400
    assert "JSON object of strings" in response.get_json()["error"]
    assert handler.grant_calls == []


def test_post_permission_deny_calls_handler_and_resolves_inbox(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{request.event_id}/deny")

    assert response.status_code == 200
    assert response.get_json() == {"outcome": "DENIED"}
    assert len(handler.deny_calls) == 1
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 0


def test_inbox_page_drops_request_after_resolution(tmp_path: Path) -> None:
    """A granted/denied request no longer renders in the inbox.

    The granted request lingers in the append-only log, so the inbox handler
    must detect the recorded response and drop the card (its re-submittable
    grant/deny form included) instead of re-rendering it.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    # Deny resolves the request without needing a discovered host.
    deny = client.post(f"/requests/{request.event_id}/deny")
    assert deny.status_code == 200

    # The resolved request no longer appears in the inbox list, so it can't be
    # re-actioned (the SPA reads this JSON in place of the old SSR page).
    listing = client.get("/ui/api/inbox")
    assert listing.status_code == 200
    card_ids = [card["id"] for card in listing.get_json()["cards"]]
    assert str(request.event_id) not in card_ids


def test_post_permission_grant_after_resolution_returns_409(tmp_path: Path) -> None:
    """A second grant on an already-resolved request is rejected, not re-applied."""
    agent_id = AgentId()
    host_id = HostId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id, host_id=host_id)

    first = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
    )
    assert first.status_code == 200
    assert len(handler.grant_calls) == 1

    second = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
    )
    assert second.status_code == 409
    # The handler must not have been invoked a second time.
    assert len(handler.grant_calls) == 1


def test_post_permission_deny_after_resolution_returns_409(tmp_path: Path) -> None:
    """A second deny on an already-resolved request is rejected, not re-applied."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    assert client.post(f"/requests/{request.event_id}/deny").status_code == 200
    assert len(handler.deny_calls) == 1

    second = client.post(f"/requests/{request.event_id}/deny")
    assert second.status_code == 409
    assert len(handler.deny_calls) == 1


def test_post_permission_grant_unknown_service_returns_400(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="not-a-real-scope",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["some-perm"], "account": _TEST_ACCOUNT},
    )

    assert response.status_code == 400
    assert handler.grant_calls == []


def test_post_permission_grant_returns_503_when_host_not_yet_discovered(tmp_path: Path) -> None:
    """Grant fails fast when the agent's host can't be resolved.

    Latchkey state is keyed by host_id; if the backend resolver hasn't
    seen the agent yet (or only reports a non-:class:`HostId` placeholder
    like the static resolver's default ``"localhost"``) the route would
    otherwise write the grant to the wrong file. 503 tells the UI to
    retry, instead of silently mis-keying state.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    # No ``agent_id=`` kwarg -> default ``StaticBackendResolver`` -> host
    # cannot be resolved.
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
    )

    assert response.status_code == 503
    assert handler.grant_calls == []
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


def test_unauthenticated_grant_post_returns_403(tmp_path: Path) -> None:
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)
    # Drop the cookie to simulate an unauthenticated request.
    client.delete_cookie(SESSION_COOKIE_NAME)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
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

    def build_request_detail_payload(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        return UiUnsupportedDetail(message="stub")

    def apply_grant_request(self, request: Request, req_event: RequestEvent) -> Response:
        self.grant_event_ids.append(str(req_event.event_id))
        return make_response(content="granted", status_code=200)

    def apply_deny_request(self, request: Request, req_event: RequestEvent) -> Response:
        self.deny_event_ids.append(str(req_event.event_id))
        return make_response(content="denied", status_code=200)


def _build_authenticated_client_with_handlers(
    tmp_path: Path,
    handlers: tuple[RequestEventHandler, ...],
    inbox: RequestInbox,
    known_agent_ids: tuple[AgentId, ...] = (),
    host_id: HostId | None = None,
) -> FlaskClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface
    if known_agent_ids:
        backend_resolver = _HostKnownStaticResolver(
            url_by_agent_and_service={},
            fixed_host_id=host_id or HostId(),
            known_agent_ids=known_agent_ids,
        )
    else:
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
    client = app.test_client()
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)
    return client


def test_dispatcher_routes_grant_to_handler_matching_request_type(tmp_path: Path) -> None:
    """Two handlers registered; only the one whose handles_request_type matches must be called."""
    other_agent_id = AgentId()
    permission_agent_id = AgentId()
    other_request = _make_other_request_event(agent_id=str(other_agent_id))
    permission_request = create_latchkey_predefined_permission_request_event(
        agent_id=str(permission_agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(other_request).add_request(permission_request)
    other_handler = _StubOtherHandler()
    permission_handler = _make_recording_handler(tmp_path)
    # The permission handler's grant POST resolves the agent_id to its
    # host_id before writing the grant; teach the static resolver about
    # both agents so the dispatcher reaches the handler instead of 503'ing.
    client = _build_authenticated_client_with_handlers(
        tmp_path,
        handlers=(other_handler, permission_handler),
        inbox=inbox,
        known_agent_ids=(other_agent_id, permission_agent_id),
    )

    # Granting an OTHER event must hit the other handler only.
    other_response = client.post(f"/requests/{other_request.event_id}/grant")
    assert other_response.status_code == 200
    assert other_handler.grant_event_ids == [str(other_request.event_id)]
    assert permission_handler.grant_calls == []

    # Granting a LATCHKEY_PERMISSION event must hit the permission handler only.
    perm_response = client.post(
        f"/requests/{permission_request.event_id}/grant",
        data={"permissions": ["slack-read-all"], "account": _TEST_ACCOUNT},
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


def test_notifications_snapshot_tracks_a_request_from_arrival_to_approval(tmp_path: Path) -> None:
    """The channel snapshot's notifications frame is derived end-to-end from the request inbox.

    Covers the real wiring behind ``derive_notifications``: the displayable
    pending set, the inbox-card display derivation (catalog title, workspace
    attribution, brand-mark service), and resolution via a recorded response.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="Needs to read the team channel.",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)
    state = get_state(client.application)
    publisher = state.ui_publisher
    assert publisher is not None

    snapshot = publisher.build_snapshot()

    assert snapshot.notifications.unresolved_count == 1
    (entry,) = snapshot.notifications.entries
    assert entry.id == str(request.event_id)
    assert entry.request_id == str(request.event_id)
    assert entry.is_resolved is False
    assert entry.outcome is None
    assert entry.title == "Slack"
    assert entry.body == "Needs to read the team channel."
    assert entry.workspace_name == f"ws-{agent_id}"
    assert entry.workspace_agent_id == str(agent_id)
    assert entry.service_name == "slack"

    response_event = create_request_response_event(
        request_event_id=str(request.event_id),
        status=RequestStatus.GRANTED,
        agent_id=str(agent_id),
        request_type=str(RequestType.LATCHKEY_PERMISSION),
        scope="slack-api",
    )
    state.request_inbox = inbox.add_response(response_event)

    resolved_snapshot = publisher.build_snapshot()

    assert resolved_snapshot.notifications.unresolved_count == 0
    (resolved_entry,) = resolved_snapshot.notifications.entries
    assert resolved_entry.is_resolved is True
    assert resolved_entry.outcome == "approved"
