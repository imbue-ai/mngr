import json
import os
import queue
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
from flask import Request
from flask import Response
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.app import _build_mngr_start_argv
from imbue.minds.desktop_client.app import _build_mngr_stop_argv
from imbue.minds.desktop_client.app import _build_requests_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _destroying_agent_ids
from imbue.minds.desktop_client.app import _is_discovery_fresh
from imbue.minds.desktop_client.app import _provider_error_message_for_workspace
from imbue.minds.desktop_client.app import _resolve_destroying_for_landing
from imbue.minds.desktop_client.app import _run_restart_sequence
from imbue.minds.desktop_client.app import _should_emit_system_interface_status
from imbue.minds.desktop_client.app import _ssh_command_for_agent
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import DEFAULT_SERVICE_NAME
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.discovery_health import ProducerRemediator
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
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

    The production path (GET /authenticate?one_time_code=...) returns a
    ``Set-Cookie`` with ``Domain=localhost`` so the cookie is valid on both
    ``localhost`` and ``<agent-id>.localhost`` subdomains. The test client's
    cookie jar is stricter than real browsers about Domain=localhost and
    silently drops that cookie on subsequent requests, so we set the cookie
    directly on the jar here instead of round-tripping through /authenticate.
    The server-side logic the test is exercising is independent of the
    Set-Cookie emission path; the bare presence/signature of the cookie is
    what ``_is_authenticated`` checks.
    """
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    # Intentionally no Domain=: the test client cookie jar is strict about
    # Domain=localhost cookies on subsequent requests.
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)


def test_landing_page_shows_login_when_unauthenticated(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Login" in response.text


def test_login_redirects_to_authenticate_via_js(tmp_path: Path) -> None:
    client, auth_store, _ = _setup_test_server(tmp_path)
    code = OneTimeCode("login-code-{}".format(AgentId()))
    auth_store.add_one_time_code(code=code)

    response = client.get(
        "/login",
        query_string={"one_time_code": str(code)},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "window.location.href" in response.text
    assert "/authenticate" in response.text


def test_login_without_one_time_code_returns_422(tmp_path: Path) -> None:
    """A missing one_time_code is a 422 (matching FastAPI's required-query-param
    rejection), not a 500."""
    client, _, _ = _setup_test_server(tmp_path)
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 422


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


def test_landing_page_lists_single_agent(tmp_path: Path) -> None:
    """When authenticated and exactly one agent is known, the landing page lists it."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


# -- Post-login redirect tests --


def test_post_login_redirects_to_create_when_no_workspaces(tmp_path: Path) -> None:
    """A just-signed-in user with no workspaces lands on the create screen (/)."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/post-login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_post_login_redirects_to_accounts_when_workspaces_exist(tmp_path: Path) -> None:
    """A returning user who already has workspaces lands on the accounts page."""
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


class _LeasedImbueCloudResolver(StaticBackendResolver):
    """Static resolver reporting every known agent as living on a leased imbue_cloud provider."""

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id in self.list_known_agent_ids():
            return AgentDisplayInfo(
                agent_name=str(agent_id),
                host_id="host-leased",
                provider_name="imbue_cloud_alice-imbue-com",
            )
        return None


def _make_leased_host_client(tmp_path: Path) -> tuple[FlaskClient, FileAuthStore, AgentId]:
    agent_id = AgentId()
    backend_resolver = _LeasedImbueCloudResolver(
        url_by_agent_and_service={str(agent_id): {"web": "http://backend"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path, backend_resolver=backend_resolver, http_client=None
    )
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_id


def test_disassociate_leased_host_returns_403(tmp_path: Path) -> None:
    client, _auth_store, agent_id = _make_leased_host_client(tmp_path)
    response = client.post(f"/workspace/{agent_id}/disassociate", follow_redirects=False)
    assert response.status_code == 403
    assert "leased from imbue_cloud" in response.text


def test_associate_leased_host_returns_403(tmp_path: Path) -> None:
    client, _auth_store, agent_id = _make_leased_host_client(tmp_path)
    response = client.post(
        f"/workspace/{agent_id}/associate",
        data={"user_id": "user-123"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "leased from imbue_cloud" in response.text


def test_settings_page_disables_disassociate_for_leased_host(tmp_path: Path) -> None:
    client, _auth_store, agent_id = _make_leased_host_client(tmp_path)
    response = client.get(f"/workspace/{agent_id}/settings")
    assert response.status_code == 200
    assert "leased from Imbue Cloud" in response.text
    # The disassociate control is present but disabled, and there is no
    # associate control (the Associate component renders a user_id select).
    assert 'id="disassociate-btn"' in response.text
    assert "disabled" in response.text


# -- Agent default redirect tests --


# -- Agent servers page tests --


# -- Proxy tests (now with service_name in URL) --


def _setup_test_server_without_backend(
    tmp_path: Path,
) -> tuple[FlaskClient, FileAuthStore, AgentId]:
    """Set up a desktop client with no backends for testing error paths."""
    agent_id = AgentId()

    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    return client, auth_store, agent_id


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


# -- Multi-server proxy tests --


# -- Integration test: MngrCliBackendResolver with desktop client --


def test_mngr_cli_resolver_landing_page_lists_single_discovered_agent(tmp_path: Path) -> None:
    """When a single agent is discovered and authenticated, the landing page lists it."""
    agent_id = AgentId()
    data_dir = tmp_path / "minds_data"

    backend_resolver = make_resolver_with_data(
        service_logs={str(agent_id): make_service_log("web", "http://test-backend")},
        agents_json=make_agents_json(agent_id),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=data_dir,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id) in response.text


def test_landing_page_shows_discovering_when_initial_discovery_not_done(tmp_path: Path) -> None:
    """Before initial discovery completes, show discovering state with auto-refresh."""
    backend_resolver = MngrCliBackendResolver()
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Discovering agents" in response.text
    assert "reload" in response.text


def test_landing_page_shows_create_form_after_discovery_finds_no_agents(tmp_path: Path) -> None:
    """After discovery completes with no agents, show the create form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "Where do you want to run your mind?" in response.text
    assert "git_url" in response.text


def test_landing_page_prefills_git_url_from_query_param(tmp_path: Path) -> None:
    """The create form pre-fills the git URL from a query parameter."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/", query_string={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    assert "file:///nonexistent-repo" in response.text


def test_create_page_shows_form(tmp_path: Path) -> None:
    """GET /create shows the agent creation form."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/create")
    assert response.status_code == 200
    assert "Where do you want to run your mind?" in response.text
    assert 'data-preset="remote"' in response.text
    assert 'data-preset="local"' in response.text


def test_creation_status_returns_404_for_unknown_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 404 for unknown creation."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    # The URL handle is a ``CreationId`` (minds-internal in-flight handle),
    # not a canonical mngr ``AgentId``; passing an AgentId-prefixed string
    # would now fail to parse and never even reach the not-tracked check.
    unknown_id = CreationId()
    response = client.get("/api/create-agent/{}/status".format(unknown_id))
    assert response.status_code == 404


def test_landing_page_lists_agents_when_multiple_known(tmp_path: Path) -> None:
    """When authenticated and multiple agents are known, the landing page lists them all."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={
            str(agent_id_1): {"web": "http://test:9100"},
            str(agent_id_2): {"web": "http://test:9200"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert str(agent_id_1) in response.text
    assert str(agent_id_2) in response.text


def test_create_form_submit_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """POST /create returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/create", data={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 501


def test_create_agent_api_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """POST /api/create-agent returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 501


def test_creating_page_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /creating/{id} returns 501 when no agent_creator is configured."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    agent_id = AgentId()
    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 501


def _create_test_server_with_agent_creator(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface | None = None,
) -> tuple[FlaskClient, FileAuthStore, AgentCreator]:
    """Create a desktop client with an agent creator for testing.

    The returned client is already authenticated with a global session.

    ``backend_resolver`` defaults to an empty ``StaticBackendResolver``; pass a
    populated resolver to exercise paths that consult it (e.g. the
    duplicate-agent-name guard in ``_handle_create_agent_api``).

    The ``AgentCreator.root_concurrency_group`` is an ad-hoc group entered for
    the helper and left active for the caller's test duration. These tests only
    exercise HTTP endpoints (status polling, form rendering, etc.) -- they do
    not actually run agent creation subprocesses against the group, so leaving
    it in the ACTIVE state until GC is acceptable here.
    """
    if backend_resolver is None:
        backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    root_cg = ConcurrencyGroup(name="test-root")
    root_cg.__enter__()
    agent_creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_cg,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
        agent_creator=agent_creator,
    )
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_creator


def test_create_form_submit_redirects_to_creating_page(tmp_path: Path) -> None:
    """POST /create with valid git_url redirects to /creating/{agent_id}."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/creating/")
    agent_creator.wait_for_all()


def test_create_form_submit_rejects_empty_git_url(tmp_path: Path) -> None:
    """POST /create with empty git_url returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/create", data={"git_url": "", "host_name": "test"})
    assert response.status_code == 400


def test_create_form_submit_passes_host_name(tmp_path: Path) -> None:
    """POST /create passes host_name to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo", "host_name": "my-workspace"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    agent_creator.wait_for_all()


def test_create_form_submit_auto_names_next_mind(tmp_path: Path) -> None:
    """POST /create with no host_name auto-generates the next free ``mind-N``.

    The form no longer asks for a name. With ``mind-1`` already a known
    workspace in the resolver, the create handler must gather existing names
    across providers and resolve the new workspace to ``mind-2``.
    """
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"workspace": "mind-1", "is_primary": "true"}),
    )
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path, backend_resolver=resolver)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    creation_id = CreationId(response.headers["Location"].rsplit("/", 1)[-1])
    info = agent_creator.get_creation_info(creation_id)
    assert info is not None
    assert info.host_name == "mind-2"
    agent_creator.wait_for_all()


