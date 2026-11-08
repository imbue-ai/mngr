"""Tests for ``POST /api/v1/remote-execution/<device_id>``: the three gates, the callers, and the stream."""

import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.api_auth import AGENTS_ONLY_CODE
from imbue.minds.desktop_client.api_transport import AGENT_LABEL_HEADER
from imbue.minds.desktop_client.api_transport import LOCAL_TRUSTED_ENVIRON_KEY
from imbue.minds.desktop_client.api_v1 import UNKNOWN_DEVICE_CODE
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.remote_execution import REMOTE_EXECUTION_DISABLED_CODE
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.remote_command_runner import REMOTE_EXECUTION_EVENTS_RELATIVE_PATH
from imbue.minds.desktop_client.remote_command_runner import RemoteCommandStartedEvent
from imbue.minds.desktop_client.testing import make_minds_config_allowing_agent_command_execution
from imbue.minds.primitives import DeviceId
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_TEST_KEY = "test-minds-api-key"
_DEVICE_ID = DeviceId.generate()


def _client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup | None,
    minds_config: MindsConfig | None,
    agent_id: AgentId | None = None,
) -> tuple[FlaskClient, FileAuthStore]:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = StaticBackendResolver(url_by_agent_and_service={} if agent_id is None else {str(agent_id): {}})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        minds_config=minds_config,
        root_concurrency_group=root_concurrency_group,
        device_id=str(_DEVICE_ID),
        mngr_caller=RecordingMngrCaller(),
    )
    return app.test_client(), auth_store


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_KEY}"}


def _route(device_id: str = str(_DEVICE_ID)) -> str:
    return f"/api/v1/remote-execution/{device_id}"


def _events(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode().splitlines() if line]


