import os
import queue
import subprocess
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
from flask import Response
from flask.testing import FlaskClient
from pydantic import SecretStr

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import _build_requests_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _collect_remote_workspace_tiles
from imbue.minds.desktop_client.app import _finalize_and_mark_destroying
from imbue.minds.desktop_client.app import _ssh_command_for_agent
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.conftest import DEFAULT_SERVICE_NAME
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.dek_store import bundle_mirror_path
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.dek_store import verify_master_password_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServiceName
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


def _create_test_desktop_client(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.Client | None,
    agent_creator: AgentCreator | None = None,
) -> tuple[FlaskClient, FileAuthStore]:
    """Create a desktop client with the given backend resolver."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=http_client,
        agent_creator=agent_creator,
    )
    client = app.test_client()

    return client, auth_store


def _setup_test_server(
    tmp_path: Path,
    service_name: ServiceName = DEFAULT_SERVICE_NAME,
) -> tuple[FlaskClient, FileAuthStore, AgentId]:
    """Set up a desktop client with a test backend for proxy testing."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(service_name): "http://test-backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    return client, auth_store, agent_id


def _authenticate_client(
    client: FlaskClient,
    auth_store: FileAuthStore,
) -> None:
    """Authenticate a test client by minting a signed session cookie and adding it to the jar.

    The production path (GET /authenticate?one_time_code=...) sets a host-only
    ``minds_session`` cookie on the bare origin (workspace subdomains get their
    own session via the forward server's /goto/ auth bridge, not this cookie).
    Setting the cookie directly on the jar skips the /authenticate round-trip
    and its one-time-code bookkeeping in tests that only care about being
    signed in. The server-side logic the test is exercising is independent of
    the Set-Cookie emission path; the bare presence/signature of the cookie is
    what ``_is_authenticated`` checks.
    """
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)


def test_authenticate_without_one_time_code_returns_422(tmp_path: Path) -> None:
    """A missing one_time_code is a 422, not a 500."""
    client, _, _ = _setup_test_server(tmp_path)
    response = client.get("/authenticate", follow_redirects=False)
    assert response.status_code == 422