def test_create_agent_api_passes_host_name(tmp_path: Path) -> None:
    """POST /api/create-agent passes host_name to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "file:///nonexistent-repo", "host_name": "my-agent"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert "agent_id" in data
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_duplicate_host_name(tmp_path: Path) -> None:
    """POST /api/create-agent returns 409 when the requested name is already in use.

    The guard walks ``backend_resolver.list_known_workspace_ids()`` and rejects
    the create if any known workspace agent's ``workspace`` label matches the
    requested name -- failing fast at the API boundary instead of deep in the
    git-mirror push.
    """
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"workspace": "existing-agent", "is_primary": "true"}),
    )
    client, _, _ = _create_test_server_with_agent_creator(tmp_path, backend_resolver=resolver)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "file:///nonexistent-repo", "host_name": "existing-agent"},
    )

    assert response.status_code == 409
    assert "existing-agent" in response.get_json()["error"]


def test_create_agent_api_allows_unique_host_name_when_others_exist(tmp_path: Path) -> None:
    """The duplicate-name guard must not false-positive on a distinct name."""
    existing_id = AgentId()
    resolver = make_resolver_with_data(
        make_agents_json(existing_id, labels={"workspace": "existing-agent", "is_primary": "true"}),
    )
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path, backend_resolver=resolver)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "file:///nonexistent-repo", "host_name": "a-different-name"},
    )

    assert response.status_code == 200
    assert "agent_id" in response.get_json()
    agent_creator.wait_for_all()


def test_create_agent_api_returns_agent_id(tmp_path: Path) -> None:
    """POST /api/create-agent returns JSON with agent_id and status."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    data = response.get_json()
    assert "agent_id" in data
    assert data["status"] == "INITIALIZING"
    agent_creator.wait_for_all()


def test_create_agent_api_accepts_json_body_without_content_type_header(tmp_path: Path) -> None:
    """A JSON body posted without an ``application/json`` Content-Type is still parsed.

    The FastAPI version parsed the request body regardless of its Content-Type
    (``await request.json()`` ignores the header). The Flask port preserves that
    via ``get_json(force=True)``; without it, Flask returns None for a missing
    header and the endpoint would 400 a valid JSON payload. Regression guard for
    that parity fix.
    """
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        data=json.dumps({"git_url": "file:///nonexistent-repo"}),
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 200
    assert "agent_id" in response.get_json()
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_empty_git_url(tmp_path: Path) -> None:
    """POST /api/create-agent with empty git_url returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post("/api/create-agent", json={"git_url": ""})
    assert response.status_code == 400


def test_create_form_submit_rejects_invalid_host_name(tmp_path: Path) -> None:
    """POST /create with a host_name that fails HostName validation re-renders the form with an error."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={"git_url": "file:///nonexistent-repo", "host_name": "bad.name"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "alphanumeric" in response.text
    assert "bad.name" in response.text


def test_create_agent_api_rejects_invalid_host_name(tmp_path: Path) -> None:
    """POST /api/create-agent with a host_name that fails HostName validation returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={"git_url": "file:///nonexistent-repo", "host_name": "bad.name"},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert "alphanumeric" in body["error"]


def test_create_agent_api_rejects_invalid_json(tmp_path: Path) -> None:
    """POST /api/create-agent with invalid JSON returns 400."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        data=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "Invalid JSON" in response.text


def test_creating_page_shows_status(tmp_path: Path) -> None:
    """GET /creating/{agent_id} shows the loading/progress page directly.

    The page no longer interposes any onboarding questions before the
    setting-up screen, so it goes straight to the loading view.
    """
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 200
    assert "Creating your project" in response.text
    assert "Setting up your workspace" in response.text
    # The onboarding question UI was removed, so none of its markers render.
    assert "data-question" not in response.text
    assert 'class="opt' not in response.text
    agent_creator.wait_for_all()


def test_creating_page_returns_404_for_unknown(tmp_path: Path) -> None:
    """GET /creating/{agent_id} returns 404 for unknown agent creation."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/creating/{}".format(CreationId()))
    assert response.status_code == 404


def test_creation_status_api_returns_status_for_tracked_agent(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns a valid status for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    creation_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/api/create-agent/{}/status".format(creation_id))
    assert response.status_code == 200
    data = response.get_json()
    # The status response now reports both ``creation_id`` (always present)
    # and ``agent_id`` (only once mngr create returns a canonical id). For
    # this test the create runs against a nonexistent repo so it may never
    # produce an agent_id; just check that the creation_id round-trips.
    assert data["creation_id"] == str(creation_id)
    assert data["status"] in (
        "INITIALIZING",
        "CLONING_REPO",
        "CHECKING_OUT_BRANCH",
        "PROVISIONING_AI",
        "CREATING_WORKSPACE",
        "WAITING_FOR_READY",
        "DONE",
        "FAILED",
    )
    agent_creator.wait_for_all()


def test_create_page_prefills_git_url_from_query(tmp_path: Path) -> None:
    """GET /create?git_url=... pre-fills the form."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create", query_string={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 200
    assert "file:///nonexistent-repo" in response.text


def test_landing_page_shows_create_link_when_multiple_agents_known(tmp_path: Path) -> None:
    """When authenticated with multiple agents known, landing page shows a 'Create' link."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={
            str(agent_id_1): {"web": "http://test:9100"},
            str(agent_id_2): {"web": "http://test:9200"},
        },
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    assert "/create" in response.text


def test_create_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/create")
    assert response.status_code == 403


def test_create_form_submit_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /create returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/create", data={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_create_agent_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """POST /api/create-agent returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.post("/api/create-agent", json={"git_url": "file:///nonexistent-repo"})
    assert response.status_code == 403


def test_creation_status_api_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/status returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/status".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_501_without_agent_creator(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 501 when no agent_creator."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/api/create-agent/{}/logs".format(AgentId()))
    assert response.status_code == 501


def test_creation_logs_sse_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/api/create-agent/{}/logs".format(AgentId()))
    assert response.status_code == 403


def test_creation_logs_sse_returns_404_for_unknown(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns 404 for unknown agent."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/api/create-agent/{}/logs".format(CreationId()))
    assert response.status_code == 404


def test_creation_logs_sse_streams_events(tmp_path: Path) -> None:
    """GET /api/create-agent/{id}/logs returns SSE stream for a tracked creation."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/api/create-agent/{}/logs".format(agent_id))
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    response.close()
    agent_creator.wait_for_all()


def test_creation_logs_sse_emits_status_events(tmp_path: Path) -> None:
    """The current ``AgentCreationStatus`` is surfaced as a ``{"_type": "status"}`` SSE event.

    Regular log lines stay on the ``{"log": ...}`` channel. This test exercises
    the polling dispatch in ``_stream_creation_logs`` by seeding a particular
    status into the ``AgentCreator``'s private state and verifying the stream
    emits a matching status event with the right ``status_text`` -- without
    running a real agent creation (which would require Docker).
    """
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    creation_id = CreationId()
    log_queue: queue.Queue[str] = queue.Queue()
    with agent_creator._lock:
        agent_creator._statuses[str(creation_id)] = AgentCreationStatus.CREATING_WORKSPACE
        agent_creator._launch_modes[str(creation_id)] = LaunchMode.DOCKER
        agent_creator._log_queues[str(creation_id)] = log_queue

    log_queue.put("regular log line")
    log_queue.put(LOG_SENTINEL)

    payloads: list[dict[str, object]] = []
    response = client.get("/api/create-agent/{}/logs".format(creation_id))
    assert response.status_code == 200
    # The seeded LOG_SENTINEL makes this stream finite, so the full body can be read.
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            payloads.append(json.loads(line[len("data: ") :]))

    status_events = [p for p in payloads if p.get("_type") == "status"]
    log_events = [p for p in payloads if "log" in p]
    assert len(status_events) == 1
    assert status_events[0]["status"] == "CREATING_WORKSPACE"
    assert status_events[0]["status_text"] == "Creating workspace..."
    assert any(p["log"] == "regular log line" for p in log_events)


def test_creation_logs_sse_emits_status_text_for_imbue_cloud(tmp_path: Path) -> None:
    """Status captions are launch-mode-aware via ``_STATUS_TEXT_IMBUE_CLOUD``."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    creation_id = CreationId()
    log_queue: queue.Queue[str] = queue.Queue()
    with agent_creator._lock:
        agent_creator._statuses[str(creation_id)] = AgentCreationStatus.CREATING_WORKSPACE
        agent_creator._launch_modes[str(creation_id)] = LaunchMode.IMBUE_CLOUD
        agent_creator._log_queues[str(creation_id)] = log_queue

    log_queue.put(LOG_SENTINEL)

    payloads: list[dict[str, object]] = []
    response = client.get("/api/create-agent/{}/logs".format(creation_id))
    assert response.status_code == 200
    # The seeded LOG_SENTINEL makes this stream finite, so the full body can be read.
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            payloads.append(json.loads(line[len("data: ") :]))

    status_events = [p for p in payloads if p.get("_type") == "status"]
    assert status_events
    assert status_events[0]["status"] == "CREATING_WORKSPACE"
    assert status_events[0]["status_text"] == "Setting up agent..."


def test_creating_page_rejects_unauthenticated(tmp_path: Path) -> None:
    """GET /creating/{id} returns 403 without authentication."""
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    client, _ = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )

    response = client.get("/creating/{}".format(AgentId()))
    assert response.status_code == 403


def test_create_form_submit_passes_launch_mode(tmp_path: Path) -> None:
    """POST /create passes launch_mode to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "host_name": "my-agent",
            "launch_mode": "DOCKER",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    agent_creator.wait_for_all()


def test_create_agent_api_passes_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent passes launch_mode to the creator."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "host_name": "my-agent",
            "launch_mode": "DOCKER",
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert "agent_id" in data
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_invalid_launch_mode(tmp_path: Path) -> None:
    """POST /api/create-agent returns 400 for an invalid launch_mode."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "host_name": "my-agent",
            "launch_mode": "INVALID_MODE",
        },
    )
    assert response.status_code == 400
    assert "Invalid launch_mode" in response.get_json()["error"]


