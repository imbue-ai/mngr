"""Tests for the /ui/api inbox routes (typed cards, per-kind details)."""

import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

from flask import Flask
from flask import Request
from flask import Response
from flask.testing import FlaskClient
from pydantic import Field

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import _derive_ui_notifications_message
from imbue.minds.desktop_client.app import _derive_ui_requests_message
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.latchkey.gateway_client import AccountsRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import PermissionEffect
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event
from imbue.minds.desktop_client.notification_feed import AgentMessageCard
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiAccountsPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.testing import ChatInWorkspaceResolver
from imbue.minds.desktop_client.testing import RecordingNotificationDispatcher
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import create_accounts_permission_request
from imbue.minds.desktop_client.ui_api_inbox import build_agent_message_card
from imbue.minds.desktop_client.ui_api_inbox import build_notification_card
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId

_STUB_REQUEST_TYPE = "UI_INBOX_STUB"


class _KnownAgentsResolver(StaticBackendResolver):
    """StaticBackendResolver that resolves display info only for the named agents."""

    known_agent_ids: tuple[AgentId, ...] = Field(default=(), description="Agents the resolver claims to know")

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.known_agent_ids:
            return None
        return AgentDisplayInfo(agent_name=f"ws-{str(agent_id)[:8]}", host_id="host-" + "0" * 32)


class _StubDetailHandler(RequestEventHandler):
    """Handler double: claims the stub request type, returns a typed accounts-style detail."""

    def handles_request_type(self) -> str:
        return _STUB_REQUEST_TYPE

    def kind_label(self) -> str:
        return "stub"

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        return "Stub Service"

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: object,
    ) -> RequestDetailPayload:
        return UiAccountsPermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name="stub-ws",
            rationale="because tests",
        )

    def apply_grant_request(self, request: Request, permission_request: StreamedPermissionRequest) -> Response:
        return make_response(content='{"outcome": "GRANTED"}', media_type="application/json")

    def apply_deny_request(self, request: Request, permission_request: StreamedPermissionRequest) -> Response:
        return make_response(content='{"outcome": "DENIED"}', media_type="application/json")


def _make_stub_request(agent_id: str, created_at: datetime | None = None) -> StreamedPermissionRequest:
    return StreamedPermissionRequest(
        request_id=f"req-{uuid.uuid4().hex}",
        agent_id=agent_id,
        rationale="because tests",
        request_type=_STUB_REQUEST_TYPE,
        payload=AccountsRequestPayload(),
        target="/tmp/permissions.json",
        effect=PermissionEffect(),
        created_at=created_at,
    )


def _build_client(
    tmp_path: Path,
    inbox: StaticPendingRequests | None,
    known_agent_ids: tuple[AgentId, ...],
    is_authenticated: bool = True,
) -> FlaskClient:
    resolver = _KnownAgentsResolver(url_by_agent_and_service={}, known_agent_ids=known_agent_ids)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=resolver,
        pending_requests=inbox,
        request_event_handlers=(_StubDetailHandler(),),
    )
    return client


def test_inbox_list_requires_authentication(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=StaticPendingRequests(), known_agent_ids=(), is_authenticated=False)

    response = client.get("/ui/api/inbox")

    assert response.status_code == 401


def test_inbox_list_returns_cards_only_for_resolvable_agents(tmp_path: Path) -> None:
    known_agent = AgentId()
    vanished_agent = AgentId()
    visible = _make_stub_request(str(known_agent))
    hidden = _make_stub_request(str(vanished_agent))
    inbox = StaticPendingRequests(
        pending=(
            visible,
            hidden,
        )
    )
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(known_agent,))

    response = client.get("/ui/api/inbox")

    assert response.status_code == 200
    body = response.get_json()
    assert [card["id"] for card in body["cards"]] == [visible.request_id]
    card = body["cards"][0]
    assert card["kind_label"] == "stub"
    assert card["display_name"] == "Stub Service"
    assert card["ws_name"] == f"ws-{str(known_agent)[:8]}"
    assert card["accent"] == DEFAULT_WORKSPACE_COLOR


