"""Integration tests for the permission routes wired into ``app.py``.

Drives the Flask app via the test client against a real catalog and a
fake ``LatchkeyPermissionGrantHandler`` so the routes are exercised
end-to-end without spawning any subprocesses.
"""

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
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

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
    grant_set_credentials_example: str | None = Field(default=None)
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
    ) -> GrantResult:
        self.grant_calls.append(
            {
                "request_event_id": request_event_id,
                "agent_id": str(agent_id),
                "host_id": str(host_id),
                "scope": service_info.scope,
                "granted_permissions": tuple(granted_permissions),
            }
        )
        # NEEDS_MANUAL_CREDENTIALS and FAILED keep the request pending and
        # write no response event; the other outcomes resolve it.
        if self.grant_outcome in (GrantOutcome.NEEDS_MANUAL_CREDENTIALS, GrantOutcome.FAILED):
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
            scope=service_info.scope,
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


def _make_recording_handler(
    tmp_path: Path,
    grant_outcome: GrantOutcome = GrantOutcome.GRANTED,
    grant_message: str = "granted",
    grant_set_credentials_example: str | None = None,
) -> _RecordingHandler:
    """Build a ``_RecordingHandler`` with stub probes that won't be exercised in routing tests."""
    gateway_client = build_fake_gateway_client()
    return _RecordingHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent"),
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
        grant_set_credentials_example=grant_set_credentials_example,
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


def test_get_permission_request_page_pre_checks_agent_requested_permissions(tmp_path: Path) -> None:
    """With no existing grants, the dialog pre-checks exactly what the agent asked for.

    The catch-all ``any`` schema is *not* pre-checked even though it is
    listed as an available option; the user must opt into it explicitly.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="I need to read the team channel to summarize today's discussion.",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    assert "Slack" in body
    assert "I need to read" in body
    # The agent-requested permission appears checked.
    read_idx = body.find('value="slack-read-all"')
    assert read_idx != -1
    tag_start = body.rfind("<input", 0, read_idx)
    tag_end = body.find(">", read_idx)
    assert "checked" in body[tag_start:tag_end]
    # ``any`` is offered as a checkbox but must not be pre-checked.
    any_idx = body.find('value="any"')
    assert any_idx != -1
    any_tag_start = body.rfind("<input", 0, any_idx)
    any_tag_end = body.find(">", any_idx)
    assert "checked" not in body[any_tag_start:any_tag_end]
    # Approve must be disabled in initial markup (JS enables it once the
    # user confirms / interacts with the form).
    assert 'id="permissions-approve-btn"' in body
    assert "disabled" in body


def test_get_permission_request_page_labels_wildcard_permission_as_all(tmp_path: Path) -> None:
    """The catch-all ``any`` permission is shown to users as ``all``.

    The underlying checkbox value stays ``any`` (Detent's wildcard that
    is actually stored / submitted), but the user-facing label reads
    ``all`` for clarity. The wildcard checkbox is also tagged with
    ``data-wildcard`` so the inbox shell can make it mutually exclusive
    with the specific permissions.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    # The checkbox keeps the wildcard value and is tagged so the shell's
    # exclusivity JS can find it.
    any_idx = body.find('value="any"')
    assert any_idx != -1
    any_tag_start = body.rfind("<input", 0, any_idx)
    any_tag_end = body.find(">", any_idx)
    assert 'data-wildcard="true"' in body[any_tag_start:any_tag_end]
    # The wildcard is labelled ``all`` (in a <code> element), and never
    # surfaced to the user as the raw ``any`` value.
    assert ">all</code>" in body
    assert ">any</code>" not in body