def test_create_form_shows_launch_mode_dropdown(tmp_path: Path) -> None:
    """GET /create form includes the launch mode dropdown."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create")
    assert response.status_code == 200
    assert "launch_mode" in response.text
    assert "docker" in response.text
    assert "cloud" in response.text
    assert "lima" in response.text
    assert "imbue_cloud" in response.text


def test_create_form_shows_ai_provider_dropdown(tmp_path: Path) -> None:
    """GET /create form includes the AI provider dropdown with all three options."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create")
    assert response.status_code == 200
    assert 'name="ai_provider"' in response.text
    assert 'value="IMBUE_CLOUD"' in response.text
    assert 'value="API_KEY"' in response.text
    assert 'value="SUBSCRIPTION"' in response.text


def test_create_form_does_not_show_env_file_checkbox(tmp_path: Path) -> None:
    """The .env-file checkbox has been removed from the form."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/create")
    assert response.status_code == 200
    assert "include_env_file" not in response.text


def test_create_form_submit_redirects_imbue_cloud_compute_without_account_to_signin(tmp_path: Path) -> None:
    """IMBUE_CLOUD compute without any account routes into the sign-in/up flow.

    With no signed-in account at all, the remote compute path is unusable, so
    rather than re-rendering an error the server sends the user to sign up (with
    a link back to the picker).
    """
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "launch_mode": "IMBUE_CLOUD",
            "ai_provider": "SUBSCRIPTION",
            "account_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/signup?return_to=%2Fcreate"


def test_create_form_submit_redirects_imbue_cloud_ai_without_account_to_signin(tmp_path: Path) -> None:
    """IMBUE_CLOUD AI provider without any account routes into the sign-in/up flow."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "launch_mode": "DOCKER",
            "ai_provider": "IMBUE_CLOUD",
            "account_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/signup?return_to=%2Fcreate"


def test_create_form_submit_rejects_api_key_provider_without_key(tmp_path: Path) -> None:
    """Selecting AI provider API_KEY without supplying a key is rejected."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "host_name": "my-agent",
            "launch_mode": "DOCKER",
            "ai_provider": "API_KEY",
            "anthropic_api_key": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Anthropic API key is required" in response.text


def test_create_form_submit_accepts_subscription_with_no_account(tmp_path: Path) -> None:
    """Subscription mode + no account is the no-account default and must be accepted."""
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "host_name": "my-agent",
            "launch_mode": "DOCKER",
            "ai_provider": "SUBSCRIPTION",
            "account_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    agent_creator.wait_for_all()


def test_create_agent_api_rejects_api_key_provider_without_key(tmp_path: Path) -> None:
    """The JSON API also rejects AI provider API_KEY without a key."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "ai_provider": "API_KEY",
            "anthropic_api_key": "",
        },
    )
    assert response.status_code == 400
    assert "anthropic_api_key is required" in response.get_json()["error"]


def test_create_agent_api_rejects_invalid_ai_provider(tmp_path: Path) -> None:
    """An unknown ai_provider is rejected by the JSON API."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "ai_provider": "BOGUS",
        },
    )
    assert response.status_code == 400
    assert "Invalid ai_provider" in response.get_json()["error"]


def test_create_agent_api_rejects_imbue_cloud_compute_without_account(tmp_path: Path) -> None:
    """API parity with the form path: IMBUE_CLOUD compute requires an account."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "launch_mode": "IMBUE_CLOUD",
            "ai_provider": "SUBSCRIPTION",
        },
    )
    assert response.status_code == 400
    assert "account_id is required" in response.get_json()["error"]


def test_create_agent_api_rejects_imbue_cloud_ai_without_account(tmp_path: Path) -> None:
    """API parity with the form path: IMBUE_CLOUD AI provider requires an account."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.post(
        "/api/create-agent",
        json={
            "git_url": "file:///nonexistent-repo",
            "launch_mode": "DOCKER",
            "ai_provider": "IMBUE_CLOUD",
        },
    )
    assert response.status_code == 400
    assert "account_id is required" in response.get_json()["error"]


def test_create_form_submit_preserves_account_id_on_validation_error(tmp_path: Path) -> None:
    """When validation fails and the form re-renders, the user's account_id choice
    must survive instead of reverting to the config default. The form submits
    ``account_id=""`` for "No account"; the re-rendered page must show that
    option as ``selected`` and must NOT show any other account as selected."""
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    # Trigger a re-rendering validation error: AI provider API_KEY with no key.
    # (imbue_cloud-without-account now redirects to sign-in instead of
    # re-rendering, so it no longer exercises this re-render path.)
    response = client.post(
        "/create",
        data={
            "git_url": "file:///nonexistent-repo",
            "launch_mode": "DOCKER",
            "ai_provider": "API_KEY",
            "anthropic_api_key": "",
            "account_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    # The "No account (private project)" option is selected when default_account_id is empty.
    assert 'value=""' in response.text and "No account" in response.text
    # And the API-key validation error should be present.
    assert "Anthropic API key is required" in response.text


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


# -- Chrome routes --


def test_chrome_page_renders_without_auth(tmp_path: Path) -> None:
    """The /_chrome route is unauthenticated and returns the chrome HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    assert "minds-titlebar" in response.text
    assert "content-frame" in response.text


def test_chrome_page_includes_sidebar_toggle(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    assert "sidebar-toggle" in response.text
    assert "sidebar-menu" in response.text


def test_chrome_sidebar_page_renders(tmp_path: Path) -> None:
    """The /_chrome/sidebar route returns the standalone sidebar HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/sidebar")
    assert response.status_code == 200
    assert "sidebar-workspaces" in response.text
    # Interactivity including the SSE fallback has moved to the external JS.
    assert "/_static/sidebar.js" in response.text


def test_chrome_events_sse_returns_auth_required_when_unauthenticated(tmp_path: Path) -> None:
    """The /_chrome/events SSE endpoint returns auth_required for unauthenticated users."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/events")
    assert response.status_code == 200
    assert "auth_required" in response.text


def test_chrome_events_sse_returns_workspaces_when_authenticated(tmp_path: Path) -> None:
    """The /_chrome/events SSE endpoint returns workspace list for authenticated users.

    We test the underlying _build_workspace_list helper since the SSE endpoint
    is an infinite stream that the test client cannot consume without blocking.
    """
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {str(DEFAULT_SERVICE_NAME): "http://test-backend"}},
    )

    workspaces = _build_workspace_list(backend_resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == str(agent_id)


class _NoopRemediator(ProducerRemediator):
    """A producer remediator whose remediations do nothing (the BLOCKED path never calls them)."""

    def bounce(self) -> None:
        pass

    def restart(self) -> None:
        pass


def test_chrome_events_sse_emits_discovery_health_blocked_on_connect(tmp_path: Path) -> None:
    """A BLOCKED watchdog makes the chrome SSE emit a discovery_health payload on connect.

    The connect-time batch is emitted before the generator's wait loop, so
    pre-setting the shutdown event lets the (otherwise infinite) stream finish
    after that batch and keeps the test client from blocking.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    watchdog = DiscoveryHealthWatchdog(remediator=_NoopRemediator())
    # Force the terminal BLOCKED tier so the connect-time batch surfaces it.
    watchdog.record_consumer_death()
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        discovery_health_watchdog=watchdog,
    )
    # End the stream right after its connect-time batch so the client doesn't block.
    get_state(app).shutdown_event.set()
    client = app.test_client()
    _authenticate_client(client, auth_store)

    response = client.get("/_chrome/events")

    assert response.status_code == 200
    assert '"type": "discovery_health"' in response.text
    assert '"state": "blocked"' in response.text


def test_chrome_events_sse_omits_discovery_health_when_healthy(tmp_path: Path) -> None:
    """A HEALTHY watchdog surfaces nothing -- the RECONNECTING/healthy tiers are silent."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    watchdog = DiscoveryHealthWatchdog(remediator=_NoopRemediator())
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        discovery_health_watchdog=watchdog,
    )
    get_state(app).shutdown_event.set()
    client = app.test_client()
    _authenticate_client(client, auth_store)

    response = client.get("/_chrome/events")

    assert response.status_code == 200
    assert "discovery_health" not in response.text


