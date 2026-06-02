"""End-to-end tests for the four routes that have migrated to Solid SSR.

These exercise the FastAPI handlers themselves (rather than the
``render_*`` shims directly): the handler pulls the sidecar off
``app.state`` and the shim falls back to the client-render shell when
the sidecar isn't running (which it never is in this test harness --
spawning a real Node subprocess is the acceptance test's job).
"""

from pathlib import Path

import httpx
from starlette.testclient import TestClient

from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.testing import extract_ssr_route_payload


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
