"""Tests for the /ui/api inbox routes (typed cards, per-kind details, auto-open)."""

import uuid
from pathlib import Path

from flask import Request
from flask import Response
from flask.testing import FlaskClient
from pydantic import Field

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.request_events import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_accounts_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiAccountsPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_response
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

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        return "Stub Service"

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: object,
        mngr_forward_origin: str,
    ) -> str:
        return "<p>legacy fragment</p>"

    def build_request_detail_payload(
        self,
        req_event: RequestEvent,
        backend_resolver: object,
    ) -> RequestDetailPayload:
        return UiAccountsPermissionDetail(
            request_id=str(req_event.event_id),
            agent_id=req_event.agent_id,
            ws_name="stub-ws",
            rationale="because tests",
        )

    def apply_grant_request(self, request: Request, req_event: RequestEvent) -> Response:
        return make_response(content='{"outcome": "GRANTED"}', media_type="application/json")

    def apply_deny_request(self, request: Request, req_event: RequestEvent) -> Response:
        return make_response(content='{"outcome": "DENIED"}', media_type="application/json")


def _make_stub_request(agent_id: str) -> RequestEvent:
    return RequestEvent(
        timestamp=IsoTimestamp("2026-01-01T00:00:00.000000Z"),
        type=EventType("stub_request"),
        event_id=EventId(f"evt-{uuid.uuid4().hex}"),
        source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
        agent_id=agent_id,
        request_type=_STUB_REQUEST_TYPE,
    )


def _build_client(
    tmp_path: Path,
    inbox: RequestInbox | None,
    known_agent_ids: tuple[AgentId, ...],
    is_authenticated: bool = True,
    minds_config: MindsConfig | None = None,
) -> FlaskClient:
    resolver = _KnownAgentsResolver(url_by_agent_and_service={}, known_agent_ids=known_agent_ids)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=resolver,
        request_inbox=inbox,
        request_event_handlers=(_StubDetailHandler(),),
        minds_config=minds_config,
    )
    return client


def test_inbox_list_requires_authentication(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=RequestInbox(), known_agent_ids=(), is_authenticated=False)

    response = client.get("/ui/api/inbox")

    assert response.status_code == 401


def test_inbox_list_returns_cards_only_for_resolvable_agents(tmp_path: Path) -> None:
    known_agent = AgentId()
    vanished_agent = AgentId()
    visible = _make_stub_request(str(known_agent))
    hidden = _make_stub_request(str(vanished_agent))
    inbox = RequestInbox().add_request(visible).add_request(hidden)
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(known_agent,))

    response = client.get("/ui/api/inbox")

    assert response.status_code == 200
    body = response.get_json()
    assert [card["id"] for card in body["cards"]] == [str(visible.event_id)]
    card = body["cards"][0]
    assert card["kind_label"] == "stub"
    assert card["display_name"] == "Stub Service"
    assert card["ws_name"] == f"ws-{str(known_agent)[:8]}"
    assert card["accent"] == DEFAULT_WORKSPACE_COLOR
    assert body["auto_open"] is True


def test_inbox_detail_returns_typed_payload_from_the_owning_handler(tmp_path: Path) -> None:
    agent = AgentId()
    req = _make_stub_request(str(agent))
    inbox = RequestInbox().add_request(req)
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get(f"/ui/api/inbox/{req.event_id}/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "accounts"
    assert detail["request_id"] == str(req.event_id)
    assert detail["ws_name"] == "stub-ws"
    assert detail["rationale"] == "because tests"


def test_inbox_detail_reports_unknown_ids_as_unavailable(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=RequestInbox(), known_agent_ids=())

    response = client.get("/ui/api/inbox/evt-doesnotexist/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "unavailable"
    assert "expired" in detail["message"]


def test_inbox_detail_reports_resolved_requests_as_unavailable(tmp_path: Path) -> None:
    agent = AgentId()
    req = _make_stub_request(str(agent))
    resolution = create_request_response_event(
        request_event_id=str(req.event_id),
        status=RequestStatus.DENIED,
        agent_id=str(agent),
        request_type=_STUB_REQUEST_TYPE,
    )
    inbox = RequestInbox().add_request(req).add_response(resolution)
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get(f"/ui/api/inbox/{req.event_id}/detail")

    assert response.status_code == 200
    detail = response.get_json()["detail"]
    assert detail["kind"] == "unavailable"
    assert "already been processed" in detail["message"]


def test_inbox_auto_open_toggle_persists_to_minds_config(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client = _build_client(tmp_path, inbox=RequestInbox(), known_agent_ids=(), minds_config=minds_config)

    response = client.post("/ui/api/inbox/auto-open", json={"enabled": False})

    assert response.status_code == 200
    assert minds_config.get_auto_open_requests_panel() is False

    response = client.post("/ui/api/inbox/auto-open", json={"enabled": True})
    assert response.status_code == 200
    assert minds_config.get_auto_open_requests_panel() is True


def test_inbox_auto_open_rejects_bodies_without_enabled(tmp_path: Path) -> None:
    client = _build_client(tmp_path, inbox=RequestInbox(), known_agent_ids=())

    response = client.post("/ui/api/inbox/auto-open", json={"wrong": 1})

    assert response.status_code == 400


def test_real_accounts_request_flows_through_the_stubless_pipeline(tmp_path: Path) -> None:
    """A real accounts-permission event with no matching handler still lists as a generic card."""
    agent = AgentId()
    req = create_latchkey_accounts_permission_request_event(agent_id=str(agent), rationale="list accounts")
    inbox = RequestInbox().add_request(req)
    client = _build_client(tmp_path, inbox=inbox, known_agent_ids=(agent,))

    response = client.get("/ui/api/inbox")

    assert response.status_code == 200
    cards = response.get_json()["cards"]
    assert [card["id"] for card in cards] == [str(req.event_id)]
    # The stub handler does not claim ACCOUNTS_PERMISSION, so the card falls
    # back to the generic kind label rather than crashing.
    assert cards[0]["kind_label"] == "request"


def test_unsupported_detail_payload_shape_round_trips() -> None:
    payload = UiUnsupportedDetail(message="nope")
    assert payload.model_dump()["kind"] == "unsupported"