def test_destroying_agent_ids_returns_ids_with_live_destroy(tmp_path: Path) -> None:
    """An agent with an alive destroy pid + still in the resolver shows up as running.

    main.js keys its "ok to navigate the user away from this workspace"
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
    ids = _destroying_agent_ids(paths, backend_resolver)
    assert ids == [str(agent_id)]


def test_destroying_agent_ids_returns_empty_when_paths_is_none() -> None:
    """The test-server helper builds a minimal app without WorkspacePaths;
    the helper must tolerate that without raising."""
    assert _destroying_agent_ids(None, StaticBackendResolver(url_by_agent_and_service={})) == []


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


def test_resolve_destroying_for_landing_finalizes_when_host_gone(tmp_path: Path) -> None:
    """A finished destroy whose host is gone is DONE: disassociated + record deleted.

    This is the Fix for the silent-orphan bug -- finalization (disassociation)
    happens only once the host is actually gone, not synchronously on click.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_workspace("user-1", str(agent_id))
    # Resolver knows no active agents and reports no host state -> the host is
    # gone -> the destroy is DONE.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})

    marker = _resolve_destroying_for_landing(paths, backend_resolver, session_store)

    assert marker == {}
    assert not (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is None


def test_resolve_destroying_for_landing_keeps_failed_when_host_still_up(tmp_path: Path) -> None:
    """A finished destroy whose host is still up is FAILED: kept + stays associated.

    The workspace must remain visible and owned so the user can retry, instead
    of vanishing while its host keeps running (and billing).
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    _write_dead_destroy_dir(paths, agent_id, HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_workspace("user-1", str(agent_id))
    # Resolver still lists the workspace agent as active -> host still up -> FAILED.
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})

    marker = _resolve_destroying_for_landing(paths, backend_resolver, session_store)

    assert marker == {str(agent_id): "failed"}
    assert (paths.data_dir / "destroying" / str(agent_id)).exists()
    assert session_store.get_account_for_workspace(str(agent_id)) is not None


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
    assert _build_requests_payload(None, resolver) == {"count": 0, "request_ids": []}
    assert _build_requests_payload(RequestInbox(), resolver) == {"count": 0, "request_ids": []}


def test_build_requests_payload_carries_pending_ids() -> None:
    """A pending request surfaces its event_id alongside the count."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="post updates"
    )
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    payload = _build_requests_payload(RequestInbox().add_request(event), resolver)
    assert payload == {"count": 1, "request_ids": [str(event.event_id)]}


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
) -> tuple[FlaskClient, FileAuthStore]:
    """Create a desktop client with session store and config for testing new routes.

    ``cli`` is forwarded to :func:`make_session_store_for_test` so callers
    can seed the session store with specific accounts; defaults to a
    fresh empty fake CLI.
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
    )
    client = app.test_client()
    return client, auth_store


def _create_test_client_with_auth_routes(tmp_path: Path) -> FlaskClient:
    """Create a desktop client with the /auth blueprint mounted.

    The auth blueprint is only registered when both a session store and an
    imbue_cloud CLI are wired, so this passes both.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    cli = make_fake_imbue_cloud_cli()
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        imbue_cloud_cli=cli,
        session_store=session_store,
    )
    return app.test_client()


def test_auth_login_page_renders_message_query_param(tmp_path: Path) -> None:
    """GET /auth/login?message=... renders the banner (e.g. the Electron shell's
    'You need to sign in...' prompt on the auth_required event)."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/login", query_string={"message": "You need to sign in to Imbue"})
    assert response.status_code == 200
    assert "You need to sign in to Imbue" in response.text


def test_auth_login_page_without_message_query_param(tmp_path: Path) -> None:
    """GET /auth/login with no message renders without injecting one."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert "You need to sign in to Imbue" not in response.text


def test_auth_page_with_return_to_shows_back_link_and_explainer(tmp_path: Path) -> None:
    """GET /auth/signup?return_to=/create shows a back link + the remote explainer."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signup", query_string={"return_to": "/create"})
    assert response.status_code == 200
    # Back link to the picker.
    assert "Back to mind setup" in response.text
    assert 'href="/create"' in response.text
    # Default explainer banner (no explicit message supplied).
    assert "run your mind on Imbue Cloud" in response.text


def test_auth_signin_modal_page_renders_overlay_with_auth_form(tmp_path: Path) -> None:
    """GET /auth/signin-modal serves the overlay sign-in page (transparent
    backdrop + the shared auth form) loaded into the shared modal view."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signin-modal")
    assert response.status_code == 200
    assert 'id="signin-modal-backdrop"' in response.text
    assert 'id="signin-form"' in response.text
    assert "run your mind on Imbue Cloud" in response.text


def test_auth_page_ignores_unsafe_return_to(tmp_path: Path) -> None:
    """An off-origin return_to is dropped: no back link to it, no explainer."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signup", query_string={"return_to": "https://evil.com"})
    assert response.status_code == 200
    assert "Back to mind setup" not in response.text
    assert "evil.com" not in response.text
    assert "run your mind on Imbue Cloud" not in response.text


def test_accounts_page_requires_auth(tmp_path: Path) -> None:
    """The /accounts page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/accounts")
    assert response.status_code == 403


def test_accounts_page_shows_empty_when_no_accounts(tmp_path: Path) -> None:
    """The /accounts page shows no accounts when none are logged in."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/accounts")
    assert response.status_code == 200
    assert "No accounts logged in" in response.text


def test_accounts_page_shows_logged_in_accounts(tmp_path: Path) -> None:
    """The /accounts page lists logged-in accounts."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-test-123", email="test@example.com")
    client, auth_store = _create_test_client_with_stores(tmp_path, cli=cli)
    _authenticate_client(client, auth_store)

    response = client.get("/accounts")
    assert response.status_code == 200
    assert "test@example.com" in response.text


def test_accounts_page_no_longer_hosts_error_reporting_toggles(tmp_path: Path) -> None:
    """The error-reporting toggles moved off the manage-accounts page to the dedicated Settings page."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/accounts")
    assert response.status_code == 200
    assert "report-errors-toggle" not in response.text


def test_settings_page_requires_auth(tmp_path: Path) -> None:
    """The /settings page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/settings")
    assert response.status_code == 403


def test_settings_page_hosts_error_reporting_toggles(tmp_path: Path) -> None:
    """The Settings page hosts the per-machine error-reporting toggles, seeded from config."""
    MindsConfig(data_dir=tmp_path).set_report_unexpected_errors(True)
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Report unexpected errors" in response.text
    report_input = response.text.split('id="report-errors-toggle"')[1].split(">")[0]
    assert "checked" in report_input
    # With reporting on, the include-logs row is revealed (not ``hidden``).
    logs_row = response.text.split('id="include-logs-row"')[1].split(">")[0]
    assert "hidden" not in logs_row


def test_workspace_settings_page_requires_auth(tmp_path: Path) -> None:
    """The workspace settings page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/workspace/agent-123/settings")
    assert response.status_code == 403


def test_workspace_settings_shows_unassociated_workspace(tmp_path: Path) -> None:
    """A workspace not associated with any account shows the associate prompt."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    test_agent_id = AgentId()
    response = client.get(f"/workspace/{test_agent_id}/settings")
    assert response.status_code == 200
    assert "associated with an account" in response.text.lower()


def test_inbox_requires_auth(tmp_path: Path) -> None:
    """The inbox page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/inbox")
    assert response.status_code == 200
    assert "Not authenticated" in response.text


def test_inbox_empty_state(tmp_path: Path) -> None:
    """With no pending requests, the inbox renders the empty-state placeholder
    and applies the ``is-empty`` body class for the centered-message layout."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/inbox")
    assert response.status_code == 200
    body = response.text
    assert "No pending requests" in body
    # The ``is-empty`` class must be on the ``inbox-body`` element itself.
    # The substring appears unconditionally inside the page's <style> block
    # (rules keyed on ``inbox-body.is-empty``), so target the opening tag's
    # attribute span specifically.
    tag_start = body.find('id="inbox-body"')
    tag_end = body.find(">", tag_start)
    assert tag_start != -1
    assert "is-empty" in body[tag_start:tag_end]
    # Should not include any inbox-card markup when empty.
    assert 'class="inbox-card' not in body


class _InboxStubLatchkeyHandler(RequestEventHandler):
    """Minimal LATCHKEY_PERMISSION handler used by the inbox tests.

    Produces a deterministic fragment that echoes the request's
    rationale so the master/detail tests can assert on the right pane's
    contents without standing up the real latchkey gateway/catalog
    machinery.
    """

    def handles_request_type(self) -> str:
        return str(RequestType.LATCHKEY_PERMISSION)

    def kind_label(self) -> str:
        return "permission"

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return ""
        return req_event.scope

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return ""
        return f'<div class="permissions-detail">{req_event.rationale}</div>'

    def apply_grant_request(self, request: Request, req_event: RequestEvent) -> Response:
        return make_response(content='{"outcome": "GRANTED"}', media_type="application/json")

    def apply_deny_request(self, request: Request, req_event: RequestEvent) -> Response:
        return make_response(content='{"outcome": "DENIED"}', media_type="application/json")


def _build_inbox_test_app(
    tmp_path: Path,
    request_inbox: RequestInbox,
) -> tuple[FlaskClient, FileAuthStore]:
    """Build an authenticated test client wired with a stub latchkey handler.

    The stub returns a fragment that echoes the rationale so the master/
    detail tests can assert on the right pane's contents without
    standing up the real latchkey gateway/catalog machinery.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = make_session_store_for_test(tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    # The inbox display hides requests whose agent can't be resolved to a
    # host; these tests exercise the running-workspace case, so use a
    # resolver that treats every agent as known.
    backend_resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
        request_event_handlers=(_InboxStubLatchkeyHandler(),),
    )
    client = app.test_client()
    _authenticate_client(client, auth_store)
    return client, auth_store


def test_inbox_master_detail_renders_first_pending_by_default(tmp_path: Path) -> None:
    """With pending requests but no ``?selected``, the inbox auto-selects the
    first (most-recent) pending item and renders its detail in the right pane."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="Need to post status updates"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_inbox_test_app(tmp_path, request_inbox)

    response = client.get("/inbox")
    assert response.status_code == 200
    body = response.text

    # The list contains a card with the event's id as a data attribute.
    assert f'data-request-id="{event.event_id}"' in body
    # The empty-state placeholder must not be present when the inbox has
    # pending items.
    assert "No pending requests" not in body
    # The right-pane detail fragment was composed server-side and includes
    # the rationale.
    assert "Need to post status updates" in body


def test_inbox_preselects_query_param(tmp_path: Path) -> None:
    """``?selected=<id>`` of a pending request renders that detail."""
    agent_id = str(AgentId())
    first = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="first request"
    )
    second = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="second request"
    )
    request_inbox = RequestInbox().add_request(first).add_request(second)
    client, _ = _build_inbox_test_app(tmp_path, request_inbox)

    # Request the earlier event (not the most-recent default).
    response = client.get(f"/inbox?selected={first.event_id}")
    assert response.status_code == 200
    body = response.text
    # The selected card carries the ``is-selected`` class.
    assert "is-selected" in body
    assert f'data-request-id="{first.event_id}"' in body
    # The server-rendered detail shows the selected request's rationale, not
    # the default-first-pending one.
    assert "first request" in body
    assert "second request" not in body


