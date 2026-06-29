"""Bare-origin and subdomain-routing tests for the FastAPI app.

The middleware and forwarding handlers depend on real network I/O
(``httpx`` / paramiko); those paths are exercised via the acceptance
test, not here. This file covers the deterministic auth + routing
surfaces using ``starlette.testclient.TestClient``.
"""

import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
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


def test_subdomain_unauthenticated_html_redirects_to_goto_bridge(tmp_path: Path) -> None:
    """A stale subdomain cookie must redirect to /goto/<id>/ on the bare
    origin, not the bare landing page.

    Background: the host app (minds.app) regenerates its signing key on
    every restart, so any pre-existing per-subdomain session cookie
    fails verification after a quit/reopen. Previously the unauthenticated
    HTML response 302-redirected to ``localhost:<port>/``, dumping the
    user on the landing page even though their session was valid on the
    bare origin. The fix self-heals by sending the browser through the
    ``/goto/<agent_id>/`` bridge: the bare-origin session cookie still
    verifies, the bridge mints a fresh subdomain auth token, the
    subdomain handler then sets a fresh subdomain cookie, and the user
    lands in their workspace without an interactive re-auth.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    listen_port = 18421
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=listen_port,
    )
    with TestClient(app, base_url=f"http://{agent_id}.localhost:{listen_port}", follow_redirects=False) as client:
        response = client.get(
            "/",
            headers={
                "accept": "text/html",
                # Cookie value that fails signature verification (signed
                # by a different key) -- the post-restart scenario.
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}=stale-cookie-from-previous-launch",
            },
        )

    assert response.status_code == 302
    assert response.headers["Location"] == f"http://localhost:{listen_port}/goto/{agent_id}/"


def test_subdomain_unauthenticated_non_html_returns_403(tmp_path: Path) -> None:
    """Stale cookie on a non-HTML request still returns 403 (no goto redirect).

    The /goto/ self-heal applies only to navigational HTML loads; an
    XHR / API call carrying a stale cookie has no browser to follow
    the redirect and should get a clean 403 instead.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
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
    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        response = client.get(
            "/api/something",
            headers={
                "accept": "application/json",
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}=stale",
            },
        )

    assert response.status_code == 403


def test_subdomain_forward_strips_session_cookie_before_proxying_to_backend(tmp_path: Path) -> None:
    """The plugin must NEVER forward its own session cookie to the agent's
    system_interface.

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
def test_subdomain_forward_routes_loopback_without_tunnel_to_recovery(
    tmp_path: Path,
    loopback_url: str,
) -> None:
    """A loopback registered URL with no SSH tunnel must route to recovery, not raw 502.

    This is what a stopped container looks like once discovery drops its SSH
    info. The handler still refuses to dial host loopback (security: PR 1482),
    but rather than returning raw 502 text it emits a ``CONNECT_ERROR``
    backend-failure envelope and serves the styled loader -- the same
    treatment as an SSH-tunnel setup failure -- so a consumer can drive its
    own recovery UI instead of the user seeing a raw error.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": loopback_url})
    tunnel_manager = SSHTunnelManager()
    envelope_output = io.StringIO()
    envelope_writer = EnvelopeWriter(output=envelope_output)
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
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/html,application/xhtml+xml",
            },
        )

    # HTML callers get the styled auto-refreshing loader, not raw 502 text.
    assert response.status_code == 503
    assert "Loading workspace" in response.text
    assert captured == [], "request must NOT be forwarded to anything when loopback fallback is refused"
    # The failure envelope is what lets a consumer drive its recovery flow.
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


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
    """When the backend refuses the connection (system_interface still booting), HTML callers
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
    assert "Loading workspace" in html_response.text
    assert 'http-equiv="refresh"' in html_response.text
    # Non-HTML callers get a plain 503 they can interpret programmatically.
    assert json_response.status_code == 503


# -- system_interface_backend_failure envelope + recovery redirect tests --


def _make_forward_app_with_capture(
    tmp_path: Path,
    capture: list[httpx.Request],
    agent_id: AgentId,
    preauth: str,
    *,
    backend_status: int = 200,
    raise_error: type[Exception] | None = None,
) -> tuple[FastAPI, io.StringIO, httpx.AsyncClient]:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(agent_id)
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend"})
    tunnel_manager = SSHTunnelManager()
    envelope_output = io.StringIO()
    envelope_writer = EnvelopeWriter(output=envelope_output)
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    async def _capture(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        if raise_error is not None:
            raise raise_error("simulated failure")
        return httpx.Response(backend_status, content=b"hi")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)
    return app, envelope_output, mock_client


def _envelope_lines(envelope_output: io.StringIO) -> list[str]:
    return [line for line in envelope_output.getvalue().splitlines() if line.strip()]


def test_subdomain_forward_emits_system_interface_backend_failure_on_5xx(tmp_path: Path) -> None:
    """A 5xx backend response triggers an ``ERROR_RESPONSE`` ``system_interface_backend_failure`` envelope."""
    agent_id = AgentId()
    preauth = "preauth-cookie-1"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        backend_status=503,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 503
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "ERROR_RESPONSE"
    assert payload["status_code"] == 503


def test_subdomain_forward_does_not_emit_failure_on_2xx(tmp_path: Path) -> None:
    """A successful backend response must not produce a failure envelope."""
    agent_id = AgentId()
    preauth = "preauth-cookie-ok"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        backend_status=200,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert _envelope_lines(env_out) == []


def test_subdomain_forward_emits_error_response_on_404(tmp_path: Path) -> None:
    """Any non-2xx response (here a 404) emits a single ``ERROR_RESPONSE`` envelope.

    The plugin does not interpret which status codes matter; it forwards the
    response unchanged and surfaces the status code so the consumer can decide.
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-404"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        backend_status=404,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/agents/agent-deadbeef/screen",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 404
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "ERROR_RESPONSE"
    assert payload["status_code"] == 404


