"""Bare-origin and subdomain-routing tests for the FastAPI app.

The middleware and forwarding handlers depend on real network I/O
(``httpx`` / paramiko); those paths are exercised via the acceptance
test, not here. This file covers the deterministic auth + routing
surfaces using ``starlette.testclient.TestClient``.
"""

import io
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import OneTimeCode
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.server import _is_loopback_url
from imbue.mngr_forward.server import _sanitize_next_url
from imbue.mngr_forward.server import create_forward_app
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager


@pytest.fixture
def app_setup(tmp_path: Path) -> tuple[TestClient, FileAuthStore, ForwardResolver]:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    client = TestClient(app, follow_redirects=False)
    return client, auth_store, resolver


def test_bare_origin_unauthenticated_returns_login_page(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, _store, _resolver = app_setup
    response = client.get("/")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_login_url_redirect_renders_js_redirect_page(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, store, _resolver = app_setup
    code = OneTimeCode("test-code-12345")
    store.add_one_time_code(code=code)
    response = client.get(f"/login?one_time_code={code}")
    assert response.status_code == 200
    # The page is the JS-redirect shim; it must reference /authenticate.
    assert "/authenticate" in response.text


def test_authenticate_consumes_otp_and_sets_cookie(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, store, _resolver = app_setup
    code = OneTimeCode("auth-test-code-1")
    store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}")
    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert MNGR_FORWARD_SESSION_COOKIE_NAME in response.cookies
    # Code is single-use: re-presenting it returns 403.
    response2 = client.get(f"/authenticate?one_time_code={code}")
    assert response2.status_code == 403


def test_invalid_otp_returns_403(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, _store, _resolver = app_setup
    response = client.get("/authenticate?one_time_code=never-issued")
    assert response.status_code == 403


def test_empty_otp_on_authenticate_returns_403_not_500(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Empty `?one_time_code=` must produce a clean 403, not a 500 from OneTimeCode validation."""
    client, _store, _resolver = app_setup
    response = client.get("/authenticate?one_time_code=")
    assert response.status_code == 403


def test_empty_otp_on_login_returns_403_not_500(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Empty `?one_time_code=` against /login must produce a clean 403, not a 500."""
    client, _store, _resolver = app_setup
    response = client.get("/login?one_time_code=")
    assert response.status_code == 403


def test_whitespace_otp_on_authenticate_returns_403_not_500(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Whitespace-only `?one_time_code=   ` must produce a clean 403, not a 500."""
    client, _store, _resolver = app_setup
    response = client.get("/authenticate?one_time_code=%20%20%20")
    assert response.status_code == 403


def test_bare_origin_authenticated_renders_debug_index(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    response = client.get("/", cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie})
    assert response.status_code == 200
    assert "Discovered agents" in response.text


def test_goto_unauthenticated_redirects_to_root(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, _store, _resolver = app_setup
    response = client.get("/goto/agent-deadbeef/")
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_goto_authenticated_redirects_to_subdomain_with_token(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    # Use a 32-hex AgentId so the AgentId() validator accepts it.
    valid_agent_id = "agent-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_agent_id}/",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"http://{valid_agent_id}.localhost:18421/_subdomain_auth?token=")
    assert "next=%2F" in location


def test_goto_rejects_protocol_relative_next(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """`/goto/<agent>/?next=//evil.com` must be sanitized to `/`, not propagated as-is."""
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_agent_id = "agent-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_agent_id}/?next=//evil.com/path",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    location = response.headers["location"]
    # The `next` query param must be the encoded form of "/" -- never a
    # protocol-relative URL the browser would interpret as cross-origin.
    assert "next=%2F&" in location or location.endswith("next=%2F")
    assert "evil.com" not in location


def test_subdomain_auth_bridge_rejects_protocol_relative_next(tmp_path: Path) -> None:
    """`/_subdomain_auth?next=//evil.com&token=<valid>` must Location: / not //evil.com.

    Uses ``TestClient`` as a context manager so the FastAPI lifespan runs and the
    subdomain-routing middleware can read ``app.state.http_client``.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    valid_agent_id = "agent-" + "0" * 31 + "a"
    token = create_subdomain_auth_token(signing_key=auth_store.get_signing_key(), agent_id=valid_agent_id)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/_subdomain_auth?token={token}&next=//evil.com/path",
            headers={"host": f"{valid_agent_id}.localhost:18421"},
        )
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_sanitize_next_url() -> None:
    """Direct unit coverage of the helper used by both bridge call sites."""
    assert _sanitize_next_url("/") == "/"
    assert _sanitize_next_url("/foo/bar") == "/foo/bar"
    assert _sanitize_next_url("//evil.com") == "/"
    assert _sanitize_next_url("//evil.com/path") == "/"
    assert _sanitize_next_url("/\\evil.com") == "/"
    assert _sanitize_next_url("http://evil.com") == "/"
    assert _sanitize_next_url("evil.com") == "/"
    assert _sanitize_next_url("") == "/"


def test_preauth_cookie_short_circuit(tmp_path: Path) -> None:
    """A pre-shared cookie value is accepted without a signature check."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value="opaque-pre-shared-token",
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get("/", cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: "opaque-pre-shared-token"})
    assert response.status_code == 200
    assert "Discovered agents" in response.text


def test_subdomain_forward_strips_session_cookie_before_proxying_to_backend(tmp_path: Path) -> None:
    """The plugin must NEVER forward its own session cookie to the agent's
    workspace_server.

    The cookie value is the plugin's auth credential -- a backend that sees
    it could replay it against ``localhost:<plugin_port>`` and reach every
    other agent's subdomain (cookie auth is not bound per-agent). The
    forwarder explicitly strips ``mngr_forward_session=...`` from the
    outbound ``Cookie`` header; this regression test locks that in.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "opaque-preauth-cookie-value"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    captured: list[httpx.Request] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        # Replace the lifespan-created http_client with one whose transport we
        # control. Local agents (``ssh_info is None``) use ``app.state.http_client``
        # directly -- no SSH tunnel client to override.
        app.state.http_client = mock_client
        response = client.get(
            "/api/whatever",
            headers={
                # Two cookies on the same Cookie header: the plugin session
                # (must be stripped) and an unrelated one (must pass through).
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}; downstream_pref=keep-me",
            },
        )

    assert response.status_code == 200
    assert len(captured) == 1, f"expected exactly one forwarded request, got {len(captured)}"
    forwarded_cookie = captured[0].headers.get("cookie", "")
    assert MNGR_FORWARD_SESSION_COOKIE_NAME not in forwarded_cookie, (
        f"plugin session cookie leaked to backend in Cookie header: {forwarded_cookie!r}"
    )
    assert "downstream_pref=keep-me" in forwarded_cookie, (
        f"unrelated cookie was unexpectedly stripped: {forwarded_cookie!r}"
    )


def test_subdomain_forward_strips_session_cookie_when_only_session_cookie_present(
    tmp_path: Path,
) -> None:
    """When the plugin's session cookie is the *only* cookie on the request,
    the outbound request must end up with no Cookie header at all (not an
    empty-string Cookie that some backends might still parse).
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "opaque-preauth-cookie-value"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    captured: list[httpx.Request] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/whatever",
            headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert "cookie" not in captured[0].headers, (
        f"Cookie header should be absent when only the session cookie was present, "
        f"got: {captured[0].headers.get('cookie')!r}"
    )


def test_is_loopback_url() -> None:
    """Direct unit coverage of the helper used by both forward handlers."""
    assert _is_loopback_url("http://localhost:8000")
    assert _is_loopback_url("http://localhost")
    assert _is_loopback_url("http://LOCALHOST:8000")
    assert _is_loopback_url("http://127.0.0.1:8000")
    assert _is_loopback_url("http://127.7.7.7:1234")
    assert _is_loopback_url("http://[::1]:8000")
    assert _is_loopback_url("http://0.0.0.0:8000")
    assert not _is_loopback_url("http://stub-backend:8000")
    assert not _is_loopback_url("http://10.0.0.5:8000")
    assert not _is_loopback_url("http://example.com")


@pytest.mark.parametrize(
    "loopback_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_subdomain_forward_refuses_loopback_fallback_without_tunnel(
    tmp_path: Path,
    loopback_url: str,
) -> None:
    """Without an SSH tunnel, a loopback registered URL must 502 -- not silently dial host loopback."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": loopback_url})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "opaque-preauth-cookie-value"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    captured: list[httpx.Request] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/whatever",
            headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
        )

    assert response.status_code == 502
    assert "refusing to dial host loopback" in response.text
    assert captured == [], "request must NOT be forwarded to anything when loopback fallback is refused"