def test_inbox_stale_selected_renders_unavailable(tmp_path: Path) -> None:
    """``?selected=<unknown_id>`` keeps the list intact and surfaces an
    unavailable message in the right pane."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="ongoing"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_inbox_test_app(tmp_path, request_inbox)

    response = client.get("/inbox?selected=evt-unknown-id")
    assert response.status_code == 200
    body = response.text
    # The right pane shows the "no longer available" message...
    assert "no longer available" in body
    # ...but the list still includes the legitimate pending card so the
    # user can pick another item.
    assert f'data-request-id="{event.event_id}"' in body


def test_inbox_list_fragment_returns_just_the_list(tmp_path: Path) -> None:
    """``GET /inbox/list`` returns the left-list fragment without a full HTML doc."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="for testing"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_inbox_test_app(tmp_path, request_inbox)

    response = client.get("/inbox/list")
    assert response.status_code == 200
    body = response.text
    assert f'data-request-id="{event.event_id}"' in body
    # Fragment-only: no <html>, no <body>, no backdrop.
    assert "<html" not in body
    assert "<body" not in body
    assert "inbox-backdrop" not in body


def test_inbox_list_fragment_empty_returns_placeholder(tmp_path: Path) -> None:
    """``GET /inbox/list`` with no pending requests returns the placeholder."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/inbox/list")
    assert response.status_code == 200
    body = response.text
    assert "inbox-empty-placeholder" in body
    assert "No pending requests" in body


def test_inbox_detail_fragment_returns_just_the_detail(tmp_path: Path) -> None:
    """``GET /inbox/detail/<id>`` returns the right-pane fragment."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="detail testing"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_inbox_test_app(tmp_path, request_inbox)

    response = client.get(f"/inbox/detail/{event.event_id}")
    assert response.status_code == 200
    body = response.text
    assert "detail testing" in body
    # Fragment-only: no <html>, no backdrop, no inbox shell JS.
    assert "<html" not in body
    assert "inbox-backdrop" not in body
    # The fragment must not include the shell's permissions-form submit
    # JS or its escape/backdrop handlers; those live in the inbox page.
    assert 'addEventListener("keydown"' not in body
    assert "submitPermissionDeny = function" not in body


def test_inbox_detail_fragment_for_unknown_id_returns_unavailable_200(tmp_path: Path) -> None:
    """An unknown id resolves to the "no longer available" fragment with HTTP 200
    so the inbox shell JS can innerHTML-swap the response directly."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/inbox/detail/evt-nonexistent-id")
    assert response.status_code == 200
    assert "no longer available" in response.text


def test_inbox_auto_open_checkbox_reflects_config(tmp_path: Path) -> None:
    """The header checkbox is pre-checked when the config has auto-open enabled."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    # Default (no config write): auto-open is True, checkbox is checked.
    response = client.get("/inbox")
    body = response.text
    assert 'id="inbox-auto-open"' in body
    assert "checked" in body[body.find('id="inbox-auto-open"') : body.find(">", body.find('id="inbox-auto-open"'))]

    # Flip the setting to False and confirm the checkbox renders unchecked.
    config = MindsConfig(data_dir=tmp_path)
    config.set_auto_open_requests_panel(False)
    response = client.get("/inbox")
    body = response.text
    tag_start = body.find('id="inbox-auto-open"')
    tag_end = body.find(">", tag_start)
    assert "checked" not in body[tag_start:tag_end]


def test_inbox_shell_reapplies_selection_after_list_refresh(tmp_path: Path) -> None:
    """The inbox shell JS re-applies the highlight after an SSE-driven list refresh.

    Regression guard: ``/inbox/list`` is selection-agnostic and always
    renders with ``selected_id=""``. When an SSE ``requests`` event arrives
    and ``fetchListFragment()`` rebuilds the list innerHTML, the previously
    highlighted card loses its ``.is-selected`` class. If the selection is
    still in the new pending set, the shell must call
    ``setSelectedCard(currentId)`` to restore the highlight; otherwise the
    user sees their selection visibly disappear despite not changing it.
    """
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/inbox")
    assert response.status_code == 200
    body = response.text
    # The SSE handler must call setSelectedCard(currentId) in the
    # "selection still pending" branch.
    assert "setSelectedCard(currentId)" in body


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


def test_auto_open_toggle(tmp_path: Path) -> None:
    """The inbox auto-open setting can be toggled.

    The on-disk setting key and the toggle route both keep
    ``requests-panel`` / ``auto_open_requests_panel`` for backward
    compatibility with existing user configs; "panel" now refers to the
    inbox modal.
    """
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post(
        "/_chrome/requests-auto-open",
        json={"enabled": False},
    )
    assert response.status_code == 200

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_auto_open_requests_panel() is False


# -- error-reporting consent + settings tests --


def test_landing_shows_login_not_consent_when_unauthenticated(tmp_path: Path) -> None:
    """The consent screen sits after login: an unauthenticated "/" shows the login prompt, not consent."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "Help improve Minds" not in response.text
    assert "Login" in response.text


def test_landing_shows_consent_screen_after_login_when_unanswered(tmp_path: Path) -> None:
    """Once authenticated, "/" shows the consent screen until it is answered."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get("/")
    assert response.status_code == 200
    assert "Help improve Minds" in response.text
    assert "Report unexpected errors" in response.text


def test_welcome_continue_without_account_routes_through_consent(tmp_path: Path) -> None:
    """ "Continue without an account" sends the user to "/" so the consent screen is offered.

    Reporting is not gated behind an Imbue account: the account-less skip path lands on "/", whose
    handler shows the "Help improve Minds" consent screen (when unanswered) before the create form.
    """
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    welcome = client.get("/welcome")
    assert welcome.status_code == 200
    # Isolate the full opening <a> tag that carries the skip-account id, regardless of
    # attribute order, and assert it links to "/" (the consent-bearing landing route)
    # rather than straight to "/create".
    before, after = welcome.text.split('id="skip-account-btn"', 1)
    skip_tag = before.rsplit("<a", 1)[1] + after.split(">", 1)[0]
    assert 'href="/"' in skip_tag
    # Following that link while consent is unanswered shows the consent screen.
    landing = client.get("/")
    assert "Help improve Minds" in landing.text


def test_consent_page_requires_auth(tmp_path: Path) -> None:
    """GET /consent bounces an unauthenticated request to the login page."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/consent")
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_consent_submit_requires_auth(tmp_path: Path) -> None:
    """POST /consent rejects an unauthenticated request and persists nothing."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/consent", json={"report_unexpected_errors": True, "include_logs": True})
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


