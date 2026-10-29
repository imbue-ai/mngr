import base64
import json
import os
import queue
import re
import shlex
import time
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx2
import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr
from werkzeug.test import TestResponse

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client import workspace_ssh
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreateAttemptErrorKind
from imbue.minds.desktop_client.api_v1 import CONNECTION_GONE_DURING_UPDATE_MESSAGE
from imbue.minds.desktop_client.api_v1 import CREATE_IN_FLIGHT_ON_CONNECTION_MESSAGE
from imbue.minds.desktop_client.api_v1 import REGISTRATION_IN_FLIGHT_ON_CONNECTION_MESSAGE
from imbue.minds.desktop_client.api_v1 import UPDATE_IN_FLIGHT_ON_CONNECTION_MESSAGE
from imbue.minds.desktop_client.api_v1 import _SSH_GRANT_WRITE_ATTEMPTS
from imbue.minds.desktop_client.api_v1 import _describe_mngr_exec_failure
from imbue.minds.desktop_client.api_v1 import _drain_backup_summary_rows
from imbue.minds.desktop_client.api_v1 import _stream_workspace_backup_summaries
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backend_resolver import WORKSPACE_DISPLAY_NAME_LABEL
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_update import BELOW_UPDATE_FLOOR_MESSAGE
from imbue.minds.desktop_client.backup_update import BLOCKED_BY_RUNNING_CHATS_PREFIX
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import TEST_RELAY_ENDPOINTS
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_provider_materializer_for_test
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import make_share_probe_result
from imbue.minds.desktop_client.conftest import session_store_account_source
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.create_status import status_text_for
from imbue.minds.desktop_client.data_types import MachineSizeSelection
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudClientTooOldCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationCliResult
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationOutcomeCliEntry
from imbue.minds.desktop_client.imbue_cloud_cli import MachineSizeCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.mock_agent_creator_test import InFlightCreateAttempt
from imbue.minds.desktop_client.mock_agent_creator_test import make_in_flight_agent_creator
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import ChatInWorkspaceResolver
from imbue.minds.desktop_client.testing import blocking_release_wait_body
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.testing import drain_ui_channel_frames
from imbue.minds.desktop_client.testing import install_stub_mngr_on_path
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.desktop_client.testing import wait_until_synced
from imbue.minds.desktop_client.testing import write_fake_mngr_pair_script
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.desktop_client.workspace_create import CreateOnCreatedCallback
from imbue.minds.desktop_client.workspace_defaults import default_workspace_template_ref
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import RECORD_TOO_NEW_MESSAGE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_update_state import UpdateAvailability
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.errors import WorkspaceNameInUseError
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import DEFAULT_AWS_INSTANCE_TYPE
from imbue.minds.primitives import DEFAULT_AZURE_VM_SIZE
from imbue.minds.primitives import DEFAULT_GCP_MACHINE_TYPE
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import LaunchMode
from imbue.minds.testing import stub_mngr_host_dir
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_TEST_KEY = "test-minds-api-key"


def _client_with_workspace(tmp_path: Path, agent_id: AgentId) -> FlaskClient:
    """Build a desktop-client test client with the /api/v1 surface mounted.

    Passing ``paths`` mounts the ``/api/v1`` blueprint, and ``minds_api_key``
    sets the bearer the routes require. The StaticBackendResolver reports the
    one workspace under both the known-agents and known-workspaces lists.
    """
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        # A recording caller so routes that shell out (e.g. the version route's
        # in-workspace git read) are fast in-memory no-ops, never spawning a
        # real ``mngr`` process.
        mngr_caller=RecordingMngrCaller(),
    )
    return app.test_client()


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_KEY}"}


class _RecordingAgentCreator(AgentCreator):
    """An ``AgentCreator`` whose ``start_create_attempt`` records its args instead of spawning.

    The real ``start_create_attempt`` launches a background thread that clones the repo
    and shells out to ``mngr create``; the create-route tests only need to assert
    on what the route *passes* to it (resolved host name, color, ...), so this
    stub captures the call and returns a fresh ``CreateAttemptId`` synchronously.
    """

    _last_call: dict[str, object] | None = PrivateAttr(default=None)

    def start_create_attempt(
        self,
        repo_source: str,
        host_name: str = "",
        display_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.DOCKER,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        cloud_account: str = "",
        instance_type: str = "",
        machine_size: MachineSizeSelection | None = None,
        modal_sandbox_timeout_seconds: int | None = None,
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
        account_id: str = "",
        is_onboarding_driven_by_agent: bool = False,
    ) -> CreateAttemptId:
        self._last_call = {
            "repo_source": repo_source,
            "host_name": host_name,
            "display_name": display_name,
            "branch": branch,
            "launch_mode": launch_mode,
            "account_email": account_email,
            "branch_or_tag": branch_or_tag,
            "region": region,
            "cloud_account": cloud_account,
            "instance_type": instance_type,
            "machine_size": machine_size,
            "modal_sandbox_timeout_seconds": modal_sandbox_timeout_seconds,
            "color": color,
            "docker_runtime": docker_runtime,
            "original_minds_version": original_minds_version,
            "account_id": account_id,
            "is_onboarding_driven_by_agent": is_onboarding_driven_by_agent,
            "on_created": on_created,
        }
        return CreateAttemptId()

    @property
    def last_call(self) -> dict[str, object]:
        assert self._last_call is not None, "start_create_attempt was never called"
        return self._last_call


class _StatusReportingAgentCreator(_RecordingAgentCreator):
    """Recording creator that also reports a fixed create attempt info for status polls."""

    fixed_info: AgentCreateAttemptInfo

    def get_create_attempt_info(self, create_attempt_id: CreateAttemptId) -> AgentCreateAttemptInfo | None:
        return self.fixed_info if create_attempt_id == self.fixed_info.create_attempt_id else None


def _client_with_agent_creator(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    *,
    resolver: BackendResolverInterface | None = None,
    agent_creator: AgentCreator | None = None,
    session_store: MultiAccountSessionStore | None = None,
    minds_config: MindsConfig | None = None,
    connection_store: ConnectionStore | None = None,
) -> FlaskClient:
    """Build a test client whose ``/api/v1`` create route has an ``AgentCreator`` wired.

    The create route returns 501 when no ``AgentCreator`` is configured (before
    any input validation runs), so reaching the validation branches requires a
    creator. The invalid-input tests assert on 400 responses that return before
    ``start_create_attempt`` is ever called; the happy-path tests pass a
    :class:`_RecordingAgentCreator` so the route's call is captured without
    starting a real background create attempt (subprocess / network).
    """
    if resolver is None:
        resolver = StaticBackendResolver(url_by_agent_and_service={})
    if agent_creator is None:
        agent_creator = AgentCreator(
            paths=InstallationPaths(data_dir=tmp_path / "minds"),
            root_concurrency_group=root_concurrency_group,
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
        )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        agent_creator=agent_creator,
        session_store=session_store,
        connection_store=connection_store,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        minds_config=minds_config,
    )
    return app.test_client()


def _make_recording_creator(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> _RecordingAgentCreator:
    return _RecordingAgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )


def _feed_entries(app: Flask) -> tuple[UiNotificationEntry, ...]:
    with app.app_context():
        feed = get_state().notification_feed
    assert feed is not None
    return feed.reconcile((), {}).entries


def _client_with_chat(
    tmp_path: Path, workspace_agent_id: AgentId, chat_agent_id: AgentId
) -> tuple[FlaskClient, Flask]:
    resolver = ChatInWorkspaceResolver(
        url_by_agent_and_service={}, workspace_agent_id=workspace_agent_id, chat_agent_id=chat_agent_id
    )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
    )
    return app.test_client(), app


def test_agent_notification_lands_in_the_feed_as_an_agent_message(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    response = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "The migration finished; 3 tables moved."},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    (entry,) = _feed_entries(app)
    assert entry.kind == "agent_message"
    assert entry.is_resolved is False
    assert entry.title == "Migration chat"
    assert entry.body == "The migration finished; 3 tables moved."
    assert entry.chat_agent_id == str(chat_agent_id)
    assert entry.workspace_agent_id == str(workspace_agent_id)
    assert entry.workspace_name == "alpha"


def test_agent_notification_title_becomes_a_prefix_on_the_body(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    response = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "all green", "title": "Test run"},
        headers=_auth_header(),
    )

    assert response.status_code == 200
    (entry,) = _feed_entries(app)
    assert entry.body == "Test run: all green"


def _app_with_moved_chat(
    tmp_path: Path, workspace_agent_id: AgentId, chat_id: AgentId, members: tuple[AgentId, ...]
) -> tuple[Flask, BackendResolverInterface]:
    """An app whose resolver knows a chat that has run on each of ``members`` in turn, under one ``chat_id``."""
    resolver = make_resolver_with_data(
        json.dumps(
            {
                "agents": [
                    {
                        "id": str(workspace_agent_id),
                        "name": "system-services",
                        "labels": {"is_primary": "true", "workspace_display_name": "alpha", "color": "#123456"},
                    },
                    *[
                        {
                            "id": str(member),
                            "name": "migration-chat",
                            "labels": {
                                "chat_id": str(chat_id),
                                "chat_seq": str(seq),
                                "workspace_display_name": "alpha",
                            },
                        }
                        for seq, member in enumerate(members, start=1)
                    ],
                ]
            }
        )
    )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
    )
    return app, resolver


def test_agent_notifications_keep_the_same_chat_destination_across_agents(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_id = AgentId()
    members = (AgentId(), AgentId())
    app, resolver = _app_with_moved_chat(tmp_path, workspace_agent_id, chat_id, members)

    for member in members:
        response = app.test_client().post(
            f"/api/v1/agents/{member}/notifications",
            json={"message": f"Finished work on {member}"},
            headers=_auth_header(),
        )
        assert response.status_code == 200

    entries = _feed_entries(app)
    assert len(entries) == 2
    assert chat_id not in resolver.list_known_agent_ids()
    for entry in entries:
        assert entry.chat_agent_id == str(chat_id)
        assert entry.workspace_agent_id == str(workspace_agent_id)
        assert entry.workspace_name == "alpha"
        assert entry.workspace_accent == "#123456"
        assert entry.title == "migration-chat"


def test_a_watched_agent_notification_is_recorded_read(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    watched = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "Seen as it happened", "watched_by": ["instance-1", "instance-2"]},
        headers=_auth_header(),
    )
    unwatched = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "Nobody was looking", "watched_by": []},
        headers=_auth_header(),
    )

    assert watched.status_code == 200
    assert unwatched.status_code == 200
    resolved_by_body = {entry.body: entry.is_resolved for entry in _feed_entries(app)}
    assert resolved_by_body == {"Seen as it happened": True, "Nobody was looking": False}


def test_reading_a_chat_through_one_of_its_agents_resolves_the_chat_messages(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)
    client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "The migration finished."},
        headers=_auth_header(),
    )

    response = client.post(f"/api/v1/agents/{chat_agent_id}/notifications/read", json={}, headers=_auth_header())
    again = client.post(f"/api/v1/agents/{chat_agent_id}/notifications/read", json={}, headers=_auth_header())

    assert response.status_code == 200
    # Nothing left to read is still a success: the workspace calls on every watch transition.
    assert again.status_code == 200
    (entry,) = _feed_entries(app)
    assert entry.is_resolved is True


def test_reading_a_chat_resolves_messages_filed_by_its_earlier_agent(tmp_path: Path) -> None:
    """The chat moved to a new agent: reading through the new one still reads what the old one sent."""
    earlier, current = AgentId(), AgentId()
    app, _resolver = _app_with_moved_chat(tmp_path, AgentId(), AgentId(), (earlier, current))
    client = app.test_client()
    client.post(
        f"/api/v1/agents/{earlier}/notifications", json={"message": "Done on the old agent"}, headers=_auth_header()
    )

    response = client.post(f"/api/v1/agents/{current}/notifications/read", json={}, headers=_auth_header())

    assert response.status_code == 200
    (entry,) = _feed_entries(app)
    assert entry.is_resolved is True


def test_agent_notification_from_the_primary_agent_with_a_destination_opens_an_app_page(tmp_path: Path) -> None:
    """The routines runner notifies from the workspace's primary agent, which is not a chat, and names a page to land on."""
    workspace_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, AgentId())

    response = client.post(
        f"/api/v1/agents/{workspace_agent_id}/notifications",
        json={
            "message": "3 new items",
            "title": "Inbox digest",
            "destination": {"type": "minds:show-routine", "routine_id": "inbox-digest"},
        },
        headers=_auth_header(),
    )

    assert response.status_code == 200
    (entry,) = _feed_entries(app)
    assert entry.kind == "agent_message"
    assert entry.chat_agent_id == ""
    # Headed by the title, not the primary agent's own name; the message stands alone as the body.
    assert entry.title == "Inbox digest"
    assert entry.body == "3 new items"
    assert entry.workspace_agent_id == str(workspace_agent_id)
    assert entry.destination is not None
    assert entry.destination.model_dump(mode="json") == {"type": "minds:show-routine", "routine_id": "inbox-digest"}
    # Reading the sender's "chat" reads nothing: there is no chat to be read in.
    read = client.post(f"/api/v1/agents/{workspace_agent_id}/notifications/read", json={}, headers=_auth_header())
    assert read.status_code == 200
    (still_unread,) = _feed_entries(app)
    assert still_unread.is_resolved is False


def test_agent_notification_with_a_malformed_destination_is_rejected(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    response = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": "hi", "destination": {"type": "shell:open", "path": "/"}},
        headers=_auth_header(),
    )

    assert response.status_code == 422
    assert response.get_json()["errors"][0]["field"] == "destination"
    assert _feed_entries(app) == ()


def test_agent_notification_without_a_message_is_rejected(tmp_path: Path) -> None:
    workspace_agent_id = AgentId()
    chat_agent_id = AgentId()
    client, app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    response = client.post(
        f"/api/v1/agents/{chat_agent_id}/notifications",
        json={"message": ""},
        headers=_auth_header(),
    )

    assert response.status_code == 400
    assert _feed_entries(app) == ()


def test_list_workspaces_returns_known_workspaces(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get("/api/v1/workspaces", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    ids = [w["agent_id"] for w in body["workspaces"]]
    assert str(agent_id) in ids


def test_list_workspaces_reports_every_listed_workspace_as_the_users_own(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces", headers=_auth_header())

    assert response.status_code == 200
    workspaces = json.loads(response.data)["workspaces"]
    assert workspaces
    assert all(workspace["is_owned"] is True for workspace in workspaces)


def test_list_workspaces_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 401


def test_list_workspaces_accepts_session_cookie(tmp_path: Path) -> None:
    # The desktop UI calls the cross-workspace routes with its session cookie
    # (not the bearer), so dual auth must accept a valid signed session cookie.
    agent_id = AgentId()
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(auth_store.get_signing_key()))

    # No bearer header -- only the session cookie.
    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    assert str(agent_id) in [w["agent_id"] for w in json.loads(response.data)["workspaces"]]


def test_get_workspace_returns_detail(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["agent_id"] == str(agent_id)


def test_list_accounts_returns_signed_in_accounts(tmp_path: Path) -> None:
    # The accounts route lets a caller turn a known email into the account id the
    # association API needs. (At the gateway it is gated by the must-ask
    # ``minds-accounts-read`` permission; here we exercise the route directly.)
    cli = _fake_sharing_cli()
    cli.add_account(user_id="11111111-1111-1111-1111-111111111111", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _build_client(
        tmp_path,
        StaticBackendResolver(url_by_agent_and_service={}),
        imbue_cloud_cli=cli,
        session_store=store,
    )

    response = client.get("/api/v1/accounts", headers=_auth_header())

    assert response.status_code == 200
    accounts = json.loads(response.data)["accounts"]
    assert any(
        a["account_id"] == "11111111-1111-1111-1111-111111111111" and a["email"] == "owner@example.com"
        for a in accounts
    )


def test_list_accounts_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.get("/api/v1/accounts")

    assert response.status_code == 401


def test_app_version_reports_a_release_tag_a_workspace_can_cap_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's whole job: hand a workspace the ceiling on how far it may update.

    update-self drops the ceiling for anything that is not a
    ``minds-v<major>.<minor>.<patch>`` tag, so a shipped app reporting a plain
    branch name would leave every workspace uncapped. Hence the shape assertion
    rather than a literal version, and the cleared operator opt-in -- under ``just
    minds-start``, ``MINDS_WORKSPACE_BRANCH`` legitimately overrides the ref with a
    branch name.
    """
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.get("/api/v1/app/version", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["workspace_template_ref"] == default_workspace_template_ref()
    assert re.fullmatch(r"minds-v\d+\.\d+\.\d+", body["workspace_template_ref"]), (
        f"the shipped template pin must be a minds-v* release tag or workspaces lose their "
        f"update ceiling; got {body['workspace_template_ref']!r}"
    )


def test_app_version_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.get("/api/v1/app/version")

    assert response.status_code == 401


def test_get_workspace_surfaces_git_url_and_branch_from_labels(tmp_path: Path) -> None:
    # git_url and branch are sourced from the agent's ``remote`` / ``original_branch``
    # labels (the create-time repo URL/path and branch), so the detail readout
    # surfaces them instead of returning null.
    agent_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(
            agent_id,
            labels={
                "workspace": "mind-1",
                "is_primary": "true",
                "remote": "https://example/repo.git",
                "original_branch": "feature/my-branch",
            },
        ),
    )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )

    response = app.test_client().get(f"/api/v1/workspaces/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["git_url"] == "https://example/repo.git"
    assert body["branch"] == "feature/my-branch"


def test_get_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/{other_id}", headers=_auth_header())

    assert response.status_code == 404


def test_malformed_workspace_id_returns_400_not_500(tmp_path: Path) -> None:
    # A malformed id in the path (cannot parse as an AgentId) is a client error:
    # the blueprint maps InvalidRandomIdError to 400 rather than letting it 500.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces/not-a-valid-agent-id", headers=_auth_header())

    assert response.status_code == 400
    assert "error" in json.loads(response.data)


def test_workspace_version_returns_original_version_label(tmp_path: Path) -> None:
    # The static resolver has no labels, so original is null; the git-derived
    # fields default to null/[] because the recording caller returns empty
    # stdout, which parses to no current version and no upgrade merges.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/version", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["agent_id"] == str(agent_id)
    assert body["original_minds_version"] is None
    assert body["current_minds_version"] is None
    assert body["upgrade_merges"] == []


def test_workspace_backups_reports_unconfigured_as_an_ordinary_empty_listing(tmp_path: Path) -> None:
    # No restic.env was written for this workspace: not an error -- the route
    # returns an empty snapshot list and is_configured false. The service
    # verification lives on the separate backup-check route.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["is_configured"] is False
    assert body["snapshots"] == []
    assert body["snapshots_total"] == 0
    assert body["is_backing_up"] is False

    check_response = client.get(f"/api/v1/workspaces/{agent_id}/backup-check", headers=_auth_header())

    assert check_response.status_code == 200
    check_body = json.loads(check_response.data)
    assert check_body["check_state"] == "OFFLINE"
    assert check_body["is_verification_enabled"] is True
    assert check_body["update_target_version"].startswith("minds-v")


@pytest.mark.timeout(120)
def test_workspace_backups_lists_snapshots_newest_first(tmp_path: Path) -> None:
    # Against a real local restic repo with three snapshots: /backups returns
    # them newest-first so settings and the full-history page need not re-sort.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    repo = str(tmp_path / "repo")
    password = "workspace-key"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    write_canonical_env(
        InstallationPaths(data_dir=tmp_path / "minds"),
        agent_id,
        f"RESTIC_REPOSITORY={repo}\nRESTIC_PASSWORD={password}\n",
    )
    source = tmp_path / "data.txt"
    for i in range(3):
        source.write_text(f"content {i}")
        restic_backup_a_file(repo, password, source)

    body = json.loads(client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header()).data)
    assert body["is_configured"] is True
    assert len(body["snapshots"]) == 3
    assert body["snapshots_total"] == 3
    times = [s["time"] for s in body["snapshots"]]
    assert times == sorted(times, reverse=True)


@pytest.mark.timeout(120)
def test_workspace_backups_limit_and_offset_page_the_newest_first_window(tmp_path: Path) -> None:
    # limit/offset trim the serialized window while snapshots_total keeps the
    # full count, so the full-history page can page without re-listing.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    repo = str(tmp_path / "repo")
    password = "workspace-key"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    write_canonical_env(
        InstallationPaths(data_dir=tmp_path / "minds"),
        agent_id,
        f"RESTIC_REPOSITORY={repo}\nRESTIC_PASSWORD={password}\n",
    )
    source = tmp_path / "data.txt"
    for i in range(3):
        source.write_text(f"content {i}")
        restic_backup_a_file(repo, password, source)

    all_times = [
        s["time"]
        for s in json.loads(client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header()).data)[
            "snapshots"
        ]
    ]

    first_page = json.loads(client.get(f"/api/v1/workspaces/{agent_id}/backups?limit=2", headers=_auth_header()).data)
    assert first_page["snapshots_total"] == 3
    assert [s["time"] for s in first_page["snapshots"]] == all_times[:2]

    second_page = json.loads(
        client.get(f"/api/v1/workspaces/{agent_id}/backups?limit=2&offset=2", headers=_auth_header()).data
    )
    assert second_page["snapshots_total"] == 3
    assert [s["time"] for s in second_page["snapshots"]] == all_times[2:]

    # limit=0 keeps the count but sends no rows (the badge/landing surfaces).
    none_page = json.loads(client.get(f"/api/v1/workspaces/{agent_id}/backups?limit=0", headers=_auth_header()).data)
    assert none_page["snapshots"] == []
    assert none_page["snapshots_total"] == 3


def test_workspace_backups_rejects_a_negative_or_malformed_limit_and_offset(tmp_path: Path) -> None:
    # Flask's `type=int` silently swallows garbage as the default, which would
    # make `?limit=abc` mean "all snapshots"; the route must parse strictly
    # and 400 instead.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    for query in ("limit=-1", "offset=-1", "limit=abc", "offset=abc", "limit=1.5"):
        response = client.get(f"/api/v1/workspaces/{agent_id}/backups?{query}", headers=_auth_header())
        assert response.status_code == 400, query
        assert "non-negative integers" in json.loads(response.data)["error"]


def test_workspaces_backups_stream_yields_one_ndjson_line_per_agent(tmp_path: Path) -> None:
    # The landing page's single streaming request: one NDJSON summary per named
    # workspace. Unconfigured workspaces degrade to an empty snapshot list.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/backups?agent_id={agent_id}", headers=_auth_header())

    assert response.status_code == 200
    assert "ndjson" in response.headers.get("Content-Type", "")
    lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["agent_id"] == str(agent_id)
    assert summary["snapshots"] == []
    assert summary["is_backing_up"] is False


def test_workspaces_backups_stream_streams_a_line_per_requested_agent(tmp_path: Path) -> None:
    # Two requested ids -> two NDJSON lines (even ids discovery doesn't know still
    # get a line, so every rendered row resolves off "Checking...").
    agent_id = AgentId()
    other_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(
        f"/api/v1/workspaces/backups?agent_id={agent_id}&agent_id={other_id}", headers=_auth_header()
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
    assert {row["agent_id"] for row in lines} == {str(agent_id), str(other_id)}


def test_workspaces_backups_stream_route_is_not_shadowed_by_the_single_workspace_route(tmp_path: Path) -> None:
    # /workspaces/backups must reach the streaming batch endpoint, not the
    # /workspaces/<agent_id> route with agent_id="backups". With no agent_id
    # params the stream is empty NDJSON, not a single-workspace JSON object.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces/backups", headers=_auth_header())

    assert response.status_code == 200
    assert "ndjson" in response.headers.get("Content-Type", "")
    assert response.get_data(as_text=True).strip() == ""


def test_workspaces_backups_stream_requires_auth(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    response = client.get("/api/v1/workspaces/backups")
    assert response.status_code == 401


def test_workspaces_backups_stream_fans_out_over_the_concurrency_group(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # With a concurrency group present the endpoint lists each workspace on its
    # own bounded worker thread; every requested id still gets exactly one line.
    agent_id = AgentId()
    other_id = AgentId()
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
        root_concurrency_group=root_concurrency_group,
    )
    client = app.test_client()

    response = client.get(
        f"/api/v1/workspaces/backups?agent_id={agent_id}&agent_id={other_id}", headers=_auth_header()
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
    assert {row["agent_id"] for row in lines} == {str(agent_id), str(other_id)}


def test_workspaces_backups_stream_degrades_non_agent_ids_without_failing_the_batch(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # The workspace list also renders create-attempt rows, whose ids are not
    # agent ids. One of them in the batched request must not 400 the whole
    # stream (that flipped every landing badge to "Backup status unknown"):
    # the bad id gets its own degraded line and the real workspace still
    # resolves normally.
    agent_id = AgentId()
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
        root_concurrency_group=root_concurrency_group,
    )
    client = app.test_client()

    response = client.get(
        f"/api/v1/workspaces/backups?agent_id={agent_id}&agent_id={create_attempt_id}", headers=_auth_header()
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
    row_by_id = {row["agent_id"]: row for row in lines}
    assert set(row_by_id) == {str(agent_id), create_attempt_id}
    assert row_by_id[str(agent_id)]["error"] is None
    assert row_by_id[create_attempt_id]["error"] == "not a workspace agent id"


def test_workspaces_backups_stream_degrades_unresolved_rows_on_row_timeout() -> None:
    # A wedged worker must not silently end the stream: every requested row
    # still owed a line gets a degraded `error` summary, so the landing page
    # never leaves a badge stuck on "Checking..." (and never blanks it).
    resolved_id = AgentId()
    wedged_id = AgentId()
    result_queue: queue.Queue[dict[str, object]] = queue.Queue()
    result_queue.put(
        {"agent_id": str(resolved_id), "snapshots": [], "is_backing_up": False, "created_at": None, "error": None}
    )

    lines = [
        json.loads(line)
        for line in _drain_backup_summary_rows(
            result_queue,
            (str(resolved_id), str(wedged_id)),
            {str(resolved_id): None, str(wedged_id): "2026-07-01T00:00:00+00:00"},
            0.05,
        )
    ]

    assert [row["agent_id"] for row in lines] == [str(resolved_id), str(wedged_id)]
    assert lines[0]["error"] is None
    assert lines[1]["error"] == "backup status probe timed out"
    assert lines[1]["snapshots"] == []
    assert lines[1]["created_at"] == "2026-07-01T00:00:00+00:00"


def test_workspaces_backups_stream_emits_degraded_rows_when_workers_cannot_spawn(tmp_path: Path) -> None:
    # A concurrency group that refuses new threads (here: already exited) must
    # not kill the stream mid-generator: each requested agent still gets a
    # line, degraded with an error, instead of the response dying.
    agent_id = AgentId()
    other_id = AgentId()
    closed_group = ConcurrencyGroup(name=f"closed-{agent_id}")
    with closed_group:
        pass

    lines = [
        json.loads(line)
        for line in _stream_workspace_backup_summaries(
            InstallationPaths(data_dir=tmp_path / "minds"),
            (str(agent_id), str(other_id)),
            {},
            closed_group,
        )
    ]

    assert {row["agent_id"] for row in lines} == {str(agent_id), str(other_id)}
    for row in lines:
        assert row["snapshots"] == []
        assert row["error"] is not None
        assert "backup status probe failed" in row["error"]


def test_workspaces_backups_stream_without_a_concurrency_group_still_yields_one_line_per_agent(
    tmp_path: Path,
) -> None:
    # Minimal setups run the sequential fallback (no concurrency group); the
    # one-line-per-requested-agent guarantee must hold there too.
    agent_id = AgentId()
    other_id = AgentId()

    lines = [
        json.loads(line)
        for line in _stream_workspace_backup_summaries(
            InstallationPaths(data_dir=tmp_path / "minds"),
            (str(agent_id), str(other_id)),
            {str(agent_id): "2026-07-01T00:00:00+00:00", str(other_id): None},
            None,
        )
    ]

    assert [row["agent_id"] for row in lines] == [str(agent_id), str(other_id)]
    assert lines[0]["created_at"] == "2026-07-01T00:00:00+00:00"
    for row in lines:
        assert row["snapshots"] == []
        assert row["error"] is None


def test_workspaces_backups_stream_marks_probe_failures_with_an_error(tmp_path: Path) -> None:
    # A configured workspace whose repository cannot be listed must not stream
    # a clean empty summary (the badge would claim "No backups"); the line
    # carries the listing error so the badge shows "unknown" instead.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    write_canonical_env(
        InstallationPaths(data_dir=tmp_path / "minds"),
        agent_id,
        f"RESTIC_REPOSITORY={tmp_path / 'no-such-repo'}\nRESTIC_PASSWORD=workspace-key\n",
    )

    response = client.get(f"/api/v1/workspaces/backups?agent_id={agent_id}", headers=_auth_header())

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["agent_id"] == str(agent_id)
    assert lines[0]["snapshots"] == []
    assert lines[0]["error"] is not None


def test_create_workspace_without_agent_creator_returns_501(tmp_path: Path) -> None:
    # The default test client has no agent_creator wired, so create is unavailable.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 501


def test_create_workspace_imbue_cloud_without_any_account_returns_signup_redirect(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # IMBUE_CLOUD with no account selected AND no accounts existing at all is the
    # no-account backstop: the route returns a 400 carrying the sign-up redirect
    # target so the create page navigates there (mirrors the old form's 303).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    assert json.loads(response.data)["redirect_url"] == "/auth/signup?return_to=%2Fcreate"


def test_create_workspace_imbue_cloud_with_account_unselected_returns_field_error(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # IMBUE_CLOUD with no account selected but accounts that DO exist must ask the
    # user to pick one (a field error on account_id), not redirect to sign-up.
    cli = _fake_sharing_cli()
    cli.add_account(user_id="11111111-1111-1111-1111-111111111111", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, session_store=store)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    body = json.loads(response.data)
    assert body["field"] == "account_id"
    assert "redirect_url" not in body


def test_create_workspace_on_a_connection_requires_the_connections_owner_to_be_selected(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The selected account picks the providers on offer: a connection is usable only under its owner, and the
    create then carries that account (the association) and, for Modal, the region the form picked and the
    sandbox lifetime the request named."""
    owner_user_id = "11111111-1111-1111-1111-111111111111"
    cli = _fake_sharing_cli()
    cli.add_account(user_id=owner_user_id, email="owner@example.com")
    data_dir = tmp_path / "sessions"
    session_store = make_session_store_for_test(data_dir, cli=cli)
    connection_store = make_connection_store_for_test(data_dir, cli)
    paths = InstallationPaths(data_dir=data_dir)
    unlock_account_with_master_password_for_test(paths, owner_user_id)
    connection_id = add_modal_connection_for_test(connection_store, owner_user_id, "team modal")
    instance_name = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(
        tmp_path,
        root_concurrency_group,
        agent_creator=creator,
        session_store=session_store,
        connection_store=connection_store,
    )

    # No account selected: the local profile's providers are on offer, and this connection is not among them.
    refused = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "MODAL", "cloud_account": instance_name},
    )
    assert refused.status_code == 400
    assert json.loads(refused.data)["field"] == "cloud_account"

    accepted = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={
            "git_url": "https://example/repo",
            "launch_mode": "MODAL",
            "cloud_account": instance_name,
            "account_id": owner_user_id,
            "region": "eu-west",
            "modal_sandbox_timeout_seconds": 10710,
        },
    )
    assert accepted.status_code == 202, accepted.data
    assert creator._last_call is not None
    assert creator._last_call["cloud_account"] == instance_name
    assert creator._last_call["account_id"] == owner_user_id
    # Modal places per create, so the form's region rides through rather than a pin.
    assert creator._last_call["region"] == "eu-west"
    assert creator._last_call["modal_sandbox_timeout_seconds"] == 10710
    # The association seeds the record with the connection instance as its provider, not the account's
    # Imbue Cloud instance, and as an account-synced row (no hosting device).
    on_created = creator._last_call["on_created"]
    assert isinstance(on_created, CreateOnCreatedCallback) and on_created.base_callback is not None
    assert on_created.base_callback.provider_kind == instance_name
    assert on_created.base_callback.is_cloud_row is True


@pytest.mark.parametrize("selected_account_id", ["", "11111111-1111-1111-1111-111111111111"])
def test_create_workspace_on_a_local_profile_connection_is_accepted_under_any_account(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, selected_account_id: str
) -> None:
    """A this-device-only connection is a device provider, like docker: offered under every account and under none,
    and the selected account (if any) is still the workspace's association."""
    account_user_id = "11111111-1111-1111-1111-111111111111"
    cli = _fake_sharing_cli()
    cli.add_account(user_id=account_user_id, email="owner@example.com")
    data_dir = tmp_path / "sessions"
    session_store = make_session_store_for_test(data_dir, cli=cli)
    connection_store = make_connection_store_for_test(data_dir, cli)
    connection_id = add_modal_connection_for_test(connection_store, LOCAL_PROFILE_USER_ID, "device modal")
    instance_name = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(
        tmp_path,
        root_concurrency_group,
        agent_creator=creator,
        session_store=session_store,
        connection_store=connection_store,
    )

    accepted = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={
            "git_url": "https://example/repo",
            "launch_mode": "MODAL",
            "cloud_account": instance_name,
            "account_id": selected_account_id,
        },
    )

    assert accepted.status_code == 202, accepted.data
    assert creator._last_call is not None
    assert creator._last_call["cloud_account"] == instance_name
    assert creator._last_call["account_id"] == selected_account_id
    # A device provider's workspace is recorded as hosted here (not as an account-synced row).
    on_created = creator._last_call["on_created"]
    assert isinstance(on_created, CreateOnCreatedCallback)
    if selected_account_id:
        assert on_created.base_callback is not None
        assert on_created.base_callback.provider_kind == instance_name
        assert on_created.base_callback.is_cloud_row is False
    else:
        assert on_created.base_callback is None