def test_inbox_detail_returns_typed_payload_from_the_owning_handler(tmp_path: Path) -> None:
    agent = AgentId()
    req = _make_stub_request(str(agent))
    inbox = StaticPendingRequests(pending=(req,))
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get(f"/ui/api/inbox/{req.request_id}/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "accounts"
    assert detail["request_id"] == req.request_id
    assert detail["ws_name"] == "stub-ws"
    assert detail["rationale"] == "because tests"


def test_inbox_detail_reports_unknown_ids_as_unavailable(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=StaticPendingRequests(), known_agent_ids=())

    response = client.get("/ui/api/inbox/evt-doesnotexist/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "unavailable"
    assert "expired" in detail["message"]


def test_inbox_detail_reports_resolved_requests_as_unavailable(tmp_path: Path) -> None:
    agent = AgentId()
    req = _make_stub_request(str(agent))
    resolution = create_request_response_event(
        request_event_id=req.request_id,
        status=RequestStatus.DENIED,
        agent_id=str(agent),
    )
    inbox = StaticPendingRequests(pending=(req,), answered=(resolution,))
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get(f"/ui/api/inbox/{req.request_id}/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "unavailable"
    assert "already been processed" in detail["message"]


def test_real_accounts_request_flows_through_the_stubless_pipeline(tmp_path: Path) -> None:
    """A real accounts-permission event with no matching handler still lists as a generic card."""
    agent = AgentId()
    req = create_accounts_permission_request(agent_id=str(agent), rationale="list accounts")
    inbox = StaticPendingRequests(pending=(req,))
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get("/ui/api/inbox")

    assert response.status_code == 200
    cards = response.get_json()["cards"]
    assert [card["id"] for card in cards] == [req.request_id]
    # The stub handler does not claim ACCOUNTS_PERMISSION, so the card falls
    # back to the generic kind label rather than crashing.
    assert cards[0]["kind_label"] == "request"


def test_unsupported_detail_payload_shape_round_trips() -> None:
    payload = UiUnsupportedDetail(message="nope")
    assert payload.model_dump()["kind"] == "unsupported"


class _SiblingAgentResolver(StaticBackendResolver):
    """Resolver shaped like production: a workspace agent and the system-services
    sibling that actually files its latchkey requests, sharing a workspace name."""

    workspace_agent_id: AgentId = Field(description="The user-facing agent whose tile is on screen.")
    sibling_agent_id: AgentId = Field(description="The agent that files the workspace's requests.")
    workspace_name: str = Field(default="alpha", description="Name both agents report.")

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return (self.workspace_agent_id, self.sibling_agent_id)

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        # Only the primary agent has a workspace tile; this is what makes the
        # request's own agent id unusable as the workspace's identity.
        return (self.workspace_agent_id,)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.workspace_name if agent_id in self.list_known_agent_ids() else None

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.list_known_agent_ids():
            return None
        return AgentDisplayInfo(agent_name=self.workspace_name, host_id="host-" + "0" * 32)


def test_inbox_card_names_the_workspace_not_the_agent_that_filed_the_request(tmp_path: Path) -> None:
    """``workspace_agent_id`` is the tile's agent, resolved by name from the filer.

    Latchkey requests are filed by the workspace's system-services sibling, so a
    request's own ``agent_id`` never equals the id of the workspace on screen.
    The shell addresses its instant-resolution message by this field, so getting
    it wrong means either never flipping the card or posting one workspace's
    request id into another workspace's page.
    """
    workspace_agent_id, sibling_agent_id = AgentId(), AgentId()
    request = _make_stub_request(str(sibling_agent_id))
    resolver = _SiblingAgentResolver(
        url_by_agent_and_service={},
        workspace_agent_id=workspace_agent_id,
        sibling_agent_id=sibling_agent_id,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        pending_requests=StaticPendingRequests(pending=(request,)),
        request_event_handlers=(_StubDetailHandler(),),
    )

    response = client.get("/ui/api/inbox")

    assert response.status_code == 200
    card = response.get_json()["cards"][0]
    assert card["workspace_agent_id"] == str(workspace_agent_id)


def test_build_notification_card_mirrors_the_inbox_card_derivation() -> None:
    """The feed-input card carries the same display fields the inbox card renders."""
    agent_id = AgentId()
    resolver = _KnownAgentsResolver(url_by_agent_and_service={}, known_agent_ids=(agent_id,))
    filed_at = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    req = _make_stub_request(str(agent_id), created_at=filed_at)

    card = build_notification_card(req, (_StubDetailHandler(),), resolver, {})

    assert card.request_id == req.request_id
    assert card.requested_at == filed_at.isoformat()
    assert card.title == "Stub Service"
    # Every streamed request carries a rationale, stub kinds included.
    assert card.body == "because tests"
    assert card.workspace_name == f"ws-{str(agent_id)[:8]}"
    # No primary agent shares the workspace name, so agent id and accent degrade.
    assert card.workspace_agent_id == ""
    assert card.workspace_accent == DEFAULT_WORKSPACE_COLOR
    assert card.service_name == ""


def test_build_notification_card_falls_back_to_the_kind_label_for_unknown_kinds() -> None:
    """An unclaimed request kind still gets a non-empty headline (the generic kind label)."""
    agent_id = AgentId()
    resolver = _KnownAgentsResolver(url_by_agent_and_service={}, known_agent_ids=(agent_id,))
    req = _make_stub_request(str(agent_id))

    card = build_notification_card(req, (), resolver, {})

    assert card.title == "request"


def test_build_notification_card_uses_the_request_rationale_as_the_body() -> None:
    agent_id = AgentId()
    resolver = _KnownAgentsResolver(url_by_agent_and_service={}, known_agent_ids=(agent_id,))
    req = create_accounts_permission_request(
        agent_id=str(agent_id),
        rationale="Needs your device accounts to hand off work.",
    )

    card = build_notification_card(req, (), resolver, {})

    assert card.body == "Needs your device accounts to hand off work."


def test_build_agent_message_card_names_the_chat_and_resolves_its_workspace_by_name() -> None:
    """The message's row wears the workspace tile's identity and accent, headed by the chat's own name."""
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    resolver = ChatInWorkspaceResolver(
        url_by_agent_and_service={},
        workspace_agent_id=workspace_agent_id,
        chat_agent_id=chat_agent_id,
    )

    card = build_agent_message_card(chat_agent_id, "Done with the build.", resolver)

    assert card.chat_agent_id == str(chat_agent_id)
    assert card.chat_name == "Migration chat"
    assert card.body == "Done with the build."
    assert card.workspace_agent_id == str(workspace_agent_id)
    assert card.workspace_name == "alpha"
    assert card.workspace_accent == "#123456"


def test_build_agent_message_card_for_an_unknown_chat_still_records_the_message() -> None:
    chat_agent_id = AgentId()
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    card = build_agent_message_card(chat_agent_id, "hello", resolver)

    assert card.chat_name == str(chat_agent_id)
    assert card.workspace_agent_id == ""
    assert card.workspace_name == str(chat_agent_id)
    assert card.workspace_accent == DEFAULT_WORKSPACE_COLOR


def _resolutions_url(workspace: AgentId) -> str:
    return f"/ui/api/inbox/resolutions?workspace={workspace}"


def _sibling_response(request_event_id: str, status: RequestStatus, sibling_agent_id: AgentId):
    return create_request_response_event(
        request_event_id=request_event_id,
        status=status,
        agent_id=str(sibling_agent_id),
    )


def test_inbox_resolutions_requires_authentication(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=StaticPendingRequests(), known_agent_ids=(), is_authenticated=False)

    response = client.get(f"/ui/api/inbox/resolutions?workspace={AgentId()}")

    assert response.status_code == 401


def test_inbox_resolutions_snapshots_only_the_asking_workspaces_verdicts(tmp_path: Path) -> None:
    """The snapshot a (re)loaded workspace frame is hydrated from.

    Verdicts come keyed by request id for the asking workspace only -- they
    must not read across workspace boundaries -- and a malformed workspace id
    is rejected outright.
    """
    workspace_agent_id, sibling_agent_id = AgentId(), AgentId()
    granted, denied = (_make_stub_request(str(sibling_agent_id)) for _ in range(2))
    inbox = StaticPendingRequests(
        answered=(
            _sibling_response(granted.request_id, RequestStatus.GRANTED, sibling_agent_id),
            _sibling_response(denied.request_id, RequestStatus.DENIED, sibling_agent_id),
        ),
    )
    resolver = _SiblingAgentResolver(
        url_by_agent_and_service={},
        workspace_agent_id=workspace_agent_id,
        sibling_agent_id=sibling_agent_id,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        pending_requests=inbox,
        request_event_handlers=(_StubDetailHandler(),),
    )

    own = client.get(_resolutions_url(workspace_agent_id))
    foreign = client.get(_resolutions_url(AgentId()))
    malformed = client.get("/ui/api/inbox/resolutions?workspace=not-an-agent")

    assert own.status_code == 200
    assert own.get_json()["resolutions"] == [
        {"request_id": granted.request_id, "resolution": "granted"},
        {"request_id": denied.request_id, "resolution": "denied"},
    ]
    assert foreign.status_code == 200
    assert foreign.get_json()["resolutions"] == []
    assert malformed.status_code == 400


def test_inbox_resolutions_caps_the_snapshot_at_the_newest_verdicts(tmp_path: Path) -> None:
    """One contract message carries the snapshot, so it is bounded; newest win.

    Cards older than the cap fall back to the transcript's own resolution
    notices, which every resolved request eventually carries.
    """
    workspace_agent_id, sibling_agent_id = AgentId(), AgentId()
    inbox = StaticPendingRequests(
        answered=tuple(
            _sibling_response(f"evt-{index}", RequestStatus.GRANTED, sibling_agent_id) for index in range(70)
        ),
    )
    resolver = _SiblingAgentResolver(
        url_by_agent_and_service={},
        workspace_agent_id=workspace_agent_id,
        sibling_agent_id=sibling_agent_id,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        pending_requests=inbox,
        request_event_handlers=(_StubDetailHandler(),),
    )

    body = client.get(_resolutions_url(workspace_agent_id)).get_json()

    assert len(body["resolutions"]) == 64
    assert body["resolutions"][0]["request_id"] == "evt-6"
    assert body["resolutions"][-1]["request_id"] == "evt-69"


def test_pending_workspace_badge_survives_notification_dismissal_until_request_is_answered(tmp_path: Path) -> None:
    workspace_id, sibling_id = AgentId(), AgentId()
    request = _make_stub_request(str(sibling_id))
    pending = StaticPendingRequests(pending=(request,))
    resolver = _SiblingAgentResolver(
        url_by_agent_and_service={}, workspace_agent_id=workspace_id, sibling_agent_id=sibling_id
    )
    client, app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        pending_requests=pending,
        request_event_handlers=(_StubDetailHandler(),),
    )
    (entry,) = _derive_ui_notifications_message(app, resolver).entries
    assert _derive_ui_requests_message(app, resolver).workspace_agent_ids == (str(workspace_id),)

    assert client.post(f"/ui/api/notifications/{entry.id}/clear").status_code == 200

    assert _derive_ui_notifications_message(app, resolver).entries == ()
    after_dismissal = _derive_ui_requests_message(app, resolver)
    assert after_dismissal.request_ids == (request.request_id,)
    assert after_dismissal.workspace_agent_ids == (str(workspace_id),)

    pending.pending = ()
    assert _derive_ui_requests_message(app, resolver).workspace_agent_ids == ()


def test_a_relaunch_neither_rebanners_pending_requests_nor_brings_back_cleared_ones(tmp_path: Path) -> None:
    """The gateway re-delivers every pending request when the app launches; none of it is news."""
    workspace_id, sibling_id = AgentId(), AgentId()
    resolver = _SiblingAgentResolver(
        url_by_agent_and_service={}, workspace_agent_id=workspace_id, sibling_agent_id=sibling_id
    )
    inbox = StaticPendingRequests()
    paths = InstallationPaths(data_dir=tmp_path / "data")

    def launch() -> tuple[FlaskClient, Flask, RecordingNotificationDispatcher]:
        dispatcher = RecordingNotificationDispatcher(is_electron=True)
        client, app, _auth_store = build_desktop_client_for_test(
            tmp_path,
            is_authenticated=True,
            backend_resolver=resolver,
            pending_requests=inbox,
            request_event_handlers=(_StubDetailHandler(),),
            paths=paths,
            notification_dispatcher=dispatcher,
        )
        return client, app, dispatcher

    client, app, dispatcher = launch()
    cleared = _make_stub_request(str(sibling_id), created_at=datetime.now(timezone.utc))
    kept = _make_stub_request(str(sibling_id), created_at=datetime.now(timezone.utc))
    inbox.pending = (cleared, kept)
    _derive_ui_notifications_message(app, resolver)
    assert len(dispatcher.dispatched) == 2
    assert client.post(f"/ui/api/notifications/{cleared.request_id}/clear").status_code == 200

    _client, relaunched_app, relaunched_dispatcher = launch()
    message = _derive_ui_notifications_message(relaunched_app, resolver)

    assert [entry.id for entry in message.entries] == [kept.request_id]
    assert relaunched_dispatcher.dispatched == []


class _FlappingDiscoveryResolver(StaticBackendResolver):
    """Resolver mid-flap: the live listing has lost a workspace the last-good topology still knows."""

    restorable_workspace_ids: tuple[AgentId, ...] = Field(
        default=(), description="Workspaces known live or from the persisted topology"
    )

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return ()

    def list_restorable_workspace_ids(self) -> tuple[AgentId, ...]:
        return self.restorable_workspace_ids


def _agent_message_card(workspace_agent_id: AgentId) -> AgentMessageCard:
    return AgentMessageCard(
        chat_agent_id=str(AgentId()),
        chat_name="Build chat",
        body="The build is green.",
        workspace_agent_id=str(workspace_agent_id),
        workspace_name="alpha",
        workspace_accent=DEFAULT_WORKSPACE_COLOR,
    )


def test_a_discovery_flap_does_not_destroy_an_unread_agent_message(tmp_path: Path) -> None:
    """Dropping a message is unrecoverable, so only a workspace the resolver no longer knows at
    all takes its messages with it -- not one merely missing from a live listing."""
    flapping_workspace_id, gone_workspace_id = AgentId(), AgentId()
    resolver = _FlappingDiscoveryResolver(
        url_by_agent_and_service={}, restorable_workspace_ids=(flapping_workspace_id,)
    )
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )
    with app.app_context():
        feed = get_state().notification_feed
    assert feed is not None
    held = feed.append_agent_message(_agent_message_card(flapping_workspace_id))
    feed.append_agent_message(_agent_message_card(gone_workspace_id))

    message = _derive_ui_notifications_message(app, resolver)

    assert [entry.id for entry in message.entries] == [held.id]