def test_consent_submit_records_choices_and_unblocks_landing(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/consent", json={"report_unexpected_errors": True, "include_logs": True})
    assert response.status_code == 200

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_error_reporting_consent_given() is True
    assert config.get_report_unexpected_errors() is True
    assert config.get_include_error_logs() is True

    # With consent answered, the authenticated "/" no longer shows the consent screen.
    landing = client.get("/")
    assert "Help improve Minds" not in landing.text


def test_consent_submit_does_not_persist_logs_without_reporting(tmp_path: Path) -> None:
    """ "Include logs" is only meaningful with reporting on, so it is not persisted otherwise."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/consent", json={"report_unexpected_errors": False, "include_logs": True})
    assert response.status_code == 200

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_error_reporting_consent_given() is True
    assert config.get_report_unexpected_errors() is False
    assert config.get_include_error_logs() is False


def test_error_reporting_settings_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/error-reporting", json={"report_unexpected_errors": True})
    assert response.status_code == 403
    # Nothing was persisted.
    config = MindsConfig(data_dir=tmp_path)
    assert config.get_report_unexpected_errors() is False


def test_error_reporting_settings_persist_each_toggle(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)

    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": True}).status_code == 200
    assert client.post("/_chrome/error-reporting", json={"include_logs": True}).status_code == 200

    config = MindsConfig(data_dir=tmp_path)
    assert config.get_report_unexpected_errors() is True
    assert config.get_include_error_logs() is True

    # A partial update touches only the named key.
    assert client.post("/_chrome/error-reporting", json={"report_unexpected_errors": False}).status_code == 200
    config = MindsConfig(data_dir=tmp_path)
    assert config.get_report_unexpected_errors() is False
    assert config.get_include_error_logs() is True


# -- get-help / report-a-bug tests --


def test_help_page_renders_report_option(tmp_path: Path) -> None:
    """The help page renders the report-a-bug flow; the agent-help option is present but disabled."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert response.status_code == 200
    assert "Report a bug to Imbue" in response.text
    assert "Have an agent help fix the problem" in response.text
    # The agent-help radio is disabled in this phase.
    agent_radio = response.text.split('value="agent"')[1].split(">")[0]
    assert "disabled" in agent_radio


def test_help_page_hides_include_logs_checkbox_when_setting_on(tmp_path: Path) -> None:
    """With the persistent include-logs setting on, logs are always attached and the checkbox is hidden."""
    MindsConfig(data_dir=tmp_path).set_include_error_logs(True)
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert 'id="help-include-logs"' not in response.text


def test_help_page_shows_include_logs_checkbox_when_setting_off(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert 'id="help-include-logs"' in response.text


def test_help_page_shows_checkboxes_inline_and_report_id_affordance(tmp_path: Path) -> None:
    """The diagnostics checkboxes are top-level (no Advanced disclosure) and the confirmation can show
    a copyable report ID."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert response.status_code == 200
    # Checkboxes are rendered directly, not hidden behind an Advanced <details> disclosure.
    assert "<details" not in response.text
    assert 'id="help-app-diagnostics"' in response.text
    assert 'id="help-remote-access"' in response.text
    # The confirmation hosts a copyable report-ID slot populated from the response's event_id.
    assert 'id="help-event-id"' in response.text
    assert 'id="help-copy-id-btn"' in response.text


def test_help_report_requires_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/report", json={"description": "  "})
    assert response.status_code == 400


def test_help_report_accepts_a_description(tmp_path: Path) -> None:
    # Sentry is not initialized in tests, so the report is collected and the route returns ok with a
    # null event_id (nothing was actually transmitted). This exercises the full collect path end to end.
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post(
        "/help/report",
        json={"description": "the app froze", "include_app_diagnostics": True, "remote_access": True},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["event_id"] is None


def test_served_page_omits_frontend_sentry_when_reporting_off(tmp_path: Path) -> None:
    # Default shipped state: report_unexpected_errors is off, so a page served by the backend must
    # not boot the frontend Sentry SDK. This is the unified gate -- the browser honors the same user
    # setting as the backend rather than the old MINDS_SENTRY_ENABLED env var.
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert response.status_code == 200
    assert "minds-sentry-config" not in response.text
    assert "sentry.browser.min.js" not in response.text


def test_served_page_emits_frontend_sentry_when_reporting_on(tmp_path: Path) -> None:
    # With the user's report_unexpected_errors setting on, a served page boots the frontend Sentry
    # SDK. The setting is read live per render, so flipping it (as the consent screen / settings do)
    # takes effect on the next page load without restarting the backend.
    MindsConfig(data_dir=tmp_path).set_report_unexpected_errors(True)
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert response.status_code == 200
    assert '<script type="application/json" id="minds-sentry-config">' in response.text
    assert "sentry.browser.min.js" in response.text


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


def test_api_v1_bug_report_accepts_authorized_request(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(
        f"/api/v1/agents/{AgentId()}/report",
        json={"description": "agent saw an error"},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_api_v1_bug_report_rejects_empty_description(tmp_path: Path) -> None:
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    response = client.post(
        f"/api/v1/agents/{AgentId()}/report",
        json={"description": ""},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert response.status_code == 400


# -- system-interface restart + recovery tests --


def test_build_mngr_stop_argv_appends_stop_host_only_for_host_restart() -> None:
    """The host tier adds --stop-host; the surgical tier stops just the agent."""
    aid = AgentId.generate()

    surgical = _build_mngr_stop_argv("/usr/local/bin/mngr", aid, is_host_restart=False)
    assert surgical[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" not in surgical

    host = _build_mngr_stop_argv("/usr/local/bin/mngr", aid, is_host_restart=True)
    assert host[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" in host


def test_build_mngr_start_argv_targets_the_agent() -> None:
    aid = AgentId.generate()
    argv = _build_mngr_start_argv("/usr/local/bin/mngr", aid)
    assert argv[:3] == ["/usr/local/bin/mngr", "start", str(aid)]


def test_provider_error_message_for_workspace_keys_on_this_workspaces_provider() -> None:
    """The provider error message is attributed by exact provider name.

    This is the per-provider keying that keeps a docker mind's recovery from
    being misclassified during a simultaneous imbue_cloud outage: only an error
    whose provider name matches this workspace's is used.
    """
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="could not reach Imbue Cloud",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    matched = _provider_error_message_for_workspace(errors, "imbue_cloud_acme")
    assert matched == "could not reach Imbue Cloud"


def test_provider_error_message_for_workspace_ignores_other_providers() -> None:
    """An error for a different provider is never blamed on this workspace."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker") is None


def test_provider_error_message_for_workspace_is_none_when_provider_unknown() -> None:
    """Pre-discovery (provider unknown), we cannot attribute any error to this workspace."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, None) is None


def test_is_discovery_fresh_distinguishes_recent_from_stale_and_missing() -> None:
    """Freshness gates the recovery redirect: only a recent snapshot is trustworthy."""
    now = datetime.now(timezone.utc)
    assert _is_discovery_fresh(now) is True
    # A snapshot well past the freshness window (a stalled pipeline) is stale.
    assert _is_discovery_fresh(now - timedelta(minutes=5)) is False
    # No snapshot at all (e.g. before initial discovery) cannot be trusted.
    assert _is_discovery_fresh(None) is False


def _drive_to_stuck_with_onset(tracker: SystemInterfaceHealthTracker, agent_id: AgentId) -> datetime:
    """Drive ``agent_id`` to STUCK via the real probe path and return its onset.

    A zero stuck-threshold makes the first probe failure stick immediately, so the
    outage onset is recorded deterministically without sleeping.
    """
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.STUCK
    onset = tracker.get_failure_run_started_wall_at(agent_id)
    assert onset is not None
    return onset


def test_should_emit_system_interface_status_gates_stuck_on_post_onset_snapshot() -> None:
    """The recovery redirect waits for a discovery snapshot taken *after* the outage began.

    STUCK is the only status the chrome redirects on. A snapshot that predates the
    outage still carries the pre-outage host state (a just-stopped container still
    reads RUNNING), so it must not promote the redirect -- only a snapshot at or
    after the outage onset does. Other statuses never gate the redirect.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()

    # Non-STUCK statuses do not drive the redirect, so they are never gated --
    # even with no discovery snapshot and no recorded onset.
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.RESTARTING) is True
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.RESTART_FAILED) is True
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.HEALTHY) is True

    onset = _drive_to_stuck_with_onset(tracker, agent_id)

    # A recent snapshot that nonetheless predates the outage is the exact bug case:
    # it is well within the absolute freshness window but still shows the pre-outage
    # host state, so it must stay suppressed.
    resolver.update_providers(
        providers=(), error_by_provider_name={}, last_full_snapshot_at=onset - timedelta(seconds=1)
    )
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.STUCK) is False

    # A snapshot taken after the outage began reflects it; promote the redirect.
    resolver.update_providers(
        providers=(), error_by_provider_name={}, last_full_snapshot_at=onset + timedelta(seconds=1)
    )
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.STUCK) is True


def test_should_emit_system_interface_status_without_onset_falls_back_to_age() -> None:
    """Without a recorded onset, STUCK gating falls back to the absolute-age freshness check.

    Only the force-``mark_stuck`` path (used in tests) reaches STUCK without a
    probe-failure run, so there is no onset to compare against; the gate then
    behaves as before -- cold start suppresses, a recent snapshot promotes. A
    missing tracker entirely is treated the same way.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    tracker.mark_stuck(agent_id)
    assert tracker.get_failure_run_started_wall_at(agent_id) is None

    # Cold start, no snapshot yet: suppressed.
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.STUCK) is False
    # A recent snapshot promotes via the age fallback.
    resolver.update_providers(
        providers=(), error_by_provider_name={}, last_full_snapshot_at=datetime.now(timezone.utc)
    )
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.STUCK) is True
    # A stale snapshot (a stalled pipeline) suppresses it again.
    resolver.update_providers(
        providers=(),
        error_by_provider_name={},
        last_full_snapshot_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert _should_emit_system_interface_status(resolver, tracker, agent_id, AgentHealth.STUCK) is False
    # No tracker at all behaves identically to a missing onset.
    assert _should_emit_system_interface_status(resolver, None, agent_id, AgentHealth.STUCK) is False


def test_recovery_page_requires_authentication(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)
    response = client.get(f"/agents/{agent_id}/recovery", follow_redirects=False)
    assert response.status_code == 403


def test_recovery_page_renders_for_authenticated_user(tmp_path: Path) -> None:
    # Mark stuck so the page renders -- a HEALTHY agent with a valid return_to
    # 302s straight to return_to (covered by the healthy-redirect test below).
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    tracker.mark_stuck(agent_id)

    # Use a legitimate localhost-subdomain return_to (the real plugin-emitted form).
    safe_return_to = f"http://{agent_id}.localhost:8421/some/path"
    response = client.get(
        f"/agents/{agent_id}/recovery?return_to={safe_return_to}",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert str(agent_id) in response.text
    assert safe_return_to in response.text
    # The recovery page chrome rendered: the host-restart button (the
    # surgical tier is auto-dispatched, so it has no button) and the
    # surgical-restart endpoint the page's JS posts to when the probe
    # reports the container reachable.
    assert "Restart workspace" in response.text
    assert "restart-system-interface" in response.text
    # The recovery page offers an in-page report button that opens the get-help modal
    # via the ``minds:open-help`` relay message.
    assert 'id="recovery-report-btn"' in response.text
    assert "minds:open-help" in response.text


def test_recovery_page_drops_open_redirect_return_to(tmp_path: Path) -> None:
    """A return_to pointing at a non-localhost host must be dropped, not rendered.

    Otherwise the recovery page would be an open-redirect: an attacker could
    craft ``?return_to=https://evil.com/`` and the page would navigate the
    user there after a successful restart.
    """
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client, auth_store)

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to=https://evil.com/phish",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "evil.com" not in response.text
    # The data-return-to attribute should be empty so the page falls back to reload().
    assert 'data-return-to=""' in response.text


def test_recovery_page_drops_protocol_relative_return_to(tmp_path: Path) -> None:
    """Protocol-relative URLs like ``//evil.com/`` must not be treated as relative."""
    client, auth_store, agent_id = _setup_test_server(tmp_path)
    _authenticate_client(client, auth_store)

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to=//evil.com/phish",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "evil.com" not in response.text