def test_create_workspace_empty_git_url_returns_field_error(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A missing repository URL is a field-level validation error so the create
    # page can render the message inline next to the git_url input.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group)

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": ""})

    assert response.status_code == 400
    body = json.loads(response.data)
    assert body["field"] == "git_url"
    assert body["error"]


def test_create_workspace_invalid_host_name_returns_field_error(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A submitted name that normalizes to an empty slug (here all punctuation)
    # surfaces as a 400 keyed to the host_name field (rather than a deferred
    # FAILED on the creating page).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "!!!"},
    )

    assert response.status_code == 400
    assert json.loads(response.data)["field"] == "host_name"


def test_create_workspace_auto_names_next_workspace_when_host_name_omitted(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # With no host_name and ``workspace-1`` already known, the route resolves the
    # next free ``workspace-N`` (workspace-2) before handing off to the creator.
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"is_primary": "true"}, host_name="workspace-1"),
    )
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, resolver=resolver, agent_creator=creator)

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 202
    assert str(creator.last_call["host_name"]) == "workspace-2"


def test_create_operation_status_includes_status_text(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The create-operation status carries a human-readable status_text (the stage
    # caption the creating page renders), derived from status + launch_mode.
    create_attempt_id = CreateAttemptId()
    creator = _StatusReportingAgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreateAttemptInfo(
            create_attempt_id=create_attempt_id,
            status=AgentCreateAttemptStatus.INITIALIZING,
            launch_mode=LaunchMode.DOCKER,
        ),
    )
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["kind"] == "create"
    assert body["status_text"] == status_text_for(
        str(AgentCreateAttemptStatus.INITIALIZING), launch_mode=LaunchMode.DOCKER
    )
    assert body["status_text"]
    # An in-flight (non-failed) create attempt carries no failure classification.
    assert body["error_kind"] is None


def test_create_operation_status_carries_error_kind_for_classified_failures(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A failed create attempt whose error was classified (e.g. a private GitHub repo
    # the local git credentials cannot see) reports the machine-readable kind
    # alongside the error message; the creating page gates its static sign-in
    # guidance on it.
    create_attempt_id = CreateAttemptId()
    creator = _StatusReportingAgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreateAttemptInfo(
            create_attempt_id=create_attempt_id,
            status=AgentCreateAttemptStatus.FAILED,
            launch_mode=LaunchMode.DOCKER,
            error="git clone failed:\nfatal: could not read Username for 'https://github.com'",
            error_kind=CreateAttemptErrorKind.GITHUB_AUTH_REQUIRED,
        ),
    )
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["status"] == "FAILED"
    assert body["error"]
    assert body["error_kind"] == "GITHUB_AUTH_REQUIRED"


def test_create_workspace_full_surface_returns_202_and_threads_fields(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The full create field surface (color, explicit name, branch, ...) is
    # accepted: a 202 with an operation handle, and the fields are passed through
    # to the creator.
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={
            "git_url": "https://example/repo",
            "host_name": "my-mind",
            "branch": "main",
            "color": "#0b292b",
            "launch_mode": "DOCKER",
            # A stale ai_provider from an old client is silently ignored.
            "ai_provider": "SUBSCRIPTION",
            "backup_provider": "CONFIGURE_LATER",
            "runtime": "RUNSC",
        },
    )

    assert response.status_code == 202
    body = json.loads(response.data)
    assert body["kind"] == "create"
    assert body["operation_id"]
    assert str(creator.last_call["host_name"]) == "my-mind"
    assert str(creator.last_call["color"]) == "#0b292b"
    assert str(creator.last_call["branch"]) == "main"
    assert creator.last_call["docker_runtime"] == DockerRuntime.RUNSC


@pytest.mark.witnesses("machine-size.create-size-reaches-the-provider")
def test_create_workspace_threads_the_machine_size_the_mode_takes(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={
            "git_url": "https://example/repo",
            "launch_mode": "DOCKER",
            "cpu_count": 2,
            "memory_gb": 16,
            # A docker machine has no data disk to size; a stray value is dropped.
            "disk_gb": 500,
        },
    )

    assert response.status_code == 202
    assert creator.last_call["machine_size"] == MachineSizeSelection(cpu_count=2, memory_gb=16)


def test_create_workspace_passes_no_machine_size_for_a_mode_sized_another_way(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "VULTR", "cpu_count": 2, "memory_gb": 16},
    )

    assert response.status_code == 202
    assert creator.last_call["machine_size"] is None


def test_create_workspace_threads_the_modal_sandbox_timeout_only_for_a_modal_create(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    dropped = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "DOCKER", "modal_sandbox_timeout_seconds": 10710},
    )
    assert dropped.status_code == 202
    assert creator.last_call["modal_sandbox_timeout_seconds"] is None

    below_one = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "DOCKER", "modal_sandbox_timeout_seconds": 0},
    )
    assert below_one.status_code == 422

    # Modal's 24h cap, less the provider's shutdown buffer: a longer sandbox would fail in the background create.
    above_the_cap = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "MODAL", "modal_sandbox_timeout_seconds": 86311},
    )
    assert above_the_cap.status_code == 422


def test_create_workspace_rejects_a_machine_size_below_one(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "DOCKER", "cpu_count": 0},
    )

    assert response.status_code == 422


def test_timezone_returns_valid_iana_name_or_empty(tmp_path: Path) -> None:
    # The endpoint reports the machine's own timezone, so the value is
    # host-dependent: assert the contract (a loadable IANA name, or "" when
    # undeterminable) rather than a specific zone.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/timezone", headers=_auth_header())

    assert response.status_code == 200
    tz_name = json.loads(response.data)["timezone"]
    assert isinstance(tz_name, str)
    if tz_name:
        ZoneInfo(tz_name)


def test_timezone_requires_auth(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/timezone")

    assert response.status_code == 401


def test_create_workspace_ignores_stale_ai_provider_field(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The AI provider moved into the workspace's own sign-in modal. A stale
    # client still sending ai_provider (even a value that used to require an
    # API key) must be accepted -- the field is silently ignored.
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "ai_provider": "API_KEY"},
    )

    assert response.status_code == 202


def test_create_workspace_rejects_invalid_backup_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # A malformed backup_provider is a structural (enum) failure, so spectree
    # rejects it up front with the uniform 422 contract, before any background
    # create attempt is started.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "backup_provider": "NOT_A_PROVIDER"},
    )

    assert response.status_code == 422
    errors = json.loads(response.data)["errors"]
    assert any(error["field"] == "backup_provider" for error in errors)


def test_create_workspace_rejects_imbue_cloud_backup_without_account(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # imbue_cloud *backups* (independent of the compute/AI provider) need an
    # account; without one the shared backup-request builder rejects it with a
    # 400 that mentions the account, before any background create attempt starts.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "backup_provider": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    assert "account" in json.loads(response.data)["error"].lower()


def test_destroy_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.post(f"/api/v1/workspaces/{other_id}/destroy", headers=_auth_header())

    assert response.status_code == 404


class _ResolverOnAParseableHost(StaticBackendResolver):
    """Static resolver reporting a host id the lifecycle routes can parse.

    The interface default reports the ``"localhost"`` placeholder, which the
    destroy route refuses with a 409 before it gets anywhere near a teardown.
    """

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="host-" + "0" * 31 + "1")