@pytest.mark.witnesses(
    "remote-execution.three-gates", partial="the grant gate is the gateway's and is not exercised here"
)
def test_a_granted_agent_runs_a_command_and_reads_its_stream(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    response = client.post(
        _route(),
        headers=_auth_header(),
        json={"command": ["sh", "-c", "echo 36290; echo warn >&2; exit 2"], "timeout_seconds": 30},
    )

    assert response.status_code == 200
    assert response.mimetype == "application/x-ndjson"
    events = _events(response.data)
    assert "".join(str(e["data"]) for e in events if e["type"] == "stdout") == "36290\n"
    assert "".join(str(e["data"]) for e in events if e["type"] == "stderr") == "warn\n"
    assert events[-1] == {"type": "exit", "code": 2, "timed_out": False}


@pytest.mark.witnesses("remote-execution.every-command-recorded")
def test_every_command_is_recorded_with_the_callers_claimed_agent(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    client, _auth_store = _client(
        tmp_path,
        root_concurrency_group,
        make_minds_config_allowing_agent_command_execution(tmp_path / "config"),
        agent_id=agent_id,
    )

    response = client.post(
        _route(), headers={**_auth_header(), AGENT_LABEL_HEADER: str(agent_id)}, json={"command": ["true"]}
    )

    assert response.status_code == 200
    lines = (tmp_path / "minds" / REMOTE_EXECUTION_EVENTS_RELATIVE_PATH).read_text().splitlines()
    started = RemoteCommandStartedEvent.model_validate_json(lines[0])
    assert started.claimed_agent_id == str(agent_id)
    assert started.workspace_id == str(agent_id)
    assert started.transport == "GATEWAY"
    assert json.loads(lines[1])["type"] == "command_finished"


@pytest.mark.witnesses("remote-execution.agents-only", partial="the cookie refusal only")
def test_the_browser_session_cookie_is_refused(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    client, auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))

    response = client.post(_route(), json={"command": ["true"]})

    assert response.status_code == 403
    assert response.get_json()["code"] == AGENTS_ONLY_CODE


@pytest.mark.witnesses("remote-execution.agents-only", partial="only that nothing else gets in")
def test_an_unauthenticated_caller_is_refused(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    response = client.post(_route(), json={"command": ["true"]})

    assert response.status_code == 401


@pytest.mark.witnesses("remote-execution.three-gates", partial="the switch gate only")
@pytest.mark.witnesses("remote-execution.agents-only", partial="only that both ways in are held to the switch")
def test_the_switch_off_refuses_every_caller_including_the_local_socket(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(tmp_path, root_concurrency_group, MindsConfig(data_dir=tmp_path / "config"))

    over_gateway = client.post(_route(), headers=_auth_header(), json={"command": ["true"]})
    over_socket = client.post(_route(), json={"command": ["true"]}, environ_base={LOCAL_TRUSTED_ENVIRON_KEY: True})

    assert over_gateway.status_code == 403
    assert over_gateway.get_json()["code"] == REMOTE_EXECUTION_DISABLED_CODE
    assert over_socket.status_code == 403
    assert over_socket.get_json()["code"] == REMOTE_EXECUTION_DISABLED_CODE
    assert not (tmp_path / "minds" / REMOTE_EXECUTION_EVENTS_RELATIVE_PATH).exists()


@pytest.mark.witnesses("remote-execution.agents-only", partial="the socket way in only")
def test_the_local_socket_needs_no_credential_once_the_switch_is_on(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    response = client.post(
        _route(), json={"command": ["sh", "-c", "echo 36291"]}, environ_base={LOCAL_TRUSTED_ENVIRON_KEY: True}
    )

    assert response.status_code == 200
    events = _events(response.data)
    assert {"type": "stdout", "data": "36291\n"} in events
    started = RemoteCommandStartedEvent.model_validate_json(
        (tmp_path / "minds" / REMOTE_EXECUTION_EVENTS_RELATIVE_PATH).read_text().splitlines()[0]
    )
    assert started.transport == "LOCAL_SOCKET"


@pytest.mark.witnesses("remote-execution.three-gates", partial="the device gate only")
def test_another_devices_id_in_the_path_is_refused(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    foreign = client.post(_route(str(DeviceId.generate())), headers=_auth_header(), json={"command": ["true"]})
    malformed = client.post(_route("not-a-device"), headers=_auth_header(), json={"command": ["true"]})

    assert foreign.status_code == 404
    assert foreign.get_json()["code"] == UNKNOWN_DEVICE_CODE
    assert malformed.status_code == 404
    assert malformed.get_json()["code"] == UNKNOWN_DEVICE_CODE


def test_a_structurally_invalid_body_is_refused_with_422(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    empty_command = client.post(_route(), headers=_auth_header(), json={"command": []})
    bad_timeout = client.post(_route(), headers=_auth_header(), json={"command": ["true"], "timeout_seconds": 0})

    assert empty_command.status_code == 422
    assert bad_timeout.status_code == 422


def test_a_working_directory_that_is_not_one_is_refused_with_400(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    response = client.post(
        _route(), headers=_auth_header(), json={"command": ["true"], "cwd": "/definitely/missing/36292"}
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["field"] == "cwd"
    assert body["code"] == "cwd_not_a_directory"


def test_a_desktop_with_no_concurrency_group_answers_503(tmp_path: Path) -> None:
    client, _auth_store = _client(
        tmp_path, None, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    response = client.post(_route(), headers=_auth_header(), json={"command": ["true"]})

    assert response.status_code == 503


def test_the_route_is_listed_in_the_gateway_schema_with_its_request_body(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _auth_store = _client(
        tmp_path, root_concurrency_group, make_minds_config_allowing_agent_command_execution(tmp_path / "config")
    )

    document = client.get("/api/schema", headers=_auth_header()).get_json()

    operation = document["paths"]["/api/v1/remote-execution/{device_id}"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RemoteExecutionRequest"
    }
    assert "RemoteExecutionRequest" in document["components"]["schemas"]