def test_recovery_page_allows_relative_return_to(tmp_path: Path) -> None:
    """A same-origin relative path must be preserved.

    Pre-arranges STUCK so the page renders (a HEALTHY agent with a valid
    return_to 302s to it; that path is covered separately).
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    tracker.mark_stuck(agent_id)

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to=/agents/{agent_id}/",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert f"/agents/{agent_id}/" in response.text


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


def test_recovery_page_renders_copy_ssh_button_from_resolver(tmp_path: Path) -> None:
    """End-to-end: the recovery handler pulls the host's SSH info from the
    backend resolver and renders a Copy SSH command button carrying the command.
    """
    agent_id = AgentId()
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    tracker = SystemInterfaceHealthTracker()
    resolver = StaticBackendResolver(
        url_by_agent_and_service={},
        ssh_info_by_agent_id={
            str(agent_id): RemoteSSHInfo(user="root", host="127.0.0.1", port=60022, key_path=Path("/home/u/.mngr/key"))
        },
    )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        system_interface_health_tracker=tracker,
    )
    client = app.test_client()
    _authenticate_client(client=client, auth_store=auth_store)
    tracker.mark_stuck(agent_id)

    response = client.get(f"/agents/{agent_id}/recovery", follow_redirects=False)
    assert response.status_code == 200
    assert 'id="copy-ssh-btn"' in response.text
    assert 'data-ssh-command="ssh -i /home/u/.mngr/key -p 60022 root@127.0.0.1"' in response.text


def test_restart_api_requires_authentication(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)
    response = client.post(f"/api/agents/{agent_id}/restart-system-interface")
    assert response.status_code == 403


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


def _setup_test_server_with_tracker(
    tmp_path: Path,
    tracker: SystemInterfaceHealthTracker,
) -> tuple[FlaskClient, FileAuthStore, AgentId]:
    """Build a test client wired to a real SystemInterfaceHealthTracker.

    The default ``_setup_test_server`` helper doesn't accept a tracker, and
    several tests need to verify the recovery page reads the tracker's
    current state. Constructing a fresh app per test keeps the tests
    isolated from each other.
    """
    agent_id = AgentId()
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        system_interface_health_tracker=tracker,
    )
    client = app.test_client()
    _authenticate_client(client=client, auth_store=auth_store)
    return client, auth_store, agent_id


def test_recovery_page_initial_status_reflects_tracker_stuck(tmp_path: Path) -> None:
    """The recovery page must read the tracker's current health into ``initial_status``.

    Without this wiring the page would always render with ``data-initial-status="healthy"``,
    so the JS would not show the busy state when the user lands on the page mid-restart.
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    tracker.mark_stuck(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.STUCK

    response = client.get(f"/agents/{agent_id}/recovery", follow_redirects=False)

    assert response.status_code == 200
    assert 'data-initial-status="stuck"' in response.text


def test_recovery_page_initial_status_reflects_tracker_restarting(tmp_path: Path) -> None:
    """A user landing on the recovery page during an in-flight restart must see RESTARTING."""
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    tracker.mark_restarting(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.RESTARTING

    response = client.get(f"/agents/{agent_id}/recovery", follow_redirects=False)

    assert response.status_code == 200
    assert 'data-initial-status="restarting"' in response.text


def test_recovery_page_redirects_to_return_to_when_agent_already_healthy(tmp_path: Path) -> None:
    """Regression: if the tracker says HEALTHY at recovery-page-render time, 302 to return_to.

    Catches a real-world race where the chrome SSE pushes ``stuck`` and the
    chrome JS navigates to /recovery, but the background probe loop flips
    the tracker back to HEALTHY in the brief window before the GET lands.
    Without the redirect, ``initial_status="healthy"`` would render the
    "Workspace unresponsive" page and the JS would never auto-reload
    (the SSE doesn't push events for HEALTHY agents).
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    # With no record in the tracker, get_health returns HEALTHY by default.
    assert tracker.get_health(agent_id) == AgentHealth.HEALTHY
    safe_return_to = f"http://{agent_id}.localhost:8421/"

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to={safe_return_to}",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == safe_return_to


def test_recovery_page_renders_for_healthy_agent_with_explicit_restart_intent(tmp_path: Path) -> None:
    """``intent=restart`` makes the page render for a HEALTHY agent instead of 302ing back.

    The home-page restart control navigates here explicitly. Without the
    intent marker the healthy-redirect guard would bounce the user straight
    back to ``return_to`` and nothing would happen. With it, the page renders
    as STUCK so its JS runs the probe and dispatches a restart.
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    # With no record in the tracker, get_health returns HEALTHY by default.
    assert tracker.get_health(agent_id) == AgentHealth.HEALTHY
    safe_return_to = f"http://{agent_id}.localhost:8421/"

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to={safe_return_to}&intent=restart",
        follow_redirects=False,
    )

    assert response.status_code == 200
    # An explicit restart of a healthy workspace renders as STUCK so the page
    # probes and dispatches rather than sitting idle.
    assert 'data-initial-status="stuck"' in response.text


def test_recovery_page_renders_normally_when_healthy_but_no_return_to(tmp_path: Path) -> None:
    """No return_to + HEALTHY: render the page (with a working restart button) instead of erroring.

    Falls back to the manual restart path. The page itself still renders
    correctly with ``initial_status="healthy"``; the user can hit the
    restart button if they want to.
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)

    response = client.get(f"/agents/{agent_id}/recovery", follow_redirects=False)

    assert response.status_code == 200
    assert 'data-initial-status="healthy"' in response.text


def test_recovery_page_does_not_redirect_when_stuck_even_with_return_to(tmp_path: Path) -> None:
    """STUCK + return_to: still render the page so the user sees the problem + restart button.

    Defends against the cleanup-side regression where the new HEALTHY-only
    redirect accidentally widens to all states.
    """
    tracker = SystemInterfaceHealthTracker()
    client, _, agent_id = _setup_test_server_with_tracker(tmp_path, tracker)
    tracker.mark_stuck(agent_id)
    safe_return_to = f"http://{agent_id}.localhost:8421/"

    response = client.get(
        f"/agents/{agent_id}/recovery?return_to={safe_return_to}",
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert 'data-initial-status="stuck"' in response.text


def _create_readiness_test_client(
    tmp_path: Path,
    edge_response: httpx.Response,
) -> tuple[FlaskClient, FileAuthStore, list[httpx.Request]]:
    """Build a desktop client whose http_client returns ``edge_response`` for any probe.

    Captures every probe request so tests can assert which URL was fetched.
    """
    probed: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        probed.append(request)
        return edge_response

    http_client = httpx.Client(transport=httpx.MockTransport(_handle), follow_redirects=False)
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=http_client,
    )
    return client, auth_store, probed


def test_sharing_readiness_returns_ready_when_edge_returns_access_redirect(tmp_path: Path) -> None:
    """When the probed hostname returns the Cloudflare Access 302, the endpoint reports ready."""
    edge_response = httpx.Response(
        302, headers={"location": "https://team.cloudflareaccess.com/cdn-cgi/access/login/x"}
    )
    client, auth_store, probed = _create_readiness_test_client(tmp_path, edge_response)
    _authenticate_client(client, auth_store)
    agent_id = AgentId()

    share_url = "https://web-abc123.tunnels.example.com"
    response = client.get(
        f"/api/sharing-readiness/{agent_id}/web",
        query_string={"url": share_url},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ready": True}
    assert len(probed) == 1
    assert str(probed[0].url) == share_url


def test_sharing_readiness_returns_not_ready_when_edge_not_live(tmp_path: Path) -> None:
    """A non-redirect edge response (Access not published yet) reports not-ready."""
    edge_response = httpx.Response(200, text="origin is up but Access is not enforced")
    client, auth_store, probed = _create_readiness_test_client(tmp_path, edge_response)
    _authenticate_client(client, auth_store)
    agent_id = AgentId()

    response = client.get(
        f"/api/sharing-readiness/{agent_id}/web",
        query_string={"url": "https://web-abc123.tunnels.example.com"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ready": False}
    assert len(probed) == 1


def test_sharing_readiness_does_not_probe_non_https_url(tmp_path: Path) -> None:
    """A non-probeable URL (e.g. http/localhost) reports not-ready without any network probe."""
    edge_response = httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/login"})
    client, auth_store, probed = _create_readiness_test_client(tmp_path, edge_response)
    _authenticate_client(client, auth_store)
    agent_id = AgentId()

    response = client.get(
        f"/api/sharing-readiness/{agent_id}/web",
        query_string={"url": "http://web-abc123.tunnels.example.com"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ready": False}
    assert len(probed) == 0


def test_sharing_readiness_requires_authentication(tmp_path: Path) -> None:
    """The readiness endpoint rejects unauthenticated callers."""
    edge_response = httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/login"})
    client, _, probed = _create_readiness_test_client(tmp_path, edge_response)
    agent_id = AgentId()

    response = client.get(
        f"/api/sharing-readiness/{agent_id}/web",
        query_string={"url": "https://web-abc123.tunnels.example.com"},
    )

    assert response.status_code == 403
    assert len(probed) == 0


# -- restart sequence (background worker) tests --


def _write_fake_mngr(tmp_path: Path, stop_exit: int = 0, start_exit: int = 0) -> str:
    """Write an executable stub that stands in for the ``mngr`` binary.

    Exits per-subcommand so a test can simulate a failing stop or start
    without a real mngr / provider. Every invocation appends its argv to a
    ``<script>.log`` sibling file so a test can assert which subcommands ran
    (e.g. that the stop step was skipped).
    """
    script = tmp_path / "fake_mngr"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$0.log"\n'
        f'case "$1" in\n  stop) exit {stop_exit} ;;\n  start) exit {start_exit} ;;\n  *) exit 0 ;;\nesac\n'
    )
    script.chmod(0o755)
    return str(script)


def _read_fake_mngr_invocations(mngr_binary: str) -> list[str]:
    """Return the recorded argv lines for a ``_write_fake_mngr`` stub (empty if never invoked)."""
    log_path = Path(mngr_binary + ".log")
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()


def _resolver_with_system_services(workspace_agent: AgentId, services_agent: AgentId) -> MngrCliBackendResolver:
    """Build a resolver where the workspace agent and system-services agent share a host."""
    host_id = HostId.generate()
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(workspace_agent, services_agent),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=workspace_agent,
                    agent_name=AgentName("my-claude-agent"),
                    provider_name=ProviderInstanceName("docker"),
                ),
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=services_agent,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName("docker"),
                ),
            ),
        )
    )
    return resolver


def test_run_restart_sequence_fails_when_system_services_agent_is_unresolved(tmp_path: Path) -> None:
    """With no system-services agent discovered, the sequence ends in RESTART_FAILED."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=False,
            tracker=tracker,
            backend_resolver=MngrCliBackendResolver(),
            mngr_binary="mngr",
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "system-services" in (tracker.get_last_restart_error(workspace_agent) or "")


def test_run_restart_sequence_fails_when_stop_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr stop`` ends the sequence in RESTART_FAILED naming the stop step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=False,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, stop_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")


def test_run_restart_sequence_fails_when_stop_command_cannot_launch(tmp_path: Path) -> None:
    """A launch failure (missing ``mngr`` binary) surfaces as RESTART_FAILED naming the stop step.

    Exercises the path where ``_run_mngr`` wraps the ``OSError`` from the failed
    fork/exec into a ``MngrCommandError`` and the restart sequence catches that
    single domain error at the call site.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
    missing_binary = str(tmp_path / "definitely_not_a_real_mngr")

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=False,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=missing_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")


def test_run_restart_sequence_recovers_on_clean_dispatch_without_plugin(tmp_path: Path) -> None:
    """Clean stop+start with no plugin route to probe through recovers the agent to HEALTHY."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=True,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY


def test_run_restart_sequence_skips_stop_when_host_already_stopped(tmp_path: Path) -> None:
    """``skip_stop=True`` on a host restart goes straight to ``mngr start`` (no stop subprocess)."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=True,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=True,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    assert any(line.startswith("start ") for line in invocations)
    assert not any(line.startswith("stop ") for line in invocations)


def test_run_restart_sequence_stops_before_start_by_default(tmp_path: Path) -> None:
    """Without ``skip_stop``, a host restart stops the host before starting it."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent)
    resolver = _resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        _run_restart_sequence(
            workspace_agent_id=workspace_agent,
            is_host_restart=True,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    stop_index = next((i for i, line in enumerate(invocations) if line.startswith("stop ")), None)
    start_index = next((i for i, line in enumerate(invocations) if line.startswith("start ")), None)
    assert stop_index is not None, invocations
    assert start_index is not None, invocations
    assert stop_index < start_index


def test_restart_host_api_requires_authentication(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)
    response = client.post(f"/api/agents/{agent_id}/restart-host")
    assert response.status_code == 403


def test_host_health_api_requires_authentication(tmp_path: Path) -> None:
    client, _, agent_id = _setup_test_server(tmp_path)
    response = client.get(f"/api/agents/{agent_id}/host-health")
    assert response.status_code == 403


# -- Workspace color route ---------------------------------------------
#
# POST /api/workspaces/<agent_id>/color writes the per-workspace color
# label via ``mngr label`` (CLI merge semantics). Tests cover the
# error responses (403 unauthenticated / 400 invalid_hex / 404 not_primary /
# 409 stale_provider / 502 host_unreachable) and the success path through
# a fake mngr stub. The optimistic-resolver-update path is unit-tested
# in backend_resolver_test.py; here we cover the route's plumbing.


def _make_workspace_color_resolver(
    agent_id: AgentId, provider_name: str = "docker", extra_labels: dict[str, str] | None = None
) -> MngrCliBackendResolver:
    """Build a resolver carrying a single primary-workspace agent.

    Primary workspaces are filtered by the ``workspace`` + ``is_primary``
    label pair (matches MngrCliBackendResolver.list_known_workspace_ids).
    """
    labels = {"workspace": "true", "is_primary": "true", **(extra_labels or {})}
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent_id,),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=HostId.generate(),
                    agent_id=agent_id,
                    agent_name=AgentName(str(agent_id)),
                    provider_name=ProviderInstanceName(provider_name),
                    certified_data={"labels": labels},
                ),
            ),
        )
    )
    return resolver