def test_destroying_a_machine_keeps_the_app_from_starting_it_back_up(tmp_path: Path) -> None:
    """A destroy marks the machine, the same as a stop does.

    The interface dies within seconds of the destroy starting, and the probe
    loop reads that exactly as it reads a wedge -- so without the mark the
    unattended dispatch would run ``mngr start`` against a host being torn down.
    The mark goes on as an in-flight one, since the interface answers for the
    first seconds of a teardown and that 200 must not clear it.
    """
    agent_id = AgentId()
    tracker = SystemInterfaceHealthTracker()
    client = _build_client(
        tmp_path,
        _ResolverOnAParseableHost(url_by_agent_and_service={str(agent_id): {}}),
        mngr_binary=_write_fake_mngr(tmp_path / "bin"),
        system_interface_health_tracker=tracker,
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/destroy", headers=_auth_header())

    assert response.status_code == 202
    tracker.record_probe_success(agent_id)
    assert tracker.is_unattended_recovery_suppressed(agent_id) is True


@pytest.mark.witnesses("remote-compatibility.newer-records-read-only", partial="covers the destroy API gate only")
def test_destroy_refuses_a_record_written_by_a_newer_app_version(tmp_path: Path) -> None:
    """The destroy route 409s with the update remedy when the workspace's record is too new.

    Seeded through a pull (as a newer client's server row arrives in real
    life), since the local write paths themselves refuse too-new records.
    """
    agent_id = AgentId()
    email = "user@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.sync_records_by_email[email] = {
        "host-too-new": {
            "host_id": "host-too-new",
            "agent_id": str(agent_id),
            "provider_kind": "lima",
            "state": "active",
            "revision": 1,
            "record_format": 2,
        }
    }
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    assert store.record_store is not None
    assert store.record_store.pull("user-1", email) is not None
    client = _build_client(
        tmp_path,
        _ResolverOnAParseableHost(url_by_agent_and_service={str(agent_id): {}}),
        mngr_binary=_write_fake_mngr(tmp_path / "bin"),
        session_store=store,
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/destroy", headers=_auth_header())

    assert response.status_code == 409
    assert json.loads(response.data)["error"] == RECORD_TOO_NEW_MESSAGE


def test_lifecycle_without_concurrency_group_returns_501(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/start", headers=_auth_header())

    assert response.status_code == 501


def test_stop_workspace_broadcasts_workspace_stopped_event(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A successful v1 stop broadcasts a one-shot ``workspace_stopped`` frame on ``/ui/ws``.

    The Electron shell closes any window still open to the workspace off this
    frame, rather than leaving it on an interface that is going away. What keeps
    the app from starting the machine straight back up is a separate thing --
    the health-tracker mark ``perform_mind_host_action`` applies before it runs
    the stop -- so this frame must not be read as that guard. The landing-page
    stop shares this route, so both stop paths emit through the one mechanism.
    """
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=fake_mngr,
        mngr_host_dir=tmp_path / "host",
    )
    client_queue = get_state(client.application).ui_channel_broadcaster.register()

    response = client.post(f"/api/v1/workspaces/{agent_id}/stop", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(client_queue.get_nowait() or "") == {"type": "workspace_stopped", "agent_id": str(agent_id)}


def test_start_workspace_does_not_broadcast_workspace_stopped(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Only the STOP action emits ``workspace_stopped``; a start emits nothing."""
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=fake_mngr,
        mngr_host_dir=tmp_path / "host",
    )
    client_queue = get_state(client.application).ui_channel_broadcaster.register()

    response = client.post(f"/api/v1/workspaces/{agent_id}/start", headers=_auth_header())

    assert response.status_code == 200
    assert client_queue.empty()


def test_workspace_refresh_broadcasts_to_every_ui_connection(tmp_path: Path) -> None:
    """The refresh route reaches every open window, keyed to the path agent id.

    This is the whole mechanism: the agent has no other way to reach the shell,
    so a frame that does not land on each registered ``/ui/ws`` connection
    means a window keeps rendering the pre-change interface.
    """
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    broadcaster = get_state(client.application).ui_channel_broadcaster
    first_queue = broadcaster.register()
    second_queue = broadcaster.register()

    # Exactly what default-workspace-template's ``system/scripts/refresh_workspace_view.py``
    # puts on the wire: an empty JSON object with a JSON content type. The route
    # declares no request model, so this pins that an empty body is accepted
    # rather than rejected as invalid.
    response = client.post(f"/api/v1/agents/{agent_id}/refresh", json={}, headers=_auth_header())

    assert response.status_code == 200
    expected = {"type": "workspace_refresh", "agent_id": str(agent_id), "loaded_before": None}
    assert json.loads(first_queue.get_nowait() or "") == expected
    assert json.loads(second_queue.get_nowait() or "") == expected


def test_workspace_refresh_succeeds_with_no_window_open(tmp_path: Path) -> None:
    """A closed workspace still answers ``ok``: the refresh is fire-and-forget.

    The caller is an agent mid-``update-self`` / ``update-app``; failing it
    because the user happens to have the window shut would turn a cosmetic
    no-op into a failed reveal.
    """
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/agents/{agent_id}/refresh", headers=_auth_header())

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def _drain_refresh_frames(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every ``workspace_refresh`` frame waiting on one ``/ui/ws`` connection.

    Filtered by type because the tracker transitions these tests drive also
    publish health frames onto the same channel.
    """
    return [frame for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "workspace_refresh"]


def test_a_machine_coming_back_tells_every_window_to_rebuild_its_view(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A recovered machine broadcasts the same refresh an in-workspace agent can ask for.

    While the machine was down every window went on painting whatever it served
    -- an error page, or a half-loaded one -- and nothing about the recovery
    changes the content frame's URL, so that dead page would sit there until the
    user navigated away and back. The unattended start usually succeeds
    without ever raising a recovery card, so the card cannot be what covers this.
    """
    agent_id = AgentId()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    client = _build_client(
        tmp_path,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=tracker,
    )
    window_queue = get_state(client.application).ui_channel_broadcaster.register()

    # The app this client is (one with a concurrency group) also runs the
    # unattended dispatch off the same stuck edge, and its start would race
    # this test to the machine's health. Marked as deliberately stopped, which
    # is what production uses to keep that dispatch off a machine; the probe
    # success below clears the mark as it fires the recovery.
    tracker.suppress_unattended_recovery(agent_id)
    # An outage, then the machine answering again -- the only thing that ends one.
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.STUCK
    tracker.record_probe_success(agent_id)

    assert _drain_refresh_frames(window_queue) == [
        {"type": "workspace_refresh", "agent_id": str(agent_id), "loaded_before": None}
    ]


def test_a_machine_that_never_went_down_does_not_refresh_windows(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Only the recovery edge refreshes: a probe success on a healthy machine is every probe.

    The probe loop reports success continuously for the machines it watches, so
    a refresh keyed on the probe rather than the edge would re-navigate every
    window every couple of seconds. The lap that has to stay quiet is the one
    where the tracker still holds a clean record (enrolled by a failure
    envelope, still HEALTHY); after that record is dropped there is nothing to
    compare and the lap is quiet either way.
    """
    agent_id = AgentId()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    client = _build_client(
        tmp_path,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=tracker,
    )
    window_queue = get_state(client.application).ui_channel_broadcaster.register()

    tracker.record_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.HEALTHY
    tracker.record_probe_success(agent_id)
    tracker.record_probe_success(agent_id)

    assert _drain_refresh_frames(window_queue) == []


def test_operation_status_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    create_attempt_id = CreateAttemptId()

    response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}", headers=_auth_header())

    assert response.status_code == 404


def test_operation_status_unknown_destroy_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/operations/destroy/{other_id}", headers=_auth_header())

    assert response.status_code == 404


def test_establish_ssh_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.post(
        f"/api/v1/workspaces/{other_id}/ssh",
        headers=_auth_header(),
        json={"public_key": "ssh-ed25519 AAAA", "requester_workspace_id": "agent-x"},
    )

    assert response.status_code == 404


def test_establish_ssh_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/ssh", json={})

    # Auth runs before validation, so an unauthenticated request with an invalid
    # body is rejected with 401 -- never a pre-auth 422 (which would leak that the
    # route exists and echo input back).
    assert response.status_code == 401


def test_establish_ssh_missing_fields_returns_422_with_field_errors(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    # An authenticated request with a structurally-invalid body (required fields
    # absent) gets the uniform 422 contract: one {field, message} per failure.
    response = client.post(f"/api/v1/workspaces/{agent_id}/ssh", headers=_auth_header(), json={})

    assert response.status_code == 422
    errors = json.loads(response.data)["errors"]
    failed_fields = {error["field"] for error in errors}
    assert failed_fields == {"public_key", "requester_workspace_id"}
    assert all(error["message"] for error in errors)


def _write_recording_fake_mngr(directory: Path, record_path: Path, changed_write_count: int = 0) -> str:
    """Fake ``mngr`` that records each invocation's argv, then exits 0.

    Args are NUL-delimited within an invocation and invocations are separated by
    a record-separator byte (0x1e), so args that legitimately contain newlines
    (the authorized_keys write script) round-trip intact.

    When invoked with ``--format json`` (the authorized_keys read), it emits a
    realistic ``mngr exec --format json`` envelope on stdout whose inner
    ``stdout`` is empty. This mirrors real ``mngr``: its default (human) output
    appends a ``Command succeeded on agent ...`` status line to stdout that the
    route must not write back, so the route reads in JSON and extracts the inner
    body. Other invocations (the write, which discards stdout) emit nothing.

    The first ``changed_write_count`` invocations that are not the read fail the
    way the guarded write script does when the file no longer matches the read
    it was composed from: exit 75 with ``AUTHORIZED_KEYS_CHANGED_SINCE_READ`` on
    stderr, which the envelope carries with ``success: false``. Every later
    write succeeds, so the route's re-read lands.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "mngr"
    rec = shlex.quote(str(record_path))
    changed_writes_seen = shlex.quote(str(directory / "changed_writes_seen"))
    envelope = json.dumps(
        {
            "results": [{"agent": "t", "stdout": "", "stderr": "", "success": True}],
            "failed_agents": [],
            "total_executed": 1,
            "total_failed": 0,
        }
    )
    # A command that exits non-zero stays in ``results`` with its stderr; ``failed_agents`` is for an exec that never ran.
    changed_envelope = json.dumps(
        {
            "results": [
                {
                    "agent": "t",
                    "stdout": "",
                    "stderr": f"{workspace_ssh.AUTHORIZED_KEYS_CHANGED_MARKER}\n",
                    "success": False,
                }
            ],
            "failed_agents": [],
            "total_executed": 1,
            "total_failed": 0,
        }
    )
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do printf \'%s\\0\' "$a" >> {rec}; done\n'
        f"printf '\\036' >> {rec}\n"
        'case "$3" in\n'
        "  cat*) ;;\n"
        "  *)\n"
        f"    seen=$(cat {changed_writes_seen} 2>/dev/null || echo 0)\n"
        f'    if [ "$seen" -lt {changed_write_count} ]; then\n'
        f"      echo $((seen + 1)) > {changed_writes_seen}\n"
        f"      printf '%s' {shlex.quote(changed_envelope)}\n"
        "      exit 1\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "json" ]; then\n'
        f"    printf '%s' {shlex.quote(envelope)}\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return str(script)


def _recorded_mngr_invocations(record_path: Path) -> list[list[str]]:
    """Parse the file written by ``_write_recording_fake_mngr`` into a list of argvs."""
    raw = record_path.read_bytes().decode()
    return [[arg for arg in inv.split("\0") if arg != ""] for inv in raw.split("\x1e") if inv != ""]


def test_establish_ssh_passes_command_as_single_mngr_exec_arg(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The authorized_keys read/write must use mngr exec's single trailing COMMAND
    form (``mngr exec <agent> <command>``), never ``-- bash -c <script>``.

    ``mngr exec``'s CLI is ``mngr exec [AGENTS]... COMMAND``, so passing
    ``-- bash -c <script>`` makes ``bash``/``-c`` parse as extra agent names and
    the call errors on ``-c`` -- which 502'd the whole SSH grant. A routable
    (remote) target is used so the route returns a direct connection and the only
    mngr work is the read + write we are guarding.
    """
    target = AgentId()
    resolver = StaticBackendResolver(
        # makes the target a known workspace
        url_by_agent_and_service={str(target): {}},
        ssh_info_by_agent_id={
            str(target): RemoteSSHInfo(user="root", host="ssh.example.com", port=2222, key_path=Path("/k"))
        },
    )
    record_path = tmp_path / "mngr_argv.bin"
    fake_mngr = _write_recording_fake_mngr(tmp_path / "bin", record_path)
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=fake_mngr)

    response = client.post(
        f"/api/v1/workspaces/{target}/ssh",
        headers=_auth_header(),
        json={
            "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5TESTKEYMATERIAL",
            "requester_workspace_id": str(AgentId()),
        },
    )
    assert response.status_code == 200, response.data

    invocations = _recorded_mngr_invocations(record_path)
    # Exactly the read then the write.
    assert len(invocations) == 2, invocations
    read_argv, write_argv = invocations
    # Read: `mngr exec <id> <cat command> --format json` -- the command is a
    # single COMMAND arg (never `-- bash -c <script>`), and JSON format keeps the
    # captured body to the command's own stdout (no human status line to write
    # back).
    assert read_argv[0] == "exec"
    assert read_argv[1] == str(target)
    assert read_argv[2] == "cat ~/.ssh/authorized_keys 2>/dev/null || true"
    assert read_argv[3:] == ["--format", "json"]
    assert "bash" not in read_argv and "-c" not in read_argv and "-lc" not in read_argv and "--" not in read_argv
    # Write: `mngr exec <id> <write script> --format json` -- like the read, the
    # command is a single COMMAND arg, and the only thing after it is the format
    # flag pair. Anything else positional here would be parsed as another agent
    # name; JSON format is what puts a failure somewhere the route can report it.
    assert write_argv[0] == "exec"
    assert write_argv[1] == str(target)
    assert write_argv[3:] == ["--format", "json"], f"write command must be a single arg, got {write_argv!r}"
    assert "bash" not in write_argv and "-c" not in write_argv and "-lc" not in write_argv and "--" not in write_argv
    # The write body is composed only from the parsed inner stdout: neither the
    # JSON envelope text nor any human-format status line may reach the file.
    write_script = write_argv[2]
    assert "authorized_keys" in write_script
    assert "Command succeeded" not in write_script
    assert '"results"' not in write_script


def test_establish_ssh_rereads_authorized_keys_when_the_guarded_write_finds_it_changed(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The write holds the file's lock and only lands on the body it was composed from; otherwise read again."""
    target = AgentId()
    resolver = StaticBackendResolver(
        url_by_agent_and_service={str(target): {}},
        ssh_info_by_agent_id={
            str(target): RemoteSSHInfo(user="root", host="ssh.example.com", port=2222, key_path=Path("/k"))
        },
    )
    record_path = tmp_path / "mngr_argv.bin"
    fake_mngr = _write_recording_fake_mngr(tmp_path / "bin", record_path, changed_write_count=1)
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=fake_mngr)

    response = client.post(
        f"/api/v1/workspaces/{target}/ssh",
        headers=_auth_header(),
        json={
            "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5TESTKEYMATERIAL",
            "requester_workspace_id": str(AgentId()),
        },
    )
    assert response.status_code == 200, response.data

    commands = [invocation[2] for invocation in _recorded_mngr_invocations(record_path)]
    # read, write (refused: the file changed), read again, write (landed)
    assert [command.startswith("cat ") for command in commands] == [True, False, True, False]
    assert all(f"flock {workspace_ssh.AUTHORIZED_KEYS_LOCK_PATH} sh -c" in command for command in commands[1::2])


def test_establish_ssh_gives_up_when_authorized_keys_keeps_changing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Every write finds the file changed: the route re-reads a bounded number of times, then answers 502."""
    target = AgentId()
    resolver = StaticBackendResolver(
        url_by_agent_and_service={str(target): {}},
        ssh_info_by_agent_id={
            str(target): RemoteSSHInfo(user="root", host="ssh.example.com", port=2222, key_path=Path("/k"))
        },
    )
    record_path = tmp_path / "mngr_argv.bin"
    fake_mngr = _write_recording_fake_mngr(
        tmp_path / "bin", record_path, changed_write_count=_SSH_GRANT_WRITE_ATTEMPTS + 1
    )
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=fake_mngr)

    response = client.post(
        f"/api/v1/workspaces/{target}/ssh",
        headers=_auth_header(),
        json={
            "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5TESTKEYMATERIAL",
            "requester_workspace_id": str(AgentId()),
        },
    )
    assert response.status_code == 502, response.data
    assert "kept changing" in json.loads(response.data)["error"]

    commands = [invocation[2] for invocation in _recorded_mngr_invocations(record_path)]
    assert [command.startswith("cat ") for command in commands] == [True, False] * _SSH_GRANT_WRITE_ATTEMPTS


def test_operation_logs_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    create_attempt_id = CreateAttemptId()

    response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_unknown_destroy_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/operations/destroy/{other_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get(f"/api/v1/workspaces/operations/create/{CreateAttemptId()}/logs")

    assert response.status_code == 401


def _build_client(
    tmp_path: Path,
    resolver: BackendResolverInterface,
    *,
    root_concurrency_group: ConcurrencyGroup | None = None,
    mngr_binary: str = "mngr",
    mngr_host_dir: Path | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    session_store: MultiAccountSessionStore | None = None,
    http_client: httpx2.Client | None = None,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = None,
    client_env_config: ClientEnvConfig | None = None,
    connection_store: ConnectionStore | None = None,
    provider_materializer: ProviderSettingsMaterializer | None = None,
    minds_config: MindsConfig | None = None,
    mngr_caller: MngrCaller | None = None,
    agent_creator: AgentCreator | None = None,
) -> FlaskClient:
    """Build a desktop-client test client with the /api/v1 surface and the given deps."""
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=http_client,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_config=minds_config,
        minds_api_key=_TEST_KEY,
        root_concurrency_group=root_concurrency_group,
        agent_creator=agent_creator,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir,
        imbue_cloud_cli=imbue_cloud_cli,
        session_store=session_store,
        connection_store=connection_store,
        provider_materializer=provider_materializer,
        system_interface_health_tracker=system_interface_health_tracker,
        mngr_caller=mngr_caller if mngr_caller is not None else RecordingMngrCaller(),
        client_env_config=client_env_config,
    )
    return app.test_client()


def _write_fake_mngr(directory: Path, exit_code: int = 0) -> str:
    """Write an executable fake ``mngr`` that always exits ``exit_code``; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "mngr"
    script.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    script.chmod(0o755)
    return str(script)


class FakeSharingCli(FakeImbueCloudCli):
    """In-memory ``ImbueCloudCli`` double for the machine-sharing routes.

    Returns canned share data and records mutating calls, so the sharing
    status/enable/disable routes can be exercised without shelling out to
    ``mngr imbue_cloud``.
    """

    share: ShareCliInfo | None = None
    created_shares: list[str] = Field(default_factory=list)
    deleted_shares: list[str] = Field(default_factory=list)
    share_status_call_count: int = Field(default=0, description="How many times get_share_status was consulted")

    def get_share_status(self, *, account: str, host_id: str) -> ShareCliInfo | None:
        self.share_status_call_count += 1
        if self.share is None:
            return None
        # The status document never carries the relay token (only create does).
        return ShareCliInfo(
            host_id=self.share.host_id,
            workspace_domain=self.share.workspace_domain,
            region=self.share.region,
            state=self.share.state,
            relay_endpoints=self.share.relay_endpoints,
            relay_token=None,
            last_tunnel_login_at=self.share.last_tunnel_login_at,
            cert_not_after=self.share.cert_not_after,
            needs_reshare=self.share.needs_reshare,
        )

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        self._raise_create_share_error_if_configured()
        self.created_shares.append(host_id)
        self.share = ShareCliInfo(
            host_id=host_id,
            workspace_domain=f"{host_id}.owner1234.us1.shares.example",
            region="us1",
            state="active",
            relay_endpoints=TEST_RELAY_ENDPOINTS,
            relay_token=SecretStr("relay-token-abc"),
        )
        return self.share

    def delete_share(self, *, account: str, host_id: str) -> None:
        self.deleted_shares.append(host_id)
        if self.share is not None:
            self.share = ShareCliInfo(
                host_id=self.share.host_id,
                workspace_domain=self.share.workspace_domain,
                region=self.share.region,
                state="inactive",
                relay_endpoints=self.share.relay_endpoints,
                relay_token=self.share.relay_token,
                last_tunnel_login_at=self.share.last_tunnel_login_at,
                cert_not_after=self.share.cert_not_after,
            )


def _associated_session_store(
    tmp_path: Path, cli: FakeSharingCli, agent_id: AgentId, *, user_id: str, email: str
) -> MultiAccountSessionStore:
    """Build a session store with one signed-in account that owns ``agent_id``."""
    cli.add_account(user_id=user_id, email=email)
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    store.associate_created_workspace(
        user_id=user_id,
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="",
        color=None,
        is_cloud_row=False,
    )
    return store


def test_patch_workspace_color_success(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=fake_mngr)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "#fff"})

    assert response.status_code == 200
    assert json.loads(response.data)["color"] == "#ffffff"
    # The optimistic local update is reflected in the resolver snapshot.
    assert resolver.get_workspace_color(agent_id) == "#ffffff"


def test_patch_workspace_color_shows_the_pick_before_the_label_write_and_reverts_when_it_fails(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id, labels={"is_primary": "true", "color": "#0b292b"}))
    colors_at_each_change: list[str | None] = []
    resolver.add_on_change_callback(lambda: colors_at_each_change.append(resolver.get_workspace_color(agent_id)))
    failing_mngr = _write_fake_mngr(tmp_path / "bin", exit_code=1)
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=failing_mngr)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "#abcdef"})

    assert response.status_code == 502
    assert colors_at_each_change == ["#abcdef", "#0b292b"]
    assert resolver.get_workspace_color(agent_id) == "#0b292b"


def test_patch_workspace_color_invalid_hex(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "not-a-color"})

    assert response.status_code == 400
    assert json.loads(response.data)["error"] == "invalid_hex"


def test_patch_workspace_color_not_primary(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.patch(f"/api/v1/workspaces/{other_id}", headers=_auth_header(), json={"color": "#abcdef"})

    assert response.status_code == 404
    assert json.loads(response.data)["error"] == "not_primary"


def test_patch_workspace_color_host_unreachable_without_concurrency_group(tmp_path: Path) -> None:
    # A known workspace with no concurrency group wired cannot run mngr label.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "#abcdef"})

    assert response.status_code == 502
    assert json.loads(response.data)["error"] == "host_unreachable"


def test_patch_workspace_associate_account(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "11111111-1111-1111-1111-111111111111"
    cli.add_account(user_id=user_id, email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": user_id})

    assert response.status_code == 200
    assert json.loads(response.data)["account_id"] == user_id
    account = store.get_account_for_workspace(str(agent_id))
    assert account is not None and str(account.email) == "owner@example.com"


def test_patch_workspace_disassociate_account_with_null(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "22222222-2222-2222-2222-222222222222"
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email="owner@example.com")
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": None})

    assert response.status_code == 200
    assert json.loads(response.data)["account_id"] is None
    assert store.get_account_for_workspace(str(agent_id)) is None


def test_patch_workspace_associate_account_by_email(tmp_path: Path) -> None:
    # Associating by email (not just id) resolves to the signed-in account and
    # echoes the canonical id + email back -- this is what unblocks an agent that
    # only knows the user's email.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "33333333-3333-3333-3333-333333333333"
    cli.add_account(user_id=user_id, email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(
        f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": "owner@example.com"}
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["account_id"] == user_id
    assert body["account_email"] == "owner@example.com"
    account = store.get_account_for_workspace(str(agent_id))
    assert account is not None and str(account.user_id) == user_id


def test_patch_workspace_associate_unknown_account_returns_404(tmp_path: Path) -> None:
    # A value matching no signed-in account is rejected (404) instead of being
    # silently accepted then garbage-collected.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    cli.add_account(user_id="44444444-4444-4444-4444-444444444444", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(
        f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": "nobody@example.com"}
    )

    assert response.status_code == 404
    assert store.get_account_for_workspace(str(agent_id)) is None


def test_get_workspace_surfaces_associated_account(tmp_path: Path) -> None:
    # After association the detail readout exposes account_id + account_email so a
    # caller can confirm it.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "55555555-5555-5555-5555-555555555555"
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email="owner@example.com")
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.get(f"/api/v1/workspaces/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["account_id"] == user_id
    assert body["account_email"] == "owner@example.com"


def test_patch_workspace_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", json={"color": "#fff"})

    assert response.status_code == 401


class _LeasedImbueCloudResolver(StaticBackendResolver):
    """Static resolver reporting every known agent as living on a leased imbue_cloud provider."""

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(
            agent_name=str(agent_id),
            host_id="host-leased",
            provider_name="imbue_cloud_alice-imbue-com",
        )


def test_patch_workspace_associate_leased_host_returns_403(tmp_path: Path) -> None:
    # A host leased from imbue_cloud is permanently bound to its leasing account,
    # so re-associating it to another account is rejected (the defense-in-depth
    # backstop to the disabled UI control).
    agent_id = AgentId()
    client = _build_client(tmp_path, _LeasedImbueCloudResolver(url_by_agent_and_service={}))

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": "user-123"})

    assert response.status_code == 403
    assert "account-owned provider" in json.loads(response.data)["error"]


def test_patch_workspace_disassociate_leased_host_returns_403(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _build_client(tmp_path, _LeasedImbueCloudResolver(url_by_agent_and_service={}))

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": None})

    assert response.status_code == 403
    assert "account-owned provider" in json.loads(response.data)["error"]


def test_dismiss_destroy_operation_is_idempotent_noop(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/destroy/{AgentId()}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {}


def test_dismiss_operation_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/destroy/{AgentId()}")

    assert response.status_code == 401


# Desktop running-workspaces / stop-hosts / state-container


def test_desktop_running_workspaces(tmp_path: Path) -> None:
    # The lone "local"-provider workspace is not on a shutdown-capable backend,
    # so no workspaces are reported as running, but the route returns the shape.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.get("/api/v1/desktop/running-workspaces", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"running": []}


def test_desktop_stop_hosts_without_concurrency_group_returns_503(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.post("/api/v1/desktop/stop-hosts", headers=_auth_header())

    assert response.status_code == 503


def test_desktop_stop_hosts_returns_still_running(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    # No system-services sibling is resolvable for the lone workspace, so nothing
    # is stopped and the (empty) still-running set is returned.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/desktop/stop-hosts?agent_id={agent_id}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"still_running": []}


def test_desktop_stop_state_container_without_concurrency_group(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.post("/api/v1/desktop/state-container/stop", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"stopped": False}


def test_desktop_stop_state_container_no_profile_reports_not_stopped(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # With a mngr host dir that has no profile, no container can be resolved, so
    # the stop is a no-op (stopped=False) and never touches Docker.
    client = _build_client(
        tmp_path,
        StaticBackendResolver(url_by_agent_and_service={}),
        root_concurrency_group=root_concurrency_group,
        mngr_host_dir=tmp_path / "empty-host",
    )

    response = client.post("/api/v1/desktop/state-container/stop", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"stopped": False}


def test_desktop_running_workspaces_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.get("/api/v1/desktop/running-workspaces")

    assert response.status_code == 401


_TEST_HOST_ID = "host-00000000000000000000000000000000"
_TEST_CLIENT_ENV_CONFIG = ClientEnvConfig(
    connector_url=AnyUrl("https://connector.shares.example"),
    litellm_proxy_url=AnyUrl("https://llm.shares.example"),
)


def _sharing_client(
    tmp_path: Path,
    agent_id: AgentId,
    cli: FakeSharingCli,
    *,
    user_id: str = "33333333-3333-3333-3333-333333333333",
    email: str = "owner@example.com",
    service_logs: dict[str, str] | None = None,
    mngr_binary: str = "mngr",
    http_client: httpx2.Client | None = None,
    agent_labels: dict[str, str] | None = None,
) -> FlaskClient:
    resolver = make_resolver_with_data(make_agents_json(agent_id, labels=agent_labels), service_logs=service_logs)
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email=email)
    return _build_client(
        tmp_path,
        resolver,
        imbue_cloud_cli=cli,
        session_store=store,
        mngr_binary=mngr_binary,
        http_client=http_client,
        client_env_config=_TEST_CLIENT_ENV_CONFIG,
    )


def _fake_sharing_cli(**kwargs: Any) -> FakeSharingCli:
    return FakeSharingCli(connector_url=FAKE_CONNECTOR_URL, **kwargs)


def _recorded_mngr_calls(cli: FakeSharingCli) -> list[list[str]]:
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    return caller.calls


def _recorded_exec_commands(cli: FakeSharingCli) -> list[str]:
    """The shell command of every ``mngr exec`` the routes issued, in order."""
    return [call[2] for call in _recorded_mngr_calls(cli) if call and call[0] == "exec"]


# The readiness response's gateway fields when the workspace reported nothing.
_NO_GATEWAY_STATUS: dict[str, object] = {
    "gateway_state": None,
    "gateway_error": None,
    "gateway_failed_attempt_count": None,
    "gateway_next_retry_at": None,
}


class _GatewayStatusCaller(RecordingMngrCaller):
    """Recording caller answering the readiness poll's gateway-status read with a canned document."""

    status_json: str = Field(default="", description="The status.json content; '' plays the absent-file case")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
        if any("share_gateway/status.json" in part for part in argv):
            envelope = {"results": [{"agent": argv[1], "stdout": self.status_json, "stderr": "", "success": True}]}
            return MngrCallResult(returncode=0, stdout=json.dumps(envelope))
        return result


def _active_share(host_id: str = _TEST_HOST_ID) -> ShareCliInfo:
    return ShareCliInfo(
        host_id=host_id,
        workspace_domain=f"{host_id}.owner1234.us1.shares.example",
        region="us1",
        state="active",
        relay_endpoints=TEST_RELAY_ENDPOINTS,
    )


def test_machine_sharing_status_disabled_when_no_share(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _sharing_client(tmp_path, agent_id, _fake_sharing_cli())

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is False
    assert body["url"] is None


def test_machine_sharing_status_lists_the_grants_of_an_unpublished_machine(tmp_path: Path) -> None:
    agent_id = AgentId()
    grants_toml = '[workspace]\nemails = ["viewer@example.com"]\nemail_domains = ["partner.org"]\n'
    cli = _fake_sharing_cli(mngr_caller=_GrantsReadCaller(grants_stdout=grants_toml))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is False
    assert body["url"] is None
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert body["grants"]["workspace"]["email_domains"] == ["partner.org"]


def test_machine_sharing_status_enabled(tmp_path: Path) -> None:
    agent_id = AgentId()
    grants_toml = '[workspace]\nemails = ["viewer@example.com"]\nemail_domains = []\n'
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_GrantsReadCaller(grants_stdout=grants_toml))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["workspace_domain"] == f"{_TEST_HOST_ID}.owner1234.us1.shares.example"
    assert body["url"] == f"https://{_TEST_HOST_ID}.owner1234.us1.shares.example/"
    # The grants document survives the exec envelope round trip.
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]


def test_machine_sharing_status_repairs_a_share_left_on_a_retired_content_domain(tmp_path: Path) -> None:
    # Opening the share panel is what moves a share the connector flags
    # needs_reshare: the read re-shares (the connector mints the new domain),
    # rewrites share.env only, and reports the old domain once so the panel
    # can say the address moved.
    agent_id = AgentId()
    stale_domain = f"{_TEST_HOST_ID}.owner1234.us1.retired.example"
    grants_toml = '[workspace]\nemails = ["viewer@example.com"]\nemail_domains = []\n'
    cli = _fake_sharing_cli(
        share=ShareCliInfo(
            host_id=_TEST_HOST_ID,
            workspace_domain=stale_domain,
            region="us1",
            state="active",
            relay_endpoints=TEST_RELAY_ENDPOINTS,
            needs_reshare=True,
        ),
        mngr_caller=_ShareProbeCaller(is_share_env_present=True, grants_stdout=grants_toml),
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["migrated_domain_from"] == stale_domain
    assert body["workspace_domain"] == f"{_TEST_HOST_ID}.owner1234.us1.shares.example"
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert cli.created_shares == [_TEST_HOST_ID]
    exec_commands = [call[2] for call in _recorded_mngr_calls(cli) if call and call[0] == "exec"]
    assert len(exec_commands) == 2
    assert "data/.secrets/share.env" in exec_commands[1]
    assert "share_grants.toml" not in exec_commands[1]
    # The moved share is current now: the next read is a plain status read.
    again = json.loads(client.get(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header()).data)
    assert again["migrated_domain_from"] is None
    assert cli.created_shares == [_TEST_HOST_ID]


def test_machine_sharing_status_reports_a_failed_domain_move_as_an_error(tmp_path: Path) -> None:
    agent_id = AgentId()
    stale_share = ShareCliInfo(
        host_id=_TEST_HOST_ID,
        workspace_domain=f"{_TEST_HOST_ID}.owner1234.us1.retired.example",
        region="us1",
        state="active",
        relay_endpoints=TEST_RELAY_ENDPOINTS,
        needs_reshare=True,
    )
    cli = _fake_sharing_cli(
        share=stale_share,
        mngr_caller=_ShareProbeCaller(is_share_env_present=True),
        create_share_error_to_raise=ImbueCloudCliError("connector refused"),
    )
    client = _sharing_client(tmp_path, agent_id, cli)
    # The readiness poll's cached lookup from before the move was attempted.
    state = get_state(client.application)
    state.active_share_cache.put(_TEST_HOST_ID, stale_share)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 502
    assert "connector refused" in json.loads(response.data)["error"]
    # The connector create may have moved the share before the failure, so
    # the poll must not keep serving the old domain from its cache.
    assert state.active_share_cache.get(_TEST_HOST_ID) is None


def test_machine_sharing_status_reports_unknown_grants_when_the_read_fails(tmp_path: Path) -> None:
    # A shared machine whose grants exec never lands must report grants: null
    # (unknown), not an empty policy: the pane would otherwise render every
    # grantee as revoked and let an edit replace a policy nobody ever saw.
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share())
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = MngrCallResult(returncode=1, stderr="agent offline")
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["grants"] is None


class _ShareProbeCaller(RecordingMngrCaller):
    """Recording caller answering the publish flow's one-exec share state probe.

    The probe command is recognized by its marker prefix; every other call
    (the write exec) keeps the canned default result. ``grants_stdout`` is the
    current grants document ('' plays the absent-document case). A write that
    lands or removes share.env moves the probe's answer with it, as the real
    workspace does, so a flow that publishes and then reads again sees what it
    just wrote.
    """

    is_gateway_present: bool = Field(default=True, description="Whether the probe reports the share gateway")
    is_share_env_present: bool = Field(default=False, description="Whether the probe reports share.env present")
    grants_stdout: str = Field(default="", description="The current grants document; '' means absent")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
        if any("MNGR_SHARE_GATEWAY" in part for part in argv):
            return make_share_probe_result(
                is_gateway_present=self.is_gateway_present,
                is_share_env_present=self.is_share_env_present,
                grants_toml_text=self.grants_stdout or None,
            )
        command = " ".join(argv)
        if "data/.secrets/share.env" in command:
            self.is_share_env_present = "rm -f" not in command
        return result


class _GrantsReadCaller(RecordingMngrCaller):
    """Recording caller that answers the grants read with a proper exec JSON envelope.

    The real read unwraps ``mngr exec --format json`` output, so a canned raw
    string cannot stand in for it; ``grants_stdout`` is the remote ``cat``'s
    own output ('' plays the absent-file case, unparseable text a corrupted
    document). Every other call keeps the canned default result.
    """

    grants_stdout: str = Field(default="", description="The remote cat's stdout inside the envelope")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
        if any("cat" in part and "share_grants.toml" in part for part in argv):
            envelope = {"results": [{"agent": "a", "stdout": self.grants_stdout, "stderr": "", "success": True}]}
            return MngrCallResult(returncode=0, stdout=json.dumps(envelope))
        return result


def test_machine_sharing_status_reports_a_malformed_document_as_an_empty_one(tmp_path: Path) -> None:
    # ``grants: null`` is kept for a read that never landed, which is a
    # different thing.
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_GrantsReadCaller(grants_stdout="not toml [["))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}


def test_machine_sharing_put_replaces_a_malformed_grants_document(tmp_path: Path) -> None:
    # A document nothing can parse must never wedge a machine.
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        share=_active_share(),
        mngr_caller=_ShareProbeCaller(is_share_env_present=True, grants_stdout="not toml [["),
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"]}},
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert any("printf" in command for command in _recorded_exec_commands(cli))


def test_machine_sharing_put_refuses_a_bad_document_before_creating_a_share(tmp_path: Path) -> None:
    # One operation, so a refusal leaves a never-published machine exactly as
    # it was.
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"email_domains": ["gmail.com"]}},
    )

    assert response.status_code == 400
    assert json.loads(response.data)["error"] == "grant_refused"
    assert cli.created_shares == []
    # The single exec is the read that decides which entries are new.
    assert len(_recorded_exec_commands(cli)) == 1
    assert not any("printf" in command for command in _recorded_exec_commands(cli))


def test_machine_sharing_put_enables_and_injects_materials(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"], "email_domains": ["partner.org"]}},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["url"] == f"https://{_TEST_HOST_ID}.owner1234.us1.shares.example/"
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert cli.created_shares == [_TEST_HOST_ID]
    # The grants + share.env writes ride over the recording mngr caller in a
    # single combined exec.
    recorded = _recorded_mngr_calls(cli)
    assert any("share_grants.toml" in " ".join(argv) for argv in recorded)
    assert any("share.env" in " ".join(argv) for argv in recorded)
    assert not any("owner_email" in " ".join(argv) for argv in recorded)


def test_machine_sharing_put_publishes_with_a_document_that_grants_nobody(tmp_path: Path) -> None:
    # Publishing admits nobody by itself, so an empty document is ordinary.
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": [], "email_domains": []}},
    )

    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}
    assert cli.created_shares == [_TEST_HOST_ID]
    empty_toml = render_grants_toml({"users": [], "emails": [], "email_domains": []}, {})
    write_command = next(command for command in _recorded_exec_commands(cli) if "printf" in command)
    assert base64.b64encode(empty_toml.encode()).decode("ascii") in write_command


def test_machine_sharing_put_with_user_grants_mirrors_them_to_the_connector(tmp_path: Path) -> None:
    # A user-id grant is written into the grants file AND mirrored (best
    # effort) to the connector: the share's grantee index and the owner's
    # contacts. Neither mirror affects who may visit.
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"users": ["user-2"], "emails": ["viewer@example.com"], "email_domains": []}},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["grants"]["workspace"]["users"] == ["user-2"]
    # No identity cache in this app: the grantee renders as a bare id.
    assert body["identities"] == {}
    recorded = _recorded_mngr_calls(cli)
    grantee_calls = [argv for argv in recorded if "set-grantees" in argv]
    assert grantee_calls == [
        [
            "imbue_cloud",
            "shares",
            "set-grantees",
            _TEST_HOST_ID,
            "--account",
            "owner@example.com",
            "--user-id",
            "user-2",
        ]
    ]
    contact_calls = [argv for argv in recorded if "contacts" in argv]
    assert contact_calls == [["imbue_cloud", "contacts", "add", "user-2", "--account", "owner@example.com"]]
    # The grants write (not the probe, which also names the file) runs under
    # the lock every in-container grants writer takes.
    locked_writes = [argv for argv in recorded if "flock data/.secrets/share_grants.toml.lock" in " ".join(argv)]
    assert len(locked_writes) == 1


