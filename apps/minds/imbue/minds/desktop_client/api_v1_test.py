import json
import os
import queue
import shlex
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from flask.testing import FlaskClient
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreationErrorKind
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_update import BLOCKED_BY_RUNNING_CHATS_PREFIX
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import TunnelInfo
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.templates import status_text_for
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.workspace_operations import OPERATION_LOG_SENTINEL
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import LaunchMode
from imbue.minds.testing import stub_mngr_host_dir
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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
    """An ``AgentCreator`` whose ``start_creation`` records its args instead of spawning.

    The real ``start_creation`` launches a background thread that clones the repo
    and shells out to ``mngr create``; the create-route tests only need to assert
    on what the route *passes* to it (resolved host name, color, ...), so this
    stub captures the call and returns a fresh ``CreationId`` synchronously.
    """

    _last_call: dict[str, object] | None = PrivateAttr(default=None)

    def start_creation(
        self,
        repo_source: str,
        host_name: str = "",
        display_name: str = "",
        branch: str = "",
        launch_mode: LaunchMode = LaunchMode.DOCKER,
        ai_provider: AIProvider = AIProvider.SUBSCRIPTION,
        account_email: str = "",
        branch_or_tag: str = "",
        region: str = "",
        anthropic_api_key: str = "",
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
    ) -> CreationId:
        self._last_call = {
            "repo_source": repo_source,
            "host_name": host_name,
            "display_name": display_name,
            "branch": branch,
            "launch_mode": launch_mode,
            "ai_provider": ai_provider,
            "account_email": account_email,
            "branch_or_tag": branch_or_tag,
            "region": region,
            "anthropic_api_key": anthropic_api_key,
            "color": color,
            "docker_runtime": docker_runtime,
            "original_minds_version": original_minds_version,
        }
        return CreationId()

    @property
    def last_call(self) -> dict[str, object]:
        assert self._last_call is not None, "start_creation was never called"
        return self._last_call


class _StatusReportingAgentCreator(_RecordingAgentCreator):
    """Recording creator that also reports a fixed creation info for status polls."""

    fixed_info: AgentCreationInfo

    def get_creation_info(self, creation_id: CreationId) -> AgentCreationInfo | None:
        return self.fixed_info if creation_id == self.fixed_info.creation_id else None


def _client_with_agent_creator(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
    *,
    resolver: BackendResolverInterface | None = None,
    agent_creator: AgentCreator | None = None,
    session_store: MultiAccountSessionStore | None = None,
) -> FlaskClient:
    """Build a test client whose ``/api/v1`` create route has an ``AgentCreator`` wired.

    The create route returns 501 when no ``AgentCreator`` is configured (before
    any input validation runs), so reaching the validation branches requires a
    creator. The invalid-input tests assert on 400 responses that return before
    ``start_creation`` is ever called; the happy-path tests pass a
    :class:`_RecordingAgentCreator` so the route's call is captured without
    starting a real background creation (subprocess / network).
    """
    if resolver is None:
        resolver = StaticBackendResolver(url_by_agent_and_service={})
    if agent_creator is None:
        agent_creator = AgentCreator(
            paths=WorkspacePaths(data_dir=tmp_path / "minds"),
            root_concurrency_group=root_concurrency_group,
            notification_dispatcher=notification_dispatcher,
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
        )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        agent_creator=agent_creator,
        session_store=session_store,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    return app.test_client()


def _make_recording_creator(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> _RecordingAgentCreator:
    return _RecordingAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )


