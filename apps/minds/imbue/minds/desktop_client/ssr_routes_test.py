"""End-to-end tests for the four routes that have migrated to Solid SSR.

These exercise the FastAPI handlers themselves (rather than the
``render_*`` shims directly): the handler pulls the sidecar off
``app.state`` and the shim falls back to the client-render shell when
the sidecar isn't running (which it never is in this test harness --
spawning a real Node subprocess is the acceptance test's job).
"""

from pathlib import Path

from starlette.testclient import TestClient

from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.testing import extract_ssr_route_payload
from imbue.mngr.primitives import AgentId


def _make_client(tmp_path: Path) -> tuple[TestClient, FileAuthStore]:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
    )
    return TestClient(app, base_url="http://localhost"), auth_store


def _authenticate(client: TestClient, auth_store: FileAuthStore) -> None:
    signing_key = auth_store.get_signing_key()
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(signing_key=signing_key))


def test_welcome_endpoint_emits_solid_route_payload(tmp_path: Path) -> None:
    client, auth_store = _make_client(tmp_path)
    _authenticate(client, auth_store)
    response = client.get("/welcome")
    assert response.status_code == 200
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "welcome"


def test_welcome_endpoint_falls_back_to_login_when_unauthenticated(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.get("/welcome")
    assert response.status_code == 200
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "login"


def test_login_endpoint_emits_login_redirect_payload(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.get(
        "/login",
        params={"one_time_code": "test-otc-abc123"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "login_redirect"
    assert payload["props"]["one_time_code"] == "test-otc-abc123"


def test_authenticate_with_invalid_code_emits_auth_error_payload(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.get("/authenticate", params={"one_time_code": "never-issued"})
    assert response.status_code == 403
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "auth_error"
    assert "invalid" in payload["props"]["message"].lower()


def test_accounts_endpoint_emits_accounts_route_payload(tmp_path: Path) -> None:
    client, auth_store = _make_client(tmp_path)
    _authenticate(client, auth_store)
    response = client.get("/accounts")
    assert response.status_code == 200
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "accounts"
    # No session store is wired in this harness, so the accounts list is
    # empty and the default account id and enabled map are both empty too.
    assert payload["props"]["accounts"] == []
    assert payload["props"]["default_account_id"] == ""
    assert payload["props"]["enabled_by_user_id"] == {}


def test_render_landing_page_falls_back_to_solid_landing_route() -> None:
    """``render_landing_page`` builds the right Solid payload directly.

    Goes through the public shim rather than the FastAPI route because the
    handler's "no workspaces + discovery done" branch falls through to the
    create form (still Jinja in Batch A) -- so the route-level test would
    only cover one narrow code path. The shim is the contract that matters
    to the Solid layer.
    """
    agent_id = AgentId("agent-" + "a" * 32)
    html = render_landing_page(
        accessible_agent_ids=[agent_id],
        mngr_forward_origin="http://forward",
        agent_names={str(agent_id): "Project A"},
        sidecar=None,
    )
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "landing"
    assert payload["props"]["agent_ids"] == [str(agent_id)]
    assert payload["props"]["mngr_forward_origin"] == "http://forward"
    assert payload["props"]["agent_names"] == {str(agent_id): "Project A"}
    assert payload["props"]["is_discovering"] is False


def test_render_landing_page_with_discovery_in_progress() -> None:
    """The discovering variant carries ``is_discovering=True`` to the Solid layer."""
    html = render_landing_page(
        accessible_agent_ids=(),
        mngr_forward_origin="",
        is_discovering=True,
        sidecar=None,
    )
    payload = extract_ssr_route_payload(html)
    assert payload["route"] == "landing"
    assert payload["props"]["agent_ids"] == []
    assert payload["props"]["is_discovering"] is True


def test_destroying_endpoint_returns_404_without_record(tmp_path: Path) -> None:
    """The destroying handler short-circuits to 404 when no api_v1_paths
    are configured in the harness, before any rendering happens. Smoke-test
    that the handler still constructs after the SSR migration.
    """
    client, auth_store = _make_client(tmp_path)
    _authenticate(client, auth_store)
    response = client.get("/destroying/does-not-exist")
    assert response.status_code == 404


def test_recovery_endpoint_emits_recovery_route_payload(tmp_path: Path) -> None:
    """The recovery handler renders the Solid recovery route via the
    SSR shim. Use a syntactically valid AgentId (mngr-side guards
    enforce the ``agent-`` prefix).
    """
    client, auth_store = _make_client(tmp_path)
    _authenticate(client, auth_store)
    agent_id = "agent-" + "a" * 32
    response = client.get(
        f"/agents/{agent_id}/recovery",
        params={"return_to": ""},
        follow_redirects=False,
    )
    assert response.status_code == 200
    payload = extract_ssr_route_payload(response.text)
    assert payload["route"] == "recovery"
    assert payload["props"]["agent_id"] == agent_id
