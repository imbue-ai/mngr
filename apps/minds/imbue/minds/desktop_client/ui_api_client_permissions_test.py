"""Tests for the /ui/api client-permission routes: the gate's decision read, the pane's section, and its writes."""

import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import ClientPermissionSource
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.testing import ChatInWorkspaceResolver
from imbue.minds.desktop_client.ui_api_client_permissions import CLIENT_PERMISSION_COPY_BY_NAME
from imbue.minds.desktop_client.ui_models import UiClientPermissionRow
from imbue.minds.desktop_client.ui_models import UiClientPermissions
from imbue.minds.desktop_client.workspace_install_settings_store import WorkspaceInstallSettingsStore
from imbue.mngr.primitives import AgentId

# The name ChatInWorkspaceResolver gives its workspace.
_WORKSPACE_NAME = "alpha"
_MICROPHONE_DESCRIPTION = "Listen to an audio input: the built-in mic, a headset, a USB interface."


def _records_dir(tmp_path: Path) -> Path:
    return tmp_path / "workspace_install_settings"


def _build_client(
    tmp_path: Path,
    workspace_id: AgentId,
    is_authenticated: bool = True,
    is_store_wired: bool = True,
) -> tuple[FlaskClient, WorkspaceInstallSettingsStore | None]:
    """A desktop client whose resolver knows ``workspace_id`` as the workspace named "alpha"."""
    store = WorkspaceInstallSettingsStore(records_dir=_records_dir(tmp_path)) if is_store_wired else None
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=ChatInWorkspaceResolver(
            url_by_agent_and_service={}, workspace_agent_id=workspace_id, chat_agent_id=AgentId.generate()
        ),
        workspace_install_settings_store=store,
    )
    return client, store


def _decision_url(workspace: str, permission: str) -> str:
    return f"/ui/api/client-permissions/decision?workspace={workspace}&permission={permission}"


def _section_url(workspace: str) -> str:
    return f"/ui/api/workspaces/{workspace}/client-permissions"


def _write_url(workspace: str, permission: str) -> str:
    return f"/ui/api/workspaces/{workspace}/client-permissions/{permission}"


def _microphone_row(client: FlaskClient, workspace_id: AgentId) -> UiClientPermissionRow:
    response = client.get(_section_url(str(workspace_id)))
    assert response.status_code == 200
    (row,) = UiClientPermissions.model_validate_json(response.data).rows
    return row


def test_every_declared_permission_has_copy_for_the_pane() -> None:
    """A permission declared without a label and description could reach the pane unlabeled."""
    assert set(CLIENT_PERMISSION_COPY_BY_NAME) == set(ClientPermissionName)


def test_the_routes_refuse_an_unauthenticated_request(tmp_path: Path) -> None:
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id, is_authenticated=False)
    assert client.get(_decision_url(str(workspace_id), "microphone")).status_code == 401
    assert client.get(_section_url(str(workspace_id))).status_code == 401
    assert client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "granted"}).status_code == 401


def test_the_decision_route_answers_the_imbue_default_and_names_the_workspace(tmp_path: Path) -> None:
    """A fresh install: every workspace defers to the Imbue default, and the gate learns the name for its toast."""
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    response = client.get(_decision_url(str(workspace_id), "microphone"))
    assert response.status_code == 200
    assert response.get_json() == {"decision": "ask", "source": "default", "workspace_name": _WORKSPACE_NAME}


def test_the_decision_route_names_a_workspace_the_resolver_does_not_know_by_its_id(tmp_path: Path) -> None:
    """The decision is this computer's and needs nothing from the workspace, so an unknown one is still answered."""
    client, _store = _build_client(tmp_path, AgentId.generate())
    unknown_id = AgentId.generate()
    response = client.get(_decision_url(str(unknown_id), "microphone"))
    assert response.status_code == 200
    assert response.get_json() == {"decision": "ask", "source": "default", "workspace_name": str(unknown_id)}


