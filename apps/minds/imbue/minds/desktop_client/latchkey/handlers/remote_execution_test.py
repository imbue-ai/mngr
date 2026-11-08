"""Unit tests for :class:`RemoteExecutionGrantHandler`."""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx2
import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_REMOTE_EXECUTION
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.handlers.remote_execution import REMOTE_EXECUTION_DISABLED_CODE
from imbue.minds.desktop_client.latchkey.handlers.remote_execution import RemoteExecutionGrantHandler
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import load_response_events
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.minds.desktop_client.latchkey.testing import leave_permissions_on_this_computer
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.request_handler import UiRemoteExecutionPermissionDetail
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import create_remote_execution_permission_request
from imbue.minds.desktop_client.testing import make_minds_config_allowing_agent_command_execution
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_HttpxHandler: Final = Callable[[httpx2.Request], httpx2.Response]


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, chat_id: AgentId, text: str, exec_agent_address: str) -> None:
        self.sent_messages.append((str(chat_id), text))


def _build_gateway_client(handler: _HttpxHandler) -> LatchkeyGatewayClient:
    return LatchkeyGatewayClient.from_credentials(
        transport=httpx2.MockTransport(handler),
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
    )


def _make_handler(
    tmp_path: Path,
    gateway_handler: _HttpxHandler,
    push_permissions_to_machine: Callable[[str], None] = leave_permissions_on_this_computer,
) -> tuple[RemoteExecutionGrantHandler, _RecordingMessageSender]:
    sender = _RecordingMessageSender(
        concurrency_group=ConcurrencyGroup(name="recording-message-sender-unused"), sent_messages=[]
    )
    return (
        RemoteExecutionGrantHandler(
            data_dir=tmp_path,
            gateway_client=_build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
            push_permissions_to_machine=push_permissions_to_machine,
        ),
        sender,
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: RemoteExecutionGrantHandler,
    inbox: StaticPendingRequests,
    minds_config: MindsConfig | None,
    backend_resolver: BackendResolverInterface | None = None,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    if backend_resolver is None:
        backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path),
        pending_requests=inbox,
        request_event_handlers=(handler,),
        minds_config=minds_config,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _recording_gateway(captured: dict[str, object]) -> _HttpxHandler:
    def _gateway_handler(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx2.Response(200, json={"request_id": "evt-abc", "applied": {}})

    return _gateway_handler


def test_handler_claims_remote_execution_request_type(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path, lambda _req: httpx2.Response(200))
    assert handler.handles_request_type() == REQUEST_TYPE_REMOTE_EXECUTION


@pytest.mark.witnesses("remote-execution.approve-with-switch-on")
def test_grant_with_the_switch_on_approves_writes_response_and_notifies_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    handler, sender = _make_handler(tmp_path, _recording_gateway(captured))
    agent_id = AgentId()
    event = create_remote_execution_permission_request(agent_id=str(agent_id), rationale="install a tool")
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        minds_config=make_minds_config_allowing_agent_command_execution(tmp_path / "config"),
    )

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"
    # The approve carries no override body: the effect is fixed per desktop.
    assert captured["method"] == "POST"
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.request_id}")
    assert captured["content"] == b""
    response_events = load_response_events(tmp_path)
    assert [(e.status, e.request_event_id) for e in response_events] == [("GRANTED", event.request_id)]
    assert sender.sent_messages == [
        (
            str(agent_id),
            format_resolution_notice(response.get_json()["message"], event.request_id, RequestStatus.GRANTED),
        )
    ]


