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
from uuid import uuid4

import httpx
import pytest
from flask.testing import FlaskClient
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreateAttemptErrorKind
from imbue.minds.desktop_client.api_v1 import _drain_backup_summary_rows
from imbue.minds.desktop_client.api_v1 import _stream_workspace_backup_summaries
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_update import BLOCKED_BY_RUNNING_CHATS_PREFIX
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
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
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.errors import WorkspaceNameInUseError
from imbue.minds.primitives import CreateAttemptId
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
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
        account_id: str = "",
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
            "color": color,
            "docker_runtime": docker_runtime,
            "original_minds_version": original_minds_version,
            "account_id": account_id,
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
    ``start_create_attempt`` is ever called; the happy-path tests pass a
    :class:`_RecordingAgentCreator` so the route's call is captured without
    starting a real background create attempt (subprocess / network).
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
        WorkspacePaths(data_dir=tmp_path / "minds"),
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
        WorkspacePaths(data_dir=tmp_path / "minds"),
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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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
            WorkspacePaths(data_dir=tmp_path / "minds"),
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
            WorkspacePaths(data_dir=tmp_path / "minds"),
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
        WorkspacePaths(data_dir=tmp_path / "minds"),
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
    create_attempt_id = CreateAttemptId()
    creator = _StatusReportingAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreateAttemptInfo(
            create_attempt_id=create_attempt_id,
            status=AgentCreateAttemptStatus.INITIALIZING,
            launch_mode=LaunchMode.DOCKER,
        ),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A failed create attempt whose error was classified (e.g. a private GitHub repo
    # the local git credentials cannot see) reports the machine-readable kind
    # alongside the error message; the creating page gates its static sign-in
    # guidance on it.
    create_attempt_id = CreateAttemptId()
    creator = _StatusReportingAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_info=AgentCreateAttemptInfo(
            create_attempt_id=create_attempt_id,
            status=AgentCreateAttemptStatus.FAILED,
            launch_mode=LaunchMode.DOCKER,
            error="git clone failed:\nfatal: could not read Username for 'https://github.com'",
            error_kind=CreateAttemptErrorKind.GITHUB_AUTH_REQUIRED,
        ),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

    response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}", headers=_auth_header())

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


def test_create_workspace_ignores_stale_ai_provider_field(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # The AI provider moved into the workspace's own sign-in modal. A stale
    # client still sending ai_provider (even a value that used to require an
    # API key) must be accepted -- the field is silently ignored.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "ai_provider": "API_KEY"},
    )

    assert response.status_code == 202


def test_create_workspace_rejects_invalid_backup_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A malformed backup_provider is a structural (enum) failure, so spectree
    # rejects it up front with the uniform 422 contract, before any background
    # create attempt is started.
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
    # 400 that mentions the account, before any background create attempt starts.
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
    set_backup_verification_enabled(WorkspacePaths(data_dir=tmp_path / "minds"), agent_id, False)

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
    # Any RUNNING operation for the workspace (here a restart) makes a second
    # dispatch a 409 instead of stacking a second worker.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)
    registry = get_state(client.application).workspace_operation_registry
    registry.start(agent_id, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))

    response = client.post(f"/api/v1/workspaces/{agent_id}/backup-service/update", headers=_auth_header(), json={})

    assert response.status_code == 409
    assert "restart is already in progress" in json.loads(response.data)["error"]
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
        WorkspacePaths(data_dir=tmp_path / "minds"),
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


def test_cloud_account_routes_gated_off_by_default(tmp_path: Path) -> None:
    # The bring-your-own-key cloud feature ships dark: with the flag unset
    # (default), both account routes are refused (403) even with valid auth and a
    # well-formed body -- the feature is unreachable by URL, not merely hidden.
    client = _client_with_workspace(tmp_path, AgentId())
    create = client.post(
        "/api/v1/desktop/cloud-accounts",
        headers=_auth_header(),
        json={
            "backend": "aws",
            "alias": "mine",
            "region": "us-east-1",
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "s",
        },
    )
    assert create.status_code == 403
    delete = client.delete("/api/v1/desktop/cloud-accounts/byok-aws-mine", headers=_auth_header())
    assert delete.status_code == 403


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
        on_created: Callable[[AgentId, HostId], None] | None = None,
        backup_request: BackupSetupRequest | None = None,
        color: str | None = None,
        docker_runtime: DockerRuntime = DockerRuntime.RUNC,
        original_minds_version: str = "",
        account_id: str = "",
    ) -> CreateAttemptId:
        raise WorkspaceNameInUseError(
            "A workspace named 'contended' is already being created. "
            "Wait for that create attempt to finish or pick a different name."
        )


def test_create_workspace_in_flight_name_conflict_returns_409(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    creator = _NameConflictAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, agent_creator=creator
    )

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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # ``workspace-1`` is known to discovery and ``workspace-2`` is held by a
    # live in-flight create attempt (invisible to discovery); the auto-namer must
    # skip both and pick ``workspace-3``.
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"is_primary": "true"}, host_name="workspace-1"),
    )
    creator = _InFlightNamesRecordingCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_in_flight_names=("workspace-2",),
    )
    client = _client_with_agent_creator(
        tmp_path, root_concurrency_group, notification_dispatcher, resolver=resolver, agent_creator=creator
    )

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 202
    assert str(creator.last_call["host_name"]) == "workspace-3"


def test_create_workspace_threads_account_id_to_start_create_attempt(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # The account id must reach start_create_attempt so the pending-create-attempt record
    # (the crash-safe association) can carry it.
    fake_cli = make_fake_imbue_cloud_cli()
    fake_cli.add_account(user_id="user-77120", email="user-77120@example.com")
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=fake_cli)
    creator = _make_recording_creator(tmp_path, root_concurrency_group, notification_dispatcher)
    client = _client_with_agent_creator(
        tmp_path,
        root_concurrency_group,
        notification_dispatcher,
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
