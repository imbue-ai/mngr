import os
import queue
import subprocess
import threading
from pathlib import Path

import httpx
from flask import Request
from flask import Response
from flask.testing import FlaskClient
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import _build_requests_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _destroying_agent_ids
from imbue.minds.desktop_client.app import _resolve_destroying_for_landing
from imbue.minds.desktop_client.app import _ssh_command_for_agent
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import verify_backup_password
from imbue.minds.desktop_client.backup_password_store import write_backup_password_hash
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
from imbue.minds.desktop_client.help_modal_requests import OpenHelpRequest
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
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
    assert "Where should it run?" in response.text
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
    assert "Where should it run?" in response.text
    assert 'data-preset="remote"' in response.text
    assert 'data-preset="local"' in response.text


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


def test_landing_row_buttons_have_tooltips(tmp_path: Path) -> None:
    """Landing workspace-row action buttons carry data-tooltip labels (rendered
    as in-page custom tooltips by tooltip_triggers.js, since the content view
    has no overlay bridge) rather than native title= attributes, plus an
    aria-label so these icon-only buttons keep an accessible name."""
    agent_id = AgentId()
    backend_resolver = StaticBackendResolver(
        url_by_agent_and_service={str(agent_id): {"web": "http://test:9100"}},
    )
    client, auth_store = _create_test_desktop_client(
        tmp_path=tmp_path,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    _authenticate_client(client=client, auth_store=auth_store)

    response = client.get("/")
    assert response.status_code == 200
    # A normal (non-shutdown-capable) row shows Restart / Open / Settings.
    assert 'data-tooltip="Restart workspace"' in response.text
    assert 'data-tooltip="Open in new window"' in response.text
    assert 'data-tooltip="Settings"' in response.text
    # No native title= tooltips remain on the row buttons.
    assert 'title="Restart workspace"' not in response.text
    assert 'title="Settings"' not in response.text
    # data-tooltip is not exposed to assistive tech, so the aria-labels stay.
    assert 'aria-label="Restart workspace"' in response.text
    assert 'aria-label="Workspace settings"' in response.text
    # The shared trigger script is loaded (via Base), which wires these up and
    # -- absent the window.minds bridge -- renders them in-page.
    assert "/_static/tooltip_triggers.js" in response.text


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
    populated resolver to exercise paths that consult it.

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


def test_creating_page_shows_status(tmp_path: Path) -> None:
    """GET /creating/{agent_id} shows the loading/progress page directly.

    The page no longer interposes any onboarding questions before the
    setting-up screen, so it goes straight to the loading view.
    """
    client, _, agent_creator = _create_test_server_with_agent_creator(tmp_path)

    agent_id = agent_creator.start_creation("file:///nonexistent-repo")

    response = client.get("/creating/{}".format(agent_id))
    assert response.status_code == 200
    assert "Creating your workspace" in response.text
    assert "Setting up your workspace" in response.text
    # The onboarding question UI was removed, so none of its markers render.
    assert "data-question" not in response.text
    assert 'class="opt' not in response.text
    agent_creator.wait_for_all()


def test_creating_page_redirects_to_landing_for_unknown(tmp_path: Path) -> None:
    """GET /creating/{agent_id} falls back to the landing page for an unknown creation.

    The creation registry is in-memory, so a ``/creating/<id>`` window that outlives
    its creation -- reopened after an app restart, or after a failed creation was
    cleaned up -- must redirect rather than dead-end on a bare 404 page.
    """
    client, _, _ = _create_test_server_with_agent_creator(tmp_path)

    response = client.get("/creating/{}".format(CreationId()), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


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


def test_chrome_page_includes_workspace_switcher(tmp_path: Path) -> None:
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    # The workspace switcher menu anchors to the breadcrumb's workspace-name
    # button; the old hamburger toggle is gone.
    assert "workspace-switcher-btn" in response.text
    assert "sidebar-menu" in response.text
    assert "sidebar-toggle" not in response.text


def test_chrome_titlebar_buttons_have_tooltips(tmp_path: Path) -> None:
    """Titlebar buttons carry data-tooltip labels (rendered as custom tooltips on
    the overlay surface) rather than native title= attributes, plus an aria-label
    so these icon-only buttons keep an accessible name for assistive tech."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome")
    assert response.status_code == 200
    assert 'data-tooltip="Switch workspace"' in response.text
    assert 'data-tooltip="Report a bug"' in response.text
    # data-tooltip is not exposed to assistive tech, so each icon-only titlebar
    # button also needs an aria-label to keep an accessible name.
    assert 'aria-label="Switch workspace"' in response.text
    assert 'aria-label="Report a bug"' in response.text


def test_chrome_sidebar_page_renders(tmp_path: Path) -> None:
    """The /_chrome/sidebar route returns the standalone sidebar HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/sidebar")
    assert response.status_code == 200
    assert "sidebar-workspaces" in response.text
    # Interactivity including the SSE fallback has moved to the external JS.
    assert "/_static/sidebar.js" in response.text


def test_chrome_overlay_page_renders(tmp_path: Path) -> None:
    """The /_chrome/overlay route returns the always-warm overlay host HTML."""
    client, _, _ = _setup_test_server(tmp_path)

    response = client.get("/_chrome/overlay")
    assert response.status_code == 200
    assert "overlay-root" in response.text
    assert "/_static/overlay.js" in response.text


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
    """An empty inbox yields a zero count, no pending ids, and no entries."""
    resolver = _AllAgentsKnownStaticResolver(url_by_agent_and_service={})
    expected = {"count": 0, "request_ids": [], "requests": []}
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
    # Each pending request also carries a per-workspace entry so the titlebar
    # badge and the notification click-through can attribute it.
    (entry,) = payload["requests"]
    assert entry["id"] == str(event.event_id)
    assert set(entry) == {"id", "kind_label", "ws_name", "display_name", "workspace_agent_id"}


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
    assert "Back to workspace setup" in response.text
    assert 'href="/create"' in response.text
    # Default explainer banner (no explicit message supplied).
    assert "run your workspace on Imbue Cloud" in response.text


def test_auth_signin_modal_page_renders_overlay_with_auth_form(tmp_path: Path) -> None:
    """GET /auth/signin-modal serves the overlay sign-in page (transparent
    backdrop + the shared auth form) loaded into the shared modal view."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signin-modal")
    assert response.status_code == 200
    assert 'id="signin-modal-backdrop"' in response.text
    assert 'id="signin-form"' in response.text
    assert "run your workspace on Imbue Cloud" in response.text


def test_signin_modal_close_button_has_tooltip(tmp_path: Path) -> None:
    """The sign-in modal's close button (DialogCloseButton) carries a Close tooltip,
    wired by the shared trigger script on the overlay surface."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signin-modal")
    assert response.status_code == 200
    assert 'data-tooltip="Close"' in response.text
    assert "/_static/tooltip_triggers.js" in response.text


def test_auth_page_ignores_unsafe_return_to(tmp_path: Path) -> None:
    """An off-origin return_to is dropped: no back link to it, no explainer."""
    client = _create_test_client_with_auth_routes(tmp_path)
    response = client.get("/auth/signup", query_string={"return_to": "https://evil.com"})
    assert response.status_code == 200
    assert "Back to workspace setup" not in response.text
    assert "evil.com" not in response.text
    assert "run your workspace on Imbue Cloud" not in response.text


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


def test_connections_requires_auth(tmp_path: Path) -> None:
    """The workspace connections page requires authentication."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get(f"/workspace/{AgentId()}/connections")
    assert response.status_code == 403


def test_connections_empty_state(tmp_path: Path) -> None:
    """With nothing pending or granted, the page shows the connectors empty
    state and no "Waiting on you" section."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.get(f"/workspace/{AgentId()}/connections")
    assert response.status_code == 200
    body = response.text
    assert "Waiting on you" not in body
    assert "No connectors have been added yet" in body
    # Interactivity lives in the external Connections shell script.
    assert "/_static/workspace_connections.js" in body


class _ConnectionsStubLatchkeyHandler(RequestEventHandler):
    """Minimal LATCHKEY_PERMISSION handler used by the connections tests.

    Produces a deterministic fragment that echoes the request's
    rationale so the tests can assert on the rendered request cards
    without standing up the real latchkey gateway/catalog machinery.
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


class _NamedWorkspaceStaticResolver(StaticBackendResolver):
    """Resolves every configured agent as a named, host-resolvable workspace.

    The connections page attributes each pending request to its owning
    workspace through the shared workspace name (requests are filed by the
    ``system-services`` sibling agent in production); these tests file the
    request under the primary agent itself, so name resolution maps it
    straight back.
    """

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="localhost")

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return f"ws-{agent_id}"


def _build_connections_test_app(
    tmp_path: Path,
    request_inbox: RequestInbox,
    agent_id: str,
) -> tuple[FlaskClient, FileAuthStore]:
    """Build an authenticated test client wired with a stub latchkey handler.

    The stub returns a fragment that echoes the rationale so the tests can
    assert on the request cards without standing up the real latchkey
    gateway/catalog machinery. The resolver knows ``agent_id`` as a named
    workspace so pending requests attribute to it.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    session_store = make_session_store_for_test(tmp_path)
    minds_config = MindsConfig(data_dir=tmp_path)
    backend_resolver = _NamedWorkspaceStaticResolver(url_by_agent_and_service={agent_id: {}})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        session_store=session_store,
        minds_config=minds_config,
        request_inbox=request_inbox,
        paths=WorkspacePaths(data_dir=tmp_path),
        request_event_handlers=(_ConnectionsStubLatchkeyHandler(),),
    )
    client = app.test_client()
    _authenticate_client(client, auth_store)
    return client, auth_store


