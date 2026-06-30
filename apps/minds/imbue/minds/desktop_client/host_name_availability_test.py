"""Tests for the create-form host-name availability check.

Covers the snapshot-reading helper (``create_helpers.taken_host_names_on_provider``)
and the cookie-only ``GET /api/v1/desktop/host-name-available`` endpoint that the
create form's Name field polls for live feedback: provider/account scoping,
exclusion of destroyed workspaces, and case-insensitive matching.
"""

import json
from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.create_helpers import taken_host_names_on_provider
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName

# An imbue_cloud account and the provider instance name it slugifies to (mirrors
# ``imbue_cloud_provider_name_for_account``); used by the account-scoping tests.
_ACCOUNT_USER_ID = "user-abc"
_ACCOUNT_EMAIL = "alice@imbue.com"
_ACCOUNT_PROVIDER = "imbue_cloud_alice-imbue-com"


def _workspace_agent(host_id: HostId, agent_id: AgentId, name: str, provider: str) -> DiscoveredAgent:
    """A primary-workspace agent whose ``workspace`` label carries its host name."""
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("system-services"),
        provider_name=ProviderInstanceName(provider),
        certified_data={"labels": {"workspace": name, "is_primary": "true"}},
    )


def _resolver_with_sample_workspaces() -> MngrCliBackendResolver:
    """A resolver holding three workspaces: docker active, docker destroyed, imbue_cloud active."""
    resolver = MngrCliBackendResolver()
    active_host = HostId.generate()
    destroyed_host = HostId.generate()
    cloud_host = HostId.generate()
    active_agent = AgentId.generate()
    destroyed_agent = AgentId.generate()
    cloud_agent = AgentId.generate()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(active_agent, destroyed_agent, cloud_agent),
            discovered_agents=(
                # Stored with mixed case to exercise case-insensitive matching.
                _workspace_agent(active_host, active_agent, "My-Mind", "docker"),
                _workspace_agent(destroyed_host, destroyed_agent, "ghost", "docker"),
                _workspace_agent(cloud_host, cloud_agent, "cloud-mind", _ACCOUNT_PROVIDER),
            ),
            host_state_by_host_id={str(destroyed_host): HostState.DESTROYED},
        )
    )
    return resolver


def test_taken_host_names_scopes_to_provider_and_excludes_destroyed() -> None:
    resolver = _resolver_with_sample_workspaces()
    # Only the active docker workspace counts; the destroyed one's name is free,
    # and the imbue_cloud workspace is on a different provider instance.
    assert taken_host_names_on_provider(resolver, "docker") == {"my-mind"}


def test_taken_host_names_is_per_provider_instance() -> None:
    resolver = _resolver_with_sample_workspaces()
    assert taken_host_names_on_provider(resolver, _ACCOUNT_PROVIDER) == {"cloud-mind"}
    # A provider with no workspaces has nothing taken.
    assert taken_host_names_on_provider(resolver, "lima") == set()


def _build_authenticated_client(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    session_store: MultiAccountSessionStore | None = None,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
        session_store=session_store,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _available(client: FlaskClient, **params: str) -> bool:
    response = client.get("/api/v1/desktop/host-name-available", query_string=params)
    assert response.status_code == 200
    return json.loads(response.text)["available"]


def test_availability_endpoint_reports_taken_name_case_insensitively(tmp_path: Path) -> None:
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    # Exact and case-variant both collide with the active docker "My-Mind".
    assert _available(client, name="My-Mind", launch_mode="DOCKER") is False
    assert _available(client, name="my-mind", launch_mode="DOCKER") is False
    assert _available(client, name="MY-MIND", launch_mode="DOCKER") is False


def test_availability_endpoint_reports_free_name_available(tmp_path: Path) -> None:
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    assert _available(client, name="brand-new", launch_mode="DOCKER") is True


def test_availability_endpoint_treats_destroyed_name_as_available(tmp_path: Path) -> None:
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    # "ghost" belongs to a destroyed workspace, so the name is reusable.
    assert _available(client, name="ghost", launch_mode="DOCKER") is True


def test_availability_endpoint_scopes_to_selected_provider(tmp_path: Path) -> None:
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    # "My-Mind" is taken on docker but free on lima (a different provider).
    assert _available(client, name="My-Mind", launch_mode="LIMA") is True


def test_availability_endpoint_scopes_imbue_cloud_to_account(tmp_path: Path) -> None:
    fake_cli = make_fake_imbue_cloud_cli()
    fake_cli.add_account(user_id=_ACCOUNT_USER_ID, email=_ACCOUNT_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=fake_cli)
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces(), session_store=session_store)
    # The cloud workspace is taken for this account...
    assert _available(client, name="cloud-mind", launch_mode="IMBUE_CLOUD", account_id=_ACCOUNT_USER_ID) is False
    # ...but a docker-only name is free on the imbue_cloud provider instance.
    assert _available(client, name="My-Mind", launch_mode="IMBUE_CLOUD", account_id=_ACCOUNT_USER_ID) is True


def test_availability_endpoint_reports_available_for_imbue_cloud_without_account(tmp_path: Path) -> None:
    # Without an account the imbue_cloud provider instance can't be named; the
    # form blocks submit on the missing account separately, so report available.
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    assert _available(client, name="cloud-mind", launch_mode="IMBUE_CLOUD") is True


def test_availability_endpoint_reports_available_for_empty_or_malformed_name(tmp_path: Path) -> None:
    client = _build_authenticated_client(tmp_path, _resolver_with_sample_workspaces())
    # Empty -> auto-named server-side; malformed -> can't collide (client owns
    # the format message). Both report available rather than a spurious conflict.
    assert _available(client, name="", launch_mode="DOCKER") is True
    assert _available(client, name="bad.name", launch_mode="DOCKER") is True
    assert _available(client, name="-bad", launch_mode="DOCKER") is True


def test_availability_endpoint_requires_authentication(tmp_path: Path) -> None:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=_resolver_with_sample_workspaces(),
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = app.test_client()
    response = client.get(
        "/api/v1/desktop/host-name-available", query_string={"name": "My-Mind", "launch_mode": "DOCKER"}
    )
    assert response.status_code == 401