def test_inbox_page_renders_as_modal(tmp_path: Path) -> None:
    """The inbox page renders as a dismissable modal overlay.

    The desktop client hosts it in a transparent full-window overlay view
    stacked over the workspace, so the page provides a dim backdrop, a
    centered dialog card, and a close affordance. Dismissal (close button,
    backdrop click, Escape) prefers the Electron modal host
    (``window.minds.closeModal``) so the workspace view is left untouched,
    falling back to navigating home only when no modal host is present
    (page opened directly in a browser). The chrome lives on the inbox
    page, not on per-handler detail fragments.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get("/inbox")

    assert response.status_code == 200
    body = response.text
    # Modal scaffolding: a dim backdrop, a dialog card, and a close button.
    assert 'id="inbox-backdrop"' in body
    assert 'id="inbox-dialog"' in body
    assert 'id="inbox-close-btn"' in body
    # The transparent body lets the overlay reveal the workspace behind it.
    assert "bg-transparent" in body
    # Dismissal prefers the Electron modal host over a home navigation.
    assert "window.minds.closeModal" in body
    # Backdrop click and Escape are wired to the same dismissal helper.
    assert "onBackdropClick" in body
    assert 'e.key === "Escape"' in body


def test_inbox_page_hides_requests_whose_host_cannot_be_resolved(tmp_path: Path) -> None:
    """A pending request from an agent the resolver no longer knows is hidden.

    When a workspace is stopped, its agent drops out of discovery, so the
    backend resolver can no longer map the agent to a host/workspace. The
    inbox would otherwise fall back to rendering the raw agent id (a
    meaningless 16-char hex string). Such requests are filtered out of the
    inbox list -- only the request whose agent is still resolvable shows.
    """
    known_agent = AgentId()
    stopped_agent = AgentId()
    visible_request = create_latchkey_predefined_permission_request_event(
        agent_id=str(known_agent),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="visible",
    )
    hidden_request = create_latchkey_predefined_permission_request_event(
        agent_id=str(stopped_agent),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="hidden",
    )
    inbox = RequestInbox().add_request(visible_request).add_request(hidden_request)
    handler = _make_recording_handler(tmp_path)
    # The resolver knows only ``known_agent``; ``stopped_agent`` resolves to None.
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=known_agent)

    response = client.get("/inbox")

    assert response.status_code == 200
    body = response.text
    assert str(visible_request.event_id) in body
    assert str(hidden_request.event_id) not in body


def test_requests_payload_excludes_unresolvable_hosts(tmp_path: Path) -> None:
    """The SSE badge payload counts only requests whose host is resolvable.

    The badge count and the rendered cards are driven off the same filter,
    so a request from a since-stopped workspace neither inflates the badge
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
    assert payload == {"count": 1, "request_ids": [str(visible_request.event_id)]}


def test_get_permission_request_page_shows_descriptions_when_present(tmp_path: Path) -> None:
    """detent's per-permission descriptions are rendered next to each permission when present."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    # The requested permission's summary comes from the catalog fixture's
    # per-permission ``description`` field.
    assert "All read operations across the Slack API." in body
    # The scope-level description is intentionally not surfaced on the dialog.
    assert "Any interaction with the Slack API." not in body


def test_get_permission_request_page_renders_no_pre_checks_when_request_and_existing_are_empty(
    tmp_path: Path,
) -> None:
    """Empty agent request + no existing grants -> nothing pre-checked.

    The catch-all ``any`` is no longer treated as an implicit default,
    so the user must actively tick a permission before they can approve.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    # No input element should carry the ``checked`` attribute.
    for value_marker in ('value="any"', 'value="slack-read-all"', 'value="slack-write-all"'):
        idx = body.find(value_marker)
        assert idx != -1
        tag_start = body.rfind("<input", 0, idx)
        tag_end = body.find(">", idx)
        assert "checked" not in body[tag_start:tag_end], (
            f"unexpected pre-check on {value_marker}: {body[tag_start : tag_end + 1]}"
        )
    # Approve stays disabled in the initial markup -- the JS re-enables
    # it as soon as the user ticks any checkbox.
    assert 'id="permissions-approve-btn"' in body
    assert "disabled" in body


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
        data={"permissions": ["slack-read-all", "slack-write-all"]},
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
        data={"permissions": ["slack-read-all"]},
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
    """NEEDS_MANUAL_CREDENTIALS must echo the example command and not resolve the inbox."""
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
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
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id)

    response = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert payload["set_credentials_example"] == expected_example
    # The request must remain pending so the user can click Approve again
    # after running the suggested command.
    final_inbox = _get_app_request_inbox(client)
    assert final_inbox.get_pending_count() == 1


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