def test_connections_renders_pending_request_details(tmp_path: Path) -> None:
    """Pending requests render as "Waiting on you" cards holding the full
    detail fragment (composed server-side)."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="Need to post status updates"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_connections_test_app(tmp_path, request_inbox, agent_id)

    response = client.get(f"/workspace/{agent_id}/connections")
    assert response.status_code == 200
    body = response.text
    assert "Waiting on you" in body
    assert f'data-request-id="{event.event_id}"' in body
    assert "Need to post status updates" in body


def test_connections_only_shows_own_workspace_requests(tmp_path: Path) -> None:
    """A request belonging to another workspace does not render on this
    workspace's connections page."""
    agent_id = str(AgentId())
    other_agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=other_agent_id, scope="slack-api", rationale="for someone else"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_connections_test_app(tmp_path, request_inbox, agent_id)

    response = client.get(f"/workspace/{agent_id}/connections")
    assert response.status_code == 200
    assert "for someone else" not in response.text


def test_connections_marks_selected_request(tmp_path: Path) -> None:
    """``?selected=<id>`` highlights the target request card; other cards
    stay unhighlighted."""
    agent_id = str(AgentId())
    first = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="first request"
    )
    second = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="github-api", rationale="second request"
    )
    request_inbox = RequestInbox().add_request(first).add_request(second)
    client, _ = _build_connections_test_app(tmp_path, request_inbox, agent_id)

    response = client.get(f"/workspace/{agent_id}/connections?selected={first.event_id}")
    assert response.status_code == 200
    body = response.text
    # Both cards render; only the selected one carries ``is-selected``.
    assert "first request" in body
    assert "second request" in body
    selected_tag_start = body.index(f'id="request-{first.event_id}"')
    selected_tag = body[body.rindex("<div", 0, selected_tag_start) : body.index(">", selected_tag_start)]
    assert "is-selected" in selected_tag
    other_tag_start = body.index(f'id="request-{second.event_id}"')
    other_tag = body[body.rindex("<div", 0, other_tag_start) : body.index(">", other_tag_start)]
    assert "is-selected" not in other_tag