def test_subdomain_forward_emits_error_response_regardless_of_method(tmp_path: Path) -> None:
    """Emission is method-agnostic: a non-GET non-2xx response also emits ``ERROR_RESPONSE``.

    The plugin no longer special-cases the request method (it previously
    skipped non-GET 404s). Any non-2xx is surfaced with its status code and
    the consumer decides what to do with it.
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-404-post"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        backend_status=404,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.post(
            "/api/agents/agent-deadbeef/message",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 404
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "ERROR_RESPONSE"
    assert payload["status_code"] == 404


def test_subdomain_forward_emits_error_response_on_application_500(tmp_path: Path) -> None:
    """An application-layer 500 now emits ``ERROR_RESPONSE`` too.

    The plugin used to suppress non-infrastructure 5xx (e.g. a 500 stack
    trace). It now surfaces every non-2xx and leaves that policy to the
    consumer (a consumer may, for instance, choose to ignore app 500s).
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-500"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        backend_status=500,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 500
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["reason"] == "ERROR_RESPONSE"
    assert payload["status_code"] == 500


def test_subdomain_forward_emits_system_interface_backend_failure_on_sse_startup_disconnect(tmp_path: Path) -> None:
    """``RemoteProtocolError`` on an SSE-startup ``send()`` must emit ``CONNECT_ERROR``.

    Regression test: previously, an SSE-style request (``Accept: text/event-stream``)
    whose backend died between SSH-tunnel accept and channel-open would surface
    ``httpx.RemoteProtocolError`` from ``http_client.send(..., stream=True)``.
    That exception was not caught by the SSE branch (only ``ConnectError``
    and ``TimeoutException`` were), so it bubbled up through starlette as a
    500 and no failure envelope was emitted -- meaning a consumer had no
    signal to drive recovery.
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-sse-startup"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        raise_error=httpx.RemoteProtocolError,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/events",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/event-stream",
            },
        )

    assert response.status_code == 503
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


def test_subdomain_forward_returns_plain_503_for_non_html_on_connect_failure(tmp_path: Path) -> None:
    """Non-HTML callers (API clients) get the plain 503 with no location header."""
    agent_id = AgentId()
    preauth = "preauth-cookie-json"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        raise_error=httpx.ConnectError,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 503
    assert "location" not in {k.lower() for k in response.headers}


def test_subdomain_forward_emits_system_interface_backend_failure_on_sse_startup_timeout(tmp_path: Path) -> None:
    """``TimeoutException`` on an SSE-startup ``send()`` must emit ``CONNECT_ERROR``.

    Regression test: a wedged-but-listening backend produces a
    ``httpx.TimeoutException`` (not ``ConnectError``) when ``send(..., stream=True)``
    waits for response headers that never arrive. Without an envelope a
    consumer would have no signal that a hung-in-user-code backend is
    failing.
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-sse-timeout"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        raise_error=httpx.ConnectTimeout,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/events",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/event-stream",
            },
        )

    assert response.status_code == 504
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