def test_machine_sharing_status_reports_the_share_target_labels(tmp_path: Path) -> None:
    # The share panel builds every link as https://<label>.<domain>/ (the bare
    # domain does not route), so the document carries the label per share
    # target: the shell and the apps (system apps included), never internal services.
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_GrantsReadCaller())
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={
            str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
            + make_service_log("web", "http://localhost:8001", "web-w3b1")
            + make_service_log("terminal", "http://localhost:8002", "terminal-t3rm")
            + make_service_log("terminal-pty", "http://localhost:8003", "terminal-pty-p7y")
        },
    )

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["service_labels"] == {
        "system_interface": "system_interface-shl1",
        "web": "web-w3b1",
        "terminal": "terminal-t3rm",
    }


def test_machine_sharing_status_reports_no_labels_before_the_registrations_arrive(tmp_path: Path) -> None:
    # Right after app start the workspace's service registrations have not
    # reached this client yet: the document says so (no labels) instead of
    # letting a client guess a link from the bare domain.
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_GrantsReadCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["service_labels"] == {}


def test_machine_sharing_put_reports_the_share_target_labels(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={
            str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
        },
    )

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"], "email_domains": []}},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["service_labels"] == {"system_interface": "system_interface-shl1"}


def test_machine_sharing_put_on_active_share_updates_grants_without_rotation(tmp_path: Path) -> None:
    agent_id = AgentId()
    # The probe reports materials present with no existing document, so the
    # guard sees nothing to protect and the grants-only path runs.
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"]}},
    )

    assert response.status_code == 200
    assert cli.created_shares == []
    recorded = _recorded_mngr_calls(cli)
    assert any("share_grants.toml" in " ".join(argv) for argv in recorded)
    assert not any("share.env" in " ".join(argv) and "printf" in " ".join(argv) for argv in recorded)


def test_machine_sharing_put_reprovisions_an_active_share_whose_materials_are_missing(tmp_path: Path) -> None:
    """A publish that failed between the connector create and the injection must be repairable.

    The connector then reports the share "active" while the workspace has no
    relay token, so a grants-only update could never bring the tunnel up.
    Publishing again has to take the full provisioning path (the connector
    reuses the share row and rotates the token) and inject the materials.
    """
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_ShareProbeCaller(is_share_env_present=False))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"]}},
    )

    assert response.status_code == 200
    assert cli.created_shares == [_TEST_HOST_ID]
    recorded = _recorded_mngr_calls(cli)
    assert any("share_grants.toml" in " ".join(argv) for argv in recorded)
    assert any("share.env" in " ".join(argv) and "printf" in " ".join(argv) for argv in recorded)


def test_workspace_sharing_put_publishes_a_workspace_that_grants_nobody(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}
    assert cli.created_shares == [_TEST_HOST_ID]
    # The gateway refuses every request when it cannot read a document, so a
    # publish leaves an empty one behind for it rather than none at all.
    recorded = _recorded_mngr_calls(cli)
    assert any("share_grants.toml" in " ".join(argv) and "printf" in " ".join(argv) for argv in recorded)


def test_workspace_sharing_put_keeps_the_grants_the_workspace_already_holds(tmp_path: Path) -> None:
    agent_id = AgentId()
    grants_toml = '[workspace]\nemails = ["viewer@example.com"]\nemail_domains = []\n'
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(grants_stdout=grants_toml))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    write_commands = [call[2] for call in _recorded_mngr_calls(cli) if call and call[0] == "exec"][1:]
    assert any("data/.secrets/share.env" in command for command in write_commands)
    assert not any("share_grants.toml" in command for command in write_commands)


def test_workspace_sharing_grants_put_saves_while_unpublished_and_publishes_nothing(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"], "email_domains": ["partner.org"]}}},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is False
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert body["grants"]["workspace"]["email_domains"] == ["partner.org"]
    assert cli.created_shares == []
    recorded = _recorded_mngr_calls(cli)
    # Exactly one write, under the lock every in-container grants writer takes,
    # and nothing that would publish the workspace.
    locked_writes = [argv for argv in recorded if "flock data/.secrets/share_grants.toml.lock" in " ".join(argv)]
    assert len(locked_writes) == 1
    assert not any("share.env" in " ".join(argv) and "printf" in " ".join(argv) for argv in recorded)


def test_workspace_sharing_grants_put_on_a_published_workspace_keeps_its_link(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"]}}},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["url"] == f"https://{_TEST_HOST_ID}.owner1234.us1.shares.example/"
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    # A grants save never rotates the token or restarts the tunnel.
    assert cli.created_shares == []
    assert not any("share.env" in " ".join(argv) and "printf" in " ".join(argv) for argv in _recorded_mngr_calls(cli))


@pytest.mark.parametrize("body", [{}, {"grants": None}])
def test_workspace_sharing_grants_put_rejects_a_body_carrying_no_document(
    tmp_path: Path, body: dict[str, object]
) -> None:
    # The route's model owns this: a missing or null document is a 422 from the
    # request schema, never a handler that parses None and 500s.
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(f"/api/v1/workspace-sharing/{agent_id}/grants", headers=_auth_header(), json=body)

    assert response.status_code == 422
    # The handler never ran, so the workspace was never touched.
    assert _recorded_exec_commands(cli) == []


def test_workspace_sharing_grants_put_names_every_entry_it_refuses(tmp_path: Path) -> None:
    # 400 with every refused entry named, so the panel can mark the rows a
    # client which skipped the same checks would have created.
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={
            "grants": {
                "workspace": {"emails": ["nope", "owner@example.com"], "email_domains": ["gmail.com"]},
                "services": {"notes": {"email_domains": ["also not a domain"]}},
            }
        },
    )

    assert response.status_code == 400
    assert json.loads(response.data) == {
        "error": "grant_refused",
        "refusals": [
            {"scope": "workspace", "kind": "email", "value": "nope", "message": "nope is not an email address."},
            {
                "scope": "workspace",
                "kind": "email",
                "value": "owner@example.com",
                "message": "owner@example.com is your own address.",
            },
            {
                "scope": "workspace",
                "kind": "email_domain",
                "value": "gmail.com",
                "message": "gmail.com cannot be granted permissions because it is a public email provider.",
            },
            {
                "scope": "notes",
                "kind": "email_domain",
                "value": "also not a domain",
                "message": "also not a domain is not a domain.",
            },
        ],
    }
    # A refused document never reaches the workspace: the one exec is the read
    # that tells the save which entries are new, and nothing is written.
    assert len(_recorded_exec_commands(cli)) == 1
    assert not any("printf" in command for command in _recorded_exec_commands(cli))


def test_workspace_sharing_grants_put_keeps_an_entry_the_document_already_carries(tmp_path: Path) -> None:
    # The create form seeds the owner's own address, so a save that resends
    # that row must land: the rules apply to what a save adds, not to what is
    # already in force.
    agent_id = AgentId()
    stored = '[workspace]\nemails = ["owner@example.com"]\nemail_domains = []\n'
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(grants_stdout=stored))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["owner@example.com", "friend@example.com"]}}},
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["grants"]["workspace"]["emails"] == ["owner@example.com", "friend@example.com"]
    assert any("printf" in command for command in _recorded_exec_commands(cli))


def test_workspace_sharing_grants_put_refuses_the_granters_own_address_when_it_is_new(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["owner@example.com"]}}},
    )

    assert response.status_code == 400
    assert json.loads(response.data)["refusals"] == [
        {
            "scope": "workspace",
            "kind": "email",
            "value": "owner@example.com",
            "message": "owner@example.com is your own address.",
        }
    ]


def test_workspace_sharing_grants_put_adds_someone_beside_a_stored_malformed_entry(tmp_path: Path) -> None:
    # An entry an older panel wrote must not lock the list: it can be kept or
    # removed, and it never blocks granting someone else.
    agent_id = AgentId()
    stored = '[workspace]\nemails = ["not-an-address"]\nemail_domains = ["gmail.com"]\n'
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(grants_stdout=stored))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={
            "grants": {
                "workspace": {"emails": ["not-an-address", "friend@example.com"], "email_domains": ["gmail.com"]}
            }
        },
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["grants"]["workspace"]["emails"] == ["not-an-address", "friend@example.com"]


def test_workspace_sharing_grants_put_replaces_a_malformed_document(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(grants_stdout="not toml [["))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"]}}},
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["grants"]["workspace"]["emails"] == ["viewer@example.com"]


def test_workspace_sharing_grants_put_stores_the_normalized_entries(tmp_path: Path) -> None:
    # What the file holds is what was checked, and what the gateway compares a
    # visitor's address against: trimmed, lowercased, no leading "@".
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": [" Viewer@Example.COM "], "email_domains": ["@Partner.ORG"]}}},
    )

    assert response.status_code == 200, response.data
    grants = json.loads(response.data)["grants"]["workspace"]
    assert grants["emails"] == ["viewer@example.com"]
    assert grants["email_domains"] == ["partner.org"]
    # The file carries the normalized entries, not what was typed.
    normalized_toml = render_grants_toml(
        {"users": [], "emails": ["viewer@example.com"], "email_domains": ["partner.org"]}, {}
    )
    write_command = next(command for command in _recorded_exec_commands(cli) if "printf" in command)
    assert base64.b64encode(normalized_toml.encode()).decode("ascii") in write_command


def test_workspace_sharing_delete_keeps_the_grants_in_its_document(tmp_path: Path) -> None:
    agent_id = AgentId()
    grants_toml = '[workspace]\nemails = ["viewer@example.com"]\nemail_domains = []\n'
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_GrantsReadCaller(grants_stdout=grants_toml))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.delete(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is False
    assert body["url"] is None
    # The list survives unpublishing, so the panel can keep showing it.
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert cli.deleted_shares == [_TEST_HOST_ID]
    clear_commands = [command for command in _recorded_exec_commands(cli) if "rm -f" in command]
    assert len(clear_commands) == 1
    assert "share_grants.toml" not in clear_commands[0]


def test_machine_sharing_put_surfaces_connector_errors(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        create_share_error_to_raise=ImbueCloudCliError("shares create failed (exit 1)"),
        mngr_caller=_ShareProbeCaller(),
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/machines/{_TEST_HOST_ID}/sharing",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"]}},
    )

    assert response.status_code == 502
    assert "Could not publish this workspace" in json.loads(response.data)["error"]


def test_workspace_sharing_put_answers_a_full_share_quota_with_a_structured_code(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        create_share_error_to_raise=ImbueCloudQuotaExceededCliError(
            "shares create: Shared workspace limit reached (50 of 50)."
        ),
        mngr_caller=_ShareProbeCaller(),
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())

    assert response.status_code == 403, response.data
    body = json.loads(response.data)
    assert body["code"] == "quota_exceeded"
    assert body["shared_workspaces_path"] == "/workspaces/shared"
    assert "Shared workspace limit reached (50 of 50)" in body["error"]


def test_machine_sharing_delete_disables(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.delete(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False
    assert cli.deleted_shares == [_TEST_HOST_ID]
    recorded = _recorded_mngr_calls(cli)
    assert any("rm -f" in " ".join(argv) for argv in recorded)


def test_machine_sharing_delete_is_idempotent(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli()
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.delete(f"/api/v1/machines/{_TEST_HOST_ID}/sharing", headers=_auth_header())

    assert response.status_code == 200
    assert cli.deleted_shares == []


def test_machine_sharing_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing")

    assert response.status_code == 401


def test_machine_sharing_readiness_ready_when_shell_label_origin_answers(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share())
    probed_hosts: list[str] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        probed_hosts.append(request.url.host)
        return httpx2.Response(302, headers={"location": "https://accounts.example/share/authorize"})

    http_client = httpx2.Client(transport=httpx2.MockTransport(_handler), follow_redirects=False)
    # The shell's label must be known for readiness -- the probe hits the shell's
    # label origin, not the bare machine domain (which does not route on a share).
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        http_client=http_client,
        service_logs={
            str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
        },
    )

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())

    assert response.status_code == 200
    # The labels ride the poll so a share panel opened before they were known
    # can build the link from the same value the probe used.
    assert json.loads(response.data) == {
        "ready": True,
        "cert_not_after": None,
        "last_tunnel_login_at": None,
        "service_labels": {"system_interface": "system_interface-shl1"},
        **_NO_GATEWAY_STATUS,
    }
    # It probed the shell LABEL origin, never the bare machine domain.
    assert probed_hosts == [f"system_interface-shl1.{_active_share().workspace_domain}"]


def test_machine_sharing_readiness_polls_reuse_the_cached_share_lookup(tmp_path: Path) -> None:
    # The readiness poll fires every ~2 seconds; each uncached share lookup is
    # a multi-second CLI subprocess, so back-to-back polls must reuse one
    # lookup (the TLS probe below is the only per-tick work).
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share())

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "https://accounts.example/share/authorize"})

    http_client = httpx2.Client(transport=httpx2.MockTransport(_handler), follow_redirects=False)
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        http_client=http_client,
        service_logs={
            str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
        },
    )

    first = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())
    second = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())

    assert json.loads(first.data)["ready"] is True
    assert json.loads(second.data)["ready"] is True
    assert cli.share_status_call_count == 1


def test_machine_sharing_readiness_not_ready_when_shell_label_unknown(tmp_path: Path) -> None:
    # Enabled share, but the workspace has not registered its shell service yet,
    # so there is no routable origin to probe -- report not-ready, but still
    # surface the per-step provisioning signals from the share record.
    agent_id = AgentId()
    bare_share = _active_share()
    share = bare_share.model_copy_update(
        to_update(bare_share.field_ref().cert_not_after, "2027-01-01 00:00:00+00:00"),
        to_update(bare_share.field_ref().last_tunnel_login_at, "2026-08-13 12:00:00+00:00"),
    )
    cli = _fake_sharing_cli(share=share)
    client = _sharing_client(tmp_path, agent_id, cli, http_client=httpx2.Client())

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {
        "ready": False,
        "cert_not_after": "2027-01-01 00:00:00+00:00",
        "last_tunnel_login_at": "2026-08-13 12:00:00+00:00",
        "service_labels": {},
        **_NO_GATEWAY_STATUS,
    }


def test_machine_sharing_readiness_reports_the_gateway_status_while_not_live(tmp_path: Path) -> None:
    # The TLS probe fails (no route to the shared hostname), so the poll asks
    # the workspace's gateway why -- and reuses that read across polls.
    agent_id = AgentId()
    status_json = json.dumps(
        {
            "state": "retrying",
            "workspace_domain": _active_share().workspace_domain,
            "failed_attempt_count": 2,
            "last_error": "certificate provisioning failed: connector refused the CSR (503)",
            "next_retry_at": "2026-09-13T12:01:00+00:00",
            "updated_at": "2026-09-13T12:00:00+00:00",
        }
    )
    caller = _GatewayStatusCaller(status_json=status_json)
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=caller)

    def _handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    http_client = httpx2.Client(transport=httpx2.MockTransport(_handler))
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        http_client=http_client,
        service_logs={
            str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
        },
    )

    first = json.loads(client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header()).data)
    second = json.loads(client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header()).data)

    assert first["ready"] is False
    assert first["gateway_state"] == "retrying"
    assert first["gateway_error"] == "certificate provisioning failed: connector refused the CSR (503)"
    assert first["gateway_failed_attempt_count"] == 2
    assert first["gateway_next_retry_at"] == "2026-09-13T12:01:00+00:00"
    assert second == first
    status_reads = [argv for argv in caller.calls if any("share_gateway/status.json" in part for part in argv)]
    assert len(status_reads) == 1
    assert status_reads[0][-3:] == ["--no-start", "--format", "json"]


def test_machine_sharing_readiness_not_ready_when_disabled(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _sharing_client(tmp_path, agent_id, _fake_sharing_cli(), http_client=httpx2.Client())

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {
        "ready": False,
        "cert_not_after": None,
        "last_tunnel_login_at": None,
        "service_labels": {},
        **_NO_GATEWAY_STATUS,
    }


def test_machine_sharing_readiness_not_ready_without_http_client(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _sharing_client(tmp_path, agent_id, _fake_sharing_cli(share=_active_share()))

    response = client.get(f"/api/v1/machines/{_TEST_HOST_ID}/sharing/readiness", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {
        "ready": False,
        "cert_not_after": None,
        "last_tunnel_login_at": None,
        "service_labels": {},
        **_NO_GATEWAY_STATUS,
    }


def _resolver_with_services_agent(agent_id: AgentId, services_id: AgentId) -> BackendResolverInterface:
    """Build a resolver where ``agent_id`` and a ``system-services`` peer share a host.

    The restart worker resolves the system-services agent on the machine's host;
    a single-agent resolver returns None there (so the restart fails fast). This
    registers both agents on the same host so ``get_system_services_agent_id``
    resolves and the worker can run its stop/start steps.
    """
    agents_json = json.dumps(
        {
            "agents": [
                {"id": str(agent_id), "labels": {"workspace": "true", "is_primary": "true"}},
                {"id": str(services_id), "name": "system-services", "labels": {}},
            ]
        }
    )
    return make_resolver_with_data(agents_json)


def test_workspace_restart_returns_202_operation_handle(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # A host restart returns a 202 with the workspace's own id as the
    # operation handle and a kind of "restart".
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=fake_mngr,
        mngr_host_dir=tmp_path / "host",
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": "host"})

    assert response.status_code == 202
    assert json.loads(response.data) == {"operation_id": str(agent_id), "kind": "restart"}


@pytest.mark.parametrize("scope", ["nope", "services"])
def test_workspace_restart_rejects_non_host_scope(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, scope: str
) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": scope})

    # ``scope`` is structurally a string (so it passes spectree), but its *value*
    # must be 'host' -- a value-semantic check the handler keeps, emitting the
    # field-naming 400. The former 'services' scope (in-place system-services
    # restart) was removed, so it is rejected the same way.
    assert response.status_code == 400
    assert "scope" in json.loads(response.data)["error"]


def test_workspace_restart_unknown_workspace_returns_404(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = make_resolver_with_data(make_agents_json(AgentId()))
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )

    response = client.post(f"/api/v1/workspaces/{AgentId()}/restart", headers=_auth_header(), json={"scope": "host"})

    assert response.status_code == 404


def test_workspace_restart_unavailable_without_tracker_returns_503(tmp_path: Path) -> None:
    # No system-interface health tracker / concurrency group wired, so a restart
    # cannot be dispatched.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.post(f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": "host"})

    assert response.status_code == 503


def test_workspace_restart_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.post(f"/api/v1/workspaces/{agent_id}/restart", json={"scope": "host"})

    assert response.status_code == 401


def test_workspace_restart_spawn_failure_returns_503_and_logs_error(tmp_path: Path) -> None:
    """A restart whose worker thread cannot be spawned fails closed with one error log.

    The spawn raises when the concurrency group is shutting down (simulated here
    with an already-exited group). The route has already claimed RECOVERING, so
    it must roll that into RECOVERY_FAILED, fail the registry operation (so the
    operation poller doesn't hang), return 503 -- and log at error level: this is
    the fifth restart-failure branch that must reach error reporting (Principle
    3: the recovery surface is quiet).
    """
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    with ConcurrencyGroup(name="exited-restart-group") as exited_group:
        pass
    tracker = SystemInterfaceHealthTracker()
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=exited_group,
        system_interface_health_tracker=tracker,
    )

    with capture_error_logs() as error_records:
        response = client.post(
            f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": "host"}
        )

    assert response.status_code == 503
    assert tracker.get_health(agent_id) == AgentHealth.RECOVERY_FAILED
    record = get_state(client.application).workspace_operation_registry.get(agent_id)
    assert record is not None and record.status == WorkspaceOperationStatus.FAILED
    assert len(error_records) == 1, error_records


_MACHINE_RESIZE_HOST_ID = "host-" + "b" * 32


def _resolver_for_machine_resize(
    agent_id: AgentId, services_id: AgentId, provider_name: str, host_state: HostState
) -> MngrCliBackendResolver:
    """A resolver reporting one workspace (with its services peer) on a host of ``provider_name`` in ``host_state``."""
    return build_resolver_with_system_services(
        agent_id,
        services_id,
        host_id=HostId(_MACHINE_RESIZE_HOST_ID),
        host_state=host_state,
        provider_name=ProviderInstanceName(provider_name),
        workspace_certified_data={"labels": {"is_primary": "true"}},
    )


@pytest.mark.witnesses("machine-size.resize-restarts-a-running-machine")
def test_machine_size_change_resizes_through_the_provider_and_restarts_a_running_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    caller = RecordingMngrCaller()
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=_write_fake_mngr(tmp_path / "bin"),
        mngr_host_dir=tmp_path / "host",
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        mngr_caller=caller,
    )

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size",
        headers=_auth_header(),
        json={"cpu_count": 2, "memory_gb": 16, "disk_gb": 500},
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"is_restart_dispatched": True, "operation_id": str(agent_id)}
    # The provider's own command records the size; a docker machine has no disk to grow.
    assert caller.calls == [["docker", "resize", _MACHINE_RESIZE_HOST_ID, "--cpus", "2", "--memory", "16g"]]
    assert _wait_for_restart_worker_and_get_status(client, agent_id)["kind"] == "restart"


@pytest.mark.witnesses("machine-size.resize-of-a-stopped-machine-waits")
def test_machine_size_change_leaves_a_stopped_machine_stopped(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "lima", HostState.STOPPED)
    caller = RecordingMngrCaller()
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        mngr_caller=caller,
    )

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"disk_gb": 200}
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"is_restart_dispatched": False, "operation_id": None}
    assert caller.calls == [["lima", "resize", _MACHINE_RESIZE_HOST_ID, "--disk", "200"]]
    # No restart was dispatched, so no operation record exists to poll.
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None