def test_connections_ignores_stale_selected(tmp_path: Path) -> None:
    """``?selected=<unknown id>`` renders the page normally with no highlight."""
    agent_id = str(AgentId())
    event = create_latchkey_predefined_permission_request_event(
        agent_id=agent_id, scope="slack-api", rationale="ongoing"
    )
    request_inbox = RequestInbox().add_request(event)
    client, _ = _build_connections_test_app(tmp_path, request_inbox, agent_id)

    response = client.get(f"/workspace/{agent_id}/connections?selected=evt-unknown-id")
    assert response.status_code == 200
    body = response.text
    assert "ongoing" in body
    # No card is highlighted (``is-selected`` appears only in the page's
    # unconditional <style> rules, never on a card tag).
    card_tag_start = body.index(f'id="request-{event.event_id}"')
    card_tag = body[body.rindex("<div", 0, card_tag_start) : body.index(">", card_tag_start)]
    assert "is-selected" not in card_tag


def test_inbox_routes_removed(tmp_path: Path) -> None:
    """The old inbox drawer routes no longer exist."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    assert client.get("/inbox").status_code == 404
    assert client.get("/inbox/list").status_code == 404
    assert client.get("/inbox/detail/evt-anything").status_code == 404
    assert client.post("/_chrome/requests-auto-open", json={"enabled": False}).status_code == 404


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


def test_appearance_toggle_persists_dark_mode(tmp_path: Path) -> None:
    """POST /_chrome/appearance persists the dark-mode setting, which the
    next page render applies on the document root."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/appearance", json={"dark_mode": True})
    assert response.status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_dark_mode() is True

    page = client.get("/settings")
    assert '<html lang="en" class="dark">' in page.text

    response = client.post("/_chrome/appearance", json={"dark_mode": False})
    assert response.status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_dark_mode() is False


