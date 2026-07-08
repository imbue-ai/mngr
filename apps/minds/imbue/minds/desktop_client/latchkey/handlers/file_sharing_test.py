"""Unit tests for :class:`FileSharingGrantHandler`."""

import json
from collections.abc import Callable
from html.parser import HTMLParser
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
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.handlers.file_sharing import FileSharingGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_file_sharing_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId

_HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]


def _parse_input_attrs(html: str, element_id: str) -> dict[str, str]:
    """Return the attribute map of the ``<input>`` with the given id.

    Used to assert that user-controlled values are safely encoded into
    attributes (the HTML parser sees the real attribute boundaries, so a
    successful breakout would surface as extra attributes rather than a
    value substring).
    """
    found: dict[str, str] = {}

    class _Finder(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "input":
                return
            attr_map = {name: (value or "") for name, value in attrs}
            if attr_map.get("id") == element_id:
                found.update(attr_map)

    _Finder().feed(html)
    if not found:
        raise AssertionError(f"no <input> with id={element_id!r} found in HTML")
    return found


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


# Broad share roots so the existing tests' representative paths
# (``/home/...``, ``/Users/...``, ``/tmp/...``) all validate as in-root.
# Tests that exercise the out-of-root rejection inject a narrower set.
_DEFAULT_TEST_SHARE_ROOTS: Final = (Path("/home"), Path("/Users"), Path("/tmp"))


def _make_file_sharing_handler(
    tmp_path: Path,
    gateway_handler: _HttpxHandler,
    share_roots: tuple[Path, ...] = _DEFAULT_TEST_SHARE_ROOTS,
    home_dir: Path = Path("/home/example"),
) -> tuple[FileSharingGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    return (
        FileSharingGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
            share_roots=share_roots,
            home_dir=home_dir,
        ),
        sender,
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: FileSharingGrantHandler,
    inbox: RequestInbox,
) -> FlaskClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface = StaticBackendResolver(url_by_agent_and_service={})
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


# -- render_request_detail_fragment --


def test_render_request_detail_fragment_shows_path_and_rationale(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="http://localhost:8421",
    )
    assert "/home/user/important.txt" in body
    assert "summarize the doc" in body
    assert "Approve" in body and "Deny" in body
    # The fragment must show the human-readable access label so the user
    # knows what's being granted.
    assert "read-only" in body
    # The fragment is right-pane-only and has no chrome of its own; the
    # inbox shell owns the backdrop, close button, and submission JS.
    assert "<html" not in body
    assert "permissions-backdrop" not in body
    assert "<script>" not in body


def test_render_request_detail_fragment_has_editable_path_and_browse(tmp_path: Path) -> None:
    """The dialog renders the path as an editable input plus a Browse button."""
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    # The path is editable: an input named ``file_path`` pre-filled with
    # the requested path, plus separate file / folder pickers keyed for
    # the inbox shell.
    assert 'name="file_path"' in body
    assert 'id="file-sharing-path-input"' in body
    assert 'value="/home/user/important.txt"' in body
    assert 'id="file-sharing-browse-file-btn"' in body
    assert 'id="file-sharing-browse-folder-btn"' in body
    assert "browseForSharePath(&#39;file&#39;)" in body or "browseForSharePath('file')" in body
    assert "browseForSharePath(&#39;directory&#39;)" in body or "browseForSharePath('directory')" in body


def test_render_request_detail_fragment_marks_write_grants_distinctly(tmp_path: Path) -> None:
    """WRITE grants render the broader human-readable access label."""
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/edit.txt",
        access="WRITE",
        rationale="edit it",
    )
    body = handler.render_request_detail_fragment(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        mngr_forward_origin="",
    )
    assert "read &amp; write" in body or "read & write" in body
    # The read-only label must not appear when WRITE access is being
    # requested, otherwise the fragment would be misleading.
    assert "read-only" not in body


def test_render_request_detail_fragment_escapes_html_in_inputs(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    # A path crafted to break out of the value="" attribute and inject an
    # event handler if the renderer naively interpolated it.
    malicious_path = '/tmp/" onfocus="alert(1)'
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=malicious_path,
        access="READ",
        rationale="<img src=x onerror=alert(2)>",
    )
    body = str(
        handler.render_request_detail_fragment(
            req_event=event,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            mngr_forward_origin="",
        )
    )
    # The rationale is rendered as element text, so raw HTML must be
    # entity-escaped.
    assert "<img src=x" not in body

    # The path is rendered into the value="" attribute of the editable
    # input. Parse the input and verify the attribute carries the path
    # verbatim with no injected handler -- i.e. the renderer safely quoted
    # the value rather than letting the crafted ``"`` break out.
    attrs = _parse_input_attrs(body, element_id="file-sharing-path-input")
    assert attrs["value"] == malicious_path
    assert "onfocus" not in attrs


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
    body = response.get_json()
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

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(agent_id),
        path="/home/user/requested.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    edited = "/Users/glenn/Documents/Shared"
    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": edited})
    assert response.status_code == 200
    body = response.get_json()
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

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/home/user/data.txt"})
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"
    assert captured["content"] == b""