@pytest.mark.witnesses("machine-size.unresizable-machines-refuse")
def test_machine_size_change_refuses_a_machine_its_provider_cannot_resize(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "modal", HostState.RUNNING)
    caller = RecordingMngrCaller()
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_caller=caller)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 2}
    )

    assert response.status_code == 409
    assert "cannot be resized" in json.loads(response.data)["error"]
    assert caller.calls == []


@pytest.mark.witnesses("machine-size.provider-refusal-is-quoted")
def test_machine_size_change_quotes_the_providers_refusal(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    # A verbose run logs its timeline before the verdict; only the verdict is quoted.
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=1,
            stderr="DEBUG | Reading the host record\nERROR: Docker refused the size: only 4 CPUs are available",
            is_mngr_output=True,
        )
    )
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        mngr_caller=caller,
    )

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 64}
    )

    assert response.status_code == 502
    error = json.loads(response.data)["error"]
    assert (
        error
        == "The machine's provider refused the new size: ERROR: Docker refused the size: only 4 CPUs are available"
    )
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None


def test_machine_size_change_does_not_quote_the_callers_own_account_as_a_refusal(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    # What MngrCaller answers when the warm process died: its own words, not mngr's.
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result")
    )
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        mngr_caller=caller,
    )

    with capture_error_logs() as error_records:
        response = client.post(
            f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 2}
        )

    assert response.status_code == 502
    error = json.loads(response.data)["error"]
    assert "refused" not in error
    assert "not recorded" in error
    assert len(error_records) == 1, error_records
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None


def test_machine_size_change_with_nothing_to_change_is_refused(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    # A docker machine takes no disk, so a disk-only request names nothing it can change.
    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"disk_gb": 200}
    )

    assert response.status_code == 400
    # The refusal names only the dimensions this machine takes.
    assert json.loads(response.data)["error"] == "Nothing to resize: pass cpu_count or memory_gb."


@pytest.mark.witnesses("machine-size.no-resize-under-another-operation")
def test_machine_size_change_is_refused_under_a_running_operation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    caller = RecordingMngrCaller()
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_caller=caller)
    registry = get_state(client.application).workspace_operation_registry
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc), None)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 2}
    )

    assert response.status_code == 409
    assert caller.calls == []


class _ResizableWorkspaceOnAnUnparseableHost(StaticBackendResolver):
    """Static resolver placing every known agent on a docker host whose id is the interface's placeholder."""

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost", provider_name="docker")


def test_machine_size_change_refuses_a_workspace_without_a_host_coordinate(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = _ResizableWorkspaceOnAnUnparseableHost(url_by_agent_and_service={str(agent_id): {}})
    caller = RecordingMngrCaller()
    client = _build_client(tmp_path, resolver, mngr_caller=caller)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 2}
    )

    assert response.status_code == 409
    assert "Cannot resolve a host" in json.loads(response.data)["error"]
    assert caller.calls == []


_IMBUE_CLOUD_OWNER_USER_ID = "33333333-3333-3333-3333-333333333333"


def _imbue_cloud_resize_client(
    tmp_path: Path, agent_id: AgentId, host_state: HostState
) -> tuple[FlaskClient, FakeImbueCloudCli]:
    """A client whose one workspace is a leased Imbue Cloud machine linked to a signed-in account."""
    resolver = _resolver_for_machine_resize(
        agent_id, AgentId(), imbue_cloud_provider_name_for_user(_IMBUE_CLOUD_OWNER_USER_ID), host_state
    )
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_IMBUE_CLOUD_OWNER_USER_ID, email="owner@example.com")
    cli.machines = [
        MachineSizeCliInfo(
            host_db_id="row-1",
            host_id=_MACHINE_RESIZE_HOST_ID,
            host_name="sunny",
            status="running" if host_state is HostState.RUNNING else "stopped",
            memory_units=8,
            disk_gb=28,
        )
    ]
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        user_id=_IMBUE_CLOUD_OWNER_USER_ID,
        agent_id=str(agent_id),
        host_id=_MACHINE_RESIZE_HOST_ID,
        display_name="",
        color=None,
        is_cloud_row=False,
    )
    caller = RecordingMngrCaller()
    client = _build_client(
        tmp_path,
        resolver,
        imbue_cloud_cli=cli,
        session_store=session_store,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        mngr_caller=caller,
    )
    return client, cli


@pytest.mark.witnesses(
    "machine-size.imbue-cloud-size-is-recorded-for-the-next-restart",
    partial="the page's rendering is covered by the frontend tests",
)
def test_machine_size_change_records_an_imbue_cloud_size_without_restarting(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.RUNNING)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size",
        headers=_auth_header(),
        json={"memory_gb": 16, "disk_gb": 56},
    )

    assert response.status_code == 200
    # Never a restart: the one that applies the size backs the disks up first, so the user picks when.
    assert json.loads(response.data) == {"is_restart_dispatched": False, "operation_id": None}
    assert cli.resize_calls == [("owner@example.com", _MACHINE_RESIZE_HOST_ID, 16, 56)]
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None
    assert cli.machines[0].target_memory_units == 16
    assert cli.machines[0].target_disk_gb == 56


def test_machine_size_change_records_an_imbue_cloud_size_on_a_stopped_machine_too(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.STOPPED)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"memory_gb": 16}
    )

    assert response.status_code == 200
    assert cli.resize_calls == [("owner@example.com", _MACHINE_RESIZE_HOST_ID, 16, None)]


@pytest.mark.witnesses(
    "machine-size.imbue-cloud-quota-refusal-is-explained",
    partial="the page's rendering is covered by the frontend tests",
)
def test_machine_size_change_explains_an_imbue_cloud_quota_refusal(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.RUNNING)
    cli.resize_error_to_raise = ImbueCloudQuotaExceededCliError(
        "machines resize: Quota exceeded: active machine units limit is 16 (currently 8)"
    )

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"memory_gb": 32}
    )

    assert response.status_code == 409
    assert json.loads(response.data)["error"] == (
        "Your plan cannot hold this size right now: Quota exceeded: active machine units limit is 16 (currently 8)"
    )


def test_machine_size_change_quotes_the_imbue_cloud_plugins_refusal(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.RUNNING)
    # The wrapper's own phrasing of a non-quota plugin failure (see ImbueCloudCli._expect_success).
    cli.resize_error_to_raise = ImbueCloudCliError(
        "machines resize failed: Disk never shrinks: target_disk_gb=20 is below the machine's current 28GB"
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"disk_gb": 20})

    assert response.status_code == 502
    assert json.loads(response.data)["error"] == (
        "The machine's provider refused the new size: "
        "Disk never shrinks: target_disk_gb=20 is below the machine's current 28GB"
    )


def test_machine_size_change_keeps_a_refusal_the_wrapper_did_not_prefix_whole(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.RUNNING)
    # A too-old refusal carries the plugin's message verbatim, with no command prefix in front of it.
    cli.resize_error_to_raise = ImbueCloudClientTooOldCliError("Client too old: update to 1.2 or newer")

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"memory_gb": 16}
    )

    assert response.status_code == 502
    assert json.loads(response.data)["error"] == (
        "The machine's provider refused the new size: Client too old: update to 1.2 or newer"
    )


def test_machine_size_change_on_an_imbue_cloud_machine_takes_no_cpu_count(tmp_path: Path) -> None:
    agent_id = AgentId()
    client, cli = _imbue_cloud_resize_client(tmp_path, agent_id, HostState.RUNNING)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"cpu_count": 4}
    )

    assert response.status_code == 400
    assert json.loads(response.data)["error"] == "Nothing to resize: pass memory_gb or disk_gb."
    assert cli.resize_calls == []


def test_machine_size_change_refuses_an_imbue_cloud_machine_with_no_linked_account(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "imbue_cloud_someone", HostState.RUNNING)
    cli = make_fake_imbue_cloud_cli()
    client = _build_client(
        tmp_path, resolver, imbue_cloud_cli=cli, session_store=make_session_store_for_test(tmp_path / "s", cli=cli)
    )

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/machine-size", headers=_auth_header(), json={"memory_gb": 16}
    )

    assert response.status_code == 409
    assert "not linked" in json.loads(response.data)["error"]


def test_machine_size_change_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = _resolver_for_machine_resize(agent_id, AgentId(), "docker", HostState.RUNNING)
    client = _build_client(tmp_path, resolver)

    response = client.post(f"/api/v1/workspaces/{agent_id}/machine-size", json={"cpu_count": 2})

    assert response.status_code == 401


def _wait_for_restart_worker_and_get_status(client: FlaskClient, agent_id: AgentId) -> dict[str, Any]:
    """Tail the restart worker's stored log to its terminal chunk, then fetch the status.

    Waits for the dispatched restart worker to finish (condition-based, no arbitrary
    sleeps) and returns the parsed body of the typed restart-operation resource,
    asserting the resource responds 200.
    """
    registry = get_state(client.application).workspace_operation_registry
    from_index = 0
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        chunk = registry.read_log_chunk(agent_id, from_index, timeout_seconds=1.0)
        assert chunk is not None
        from_index = chunk.next_index
        if chunk.is_terminal:
            break
    status_resp = client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header())
    assert status_resp.status_code == 200
    return json.loads(status_resp.data)


def test_restart_dispatches_for_never_probed_workspace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A recovery-page dispatch for a never-probed machine must actually restart.

    A machine whose host has been offline since before this process started is
    never enrolled as a probe suspect, so the tracker reports default-HEALTHY for
    it. A veto keyed on that reading would drop the recovery page's
    unconditional entry dispatch (host scope + ``start_only``), stranding the
    machine on the loader forever. The dispatch must proceed to a real restart
    operation -- self-recovery races are absorbed by ``mngr start`` only
    targeting STOPPED agents, not by an endpoint-side veto.
    """
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    tracker = SystemInterfaceHealthTracker()
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=fake_mngr,
        mngr_host_dir=tmp_path / "host",
        system_interface_health_tracker=tracker,
    )

    # Tracker has no record for this workspace (never probed): the dispatch must
    # still go through rather than being vetoed off the default-HEALTHY reading.
    response = client.post(
        f"/api/v1/workspaces/{agent_id}/restart",
        headers=_auth_header(),
        json={"scope": "host", "start_only": True},
    )

    assert response.status_code == 202
    assert json.loads(response.data) == {"operation_id": str(agent_id), "kind": "restart"}

    # Confirm a real restart operation ran to DONE (with no mngr_forward_port
    # wired, a clean dispatch counts as success).
    body = _wait_for_restart_worker_and_get_status(client, agent_id)
    assert body["kind"] == "restart"
    assert body["status"] == "DONE"


def test_workspace_restart_registers_operation_reaching_done(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # End-to-end: a dispatched restart registers a restart operation that the
    # operations resource reports as kind=restart and that reaches DONE once the
    # (faked) stop/start steps complete. With no mngr_forward_port wired, a clean
    # dispatch counts as success.
    agent_id = AgentId()
    services_id = AgentId()
    resolver = _resolver_with_services_agent(agent_id, services_id)
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=fake_mngr,
        mngr_host_dir=tmp_path / "host",
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )

    dispatch = client.post(f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": "host"})
    assert dispatch.status_code == 202

    body = _wait_for_restart_worker_and_get_status(client, agent_id)
    assert body["kind"] == "restart"
    assert body["is_done"] is True
    assert body["status"] == "DONE"


def test_restart_operation_status_reports_registry_record(tmp_path: Path) -> None:
    # The typed restart endpoint reports a restart registry record keyed by the
    # workspace agent id as kind=restart, transitioning RUNNING -> DONE.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))

    running = json.loads(client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data)
    assert running["kind"] == "restart"
    assert running["status"] == "RUNNING"
    assert running["is_done"] is False

    registry.complete(agent_id)
    done = json.loads(client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data)
    assert done["is_done"] is True
    assert done["status"] == "DONE"
    assert done["warning"] is None


def test_restart_operation_status_reports_a_declined_start_as_neither_done_nor_failed(tmp_path: Path) -> None:
    # A start the connector refused as an operator hold: the record is DECLINED
    # with the refusal as its warning, so the frontend shows the sentence
    # without reading the machine as answering (is_done) or the recovery as
    # failed (error).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    registry.decline(agent_id, "This workspace is undergoing maintenance and will be back shortly.")

    body = json.loads(client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data)

    assert body["status"] == "DECLINED"
    assert body["is_done"] is False
    assert body["error"] is None
    assert body["warning"] == "This workspace is undergoing maintenance and will be back shortly."


def test_restart_operation_status_hides_backup_operation_records(tmp_path: Path) -> None:
    # Kind segregation in the restart direction: a backup update record for the
    # same workspace agent id must not read as a restart through the typed
    # restart endpoint (mirrors the backup endpoint hiding restart records).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    response = client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header())

    assert response.status_code == 404


def test_typed_operation_routes_report_independently_for_one_agent_id(tmp_path: Path) -> None:
    # The whole point of type-segmenting the operations resource: a destroy and a
    # (stale, never-pruned) restart record for the *same* workspace agent id no
    # longer shadow each other -- each typed endpoint reports only its own kind.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    registry.complete(agent_id)

    # Write an on-disk destroy record (a live pid -> RUNNING) for the same id,
    # matching the layout documented in ``destroying.py``.
    destroy_dir = tmp_path / "minds" / "destroying" / str(agent_id)
    destroy_dir.mkdir(parents=True)
    (destroy_dir / "pid").write_text(f"{os.getpid()}\n")

    destroy_body = json.loads(
        client.get(f"/api/v1/workspaces/operations/destroy/{agent_id}", headers=_auth_header()).data
    )
    assert destroy_body["kind"] == "destroy"
    assert destroy_body["status"] == "RUNNING"

    restart_body = json.loads(
        client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data
    )
    assert restart_body["kind"] == "restart"


def test_operation_logs_streams_restart_log_lines(tmp_path: Path) -> None:
    # A restart op's logs stream from the in-memory registry queue, ending with a
    # terminal done frame when the operation completes.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    registry.append_log(agent_id, "restarting now")
    registry.complete(agent_id)

    response = client.get(f"/api/v1/workspaces/operations/restart/{agent_id}/logs", headers=_auth_header())

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "restarting now" in text
    assert '"done": true' in text


def test_workspace_backup_check_reports_offline_workspace_with_verification_enabled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # With no discovery host-state data the workspace reads OFFLINE, so the
    # route answers from local data alone (no exec into the workspace).
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backup-check", headers=_auth_header())

    assert response.status_code == 200
    entry = json.loads(response.data)
    assert entry["agent_id"] == str(agent_id)
    assert entry["check_state"] == "OFFLINE"
    assert entry["problems"] == []
    assert entry["is_verification_enabled"] is True


def test_workspace_backup_check_reports_disabled_verification_without_exec(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Verification disabled: no exec runs and the check reports DISABLED.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    set_backup_verification_enabled(InstallationPaths(data_dir=tmp_path / "minds"), agent_id, False)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backup-check", headers=_auth_header())

    assert response.status_code == 200
    entry = json.loads(response.data)
    assert entry["check_state"] == "DISABLED"
    assert entry["is_verification_enabled"] is False


def test_workspace_backups_route_no_longer_carries_the_check_fields(tmp_path: Path) -> None:
    # The snapshot route must never wait on (or leak) the exec-based check:
    # its response carries only the snapshot picture.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 200
    entry = json.loads(response.data)
    assert "check_state" not in entry
    assert "problems" not in entry
    assert entry["is_configured"] is False
    assert entry["snapshots"] == []


def test_backup_service_update_unknown_workspace_returns_404(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = make_resolver_with_data(make_agents_json(AgentId()))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/workspaces/{AgentId()}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 404


def test_backup_service_update_unavailable_without_concurrency_group_returns_503(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 503


def test_backup_service_update_conflicts_with_a_running_operation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Any RUNNING operation for the workspace (here a host recovery) makes a
    # second dispatch a 409 instead of stacking a second worker.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert "A workspace recovery is already in progress" in json.loads(response.data)["error"]
    # The dispatch did not replace the running record.
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.RECOVERY


def test_backup_service_update_refuses_a_machine_below_the_in_place_floor(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A machine too old for today's backup service is refused at the route, before any worker.

    The worker refuses it too, but only after the button has started a spinner
    the machine can never end.
    """
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        # Without a health tracker the update machinery is not assembled at all,
        # and the route would read no version and dispatch regardless.
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    update_service = get_state(client.application).workspace_update_service
    assert update_service is not None
    update_service.state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.NEEDS_RECREATION),
        current_version="minds-v0.3.9",
        supported_version="",
        is_readable=True,
    )

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert json.loads(response.data)["error"] == BELOW_UPDATE_FLOOR_MESSAGE
    # Refused outright: no operation was claimed, so the workspace is still free.
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None