def test_default_region_setting_persists_and_validates(tmp_path: Path) -> None:
    """POST /_chrome/default-region writes the imbue_cloud region preference
    (the one the create form pre-selects) and rejects unknown regions."""
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    known = known_regions_for_provider(IMBUE_CLOUD_PROVIDER_KEY)
    response = client.post("/_chrome/default-region", json={"region": known[-1]})
    assert response.status_code == 200
    assert MindsConfig(data_dir=tmp_path).get_region(IMBUE_CLOUD_PROVIDER_KEY) == known[-1]

    response = client.post("/_chrome/default-region", json={"region": "not-a-region"})
    assert response.status_code == 400


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


# -- backup master-password change tests --


def test_backup_password_change_requires_auth(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/_chrome/backup-password", json={"new_password": "x", "new_password_confirm": "x"})
    assert response.status_code == 403
    # The hash authority was not touched (still the startup empty-password seed).
    assert verify_backup_password(WorkspacePaths(data_dir=tmp_path), SecretStr("")) is True


def test_backup_password_change_rejects_mismatched_confirmation(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    response = client.post("/_chrome/backup-password", json={"new_password": "one", "new_password_confirm": "two"})
    assert response.status_code == 400
    assert "match" in response.get_json()["error"]
    assert verify_backup_password(WorkspacePaths(data_dir=tmp_path), SecretStr("")) is True


def test_backup_password_change_updates_the_hash_and_optionally_saves(tmp_path: Path) -> None:
    # No workspaces exist, so the rotation itself is a no-op; the flow still
    # updates the hash authority and (when asked) the plaintext copy.
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    paths = WorkspacePaths(data_dir=tmp_path)

    response = client.post(
        "/_chrome/backup-password",
        json={"new_password": "brand-new", "new_password_confirm": "brand-new", "save_password": True},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["results"] == []
    assert verify_backup_password(paths, SecretStr("brand-new")) is True
    assert verify_backup_password(paths, SecretStr("")) is False
    assert read_saved_backup_password(paths) == "brand-new"


def test_backup_password_change_may_return_to_the_empty_password(tmp_path: Path) -> None:
    client, auth_store = _create_test_client_with_stores(tmp_path)
    _authenticate_client(client, auth_store)
    paths = WorkspacePaths(data_dir=tmp_path)
    write_backup_password_hash(paths, SecretStr("something"))

    response = client.post("/_chrome/backup-password", json={"new_password": "", "new_password_confirm": ""})

    assert response.status_code == 200
    assert verify_backup_password(paths, SecretStr("")) is True


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


def test_help_page_close_button_has_tooltip(tmp_path: Path) -> None:
    """The help dialog's close button carries a custom tooltip wired by the shared
    trigger script (modal pages can render tooltips on the overlay surface too)."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help")
    assert response.status_code == 200
    assert 'data-tooltip="Close"' in response.text
    assert "/_static/tooltip_triggers.js" in response.text


def test_help_page_enables_agent_option_for_a_healthy_workspace(tmp_path: Path) -> None:
    """Opened from a reachable workspace (assist=1), the agent-help option is enabled and the default."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get(f"/help?workspace={AgentId()}&assist=1")
    assert response.status_code == 200
    agent_radio = response.text.split('value="agent"')[1].split(">")[0]
    assert "disabled" not in agent_radio
    assert "checked" in agent_radio


def test_help_page_disables_agent_option_when_workspace_not_reachable(tmp_path: Path) -> None:
    """With a workspace id but no assist=1 (e.g. a loading/stuck workspace), the agent-help option is
    disabled -- spawning a chat there couldn't be seen or used -- while a bug report stays available."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get(f"/help?workspace={AgentId()}")
    assert response.status_code == 200
    agent_radio = response.text.split('value="agent"')[1].split(">")[0]
    assert "disabled" in agent_radio
    # Report is the default when agent help isn't available.
    report_radio = response.text.split('value="report"')[1].split(">")[0]
    assert "checked" in report_radio
    assert "Available once this workspace is responding." in response.text


def test_help_assist_requires_a_workspace(tmp_path: Path) -> None:
    """Agent help is only available inside a workspace, so a request without one is rejected."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "it broke"})
    assert response.status_code == 400


def test_help_assist_requires_a_description(tmp_path: Path) -> None:
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.post("/help/assist", json={"description": "  ", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 400


def test_help_assist_refuses_a_workspace_without_the_assist_skill(tmp_path: Path) -> None:
    """A workspace from an older DEFAULT_WORKSPACE_TEMPLATE (no /assist skill) is refused up front (409) rather than spawning
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
    """A supported workspace probes clean, then the chat is created (probe call + create call)."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_PRESENT\n"))
    client, _ = _create_test_client_with_stores(tmp_path, mngr_caller=caller)
    response = client.post("/help/assist", json={"description": "it broke", "workspace_agent_id": str(AgentId())})
    assert response.status_code == 200
    # First the skill probe, then the inner ``mngr create``.
    assert len(caller.calls) == 2
    assert caller.calls[0][0] == "exec"
    assert caller.calls[1][:2] == ["exec", "--agent"]
    assert "mngr create" in caller.calls[1][3]


def test_help_page_prefills_description_from_query(tmp_path: Path) -> None:
    """When an /assist agent asks the app to open the modal, the description arrives pre-filled."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get("/help?description=the+database+migration+failed")
    assert response.status_code == 200
    assert "the database migration failed" in response.text


def test_help_page_with_prefilled_description_defaults_to_report_mode(tmp_path: Path) -> None:
    """An agent escalation opens the modal with a healthy workspace (assist=1) AND a description; even
    though agent help is available, it must default to the report form (so a human reviews and submits)
    rather than agent-help mode (which would spawn another /assist chat)."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get(f"/help?workspace={AgentId()}&assist=1&description=it+broke")
    assert response.status_code == 200
    agent_radio = response.text.split('value="agent"')[1].split(">")[0]
    report_radio = response.text.split('value="report"')[1].split(">")[0]
    # Agent help is enabled (assist=1) but not the default when a diagnosis was pre-filled.
    assert "disabled" not in agent_radio
    assert "checked" not in agent_radio
    assert "checked" in report_radio


def test_help_page_agent_report_frames_as_agent_submission_and_hides_mode_choice(tmp_path: Path) -> None:
    """An agent escalation (``agent_report=1``) frames the modal as the agent's submission and drops
    the have-an-agent-help / report-a-bug choice -- a report is already underway, so there is nothing
    to choose. The mode radios must not be rendered, and the description is still pre-filled."""
    client, _ = _create_test_client_with_stores(tmp_path)
    response = client.get(f"/help?workspace={AgentId()}&description=it+broke&agent_report=1")
    assert response.status_code == 200
    assert "wants to submit this report" in response.text
    # The mode-choice radios are gone (so the user cannot redirect an agent report into agent-help
    # mode). ``value="agent"`` / ``value="report"`` are unique to those radio inputs -- the submit JS
    # references the mode by ``input[name="help-mode"]`` and bare ``"agent"`` / ``"report"`` strings,
    # so keying off ``value="..."`` isolates the rendered radios from the always-present script.
    assert 'value="agent"' not in response.text
    assert 'value="report"' not in response.text
    # The pre-filled description still survives into the textarea.
    assert "it broke" in response.text


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


def test_api_v1_bug_report_opens_prefilled_modal_instead_of_submitting(tmp_path: Path) -> None:
    """The agent report route does not submit to Sentry: it asks the app to open the report modal
    pre-filled with the agent's description, scoped to the caller's own workspace."""
    client = _create_test_client_with_api_key(tmp_path, api_key="secret-key")
    agent_id = AgentId()
    request_queue: "queue.Queue[OpenHelpRequest]" = queue.Queue()
    wake_event = threading.Event()
    get_state(client.application).help_modal_request_broker.subscribe(request_queue, wake_event)
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
    # The route published an open-help request (scoped to the caller's workspace) instead of submitting.
    received = request_queue.get_nowait()
    assert received.description == "agent saw an error"
    assert received.workspace_agent_id == str(agent_id)


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
    # versioned health + restart endpoints the page's JS drives once the probe
    # reports the container reachable.
    assert "Restart workspace" in response.text
    assert "/api/v1/workspaces/" in response.text
    assert "/health" in response.text
    assert "/restart" in response.text
    # The recovery page offers an in-page report button that opens the get-help modal
    # via the ``minds:open-help`` relay message. It renders hidden by default so it
    # never shows on the transient "Loading workspace" spinner; the recovery JS
    # reveals it only on the terminal restart/retry states.
    assert '<button type="button" id="recovery-report-btn" class="hidden">' in response.text
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
    # The page's background convergence poll keys off this header to tell "still
    # restarting" (keep waiting, no focus-stealing reload) from a state change.
    assert response.headers["X-Recovery-Status"] == "restarting"


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