@pytest.mark.witnesses(
    "remote-execution.approve-needs-the-switch",
    partial="the refusal and the pending request; the popup is the frontend's",
)
@pytest.mark.witnesses(
    "remote-execution.grant-is-its-own-kind", partial="only that approval is refused while the switch is off"
)
def test_grant_with_the_switch_off_is_refused_and_leaves_the_request_pending(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    handler, sender = _make_handler(tmp_path, _recording_gateway(captured))
    event = create_remote_execution_permission_request(agent_id=str(AgentId()), rationale="install a tool")
    inbox = StaticPendingRequests(pending=(event,))
    client = _build_authenticated_client(tmp_path, handler, inbox, minds_config=MindsConfig(data_dir=tmp_path / "c"))

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 409
    assert response.get_json()["code"] == REMOTE_EXECUTION_DISABLED_CODE
    assert captured == {}
    assert load_response_events(tmp_path) == []
    assert sender.sent_messages == []
    assert inbox.get_pending(event.request_id) is not None


def test_grant_is_refused_when_the_app_has_no_settings_storage(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    handler, _sender = _make_handler(tmp_path, _recording_gateway(captured))
    event = create_remote_execution_permission_request(agent_id=str(AgentId()), rationale="install a tool")
    client = _build_authenticated_client(tmp_path, handler, StaticPendingRequests(pending=(event,)), minds_config=None)

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 409
    assert captured == {}


def test_deny_deletes_request_writes_response_and_notifies_agent_whatever_the_switch(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _gateway_handler(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        return httpx2.Response(200)

    handler, sender = _make_handler(tmp_path, _gateway_handler)
    agent_id = AgentId()
    event = create_remote_execution_permission_request(agent_id=str(agent_id), rationale="install a tool")
    client = _build_authenticated_client(
        tmp_path, handler, StaticPendingRequests(pending=(event,)), minds_config=MindsConfig(data_dir=tmp_path / "c")
    )

    response = client.post(f"/requests/{event.request_id}/deny")

    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    assert captured["method"] == "DELETE"
    assert [e.status for e in load_response_events(tmp_path)] == ["DENIED"]
    assert sender.sent_messages == [
        (
            str(agent_id),
            format_resolution_notice(response.get_json()["message"], event.request_id, RequestStatus.DENIED),
        )
    ]


@pytest.mark.parametrize("is_enabled", [False, True])
@pytest.mark.witnesses(
    "remote-execution.approve-needs-the-switch", partial="only the detail the popup reads the switch from"
)
def test_detail_payload_reports_the_switch_and_names_this_computer(tmp_path: Path, is_enabled: bool) -> None:
    handler, _sender = _make_handler(tmp_path, lambda _req: httpx2.Response(200))
    event = create_remote_execution_permission_request(agent_id=str(AgentId()), rationale="install a tool")
    minds_config = (
        make_minds_config_allowing_agent_command_execution(tmp_path / "config")
        if is_enabled
        else MindsConfig(data_dir=tmp_path / "c")
    )
    client = _build_authenticated_client(tmp_path, handler, StaticPendingRequests(pending=(event,)), minds_config)

    response = client.get(f"/ui/api/inbox/{event.request_id}/detail")

    assert response.status_code == 200
    payload = UiRemoteExecutionPermissionDetail.model_validate(response.get_json()["detail"])
    assert payload.request_id == event.request_id
    assert payload.rationale == "install a tool"
    assert payload.is_setting_enabled is is_enabled
    assert payload.is_workspace_shared is False
    assert payload.device_hostname


def test_grant_hands_the_spliced_policy_to_the_workspaces_own_machine(tmp_path: Path) -> None:
    carried: list[str] = []
    handler, _sender = _make_handler(
        tmp_path,
        lambda _req: httpx2.Response(200, json={"request_id": "evt-abc", "applied": {}}),
        push_permissions_to_machine=carried.append,
    )
    agent_id = AgentId()
    event = create_remote_execution_permission_request(agent_id=str(agent_id), rationale="install a tool")
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        minds_config=make_minds_config_allowing_agent_command_execution(tmp_path / "config"),
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=HostId(), known_agent_ids=(agent_id,)
        ),
    )

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 200
    assert carried == [str(agent_id)]


@pytest.mark.witnesses(
    "remote-execution.ask-for-one-program",
    partial="the detail the popup reads; the gateway's pin is permission_requests_test",
)
def test_detail_payload_for_a_program_request_names_it_and_where_it_resolves(tmp_path: Path) -> None:
    """A ``~``-rooted program resolves as a file under the home directory, present or not yet; a bare interpreter is flagged."""
    handler, _sender = _make_handler(tmp_path, lambda _req: httpx2.Response(200))
    script_request = create_remote_execution_permission_request(
        agent_id=str(AgentId()), rationale="deploy", program="~/.studio/custom-scripts/deploy-36303.sh"
    )
    shell_request = create_remote_execution_permission_request(
        agent_id=str(AgentId()), rationale="shell", program="sh"
    )
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(script_request, shell_request)),
        make_minds_config_allowing_agent_command_execution(tmp_path / "config"),
    )

    script_detail = UiRemoteExecutionPermissionDetail.model_validate(
        client.get(f"/ui/api/inbox/{script_request.request_id}/detail").get_json()["detail"]
    )
    shell_detail = UiRemoteExecutionPermissionDetail.model_validate(
        client.get(f"/ui/api/inbox/{shell_request.request_id}/detail").get_json()["detail"]
    )

    assert script_detail.program == "~/.studio/custom-scripts/deploy-36303.sh"
    assert script_detail.resolved_program_path is None
    assert script_detail.is_program_interpreter is False
    assert shell_detail.program == "sh"
    assert shell_detail.resolved_program_path is not None
    assert shell_detail.resolved_program_path.endswith("/sh")
    assert shell_detail.is_program_interpreter is True


def test_an_any_command_request_carries_no_program(tmp_path: Path) -> None:
    handler, _sender = _make_handler(tmp_path, lambda _req: httpx2.Response(200))
    event = create_remote_execution_permission_request(agent_id=str(AgentId()), rationale="anything")
    client = _build_authenticated_client(
        tmp_path, handler, StaticPendingRequests(pending=(event,)), MindsConfig(data_dir=tmp_path / "c")
    )

    detail = UiRemoteExecutionPermissionDetail.model_validate(
        client.get(f"/ui/api/inbox/{event.request_id}/detail").get_json()["detail"]
    )

    assert (detail.program, detail.resolved_program_path, detail.is_program_interpreter) == (None, None, False)
    assert handler.display_name_for_event(event) == "Run commands on this computer"


def test_a_program_grant_tells_the_agent_which_program_it_may_run(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    handler, sender = _make_handler(tmp_path, _recording_gateway(captured))
    event = create_remote_execution_permission_request(agent_id=str(AgentId()), rationale="sync", program="rsync")
    client = _build_authenticated_client(
        tmp_path,
        handler,
        StaticPendingRequests(pending=(event,)),
        make_minds_config_allowing_agent_command_execution(tmp_path / "config"),
    )

    response = client.post(f"/requests/{event.request_id}/grant")

    assert response.status_code == 200
    assert str(captured["path"]).endswith(f"/permission-requests/approve/{event.request_id}")
    assert captured["content"] == b""
    assert handler.display_name_for_event(event) == "Run rsync on this computer"
    assert "Your request to run rsync on" in sender.sent_messages[0][1]