def test_workspace_restart_conflicts_with_a_running_backup_operation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # The reverse serialization direction: a restart dispatched while a backup
    # update is RUNNING must 409 instead of replacing the registry record (and
    # bouncing the host under the in-flight backup mutation).
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(
        tmp_path,
        resolver,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/restart", headers=_auth_header(), json={"scope": "host"})

    assert response.status_code == 409
    assert "backup software update is already in progress" in json.loads(response.data)["error"]
    # The running backup operation's record was not replaced.
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.BACKUP_UPDATE
    assert record.status == WorkspaceOperationStatus.RUNNING


def test_backup_service_update_cancel_without_an_update_returns_404(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    cancel_url = f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel"

    # No operation at all.
    assert client.post(cancel_url, headers=_auth_header()).status_code == 404

    # A non-backup-update record (a restart) must not be cancellable through
    # the backup route either.
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    assert client.post(cancel_url, headers=_auth_header()).status_code == 404


def test_backup_service_update_cancel_flags_a_running_update(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel", headers=_auth_header())

    assert response.status_code == 200
    assert registry.is_cancel_requested(agent_id) is True


def test_backup_restore_unknown_workspace_returns_404(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = make_resolver_with_data(make_agents_json(AgentId()))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/workspaces/{AgentId()}/backups/abc123/restore", headers=_auth_header(), json={})

    assert response.status_code == 404


def test_backup_restore_unconfigured_workspace_returns_409(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Without a canonical restic.env there is no repository to restore from,
    # so the dispatch is rejected up front instead of spawning a worker.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/workspaces/{agent_id}/backups/abc123/restore", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert "not configured" in json.loads(response.data)["error"]
    # No operation record was created.
    assert get_state(client.application).workspace_operation_registry.get(agent_id) is None


def test_backup_restore_conflicts_with_a_running_operation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Any RUNNING operation (including a double-pressed restore) makes a
    # second dispatch a 409 instead of stacking a second worker.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    write_canonical_env(
        InstallationPaths(data_dir=tmp_path / "minds"),
        agent_id,
        "RESTIC_REPOSITORY=/tmp/repo\nRESTIC_PASSWORD=pw\n",
    )
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backups/abc123/restore", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert "restore is already in progress" in json.loads(response.data)["error"]
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.BACKUP_RESTORE


def test_backup_service_update_cancel_flags_a_running_restore(tmp_path: Path) -> None:
    # The shared cancel route also covers a waiting restore (same slot, same
    # cancellable waiting phase).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel", headers=_auth_header())

    assert response.status_code == 200
    assert registry.is_cancel_requested(agent_id) is True


def test_backup_operation_status_reports_a_restore(tmp_path: Path) -> None:
    # A BACKUP_RESTORE record is visible through the shared backup operations
    # endpoint (the settings page polls it with the same code as update).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["kind"] == "backup_restore"
    assert body["status"] == "RUNNING"


def test_backup_operation_status_reports_the_snapshot_a_restore_targets(tmp_path: Path) -> None:
    # A restore reports itself on its table row, so a page loaded mid-restore
    # needs to be told which row: without the snapshot id it would show an idle
    # table over a busy workspace.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    assert registry.start_if_idle(
        agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc), "abc123"
    )

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["snapshot_id"] == "abc123"


def test_backup_operation_status_reports_no_snapshot_for_a_whole_workspace_operation(tmp_path: Path) -> None:
    # An update acts on the workspace, not a snapshot, so it claims no row.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc), None)

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["snapshot_id"] is None


def test_backup_operation_status_reports_cancellable_only_before_mutation(tmp_path: Path) -> None:
    # The UI drives the Cancel button off is_cancellable: offered while the
    # operation is still waiting, withdrawn the moment its worker claims the
    # point of no return.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    waiting = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)
    assert waiting["is_cancellable"] is True

    assert registry.begin_mutation(agent_id) is True
    mutating = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)
    assert mutating["is_cancellable"] is False
    assert mutating["status"] == "RUNNING"


def test_backup_operation_status_reports_configure_as_never_cancellable(tmp_path: Path) -> None:
    # Configure operations have no waiting phase, so a Cancel would always be
    # a lie; the status must never offer it.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc))

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["is_cancellable"] is False


def test_backup_operation_status_reports_a_cancelled_operation_neutrally(tmp_path: Path) -> None:
    # A cancel honored before mutation ends the operation as CANCELLED: not
    # done, but with no error either -- the UI renders a neutral notice, never
    # a red failure box, for something the user asked for.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))
    registry.cancel(agent_id)

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["status"] == "CANCELLED"
    assert body["is_done"] is False
    assert body["error"] is None
    assert body["is_cancellable"] is False


def test_backup_operation_status_carries_a_completion_warning(tmp_path: Path) -> None:
    # A restore that succeeded but whose chained update failed ends DONE with
    # a warning; the status must surface it so the UI can show success plus
    # the caveat.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))
    registry.complete_with_warning(agent_id, "The backup service update failed afterwards.")

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["is_done"] is True
    assert body["error"] is None
    assert body["warning"] == "The backup service update failed afterwards."


def test_backup_operation_logs_replay_full_history_to_a_late_reader(tmp_path: Path) -> None:
    # The log is stored on the operation, not consumed from a queue: a stream
    # opened after lines were appended (or after the operation finished) still
    # sees the complete history, so a page attaching mid-operation shows the
    # same accounting as the dispatching page.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))
    registry.append_log(agent_id, "phase one")
    registry.append_log(agent_id, "phase two")
    registry.complete(agent_id)

    # Each streaming response is consumed fully before the next request opens
    # (the test client keeps one request context alive per unconsumed stream).
    for _ in range(2):
        response = client.get(f"/api/v1/workspaces/operations/backup/{agent_id}/logs", headers=_auth_header())
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert '"phase one"' in text
        assert '"phase two"' in text
        assert '"done": true' in text


def test_backup_service_update_cancel_rejects_a_too_late_cancel(tmp_path: Path) -> None:
    # Once the operation started mutating, a cancel must fail loudly (409)
    # instead of pretending it took effect while the restore runs on.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))
    assert registry.begin_mutation(agent_id) is True

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel", headers=_auth_header())

    assert response.status_code == 409
    assert "no longer be cancelled" in json.loads(response.data)["error"]
    assert registry.is_cancel_requested(agent_id) is False


def test_backup_service_update_cancel_rejects_a_finished_operation(tmp_path: Path) -> None:
    # A cancel that arrives after the operation ended must not read as
    # success: request_cancel refuses (nothing is running to cancel) and the
    # route must surface that refusal instead of returning 200 over a no-op.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))
    registry.complete(agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel", headers=_auth_header())

    assert response.status_code == 409
    assert "no longer be cancelled" in json.loads(response.data)["error"]


def test_backup_service_configure_rejects_configure_later_and_invalid_providers(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    configure_url = f"/api/v1/workspaces/{agent_id}/backup-service/configure"

    later = client.post(configure_url, headers=_auth_header(), json={"backup_provider": "CONFIGURE_LATER"})
    assert later.status_code == 400

    invalid = client.post(configure_url, headers=_auth_header(), json={"backup_provider": "NOT_A_PROVIDER"})
    assert invalid.status_code == 400


def test_backup_service_disable_unknown_workspace_returns_404(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = make_resolver_with_data(make_agents_json(AgentId()))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/workspaces/{AgentId()}/backup-service/disable", headers=_auth_header())

    assert response.status_code == 404


def test_backup_service_disable_conflicts_with_a_running_operation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/disable", headers=_auth_header())

    assert response.status_code == 409


def test_backup_operation_status_unknown_or_wrong_kind_returns_404(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    status_url = f"/api/v1/workspaces/operations/backup/{agent_id}"

    # No operation at all.
    assert client.get(status_url, headers=_auth_header()).status_code == 404

    # Kind segregation: a restart record is not visible through the backup
    # operations endpoint.
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    assert client.get(status_url, headers=_auth_header()).status_code == 404


def test_backup_operation_status_reports_running_then_done(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc))
    status_url = f"/api/v1/workspaces/operations/backup/{agent_id}"

    running = json.loads(client.get(status_url, headers=_auth_header()).data)
    assert running["kind"] == "backup_configure"
    assert running["status"] == "RUNNING"
    assert running["is_done"] is False
    assert running["blocked_chats"] == []

    registry.complete(agent_id)
    done = json.loads(client.get(status_url, headers=_auth_header()).data)
    assert done["is_done"] is True
    assert done["status"] == "DONE"


def test_backup_operation_status_surfaces_blocked_chats(tmp_path: Path) -> None:
    # A failure with the structured blocked-by-running-chats error exposes the
    # chat names so the UI can offer "Stop all chats and retry".
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))
    registry.fail(agent_id, f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}chat-1,chat-2")

    body = json.loads(client.get(f"/api/v1/workspaces/operations/backup/{agent_id}", headers=_auth_header()).data)

    assert body["kind"] == "backup_update"
    assert body["is_done"] is False
    assert body["blocked_chats"] == ["chat-1", "chat-2"]


def test_backup_verification_toggle_round_trips(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    paths = InstallationPaths(data_dir=tmp_path / "minds")
    toggle_url = f"/api/v1/workspaces/{agent_id}/backup-service/verification"

    disabled = client.post(toggle_url, headers=_auth_header(), json={"enabled": False})
    assert disabled.status_code == 200
    assert is_backup_verification_enabled(paths, agent_id) is False

    enabled = client.post(toggle_url, headers=_auth_header(), json={"enabled": True})
    assert enabled.status_code == 200
    assert is_backup_verification_enabled(paths, agent_id) is True


def test_backup_verification_toggle_requires_the_enabled_field(tmp_path: Path) -> None:
    # A missing ``enabled`` is a structural failure, so spectree rejects it up
    # front with the uniform 422 contract.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(
        f"/api/v1/workspaces/{agent_id}/backup-service/verification", headers=_auth_header(), json={}
    )

    assert response.status_code == 422
    errors = json.loads(response.data)["errors"]
    assert any(error["field"] == "enabled" for error in errors)


def test_backup_verification_toggle_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.post(
        f"/api/v1/workspaces/{AgentId()}/backup-service/verification",
        headers=_auth_header(),
        json={"enabled": False},
    )

    assert response.status_code == 404


def test_backup_routes_require_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    assert client.get(f"/api/v1/workspaces/{agent_id}/backups").status_code == 401
    assert client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", json={}).status_code == 401
    assert (
        client.post(f"/api/v1/workspaces/{agent_id}/backup-service/verification", json={"enabled": False}).status_code
        == 401
    )


_CONNECTIONS_ROOT_NAME = "minds-dev-tname"
_CONNECTION_OWNER_ID = "3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c"
_CONNECTION_OWNER_EMAIL = "owner@example.com"


class _ConnectionClientParts(FrozenModel):
    """A desktop client with connections wired up, plus the stores the tests assert on."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: FlaskClient
    connection_store: ConnectionStore
    session_store: MultiAccountSessionStore
    cli: FakeImbueCloudCli
    settings_path: Path
    stub_argv_path: Path


class _FinishedResponse(FrozenModel):
    """A connection add or update driven to its end: the status and body the synchronous route used to answer with."""

    status_code: int
    data: bytes


def _finish_connection_operation(client: FlaskClient, response: TestResponse) -> _FinishedResponse:
    """Poll a 202 handle until the worker ends; anything else is returned as it was answered."""
    if response.status_code != 202:
        return _FinishedResponse(status_code=response.status_code, data=response.data)
    operation_id = json.loads(response.data)["operation_id"]

    def _terminal() -> dict[str, Any] | None:
        polled = client.get(f"/api/v1/desktop/cloud-accounts/operations/{operation_id}", headers=_auth_header())
        assert polled.status_code == 200, polled.data
        info = json.loads(polled.data)
        return info if info["status"] in ("DONE", "FAILED") else None

    info, _poll_count, _elapsed = poll_for_value(_terminal, timeout=20.0, poll_interval=0.05)
    assert info is not None, "the connection operation never ended"
    if info["status"] == "DONE":
        return _FinishedResponse(status_code=200, data=json.dumps(info["account"]).encode())
    body: dict[str, Any] = {"error": info["error"]}
    if info["code"] is not None:
        body["code"] = info["code"]
    return _FinishedResponse(status_code=info["status_code"], data=json.dumps(body).encode())


def _post_connection(parts: _ConnectionClientParts, body: Mapping[str, object]) -> _FinishedResponse:
    """Register a connection and wait for its worker, as the modal does."""
    response = parts.client.post("/api/v1/desktop/cloud-accounts", headers=_auth_header(), json=body)
    return _finish_connection_operation(parts.client, response)


def _put_connection(
    parts: _ConnectionClientParts, instance_name: str, body: Mapping[str, object]
) -> _FinishedResponse:
    """Update a connection's credentials and wait for its worker."""
    response = parts.client.put(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header(), json=body)
    return _finish_connection_operation(parts.client, response)


def _connection_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_concurrency_group: ConcurrencyGroup,
    resolver: BackendResolverInterface,
    stub_exit_code: int,
    minds_config: MindsConfig | None = None,
    agent_creator: AgentCreator | None = None,
) -> _ConnectionClientParts:
    """The cloud-connection routes' whole environment: a stubbed ``mngr`` on PATH (recording its
    argv, exiting with ``stub_exit_code``), a signed-in account, and the stores."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _CONNECTIONS_ROOT_NAME)
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _CONNECTIONS_ROOT_NAME)
    stub_argv_path = tmp_path / "stub-argv.txt"
    install_stub_mngr_on_path(tmp_path / "bin", monkeypatch, f'echo "$@" >> "{stub_argv_path}"\nexit {stub_exit_code}')
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_CONNECTION_OWNER_ID, email=_CONNECTION_OWNER_EMAIL)
    data_dir = tmp_path / "minds"
    session_store = make_session_store_for_test(data_dir, cli)
    connection_store = make_connection_store_for_test(data_dir, cli)
    materializer = make_provider_materializer_for_test(
        _CONNECTIONS_ROOT_NAME, connection_store, session_store_account_source(session_store)
    )
    client = _build_client(
        tmp_path,
        resolver,
        imbue_cloud_cli=cli,
        session_store=session_store,
        connection_store=connection_store,
        provider_materializer=materializer,
        minds_config=minds_config,
        agent_creator=agent_creator,
        root_concurrency_group=root_concurrency_group,
    )
    return _ConnectionClientParts(
        client=client,
        connection_store=connection_store,
        session_store=session_store,
        cli=cli,
        settings_path=settings_path,
        stub_argv_path=stub_argv_path,
    )


def _modal_connection_body(owner: str) -> dict[str, str]:
    return {
        "backend": "modal",
        "alias": "eval modal",
        "owner": owner,
        "modal_token_id": "ak-token-id-1234",
        "modal_token_secret": "as-token-secret",
    }


def test_create_local_modal_connection_writes_its_block_after_a_successful_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )

    response = _post_connection(parts, _modal_connection_body("local"))

    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert body["backend"] == "modal" and body["alias"] == "eval modal" and body["region"] == ""
    assert body["identifier"] == "ak-t…1234"
    assert body["owner_user_id"] == "local" and body["is_region_pinned"] is False
    instance_name = body["name"]
    assert instance_name.startswith("modal_")
    # The token was validated with one listing on the freshly-written instance.
    assert f"list --provider {instance_name} --format json" in parts.stub_argv_path.read_text()
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_secret"] == "as-token-secret"
    assert block["user_id"] == instance_name.removeprefix("modal_")
    # A local-profile connection is never pushed.
    assert parts.cli.document_push_count == 0
    (view,) = parts.connection_store.list_all_views()
    assert view.owner_user_id == "local"


def test_create_local_modal_connection_may_name_its_modal_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The evals harness names the trial's environment after its stamped user id; the block carries it."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    body = {**_modal_connection_body("local"), "modal_user_id": "evals-ci-20261015-abcdef"}

    response = _post_connection(parts, body)

    assert response.status_code == 200, response.data
    instance_name = json.loads(response.data)["name"]
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["user_id"] == "evals-ci-20261015-abcdef"
    (view,) = parts.connection_store.list_all_views()
    assert view.placement == {"user_id": "evals-ci-20261015-abcdef"}


def test_create_connection_refuses_a_modal_namespace_under_an_account_or_in_a_bad_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)

    under_account = _post_connection(
        parts, {**_modal_connection_body(_CONNECTION_OWNER_ID), "modal_user_id": "evals-ci-1"}
    )
    assert under_account.status_code == 400, under_account.data
    assert json.loads(under_account.data) == {
        "error": "modal_user_id is for this-device-only connections",
        "field": "modal_user_id",
    }
    for bad_user_id in ("-leading-dash", "a" * 49, "has space", ""):
        bad_shape = _post_connection(parts, {**_modal_connection_body("local"), "modal_user_id": bad_user_id})
        if bad_user_id == "":
            # Blank means "not named": the id-derived namespace, like an omitted field.
            assert bad_shape.status_code == 200, bad_shape.data
            continue
        assert bad_shape.status_code == 400, bad_user_id
        assert json.loads(bad_shape.data)["field"] == "modal_user_id"
    assert len(parts.connection_store.list_all_views()) == 1


def test_create_connection_answers_503_and_drops_the_row_when_the_account_listing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A failed listing writes no block, so the route must not blame the credentials for the missing instance."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    parts.session_store.invalidate_identity_cache()
    parts.cli.is_auth_list_failing = True

    response = _post_connection(parts, _modal_connection_body("local"))

    assert response.status_code == 503, response.data
    assert json.loads(response.data)["code"] == "account_listing_failed"
    assert "signed-in accounts" in json.loads(response.data)["error"]
    assert parts.connection_store.list_all_views() == []
    # No validation ran against the instance whose block was never written (the stub was never invoked).
    assert not parts.stub_argv_path.exists()


def test_create_connection_answers_503_when_the_account_listing_fails_for_an_account_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A failed listing reads like no accounts; the route must not tell a signed-in owner they are signed out."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    parts.session_store.invalidate_identity_cache()
    parts.cli.is_auth_list_failing = True

    response = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))

    assert response.status_code == 503, response.data
    assert "signed-in accounts" in json.loads(response.data)["error"]
    assert parts.connection_store.list_all_views() == []
    assert not parts.stub_argv_path.exists()


_GCP_SERVICE_ACCOUNT_KEY_JSON = json.dumps(
    {"type": "service_account", "client_email": "svc@proj.iam.gserviceaccount.com"}
)


@pytest.mark.parametrize(
    ("body", "expected_prepare", "expected_placement"),
    [
        (
            {
                "backend": "aws",
                "region": "us-west-2",
                "aws_access_key_id": "AKIA1234EXAMPLE9",
                "aws_secret_access_key": "s",
            },
            "aws prepare --provider {instance} --region us-west-2",
            {"default_region": "us-west-2", "default_instance_type": DEFAULT_AWS_INSTANCE_TYPE},
        ),
        (
            {
                "backend": "gcp",
                "region": "us-central1-a",
                "gcp_service_account_key_json": _GCP_SERVICE_ACCOUNT_KEY_JSON,
            },
            "gcp prepare --provider {instance}",
            {"default_zone": "us-central1-a", "default_machine_type": DEFAULT_GCP_MACHINE_TYPE},
        ),
        (
            {
                "backend": "azure",
                "region": "eastus",
                "azure_subscription_id": "sub",
                "azure_tenant_id": "ten",
                "azure_client_id": "cli",
                "azure_client_secret": "sec",
            },
            "azure prepare --provider {instance}",
            {"default_region": "eastus", "default_vm_size": DEFAULT_AZURE_VM_SIZE},
        ),
    ],
    ids=["aws", "gcp", "azure"],
)
def test_create_vps_connection_runs_prepare_on_its_instance_and_pins_the_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_concurrency_group: ConcurrencyGroup,
    body: dict[str, str],
    expected_prepare: str,
    expected_placement: dict[str, str],
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )

    response = _post_connection(parts, {**body, "alias": "team cloud", "owner": "local"})

    assert response.status_code == 200, response.data
    summary = json.loads(response.data)
    instance_name = summary["name"]
    assert instance_name.startswith(f"{body['backend']}_")
    assert summary["is_region_pinned"] is True and summary["owner_user_id"] == "local"
    assert expected_prepare.format(instance=instance_name) in parts.stub_argv_path.read_text()
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert {key: block[key] for key in expected_placement} == expected_placement
    if body["backend"] == "azure":
        assert block["resource_group"] == f"mngr-{instance_name.removeprefix('azure_')}-eastus"


@pytest.mark.parametrize(
    ("body", "expected_prepare", "expected_block_keys", "forbidden_block_keys", "expected_placement"),
    [
        (
            {"backend": "aws", "region": "us-west-2", "credential_source": "local_profile", "aws_profile": "work"},
            "aws prepare --provider {instance} --region us-west-2",
            {"aws_profile": "work"},
            ("aws_access_key_id", "aws_secret_access_key"),
            {"default_region": "us-west-2", "default_instance_type": DEFAULT_AWS_INSTANCE_TYPE, "aws_profile": "work"},
        ),
        (
            {
                "backend": "azure",
                "region": "eastus",
                "credential_source": "local_profile",
                "azure_subscription_id": "sub-0123456789",
                "azure_tenant_id": "tenant-1",
            },
            "azure prepare --provider {instance}",
            {"subscription_id": "sub-0123456789", "tenant_id": "tenant-1", "use_azure_cli_login": True},
            ("client_id", "client_secret"),
            {"default_region": "eastus", "default_vm_size": DEFAULT_AZURE_VM_SIZE, "tenant_id": "tenant-1"},
        ),
        (
            {"backend": "modal", "credential_source": "local_profile", "modal_profile": "work"},
            "list --provider {instance} --format json",
            {"modal_profile": "work"},
            ("token_id", "token_secret"),
            {"modal_profile": "work"},
        ),
    ],
    ids=["aws", "azure", "modal"],
)
def test_create_local_profile_connection_writes_a_block_naming_the_login_and_no_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_concurrency_group: ConcurrencyGroup,
    body: dict[str, str],
    expected_prepare: str,
    expected_block_keys: dict[str, object],
    forbidden_block_keys: tuple[str, ...],
    expected_placement: dict[str, str],
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)

    response = _post_connection(parts, {**body, "alias": "work cloud", "owner": _CONNECTION_OWNER_ID})

    assert response.status_code == 200, response.data
    summary = json.loads(response.data)
    instance_name = summary["name"]
    assert summary["credential_source"] == "local_profile"
    assert expected_prepare.format(instance=instance_name) in parts.stub_argv_path.read_text()
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert {key: block[key] for key in expected_block_keys} == expected_block_keys
    assert not any(key in block for key in forbidden_block_keys)
    # The login rides the synced metadata in plaintext; the document carries no payload at all.
    (stored,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"].values()
    assert stored["encrypted_payload"] is None
    assert {key: stored["metadata"]["placement"][key] for key in expected_placement} == expected_placement
    assert stored["metadata"]["credential_source"] == "local_profile"


def test_create_local_profile_connection_rejects_gcp_a_blank_profile_and_an_unknown_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )

    gcp = _post_connection(
        parts,
        {
            "backend": "gcp",
            "alias": "g",
            "owner": "local",
            "region": "us-central1-a",
            "credential_source": "local_profile",
        },
    )
    assert gcp.status_code == 400 and json.loads(gcp.data)["field"] == "credential_source"
    blank_profile = _post_connection(
        parts, {"backend": "modal", "alias": "m", "owner": "local", "credential_source": "local_profile"}
    )
    assert blank_profile.status_code == 400 and json.loads(blank_profile.data)["field"] == "modal_profile"
    half_azure = _post_connection(
        parts,
        {
            "backend": "azure",
            "alias": "a",
            "owner": "local",
            "region": "eastus",
            "credential_source": "local_profile",
            "azure_subscription_id": "sub",
        },
    )
    assert half_azure.status_code == 400 and json.loads(half_azure.data)["field"] == "azure_subscription_id"
    unknown = _post_connection(
        parts, {"backend": "modal", "alias": "m", "owner": "local", "credential_source": "vault", "modal_profile": "w"}
    )
    assert unknown.status_code == 400 and json.loads(unknown.data)["field"] == "credential_source"
    assert parts.connection_store.list_all_views() == []
    assert not parts.stub_argv_path.exists()


def test_local_profiles_route_lists_this_machines_logins_per_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    (tmp_path / "aws-config").write_text("[profile work]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))

    response = parts.client.get("/api/v1/desktop/cloud-accounts/local-profiles?backend=aws", headers=_auth_header())

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {
        "backend": "aws",
        "profiles": [
            {
                "name": "work",
                "label": "work",
                "region": "eu-west-1",
                "tenant_id": None,
                "subscription_id": None,
                "unusable_reason": "",
            }
        ],
    }
    unknown = parts.client.get("/api/v1/desktop/cloud-accounts/local-profiles?backend=nope", headers=_auth_header())
    assert unknown.status_code == 400


def test_recheck_route_bounces_discovery_for_a_known_connection_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    created = _post_connection(
        parts,
        {
            "backend": "modal",
            "alias": "m",
            "owner": "local",
            "credential_source": "local_profile",
            "modal_profile": "w",
        },
    )
    assert created.status_code == 200, created.data
    instance_name = json.loads(created.data)["name"]

    # No forward supervisor is wired in tests, so the bounce is a no-op; the route still answers.
    response = parts.client.post(f"/api/v1/desktop/cloud-accounts/{instance_name}/recheck", headers=_auth_header())
    assert response.status_code == 200, response.data
    missing = parts.client.post(
        "/api/v1/desktop/cloud-accounts/modal_" + "0" * 32 + "/recheck", headers=_auth_header()
    )
    assert missing.status_code == 404


def test_create_connection_rolls_back_when_the_credentials_fail_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 6
    )

    response = _post_connection(parts, _modal_connection_body("local"))

    assert response.status_code == 502
    assert json.loads(response.data)["code"] == "credentials_rejected"
    assert "Account setup failed" in json.loads(response.data)["error"]
    assert parts.connection_store.list_all_views() == []
    providers = tomllib.loads(parts.settings_path.read_text()).get("providers", {})
    assert not any(name.startswith("modal_") for name in providers)


def test_create_connection_rolls_back_when_the_validation_command_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A failure that is not the provider's own refusal (here: no runnable mngr) must not leave the row or its block."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    # No mngr anywhere on PATH (every other binary stays reachable), so the spawn itself
    # fails before any exit code.
    (tmp_path / "bin" / MNGR_BINARY).unlink()
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            entry for entry in os.environ["PATH"].split(os.pathsep) if not (Path(entry) / MNGR_BINARY).exists()
        ),
    )

    response = _post_connection(parts, _modal_connection_body("local"))

    assert response.status_code == 500, response.data
    assert parts.connection_store.list_all_views() == []
    providers = tomllib.loads(parts.settings_path.read_text()).get("providers", {})
    assert not any(name.startswith("modal_") for name in providers)


def test_create_synced_connection_requires_an_unlocked_account_with_a_master_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )

    response = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))

    assert response.status_code == 409
    assert json.loads(response.data)["code"] == "master_password_required"
    assert "local profile" in json.loads(response.data)["error"]
    assert parts.connection_store.list_all_views() == []


def test_create_synced_connection_pushes_the_document_to_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)

    response = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))

    assert response.status_code == 200, response.data
    instance_name = json.loads(response.data)["name"]
    documents = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"]
    (stored,) = documents.values()
    assert stored["metadata"]["alias"] == "eval modal" and stored["revision"] == 1
    assert instance_name == f"modal_{ConnectionId(str(stored['doc_id'])).get_uuid().hex}"
    assert instance_name in tomllib.loads(parts.settings_path.read_text())["providers"]


def test_create_synced_connection_rolls_back_when_the_push_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Adding a synced connection requires connectivity: a failed push leaves no row, block, or document."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    parts.cli.is_sync_offline = True

    response = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))

    assert response.status_code == 502, response.data
    assert json.loads(response.data)["code"] == "sync_failed"
    assert "Could not sync the connection" in json.loads(response.data)["error"]
    assert parts.connection_store.list_all_views() == []
    assert parts.cli.sync_documents_by_email.get(_CONNECTION_OWNER_EMAIL, {}).get("connection", {}) == {}
    assert not any(
        name.startswith("modal_") for name in tomllib.loads(parts.settings_path.read_text()).get("providers", {})
    )


def test_create_connection_without_an_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """There is no install-wide default account: the window adding a connection names the owner itself."""
    parts = _connection_client(
        tmp_path,
        monkeypatch,
        root_concurrency_group,
        StaticBackendResolver(url_by_agent_and_service={}),
        0,
    )
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    body = {key: value for key, value in _modal_connection_body("").items() if key != "owner"}

    response = _post_connection(parts, body)

    assert response.status_code == 400, response.data
    assert json.loads(response.data)["field"] == "owner"
    assert parts.connection_store.list_all_views() == []


def test_create_connection_rejects_an_unknown_owner_a_missing_token_a_blank_alias_and_a_blank_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )

    unknown_owner = _post_connection(parts, _modal_connection_body(str(uuid4())))
    assert unknown_owner.status_code == 400
    assert json.loads(unknown_owner.data)["field"] == "owner"
    missing_token = _post_connection(
        parts, {"backend": "modal", "alias": "x", "owner": "local", "modal_token_id": "ak"}
    )
    assert missing_token.status_code == 400
    assert json.loads(missing_token.data)["field"] == "modal_token_id"
    # Spectree's min_length passes a whitespace-only alias; the route rejects it once stripped.
    blank_alias = _post_connection(parts, {**_modal_connection_body("local"), "alias": "  "})
    assert blank_alias.status_code == 400
    assert json.loads(blank_alias.data)["field"] == "alias"
    # Same for a placed backend's region, which Modal bodies do not carry at all.
    blank_region = _post_connection(
        parts,
        {
            "backend": "aws",
            "alias": "work aws",
            "owner": "local",
            "region": "  ",
            "aws_access_key_id": "AKIA1234EXAMPLE9",
            "aws_secret_access_key": "very-secret",
        },
    )
    assert blank_region.status_code == 400
    assert json.loads(blank_region.data)["field"] == "region"
    assert parts.connection_store.list_all_views() == []


def _resolver_with_workspace_on(provider_name: str, agent_id: AgentId) -> MngrCliBackendResolver:
    resolver = MngrCliBackendResolver()
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=agent_id,
        agent_name=AgentName("system-services"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": {"is_primary": "true"}},
    )
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))
    return resolver


def _rebuild_with_resolver(
    tmp_path: Path, parts: _ConnectionClientParts, resolver: BackendResolverInterface
) -> _ConnectionClientParts:
    """The same stores behind a fresh client whose discovery view is ``resolver``."""
    session_store = make_session_store_for_test(tmp_path / "minds", parts.cli)
    return _ConnectionClientParts(
        client=_build_client(
            tmp_path,
            resolver,
            imbue_cloud_cli=parts.cli,
            session_store=session_store,
            connection_store=parts.connection_store,
            provider_materializer=make_provider_materializer_for_test(
                _CONNECTIONS_ROOT_NAME, parts.connection_store, session_store_account_source(session_store)
            ),
        ),
        connection_store=parts.connection_store,
        session_store=session_store,
        cli=parts.cli,
        settings_path=parts.settings_path,
        stub_argv_path=parts.stub_argv_path,
    )


def test_delete_connection_refuses_while_a_workspace_uses_it_then_removes_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = MngrCliBackendResolver()
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, resolver, 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]
    busy_parts = _rebuild_with_resolver(tmp_path, parts, _resolver_with_workspace_on(instance_name, AgentId()))

    refused = busy_parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
    assert refused.status_code == 409
    assert json.loads(refused.data)["code"] == "connection_in_use"
    assert "1 active workspace" in json.loads(refused.data)["error"]
    assert instance_name in tomllib.loads(parts.settings_path.read_text())["providers"]

    removed = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
    assert removed.status_code == 200
    assert instance_name not in tomllib.loads(parts.settings_path.read_text())["providers"]
    assert parts.connection_store.list_all_views() == []
    unknown = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
    assert unknown.status_code == 404


def test_delete_account_connection_tombstones_its_document_and_removes_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    created = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))
    assert created.status_code == 200, created.data
    instance_name = json.loads(created.data)["name"]

    removed = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())

    assert removed.status_code == 200, removed.data
    (stored,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"].values()
    assert stored["state"] == "deleted" and stored["encrypted_payload"] is None and stored["revision"] == 2
    assert parts.connection_store.list_all_views() == []
    assert instance_name not in tomllib.loads(parts.settings_path.read_text())["providers"]


def test_delete_connection_refuses_a_connection_a_newer_app_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    connection_id = ConnectionId.generate()
    parts.cli.seed_connection_document(
        _CONNECTION_OWNER_EMAIL,
        str(connection_id),
        {"connection_format": 99, "backend": "modal", "alias": "from the future", "identifier": "", "placement": {}},
    )
    assert parts.connection_store.pull(_CONNECTION_OWNER_ID, _CONNECTION_OWNER_EMAIL).is_changed is True
    instance_name = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)

    refused = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())

    assert refused.status_code == 409, refused.data
    assert "newer" in json.loads(refused.data)["error"]
    (view,) = parts.connection_store.list_all_views()
    assert view.connection_id == str(connection_id)


def test_delete_local_connection_refuses_while_an_accounts_records_hold_a_workspace_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A workspace on a this-device-only connection may be recorded under an account, so every replica counts."""
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]
    record_store = parts.session_store.record_store
    assert record_store is not None
    record_store.upsert_local_record(
        _CONNECTION_OWNER_ID,
        _CONNECTION_OWNER_EMAIL,
        ReplicaRecord(
            host_id=str(HostId.generate()),
            agent_id=str(AgentId()),
            display_name="on the device connection",
            provider_kind=instance_name,
            hosting_device_id="this-device",
            device_label="here",
            state=RECORD_STATE_ACTIVE,
        ),
    )

    refused = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())

    assert refused.status_code == 409, refused.data
    assert "1 active workspace" in json.loads(refused.data)["error"]
    assert instance_name in tomllib.loads(parts.settings_path.read_text())["providers"]