def test_subdomain_forward_allows_loopback_fallback_when_opted_in(tmp_path: Path) -> None:
    """``allow_host_loopback=True`` (the legacy DEV-mode escape hatch) restores the old fallback path."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://127.0.0.1:8000"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "opaque-preauth-cookie-value"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
        allow_host_loopback=True,
    )

    captured: list[httpx.Request] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/whatever",
            headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
        )

    assert response.status_code == 200
    assert len(captured) == 1


def test_subdomain_forward_returns_retry_page_on_backend_connect_error(tmp_path: Path) -> None:
    """When the backend refuses the connection (workspace_server still booting), HTML callers
    must get the auto-refresh retry page rather than a hard 502."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    # Non-loopback URL so we don't trip the loopback-refusal path; the
    # retry-page behaviour is independent of that check.
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "opaque-preauth-cookie-value"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    async def _refuse(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("backend not yet listening")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse), follow_redirects=False)

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        html_response = client.get(
            "/",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/html,application/xhtml+xml",
            },
        )
        json_response = client.get(
            "/api/something",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    # HTML navigations get the auto-refresh retry page so the user lands on
    # something useful instead of a hard 502.
    assert html_response.status_code == 503
    assert "Retrying" in html_response.text
    assert 'http-equiv="refresh"' in html_response.text
    # Non-HTML callers get a plain 503 they can interpret programmatically.
    assert json_response.status_code == 503