def test_authenticate_with_valid_code_sets_cookie_and_redirects(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert any(SESSION_COOKIE_NAME in header for header in response.headers.getlist("Set-Cookie"))


def test_authenticate_redirects_to_landing_page(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("auth-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_authenticate_with_invalid_code_returns_403(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get(
        "/authenticate",
        query_string={"one_time_code": "bogus-code-82734"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "invalid or has already been used" in response.text


def test_authenticate_code_cannot_be_reused(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("once-only-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    first_response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert first_response.status_code == 307

    second_response = client.get(
        "/authenticate",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )
    assert second_response.status_code == 403


def test_post_login_redirects_to_create_when_no_workspaces(tmp_path: Path) -> None:
    """A just-signed-in user with no machines lands on the create screen (/)."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_post_login_redirects_to_accounts_when_workspaces_exist(tmp_path: Path) -> None:
    """A returning user who already has machines lands on the accounts page."""
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {"web": "http://backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/accounts"


def test_post_login_redirects_to_login_when_unauthenticated(tmp_path: Path) -> None:
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_post_login_honors_safe_return_to(tmp_path: Path) -> None:
    """A ``return_to`` (e.g. /create, from the remote-preset sign-in flow) wins."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", query_string={"return_to": "/create"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/create"


def test_post_login_ignores_unsafe_return_to(tmp_path: Path) -> None:
    """An off-origin ``return_to`` is ignored and the default destination is used."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", query_string={"return_to": "https://evil.com"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


# -- Leased imbue_cloud host account-binding tests --


def test_login_redirects_if_already_authenticated(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    new_code = OneTimeCode("second-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=new_code)

    response = client.get(
        "/login",
        query_string={"one_time_code": str(new_code)},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_unhandled_exception_returns_500_with_message(tmp_path: Path) -> None:
    """Unhandled exceptions in routes produce a 500 response with the error message."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    @app.get("/explode")
    def explode() -> Response:
        raise RuntimeError("test boom")

    client = app.test_client()
    response = client.get("/explode")
    assert response.status_code == 500
    assert "test boom" in response.text


# -- Workspace-list / destroying-marker derivation helpers --


def test_build_workspace_list_returns_workspaces_for_the_channel(tmp_path: Path) -> None:
    """``_build_workspace_list`` surfaces each resolver-known workspace as a payload row.

    The rows it builds are what the ``workspaces`` channel message (and the
    bootstrap snapshot) are derived from.
    """
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(DEFAULT_SERVICE_NAME): "http://test-backend"}},
    )

    workspaces = _build_workspace_list(backend_resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == str(agent_id)


def test_destroying_marker_includes_ids_with_live_destroy(tmp_path: Path) -> None:
    """An agent with an alive destroy pid + still in the resolver shows up as running.

    main.js keys its "ok to navigate the user away from this machine"
    decision off this list, so the helper must surface every in-flight or
    failed destroy id whose marker dir exists on disk.
    """
    agent_id = AgentId()
    paths = WorkspacePaths(data_dir=tmp_path)
    destroying_dir = tmp_path / "destroying" / str(agent_id)
    destroying_dir.mkdir(parents=True)
    # The current process pid is alive, so the helper sees the destroy as
    # RUNNING (rather than DONE/FAILED, which would still be a valid hit but
    # the running case is the most direct check).
    (destroying_dir / "pid").write_text(str(os.getpid()))
    (destroying_dir / "output.log").write_text("destroy in flight...\n")

    # The pid is alive, so the record is RUNNING regardless of host state; an
    # empty resolver is enough to drive the helper.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    marker = _finalize_and_mark_destroying(paths, backend_resolver, None, None)
    assert marker == {str(agent_id): "running"}


def test_destroying_marker_returns_empty_when_paths_is_none() -> None:
    """The test-server helper builds a minimal app without WorkspacePaths;
    the helper must tolerate that without raising."""
    assert _finalize_and_mark_destroying(None, StaticBackendResolver(url_by_agent_and_service={}), None, None) == {}


def _write_dead_destroy_dir(paths: WorkspacePaths, agent_id: AgentId, host_id: HostId) -> None:
    """Create a destroying/<agent_id>/ dir whose wrapper pid is already dead.

    Spawns and reaps a trivial child so its pid is reliably not alive, then
    writes the same three files ``start_destroy`` would (pid, host_id, log).
    """
    dir_path = paths.data_dir / "destroying" / str(agent_id)
    dir_path.mkdir(parents=True)
    proc = subprocess.Popen(["true"])
    proc.wait()
    (dir_path / "pid").write_text(f"{proc.pid}\n")
    (dir_path / "host_id").write_text(f"{host_id}\n")
    (dir_path / "output.log").write_text("done\n")


def test_finalize_and_mark_destroying_finalizes_when_host_gone(tmp_path: Path) -> None:
    """A finished destroy whose host is gone is DONE: the record is tombstoned.

    Finalization happens only once the host is actually gone, not
    synchronously on click. The record is kept (state=DESTROYED, secrets
    intact) so the machine's backups stay reachable, but it no longer
    reads as the machine's owner.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    # Resolver knows no active agents and reports no host state -> the host is
    # gone -> the destroy is DONE.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {}
    assert not (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is None
    # The tombstone survives (with its metadata) for future backup access.
    assert session_store.record_store is not None
    records = session_store.record_store.list_records("user-1")
    assert len(records) == 1
    assert records[0].state == "destroyed"


def test_finalize_and_mark_destroying_keeps_failed_when_host_still_up(tmp_path: Path) -> None:
    """A finished destroy whose host is still up is FAILED: kept + stays associated.

    The machine must remain visible and owned so the user can retry, instead
    of vanishing while its host keeps running (and billing).
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="kept",
        color=None,
        is_cloud_row=False,
    )
    # Resolver still lists the workspace agent as active -> host still up -> FAILED.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {str(agent_id): "failed"}
    assert (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is not None


def test_remote_tiles_wait_for_the_initial_discovery_snapshot(tmp_path: Path) -> None:
    """No record renders as a remote tile until discovery has produced its first snapshot.

    Before that, local knowledge is empty and every record -- including this
    device's own machines -- would misclassify as a greyed remote tile.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id="agent-elsewhere",
        host_id="host-elsewhere",
        display_name="remote-ws",
        color=None,
        is_cloud_row=False,
    )

    undiscovered_resolver = MngrCliBackendResolver()
    assert _collect_remote_workspace_tiles(undiscovered_resolver, session_store) == []

    discovered_resolver = make_resolver_with_data(agents_json=make_agents_json(AgentId.generate()))
    tiles = _collect_remote_workspace_tiles(discovered_resolver, session_store)
    assert [tile.agent_id for tile in tiles] == ["agent-elsewhere"]


class _AllAgentsKnownStaticResolver(StaticBackendResolver):
    """Reports every queried agent as a known, host-resolvable agent.

    The inbox display filters out requests whose agent can't be resolved
    to a host (see ``_displayable_pending_requests``). These tests cover
    the running-workspace case where every agent resolves, so the resolver
    claims to know any agent it's asked about.
    """

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")


def test_build_requests_payload_empty_inbox() -> None:
    """An empty inbox yields a zero count and no pending ids."""
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    expected = {"count": 0, "request_ids": []}
    assert _build_requests_payload(None, resolver) == expected
    assert _build_requests_payload(RequestInbox(), resolver) == expected


def test_build_requests_payload_carries_pending_ids() -> None:
    """A pending request surfaces its event_id alongside the count."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="post updates"
    )
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    payload = _build_requests_payload(RequestInbox().add_request(event), resolver)
    assert payload["count"] == 1
    assert payload["request_ids"] == [str(event.event_id)]


def test_build_requests_payload_distinguishes_equal_count_different_contents() -> None:
    """A swap of the pending set at constant size changes the payload.

    This is the soundness property: keying live updates off the bare count
    would miss this transition (count stays 1), so the payload must differ.
    """
    agent_id = str(AgentId())
    request_a = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="a"
    )
    request_b = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="github-api", rationale="b"
    )

    inbox_with_a = RequestInbox().add_request(request_a)
    # Resolve A and add B: the pending set becomes {B}, same size as {A}.
    inbox_with_b = inbox_with_a.add_response(
        create_request_response_event(
            request_event_id=str(request_a.event_id),
            status=RequestStatus.GRANTED,
            agent_id=agent_id,
            request_type=request_a.request_type,
            scope="slack-api",
        )
    ).add_request(request_b)

    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    payload_a = _build_requests_payload(inbox_with_a, resolver)
    payload_b = _build_requests_payload(inbox_with_b, resolver)
    assert payload_a["count"] == payload_b["count"] == 1
    assert payload_a != payload_b
    assert payload_b["request_ids"] == [str(request_b.event_id)]


# -- Tests for new account management and request routes --


def _create_test_client_with_stores(
    tmp_path: Path,
    cli: ImbueCloudCli | None = None,
    mngr_caller: MngrCaller | None = None,
    # When set, also wired into the app state as ``imbue_cloud_cli`` so routes
    # that reach the connector through ``get_state().imbue_cloud_cli`` (e.g.
    # the accounts plan-view fragment) hit the fake instead of degrading.
    imbue_cloud_cli: ImbueCloudCli | None = None,
    # When set, wired into the app state so routes that reach the backup
    # reaper through ``get_state().sync_scheduler.backup_reaper`` work.
    sync_scheduler: WorkspaceSyncScheduler | None = None,
) -> tuple[FlaskClient, FileAuthStore]:
    """Create a desktop client with session store and config for testing new routes.

    ``cli`` is forwarded to :func:`make_session_store_for_test` so callers
    can seed the session store with specific accounts; defaults to a
    fresh empty fake CLI. ``mngr_caller`` injects a fake mngr CLI caller (e.g.
    :class:`RecordingMngrCaller`) so routes that shell out (``/help/assist``) can be
    exercised without a real warm process.
    """
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    minds_config = MindsConfig(data_dir=tmp_path)
    request_inbox = RequestInbox()

    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
        mngr_caller=mngr_caller,
        imbue_cloud_cli=imbue_cloud_cli,
        sync_scheduler=sync_scheduler,
    )
    client = app.test_client()
    return client, auth_store


def test_accounts_listing_shows_logged_in_accounts(tmp_path: Path) -> None:
    """The accounts listing the SPA renders carries every logged-in account."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)

    response = client.get("/ui/api/accounts")
    assert response.status_code == 200
    assert "test@example.com" in response.get_data(as_text=True)


def test_account_plan_modal_unknown_account_returns_404(tmp_path: Path) -> None:
    """A user id with no signed-in account is a 404, not a blank modal."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)

    response = client.get("/accounts/user-does-not-exist/plan-modal")

    assert response.status_code == 404


def test_error_reporting_settings_endpoint_persists_toggle(tmp_path: Path) -> None:
    """POST /_chrome/error-reporting persists the single report_unexpected_errors flag live."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)

    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": False}).status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is False

    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": True}).status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is True


def test_error_reporting_settings_endpoint_requires_auth(tmp_path: Path) -> None:
    """POST /_chrome/error-reporting rejects an unauthenticated request and records nothing."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/error-reporting", json={"report_unexpected_errors": False})
    assert response.status_code == 403
    assert MindsConfig(data_dir=tmp_path).get_report_unexpected_errors() is True