def test_delete_connection_counts_a_workspace_in_both_discovery_and_the_owners_records_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A workspace hosted here is in local discovery AND mirrored into the owner's records: one workspace, not two."""
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    created = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))
    assert created.status_code == 200, created.data
    instance_name = json.loads(created.data)["name"]
    hosted_here = AgentId()
    busy_parts = _rebuild_with_resolver(tmp_path, parts, _resolver_with_workspace_on(instance_name, hosted_here))
    record_store = busy_parts.session_store.record_store
    assert record_store is not None
    for agent_id, device_id in ((hosted_here, "this-device"), (AgentId(), None)):
        record_store.upsert_local_record(
            _CONNECTION_OWNER_ID,
            _CONNECTION_OWNER_EMAIL,
            ReplicaRecord(
                host_id=str(HostId.generate()),
                agent_id=str(agent_id),
                display_name="on the connection",
                provider_kind=instance_name,
                hosting_device_id=device_id,
                device_label="somewhere",
                state=RECORD_STATE_ACTIVE,
            ),
        )

    refused = busy_parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())

    assert refused.status_code == 409
    # The hosted-here workspace appears in both sources; the other only in the records.
    assert "2 active workspace" in json.loads(refused.data)["error"]


def _rotated_modal_body() -> dict[str, str]:
    return {
        "backend": "modal",
        "alias": "rotated modal",
        "modal_token_id": "ak-token-id-5678",
        "modal_token_secret": "as-rotated-secret",
    }


def _created_synced_modal_connection(parts: _ConnectionClientParts) -> str:
    unlock_account_with_master_password_for_test(parts.connection_store.paths, _CONNECTION_OWNER_ID)
    created = _post_connection(parts, _modal_connection_body(_CONNECTION_OWNER_ID))
    assert created.status_code == 200, created.data
    return json.loads(created.data)["name"]


def test_update_connection_rotates_the_credentials_in_place_and_pushes_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)

    response = _put_connection(parts, instance_name, _rotated_modal_body())

    assert response.status_code == 200, response.data
    summary = json.loads(response.data)
    assert summary["name"] == instance_name and summary["alias"] == "rotated modal"
    assert summary["identifier"] == "ak-t…5678" and summary["owner_user_id"] == _CONNECTION_OWNER_ID
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_id"] == "ak-token-id-5678" and block["token_secret"] == "as-rotated-secret"
    assert parts.stub_argv_path.read_text().count(f"list --provider {instance_name} --format json") == 2
    (stored,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"].values()
    assert stored["metadata"]["alias"] == "rotated modal" and stored["revision"] == 2
    (record,) = parts.connection_store.list_connections(_CONNECTION_OWNER_ID)
    assert record.validation_rollback is None and record.is_dirty is False


def test_update_connection_switches_a_pasted_connection_to_a_local_profile_and_drops_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)

    response = _put_connection(
        parts, instance_name, {"backend": "modal", "credential_source": "local_profile", "modal_profile": "work"}
    )

    assert response.status_code == 200, response.data
    summary = json.loads(response.data)
    assert summary["name"] == instance_name and summary["credential_source"] == "local_profile"
    assert summary["identifier"] == "profile work"
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["modal_profile"] == "work"
    assert "token_id" not in block and "token_secret" not in block
    # The environment the connection's hosts live in is unchanged by the switch.
    assert block["user_id"] == instance_name.removeprefix("modal_")
    (stored,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"].values()
    assert stored["encrypted_payload"] is None and stored["metadata"]["credential_source"] == "local_profile"
    assert stored["metadata"]["placement"]["modal_profile"] == "work"


def test_update_connection_restores_the_previous_credentials_when_the_new_ones_fail_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)
    failing_parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 6)

    response = _put_connection(failing_parts, instance_name, _rotated_modal_body())

    assert response.status_code == 502, response.data
    assert "Account setup failed" in json.loads(response.data)["error"]
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_id"] == "ak-token-id-1234"
    (record,) = parts.connection_store.list_connections(_CONNECTION_OWNER_ID)
    assert record.validation_rollback is None and record.metadata["alias"] == "eval modal"
    (stored,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"].values()
    assert stored["revision"] == 1


def test_update_connection_restores_the_previous_credentials_when_the_push_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)
    parts.cli.is_sync_offline = True

    response = _put_connection(parts, instance_name, _rotated_modal_body())

    assert response.status_code == 502, response.data
    assert "Could not sync the connection" in json.loads(response.data)["error"]
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_id"] == "ak-token-id-1234"
    (record,) = parts.connection_store.list_connections(_CONNECTION_OWNER_ID)
    assert record.validation_rollback is None and record.metadata["alias"] == "eval modal"


def test_update_connection_answers_409_when_the_connection_was_removed_on_another_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)
    (doc_id,) = parts.cli.sync_documents_by_email[_CONNECTION_OWNER_EMAIL]["connection"]
    parts.cli.tombstone_connection_document(_CONNECTION_OWNER_EMAIL, doc_id)

    response = _put_connection(parts, instance_name, _rotated_modal_body())

    assert response.status_code == 409, response.data
    assert json.loads(response.data)["error"] == "This connection was removed on another device."
    assert json.loads(response.data)["code"] == "connection_removed_elsewhere"
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_id"] == "ak-token-id-1234"
    # The restored row is clean, so the next pull retires it like any removal from elsewhere.
    parts.connection_store.pull(_CONNECTION_OWNER_ID, _CONNECTION_OWNER_EMAIL)
    assert parts.connection_store.list_connections(_CONNECTION_OWNER_ID) == []


def test_update_connection_rejects_another_backend_and_a_missing_credential_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]

    other_backend = _put_connection(
        parts, instance_name, {"backend": "aws", "aws_access_key_id": "AKIA1234EXAMPLE9", "aws_secret_access_key": "s"}
    )
    assert other_backend.status_code == 400
    assert json.loads(other_backend.data)["field"] == "backend"
    missing_secret = _put_connection(parts, instance_name, {"backend": "modal", "modal_token_id": "ak-token-id-5678"})
    assert missing_secret.status_code == 400
    assert json.loads(missing_secret.data)["field"] == "modal_token_id"
    unknown = parts.client.put(
        "/api/v1/desktop/cloud-accounts/modal_" + "0" * 32, headers=_auth_header(), json=_rotated_modal_body()
    )
    assert unknown.status_code == 404
    block = tomllib.loads(parts.settings_path.read_text())["providers"][instance_name]
    assert block["token_id"] == "ak-token-id-1234"


def test_update_connection_keeps_the_alias_when_the_body_leaves_it_out_or_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]

    credentials_only = {key: value for key, value in _rotated_modal_body().items() if key != "alias"}
    for body in (credentials_only, {**credentials_only, "alias": None}):
        response = _put_connection(parts, instance_name, body)
        assert response.status_code == 200, response.data
        assert json.loads(response.data)["alias"] == "eval modal"


def test_delete_and_update_refuse_a_connection_whose_credentials_are_still_being_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The update route settles the row when its validation ends; a removal meanwhile would be undone by that."""
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]
    (view,) = parts.connection_store.list_all_views()
    parts.connection_store.update_connection(
        LOCAL_PROFILE_USER_ID,
        view.connection_id,
        alias="mid update",
        identifier="ak-t…5678",
        credentials=ConnectionCredentials(token_id="ak-token-id-5678", token_secret=SecretStr("as-rotated-secret")),
    )

    refused_delete = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
    refused_update = _put_connection(parts, instance_name, _rotated_modal_body())

    for refused in (refused_delete, refused_update):
        assert refused.status_code == 409, refused.data
        assert json.loads(refused.data) == {
            "error": UPDATE_IN_FLIGHT_ON_CONNECTION_MESSAGE,
            "code": "connection_in_use",
        }
    (record,) = parts.connection_store.list_connections(LOCAL_PROFILE_USER_ID)
    assert record.validation_rollback is not None and record.metadata["alias"] == "mid update"
    assert instance_name in tomllib.loads(parts.settings_path.read_text())["providers"]


def test_delete_and_update_refuse_a_connection_whose_registration_is_still_validating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The row exists once the add route answered 202; its worker still has to read it back or roll it back.

    An update meanwhile must not touch the row either: its block is what the
    registration's validation is running against.
    """
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    release_path = tmp_path / "release-validation"
    install_stub_mngr_on_path(tmp_path / "bin", monkeypatch, blocking_release_wait_body(release_path) + "\nexit 0")

    accepted = parts.client.post(
        "/api/v1/desktop/cloud-accounts", headers=_auth_header(), json=_modal_connection_body("local")
    )
    assert accepted.status_code == 202, accepted.data
    instance_name = json.loads(accepted.data)["operation_id"]
    try:
        refused_delete = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
        refused_update = parts.client.put(
            f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header(), json=_rotated_modal_body()
        )
        (record,) = parts.connection_store.list_connections(LOCAL_PROFILE_USER_ID)
    finally:
        release_path.touch()

    for refused in (refused_delete, refused_update):
        assert refused.status_code == 409, refused.data
        assert json.loads(refused.data) == {
            "error": REGISTRATION_IN_FLIGHT_ON_CONNECTION_MESSAGE,
            "code": "connection_in_use",
        }
    # The refused update never replaced the row the registration is validating.
    assert record.validation_rollback is None and record.metadata["alias"] == "eval modal"
    registered = _finish_connection_operation(parts.client, accepted)
    assert registered.status_code == 200, registered.data
    removed = parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())
    assert removed.status_code == 200, removed.data
    assert parts.connection_store.list_all_views() == []


def test_connection_operation_routes_answer_404_for_an_unknown_id_and_replay_the_prepare_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The dialog tails the logs route for the prepare's output; a Modal listing's stdout is JSON, not progress."""
    parts = _connection_client(
        tmp_path, monkeypatch, root_concurrency_group, StaticBackendResolver(url_by_agent_and_service={}), 0
    )
    install_stub_mngr_on_path(
        tmp_path / "bin", monkeypatch, 'echo "stdout-listing-noise"\necho "checking the Modal token" >&2\nexit 0'
    )
    unknown_status = parts.client.get(
        "/api/v1/desktop/cloud-accounts/operations/modal_unknown", headers=_auth_header()
    )
    unknown_logs = parts.client.get(
        "/api/v1/desktop/cloud-accounts/operations/modal_unknown/logs", headers=_auth_header()
    )
    assert unknown_status.status_code == 404, unknown_status.data
    assert unknown_logs.status_code == 404, unknown_logs.data

    accepted = parts.client.post(
        "/api/v1/desktop/cloud-accounts", headers=_auth_header(), json=_modal_connection_body("local")
    )

    assert accepted.status_code == 202, accepted.data
    handle = json.loads(accepted.data)
    assert handle["kind"] == "connection_add"
    operation_id = handle["operation_id"]
    registered = _finish_connection_operation(parts.client, accepted)
    assert registered.status_code == 200, registered.data
    status = json.loads(
        parts.client.get(f"/api/v1/desktop/cloud-accounts/operations/{operation_id}", headers=_auth_header()).data
    )
    assert status["kind"] == "connection_add" and status["is_done"] is True
    assert status["account"]["name"] == operation_id and status["error"] is None
    logs = parts.client.get(f"/api/v1/desktop/cloud-accounts/operations/{operation_id}/logs", headers=_auth_header())
    assert logs.status_code == 200
    text = logs.get_data(as_text=True)
    assert '"checking the Modal token"' in text
    assert "stdout-listing-noise" not in text
    assert '"done": true' in text


def test_update_connection_answers_409_when_its_owner_signs_out_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A signout drops the row mid-update; the settlement finds nothing to clear or restore, and the route says so."""
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    instance_name = _created_synced_modal_connection(parts)
    release_path = tmp_path / "release-validation"
    install_stub_mngr_on_path(tmp_path / "bin", monkeypatch, blocking_release_wait_body(release_path) + "\nexit 0")

    accepted = parts.client.put(
        f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header(), json=_rotated_modal_body()
    )
    assert accepted.status_code == 202, accepted.data
    try:
        wait_for(
            lambda: any(
                record.validation_rollback is not None
                for record in parts.connection_store.list_connections(_CONNECTION_OWNER_ID)
            ),
            timeout=10.0,
            poll_interval=0.05,
            error_message="the update never reached its validation",
        )
        parts.connection_store.retain_unpushed_removals(_CONNECTION_OWNER_ID)
    finally:
        release_path.touch()
    response = _finish_connection_operation(parts.client, accepted)

    assert response.status_code == 409, response.data
    assert json.loads(response.data) == {"error": CONNECTION_GONE_DURING_UPDATE_MESSAGE}
    assert parts.connection_store.list_connections(_CONNECTION_OWNER_ID) == []
    assert instance_name not in tomllib.loads(parts.settings_path.read_text())["providers"]


@pytest.mark.parametrize("is_create_on_the_connection", [True, False])
def test_delete_connection_waits_for_a_workspace_still_being_created_on_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_concurrency_group: ConcurrencyGroup,
    is_create_on_the_connection: bool,
) -> None:
    """A create holds no record until `mngr create` returns, so the gate reads the creates in flight too."""
    parts = _connection_client(tmp_path, monkeypatch, root_concurrency_group, MngrCliBackendResolver(), 0)
    created = _post_connection(parts, _modal_connection_body("local"))
    instance_name = json.loads(created.data)["name"]
    in_flight_provider = instance_name if is_create_on_the_connection else "docker"
    busy_parts = _connection_client(
        tmp_path,
        monkeypatch,
        root_concurrency_group,
        MngrCliBackendResolver(),
        0,
        agent_creator=make_in_flight_agent_creator(
            tmp_path,
            root_concurrency_group,
            (InFlightCreateAttempt(provider_instance_name=in_flight_provider, host_name="still-creating"),),
        ),
    )

    response = busy_parts.client.delete(f"/api/v1/desktop/cloud-accounts/{instance_name}", headers=_auth_header())

    if is_create_on_the_connection:
        assert response.status_code == 409, response.data
        assert json.loads(response.data)["error"] == CREATE_IN_FLIGHT_ON_CONNECTION_MESSAGE
        assert json.loads(response.data)["code"] == "connection_in_use"
        assert instance_name in tomllib.loads(parts.settings_path.read_text())["providers"]
    else:
        assert response.status_code == 200, response.data
        assert instance_name not in tomllib.loads(parts.settings_path.read_text())["providers"]


class _NameConflictAgentCreator(_RecordingAgentCreator):
    """Recording creator whose ``start_create_attempt`` always reports an in-flight name conflict."""

    def start_create_attempt(
        self,
        repo_source: str,
        host_name: str = "",
        display_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.DOCKER,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        cloud_account: str = "",
        instance_type: str = "",
        machine_size: MachineSizeSelection | None = None,
        modal_sandbox_timeout_seconds: int | None = None,
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
        account_id: str = "",
        is_onboarding_driven_by_agent: bool = False,
    ) -> CreateAttemptId:
        raise WorkspaceNameInUseError(
            "A machine named 'contended' is already being created. "
            "Wait for that create attempt to finish or pick a different name."
        )


def test_create_workspace_in_flight_name_conflict_returns_409(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    creator = _NameConflictAgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "contended"},
    )

    assert response.status_code == 409
    body = json.loads(response.data)
    assert body["field"] == "host_name"
    assert "already being created" in body["error"]


class _InFlightNamesRecordingCreator(_RecordingAgentCreator):
    """Recording creator that also reports fixed in-flight names (any provider)."""

    fixed_in_flight_names: tuple[str, ...] = ()

    def live_in_flight_host_names(self, provider_instance_name: str | None = None) -> set[str]:
        del provider_instance_name
        return {name.casefold() for name in self.fixed_in_flight_names}


def test_create_workspace_auto_namer_avoids_in_flight_names(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # ``workspace-1`` is known to discovery and ``workspace-2`` is held by a
    # live in-flight create attempt (invisible to discovery); the auto-namer must
    # skip both and pick ``workspace-3``.
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"is_primary": "true"}, host_name="workspace-1"),
    )
    creator = _InFlightNamesRecordingCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_in_flight_names=("workspace-2",),
    )
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, resolver=resolver, agent_creator=creator)

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 202
    assert str(creator.last_call["host_name"]) == "workspace-3"


def test_create_workspace_threads_account_id_to_start_create_attempt(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The account id must reach start_create_attempt so the pending-create-attempt record
    # (the crash-safe association) can carry it.
    fake_cli = make_fake_imbue_cloud_cli()
    fake_cli.add_account(user_id="user-77120", email="user-77120@example.com")
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=fake_cli)
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(
        tmp_path,
        root_concurrency_group,
        agent_creator=creator,
        session_store=session_store,
    )

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "with-account", "account_id": "user-77120"},
    )

    assert response.status_code == 202
    assert creator.last_call["account_id"] == "user-77120"


def test_create_workspace_threads_the_onboarding_questions_flag_off_unless_the_body_turns_it_on(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # The creation page reads the flag back from the attempt's record, so it must reach the creator;
    # the form leaves it off, and the start flow's creates (and the form's switch) turn it on.
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, agent_creator=creator)

    quiet = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "quiet-create"},
    )
    assert quiet.status_code == 202
    assert creator.last_call["is_onboarding_driven_by_agent"] is False

    asked = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "asked-create", "is_onboarding_driven_by_agent": True},
    )
    assert asked.status_code == 202
    assert creator.last_call["is_onboarding_driven_by_agent"] is True


def test_create_workspace_marks_onboarding_complete(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    # Starting any create is the point past which the first-run start flow is no
    # longer useful, so the front door records it for every surface.
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    creator = _make_recording_creator(tmp_path, root_concurrency_group)
    client = _client_with_agent_creator(
        tmp_path,
        root_concurrency_group,
        agent_creator=creator,
        minds_config=minds_config,
    )

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "host_name": "first-ever-workspace"},
    )

    assert response.status_code == 202
    assert minds_config.get_is_onboarding_complete() is True


# The stderr an `mngr exec` run really produced against a healthy pre-declutter
# workspace: three warnings about orphaned key dirs for long-gone hosts, and not
# one word about the host that was actually asked for.
_UNRELATED_HOST_WARNINGS = (
    "WARNING: imbue_cloud[imbue_cloud_acct] outer SSH unreachable for host host-a15c1302: "
    "Host not found: host-a15c1302\n"
    "WARNING: imbue_cloud[imbue_cloud_acct] outer SSH unreachable for host host-0b17800a: "
    "Host not found: host-0b17800a\n"
)


def test_describe_mngr_exec_failure_leads_with_the_target_not_the_unrelated_warnings() -> None:
    """The per-agent reason must come first, with the warnings kept behind it.

    Leading with stderr told the caller the hub could not reach *some other*
    workspace's host, which reads as "your box is down" and sent an agent off to
    restore from backups instead of retrying. Dropping the warnings instead would
    trade that for a different loss: they are real diagnostics (these ones are a
    genuine orphaned-key-dir bug), and a caller on another host has no other copy.
    """
    envelope = json.dumps(
        {
            "results": [],
            "failed_agents": [{"agent": "system-services", "error": "Agent system-services not found on host"}],
            "total_executed": 0,
            "total_failed": 1,
        }
    )

    description = _describe_mngr_exec_failure(envelope, _UNRELATED_HOST_WARNINGS)

    assert description.startswith("system-services: Agent system-services not found on host")
    # The warnings survive, in full, behind the verdict.
    assert "outer SSH unreachable for host host-a15c1302" in description
    assert "outer SSH unreachable for host host-0b17800a" in description


def test_describe_mngr_exec_failure_caps_a_runaway_stderr() -> None:
    """Discovery can warn about every unreachable host, so the appended context is bounded."""
    envelope = json.dumps({"results": [], "failed_agents": [{"agent": "a", "error": "boom"}], "total_failed": 1})

    description = _describe_mngr_exec_failure(envelope, "WARNING: noise\n" * 5000)

    assert description.startswith("a: boom")
    assert "truncated" in description
    assert len(description) < 3000


def test_describe_mngr_exec_failure_falls_back_to_stderr_without_an_envelope() -> None:
    """A run that died before emitting JSON still has to report something."""
    assert _describe_mngr_exec_failure("", "mngr: command not found") == "mngr: command not found"


def test_describe_mngr_exec_failure_falls_back_when_no_agent_carries_a_reason() -> None:
    """A well-formed envelope with nothing useful in it must not report an empty reason."""
    envelope = json.dumps({"results": [], "failed_agents": [], "total_executed": 0, "total_failed": 0})

    assert _describe_mngr_exec_failure(envelope, "something went wrong") == "something went wrong"


def test_workspace_sharing_routes_resolve_the_workspace_to_its_machine(tmp_path: Path) -> None:
    """The workspace-keyed sharing routes serve the same documents as the host-keyed shims."""
    agent_id = AgentId()
    client = _sharing_client(tmp_path, agent_id, _fake_sharing_cli())

    by_workspace = client.get(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())
    assert by_workspace.status_code == 200
    assert json.loads(by_workspace.data)["enabled"] is False

    # A legacy host coordinate is accepted at the same route.
    by_host = client.get(f"/api/v1/workspace-sharing/{_TEST_HOST_ID}", headers=_auth_header())
    assert by_host.status_code == 200

    unknown = client.get(f"/api/v1/workspace-sharing/{AgentId()}", headers=_auth_header())
    assert unknown.status_code == 404