def test_grant_rejects_relative_edited_path(tmp_path: Path) -> None:
    """A relative edited path is rejected with a 400 before the gateway is called."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "relative/path"})
    assert response.status_code == 400
    assert "absolute" in response.get_json()["error"].lower()
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

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/home/user/../../etc/shadow"})
    assert response.status_code == 400
    assert ".." in response.get_json()["error"]
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
    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler, share_roots=(tmp_path,))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=in_root,
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "/etc/passwd"})
    assert response.status_code == 400
    error = response.get_json()["error"]
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

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler, share_roots=(tmp_path,))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=str(tmp_path / "orig.txt"),
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    edited = str(tmp_path / "nested" / "file.txt")
    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": edited})
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"


def test_grant_with_tilde_edited_path_expands_to_home(tmp_path: Path) -> None:
    """A ``~/...`` edited path expands to the home directory before reaching the gateway."""
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = _make_file_sharing_handler(
        tmp_path, _gateway_handler, share_roots=(tmp_path,), home_dir=tmp_path
    )
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path=str(tmp_path / "requested.txt"),
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "~/Documents/Shared"})
    assert response.status_code == 200, response.text
    body = response.get_json()
    assert body["outcome"] == "GRANTED"
    expanded = str(tmp_path / "Documents" / "Shared")
    # The gateway received the expanded absolute path, not the ``~`` form.
    sent_body = captured["content"]
    assert isinstance(sent_body, bytes)
    assert json.loads(sent_body) == {"path": expanded}
    # The granted message and persisted response event name the expanded path.
    assert expanded in body["message"]
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].scope == expanded


def test_grant_rejects_tilde_user_edited_path(tmp_path: Path) -> None:
    """``~user`` (another user's home) is rejected with a 400 before the gateway is called."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/example/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/grant", data={"file_path": "~otheruser/secret.txt"})
    assert response.status_code == 400
    assert "~user" in response.get_json()["error"]
    assert gateway_called is False


def test_render_request_detail_fragment_embeds_home_dir(tmp_path: Path) -> None:
    """The dialog embeds the home directory so the inbox shell can expand ``~`` client-side."""
    handler, _sender = _make_file_sharing_handler(
        tmp_path, lambda r: httpx.Response(200), home_dir=Path("/home/glenn")
    )
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/glenn/notes.txt",
        access="READ",
        rationale="r",
    )
    body = str(
        handler.render_request_detail_fragment(
            req_event=event,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            mngr_forward_origin="",
        )
    )
    attrs = _parse_input_attrs(body, element_id="file-sharing-path-input")
    assert attrs["data-home-dir"] == "/home/glenn"


def test_render_request_detail_fragment_embeds_allowed_roots(tmp_path: Path) -> None:
    """The dialog embeds the share roots so the inbox shell can validate client-side."""
    handler, _sender = _make_file_sharing_handler(
        tmp_path, lambda r: httpx.Response(200), share_roots=(Path("/home/glenn"), Path("/tmp"))
    )
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/glenn/notes.txt",
        access="READ",
        rationale="r",
    )
    body = str(
        handler.render_request_detail_fragment(
            req_event=event,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            mngr_forward_origin="",
        )
    )
    attrs = _parse_input_attrs(body, element_id="file-sharing-path-input")
    assert json.loads(attrs["data-allowed-roots"]) == ["/home/glenn", "/tmp"]


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
    assert "gateway" in response.get_json()["error"].lower()
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
    body = response.get_json()
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
    assert response.get_json()["outcome"] == "DENIED"
    assert len(load_response_events(tmp_path)) == 1
    assert len(sender.sent_messages) == 1


# -- Wiring through the Flask dispatcher --


def test_inbox_detail_route_dispatches_to_handler(tmp_path: Path) -> None:
    """GET /inbox/detail/<id> for a file-sharing event routes to FileSharingGrantHandler."""
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_latchkey_file_sharing_permission_request_event(
        agent_id=str(AgentId()),
        path="/home/user/x.txt",
        access="READ",
        rationale="r",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.get(f"/inbox/detail/{event.event_id}")
    assert response.status_code == 200
    assert "/home/user/x.txt" in response.text