def test_sharing_urls_redirect_to_the_options_panels_share_tab(tmp_path: Path) -> None:
    """Legacy /sharing/<id> URLs land on the Share machine pane, not a 404.

    The standalone sharing editor is gone -- the workspace options panel's
    Share tab is the one sharing surface -- but its URLs were handed out, so
    they redirect. A service segment picks that share target.
    """
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    agent_id = str(AgentId.generate())

    response = client.get(f"/sharing/{agent_id}")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share"

    service_response = client.get(f"/sharing/{agent_id}/frontend")
    assert service_response.status_code == 302
    assert service_response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share&target=frontend"

    modal_response = client.get(f"/sharing/{agent_id}/frontend/modal")
    assert modal_response.status_code == 302
    assert modal_response.headers["Location"] == f"/workspace/{agent_id}/options?tab=share&target=frontend"


# -- Workspace options panel routes --


def test_old_requests_panel_route_removed(tmp_path: Path) -> None:
    """The legacy panel route no longer exists."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/_chrome/requests-panel")
    assert response.status_code == 404


def test_old_requests_page_route_removed(tmp_path: Path) -> None:
    """The legacy standalone request page no longer exists."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/requests/evt-anything")
    assert response.status_code == 404


def test_set_default_account(tmp_path: Path) -> None:
    """Setting a default account works correctly."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post(
        "/accounts/set-default",
        data={"user_id": "user-default-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_default_account_id() == "user-default-123"


# -- error-reporting consent + settings tests --


def test_consent_page_requires_auth(tmp_path: Path) -> None:
    """GET /consent bounces an unauthenticated request to the login page."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/consent")
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_consent_submit_requires_auth(tmp_path: Path) -> None:
    """POST /consent rejects an unauthenticated request and records nothing."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/consent", json={})
    assert response.status_code == 403
    assert MindsConfig(data_dir=tmp_path).get_error_reporting_consent_given() is False


def test_post_login_routes_to_landing_while_consent_unanswered(tmp_path: Path) -> None:
    """While consent is unanswered, post-login routes to "/" (which shows consent), not /accounts."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_backup_password_change_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/backup-password", json={"new_password": "x", "new_password_confirm": "x"})
    assert response.status_code == 403