def test_subdomain_forward_emits_system_interface_backend_failure_on_non_sse_timeout(tmp_path: Path) -> None:
    """``TimeoutException`` on a non-SSE backend request must emit ``CONNECT_ERROR``.

    Regression test: covers the non-streaming path counterpart to the
    SSE-startup timeout case. Both paths previously returned a 504 with
    no failure envelope, so the chrome health SSE never saw a tick toward
    STUCK for hung backends.
    """
    agent_id = AgentId()
    preauth = "preauth-cookie-json-timeout"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        agent_id,
        preauth,
        raise_error=httpx.ConnectTimeout,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 504
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


class _FailingTunnelManager(SSHTunnelManager):
    """SSHTunnelManager whose tunnel setup always fails, simulating a stopped container.

    A stopped agent container still has a resolver entry (stop is not destroy),
    so the forward handler resolves a target with ssh_info and then fails when
    opening the SSH tunnel -- exactly the path a stopped container exercises.
    """

    def get_tunnel_socket_path(self, ssh_info: RemoteSSHInfo, remote_host: str, remote_port: int) -> Path:
        raise SSHTunnelError(f"Unable to connect to port {remote_port} on {remote_host}")


def test_subdomain_forward_emits_failure_on_ssh_tunnel_setup_error(tmp_path: Path) -> None:
    """An SSH-tunnel setup failure (stopped container) must emit ``CONNECT_ERROR`` and serve the loader.

    Regression test: previously this path returned a raw 502 with no failure
    envelope, so a consumer had no signal to drive recovery -- the user just
    saw raw "SSH tunnel failed" text.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path.
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        agent_id,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-tunnel-fail"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=_FailingTunnelManager(),
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421", follow_redirects=False) as client:
        html_response = client.get(
            "/",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/html,application/xhtml+xml",
            },
        )

    # HTML callers get the styled auto-refreshing loader, not raw 502 text.
    assert html_response.status_code == 503
    assert "Loading workspace" in html_response.text
    # The failure envelope is what lets a consumer drive its recovery flow.
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


def test_subdomain_forward_websocket_emits_failure_on_ssh_tunnel_setup_error(tmp_path: Path) -> None:
    """A websocket whose SSH-tunnel setup fails must emit ``CONNECT_ERROR``.

    The websocket analogue of
    ``test_subdomain_forward_emits_failure_on_ssh_tunnel_setup_error``: a
    stopped container still has a resolver entry, so the handler resolves a
    target with ssh_info and then fails opening the tunnel, closing the socket
    before ``accept()``.

    Regression test: the websocket forward path used to close the socket
    without emitting a failure envelope, unlike the HTTP path. A mind whose
    only live channel is a websocket -- an already-loaded SPA after its system
    interface dies -- would then leave minds blind to the dead backend: the
    agent was never enrolled as a probe suspect, so it never reached STUCK and
    the chrome never redirected to the recovery page.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    agent_id = AgentId()
    resolver.add_known_agent(agent_id)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path,
    # where the failing tunnel manager raises during tunnel setup.
    resolver.update_services(agent_id, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        agent_id,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-ws-tunnel-fail"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=_FailingTunnelManager(),
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    with TestClient(app, base_url=f"http://{agent_id}.localhost:18421") as client:
        # ``websocket_connect`` ignores ``base_url`` and builds the URL against
        # ``ws://testserver``, so the agent subdomain must be in the URL itself
        # for the handler to route on the right host header. The handler closes
        # the socket before accepting, which the test client surfaces as a
        # WebSocketDisconnect on connect.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"ws://{agent_id}.localhost:18421/api/ws",
                headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
            ):
                pass

    # The failure envelope is what lets minds enroll the agent as a probe
    # suspect and drive its recovery flow.
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"
