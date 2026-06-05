"""End-to-end dispatcher tests for the latchkey request-event handlers.

Unlike the handler unit tests (``file_sharing_test.py``,
``predefined_test.py``), which call handler methods and render functions
directly, these tests stand up the whole desktop-client FastAPI app via
:func:`create_desktop_client` and drive real HTTP routing through a
``TestClient``. They exercise the dispatcher end to end -- session
authentication, route matching, request-event lookup, and handler
dispatch -- so they are integration tests rather than units.
"""

import json
from pathlib import Path

import httpx

from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.testing import build_authenticated_client
from imbue.minds.desktop_client.latchkey.handlers.testing import build_handler
from imbue.minds.desktop_client.latchkey.handlers.testing import build_slack_services_catalog
from imbue.minds.desktop_client.latchkey.handlers.testing import make_file_sharing_handler
from imbue.minds.desktop_client.latchkey.handlers.testing import read_recording
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_file_sharing_permission_request_event
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId

# -- File-sharing apply_grant_request via the dispatcher --


def test_grant_calls_gateway_approve_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(agent_id),
        path="/home/user/data.txt",
        access="WRITE",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

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


def test_grant_with_edited_path_sends_override_and_uses_it(tmp_path: Path) -> None:
    """Editing the path in the dialog sends an override body and reflects the new path everywhere."""
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(agent_id),
        path="/home/user/requested.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    edited = "/Users/glenn/Documents/Shared"
    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": edited})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "GRANTED"
    # The granted message names the edited path, not the requested one.
    assert edited in body["message"]
    assert "/home/user/requested.txt" not in body["message"]

    # The gateway received the override path as a JSON body.
    sent_body = captured["content"]
    assert isinstance(sent_body, bytes)
    assert json.loads(sent_body) == {"path": edited}

    # The persisted response event records the edited path as its scope.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].scope == edited
    # The agent notification names the edited path.
    assert sender.sent_messages == [(str(agent_id), body["message"])]


def test_grant_with_unchanged_path_sends_no_override_body(tmp_path: Path) -> None:
    """Submitting the original path verbatim must not send an override (gateway uses the precomputed effect)."""
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/home/user/data.txt"})
    assert response.status_code == 200
    assert response.json()["outcome"] == "GRANTED"
    assert captured["content"] == b""


def test_grant_rejects_relative_edited_path(tmp_path: Path) -> None:
    """A relative edited path is rejected with a 400 before the gateway is called."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "relative/path"})
    assert response.status_code == 400
    assert "absolute" in response.json()["error"].lower()
    assert gateway_called is False
    # The request stays pending: no response event, no agent notification.
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


def test_grant_rejects_traversal_in_edited_path(tmp_path: Path) -> None:
    """A ``..`` segment in the edited path is rejected with a 400 before the gateway is called."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/home/user/../../etc/shadow"})
    assert response.status_code == 400
    assert ".." in response.json()["error"]
    assert gateway_called is False


def test_grant_rejects_edited_path_outside_share_roots(tmp_path: Path) -> None:
    """A path outside the WebDAV mount roots is rejected with a clean 400, not forwarded to the gateway."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    in_root = str(tmp_path / "ok.txt")
    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler, share_roots=(tmp_path,))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=in_root,
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/etc/passwd"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert "shared folder" in error
    # The error names the allowed root(s) so the user can self-correct.
    assert str(tmp_path) in error
    # We did not fall back to the gateway, and the request stays pending.
    assert gateway_called is False
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


def test_grant_accepts_edited_path_within_share_roots(tmp_path: Path) -> None:
    """A path nested under an allowed root is accepted."""

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = make_file_sharing_handler(tmp_path, _gateway_handler, share_roots=(tmp_path,))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=str(tmp_path / "orig.txt"),
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    edited = str(tmp_path / "nested" / "file.txt")
    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": edited})
    assert response.status_code == 200
    assert response.json()["outcome"] == "GRANTED"


def test_grant_returns_502_when_gateway_rejects(tmp_path: Path) -> None:
    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "boom"})

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant")
    assert response.status_code == 502
    assert "gateway" in response.json()["error"].lower()
    # No response event written; the request stays pending.
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


# -- File-sharing apply_deny_request via the dispatcher --


def test_deny_calls_gateway_delete_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="READ",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

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

    handler, sender = make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="WRITE",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")
    assert response.status_code == 200
    assert response.json()["outcome"] == "DENIED"
    assert len(load_response_events(tmp_path)) == 1
    assert len(sender.sent_messages) == 1


# -- Wiring through the FastAPI dispatcher --


def test_inbox_detail_route_dispatches_to_handler(tmp_path: Path) -> None:
    """GET /inbox/detail/<id> for a file-sharing event routes to FileSharingGrantHandler."""
    handler, _sender = make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/x.txt",
        access="READ",
        rationale="r",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{event.event_id}")
    assert response.status_code == 200
    assert "/home/user/x.txt" in response.text


# -- Predefined-permission apply_deny_request via the dispatcher --


def test_apply_deny_request_succeeds_for_unknown_scope(tmp_path: Path) -> None:
    """Deny must work even when the request's scope is not in the gateway catalog.

    An agent can file a permission request under an unknown scope
    (typo, stale catalog, etc.); the rendered detail fragment
    (:func:`_render_unknown_scope_fragment`) offers Deny as the only
    action. The deny HTTP path must therefore still tear down the
    pending request, append a DENIED response event, and notify the
    agent -- using the raw scope string in place of a catalog
    display name.
    """
    fake_client = FakeLatchkeyGatewayClient()
    handler = build_handler(tmp_path, credential_status="valid")
    # Swap in a gateway client that records delete calls so we can
    # assert the pending request was torn down.
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=handler.latchkey,
        services_catalog=build_slack_services_catalog(fake_client),
        mngr_message_sender=handler.mngr_message_sender,
        gateway_client=fake_client,
    )
    agent_id = AgentId()
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="not-in-catalog-scope",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")

    assert response.status_code == 200
    assert response.json() == {"outcome": "DENIED"}
    # Gateway DELETE for the pending request must have been issued.
    assert fake_client.deleted_request_ids == (str(event.event_id),)
    # Response event was appended on disk, carrying the raw scope.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == str(RequestStatus.DENIED)
    assert response_events[0].scope == "not-in-catalog-scope"
    # Agent was notified; the message falls back to the raw scope as
    # the display name since no catalog entry exists.
    mngr_recording = read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert "denied" in argv[2].lower()
    assert "not-in-catalog-scope" in argv[2]