def test_a_stored_decision_answers_the_gate_with_the_workspace_as_its_source(tmp_path: Path) -> None:
    workspace_id = AgentId.generate()
    client, store = _build_client(tmp_path, workspace_id)
    assert store is not None
    store.set_client_permission(str(workspace_id), ClientPermissionName.MICROPHONE, ClientPermissionDecision.DENIED)
    response = client.get(_decision_url(str(workspace_id), "microphone"))
    assert response.get_json() == {"decision": "denied", "source": "workspace", "workspace_name": _WORKSPACE_NAME}


@pytest.mark.parametrize(
    "query",
    [
        "permission=microphone",
        "workspace=&permission=microphone",
        "workspace=not-a-workspace&permission=microphone",
        "workspace=host-00112233445566778899aabbccddeeff&permission=microphone",
    ],
    ids=["missing", "empty", "not-an-id", "legacy-host-id"],
)
def test_the_decision_route_refuses_a_workspace_that_is_not_an_agent_id(tmp_path: Path, query: str) -> None:
    """The backend accepts only agent ids (spec section 5.7); the gate translates legacy host origins itself."""
    client, _store = _build_client(tmp_path, AgentId.generate())
    response = client.get(f"/ui/api/client-permissions/decision?{query}")
    assert response.status_code == 400
    assert "workspace" in response.get_json()["error"]


@pytest.mark.parametrize("permission", ["camera", "", "MICROPHONE"], ids=["undeclared", "missing", "wrong-case"])
def test_the_decision_route_refuses_an_undeclared_permission(tmp_path: Path, permission: str) -> None:
    """Only the lowercase Permissions Policy feature names this build declares are permissions (spec D2, D13)."""
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    assert client.get(_decision_url(str(workspace_id), permission)).status_code == 404


def test_the_section_lists_one_row_per_declared_permission_deferring_to_the_default(tmp_path: Path) -> None:
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    response = client.get(_section_url(str(workspace_id)))
    assert response.status_code == 200
    assert UiClientPermissions.model_validate_json(response.data) == UiClientPermissions(
        rows=(
            UiClientPermissionRow(
                permission="microphone",
                label="Microphone",
                description=_MICROPHONE_DESCRIPTION,
                decision=ClientPermissionDecision.ASK,
                source=ClientPermissionSource.DEFAULT,
                stored=None,
            ),
        )
    )


def test_a_put_stores_the_decision_for_the_next_launch_and_answers_the_refreshed_section(tmp_path: Path) -> None:
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    response = client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "denied"})
    assert response.status_code == 200
    (row,) = UiClientPermissions.model_validate_json(response.data).rows
    assert (row.decision, row.source, row.stored) == (
        ClientPermissionDecision.DENIED,
        ClientPermissionSource.WORKSPACE,
        ClientPermissionDecision.DENIED,
    )
    assert json.loads((_records_dir(tmp_path) / f"{workspace_id}.json").read_text()) == {
        "settings_format": SUPPORTED_SETTINGS_FORMAT,
        "client_permissions": {"microphone": "denied"},
    }
    assert client.get(_decision_url(str(workspace_id), "microphone")).get_json()["decision"] == "denied"


def test_a_put_of_null_deletes_the_entry_so_the_workspace_defers_again(tmp_path: Path) -> None:
    """Use the default (spec D14): the workspace holds no value of its own, and its file goes with it."""
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    assert client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "granted"}).status_code == 200
    assert (_records_dir(tmp_path) / f"{workspace_id}.json").is_file()
    response = client.put(_write_url(str(workspace_id), "microphone"), json={"decision": None})
    assert response.status_code == 200
    (row,) = UiClientPermissions.model_validate_json(response.data).rows
    assert (row.decision, row.source, row.stored) == (
        ClientPermissionDecision.ASK,
        ClientPermissionSource.DEFAULT,
        None,
    )
    assert not (_records_dir(tmp_path) / f"{workspace_id}.json").exists()


def test_an_explicit_ask_is_the_workspaces_own_answer_rather_than_a_deferral(tmp_path: Path) -> None:
    """Spec D14: a stored ask behaves like the default today but is the workspace's own, told apart by its source."""
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    assert client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "ask"}).status_code == 200
    row = _microphone_row(client, workspace_id)
    assert (row.decision, row.source, row.stored) == (
        ClientPermissionDecision.ASK,
        ClientPermissionSource.WORKSPACE,
        ClientPermissionDecision.ASK,
    )


