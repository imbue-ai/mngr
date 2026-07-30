"""Unit tests for the friendly navigation-level 404/405 error page.

A routing-level 404/405 reached by a real page navigation renders the
minds-styled RequestError page with a way back home, while fetch/XHR callers
(who read resp.ok, not the body) keep the raw status and default body.
"""

from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie


def _client(tmp_path: Path) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
    )
    client = app.test_client()
    # Authenticate: the app's auth preprocessing redirects anonymous page
    # requests to login before routing raises its 404/405, which would mask
    # the error-page behavior under test.
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def test_document_navigation_to_unknown_path_renders_the_friendly_404_page(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/no-such-page", headers={"Sec-Fetch-Dest": "document"})

    assert response.status_code == 404
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert "Page not found" in body
    # The whole point: the page always offers a way back to the workspace list.
    assert "Back to machines" in body
    assert 'href="/"' in body


def test_document_navigation_to_post_only_route_renders_the_friendly_405_page(tmp_path: Path) -> None:
    # A link pointing at a POST-only route (e.g. persisted by an old session)
    # must not strand the user on werkzeug's bare Method Not Allowed page.
    client = _client(tmp_path)

    response = client.get("/settings/permissions/revoke", headers={"Sec-Fetch-Dest": "document"})

    assert response.status_code == 405
    body = response.get_data(as_text=True)
    # The apostrophe in "can't" is HTML-escaped, so match around it.
    assert "be opened as a page." in body
    assert "Back to machines" in body


def test_fetch_style_request_keeps_the_raw_404(tmp_path: Path) -> None:
    # fetch()/XHR callers read resp.ok, not the body; they must keep the raw
    # status body (Chromium sends Sec-Fetch-Dest: empty for fetches).
    client = _client(tmp_path)

    response = client.get("/no-such-page", headers={"Sec-Fetch-Dest": "empty", "Accept": "*/*"})

    assert response.status_code == 404
    assert "Back to machines" not in response.get_data(as_text=True)


def test_accept_header_fallback_detects_a_navigation_without_sec_fetch_dest(tmp_path: Path) -> None:
    # Clients that don't send Sec-Fetch-Dest are classified by Accept:
    # a text/html request is a navigation, a */* request is not.
    client = _client(tmp_path)

    navigation = client.get("/no-such-page", headers={"Accept": "text/html,application/xhtml+xml"})
    fetch_like = client.get("/no-such-page", headers={"Accept": "*/*"})

    assert navigation.status_code == 404
    assert "Back to machines" in navigation.get_data(as_text=True)
    assert fetch_like.status_code == 404
    assert "Back to machines" not in fetch_like.get_data(as_text=True)
