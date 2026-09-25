"""Unit tests for :class:`FileSharingGrantHandler`."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingSyncRequest
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_FILE_SHARING
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.file_sharing import FileSharingGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import load_response_events
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.minds.desktop_client.latchkey.testing import leave_permissions_on_this_computer
from imbue.minds.desktop_client.request_handler import UiFileSharingPermissionDetail
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import create_file_sharing_permission_request
from imbue.minds.desktop_client.testing import write_fake_mngr_pair_script
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.testing import make_full_fake_latchkey

_HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    # This double overrides ``send`` entirely and never dispatches to a
    # concurrency group, so relax the base class's required field.
    concurrency_group: ConcurrencyGroup | None = None
    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, chat_id: AgentId, text: str, exec_agent_address: str) -> None:
        self.sent_messages.append((str(chat_id), text))


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
    push_permissions_to_machine: Callable[[str], None] = leave_permissions_on_this_computer,
) -> tuple[FileSharingGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(sent_messages=[])
    return (
        FileSharingGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            latchkey=make_full_fake_latchkey(tmp_path),
            mngr_message_sender=sender,
            push_permissions_to_machine=push_permissions_to_machine,
            share_roots=share_roots,
            home_dir=home_dir,
        ),
        sender,
    )


class _NamedWorkspaceResolver(StaticBackendResolver):
    """Static resolver that reports every configured agent as a named machine."""

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return f"ws-{agent_id}"


def _build_authenticated_client(
    tmp_path: Path,
    handler: FileSharingGrantHandler,
    inbox: StaticPendingRequests,
    known_agent: AgentId | None = None,
    backend_resolver: BackendResolverInterface | None = None,
    folder_sync_manager: FolderSyncManager | None = None,
) -> FlaskClient:
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    if backend_resolver is None:
        backend_resolver = (
            _NamedWorkspaceResolver(url_by_agent_and_service={str(known_agent): {}})
            if known_agent is not None
            else StaticBackendResolver(url_by_agent_and_service={})
        )
    paths = InstallationPaths(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        pending_requests=inbox,
        request_event_handlers=(handler,),
        folder_sync_manager=folder_sync_manager,
    )
    client = app.test_client()
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)
    return client


_DEVICE_ID: Final[str] = "host-0f0e0d0c0b0a09080706050403020100"
_START_TIMEOUT_SECONDS: Final[float] = 30.0


class _ChatWorkspaceResolver(StaticBackendResolver):
    """A workspace whose primary agent and chat agent share one name and host.

    The shape a real request arrives in: filed by the chat, while everything
    the desktop keeps per workspace is keyed by the primary agent.
    """

    primary_agent_id: AgentId = Field(description="The user-facing agent the Permissions tab is keyed by.")
    chat_agent_id: AgentId = Field(description="The chat agent that files the request.")

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return (self.primary_agent_id, self.chat_agent_id)

    def list_known_workspace_ids(self) -> tuple[AgentId, ...]:
        return (self.primary_agent_id,)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.list_known_agent_ids():
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return "alpha" if agent_id in self.list_known_agent_ids() else None


def _folder_sync_manager(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    backend_resolver: BackendResolverInterface | None = None,
) -> FolderSyncManager:
    """A manager whose ``mngr`` is the test stand-in, rooted at ``tmp_path``."""
    return FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
        backend_resolver=(
            _NamedWorkspaceResolver(url_by_agent_and_service={str(agent_id): {}})
            if backend_resolver is None
            else backend_resolver
        ),
        store=FolderSyncStore(records_dir=tmp_path / "folder_syncs"),
    )


def _syncable_setup(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    gateway_handler: _HttpxHandler,
    path: Path,
    access: str = "WRITE",
    sync: FileSharingSyncRequest | None = None,
) -> tuple[FlaskClient, FolderSyncManager, StreamedPermissionRequest, _RecordingMessageSender]:
    """A client whose build can sync, with one file-sharing request pending for ``path``."""
    agent_id = AgentId()
    handler, sender = _make_file_sharing_handler(tmp_path, gateway_handler, share_roots=(tmp_path,), home_dir=tmp_path)
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, agent_id)
    event = create_file_sharing_permission_request(
        agent_id=str(agent_id), path=str(path), access=access, rationale="keep it close", sync=sync
    )
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        known_agent=agent_id,
        folder_sync_manager=manager,
    )
    return client, manager, event, sender


# handler.handles_request_type


def test_handler_claims_file_sharing_request_type(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    assert handler.handles_request_type() == REQUEST_TYPE_FILE_SHARING
    assert handler.kind_label() == "file sharing"


def test_display_name_returns_path(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    assert handler.display_name_for_event(event) == "/home/user/data.txt"


# apply_grant_request


def test_grant_calls_gateway_approve_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_file_sharing_permission_request(
        agent_id=str(agent_id),
        path="/home/user/data.txt",
        access="WRITE",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant")
    assert response.status_code == 200
    body = response.get_json()
    assert body["outcome"] == "GRANTED"
    assert "/home/user/data.txt" in body["message"]
    # Granted-message text reflects the access mode the agent asked for
    # so the agent's response handler can see what it ended up with.
    assert "read & write" in body["message"]

    # Gateway received the approve request.
    assert captured["method"] == "POST"
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.request_id}")

    # Response event was appended on disk.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == "GRANTED"
    assert response_events[0].request_event_id == event.request_id

    # Agent was notified, with the request id appended so the chat harness can
    # correlate this notice to the right card regardless of arrival order.
    assert sender.sent_messages == [
        (str(agent_id), format_resolution_notice(body["message"], event.request_id, RequestStatus.GRANTED))
    ]


def test_grant_with_edited_path_sends_override_and_uses_it(tmp_path: Path) -> None:
    """Editing the path in the dialog sends an override body and reflects the new path everywhere."""
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_file_sharing_permission_request(
        agent_id=str(agent_id),
        path="/home/user/requested.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    edited = "/Users/glenn/Documents/Shared"
    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": edited})
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
    # The agent notification names the edited path.
    assert sender.sent_messages == [
        (str(agent_id), format_resolution_notice(body["message"], event.request_id, RequestStatus.GRANTED))
    ]


def test_grant_with_unchanged_path_sends_no_override_body(tmp_path: Path) -> None:
    """Submitting the original path verbatim must not send an override (gateway uses the precomputed effect)."""
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "/home/user/data.txt"})
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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "relative/path"})
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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "/home/user/../../etc/shadow"})
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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path=in_root,
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "/etc/passwd"})
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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path=str(tmp_path / "orig.txt"),
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    edited = str(tmp_path / "nested" / "file.txt")
    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": edited})
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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path=str(tmp_path / "requested.txt"),
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "~/Documents/Shared"})
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


def test_grant_rejects_tilde_user_edited_path(tmp_path: Path) -> None:
    """``~user`` (another user's home) is rejected with a 400 before the gateway is called."""
    gateway_called = False

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        del request
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    handler, _sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/example/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": "~otheruser/secret.txt"})
    assert response.status_code == 400
    assert "~user" in response.get_json()["error"]
    assert gateway_called is False


def test_grant_returns_502_when_gateway_rejects(tmp_path: Path) -> None:
    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "boom"})

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/data.txt",
        access="READ",
        rationale="need data",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/grant")
    assert response.status_code == 502
    assert "gateway" in response.get_json()["error"].lower()
    # No response event written; the request stays pending.
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []


# apply_deny_request


def test_deny_calls_gateway_delete_writes_response_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(204)

    handler, sender = _make_file_sharing_handler(tmp_path, _gateway_handler)
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="READ",
        rationale="please",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/deny")
    assert response.status_code == 200
    body = response.get_json()
    assert body["outcome"] == "DENIED"

    # Gateway received DELETE (not POST).
    assert captured["method"] == "DELETE"
    assert str(captured["path"]).endswith(f"/permission-requests/{event.request_id}")

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
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/secret.txt",
        access="WRITE",
        rationale="please",
    )
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.request_id}/deny")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    assert len(load_response_events(tmp_path)) == 1
    assert len(sender.sent_messages) == 1


# Wiring through the Flask dispatcher


def test_build_request_detail_payload_matches_the_fragment_inputs(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/important.txt",
        access="READ",
        rationale="summarize the doc",
    )

    payload = handler.build_request_detail_payload(
        permission_request=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiFileSharingPermissionDetail):
        pytest.fail(f"expected a file_sharing detail payload, got {payload!r}")
    assert payload.request_id == event.request_id
    assert payload.file_path == "/home/user/important.txt"
    assert payload.access == "READ"
    assert payload.access_human_label == "read-only"
    assert payload.rationale == "summarize the doc"
    assert str(handler.home_dir) == payload.home_dir
    assert payload.allowed_roots == tuple(str(root) for root in handler.share_roots)