@pytest.mark.parametrize(
    "body",
    [{"decision": "maybe"}, {}, {"decision": "granted", "remember": True}, {"decision": 1}],
    ids=["unknown-decision", "no-decision", "extra-key", "not-a-string"],
)
def test_a_put_refuses_a_malformed_body_and_stores_nothing(tmp_path: Path, body: dict[str, object]) -> None:
    workspace_id = AgentId.generate()
    client, store = _build_client(tmp_path, workspace_id)
    assert store is not None
    response = client.put(_write_url(str(workspace_id), "microphone"), json=body)
    assert response.status_code == 400
    assert "decision" in response.get_json()["error"]
    assert store.read(str(workspace_id)).client_permissions == {}


@pytest.mark.parametrize("raw_body", ['["granted"]', "granted", ""], ids=["list", "bare-word", "empty"])
def test_a_put_refuses_a_body_that_is_not_an_object(tmp_path: Path, raw_body: str) -> None:
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id)
    response = client.put(_write_url(str(workspace_id), "microphone"), data=raw_body, content_type="application/json")
    assert response.status_code == 400


def test_a_put_to_an_undeclared_permission_is_refused_and_stores_nothing(tmp_path: Path) -> None:
    workspace_id = AgentId.generate()
    client, store = _build_client(tmp_path, workspace_id)
    assert store is not None
    assert client.put(_write_url(str(workspace_id), "camera"), json={"decision": "granted"}).status_code == 404
    assert store.read(str(workspace_id)).client_permissions == {}


def test_the_workspace_routes_refuse_a_workspace_id_that_is_not_an_agent_id(tmp_path: Path) -> None:
    client, _store = _build_client(tmp_path, AgentId.generate())
    assert client.get(_section_url("not-a-workspace")).status_code == 404
    assert client.put(_write_url("not-a-workspace", "microphone"), json={"decision": "granted"}).status_code == 404


def test_a_build_without_a_store_answers_every_route_unavailable(tmp_path: Path) -> None:
    """A minimal app has nowhere to keep decisions, so it answers neither the gate nor the pane."""
    workspace_id = AgentId.generate()
    client, _store = _build_client(tmp_path, workspace_id, is_store_wired=False)
    responses = (
        client.get(_decision_url(str(workspace_id), "microphone")),
        client.get(_section_url(str(workspace_id))),
        client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "granted"}),
    )
    assert [response.status_code for response in responses] == [503, 503, 503]
    assert all("error" in response.get_json() for response in responses)


def test_a_record_from_a_newer_build_is_shown_but_refuses_a_write(tmp_path: Path) -> None:
    """Spec section 6: a too-new record is resolved from and shown, and this build must not rewrite it."""
    workspace_id = AgentId.generate()
    _records_dir(tmp_path).mkdir()
    too_new = {"settings_format": SUPPORTED_SETTINGS_FORMAT + 1, "client_permissions": {"microphone": "granted"}}
    (_records_dir(tmp_path) / f"{workspace_id}.json").write_text(json.dumps(too_new))
    client, _store = _build_client(tmp_path, workspace_id)
    row = _microphone_row(client, workspace_id)
    assert (row.decision, row.source, row.stored) == (
        ClientPermissionDecision.GRANTED,
        ClientPermissionSource.WORKSPACE,
        ClientPermissionDecision.GRANTED,
    )
    response = client.put(_write_url(str(workspace_id), "microphone"), json={"decision": "denied"})
    assert response.status_code == 409
    assert "newer version" in response.get_json()["error"]
    assert json.loads((_records_dir(tmp_path) / f"{workspace_id}.json").read_text()) == too_new


def test_an_app_with_a_data_directory_keeps_its_records_beside_the_folder_syncs(tmp_path: Path) -> None:
    """The store is built from the data directory when none is passed in (spec section 5.2)."""
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, paths=InstallationPaths(data_dir=tmp_path / "data")
    )
    store = get_state(app).workspace_install_settings_store
    assert store is not None
    assert store.records_dir == tmp_path / "data" / "workspace_install_settings"