def test_list_workspaces_returns_known_workspaces(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get("/api/v1/workspaces", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    ids = [w["agent_id"] for w in body["workspaces"]]
    assert str(agent_id) in ids


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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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
    # returns an empty snapshot list, is_configured false, and the check half
    # still reports its verdict (OFFLINE here: no discovery host-state data).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["is_configured"] is False
    assert body["snapshots"] == []
    assert body["is_backing_up"] is False
    assert body["check_state"] == "OFFLINE"
    assert body["is_verification_enabled"] is True
    assert body["update_target_version"].startswith("minds-v")


def test_create_workspace_without_agent_creator_returns_501(tmp_path: Path) -> None:
    # The default test client has no agent_creator wired, so create is unavailable.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 501


def test_create_workspace_imbue_cloud_without_any_account_returns_signup_redirect(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # IMBUE_CLOUD with no account selected AND no accounts existing at all is the
    # no-account backstop: the route returns a 400 carrying the sign-up redirect
    # target so the create page navigates there (mirrors the old form's 303).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # IMBUE_CLOUD with no account selected but accounts that DO exist must ask the
    # user to pick one (a field error on account_id), not redirect to sign-up.
    cli = _fake_sharing_cli()
    cli.add_account(user_id="11111111-1111-1111-1111-111111111111", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher, session_store=store)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    body = json.loads(response.data)
    assert body["field"] == "account_id"
    assert "redirect_url" not in body


def test_create_workspace_empty_git_url_returns_field_error(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A missing repository URL is a field-level validation error so the create
    # page can render the message inline next to the git_url input.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": ""})

    assert response.status_code == 400
    body = json.loads(response.data)
    assert body["field"] == "git_url"
    assert body["error"]


def test_create_workspace_invalid_host_name_returns_field_error(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A submitted name that normalizes to an empty slug (here all punctuation)
    # surfaces as a 400 keyed to the host_name field (rather than a deferred
    # FAILED on the creating page).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # With no host_name and ``workspace-1`` already known, the route resolves the
    # next free ``workspace-N`` (workspace-2) before handing off to the creator.
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"is_primary": "true"}, host_name="workspace-1"),
    )
    creator = _make_recording_creator(tmp_path, root_concurrency_group, notification_dispatcher)
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, resolver=resolver, agent_creator=creator
    )

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 202
    assert str(creator.last_call["host_name"]) == "workspace-2"


def test_create_operation_status_includes_status_text(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # The create-operation status carries a human-readable status_text (the stage
    # caption the creating page renders), derived from status + launch_mode.
    creation_id = CreationId()
    creator = _StatusReportingAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreationInfo(
            creation_id=creation_id,
            status=AgentCreationStatus.INITIALIZING,
            launch_mode=LaunchMode.DOCKER,
        ),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

    response = client.get(f"/api/v1/workspaces/operations/create/{creation_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["kind"] == "create"
    assert body["status_text"] == status_text_for(str(AgentCreationStatus.INITIALIZING), launch_mode=LaunchMode.DOCKER)
    assert body["status_text"]
    # An in-flight (non-failed) creation carries no failure classification.
    assert body["error_kind"] is None


def test_create_operation_status_carries_error_kind_for_classified_failures(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A failed creation whose error was classified (e.g. a private GitHub repo
    # the local git credentials cannot see) reports the machine-readable kind
    # alongside the error message; the creating page gates its static sign-in
    # guidance on it.
    creation_id = CreationId()
    creator = _StatusReportingAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreationInfo(
            creation_id=creation_id,
            status=AgentCreationStatus.FAILED,
            launch_mode=LaunchMode.DOCKER,
            error="git clone failed:\nfatal: could not read Username for 'https://github.com'",
            error_kind=CreationErrorKind.GITHUB_AUTH_REQUIRED,
        ),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

    response = client.get(f"/api/v1/workspaces/operations/create/{creation_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["status"] == "FAILED"
    assert body["error"]
    assert body["error_kind"] == "GITHUB_AUTH_REQUIRED"


def test_create_workspace_full_surface_returns_202_and_threads_fields(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # The full create field surface (color, explicit name, branch, ...) is
    # accepted: a 202 with an operation handle, and the fields are passed through
    # to the creator.
    creator = _make_recording_creator(tmp_path, root_concurrency_group, notification_dispatcher)
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={
            "git_url": "https://example/repo",
            "host_name": "my-mind",
            "branch": "main",
            "color": "#0b292b",
            "launch_mode": "DOCKER",
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


def test_create_workspace_requires_api_key_for_api_key_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # An API_KEY ai_provider without an anthropic_api_key must fail validation
    # up front (400).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "ai_provider": "API_KEY"},
    )

    assert response.status_code == 400
    assert json.loads(response.data)["field"] == "anthropic_api_key"


def test_create_workspace_rejects_invalid_backup_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A malformed backup_provider is a structural (enum) failure, so spectree
    # rejects it up front with the uniform 422 contract, before any background
    # creation is started.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # imbue_cloud *backups* (independent of the compute/AI provider) need an
    # account; without one the shared backup-request builder rejects it with a
    # 400 that mentions the account, before any background creation starts.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

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


def test_lifecycle_without_concurrency_group_returns_501(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/start", headers=_auth_header())

    assert response.status_code == 501


def test_stop_workspace_broadcasts_workspace_stopped_event(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A successful v1 stop broadcasts a one-shot ``workspace_stopped`` chrome SSE payload.

    The Electron shell closes any window still open to the workspace off this
    event (otherwise the open view would observe the dead interface, redirect
    to recovery, and auto-restart the host -- silently undoing an
    agent-requested stop). The landing-page stop shares this route, so both
    stop paths emit through the one mechanism.
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
    event_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    wake_event = threading.Event()
    get_state(client.application).chrome_event_broadcaster.subscribe(event_queue, wake_event)

    response = client.post(f"/api/v1/workspaces/{agent_id}/stop", headers=_auth_header())

    assert response.status_code == 200
    assert wake_event.is_set()
    assert event_queue.get_nowait() == {"type": "workspace_stopped", "agent_id": str(agent_id)}


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
    event_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    get_state(client.application).chrome_event_broadcaster.subscribe(event_queue, threading.Event())

    response = client.post(f"/api/v1/workspaces/{agent_id}/start", headers=_auth_header())

    assert response.status_code == 200
    assert event_queue.empty()


def test_operation_status_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    creation_id = CreationId()

    response = client.get(f"/api/v1/workspaces/operations/create/{creation_id}", headers=_auth_header())

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


def _write_recording_fake_mngr(directory: Path, record_path: Path) -> str:
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
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "mngr"
    rec = shlex.quote(str(record_path))
    envelope = json.dumps(
        {
            "results": [{"agent": "t", "stdout": "", "stderr": "", "success": True}],
            "failed_agents": [],
            "total_executed": 1,
            "total_failed": 0,
        }
    )
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do printf \'%s\\0\' "$a" >> {rec}; done\n'
        f"printf '\\036' >> {rec}\n"
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
    # Write: `mngr exec <id> <write script>` -- single trailing COMMAND arg.
    assert write_argv[0] == "exec"
    assert write_argv[1] == str(target)
    assert len(write_argv) == 3, f"write command must be a single arg, got {write_argv!r}"
    assert "bash" not in write_argv and "-c" not in write_argv and "-lc" not in write_argv and "--" not in write_argv
    # The write body is composed only from the parsed inner stdout: neither the
    # JSON envelope text nor any human-format status line may reach the file.
    write_script = write_argv[2]
    assert "authorized_keys" in write_script
    assert "Command succeeded" not in write_script
    assert '"results"' not in write_script


def test_operation_logs_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    creation_id = CreationId()

    response = client.get(f"/api/v1/workspaces/operations/create/{creation_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_unknown_destroy_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/operations/destroy/{other_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get(f"/api/v1/workspaces/operations/create/{CreationId()}/logs")

    assert response.status_code == 401


# -- Shared builders for the new routes --


def _build_client(
    tmp_path: Path,
    resolver: BackendResolverInterface,
    *,
    root_concurrency_group: ConcurrencyGroup | None = None,
    mngr_binary: str = "mngr",
    mngr_host_dir: Path | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    session_store: MultiAccountSessionStore | None = None,
    http_client: httpx.Client | None = None,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = None,
) -> FlaskClient:
    """Build a desktop-client test client with the /api/v1 surface and the given deps."""
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=http_client,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir,
        imbue_cloud_cli=imbue_cloud_cli,
        session_store=session_store,
        system_interface_health_tracker=system_interface_health_tracker,
        mngr_caller=RecordingMngrCaller(),
    )
    return app.test_client()


def _write_fake_mngr(directory: Path) -> str:
    """Write an executable fake ``mngr`` that always exits 0; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "mngr"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return str(script)


class FakeSharingCli(FakeImbueCloudCli):
    """In-memory ``ImbueCloudCli`` double for the sharing routes.

    Returns canned tunnel / service / policy data and records mutating calls,
    so the sharing status/enable/disable routes can be exercised without
    shelling out to ``mngr imbue_cloud``.
    """

    tunnel: TunnelInfo | None = None
    service_entries: list[dict[str, Any]] = Field(default_factory=list)
    service_auth: dict[str, Any] = Field(default_factory=dict)
    removed_services: list[str] = Field(default_factory=list)
    added_services: list[str] = Field(default_factory=list)
    enabled_policies: list[dict[str, Any]] = Field(default_factory=list)
    enable_sharing_error_stderr: str | None = Field(
        default=None, description="When set, enable_sharing raises an ImbueCloudCliError carrying this stderr"
    )

    def find_tunnel_for_agent(self, account: str, agent_id: str) -> TunnelInfo | None:
        return self.tunnel

    def create_tunnel(self, *, account: str, agent_id: str, default_policy: Any = None) -> TunnelInfo:
        assert self.tunnel is not None
        return self.tunnel

    def enable_sharing(
        self,
        *,
        account: str,
        agent_id: str,
        service_name: str,
        service_url: str,
        policy: Any,
    ) -> tuple[TunnelInfo, dict[str, Any]]:
        if self.enable_sharing_error_stderr is not None:
            error = ImbueCloudCliError("tunnels enable-sharing failed (exit 1); see the desktop client logs")
            error.stderr = self.enable_sharing_error_stderr
            raise error
        assert self.tunnel is not None
        self.added_services.append(service_name)
        self.enabled_policies.append(dict(policy))
        hostname = next(
            (e.get("hostname") for e in self.service_entries if e.get("service_name") == service_name),
            "share.example.com",
        )
        return self.tunnel, {"service_name": service_name, "service_url": service_url, "hostname": hostname}

    def list_services(self, account: str, tunnel_name: str) -> list[dict[str, Any]]:
        return list(self.service_entries)

    def get_service_auth(self, account: str, tunnel_name: str, service_name: str) -> dict[str, Any]:
        return dict(self.service_auth)

    def remove_service(self, account: str, tunnel_name: str, service_name: str) -> None:
        self.removed_services.append(service_name)

    def delete_tunnel(self, account: str, tunnel_name: str) -> None:
        return None


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


# -- PATCH /api/v1/workspaces/<id> (color + account) --


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
    # silently accepted then garbage-collected -- the previous false-success bug.
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
    # caller can confirm it (previously there was no account field at all).
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
    assert "leased from imbue_cloud" in json.loads(response.data)["error"]


def test_patch_workspace_disassociate_leased_host_returns_403(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _build_client(tmp_path, _LeasedImbueCloudResolver(url_by_agent_and_service={}))

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": None})

    assert response.status_code == 403
    assert "leased from imbue_cloud" in json.loads(response.data)["error"]


# -- DELETE /api/v1/workspaces/operations/destroy/<id> (dismiss) --


def test_dismiss_destroy_operation_is_idempotent_noop(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/destroy/{AgentId()}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {}


def test_dismiss_operation_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/destroy/{AgentId()}")

    assert response.status_code == 401


# -- Desktop provider toggle --


def test_patch_provider_enable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-tname")
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    client = _build_client(tmp_path, resolver)

    response = client.patch("/api/v1/desktop/providers/docker", headers=_auth_header(), json={"enabled": True})

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body == {"provider_name": "docker", "enabled": True, "changed": True}
    assert "is_enabled = true" in settings_path.read_text()


def test_patch_provider_disable_with_active_workspaces_conflicts(tmp_path: Path) -> None:
    # The single workspace is served by provider "local" and its host is not
    # DESTROYED, so disabling "local" must be rejected with a 409 and no write.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.patch("/api/v1/desktop/providers/local", headers=_auth_header(), json={"enabled": False})

    assert response.status_code == 409
    assert "active workspace" in json.loads(response.data)["error"].lower()


@pytest.mark.parametrize(
    "body",
    [
        # missing 'enabled' key -> enabled is None
        {},
        # wrong type (string)
        {"enabled": "yes"},
        # wrong type (int, must not be accepted via truthiness)
        {"enabled": 1},
        # non-object JSON body
        [1, 2, 3],
    ],
)
def test_patch_provider_rejects_invalid_body(tmp_path: Path, body: object) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.patch("/api/v1/desktop/providers/docker", headers=_auth_header(), json=body)

    # Structural validation (required strict bool) is now enforced by spectree,
    # so a missing/wrong-typed ``enabled`` yields the uniform 422 contract.
    assert response.status_code == 422
    assert "errors" in json.loads(response.data)


def test_patch_provider_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.patch("/api/v1/desktop/providers/docker", json={"enabled": True})

    assert response.status_code == 401


# -- Desktop running-workspaces / stop-hosts / state-container --


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


# -- Sharing sub-resource --


def _sharing_client(
    tmp_path: Path,
    agent_id: AgentId,
    cli: FakeSharingCli,
    *,
    user_id: str = "33333333-3333-3333-3333-333333333333",
    email: str = "owner@example.com",
    service_logs: dict[str, str] | None = None,
    mngr_binary: str = "mngr",
) -> FlaskClient:
    resolver = make_resolver_with_data(make_agents_json(agent_id), service_logs=service_logs)
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email=email)
    return _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store, mngr_binary=mngr_binary)


def _fake_sharing_cli(tunnel: TunnelInfo | None = None, **kwargs: Any) -> FakeSharingCli:
    return FakeSharingCli(
        connector_url=FAKE_CONNECTOR_URL,
        tunnel=tunnel,
        **kwargs,
    )


def test_sharing_status_enabled(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", services=("web",)),
        service_entries=[{"service_name": "web", "hostname": "share.example.com"}],
        service_auth={"emails": ["owner@example.com"]},
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["url"] == "https://share.example.com"
    assert body["policy"]["emails"] == ["owner@example.com"]


def test_sharing_status_disabled_when_no_tunnel(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(tunnel=None)
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False


def test_sharing_enable_returns_json(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", token=SecretStr("token"), services=("web",))
    )
    # The tunnel-token injection runs `mngr exec` through ``cli.mngr_caller``,
    # which the fake CLI defaults to an in-memory RecordingMngrCaller -- a fast
    # no-op, so no real ``mngr`` process is spawned.
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={str(agent_id): make_service_log("web", "http://127.0.0.1:9000")},
    )

    response = client.put(
        f"/api/v1/workspaces/{agent_id}/sharing/web",
        headers=_auth_header(),
        json={"emails": ["viewer@example.com"]},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    # The enable response carries the share URL so the editor can start the
    # readiness poll without a follow-up status fetch.
    assert body["url"] == "https://share.example.com"
    assert "web" in cli.added_services
    assert cli.enabled_policies == [{"emails": ["viewer@example.com"]}]


def test_sharing_enable_translates_transient_cloudflare_access_error(tmp_path: Path) -> None:
    # A Cloudflare Access-API 5xx that escapes the connector's retries should
    # read as "temporary problem, try again", not a raw exit-code error.
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", token=SecretStr("token"), services=()),
    )
    cli.enable_sharing_error_stderr = (
        '{"error": "Connector error 500: {\\"detail\\":{\\"errors\\":[{\\"code\\":10001,'
        '\\"message\\":\\"access.api.error.internal_server_error\\"}]}}"}'
    )
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={str(agent_id): make_service_log("web", "http://127.0.0.1:9000")},
    )

    response = client.put(
        f"/api/v1/workspaces/{agent_id}/sharing/web",
        headers=_auth_header(),
        json={"emails": ["viewer@example.com"]},
    )

    assert response.status_code == 502
    body = json.loads(response.data)
    assert "temporary problem" in body["error"]
    assert "try again" in body["error"]
    assert "exit 1" not in body["error"]


def test_sharing_enable_rejects_empty_emails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An enable with no emails is rejected (422) rather than silently creating an
    # unprotected, never-ready public share: the request is refused before any
    # tunnel/service side effects.
    agent_id = AgentId()
    cli = _fake_sharing_cli(tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", token=SecretStr("token"), services=()))
    fake_mngr_dir = tmp_path / "bin"
    _write_fake_mngr(fake_mngr_dir)
    monkeypatch.setenv("PATH", f"{fake_mngr_dir}{os.pathsep}{os.environ['PATH']}")
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={str(agent_id): make_service_log("web", "http://127.0.0.1:9000")},
    )

    response = client.put(
        f"/api/v1/workspaces/{agent_id}/sharing/web",
        headers=_auth_header(),
        json={"emails": []},
    )

    assert response.status_code == 422
    assert not cli.added_services


def test_sharing_disable_returns_json(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", services=("web",)))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.delete(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False
    assert "web" in cli.removed_services


def test_sharing_status_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web")

    assert response.status_code == 401


def test_sharing_readiness_reports_ready_on_access_redirect(tmp_path: Path) -> None:
    agent_id = AgentId()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/login"})

    http_client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, http_client=http_client)

    response = client.get(
        f"/api/v1/workspaces/{agent_id}/sharing/web/readiness?url=https://share.example.com",
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"ready": True}


def test_sharing_readiness_not_ready_without_http_client(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(
        f"/api/v1/workspaces/{agent_id}/sharing/web/readiness?url=https://share.example.com",
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"ready": False}


# -- Workspace recovery: health probe + restart --


def _resolver_with_services_agent(agent_id: AgentId, services_id: AgentId) -> BackendResolverInterface:
    """Build a resolver where ``agent_id`` and a ``system-services`` peer share a host.

    The restart worker resolves the system-services agent on the workspace's host;
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


def test_workspace_health_returns_probes_for_known_workspace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # A known workspace returns the flat HostHealthResponse the recovery page
    # renders: a probe list plus the derived dispatch tier.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.get(f"/api/v1/workspaces/{agent_id}/health", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert isinstance(body["probes"], list)
    assert "dispatch_tier" in body


def test_workspace_health_unknown_workspace_returns_404(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    resolver = make_resolver_with_data(make_agents_json(AgentId()))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.get(f"/api/v1/workspaces/{AgentId()}/health", headers=_auth_header())

    assert response.status_code == 404


def test_workspace_health_requires_bearer(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.get(f"/api/v1/workspaces/{agent_id}/health")

    assert response.status_code == 401


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
    with an already-exited group). The route has already claimed RESTARTING, so
    it must roll that into RESTART_FAILED, fail the registry operation (so the
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
    assert tracker.get_health(agent_id) == AgentHealth.RESTART_FAILED
    record = get_state(client.application).workspace_operation_registry.get(agent_id)
    assert record is not None and record.status == WorkspaceOperationStatus.FAILED
    assert len(error_records) == 1, error_records


def _wait_for_restart_worker_and_get_status(client: FlaskClient, agent_id: AgentId) -> dict[str, Any]:
    """Drain the restart worker's log queue to its terminal sentinel, then fetch the status.

    Waits for the dispatched restart worker to finish (condition-based, no arbitrary
    sleeps) and returns the parsed body of the typed restart-operation resource,
    asserting the resource responds 200.
    """
    registry = get_state(client.application).workspace_operation_registry
    log_queue = registry.get_log_queue(agent_id)
    assert log_queue is not None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if log_queue.get(timeout=15.0) == OPERATION_LOG_SENTINEL:
            break
    status_resp = client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header())
    assert status_resp.status_code == 200
    return json.loads(status_resp.data)


def test_restart_dispatches_for_never_probed_workspace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A recovery-page dispatch for a never-probed workspace must actually restart.

    A workspace whose host has been offline since before this process started is
    never enrolled as a probe suspect, so the tracker reports default-HEALTHY for
    it. A veto keyed on that reading would drop the recovery page's
    unconditional entry dispatch (host scope + ``start_only``), stranding the
    workspace on the loader forever. The dispatch must proceed to a real restart
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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))

    running = json.loads(client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data)
    assert running["kind"] == "restart"
    assert running["status"] == "RUNNING"
    assert running["is_done"] is False

    registry.complete(agent_id)
    done = json.loads(client.get(f"/api/v1/workspaces/operations/restart/{agent_id}", headers=_auth_header()).data)
    assert done["is_done"] is True
    assert done["status"] == "DONE"


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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    registry.append_log(agent_id, "restarting now")
    registry.complete(agent_id)

    response = client.get(f"/api/v1/workspaces/operations/restart/{agent_id}/logs", headers=_auth_header())

    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "restarting now" in text
    assert '"done": true' in text


# -- Backup service routes --


def test_workspace_backups_reports_offline_workspace_with_verification_enabled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # With no discovery host-state data the workspace reads OFFLINE, so the
    # route answers from local data alone (no exec into the workspace).
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 200
    entry = json.loads(response.data)
    assert entry["agent_id"] == str(agent_id)
    assert entry["check_state"] == "OFFLINE"
    assert entry["problems"] == []
    assert entry["is_verification_enabled"] is True


def test_workspace_backups_reports_disabled_verification_without_exec(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # Verification disabled: no exec runs and the check half reports DISABLED.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    set_backup_verification_enabled(WorkspacePaths(data_dir=tmp_path / "minds"), agent_id, False)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 200
    entry = json.loads(response.data)
    assert entry["check_state"] == "DISABLED"
    assert entry["is_verification_enabled"] is False


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
    # Any RUNNING operation for the workspace (here a restart) makes a second
    # dispatch a 409 instead of stacking a second worker.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert "RESTART" in json.loads(response.data)["error"]
    # The dispatch did not replace the running record.
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.RESTART


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
    assert "BACKUP_UPDATE" in json.loads(response.data)["error"]
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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    assert client.post(cancel_url, headers=_auth_header()).status_code == 404


def test_backup_service_update_cancel_flags_a_running_update(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update/cancel", headers=_auth_header())

    assert response.status_code == 200
    assert registry.is_cancel_requested(agent_id) is True


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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))

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
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
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
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
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