def test_build_request_detail_payload_labels_write_access_distinctly(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()),
        path="/home/user/notes",
        access="WRITE",
        rationale="edit the notes",
    )

    payload = handler.build_request_detail_payload(
        permission_request=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiFileSharingPermissionDetail):
        pytest.fail(f"expected a file_sharing detail payload, got {payload!r}")
    assert payload.access == "WRITE"
    assert payload.access_human_label == "read & write"


def test_grant_hands_the_spliced_policy_to_the_workspaces_own_machine(tmp_path: Path) -> None:
    """The gateway splices the grant into this computer's copy; the machine enforces its own."""
    carried: list[str] = []
    handler, _sender = _make_file_sharing_handler(
        tmp_path,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        push_permissions_to_machine=carried.append,
    )
    agent_id = AgentId()
    host_id = HostId()
    event = create_file_sharing_permission_request(
        agent_id=str(agent_id),
        path="/home/user/data.txt",
        access="WRITE",
        rationale="need data",
    )
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host_id, known_agent_ids=(agent_id,)
        ),
    )

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 200, response.text
    assert carried == [str(agent_id)]


# The sync the dialog can ask for alongside the grant


def test_grant_with_sync_starts_one_and_tells_the_agent_where_the_copy_lands(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    client, manager, event, sender = _syncable_setup(
        tmp_path,
        root_concurrency_group,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        folder,
        sync=FileSharingSyncRequest(),
    )

    response = client.post(
        f"/requests/{event.request_id}/grant",
        data={"file_path": str(folder), "sync": "true", "sync_conflict": "WORKSPACE"},
    )

    assert response.status_code == 200, response.text
    body = response.get_json()
    assert body["outcome"] == "GRANTED"
    # The agent is told where its copy lives, since that is the path it will read.
    assert f"~/synced_folders/{_DEVICE_ID}{folder}" in body["message"]
    assert sender.sent_messages == [
        (event.agent_id, format_resolution_notice(body["message"], event.request_id, RequestStatus.GRANTED))
    ]
    status = manager.wait_until_started(event.agent_id, str(folder), _START_TIMEOUT_SECONDS)
    assert status is not None
    assert status.spec.conflict == FolderSyncConflict.WORKSPACE
    assert manager.desired_activity_for(event.agent_id, str(folder)) == FolderSyncActivity.ACTIVE
    manager.stop_all()


def test_grant_refuses_a_sync_it_cannot_start_before_granting_anything(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A file cannot be synced; the request stays pending so the user can untick the option."""
    gateway_called = False

    def _gateway_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal gateway_called
        gateway_called = True
        return httpx.Response(200, json={"request_id": "evt-abc", "applied": {}})

    shared_file = tmp_path / "notes.txt"
    shared_file.write_text("hello")
    client, manager, event, sender = _syncable_setup(
        tmp_path, root_concurrency_group, _gateway_handler, shared_file, access="READ", sync=FileSharingSyncRequest()
    )

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": str(shared_file), "sync": "true"})

    assert response.status_code == 400, response.text
    assert "Only folders can be synced" in response.get_json()["error"]
    assert gateway_called is False
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []
    assert manager.desired_activity_for(event.agent_id, str(shared_file)) is None


def test_grant_tells_the_agent_when_the_sync_it_asked_for_was_declined(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    client, manager, event, _sender = _syncable_setup(
        tmp_path,
        root_concurrency_group,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        folder,
        sync=FileSharingSyncRequest(),
    )

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": str(folder), "sync": "false"})

    assert response.status_code == 200, response.text
    body = response.get_json()
    assert body["outcome"] == "GRANTED"
    assert "was not enabled" in body["message"]
    assert manager.desired_activity_for(event.agent_id, str(folder)) is None


def test_grant_says_nothing_about_syncing_when_nobody_asked(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    client, _manager, event, _sender = _syncable_setup(
        tmp_path,
        root_concurrency_group,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        folder,
    )

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": str(folder), "sync": "false"})

    assert response.status_code == 200, response.text
    assert (
        response.get_json()["message"]
        == f"Your read & write file-sharing permission request for '{folder}' was granted."
    )


def test_grant_rejects_a_sync_choice_it_cannot_read(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    client, _manager, event, _sender = _syncable_setup(
        tmp_path,
        root_concurrency_group,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        folder,
    )

    response = client.post(
        f"/requests/{event.request_id}/grant",
        data={"file_path": str(folder), "sync": "true", "sync_conflict": "BOTH"},
    )

    assert response.status_code == 400, response.text
    assert load_response_events(tmp_path) == []


def test_grant_refuses_a_sync_when_this_build_cannot_run_one(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(
        tmp_path,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        share_roots=(tmp_path,),
        home_dir=tmp_path,
    )
    folder = tmp_path / "project"
    folder.mkdir()
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()), path=str(folder), access="READ", rationale="keep it close"
    )
    client = _build_authenticated_client(tmp_path, handler, StaticPendingRequests(pending=(event,)))

    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": str(folder), "sync": "true"})

    assert response.status_code == 400, response.text
    assert "unavailable in this build" in response.get_json()["error"]


def test_detail_carries_the_sync_ask_and_whether_the_path_can_be_synced(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    client, _manager, event, _sender = _syncable_setup(
        tmp_path,
        root_concurrency_group,
        lambda _req: httpx.Response(200),
        folder,
        sync=FileSharingSyncRequest(conflict=FolderSyncConflict.THIS_COMPUTER),
    )

    detail = client.get(f"/ui/api/inbox/{event.request_id}/detail").get_json()["detail"]

    assert detail["kind"] == "file_sharing"
    assert detail["is_sync_supported"] is True
    assert detail["is_sync_requested"] is True
    assert detail["sync_conflict"] == "THIS_COMPUTER"
    assert detail["sync_unavailable_reason"] == ""


def test_detail_says_why_a_requested_file_cannot_be_synced(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    shared_file = tmp_path / "notes.txt"
    shared_file.write_text("hello")
    client, _manager, event, _sender = _syncable_setup(
        tmp_path, root_concurrency_group, lambda _req: httpx.Response(200), shared_file, access="READ"
    )

    detail = client.get(f"/ui/api/inbox/{event.request_id}/detail").get_json()["detail"]

    assert detail["is_sync_requested"] is False
    assert detail["sync_conflict"] == "NEWER"
    assert "Only folders can be synced" in detail["sync_unavailable_reason"]


def test_detail_offers_no_sync_when_this_build_cannot_run_one(tmp_path: Path) -> None:
    handler, _sender = _make_file_sharing_handler(tmp_path, lambda r: httpx.Response(200))
    event = create_file_sharing_permission_request(
        agent_id=str(AgentId()), path="/home/user/notes", access="WRITE", rationale="edit the notes"
    )

    payload = handler.build_request_detail_payload(
        permission_request=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiFileSharingPermissionDetail):
        pytest.fail(f"expected a file_sharing detail payload, got {payload!r}")
    assert payload.is_sync_supported is False
    assert payload.sync_unavailable_reason == ""


def test_a_sync_asked_for_by_a_chat_is_keyed_by_its_workspace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The Permissions tab draws syncs by the workspace's primary agent, so that is where a grant's sync must land."""
    primary, chat = AgentId(), AgentId()
    resolver = _ChatWorkspaceResolver(url_by_agent_and_service={}, primary_agent_id=primary, chat_agent_id=chat)
    folder = tmp_path / "project"
    folder.mkdir()
    handler, _sender = _make_file_sharing_handler(
        tmp_path,
        lambda _req: httpx.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        share_roots=(tmp_path,),
        home_dir=tmp_path,
    )
    manager = _folder_sync_manager(tmp_path, root_concurrency_group, primary, backend_resolver=resolver)
    event = create_file_sharing_permission_request(
        agent_id=str(chat), path=str(folder), access="READ", rationale="keep it close", sync=FileSharingSyncRequest()
    )
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        backend_resolver=resolver,
        folder_sync_manager=manager,
    )

    detail = client.get(f"/ui/api/inbox/{event.request_id}/detail").get_json()["detail"]
    response = client.post(f"/requests/{event.request_id}/grant", data={"file_path": str(folder), "sync": "true"})

    assert detail["sync_unavailable_reason"] == ""
    assert response.status_code == 200, response.text
    assert manager.desired_activity_for(str(primary), str(folder)) == FolderSyncActivity.ACTIVE
    assert manager.desired_activity_for(str(chat), str(folder)) is None
    assert manager.wait_until_started(str(primary), str(folder), _START_TIMEOUT_SECONDS) is not None
    manager.stop_all()