def test_workspace_sharing_put_delete_and_readiness_accept_the_workspace_coordinate(tmp_path: Path) -> None:
    """The mutating and readiness workspace-keyed routes resolve to the same machine as the shims."""
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    enabled = client.put(
        f"/api/v1/workspace-sharing/{agent_id}",
        headers=_auth_header(),
        json={"workspace": {"emails": ["viewer@example.com"]}},
    )
    assert enabled.status_code == 200
    assert json.loads(enabled.data)["enabled"] is True
    assert cli.created_shares == [_TEST_HOST_ID]

    readiness = client.get(f"/api/v1/workspace-sharing/{agent_id}/readiness", headers=_auth_header())
    assert readiness.status_code == 200
    assert json.loads(readiness.data)["ready"] is False

    disabled = client.delete(f"/api/v1/workspace-sharing/{agent_id}", headers=_auth_header())
    assert disabled.status_code == 200
    assert json.loads(disabled.data)["enabled"] is False
    assert cli.deleted_shares == [_TEST_HOST_ID]


# Invitations (specs/inviting-granted-visitors/spec.md)


_INVITED_WORKSPACE_NAME: Final[str] = "Robot Butler"


def _invitation_client(
    tmp_path: Path, agent_id: AgentId, cli: FakeSharingCli, extra_service_logs: str = ""
) -> FlaskClient:
    """A sharing client whose workspace is published, carries a display name, and whose shell registered its label."""
    cli.share = _active_share()
    service_logs = {
        str(agent_id): make_service_log("system_interface", "http://localhost:8000", "system_interface-shl1")
        + extra_service_logs
    }
    return _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs=service_logs,
        agent_labels={"is_primary": "true", WORKSPACE_DISPLAY_NAME_LABEL: _INVITED_WORKSPACE_NAME},
    )


def test_workspace_sharing_grants_put_pushes_the_document_to_imbue_cloud_while_published(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"]}}},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["grants_synced"] is True
    ((account, host_id, document),) = cli.pushed_grants_documents
    assert host_id == _TEST_HOST_ID
    assert document["workspace"]["emails"] == ["viewer@example.com"]


def test_workspace_sharing_grants_put_reports_a_failed_push_as_not_synced(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        share=_active_share(), mngr_caller=_ShareProbeCaller(is_share_env_present=True), is_grants_push_failing=True
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"]}}},
    )

    # The save itself landed; only the mirror did not.
    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["grants"]["workspace"]["emails"] == ["viewer@example.com"]
    assert body["grants_synced"] is False


def test_workspace_sharing_grants_put_pushes_nothing_while_unpublished(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.put(
        f"/api/v1/workspace-sharing/{agent_id}/grants",
        headers=_auth_header(),
        json={"grants": {"workspace": {"emails": ["viewer@example.com"]}}},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["grants_synced"] is None
    assert cli.pushed_grants_documents == []


def test_workspace_sharing_invite_carries_the_targets_link_and_reports_the_outcome(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _invitation_client(tmp_path, agent_id, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations",
        headers=_auth_header(),
        json={"email": "bob@example.com"},
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {"outcome": "invited", "invited_at": "2026-09-30T12:00:00+00:00"}
    (call,) = cli.invite_calls
    assert call["email"] == "bob@example.com"
    assert call["user_id"] is None
    assert call["app"] is None
    assert call["link"] == f"https://system_interface-shl1.{_TEST_HOST_ID}.owner1234.us1.shares.example/"
    assert call["workspace_name"] == _INVITED_WORKSPACE_NAME
    # The whole machine is named for what it grants, never after the shell app.
    assert call["app_display_name"] is None


def test_workspace_sharing_invite_carries_the_name_a_person_reads_for_the_app(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _invitation_client(
        tmp_path,
        agent_id,
        cli,
        extra_service_logs=make_service_log(
            "world-of-nonsense", "http://localhost:8100", "world-of-nonsense-fs6k", "World of Nonsense"
        ),
    )

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations",
        headers=_auth_header(),
        json={"email": "bob@example.com", "app": "world-of-nonsense"},
    )

    assert response.status_code == 200, response.data
    (call,) = cli.invite_calls
    assert call["app"] == "world-of-nonsense"
    assert call["app_display_name"] == "World of Nonsense"
    assert call["link"] == f"https://world-of-nonsense-fs6k.{_TEST_HOST_ID}.owner1234.us1.shares.example/"


def test_workspace_sharing_invite_sends_no_app_name_when_the_app_registered_none(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _invitation_client(
        tmp_path,
        agent_id,
        cli,
        extra_service_logs=make_service_log("web", "http://localhost:8100", "web-fs6k"),
    )

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations",
        headers=_auth_header(),
        json={"email": "bob@example.com", "app": "web"},
    )

    assert response.status_code == 200, response.data
    (call,) = cli.invite_calls
    assert call["app"] == "web"
    assert call["app_display_name"] is None


def test_workspace_sharing_invite_pushes_the_document_and_retries_once_when_out_of_date(tmp_path: Path) -> None:
    agent_id = AgentId()
    out_of_date = ImbueCloudShareRefusedCliError("shares invite: refused")
    out_of_date.code = "grants_out_of_date"
    cli = _fake_sharing_cli(
        mngr_caller=_ShareProbeCaller(is_share_env_present=True),
        invite_results=[out_of_date, InvitationCliResult(outcome="invited", invited_at="2026-09-30T12:00:00+00:00")],
    )
    client = _invitation_client(tmp_path, agent_id, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations", headers=_auth_header(), json={"email": "bob@example.com"}
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["outcome"] == "invited"
    assert len(cli.invite_calls) == 2
    assert len(cli.pushed_grants_documents) == 1


def test_workspace_sharing_invite_reports_a_refusal_by_its_code(tmp_path: Path) -> None:
    agent_id = AgentId()
    not_invitable = ImbueCloudShareRefusedCliError("shares invite: this person has already joined")
    not_invitable.code = "not_invitable"
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(is_share_env_present=True), invite_results=[not_invitable])
    client = _invitation_client(tmp_path, agent_id, cli)

    refused = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations", headers=_auth_header(), json={"email": "bob@example.com"}
    )
    two_subjects = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations",
        headers=_auth_header(),
        json={"email": "bob@example.com", "user_id": "u-1"},
    )

    assert refused.status_code == 409
    assert json.loads(refused.data)["error"] == "not_invitable"
    assert two_subjects.status_code == 400
    assert len(cli.invite_calls) == 1


def test_workspace_sharing_invite_refuses_an_unpublished_workspace(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller())
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/invitations", headers=_auth_header(), json={"email": "bob@example.com"}
    )

    assert response.status_code == 502
    assert "Publish" in json.loads(response.data)["error"]
    assert cli.invite_calls == []


def test_workspace_sharing_invitation_outcomes_lists_each_entry(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        mngr_caller=_ShareProbeCaller(is_share_env_present=True),
        invitation_outcomes_to_return=[
            InvitationOutcomeCliEntry(
                kind="email",
                value="bob@example.com",
                app="system_interface",
                outcome="invited",
                invited_at="2026-09-30T12:00:00+00:00",
            ),
            InvitationOutcomeCliEntry(
                kind="user", value="u-2", app="notes", outcome="joined", joined_at="2026-09-29T00:00:00+00:00"
            ),
        ],
    )
    client = _invitation_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}/invitation-outcomes", headers=_auth_header())

    assert response.status_code == 200, response.data
    outcomes = json.loads(response.data)["outcomes"]
    assert [(entry["app"], entry["value"], entry["outcome"]) for entry in outcomes] == [
        ("system_interface", "bob@example.com", "invited"),
        ("notes", "u-2", "joined"),
    ]
    assert outcomes[1]["joined_at"] == "2026-09-29T00:00:00+00:00"
    assert outcomes[0]["last_visited_at"] is None


def test_workspace_sharing_invitation_outcomes_is_empty_while_unpublished(tmp_path: Path) -> None:
    agent_id = AgentId()
    not_published = ImbueCloudShareRefusedCliError("shares invitation-outcomes: not published")
    not_published.code = "not_published"
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(), invitation_outcomes_error_to_raise=not_published)
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}/invitation-outcomes", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"outcomes": []}


def test_workspace_sharing_mobile_access_link_carries_the_whole_workspaces_link_and_its_name(
    tmp_path: Path,
) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(is_share_env_present=True))
    client = _invitation_client(tmp_path, agent_id, cli)

    response = client.post(f"/api/v1/workspace-sharing/{agent_id}/mobile-access-link", headers=_auth_header())

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["outcome"] == "sent"
    (call,) = cli.mobile_access_link_calls
    assert call["link"] == f"https://system_interface-shl1.{_TEST_HOST_ID}.owner1234.us1.shares.example/"
    assert call["workspace_name"] == _INVITED_WORKSPACE_NAME


def test_workspace_sharing_mobile_access_link_reports_a_refusal_by_its_code(tmp_path: Path) -> None:
    agent_id = AgentId()
    no_link = ImbueCloudShareRefusedCliError("shares mobile-access-link: no link yet")
    no_link.code = "no_workspace_link"
    cli = _fake_sharing_cli(
        mngr_caller=_ShareProbeCaller(is_share_env_present=True), mobile_access_link_results=[no_link]
    )
    client = _invitation_client(tmp_path, agent_id, cli)

    response = client.post(f"/api/v1/workspace-sharing/{agent_id}/mobile-access-link", headers=_auth_header())

    assert response.status_code == 409
    assert json.loads(response.data)["error"] == "no_workspace_link"


def test_workspace_sharing_mobile_access_link_leaves_an_unshared_workspace_to_imbue_cloud(
    tmp_path: Path,
) -> None:
    # The desktop makes no judgment of its own about the share's state: it asks
    # with no link, and the connector answers the refusal the panel shows.
    agent_id = AgentId()
    not_published = ImbueCloudShareRefusedCliError("shares mobile-access-link: not published")
    not_published.code = "not_published"
    cli = _fake_sharing_cli(mngr_caller=_ShareProbeCaller(), mobile_access_link_results=[not_published])
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.post(f"/api/v1/workspace-sharing/{agent_id}/mobile-access-link", headers=_auth_header())

    assert response.status_code == 409
    assert json.loads(response.data)["error"] == "not_published"
    (call,) = cli.mobile_access_link_calls
    assert call["link"] is None


_FOLDER_SYNC_DEVICE_ID: Final[str] = "host-0f0e0d0c0b0a09080706050403020100"
_FOLDER_SYNC_START_TIMEOUT_SECONDS: Final[float] = 20.0


def _client_with_folder_syncs(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    workspace_agent_id: AgentId,
    chat_agent_id: AgentId,
) -> tuple[FlaskClient, FolderSyncManager]:
    """A client whose folder syncs run against the stand-in ``mngr`` and a workspace with a chat in it."""
    resolver = ChatInWorkspaceResolver(
        url_by_agent_and_service={}, workspace_agent_id=workspace_agent_id, chat_agent_id=chat_agent_id
    )
    manager = FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_FOLDER_SYNC_DEVICE_ID,
        backend_resolver=resolver,
        store=FolderSyncStore(records_dir=tmp_path / "folder_syncs"),
    )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        mngr_caller=RecordingMngrCaller(),
        folder_sync_manager=manager,
        device_id=_FOLDER_SYNC_DEVICE_ID,
    )
    return app.test_client(), manager


def test_folder_syncs_answers_any_agent_of_the_workspace_with_the_workspaces_syncs(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A chat asks under its own id, and is answered with what is filed under the primary's."""
    workspace_agent_id, chat_agent_id = AgentId(), AgentId()
    client, manager = _client_with_folder_syncs(tmp_path, root_concurrency_group, workspace_agent_id, chat_agent_id)
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    manager.start(str(workspace_agent_id), str(local_folder), FolderSyncDirection.BOTH, FolderSyncConflict.NEWER)
    try:
        manager.wait_until_started(str(workspace_agent_id), str(local_folder), _FOLDER_SYNC_START_TIMEOUT_SECONDS)
        wait_until_synced(manager, str(workspace_agent_id), str(local_folder), _FOLDER_SYNC_START_TIMEOUT_SECONDS)

        response = client.get(f"/api/v1/agents/{chat_agent_id}/folder-syncs", headers=_auth_header())
    finally:
        manager.stop_all()

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["device_id"] == _FOLDER_SYNC_DEVICE_ID
    (sync,) = body["syncs"]
    assert sync["local_path"] == str(local_folder)
    assert sync["workspace_path"] == f"~/synced_folders/{_FOLDER_SYNC_DEVICE_ID}{local_folder}"
    assert sync["activity"] == "ACTIVE"
    assert sync["state"] == "SYNCED"
    assert sync["message"] is None
    assert sync["bytes_done"] is None and sync["bytes_total"] is None
    assert sync["direction"] == "BOTH"
    assert sync["conflict"] == "NEWER"


def test_folder_syncs_reports_where_a_turned_off_syncs_copy_was_set_aside(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    workspace_agent_id, chat_agent_id = AgentId(), AgentId()
    client, manager = _client_with_folder_syncs(tmp_path, root_concurrency_group, workspace_agent_id, chat_agent_id)
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    manager.start(str(workspace_agent_id), str(local_folder), FolderSyncDirection.BOTH, FolderSyncConflict.NEWER)
    manager.wait_until_started(str(workspace_agent_id), str(local_folder), _FOLDER_SYNC_START_TIMEOUT_SECONDS)
    manager.stop(str(workspace_agent_id), str(local_folder))
    assert manager.wait_until_settled(str(workspace_agent_id), str(local_folder), _FOLDER_SYNC_START_TIMEOUT_SECONDS)
    manager.forget(str(workspace_agent_id), str(local_folder))

    response = client.get(f"/api/v1/agents/{workspace_agent_id}/folder-syncs", headers=_auth_header())

    (sync,) = json.loads(response.data)["syncs"]
    assert sync["activity"] == "INACTIVE"
    assert sync["state"] == "STOPPED"
    assert sync["workspace_path"] == f"~/inactive_synced_folders/{_FOLDER_SYNC_DEVICE_ID}{local_folder}"


def test_folder_syncs_refuses_an_agent_this_desktop_does_not_know(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, _manager = _client_with_folder_syncs(tmp_path, root_concurrency_group, AgentId(), AgentId())

    response = client.get(f"/api/v1/agents/{AgentId()}/folder-syncs", headers=_auth_header())

    assert response.status_code == 404


def test_folder_syncs_answers_with_none_in_a_build_that_cannot_sync(tmp_path: Path) -> None:
    workspace_agent_id, chat_agent_id = AgentId(), AgentId()
    client, _app = _client_with_chat(tmp_path, workspace_agent_id, chat_agent_id)

    response = client.get(f"/api/v1/agents/{chat_agent_id}/folder-syncs", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["syncs"] == []


# Visiting agents: the Share panel's routes onto the workspace's agent-access CLI.


class _VisitingAgentsCaller(_ShareProbeCaller):
    """The share probe caller, also answering every ``agent-access`` exec with ``agent_access_answer``."""

    agent_access_answer: str = Field(default="{}", description="The JSON the CLI prints")
    agent_access_returncode: int = Field(default=0, description="The CLI's exit code")
    agent_access_stderr: str = Field(default="", description="What the CLI says on stderr when it refuses")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        if len(argv) > 2 and "agent-access" in argv[2]:
            super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
            envelope = {
                "results": [
                    {
                        "agent": "system-services",
                        "stdout": self.agent_access_answer,
                        "stderr": self.agent_access_stderr,
                        "success": self.agent_access_returncode == 0,
                    }
                ]
            }
            return MngrCallResult(
                returncode=self.agent_access_returncode, stdout=json.dumps(envelope), stderr=self.agent_access_stderr
            )
        return super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)


def _visiting_agents_client(
    tmp_path: Path, agent_id: AgentId, caller: _VisitingAgentsCaller, cli: FakeSharingCli | None = None
) -> FlaskClient:
    sharing_cli = cli if cli is not None else _fake_sharing_cli(mngr_caller=caller)
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    store = _associated_session_store(
        tmp_path, sharing_cli, agent_id, user_id="33333333-3333-3333-3333-333333333333", email="owner@example.com"
    )
    return _build_client(
        tmp_path,
        resolver,
        imbue_cloud_cli=sharing_cli,
        session_store=store,
        client_env_config=_TEST_CLIENT_ENV_CONFIG,
        mngr_caller=caller,
    )


def _agent_access_exec_commands(caller: _VisitingAgentsCaller) -> list[str]:
    """The CLI invocation inside each agent-access exec, without the template-capability check around it."""
    commands: list[str] = []
    for call in caller.calls:
        if len(call) > 2 and "agent-access" in call[2]:
            assert call[2].startswith("if [ -f system/services/share_gateway/src/share_gateway/agent_cli.py ]; then ")
            commands.append(call[2].split("; then ", 1)[1].split("; else ", 1)[0])
    return commands


def test_visiting_agents_list_runs_the_workspaces_cli_and_reports_its_agents(tmp_path: Path) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=True,
        agent_access_answer=json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "va-" + "1" * 32,
                        "name": "Josh Claude",
                        "fingerprint": "SHA256:abc",
                        "scope": "workspace",
                        "granter": "user:33333333-3333-3333-3333-333333333333",
                        "created_at": "2026-10-15T18:00:00+00:00",
                        "expires_at": None,
                        "last_seen_at": None,
                    }
                ]
            }
        ),
    )
    client = _visiting_agents_client(tmp_path, agent_id, caller)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}/agents", headers=_auth_header())

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {
        "agents": [
            {
                "agent_id": "va-" + "1" * 32,
                "name": "Josh Claude",
                "fingerprint": "SHA256:abc",
                "scope": "workspace",
                "granter": "user:33333333-3333-3333-3333-333333333333",
                "created_at": "2026-10-15T18:00:00+00:00",
                "expires_at": None,
                "last_seen_at": None,
            }
        ]
    }
    assert _agent_access_exec_commands(caller) == ["uv run --no-sync agent-access list --json"]


def test_visiting_agents_invite_publishes_an_unpublished_workspace_then_mints_as_the_owner(tmp_path: Path) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=False,
        agent_access_answer=json.dumps(
            {
                "code": "7K3M-QX9A-2PFH",
                "expires_at": "2026-10-15T18:15:00+00:00",
                "scope": "workspace",
                "granter": "user:33333333-3333-3333-3333-333333333333",
                "auth_origin": "https://auth-x.alpha.example.com",
                "is_published": True,
                "instructions": "Join at https://auth-x.alpha.example.com with 7K3M-QX9A-2PFH",
            }
        ),
    )
    cli = _fake_sharing_cli(mngr_caller=caller)
    client = _visiting_agents_client(tmp_path, agent_id, caller, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/agents/invites",
        headers=_auth_header(),
        json={"scope": "workspace", "name_hint": "Josh Claude"},
    )

    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert body["code"] == "7K3M-QX9A-2PFH" and body["auth_origin"] == "https://auth-x.alpha.example.com"
    assert "7K3M-QX9A-2PFH" in body["instructions"] and body["scope"] == "workspace"
    # The workspace was published first (the share was created and its materials written), then the CLI ran.
    assert len(cli.created_shares) == 1
    assert caller.is_share_env_present is True
    assert _agent_access_exec_commands(caller) == [
        "uv run --no-sync agent-access invite --granter user:33333333-3333-3333-3333-333333333333 "
        "--scope workspace --name-hint 'Josh Claude' --json"
    ]


def test_visiting_agents_invite_does_not_republish_a_published_workspace_and_passes_an_app_scope(
    tmp_path: Path,
) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=True,
        agent_access_answer=json.dumps(
            {
                "code": "C",
                "expires_at": "2026-10-15T18:15:00+00:00",
                "scope": ["chat"],
                "granter": "user:33333333-3333-3333-3333-333333333333",
                "auth_origin": "https://auth-x.alpha.example.com",
                "is_published": True,
                "instructions": "x",
            }
        ),
    )
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=caller)
    client = _visiting_agents_client(tmp_path, agent_id, caller, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/agents/invites",
        headers=_auth_header(),
        json={"scope": ["chat"], "expires_in_days": 7},
    )

    assert response.status_code == 200, response.data
    assert json.loads(response.data)["scope"] == ["chat"]
    assert cli.created_shares == []
    assert _agent_access_exec_commands(caller) == [
        "uv run --no-sync agent-access invite --granter user:33333333-3333-3333-3333-333333333333 "
        "--scope chat --expires-in-days 7.0 --json"
    ]


def test_visiting_agents_invite_refuses_when_the_share_never_reached_the_workspace(tmp_path: Path) -> None:
    """The connector holds a share but the workspace has no materials: the CLI's placeholder origin is not handed out."""
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=False,
        agent_access_answer=json.dumps(
            {
                "code": "C",
                "expires_at": "2026-10-15T18:15:00+00:00",
                "scope": "workspace",
                "granter": "user:33333333-3333-3333-3333-333333333333",
                "auth_origin": "",
                "is_published": False,
                "instructions": "Join at https://<the workspace's address once it is published>",
            }
        ),
    )
    cli = _fake_sharing_cli(share=_active_share(), mngr_caller=caller)
    client = _visiting_agents_client(tmp_path, agent_id, caller, cli)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/agents/invites", headers=_auth_header(), json={"scope": "workspace"}
    )

    assert response.status_code == 502, response.data
    assert "Turn sharing off and on again" in json.loads(response.data)["error"]
    assert cli.created_shares == []


def test_visiting_agents_invite_reports_a_cli_refusal_as_a_bad_request(tmp_path: Path) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=True,
        agent_access_returncode=2,
        agent_access_stderr=json.dumps({"error": "not a grantable app: owner-exec", "kind": "refused"}) + "\n",
    )
    client = _visiting_agents_client(tmp_path, agent_id, caller)

    response = client.post(
        f"/api/v1/workspace-sharing/{agent_id}/agents/invites", headers=_auth_header(), json={"scope": ["owner-exec"]}
    )

    assert response.status_code == 400
    assert "not a grantable app" in json.loads(response.data)["error"]


def test_visiting_agents_revoke_runs_the_cli_as_the_owner_and_maps_an_unknown_agent_to_404(tmp_path: Path) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=True, agent_access_answer=json.dumps({"revoked": "va-" + "1" * 32, "name": "x"})
    )
    client = _visiting_agents_client(tmp_path, agent_id, caller)

    response = client.delete(f"/api/v1/workspace-sharing/{agent_id}/agents/va-{'1' * 32}", headers=_auth_header())

    assert response.status_code == 200, response.data
    assert json.loads(response.data) == {"revoked": "va-" + "1" * 32}
    assert _agent_access_exec_commands(caller) == [
        f"uv run --no-sync agent-access revoke va-{'1' * 32} --revoker user:33333333-3333-3333-3333-333333333333 "
        "--owner --json"
    ]

    caller.agent_access_returncode = 2
    caller.agent_access_stderr = json.dumps({"error": f"no agent va-{'f' * 32}", "kind": "refused"}) + "\n"
    missing = client.delete(f"/api/v1/workspace-sharing/{agent_id}/agents/va-{'f' * 32}", headers=_auth_header())
    assert missing.status_code == 404

    # A value that is not an agent id is refused before anything runs in the workspace.
    malformed = client.delete(f"/api/v1/workspace-sharing/{agent_id}/agents/--json", headers=_auth_header())
    assert malformed.status_code == 400
    assert len(_agent_access_exec_commands(caller)) == 2


def test_visiting_agents_on_a_workspace_without_the_cli_answer_409_with_the_update_hint(tmp_path: Path) -> None:
    """The exec's own capability check answers the CLI's refusal shape with kind ``unsupported``."""
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(
        is_share_env_present=True,
        agent_access_returncode=3,
        agent_access_stderr=json.dumps({"error": "predates visiting agents; update it", "kind": "unsupported"}),
    )
    client = _visiting_agents_client(tmp_path, agent_id, caller)

    response = client.get(f"/api/v1/workspace-sharing/{agent_id}/agents", headers=_auth_header())

    assert response.status_code == 409, response.data
    assert "update it" in json.loads(response.data)["error"]


def test_visiting_agents_routes_require_auth_and_a_known_workspace(tmp_path: Path) -> None:
    agent_id = AgentId()
    caller = _VisitingAgentsCaller(is_share_env_present=True, agent_access_answer=json.dumps({"agents": []}))
    client = _visiting_agents_client(tmp_path, agent_id, caller)
    assert client.get(f"/api/v1/workspace-sharing/{agent_id}/agents").status_code == 401
    assert client.get(f"/api/v1/workspace-sharing/{AgentId()}/agents", headers=_auth_header()).status_code == 404
    # A legacy machine coordinate resolves to the machine's agent, as on every sharing route; an unknown one is 404.
    assert client.get(f"/api/v1/workspace-sharing/{_TEST_HOST_ID}/agents", headers=_auth_header()).status_code == 200
    unknown_host = client.get(f"/api/v1/workspace-sharing/host-{'f' * 32}/agents", headers=_auth_header())
    assert unknown_host.status_code == 404, unknown_host.data
    # Only the resolved coordinate reached the workspace's CLI.
    assert len(_agent_access_exec_commands(caller)) == 1
