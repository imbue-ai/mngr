"""Unit tests for the static one-time-code login page (GET /login)."""

from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test


def _login_test_client(tmp_path: Path, is_authenticated: bool = False) -> FlaskClient:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )
    return client


def test_login_without_one_time_code_explains_the_terminal_login_url(tmp_path: Path) -> None:
    client = _login_test_client(tmp_path)
    response = client.get("/login")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "one-time login link" in body
    assert "<script>" not in body


def test_login_with_code_js_redirects_to_authenticate(tmp_path: Path) -> None:
    client = _login_test_client(tmp_path)
    response = client.get("/login?one_time_code=abc123def456")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # The JS hop (not an HTTP redirect) keeps prefetchers from consuming the code.
    assert "window.location.replace('/authenticate?one_time_code=' + encodeURIComponent(\"abc123def456\"))" in body


def test_login_with_hostile_code_cannot_break_out_of_the_script_tag(tmp_path: Path) -> None:
    client = _login_test_client(tmp_path)
    response = client.get("/login", query_string={"one_time_code": '</script><img src=x>"'})
    assert response.status_code == 200
    assert "</script><img" not in response.get_data(as_text=True)


def test_login_while_authenticated_redirects_home(tmp_path: Path) -> None:
    client = _login_test_client(tmp_path, is_authenticated=True)
    response = client.get("/login?one_time_code=abc123def456")
    assert response.status_code == 307
    assert response.headers["Location"] == "/"


def test_spa_index_redirects_unauthenticated_navigation_to_login(tmp_path: Path) -> None:
    client = _login_test_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login"