def test_get_permission_request_page_shows_unavailable_after_resolution(tmp_path: Path) -> None:
    """Re-opening a granted/denied request shows the "no longer available" page.

    The granted request lingers in the append-only log, so the page handler
    must detect the recorded response and render the friendly notice instead
    of the (re-submittable) grant/deny form.
    """
    agent_id = AgentId()
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    # Deny resolves the request without needing a discovered host.
    deny = client.post(f"/requests/{request.event_id}/deny")
    assert deny.status_code == 200

    page = client.get(f"/inbox/detail/{request.event_id}")
    assert page.status_code == 200
    body = page.text
    assert "no longer available" in body
    # The actionable form must be gone so it cannot be submitted again.
    assert 'id="permissions-approve-btn"' not in body
    assert 'action="/requests/' not in body


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
        data={"permissions": ["slack-read-all"]},
    )
    assert first.status_code == 200
    assert len(handler.grant_calls) == 1

    second = client.post(
        f"/requests/{request.event_id}/grant",
        data={"permissions": ["slack-read-all"]},
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
        data={"permissions": ["some-perm"]},
    )

    assert response.status_code == 400
    assert handler.grant_calls == []


def test_get_permission_request_page_pre_checks_existing_grants(tmp_path: Path) -> None:
    agent_id = AgentId()
    host_id = HostId()
    # Latchkey permissions are stored per-host now: pre-populate the
    # host-keyed file so the dialog should pre-check the matching
    # permissions for this host (every agent on the host shares this
    # config). The backend resolver is configured to map ``agent_id`` to
    # ``host_id`` so ``_resolve_host_id`` returns the same host the file
    # is keyed by.
    save_permissions(
        permissions_path_for_host(tmp_path / "mngr_latchkey", host_id),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-chat-read"]},)),
    )
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id, host_id=host_id)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    # The previously-granted permission appears checked.
    chat_read_idx = body.find('value="slack-chat-read"')
    assert chat_read_idx != -1
    # Find the surrounding <input ...> tag and assert it has 'checked'.
    tag_start = body.rfind("<input", 0, chat_read_idx)
    tag_end = body.find(">", chat_read_idx)
    assert "checked" in body[tag_start:tag_end]


def test_get_permission_request_page_pre_checks_union_of_existing_and_requested(tmp_path: Path) -> None:
    """When the agent asks for a permission that isn't yet granted, the dialog pre-checks the union.

    Approving without modification grants the union, which is the
    intuitive behavior for "give the agent what it's asking for, on top
    of what it already has". A previous design pre-checked only the
    existing grants, which silently turned Approve into a no-op against
    the agent's new request.
    """
    agent_id = AgentId()
    host_id = HostId()
    save_permissions(
        permissions_path_for_host(tmp_path / "mngr_latchkey", host_id),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-chat-read"]},)),
    )
    request = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="slack-api",
        permissions=("slack-write-all",),
        rationale="reason",
    )
    inbox = RequestInbox().add_request(request)
    handler = _make_recording_handler(tmp_path)
    client = _build_authenticated_client(tmp_path, handler, inbox, agent_id=agent_id, host_id=host_id)

    response = client.get(f"/inbox/detail/{request.event_id}")

    assert response.status_code == 200
    body = response.text
    for expected_checked in ("slack-chat-read", "slack-write-all"):
        idx = body.find(f'value="{expected_checked}"')
        assert idx != -1, f"checkbox for {expected_checked} missing from dialog"
        tag_start = body.rfind("<input", 0, idx)
        tag_end = body.find(">", idx)
        assert "checked" in body[tag_start:tag_end], (
            f"expected {expected_checked} to be pre-checked: {body[tag_start : tag_end + 1]}"
        )
    # ``slack-read-all`` is in the catalog but neither requested nor
    # previously granted, so it must not be pre-checked.
    read_all_idx = body.find('value="slack-read-all"')
    assert read_all_idx != -1
    read_all_tag_start = body.rfind("<input", 0, read_all_idx)
    read_all_tag_end = body.find(">", read_all_idx)
    assert "checked" not in body[read_all_tag_start:read_all_tag_end]


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
        data={"permissions": ["slack-read-all"]},
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

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        return "ok"

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