def test_backup_password_change_rejects_mismatched_confirmation(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/backup-password", json={"new_password": "one", "new_password_confirm": "two"})
    assert response.status_code == 400
    assert "match" in response.get_json()["error"]
    assert not bundle_mirror_path(WorkspacePaths(data_dir=tmp_path), "user-1").exists()


def test_backup_password_change_requires_a_signed_in_account(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/backup-password", json={"new_password": "x", "new_password_confirm": "x"})
    assert response.status_code == 400
    assert "Sign in" in response.get_json()["error"]


def test_backup_password_change_wraps_the_dek_and_pushes_the_bundle(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    paths = WorkspacePaths(data_dir=tmp_path)

    response = client.post(
        "/_chrome/backup-password",
        json={"new_password": "brand-new", "new_password_confirm": "brand-new"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["results"] == [{"account": "a@b.com", "is_ok": True, "error": None}]
    assert verify_master_password_for_account(paths, "user-1", SecretStr("brand-new")) is True
    assert verify_master_password_for_account(paths, "user-1", SecretStr("")) is False
    # The wrapped bundle was pushed to the (fake) connector.
    assert "a@b.com" in cli.sync_bundle_by_email


def test_backup_password_change_may_return_to_the_empty_password(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    paths = WorkspacePaths(data_dir=tmp_path)
    assert (
        client.post(
            "/_chrome/backup-password", json={"new_password": "temp", "new_password_confirm": "temp"}
        ).status_code
        == 200
    )

    response = client.post("/_chrome/backup-password", json={"new_password": "", "new_password_confirm": ""})

    assert response.status_code == 200
    assert verify_master_password_for_account(paths, "user-1", SecretStr("")) is True
    # Clearing scrubs the server: no bundle remains on the (fake) connector.
    assert "a@b.com" not in cli.sync_bundle_by_email


def test_backup_password_change_refuses_accounts_locked_on_this_device(tmp_path: Path) -> None:
    """Rewrapping a locked account would mint a fresh DEK and overwrite the
    server bundle wrapping the real one, orphaning every synced secret -- the
    change endpoint must report a failure and touch nothing instead."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # Another device set a password and synced a secrets-carrying record; this
    # device has no DEK for the account (it is locked here).
    other_device = WorkspacePaths(data_dir=tmp_path / "other-device")
    bundle = set_master_password_for_account(other_device, "user-1", SecretStr("hunter2"))
    assert bundle is not None
    cli.sync_bundle_push("a@b.com", bundle)
    remote = ReplicaRecord(
        host_id="host-remote-1",
        agent_id=str(AgentId.generate()),
        display_name="remote-ws",
        provider_kind="lima",
        hosting_device_id="device-other",
        device_label="other-device",
        encrypted_secrets="b3BhcXVl",
    )
    cli.sync_records_by_email["a@b.com"] = {"host-remote-1": remote.to_wire(1)}

    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    session_store = get_state(client.application).session_store
    assert session_store is not None and session_store.record_store is not None
    session_store.record_store.pull("user-1", "a@b.com")
    bundle_before = dict(cli.sync_bundle_by_email["a@b.com"])

    response = client.post(
        "/_chrome/backup-password", json={"new_password": "new-pass", "new_password_confirm": "new-pass"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False
    assert body["results"] == [{"account": "a@b.com", "is_ok": False, "error": body["results"][0]["error"]}]
    assert "locked" in body["results"][0]["error"]
    # The server bundle (wrapping the real DEK) is untouched and no divergent
    # local DEK was minted.
    assert cli.sync_bundle_by_email["a@b.com"] == bundle_before
    assert not is_account_unlocked(WorkspacePaths(data_dir=tmp_path), "user-1")


# -- get-help / report-a-bug tests --


def test_help_assist_requires_a_workspace(tmp_path: Path) -> None:
    """Agent help is only available inside a machine, so a request without one is rejected."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "it broke"})
    assert response.status_code == 400


def test_help_assist_requires_a_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "  ", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 400


def test_help_assist_refuses_a_workspace_without_the_assist_skill(tmp_path: Path) -> None:
    """A machine from an older DEFAULT_WORKSPACE_TEMPLATE (no /assist skill) is refused up front (409) rather than spawning
    a chat that would hang on the unknown ``/assist`` command -- and no ``mngr create`` is attempted."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_ABSENT\n"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 409
    assert "agent-assist skill" in response.get_json()["error"]
    # Only the probe ran; we never attempted to create the chat.
    assert len(caller.calls) == 1
    assert caller.calls[0][0] == "exec"


def test_help_assist_reports_unreachable_workspace(tmp_path: Path) -> None:
    """When the probe can't run (no sentinel -- host down/timeout), we return 502 rather than guess."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="connection refused"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 502
    assert len(caller.calls) == 1


def test_help_assist_spawns_when_the_skill_is_present(tmp_path: Path) -> None:
    """A supported machine probes clean, then the chat is created (probe call + create call)."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_PRESENT\n"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 200
    # First the skill probe, then the inner ``mngr create``.
    assert len(caller.calls) == 2
    assert caller.calls[0][0] == "exec"
    assert caller.calls[1][:2] == ["exec", "--agent"]
    assert "mngr create" in caller.calls[1][3]


def test_help_report_requires_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/report", json={"description": "  "})
    assert response.status_code == 400


def test_help_report_accepts_a_description(tmp_path: Path) -> None:
    # Sentry is not initialized in tests, so the report is collected and the route returns ok with a
    # null event_id (nothing was actually transmitted). This exercises the full collect path end to end.
    client, _ = _create_test_client_with_stores(tmp_path)
    # App diagnostics are always collected server-side now; the request need not opt in.
    response = client.post(
        "/help/report",
        json={"description": "the app froze", "remote_access": True},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["event_id"] is None


def _create_test_client_with_api_key(tmp_path: Path, api_key: str) -> FlaskClient:
    """Build a client with the /api/v1 blueprint mounted and a known central API key."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = make_session_store_for_test(tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        paths=WorkspacePaths(data_dir=tmp_path),
        minds_api_key=api_key,
    )
    return app.test_client()


def test_api_v1_bug_report_requires_bearer_token(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(f"/api/v1/agents/{AgentId()}/report", json={"description": "boom"})
    assert response.status_code == 401


def test_api_v1_bug_report_opens_prefilled_modal_instead_of_submitting(tmp_path: Path) -> None:
    """The agent report route does not submit to Sentry: it asks the app to open the report modal
    pre-filled with the agent's description, scoped to the caller's own machine."""
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    agent_id = AgentId()
    event_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    wake_event = threading.Event()
    get_state(client.application).chrome_event_broadcaster.subscribe(event_queue, wake_event)
    response = client.post(
        f"/api/v1/agents/{agent_id}/report",
        json={"description": "agent saw an error"},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    # No Sentry submission happens here, so there is no event_id to return.
    assert "event_id" not in body
    # The route broadcast an open_help SSE payload (scoped to the caller's workspace) instead of submitting.
    assert wake_event.is_set()
    assert event_queue.get_nowait() == {
        "type": "open_help",
        "description": "agent saw an error",
        "workspace_agent_id": str(agent_id),
    }


def test_api_v1_bug_report_rejects_empty_description(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(
        f"/api/v1/agents/{AgentId()}/report",
        json={"description": ""},
        headers={"Authorization": "Bearer secret-key"},
    )
    # An empty description fails the request model's min-length structurally, so
    # it is rejected with the uniform 422 validation contract.
    assert response.status_code == 422
    assert any(error["field"] == "description" for error in response.get_json()["errors"])


# -- system-interface restart + recovery tests --


def test_ssh_command_for_agent_builds_command_from_resolver() -> None:
    """_ssh_command_for_agent renders the resolver's SSH info as a runnable command."""
    agent_id = AgentId()
    resolver = StaticBackendResolver(
        url_by_agent_and_service={},
        ssh_info_by_agent_id={
            str(agent_id): RemoteSSHInfo(user="root", host="127.0.0.1", port=60022, key_path=Path("/home/u/.mngr/key"))
        },
    )
    assert _ssh_command_for_agent(resolver, agent_id) == "ssh -i /home/u/.mngr/key -p 60022 root@127.0.0.1"


def test_ssh_command_for_agent_returns_none_without_ssh_info() -> None:
    """An agent the resolver has no SSH info for yields no command (button is then omitted)."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert _ssh_command_for_agent(resolver, AgentId()) is None


def test_create_desktop_client_stashes_system_interface_health_tracker(tmp_path: Path) -> None:
    """create_desktop_client should expose the tracker on the app state for handlers."""
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    tracker = SystemInterfaceHealthTracker()
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        system_interface_health_tracker=tracker,
    )

    assert get_state(app).system_interface_health_tracker is tracker


# -- sync unlock / remove-record tests --


def test_sync_unlock_installs_the_dek_for_a_locked_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # Another device set a password and synced a workspace with secrets: the
    # bundle + a secret-carrying record exist on the (fake) connector, but
    # this device has no DEK file.
    other_device = WorkspacePaths(data_dir=tmp_path / "other-device")
    bundle = set_master_password_for_account(other_device, "user-1", SecretStr("hunter2"))
    assert bundle is not None
    cli.sync_bundle_push("a@b.com", bundle)
    remote = ReplicaRecord(
        host_id="host-remote-1",
        agent_id=str(AgentId.generate()),
        display_name="remote-ws",
        provider_kind="lima",
        hosting_device_id="device-other",
        device_label="other-device",
        encrypted_secrets="b3BhcXVl",
    )
    cli.sync_records_by_email["a@b.com"] = {"host-remote-1": remote.to_wire(1)}

    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    # The reconcile normally pulls on startup; do it directly for the test.
    session_store = get_state(client.application).session_store
    assert session_store is not None and session_store.record_store is not None
    session_store.record_store.pull("user-1", "a@b.com")

    wrong = client.post("/_chrome/sync-unlock", json={"password": "nope"})
    assert wrong.status_code == 200
    assert wrong.get_json()["ok"] is False

    response = client.post("/_chrome/sync-unlock", json={"password": "hunter2"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["unlocked"] == ["a@b.com"]
    assert is_account_unlocked(WorkspacePaths(data_dir=tmp_path), "user-1")


def test_sync_unlock_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    assert client.post("/_chrome/sync-unlock", json={"password": "x"}).status_code == 403


def test_remove_workspace_record_deletes_the_row(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    session_store = get_state(client.application).session_store
    assert session_store is not None
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(AgentId.generate()),
        host_id="host-remove-me",
        display_name="stale",
        color=None,
        is_cloud_row=False,
    )
    assert "host-remove-me" in cli.sync_records_by_email["a@b.com"]

    response = client.post("/_chrome/workspaces/remove-record", json={"host_id": "host-remove-me"})

    assert response.status_code == 200
    assert "host-remove-me" not in cli.sync_records_by_email["a@b.com"]
    assert session_store.record_store is not None
    assert session_store.record_store.list_records("user-1") == []


def test_remove_workspace_record_unknown_host_is_404(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)
    assert client.post("/_chrome/workspaces/remove-record", json={"host_id": "host-nope"}).status_code == 404


# -- Recently destroyed workspaces page --

_DESTROYED_AGENT_ID = "agent-" + "9" * 32


def _seed_destroyed_record(tmp_path: Path, cli: "FakeImbueCloudCli", destroyed_days_ago: int = 3) -> None:
    """Tombstone one record for the signed-in test account over the shared data dir."""
    seed_store = make_session_store_for_test(tmp_path, cli=cli)
    assert seed_store.record_store is not None
    destroyed_at = (datetime.now(timezone.utc) - timedelta(days=destroyed_days_ago)).isoformat()
    seed_store.record_store.upsert_local_record(
        "user-test-123",
        "test@example.com",
        ReplicaRecord(
            host_id="host-destroyed1",
            agent_id=_DESTROYED_AGENT_ID,
            display_name="old-workspace",
            state="destroyed",
            destroyed_at=destroyed_at,
        ),
    )


def _make_destroyed_delete_client(
    tmp_path: Path, cli: "FakeImbueCloudCli"
) -> tuple[FlaskClient, WorkspaceRecordStore]:
    """An authenticated client whose app state carries a scheduler with a backup reaper.

    The delete-backup route reaches the reaper via
    ``get_state().sync_scheduler.backup_reaper``, so both delete tests need
    this full stack; the record store is returned for assertions.
    """
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    record_store = session_store.record_store
    assert record_store is not None
    reaper = BackupReaperManager(
        paths=record_store.paths,
        record_store=record_store,
        imbue_cloud_cli=None,
        connector_url="",
    )
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        backup_reaper=reaper,
    )
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli, sync_scheduler=scheduler)
    _authenticate_client(client, auth_store)
    return client, record_store


def test_destroyed_workspaces_delete_backup_reaps_record(tmp_path: Path) -> None:
    """POST delete-backup runs the reaper's strict deletion and redirects back to the page."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    _seed_destroyed_record(tmp_path, cli)
    client, record_store = _make_destroyed_delete_client(tmp_path, cli)

    response = client.post(f"/workspaces/destroyed/{_DESTROYED_AGENT_ID}/delete-backup")

    assert response.status_code == 303
    assert response.headers["Location"] == "/workspaces/destroyed"
    assert record_store.list_records("user-test-123") == []


def test_finalize_and_mark_destroying_deletes_the_machines_share(tmp_path: Path) -> None:
    """Destroying a machine tears down its machine share.

    Nothing downstream of ``mngr destroy`` knows the share exists, so without
    this the share outlives every identifier that could find it: it keeps a
    relay hostname reserved and counts against a quota measured in machines
    ever created rather than live ones.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    _write_dead_destroy_dir(paths, agent_id, host_id)
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    cli.add_share(account="a@b.com", host_id=str(host_id))
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert cli.deleted_share_host_ids == [str(host_id)]
    assert cli.get_share_status(account="a@b.com", host_id=str(host_id)) is None


def test_finalize_and_mark_destroying_tombstones_even_if_the_share_delete_fails(tmp_path: Path) -> None:
    """A connector hiccup must not leave the machine stuck in the UI.

    A share that survives is litter; a machine that cannot be retired is a
    stuck row the user cannot clear.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    _write_dead_destroy_dir(paths, agent_id, host_id)
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    # The share lookup itself blows up; teardown must still proceed.
    cli.is_share_lookup_failing = True
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="doomed",
        color=None,
        is_cloud_row=False,
    )
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    marker = _finalize_and_mark_destroying(paths, backend_resolver, session_store, cli)

    assert marker == {}
    assert not (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.record_store is not None
    assert session_store.record_store.list_records("user-1")[0].state == "destroyed"


def test_forward_bridge_redirects_authenticated_browser_to_plugin(tmp_path: Path) -> None:
    """/forward-bridge bounces a signed-in browser to the plugin's /_bridge with the spawn secret.

    This is browser mode's twin of the Electron preauth cookie injection: the
    chrome iframe enters workspaces through this hop so the plugin can set its
    bare-origin session cookie without an OTP.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        mngr_forward_port=9876,
        mngr_forward_browser_bridge_token="bridge-tok",
    )
    client = app.test_client()
    _authenticate_client(client, auth_store)
    next_path = "/goto/host-00000000000000000000000000000000/"
    response = client.get(f"/forward-bridge?next={next_path}")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://localhost:9876/_bridge?token=bridge-tok&next=")
    assert "goto" in location
    # Off-origin next targets collapse to "/" (no open redirect).
    evil = client.get("/forward-bridge?next=//evil.com/")
    assert evil.headers["Location"].endswith("&next=%2F")


def test_forward_bridge_unauthenticated_redirects_home(tmp_path: Path) -> None:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        mngr_forward_port=9876,
        mngr_forward_browser_bridge_token="bridge-tok",
    )
    client = app.test_client()
    response = client.get("/forward-bridge?next=/goto/host-00000000000000000000000000000000/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_forward_bridge_is_404_without_spawn_token(tmp_path: Path) -> None:
    client, auth_store, _agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client, auth_store)
    assert client.get("/forward-bridge?next=/").status_code == 404