def _create_test_desktop_client_with_color_runtime(
    tmp_path: Path,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    concurrency_group: ConcurrencyGroup | None,
) -> tuple[FlaskClient, FileAuthStore]:
    """Like ``_create_test_desktop_client`` but wires mngr_binary + concurrency
    group so the workspace-color route can shell out to a fake ``mngr label``."""
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        mngr_binary=mngr_binary,
        root_concurrency_group=concurrency_group,
        mngr_host_dir=tmp_path / "mngr",
    )
    return app.test_client(), auth_store


def test_set_workspace_color_requires_authentication(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    resolver = _make_workspace_color_resolver(agent_id)
    client, _ = _create_test_desktop_client_with_color_runtime(
        tmp_path=tmp_path, backend_resolver=resolver, mngr_binary="mngr", concurrency_group=None
    )
    response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "#0b292b"})
    assert response.status_code == 403


def test_set_workspace_color_rejects_invalid_hex(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    resolver = _make_workspace_color_resolver(agent_id)
    client, auth_store = _create_test_desktop_client_with_color_runtime(
        tmp_path=tmp_path, backend_resolver=resolver, mngr_binary="mngr", concurrency_group=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "not-a-hex"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_hex"


def test_set_workspace_color_rejects_non_primary_agent(tmp_path: Path) -> None:
    """An agent without the ``workspace`` + ``is_primary`` label pair (e.g.
    the sibling system-services agent) is not a valid color-write target.
    The resolver returns 404 ``not_primary``."""
    primary_id = AgentId.generate()
    services_id = AgentId.generate()
    host = HostId.generate()
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(primary_id, services_id),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host,
                    agent_id=primary_id,
                    agent_name=AgentName("user-agent"),
                    provider_name=ProviderInstanceName("docker"),
                    certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
                ),
                DiscoveredAgent(
                    host_id=host,
                    agent_id=services_id,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName("docker"),
                    certified_data={"labels": {}},
                ),
            ),
        )
    )
    client, auth_store = _create_test_desktop_client_with_color_runtime(
        tmp_path=tmp_path, backend_resolver=resolver, mngr_binary="mngr", concurrency_group=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/workspaces/{services_id}/color", json={"hex": "#0b292b"})
    assert response.status_code == 404
    assert response.get_json()["error"] == "not_primary"


def test_set_workspace_color_rejects_stale_provider(tmp_path: Path) -> None:
    """A workspace whose provider's latest discovery poll errored is
    flagged is_stale and is not a safe color-write target -- writes
    against an unreachable host would not be observable. Returns 409."""
    agent_id = AgentId.generate()
    provider_name = "imbue_cloud_acct"
    resolver = _make_workspace_color_resolver(agent_id, provider_name=provider_name)
    errored = ProviderInstanceName(provider_name)
    resolver.update_providers(
        providers=(),
        error_by_provider_name={
            errored: DiscoveryError(type_name="RuntimeError", message="boom", provider_name=errored)
        },
        last_full_snapshot_at=datetime.now(timezone.utc),
    )
    client, auth_store = _create_test_desktop_client_with_color_runtime(
        tmp_path=tmp_path, backend_resolver=resolver, mngr_binary="mngr", concurrency_group=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "#0b292b"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "stale_provider"


def test_set_workspace_color_returns_502_when_mngr_label_fails(tmp_path: Path) -> None:
    """A non-zero ``mngr label`` exit (host unreachable, label-mode bug,
    etc.) surfaces as 502 with the detail in the response so the
    settings UI can show an inline error."""
    agent_id = AgentId.generate()
    resolver = _make_workspace_color_resolver(agent_id)
    # Fake mngr that fails on the ``label`` subcommand.
    fake = tmp_path / "fake_mngr_failing"
    fake.write_text('#!/bin/sh\ncase "$1" in\n  label) echo "label failed" >&2; exit 1 ;;\n  *) exit 0 ;;\nesac\n')
    fake.chmod(0o755)

    with ConcurrencyGroup(name="test-color-failure") as cg:
        client, auth_store = _create_test_desktop_client_with_color_runtime(
            tmp_path=tmp_path,
            backend_resolver=resolver,
            mngr_binary=str(fake),
            concurrency_group=cg,
        )
        _authenticate_client(client=client, auth_store=auth_store)
        response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "#0b292b"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "host_unreachable"


def test_set_workspace_color_writes_label_and_updates_resolver(tmp_path: Path) -> None:
    """End-to-end: a valid POST (a) shells out to ``mngr label`` with the
    normalized hex, (b) optimistically updates the resolver's snapshot,
    (c) returns 200 with the normalized hex."""
    agent_id = AgentId.generate()
    resolver = _make_workspace_color_resolver(agent_id)
    # Fake mngr that logs every invocation but always exits clean.
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-color-success") as cg:
        client, auth_store = _create_test_desktop_client_with_color_runtime(
            tmp_path=tmp_path,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            concurrency_group=cg,
        )
        _authenticate_client(client=client, auth_store=auth_store)

        # Lenient input ``"FFF"`` should normalize to ``"#ffffff"`` server-side.
        response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "FFF"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["agent_id"] == str(agent_id)
    assert body["color"] == "#ffffff"

    # Fake mngr captured the label argv with the normalized hex.
    invocations = _read_fake_mngr_invocations(mngr_binary)
    label_lines = [line for line in invocations if line.startswith("label ")]
    assert len(label_lines) == 1, invocations
    assert f"label {agent_id} -l color=#ffffff" in label_lines[0]

    # The resolver's cached snapshot reflects the new color, so the
    # next SSE workspaces emit will carry it.
    assert resolver.get_workspace_color(agent_id) == "#ffffff"


def test_set_workspace_color_returns_502_when_concurrency_group_missing(tmp_path: Path) -> None:
    """If the desktop client was created without a concurrency group
    (a test-only path), the color route cannot shell out and returns
    502 with the missing-runtime detail."""
    agent_id = AgentId.generate()
    resolver = _make_workspace_color_resolver(agent_id)
    client, auth_store = _create_test_desktop_client_with_color_runtime(
        tmp_path=tmp_path, backend_resolver=resolver, mngr_binary="mngr", concurrency_group=None
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.post(f"/api/workspaces/{agent_id}/color", json={"hex": "#0b292b"})
    assert response.status_code == 502
    assert response.get_json()["error"] == "host_unreachable"
