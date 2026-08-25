"""Bare-origin and subdomain-routing tests for the FastAPI app.

The middleware and forwarding handlers depend on real network I/O
(``httpx`` / paramiko); those paths are exercised via the acceptance
test, not here. This file covers the deterministic auth + routing
surfaces using ``starlette.testclient.TestClient``.
"""

import asyncio
import io
import json
import socket as socket_module
import struct
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from collections.abc import Iterator
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient
from starlette.types import Message
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve as ws_serve

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.embedding import EmbedderOrigin
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.errors import MngrForwardError
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import OneTimeCode
from imbue.mngr_forward.primitives import SHARE_EMAIL_HEADER
from imbue.mngr_forward.primitives import SHARE_OWNER_HEADER
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.server import TunnelWarningRateLimiter
from imbue.mngr_forward.server import _PROXY_BACKSTOP_TIMEOUT_SECONDS
from imbue.mngr_forward.server import _PROXY_CONNECT_TIMEOUT_SECONDS
from imbue.mngr_forward.server import _PROXY_POOL_LIMITS
from imbue.mngr_forward.server import _PROXY_TIMEOUT
from imbue.mngr_forward.server import _SSE_READ_TIMEOUT_SECONDS
from imbue.mngr_forward.server import _STALL_NOTICE_SECONDS
from imbue.mngr_forward.server import _StallGuardedStreamingResponse
from imbue.mngr_forward.server import _TUNNEL_POOL_LIMITS
from imbue.mngr_forward.server import _forward_workspace_http
from imbue.mngr_forward.server import _get_tunnel_http_client
from imbue.mngr_forward.server import _is_loopback_url
from imbue.mngr_forward.server import _never_refused
from imbue.mngr_forward.server import _sanitize_next_url
from imbue.mngr_forward.server import _select_ws_receive_payload
from imbue.mngr_forward.server import create_forward_app
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import SSHTunnelPhase
from imbue.mngr_forward.ssh_tunnel import _create_short_path_tmpdir
from imbue.mngr_forward.ssh_tunnel import _create_tunnel_listener

# The workspace host every test agent runs on, and the shared test agent id:
# requests route by the ``agent-<hex>.localhost`` Host header (the canonical
# origin coordinate), and the resolver maps it back to the agent instance.
_TEST_HOST_ID = "host-" + "0123456789abcdef0123456789abcdef"
# A deliberately different hex payload from the host id, so the legacy-origin
# redirect assertions prove a real resolver lookup rather than a prefix swap.
_TEST_AGENT_ID = "agent-" + "feedfacefeedfacefeedfacefeedface"


def _make_test_instance_key() -> AgentInstanceKey:
    """The shared test agent's instance key (the resolver is instance-keyed)."""
    return AgentInstanceKey.build(AgentId(_TEST_AGENT_ID), HostId(_TEST_HOST_ID))


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


@pytest.fixture
def http2_app_setup(tmp_path: Path) -> tuple[TestClient, FileAuthStore, ForwardResolver]:
    """Same as ``app_setup`` but with ``use_http2=True`` so client-facing URLs

    become ``https``/``wss`` and the session cookie is marked ``Secure``.
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
        use_http2=True,
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


def test_authenticate_cookie_not_secure_without_http2(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """With the flag off the session cookie must NOT be Secure (plain http origin)."""
    client, store, _resolver = app_setup
    code = OneTimeCode("no-http2-cookie-1")
    store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}")
    assert response.status_code == 307
    set_cookie = response.headers["set-cookie"]
    assert MNGR_FORWARD_SESSION_COOKIE_NAME in set_cookie
    assert "secure" not in set_cookie.lower()


def test_http2_authenticate_sets_secure_cookie(
    http2_app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """With ``use_http2`` on, the session cookie set by /authenticate is Secure."""
    client, store, _resolver = http2_app_setup
    code = OneTimeCode("http2-cookie-1")
    store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}")
    assert response.status_code == 307
    set_cookie = response.headers["set-cookie"]
    assert MNGR_FORWARD_SESSION_COOKIE_NAME in set_cookie
    assert "secure" in set_cookie.lower()


def test_http2_goto_authenticated_redirects_to_https_subdomain(
    http2_app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """With ``use_http2`` on, the /goto bridge sends the browser to an https subdomain URL."""
    client, store, _resolver = http2_app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"https://{valid_host_id}.localhost:18421/_subdomain_auth?token=")


def test_http2_subdomain_unauthenticated_html_redirects_to_https_goto(tmp_path: Path) -> None:
    """With ``use_http2`` on, a stale-cookie subdomain HTML load redirects to the https /goto bridge."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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
        use_http2=True,
    )
    with TestClient(
        app, base_url=f"https://{_TEST_AGENT_ID}.localhost:{listen_port}", follow_redirects=False
    ) as client:
        response = client.get(
            "/",
            headers={
                "accept": "text/html",
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}=stale-cookie-from-previous-launch",
            },
        )
    assert response.status_code == 302
    assert response.headers["Location"] == f"https://localhost:{listen_port}/goto/{_TEST_AGENT_ID}/?next=%2F"


def test_http2_subdomain_auth_bridge_sets_secure_cookie(tmp_path: Path) -> None:
    """With ``use_http2`` on, the /_subdomain_auth bridge sets a Secure session cookie."""
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
        use_http2=True,
    )
    valid_host_id = "host-" + "0" * 31 + "a"
    token = create_subdomain_auth_token(signing_key=auth_store.get_signing_key(), origin_coordinate=valid_host_id)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/_subdomain_auth?token={token}&next=/",
            headers={"host": f"{valid_host_id}.localhost:18421"},
        )
    assert response.status_code == 302
    set_cookie = response.headers["set-cookie"]
    assert MNGR_FORWARD_SESSION_COOKIE_NAME in set_cookie
    assert "secure" in set_cookie.lower()


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


def test_bare_origin_html_navigation_redirects_to_shell_label(tmp_path: Path) -> None:
    """An authenticated HTML navigation to the bare workspace origin 302s to the
    shell service's own label origin (keeping local grammar identical to a share)."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
    resolver.update_service_labels(instance_key, {"system_interface-shell111": "system_interface"})
    tunnel_manager = SSHTunnelManager()
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get(
            "/some/page?x=1",
            headers={"accept": "text/html"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 302
    assert (
        response.headers["location"]
        == f"http://system_interface-shell111.{_TEST_AGENT_ID}.localhost:18421/some/page?x=1"
    )


def test_bare_origin_non_html_does_not_redirect(tmp_path: Path) -> None:
    """A non-HTML request to the bare origin (e.g. the readiness probe) is served
    by the shell directly rather than redirected, so probes are unaffected."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
    resolver.update_service_labels(instance_key, {"system_interface-shell111": "system_interface"})
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )

    async def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_ok), follow_redirects=False)
        response = client.get(
            "/api/health",
            headers={"accept": "application/json"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 200


def _make_legacy_workspace_app(tmp_path: Path) -> tuple[FastAPI, FileAuthStore]:
    """An app whose agent has label-less (pre-origin-label) service registrations.

    Mirrors a workspace baked before origin labels existed: the shell and the
    ``terminal`` / ``browser`` services are registered by name only, so the
    shell serves on the bare origin and services resolve via the
    label-as-name fallback.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(
        instance_key,
        {
            "system_interface": "http://stub-shell",
            "terminal": "http://stub-terminal",
            "browser": "http://stub-browser",
        },
    )
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    return app, auth_store


def test_legacy_service_path_navigation_redirects_to_service_origin(tmp_path: Path) -> None:
    """A navigation to ``/service/<name>/`` on the shell's (bare) origin 307s to the
    service's own origin, preserving the query -- so old system_interface builds'
    service iframes skip their service-worker bootstrap entirely."""
    app, auth_store = _make_legacy_workspace_app(tmp_path)
    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get(
            "/service/terminal/?arg=_&arg=session&arg=terminal-1",
            headers={"accept": "text/html", "sec-fetch-mode": "navigate"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == f"http://terminal.{_TEST_AGENT_ID}.localhost:18421/?arg=_&arg=session&arg=terminal-1"
    )


def test_legacy_service_path_navigation_preserves_subpath(tmp_path: Path) -> None:
    """The path after the ``/service/<name>`` prefix survives the redirect verbatim."""
    app, auth_store = _make_legacy_workspace_app(tmp_path)
    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get(
            "/service/browser/viewer/index.html?session=abc",
            headers={"accept": "text/html", "sec-fetch-mode": "navigate"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 307
    assert (
        response.headers["location"]
        == f"http://browser.{_TEST_AGENT_ID}.localhost:18421/viewer/index.html?session=abc"
    )


def test_legacy_service_path_unknown_service_passes_through_to_shell(tmp_path: Path) -> None:
    """A ``/service/<name>/`` navigation naming a service the agent has not
    registered is NOT redirected -- it forwards to the shell, whose own handler
    owns the unknown-service response."""
    app, auth_store = _make_legacy_workspace_app(tmp_path)

    def _shell_marker(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="shell served this")

    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_shell_marker), follow_redirects=False)
        response = client.get(
            "/service/never-registered/",
            headers={"accept": "text/html", "sec-fetch-mode": "navigate"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 200
    assert response.text == "shell served this"


def test_legacy_service_path_non_navigation_passes_through_to_shell(tmp_path: Path) -> None:
    """Only navigations are redirected: a subresource fetch under ``/service/...``
    (no ``sec-fetch-mode: navigate``) forwards to the shell unchanged, so a
    cross-origin redirect can never break a same-origin XHR."""
    app, auth_store = _make_legacy_workspace_app(tmp_path)

    def _shell_marker(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="shell served this")

    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_shell_marker), follow_redirects=False)
        response = client.get(
            "/service/terminal/__sw.js",
            headers={"accept": "*/*"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 200
    assert response.text == "shell served this"


def test_legacy_service_path_on_non_shell_origin_passes_through(tmp_path: Path) -> None:
    """A ``/service/...`` path on a NON-shell service origin is that service's own
    path space and must be proxied to it untouched."""
    app, auth_store = _make_legacy_workspace_app(tmp_path)

    def _terminal_marker(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="terminal served this")

    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(
        app, base_url=f"http://terminal.{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False
    ) as client:
        app.state.http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_terminal_marker), follow_redirects=False
        )
        response = client.get(
            "/service/terminal/whatever",
            headers={"accept": "text/html", "sec-fetch-mode": "navigate"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 200
    assert response.text == "terminal served this"


def test_forwarded_request_gets_owner_header_and_drops_forged_identity(tmp_path: Path) -> None:
    """The proxy stamps X-Share-Owner=true and never trusts a client-supplied identity.

    The local user is always the workspace owner, so a forged X-Share-Owner /
    X-Share-Email on the inbound request must be dropped and the authoritative
    owner flag injected before the backend sees it.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
    resolver.update_service_labels(instance_key, {"system_interface-shell111": "system_interface"})
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )

    seen_headers: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)
        response = client.get(
            "/api/health",
            headers={
                "accept": "application/json",
                SHARE_OWNER_HEADER: "false",
                SHARE_EMAIL_HEADER: "attacker@evil.example",
            },
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 200
    assert seen_headers[SHARE_OWNER_HEADER.lower()] == "true"
    assert SHARE_EMAIL_HEADER.lower() not in seen_headers


def test_goto_unauthenticated_redirects_to_root(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, _store, _resolver = app_setup
    response = client.get("/goto/host-deadbeef/")
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_goto_authenticated_redirects_to_subdomain_with_token(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    # Use a 32-hex id so the HostId() validator accepts it.
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"http://{valid_host_id}.localhost:18421/_subdomain_auth?token=")
    assert "next=%2F" in location


def test_goto_rejects_malformed_host_ids(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Ids outside the strict host-<32hex> shape 404 instead of reaching the Location header.

    ``HostId`` alone accepts newline-suffixed / underscore / 0x-prefixed hex
    (``int(hex, 16)`` semantics), which would inject the raw bytes into the
    redirect hostname.
    """
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    for bad_id in ("host-" + "a" * 31 + "%0A", "host-" + "a" * 30 + "_b", "host-0x" + "a" * 30, "host-deadbeef"):
        response = client.get(f"/goto/{bad_id}/", cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie})
        assert response.status_code == 404, bad_id


def test_goto_lowercases_uppercase_host_ids(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """An uppercase id redirects to the lowercased origin so the minted token verifies there."""
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    upper_host_id = "host-" + "0" * 31 + "A"
    response = client.get(f"/goto/{upper_host_id}/", cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie})
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"http://{upper_host_id.lower()}.localhost:18421/_subdomain_auth?token=")


def test_goto_rejects_protocol_relative_next(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """`/goto/<agent>/?next=//evil.com` must be sanitized to `/`, not propagated as-is."""
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/?next=//evil.com/path",
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
    valid_host_id = "host-" + "0" * 31 + "a"
    token = create_subdomain_auth_token(signing_key=auth_store.get_signing_key(), origin_coordinate=valid_host_id)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/_subdomain_auth?token={token}&next=//evil.com/path",
            headers={"host": f"{valid_host_id}.localhost:18421"},
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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
    with TestClient(
        app, base_url=f"http://{_TEST_AGENT_ID}.localhost:{listen_port}", follow_redirects=False
    ) as client:
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
    assert response.headers["Location"] == f"http://localhost:{listen_port}/goto/{_TEST_AGENT_ID}/?next=%2F"


def test_subdomain_unauthenticated_non_html_returns_403(tmp_path: Path) -> None:
    """Stale cookie on a non-HTML request still returns 403 (no goto redirect).

    The /goto/ self-heal applies only to navigational HTML loads; an
    XHR / API call carrying a stale cookie has no browser to follow
    the redirect and should get a clean 403 instead.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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
    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": loopback_url})
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

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://127.0.0.1:8000"})
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

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    # Non-loopback URL so we don't trip the loopback-refusal path; the
    # retry-page behaviour is independent of that check.
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    # The loader re-attempts the workspace by polling in the background and
    # reloading once it answers (rather than a focus-stealing meta refresh).
    assert "fetch(" in html_response.text
    assert "location.reload" in html_response.text
    # Non-HTML callers get a plain 503 they can interpret programmatically.
    assert json_response.status_code == 503


# -- system_interface_backend_failure envelope + recovery redirect tests --


def _make_forward_app_with_capture(
    tmp_path: Path,
    capture: list[httpx.Request],
    instance_key: AgentInstanceKey,
    preauth: str,
    *,
    backend_status: int = 200,
    raise_error: type[Exception] | None = None,
    backend_delay_seconds: float = 0.0,
    stall_notice_seconds: float = _STALL_NOTICE_SECONDS,
) -> tuple[FastAPI, io.StringIO, httpx.AsyncClient]:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
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
        stall_notice_seconds=stall_notice_seconds,
    )

    async def _capture(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        if backend_delay_seconds > 0:
            await asyncio.sleep(backend_delay_seconds)
        if raise_error is not None:
            raise raise_error("simulated failure")
        return httpx.Response(backend_status, content=b"hi")

    # Given production's timeout, so a captured request carries that rather
    # than httpx's default. ``MockTransport`` never enforces one, so this
    # changes nothing for the tests that only look at status codes and
    # envelopes. The pool ceiling ``_managed_lifespan`` also sets is not
    # mirrored: httpx ignores ``limits`` when handed an explicit transport.
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_capture), follow_redirects=False, timeout=_PROXY_TIMEOUT
    )
    return app, envelope_output, mock_client


def _envelope_lines(envelope_output: io.StringIO) -> list[str]:
    return [line for line in envelope_output.getvalue().splitlines() if line.strip()]


def test_subdomain_forward_emits_system_interface_backend_failure_on_5xx(tmp_path: Path) -> None:
    """A 5xx backend response triggers an ``ERROR_RESPONSE`` ``system_interface_backend_failure`` envelope."""
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-1"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_status=503,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "ERROR_RESPONSE"
    assert payload["status_code"] == 503


def test_subdomain_forward_does_not_emit_failure_on_2xx(tmp_path: Path) -> None:
    """A successful backend response must not produce a failure envelope."""
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-ok"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_status=200,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-404"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_status=404,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-404-post"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_status=404,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-500"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_status=500,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-sse-startup"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        raise_error=httpx.RemoteProtocolError,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


def test_subdomain_forward_returns_plain_503_for_non_html_on_connect_failure(tmp_path: Path) -> None:
    """Non-HTML callers (API clients) get the plain 503 with no location header."""
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-json"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        raise_error=httpx.ConnectError,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-sse-timeout"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        raise_error=httpx.ConnectTimeout,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    assert envelope["agent_id"] == str(instance_key.agent_id)
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
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-json-timeout"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        raise_error=httpx.ConnectTimeout,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"


def test_sse_backend_requests_keep_a_tighter_read_budget_than_buffered_ones(tmp_path: Path) -> None:
    """SSE pins its own read budget instead of riding along on the buffered backstop.

    The buffered path's backstop is long enough that a silent backend is not
    evidence of anything, which is the point -- a user app endpoint may
    legitimately take that long. An SSE producer may not: it is expected to
    heartbeat, so a silent stream is the fastest signal a wedged backend gives.
    Only the per-request override on the SSE ``build_request`` keeps the two
    apart, and losing it fails nothing else: the other SSE timeout test raises
    its ``ReadTimeout`` from the transport, so it passes under any budget.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-read-budget"
    captured: list[httpx.Request] = []
    app, _env_out, mock_client = _make_forward_app_with_capture(tmp_path, captured, instance_key, preauth)

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        cookie = f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"
        sse_response = client.get("/api/events", headers={"cookie": cookie, "accept": "text/event-stream"})
        buffered_response = client.get("/api/state", headers={"cookie": cookie, "accept": "application/json"})

    assert sse_response.status_code == 200
    assert buffered_response.status_code == 200
    sse_request, buffered_request = captured
    assert sse_request.extensions["timeout"]["read"] == _SSE_READ_TIMEOUT_SECONDS
    assert buffered_request.extensions["timeout"]["read"] == _PROXY_BACKSTOP_TIMEOUT_SECONDS


def test_subdomain_forward_reports_a_stalled_backend_without_abandoning_the_request(tmp_path: Path) -> None:
    """A backend slower than the stall window emits ``STALLED`` but still delivers its response.

    The stall notice exists so a consumer can start probing a possibly-wedged
    workspace. It must not double as a request deadline: an endpoint that
    legitimately takes longer than the window has to survive, and a window that
    cancelled at its own expiry would kill it.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-stall"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_delay_seconds=0.5,
        stall_notice_seconds=0.05,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/slow",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.content == b"hi"
    lines = _envelope_lines(env_out)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "STALLED"
    assert payload["status_code"] is None


def test_subdomain_forward_emits_no_stall_envelope_when_the_backend_answers_in_time(tmp_path: Path) -> None:
    """A backend that answers inside the stall window must not enroll the agent for probing.

    Guards the cancel half of the timer: leaving it armed would emit a
    ``STALLED`` envelope for every healthy request and keep every workspace
    permanently enrolled as a probe suspect.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-no-stall"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        backend_delay_seconds=0.0,
        stall_notice_seconds=0.05,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/api/quick",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )
        assert response.status_code == 200
        # Polled from inside the block, where the client's event loop is still
        # running, and for longer than the window: leaving the block first tears
        # that loop down, so an uncancelled timer would die with it instead of
        # firing and the assertion would hold no matter what.
        assert not poll_until(lambda: _envelope_lines(env_out) != [], timeout=0.5, poll_interval=0.05), (
            "the stall timer outlived the request it was armed for"
        )


async def _drive_request_until_client_disconnects(
    app: FastAPI,
    path: str,
    preauth: str,
    disconnect_after_seconds: float,
    accept_header: bytes = b"application/json",
) -> tuple[float, list[str], int | None]:
    """Run one request through ``app``'s ASGI interface, disconnecting mid-flight.

    ``TestClient`` cannot express this: its receive channel only yields
    ``http.disconnect`` once the response is complete, which is exactly the
    ordering under test. Returns how long the app took, the message types it
    sent back, and the status it started the response with (``None`` if it
    never started one).
    """
    sent_types: list[str] = []
    sent_status: int | None = None
    is_body_delivered = False

    async def _receive() -> Message:
        nonlocal is_body_delivered
        if not is_body_delivered:
            is_body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(disconnect_after_seconds)
        return {"type": "http.disconnect"}

    async def _send(message: Message) -> None:
        nonlocal sent_status
        sent_types.append(str(message["type"]))
        if message["type"] == "http.response.start":
            sent_status = int(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", f"{_TEST_AGENT_ID}.localhost:18421".encode("utf-8")),
            (b"cookie", f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}".encode("utf-8")),
            (b"accept", accept_header),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 18421),
    }
    started_at = time.monotonic()
    await app(scope, _receive, _send)
    return time.monotonic() - started_at, sent_types, sent_status


def test_subdomain_forward_abandons_the_backend_when_the_client_gives_up(tmp_path: Path) -> None:
    """A client that disconnects releases the backend request instead of pinning it.

    minds' health probe allows a workspace 2 seconds and then hangs up, but the
    plugin-side handler outlives that timeout -- a buffered handler never reads
    the receive channel, so the disconnect goes unnoticed. Left running to the
    backstop, those abandoned probes accumulate at roughly one every four
    seconds against the proxy's connection pool, and for a remote agent each one
    also pins an SSH channel and its relay thread.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-disconnect"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        # Far longer than the disconnect, so finishing early can only mean the
        # request was abandoned rather than awaited.
        backend_delay_seconds=5.0,
        stall_notice_seconds=_STALL_NOTICE_SECONDS,
    )
    app.state.http_client = mock_client
    app.state.ssh_http_clients = {}
    app.state.ssh_http_clients_lock = threading.Lock()

    elapsed_seconds, sent_types, sent_status = asyncio.run(
        _drive_request_until_client_disconnects(app, "/api/state", preauth, disconnect_after_seconds=0.05)
    )

    assert elapsed_seconds < 2.0, "the handler waited for the backend instead of abandoning the request"
    assert sent_types == ["http.response.start", "http.response.body"]
    # 499, not the 504 a genuinely-timed-out backend gets: nothing answered
    # wrongly here, the client stopped listening. The status is only there to
    # end the ASGI exchange, but it must not claim success or blame the backend.
    assert sent_status == 499
    assert len(captured) == 1, "the backend request should have been started before being abandoned"
    # A client hanging up says nothing about the backend's health.
    assert _envelope_lines(env_out) == []


def test_subdomain_forward_abandons_the_backend_stream_when_the_client_gives_up(tmp_path: Path) -> None:
    """An SSE client that disconnects during the backend handoff releases the pooled slot.

    Once the streaming response exists, starlette guards the body: hypercorn
    advertises ASGI spec 2.1, so ``StreamingResponse.__call__`` races
    ``listen_for_disconnect`` against the body loop. Nothing guarded the window
    *before* that -- opening the backend stream -- so a client that left during
    the handoff held its pooled connection until the SSE read budget expired.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-sse-disconnect"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        # Far longer than the disconnect, so finishing early can only mean the
        # handoff was abandoned rather than awaited.
        backend_delay_seconds=5.0,
    )
    app.state.http_client = mock_client
    app.state.ssh_http_clients = {}
    app.state.ssh_http_clients_lock = threading.Lock()

    elapsed_seconds, sent_types, sent_status = asyncio.run(
        _drive_request_until_client_disconnects(
            app, "/api/events", preauth, disconnect_after_seconds=0.05, accept_header=b"text/event-stream"
        )
    )

    assert elapsed_seconds < 2.0, "the handler waited for the backend stream instead of abandoning the handoff"
    assert sent_types == ["http.response.start", "http.response.body"]
    assert sent_status == 499
    assert len(captured) == 1, "the backend request should have been started before being abandoned"
    # A client hanging up says nothing about the backend's health.
    assert _envelope_lines(env_out) == []


class _FailingTunnelManager(SSHTunnelManager):
    """SSHTunnelManager whose tunnel setup always fails, simulating a stopped container.

    A stopped agent container still has a resolver entry (stop is not destroy),
    so the forward handler resolves a target with ssh_info and then fails when
    opening the SSH tunnel -- exactly the path a stopped container exercises.
    ``phase`` picks which side of the tunnel the failure is attributed to; the
    forward reads it to decide whether the agent's host is implicated at all.
    """

    phase: SSHTunnelPhase = Field(
        default=SSHTunnelPhase.HOST_CONNECT, description="Phase to tag the simulated failure with"
    )

    def get_tunnel_socket_path(self, ssh_info: RemoteSSHInfo, remote_host: str, remote_port: int) -> Path:
        raise SSHTunnelError(f"Unable to connect to port {remote_port} on {remote_host}", self.phase)


@pytest.mark.parametrize(
    "phase, expected_reason",
    [
        # The tunnel was dialed and the host did not answer -- a stopped
        # container, a host that went away. Evidence about the workspace.
        (SSHTunnelPhase.HOST_CONNECT, "CONNECT_ERROR"),
        # This device could not build its own end (no known_hosts to pin
        # against, a socket that would not bind). Every one is raised against
        # this device's own filesystem or socket table, so nothing here is
        # evidence about the workspace at all.
        (SSHTunnelPhase.LOCAL_SETUP, "TUNNEL_SETUP_FAILED"),
    ],
)
def test_subdomain_forward_emits_failure_on_ssh_tunnel_setup_error(
    tmp_path: Path, phase: SSHTunnelPhase, expected_reason: str
) -> None:
    """An SSH-tunnel setup failure must emit a failure envelope naming which side failed, and serve the loader.

    Regression test: previously this path returned a raw 502 with no failure
    envelope, so a consumer had no signal to drive recovery -- the user just
    saw raw "SSH tunnel failed" text. The reason is split by phase because both
    failures raise the same exception type here, and reading a local trust-material
    failure as CONNECT_ERROR is what makes minds blame (and "restart") a
    workspace that was answering all along.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path.
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        instance_key,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-tunnel-fail"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=_FailingTunnelManager(phase=phase),
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
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
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == expected_reason
    # The verbatim text is what the recovery card shows the user; a category
    # name alone leaves a broken install undiagnosable from the app.
    assert "Unable to connect to port 8000 on stub-backend" in payload["detail"]


@pytest.mark.parametrize(
    "phase, expected_reason",
    [
        (SSHTunnelPhase.HOST_CONNECT, "CONNECT_ERROR"),
        (SSHTunnelPhase.LOCAL_SETUP, "TUNNEL_SETUP_FAILED"),
    ],
)
def test_subdomain_forward_websocket_emits_failure_on_ssh_tunnel_setup_error(
    tmp_path: Path, phase: SSHTunnelPhase, expected_reason: str
) -> None:
    """A websocket whose SSH-tunnel setup fails must emit the same phase-split reason as the HTTP path.

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
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path,
    # where the failing tunnel manager raises during tunnel setup.
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        instance_key,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-ws-tunnel-fail"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=_FailingTunnelManager(phase=phase),
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421") as client:
        # ``websocket_connect`` ignores ``base_url`` and builds the URL against
        # ``ws://testserver``, so the agent subdomain must be in the URL itself
        # for the handler to route on the right host header. The handler closes
        # the socket before accepting, which the test client surfaces as a
        # WebSocketDisconnect on connect.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"ws://{_TEST_AGENT_ID}.localhost:18421/api/ws",
                headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
            ):
                pass

    # The failure envelope is what lets minds enroll the agent as a probe
    # suspect and drive its recovery flow.
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == expected_reason


class _AcceptThenCloseTunnelManager(SSHTunnelManager):
    """Tunnel manager whose socket accepts a connection and immediately closes it.

    This is what a forward tunnel does to an in-flight connection either way an
    open can fail: ``_open_and_relay`` closes the accepted socket without ever
    speaking HTTP. The ``websockets`` client reports that as ``InvalidMessage``,
    not as an ``OSError``.

    Which failure it was is invisible in the socket, which is the whole reason
    the tunnel layer counts refusals separately. When ``is_refusal_recorded``
    is on, the accept loop records a refusal against the tunnel key the forward
    asked for -- exactly as ``_open_and_relay`` does when sshd answers and
    refuses the inner port -- before closing, so the two cases stay
    byte-identical on the wire and differ only in that count.
    """

    is_refusal_recorded: bool = Field(
        default=False, description="Whether each accepted connection counts as a refused channel open"
    )

    _socket_tmpdir: tempfile.TemporaryDirectory[str] = PrivateAttr()
    _socket_path: Path = PrivateAttr()
    _server: socket_module.socket = PrivateAttr()
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread = PrivateAttr()
    _requested_tunnel_key: str | None = PrivateAttr(default=None)

    def model_post_init(self, __context: object) -> None:
        # Not pytest's tmp_path: on macOS that lives under /var/folders/... and
        # overflows AF_UNIX's sun_path on its own, so the socket is placed by
        # the same rule the manager under test uses for its own.
        self._socket_tmpdir = _create_short_path_tmpdir("mngr-fwd-ws-test-")
        self._socket_path = Path(self._socket_tmpdir.name) / "accept-then-close.sock"
        self._server = _create_tunnel_listener(self._socket_path)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            requested_tunnel_key = self._requested_tunnel_key
            if self.is_refusal_recorded and requested_tunnel_key is not None:
                self._record_backend_refusal(requested_tunnel_key)
            conn.close()

    def get_tunnel_socket_path(self, ssh_info: RemoteSSHInfo, remote_host: str, remote_port: int) -> Path:
        self._requested_tunnel_key = f"{ssh_info.host}:{ssh_info.port}->{remote_host}:{remote_port}"
        return self._socket_path

    def cleanup(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._server.close()
        self._socket_tmpdir.cleanup()
        super().cleanup()


@pytest.mark.parametrize(
    "is_refusal_recorded, expected_reason",
    [
        # The tunnel retired itself under the handshake (its SSH transport
        # stopped answering): nothing was learned about the inner port.
        (False, "CONNECT_ERROR"),
        # sshd answered and refused the channel to the inner port: the host is
        # reachable and its server is not listening.
        (True, "BACKEND_NOT_LISTENING"),
    ],
)
def test_websocket_emits_failure_when_backend_closes_during_handshake(
    tmp_path: Path, is_refusal_recorded: bool, expected_reason: str
) -> None:
    """A backend that closes mid-handshake must emit a failure envelope, not escape as an ASGI error.

    Regression test for the same class of bug as
    ``test_websocket_forward_emits_failure_on_ssh_tunnel_setup_error``, reached
    by a different exception. There the tunnel fails to open; here it opens and
    then dies under the handshake, which is what happens when the tunnel retires
    itself after its SSH transport stops answering (a laptop resumed from sleep).

    ``websockets`` reports that as ``InvalidMessage`` (with an ``EOFError``
    as its ``__cause__``), which descends from ``WebSocketException`` rather
    than ``OSError``, so it used to slip past the handler's ``except`` and
    escape into the ASGI framework. The cost was the same one that test
    describes -- no envelope, so minds never enrolled the agent as a probe
    suspect -- plus the client never received the 1011 close that tells it to
    reconnect promptly.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path.
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        instance_key,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-ws-handshake-eof"
    tunnel_manager = _AcceptThenCloseTunnelManager(is_refusal_recorded=is_refusal_recorded)
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18422,
        preauth_cookie_value=preauth,
    )

    try:
        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18422") as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    f"ws://{_TEST_AGENT_ID}.localhost:18422/api/ws",
                    headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
                ):
                    pass
    finally:
        tunnel_manager.cleanup()

    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["agent_id"] == str(instance_key.agent_id)
    payload = envelope["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == expected_reason


@pytest.mark.parametrize(
    "is_refusal_recorded, expected_reason",
    [
        (False, "CONNECT_ERROR"),
        (True, "BACKEND_NOT_LISTENING"),
    ],
)
def test_http_forward_reports_a_refused_channel_as_the_backend_not_listening(
    tmp_path: Path, is_refusal_recorded: bool, expected_reason: str
) -> None:
    """A refused ``direct-tcpip`` open must be tellable from a host that could not be reached.

    Both reach the proxy as the tunnel socket closing under an in-flight
    request, so the exception is identical (``RemoteProtocolError``); only the
    refusal the tunnel layer recorded separates them. The distinction is the
    passive replacement for the deleted in-container LISTEN scan: it says the
    container is up and its server is not, at every retry rather than once.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    # Non-loopback URL + ssh_info so the handler takes the SSH-tunnel path.
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend:8000"})
    resolver.update_ssh_info(
        instance_key,
        RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
    )
    envelope_output = io.StringIO()
    preauth = "preauth-cookie-http-refused-channel"
    tunnel_manager = _AcceptThenCloseTunnelManager(is_refusal_recorded=is_refusal_recorded)
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18423,
        preauth_cookie_value=preauth,
    )

    try:
        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18423", follow_redirects=False) as client:
            response = client.get(
                "/api/state",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    "accept": "application/json",
                },
            )
    finally:
        tunnel_manager.cleanup()

    assert response.status_code == 503
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == expected_reason


# How long the failing backend below waits for the proxy to connect, and the
# ceiling on how long it holds a stalled body open. Generous for the accept,
# which only ever elapses when the request never arrives -- i.e. the test is
# already failing -- and the serving thread is a daemon that must not outlive
# the test either way.
_STUB_BACKEND_ACCEPT_TIMEOUT_SECONDS: Final[float] = 10.0

# A ``content-length`` deliberately larger than the bytes actually sent, so the
# body is unmistakably incomplete when the connection dies or stalls under it.
_TRUNCATED_BODY_HEAD = b"HTTP/1.1 200 OK\r\ncontent-length: 64\r\n\r\npartial"


class _BackendFailureMode(UpperCaseStrEnum):
    """How the stub backend below fails the one connection it serves.

    The two resets are what make their tests say the same thing on every
    platform. A peer that merely closes is a clean EOF on macOS and, when it
    still holds the unread request, an RST on Linux -- so an ordinary close
    reaches httpx as ``RemoteProtocolError`` on one and ``ReadError`` on the
    other. Draining the request first and closing under ``SO_LINGER`` with a
    zero timeout produces the RST (and so the ``ReadError``) on both.

    ``STALL_AFTER_HEADERS`` answers and then simply stops, which httpx reports
    as a ``ReadTimeout`` from under the body -- the same exception a backend
    that never sent headers raises, so only the phase separates the two.
    """

    RESET_BEFORE_HEADERS = auto()
    RESET_AFTER_HEADERS = auto()
    STALL_AFTER_HEADERS = auto()


def _arm_reset_on_close(conn: socket_module.socket) -> None:
    """Make this socket's close send an RST rather than a FIN."""
    conn.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_LINGER, struct.pack("ii", 1, 0))


@contextmanager
def _failing_backend(mode: _BackendFailureMode) -> Iterator[int]:
    """Serve one loopback connection that fails in ``mode``, yielding the port to point a backend URL at."""
    server = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    server.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(_STUB_BACKEND_ACCEPT_TIMEOUT_SECONDS)
    port = int(server.getsockname()[1])
    is_released = threading.Event()

    def _serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            # Drain the request so a reset below is this backend's own choice
            # rather than a side effect of closing on unread data.
            conn.recv(65536)
            match mode:
                case _BackendFailureMode.RESET_BEFORE_HEADERS:
                    _arm_reset_on_close(conn)
                case _BackendFailureMode.RESET_AFTER_HEADERS:
                    conn.sendall(_TRUNCATED_BODY_HEAD)
                    _arm_reset_on_close(conn)
                case _BackendFailureMode.STALL_AFTER_HEADERS:
                    conn.sendall(_TRUNCATED_BODY_HEAD)
                    # Hold the rest of the body back so the proxy's own read
                    # budget is what ends the request. Released on teardown.
                    is_released.wait(_STUB_BACKEND_ACCEPT_TIMEOUT_SECONDS)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        is_released.set()
        # Joined past the accept timeout before the listener is closed: a test
        # body that failed before issuing its request leaves the thread blocked
        # in ``accept``, and closing the socket out from under it is the wrong
        # way round even when the thread is a daemon that would survive it.
        thread.join(timeout=_STUB_BACKEND_ACCEPT_TIMEOUT_SECONDS + 1.0)
        server.close()


def _make_loopback_backend_app(tmp_path: Path, port: int, preauth: str) -> tuple[FastAPI, io.StringIO]:
    """Build a forward app whose backend is a loopback port, so no tunnel is involved."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": f"http://127.0.0.1:{port}"})
    envelope_output = io.StringIO()
    app = create_forward_app(
        auth_store=FileAuthStore(data_directory=tmp_path),
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=envelope_output),
        listen_host="127.0.0.1",
        listen_port=18424,
        preauth_cookie_value=preauth,
        # The backend is a real socket on this machine, which is exactly what
        # the proxy otherwise refuses to dial.
        allow_host_loopback=True,
    )
    return app, envelope_output


def test_http_forward_reports_a_reset_before_the_headers_as_a_connect_failure(tmp_path: Path) -> None:
    """A backend that resets before answering never answered, whichever exception carries it.

    The reset reaches httpx as ``ReadError``, which reads like a mid-response
    failure and is not one: nothing was delivered, so this is the same
    unreachable-backend signal the recovery UI acts on. This is the case a
    refused ``direct-tcpip`` open produces on Linux, where the tunnel closes the
    accepted socket with the request still unread.
    """
    preauth = "preauth-cookie-reset-before-headers"
    with _failing_backend(_BackendFailureMode.RESET_BEFORE_HEADERS) as port:
        app, envelope_output = _make_loopback_backend_app(tmp_path, port, preauth)
        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18424", follow_redirects=False) as client:
            response = client.get(
                "/api/state",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    "accept": "application/json",
                },
            )

    assert response.status_code == 503
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "CONNECT_ERROR"
    # httpx leaves a ``ReadError`` with no message at all, so the class name is
    # what reaches the consumer instead of an empty string.
    assert payload["detail"] == "ReadError"


def test_http_forward_reports_a_reset_after_the_headers_as_a_lost_body(tmp_path: Path) -> None:
    """A backend that answers and then dies mid-body is still a mid-response failure.

    The other side of the split: the headers arrived, so the backend was
    reachable and answering, and calling this unreachable would send a consumer
    into recovery for a workspace that is up.
    """
    preauth = "preauth-cookie-reset-after-headers"
    with _failing_backend(_BackendFailureMode.RESET_AFTER_HEADERS) as port:
        app, envelope_output = _make_loopback_backend_app(tmp_path, port, preauth)
        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18424", follow_redirects=False) as client:
            response = client.get(
                "/api/state",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    "accept": "application/json",
                },
            )

    assert response.status_code == 502
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "SSE_EOF"


# Read budget for the stalling-body test, standing in for production's 600s
# backstop. What that test pins is which phase the timeout lands in, not how
# long it takes to fire, and it must stay well under the 30s STALLED advisory
# so the stall does not add a second envelope.
_STALLED_BODY_READ_TIMEOUT_SECONDS: Final[float] = 2.0


def test_http_forward_reports_a_body_that_stalls_after_its_headers_as_a_lost_body(tmp_path: Path) -> None:
    """A read timeout under the body is a mid-response failure, not an unreachable backend.

    The timeout counterpart of the reset split above. httpx raises the same
    ``ReadTimeout`` either side of the headers, so without the phase to separate
    them a body that stalls once the backend has answered is reported as a
    backend that never answered -- evidence against a workspace that
    demonstrably did. The SSE path already reads a mid-stream
    ``TimeoutException`` as ``SSE_EOF``.
    """
    preauth = "preauth-cookie-stalled-body"
    with _failing_backend(_BackendFailureMode.STALL_AFTER_HEADERS) as port:
        app, envelope_output = _make_loopback_backend_app(tmp_path, port, preauth)
        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18424", follow_redirects=False) as client:
            app.state.http_client.timeout = httpx.Timeout(
                connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
                pool=_PROXY_CONNECT_TIMEOUT_SECONDS,
                read=_STALLED_BODY_READ_TIMEOUT_SECONDS,
                write=_PROXY_BACKSTOP_TIMEOUT_SECONDS,
            )
            response = client.get(
                "/api/state",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    "accept": "application/json",
                },
            )

    assert response.status_code == 502
    lines = _envelope_lines(envelope_output)
    assert len(lines) == 1
    payload = json.loads(lines[0])["payload"]
    assert payload["type"] == "system_interface_backend_failure"
    assert payload["reason"] == "SSE_EOF"
    # A ``ReadTimeout`` is message-less too, so the same fallback applies.
    assert payload["detail"] == "ReadTimeout"


def test_service_origin_routes_to_named_service_backend(tmp_path: Path) -> None:
    """A ``<service>.host-<hex>.localhost`` origin forwards to that service's registered URL."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(
        instance_key,
        {"system_interface": "http://stub-shell", "terminal": "http://stub-terminal"},
    )
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "preauth-service-origin"
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

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"ok")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(
        app, base_url=f"http://terminal.{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False
    ) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/ws-info",
            headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert str(captured[0].url).startswith("http://stub-terminal/")


def test_deep_service_origin_routes_to_owning_service(tmp_path: Path) -> None:
    """Deeper labels (``sub.svc.host-<hex>.localhost``) route to the same service."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"svc": "http://stub-svc"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "preauth-deep-origin"
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

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"ok")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), follow_redirects=False)

    with TestClient(
        app, base_url=f"http://deep.svc.{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False
    ) as client:
        app.state.http_client = mock_client
        response = client.get(
            "/asset.js",
            headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
        )

    assert response.status_code == 200
    assert len(captured) == 1
    assert str(captured[0].url).startswith("http://stub-svc/")


def test_service_origin_unregistered_service_serves_loading_page(tmp_path: Path) -> None:
    """An unknown-but-plausible service label serves the auto-retrying loader, not a 404."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-shell"})
    tunnel_manager = SSHTunnelManager()
    envelope_writer = EnvelopeWriter(output=io.StringIO())
    preauth = "preauth-unregistered"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )

    with TestClient(app, base_url=f"http://notyet.{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get(
            "/",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/html",
            },
        )

    assert response.status_code == 503
    assert "Loading workspace" in response.text


def test_goto_with_service_param_redirects_to_service_origin(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """The /goto/ bridge carries the service label chain so the bounce lands on the exact origin."""
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/?service=deep.svc&next=/panel",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"http://deep.svc.{valid_host_id}.localhost:18421/_subdomain_auth?token=")
    assert "next=%2Fpanel" in location


def test_goto_accepts_looser_sub_origin_labels_before_the_service(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Deeper labels are the service's own sub-origin space, routed with the hostname-label charset.

    Only the last label is a service name; a sub-origin like ``a--b`` (valid
    per FORWARD_SUBDOMAIN_PATTERN, invalid as a ServiceLabel) must bounce back
    to its own origin instead of 404ing mid-login.
    """
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/?service=a--b.svc",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        f"http://a--b.svc.{valid_host_id}.localhost:18421/_subdomain_auth?token="
    )


def test_goto_still_rejects_a_malformed_service_name_even_with_valid_sub_origins(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    # The LAST label is the service name and keeps the strict ServiceLabel rule.
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    for bad_chain in ("sub.a--b", "sub..svc", "UP.svc"):
        response = client.get(
            f"/goto/{valid_host_id}/?service={bad_chain}",
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
        assert response.status_code == 404, bad_chain


def test_goto_rejects_invalid_service_labels(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """Crafted ``service`` values that are not valid labels must 404, not redirect."""
    client, store, _resolver = app_setup
    cookie = create_session_cookie(store.get_signing_key())
    valid_host_id = "host-" + "0" * 31 + "a"
    response = client.get(
        f"/goto/{valid_host_id}/?service=EVIL",
        cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
    )
    assert response.status_code == 404


def test_subdomain_auth_bridge_sets_workspace_domain_cookie(tmp_path: Path) -> None:
    """The bridge cookie is domain-scoped so one hop covers the shell and every service origin."""
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
    valid_host_id = "host-" + "0" * 31 + "a"
    token = create_subdomain_auth_token(signing_key=auth_store.get_signing_key(), origin_coordinate=valid_host_id)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/_subdomain_auth?token={token}&next=/",
            headers={"host": f"svc.{valid_host_id}.localhost:18421"},
        )
    assert response.status_code == 302
    set_cookie = response.headers["set-cookie"].lower()
    assert f"domain={valid_host_id}.localhost" in set_cookie


def test_select_ws_receive_payload_selects_by_value_not_key() -> None:
    """A binary frame must yield its bytes, not the co-present ``text: None``.

    hypercorn emits every ``websocket.receive`` event with BOTH ``text`` and
    ``bytes`` keys, setting the unused one to ``None`` (uvicorn omitted it). A
    key-presence check would pick ``text=None`` on a binary frame and forward
    ``None``, which raises ``TypeError`` in the ``websockets`` client and kills
    the terminal / state sockets the workspace SPA relies on. Selecting by value
    fixes it; an event with neither payload yields ``None`` (caller skips it).
    """
    assert _select_ws_receive_payload({"type": "websocket.receive", "bytes": None, "text": "hello"}) == "hello"
    # The regression case: a binary frame carries text=None alongside its bytes.
    assert _select_ws_receive_payload({"type": "websocket.receive", "bytes": b"world", "text": None}) == b"world"
    assert _select_ws_receive_payload({"type": "websocket.receive", "bytes": None, "text": None}) is None


def test_ws_forward_closes_client_leg_when_backend_closes(tmp_path: Path) -> None:
    """When the backend WS closes, the client leg must be closed too.

    Regression test for the half-open wedge behind the chat-desync incidents:
    the forwarder used to ``gather`` both relay tasks, so after a backend death
    the client->backend task kept blocking on a send-quiet client (the system
    interface's ``/api/ws`` sends nothing after registration) and the client
    socket stayed open forever. The browser never observed a close, never
    reconnected, and silently stopped receiving all real-time updates while
    the server saw zero clients (agent layout ops failed with 412).
    """

    def backend_handler(connection: ServerConnection) -> None:
        connection.send("hello-from-backend")
        # Returning closes the backend side of the connection.

    backend_server = ws_serve(backend_handler, "127.0.0.1", 0)
    backend_port = backend_server.socket.getsockname()[1]
    server_thread = threading.Thread(target=backend_server.serve_forever, daemon=True)
    server_thread.start()
    try:
        auth_store = FileAuthStore(data_directory=tmp_path)
        resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
        instance_key = _make_test_instance_key()
        resolver.add_known_agent(instance_key)
        resolver.update_services(instance_key, {"system_interface": f"http://127.0.0.1:{backend_port}"})
        preauth = "preauth-cookie-ws-backend-close"
        app = create_forward_app(
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=SSHTunnelManager(),
            envelope_writer=EnvelopeWriter(output=io.StringIO()),
            listen_host="127.0.0.1",
            listen_port=18421,
            preauth_cookie_value=preauth,
            allow_host_loopback=True,
        )

        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421") as client:
            with client.websocket_connect(
                f"ws://{_TEST_AGENT_ID}.localhost:18421/api/ws",
                headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
            ) as websocket_session:
                assert websocket_session.receive_text() == "hello-from-backend"
                # The client never sends: the close must reach it anyway. Run
                # the receive on a bounded thread so a regression (client leg
                # left half-open) fails fast instead of hanging the suite.
                outcomes: list[str] = []

                def _receive_until_close() -> None:
                    try:
                        websocket_session.receive_text()
                        outcomes.append("unexpected-message")
                    except WebSocketDisconnect:
                        outcomes.append("disconnect")

                receiver = threading.Thread(target=_receive_until_close, daemon=True)
                receiver.start()
                receiver.join(timeout=10)
                assert outcomes == ["disconnect"], (
                    f"client leg did not observe the backend close (outcomes={outcomes})"
                )
    finally:
        backend_server.shutdown()


def test_ws_forward_stamps_owner_header_on_backend_handshake(tmp_path: Path) -> None:
    """The WS forward must stamp X-Share-Owner=true and never forward a client-supplied identity.

    The WebSocket analogue of
    ``test_forwarded_request_gets_owner_header_and_drops_forged_identity``: the
    single authenticated local user is always the workspace owner, so the
    backend handshake must carry the authoritative owner flag (and no email)
    regardless of any forged ``X-Share-Owner`` / ``X-Share-Email`` the client
    sends -- client headers are not forwarded on this path.
    """
    captured: dict[str, str | None] = {}

    def backend_handler(connection: ServerConnection) -> None:
        # The handshake request is always present inside the handler; assert it
        # so the header reads are not against ``Request | None``.
        assert connection.request is not None
        captured["owner"] = connection.request.headers.get(SHARE_OWNER_HEADER)
        captured["email"] = connection.request.headers.get(SHARE_EMAIL_HEADER)
        connection.send("hello-from-backend")

    backend_server = ws_serve(backend_handler, "127.0.0.1", 0)
    backend_port = backend_server.socket.getsockname()[1]
    server_thread = threading.Thread(target=backend_server.serve_forever, daemon=True)
    server_thread.start()
    try:
        auth_store = FileAuthStore(data_directory=tmp_path)
        resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
        instance_key = _make_test_instance_key()
        resolver.add_known_agent(instance_key)
        resolver.update_services(instance_key, {"system_interface": f"http://127.0.0.1:{backend_port}"})
        preauth = "preauth-cookie-ws-owner-header"
        app = create_forward_app(
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=SSHTunnelManager(),
            envelope_writer=EnvelopeWriter(output=io.StringIO()),
            listen_host="127.0.0.1",
            listen_port=18421,
            preauth_cookie_value=preauth,
            allow_host_loopback=True,
        )

        with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421") as client:
            with client.websocket_connect(
                f"ws://{_TEST_AGENT_ID}.localhost:18421/api/ws",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    SHARE_OWNER_HEADER: "false",
                    SHARE_EMAIL_HEADER: "attacker@evil.example",
                },
            ) as websocket_session:
                # Receiving the backend's message guarantees its handler ran and
                # captured the handshake headers (capture precedes the send).
                assert websocket_session.receive_text() == "hello-from-backend"
        assert captured["owner"] == "true"
        assert captured["email"] is None
    finally:
        backend_server.shutdown()


# -- Embedding substrate: cookie attributes, /_bridge, frame-ancestors ------


def test_http2_authenticate_cookie_is_none_and_partitioned(
    http2_app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """On the TLS path the session cookie must be sendable from a cross-site iframe.

    The minds chrome embeds workspace origins cross-site, so anything short of
    ``SameSite=None; Secure; Partitioned`` means the embedded workspace's
    requests silently omit the cookie and auth fails.
    """
    client, store, _resolver = http2_app_setup
    code = OneTimeCode("http2-partitioned-1")
    store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}")
    set_cookie = response.headers["set-cookie"].lower()
    assert "samesite=none" in set_cookie
    assert "secure" in set_cookie
    assert "partitioned" in set_cookie


def test_plain_http_authenticate_cookie_stays_lax(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """SameSite=None requires Secure, so the plain-HTTP path keeps Lax (no embedding)."""
    client, store, _resolver = app_setup
    code = OneTimeCode("plain-lax-1")
    store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}")
    set_cookie = response.headers["set-cookie"].lower()
    assert "samesite=lax" in set_cookie
    assert "partitioned" not in set_cookie


def test_http2_subdomain_auth_bridge_cookie_is_none_and_partitioned(tmp_path: Path) -> None:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
        use_http2=True,
    )
    valid_host_id = "host-" + "0" * 31 + "a"
    token = create_subdomain_auth_token(signing_key=auth_store.get_signing_key(), origin_coordinate=valid_host_id)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/_subdomain_auth?token={token}&next=/",
            headers={"host": f"{valid_host_id}.localhost:18421"},
        )
    set_cookie = response.headers["set-cookie"].lower()
    assert "samesite=none" in set_cookie
    assert "partitioned" in set_cookie
    assert f"domain={valid_host_id}.localhost" in set_cookie


def _bridge_app(tmp_path: Path, browser_bridge_token: str | None) -> tuple[TestClient, FileAuthStore]:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
        use_http2=True,
        browser_bridge_token=browser_bridge_token,
    )
    return TestClient(app, follow_redirects=False), auth_store


def test_browser_bridge_is_404_when_no_token_configured(tmp_path: Path) -> None:
    client, _store = _bridge_app(tmp_path, browser_bridge_token=None)
    response = client.get("/_bridge?token=anything&next=/")
    assert response.status_code == 404


def test_browser_bridge_rejects_wrong_token(tmp_path: Path) -> None:
    client, _store = _bridge_app(tmp_path, browser_bridge_token="right-token")
    response = client.get("/_bridge?token=wrong-token&next=/")
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    # A missing token is rejected the same way (never treated as a match).
    assert client.get("/_bridge").status_code == 403
    # A non-ASCII token is a clean 403, not a 500 (compare_digest raises
    # TypeError on non-ASCII str; the compare must happen on bytes).
    assert client.get("/_bridge?token=%C3%A9&next=/").status_code == 403


def test_browser_bridge_sets_cookie_and_redirects_to_sanitized_next(tmp_path: Path) -> None:
    """The bridge is the browser twin of Electron's preauth cookie injection.

    A matching spawn-time token yields the bare-origin session cookie and an
    onward redirect; an off-origin ``next`` collapses to ``/`` so the bridge
    cannot be used as an open redirector.
    """
    client, _store = _bridge_app(tmp_path, browser_bridge_token="right-token")
    response = client.get("/_bridge?token=right-token&next=/goto/host-00000000000000000000000000000000/")
    assert response.status_code == 302
    assert response.headers["location"] == "/goto/host-00000000000000000000000000000000/"
    set_cookie = response.headers["set-cookie"].lower()
    assert MNGR_FORWARD_SESSION_COOKIE_NAME in set_cookie
    assert "samesite=none" in set_cookie
    evil = client.get("/_bridge?token=right-token&next=//evil.com/")
    assert evil.headers["location"] == "/"


def test_workspace_responses_carry_default_deny_frame_ancestors(tmp_path: Path) -> None:
    """Every proxied workspace-origin response gets the appended frame-ancestors CSP.

    The unauthenticated redirect is proxy-generated, but it flows through the
    same middleware seam as real proxied responses, so asserting on it proves
    the injection point without a live backend.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
        use_http2=True,
    )
    with TestClient(app, base_url=f"https://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get("/", headers={"accept": "text/html"})
    policy = response.headers["content-security-policy"]
    assert policy == (
        f"frame-ancestors 'self' https://{_TEST_AGENT_ID}.localhost:18421 https://*.{_TEST_AGENT_ID}.localhost:18421"
    )


def test_workspace_responses_include_configured_embedder_origins(tmp_path: Path) -> None:
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
        use_http2=True,
        embedder_origins=(EmbedderOrigin("http://localhost:8420"),),
    )
    with TestClient(app, base_url=f"https://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        response = client.get("/", headers={"accept": "text/html"})
    assert response.headers["content-security-policy"].endswith("http://localhost:8420")


def test_bare_origin_responses_carry_no_frame_ancestors(
    app_setup: tuple[TestClient, FileAuthStore, ForwardResolver],
) -> None:
    """The policy applies to workspace origins only; the bare login origin is untouched."""
    client, _store, _resolver = app_setup
    response = client.get("/")
    assert "content-security-policy" not in response.headers


# -- TunnelWarningRateLimiter ----------------------------------------------


def test_tunnel_warning_rate_limiter_logs_the_first_warning_per_agent() -> None:
    clock = [100.0]
    limiter = TunnelWarningRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])

    assert limiter.suppressed_repeats_if_should_log("agent-a") == 0
    assert limiter.suppressed_repeats_if_should_log("agent-b") == 0


def test_tunnel_warning_rate_limiter_suppresses_repeats_inside_the_interval() -> None:
    clock = [100.0]
    limiter = TunnelWarningRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])

    assert limiter.suppressed_repeats_if_should_log("agent-a") == 0
    clock[0] = 130.0
    assert limiter.suppressed_repeats_if_should_log("agent-a") is None
    clock[0] = 159.9
    assert limiter.suppressed_repeats_if_should_log("agent-a") is None


def test_tunnel_warning_rate_limiter_reports_the_suppressed_count_after_the_interval() -> None:
    clock = [100.0]
    limiter = TunnelWarningRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])

    assert limiter.suppressed_repeats_if_should_log("agent-a") == 0
    for tick in (110.0, 120.0, 130.0):
        clock[0] = tick
        assert limiter.suppressed_repeats_if_should_log("agent-a") is None
    clock[0] = 161.0
    assert limiter.suppressed_repeats_if_should_log("agent-a") == 3
    # The count resets once reported.
    clock[0] = 222.0
    assert limiter.suppressed_repeats_if_should_log("agent-a") == 0


def test_tunnel_warning_rate_limiter_tracks_agents_independently() -> None:
    clock = [100.0]
    limiter = TunnelWarningRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])

    assert limiter.suppressed_repeats_if_should_log("agent-a") == 0
    clock[0] = 110.0
    assert limiter.suppressed_repeats_if_should_log("agent-b") == 0
    assert limiter.suppressed_repeats_if_should_log("agent-a") is None


# -- Streaming stall/close regression tests --------------------------------
#
# Regression tests for the forward pool-saturation wedge: an SSE client leg
# that went quiet without disconnecting left the ASGI ``send`` blocked forever
# (the ASGI server has no write timeout for an active stream), pinning the
# suspended body generator and its pooled backend connection until the pool
# saturated and every proxied request 504'd without ever dialing the backend.
# These drive the real streaming response against a real httpx client and a
# real socket backend, so they exercise the exact resources that leaked.

_SSE_KEEPALIVE_CHUNK = b": keepalive\n\n"

# Bounds the stub backend's keepalive stream so the test server cannot spin
# forever if a scenario wedges; scenarios finish after a handful of chunks.
_MAX_TEST_KEEPALIVE_CHUNKS = 10_000


async def _serve_sse_keepalives(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP/1.1 backend: consume the request head, then stream chunked SSE keepalives."""
    for _ in range(100):
        line = await reader.readline()
        if not line or line == b"\r\n":
            break
    writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n")
    try:
        for _ in range(_MAX_TEST_KEEPALIVE_CHUNKS):
            writer.write(b"%x\r\n%s\r\n" % (len(_SSE_KEEPALIVE_CHUNK), _SSE_KEEPALIVE_CHUNK))
            await writer.drain()
            await asyncio.sleep(0.005)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _proxy_body_from(backend_response: httpx.Response) -> AsyncGenerator[bytes, None]:
    """The same shape as ``_forward_workspace_http``'s ``_stream()``: relays chunks, closes nothing."""
    async for chunk in backend_response.aiter_bytes():
        yield chunk


@asynccontextmanager
async def _running_sse_backend() -> AsyncGenerator[int, None]:
    """Serve ``_serve_sse_keepalives`` on a loopback port for the duration of the block."""
    server = await asyncio.start_server(_serve_sse_keepalives, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def _sse_proxy_scenario(
    close_events: list[str],
) -> AsyncGenerator[tuple[httpx.Response, _StallGuardedStreamingResponse], None]:
    """Yield a live backend SSE response and the guarded streaming response proxying it."""
    async with _running_sse_backend() as listen_port:
        client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        try:
            backend_response = await client.send(
                client.build_request("GET", f"http://127.0.0.1:{listen_port}/api/events"), stream=True
            )

            async def _close_backend() -> None:
                close_events.append("close_started")
                await backend_response.aclose()
                close_events.append("close_returned")

            response = _StallGuardedStreamingResponse(
                body_generator=_proxy_body_from(backend_response),
                close_backend=_close_backend,
                agent_id=AgentId(),
                status_code=200,
                media_type="text/event-stream",
                headers={},
                send_timeout_seconds=0.2,
            )
            yield backend_response, response
        finally:
            await client.aclose()


class _StallingSendChannel(MutableModel):
    """ASGI send stub that accepts a fixed number of messages, then blocks forever (a quiet client leg)."""

    accepted_message_count: int = Field(description="Messages to accept before stalling")
    sent_messages: list[dict[str, Any]] = Field(default_factory=list, description="Messages accepted so far")

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        if len(self.sent_messages) >= self.accepted_message_count:
            await asyncio.Event().wait()
        self.sent_messages.append(dict(message))


class _RaisingSendChannel(MutableModel):
    """ASGI send stub that accepts a fixed number of messages, then raises (a torn-down client transport)."""

    accepted_message_count: int = Field(description="Messages to accept before raising")
    sent_messages: list[dict[str, Any]] = Field(default_factory=list, description="Messages accepted so far")

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        if len(self.sent_messages) >= self.accepted_message_count:
            raise MngrForwardError("simulated client transport failure")
        self.sent_messages.append(dict(message))


class _NeverDisconnectingReceiveChannel(MutableModel):
    """ASGI receive stub for a client leg that never delivers ``http.disconnect``.

    The (empty) request body is delivered once, as a server would, so a handler
    reading it does not block; after that the channel goes silent forever.
    """

    _is_request_delivered: bool = PrivateAttr(default=False)

    async def __call__(self) -> dict[str, Any]:
        if not self._is_request_delivered:
            self._is_request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise MngrForwardError("unreachable: the never-set event resolved")


async def _run_stalled_client_scenario() -> None:
    close_events: list[str] = []
    async with _sse_proxy_scenario(close_events) as (backend_response, response):
        send_channel = _StallingSendChannel(accepted_message_count=2)
        scope = {"type": "http", "asgi": {"spec_version": "2.1", "version": "3.0"}}
        # The full ASGI call must complete on its own once the per-send stall
        # timeout trips; the outer timeout only bounds the test on regression.
        async with asyncio.timeout(10.0):
            await response(scope, _NeverDisconnectingReceiveChannel(), send_channel)
        assert close_events == ["close_started", "close_returned"]
        assert backend_response.is_closed
        assert len(send_channel.sent_messages) == send_channel.accepted_message_count


def test_stalled_client_send_releases_backend_sse_stream() -> None:
    """A client leg that goes quiet mid-stream must not pin the backend response forever."""
    asyncio.run(_run_stalled_client_scenario())


async def _run_stalled_response_start_scenario() -> None:
    close_events: list[str] = []
    async with _sse_proxy_scenario(close_events) as (backend_response, response):
        # Accepts nothing at all: the response *headers* are the send that stalls.
        send_channel = _StallingSendChannel(accepted_message_count=0)
        scope = {"type": "http", "asgi": {"spec_version": "2.1", "version": "3.0"}}
        async with asyncio.timeout(10.0):
            await response(scope, _NeverDisconnectingReceiveChannel(), send_channel)
        assert send_channel.sent_messages == [], "the stall was supposed to happen on the very first send"
        assert close_events == ["close_started", "close_returned"]
        assert backend_response.is_closed


def test_stalled_response_start_releases_backend_sse_stream() -> None:
    """A stall on the response headers -- before the body generator ever starts -- must still free the pool slot.

    Hypercorn writes ``http.response.start`` straight to the socket and drains
    it, so it blocks on the same unwritable client leg as any body chunk. The
    body generator has not been started at that point, and closing an unstarted
    async generator runs none of its body, so cleanup owned by the generator's
    ``finally`` would silently skip this path and pin the pooled backend
    connection for the life of the process.
    """
    asyncio.run(_run_stalled_response_start_scenario())


async def _run_raising_send_scenario() -> None:
    close_events: list[str] = []
    async with _sse_proxy_scenario(close_events) as (backend_response, response):
        send_channel = _RaisingSendChannel(accepted_message_count=2)
        with pytest.raises(MngrForwardError):
            async with asyncio.timeout(10.0):
                await response.stream_response(send_channel)
        # The backend response must be closed by the time stream_response
        # unwinds -- deterministically, not whenever GC finalizes the
        # abandoned generator.
        assert close_events == ["close_started", "close_returned"]
        assert backend_response.is_closed


def test_send_raising_mid_stream_closes_backend_stream_deterministically() -> None:
    """A ``send`` that raises mid-stream must close the backend response without waiting for GC."""
    asyncio.run(_run_raising_send_scenario())


async def _run_sse_write_failure_through_the_forward_handler(
    envelope_output: io.StringIO,
) -> tuple[list[dict[str, Any]], int]:
    """Proxy a real SSE stream through the real handler, fail a client write, then re-use the pool.

    The client's pool holds exactly one connection, so the follow-up request at
    the end is the release assertion: it can only be served if the backend
    stream that just ended gave its slot back.
    """
    async with _running_sse_backend() as listen_port:
        # Accepts the response head plus three chunks, then fails the way a
        # client transport that has been torn down does. Unlike a disconnect
        # (which cancels the response, and httpcore closes its own stream under
        # cancellation regardless), this leaves the body generator suspended at
        # its ``yield`` -- so only the response's own ``finally`` gives the
        # pooled slot back.
        send_channel = _RaisingSendChannel(accepted_message_count=4)
        receive_channel = _NeverDisconnectingReceiveChannel()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 18421),
        }
        http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(5.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        try:
            response = await _forward_workspace_http(
                request=Request(scope, receive_channel),
                backend_url=f"http://127.0.0.1:{listen_port}",
                http_client=http_client,
                agent_id=AgentId(),
                envelope_writer=EnvelopeWriter(output=envelope_output),
                stall_notice_seconds=_STALL_NOTICE_SECONDS,
                was_backend_refused=_never_refused,
            )
            with pytest.raises(MngrForwardError):
                # The ASGI call must unwind on its own once the write fails; the
                # outer timeout only bounds the test on regression.
                async with asyncio.timeout(10.0):
                    await response(scope, receive_channel, send_channel)
            # Short pool budget so a slot that was never released fails here
            # rather than stalling the test for the full connect budget.
            probe_response = await http_client.send(
                http_client.build_request(
                    "GET",
                    f"http://127.0.0.1:{listen_port}/api/events",
                    timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=2.0),
                ),
                stream=True,
            )
            await probe_response.aclose()
            return send_channel.sent_messages, probe_response.status_code
        finally:
            await http_client.aclose()


def test_forwarded_sse_stream_returns_its_pooled_backend_connection_when_a_client_write_fails() -> None:
    """The handler's own streaming response must free its pooled backend slot when the client leg fails.

    This drives ``_forward_workspace_http`` itself -- not a hand-built response
    -- against a real socket backend over a real, one-connection pool, so it is
    what pins the production wiring rather than a replica of it. A handler that
    returned a plain ``StreamingResponse``, or one that dropped
    ``close_backend``, leaves the backend stream pinned by a body generator
    suspended at its ``yield``, and the follow-up request below then finds no
    free slot and raises ``httpx.PoolTimeout``: the wedge this branch fixes, in
    miniature -- pool saturated, backend never dialed.
    """
    envelope_output = io.StringIO()
    sent_messages, probe_status_code = asyncio.run(_run_sse_write_failure_through_the_forward_handler(envelope_output))

    assert probe_status_code == 200
    message_types = [message["type"] for message in sent_messages]
    assert message_types == ["http.response.start"] + ["http.response.body"] * 3, (
        "the backend stream never reached the client"
    )
    # A client leg that broke says nothing about the backend's health.
    assert _envelope_lines(envelope_output) == []


def test_subdomain_forward_distinguishes_pool_exhaustion_from_backend_timeout(tmp_path: Path) -> None:
    """``PoolTimeout`` (proxy pool exhausted, backend never dialed) must be tellable apart from a backend timeout.

    Both used to surface as an identical 504 "Backend timed out" +
    ``CONNECT_ERROR``, which made a saturated proxy indistinguishable from a
    wedged backend -- so minds read the proxy's own saturation as the workspace
    being sick and restarted it. ``POOL_EXHAUSTED`` says whose fault it is.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-pool-timeout"
    captured: list[httpx.Request] = []
    app, env_out, mock_client = _make_forward_app_with_capture(
        tmp_path,
        captured,
        instance_key,
        preauth,
        raise_error=httpx.PoolTimeout,
    )

    with TestClient(app, base_url=f"http://{_TEST_AGENT_ID}.localhost:18421", follow_redirects=False) as client:
        app.state.http_client = mock_client
        sse_response = client.get(
            "/api/events",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "text/event-stream",
            },
        )
        plain_response = client.get(
            "/api/state",
            headers={
                "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                "accept": "application/json",
            },
        )

    assert sse_response.status_code == 504
    assert sse_response.text == "Proxy connection pool exhausted"
    assert plain_response.status_code == 504
    assert plain_response.text == "Proxy connection pool exhausted"
    lines = _envelope_lines(env_out)
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)["payload"]
        assert payload["type"] == "system_interface_backend_failure"
        assert payload["reason"] == "POOL_EXHAUSTED"
        # ``POOL_EXHAUSTED`` is a device-side reason, and a device-side detail is
        # what the recovery card expands verbatim behind "Error details".
        # ``PoolTimeout`` stringifies empty, so without a description of its own
        # the class-name fallback would put the bare word in front of a user.
        assert "PoolTimeout" not in payload["detail"]
        assert "pool" in payload["detail"]


class _FixedSocketTunnelManager(SSHTunnelManager):
    """SSHTunnelManager that reports a tunnel socket path without opening a tunnel."""

    reported_socket_path: Path = Field(description="Path reported for every tunnel")

    def get_tunnel_socket_path(self, ssh_info: RemoteSSHInfo, remote_host: str, remote_port: int) -> Path:
        return self.reported_socket_path


def _pool_ceiling_of(client: httpx.AsyncClient) -> int:
    """Read back the connection ceiling httpx built into a client's transport pool.

    httpx keeps ``limits`` only long enough to construct the pool, so confirming
    that a client was given one means reading the pool it built. The instance
    dictionaries are walked rather than the attributes because neither hop
    type-checks: ``AsyncClient._transport`` is annotated as the abstract
    ``AsyncBaseTransport``, which declares no pool at all, and ``_pool`` is in
    turn a union whose other arm is the stub raised when SOCKS support is
    missing, which declares no ceiling.
    """
    pool = client._transport.__dict__["_pool"]
    return int(pool.__dict__["_max_connections"])


def test_both_proxying_http_clients_are_built_with_explicit_pool_ceilings(tmp_path: Path) -> None:
    """Neither proxying client may fall back to httpx's default connection ceiling.

    That default (100) is exactly hypercorn's ``h2_max_concurrent_streams``, and
    the coincidence is what turned a handful of wedged streaming responses into
    a proxy-wide dead end: every later request timed out waiting for a pooled
    slot without ever dialing a backend. Each ``limits=`` kwarg is one word, and
    losing either restores that ceiling silently.
    """
    instance_key = _make_test_instance_key()
    preauth = "preauth-cookie-pool-limits"
    app, _env_out, _mock_client = _make_forward_app_with_capture(tmp_path, [], instance_key, preauth)

    # Entered so the lifespan builds the direct client the way production does.
    with TestClient(app, base_url=f"http://{_TEST_HOST_ID}.localhost:18421", follow_redirects=False):
        assert _pool_ceiling_of(app.state.http_client) == _PROXY_POOL_LIMITS.max_connections

    # No socket is opened: the tunnel client is only constructed, never dialed.
    tunnel_client = _get_tunnel_http_client(
        tunnel_manager=_FixedSocketTunnelManager(reported_socket_path=tmp_path / "tunnel.sock"),
        backend_url="http://stub-backend:8000",
        ssh_info=RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=tmp_path / "fake_key"),
        ssh_http_clients={},
        ssh_http_clients_lock=threading.Lock(),
    )
    assert tunnel_client is not None
    try:
        assert _pool_ceiling_of(tunnel_client) == _TUNNEL_POOL_LIMITS.max_connections
    finally:
        asyncio.run(tunnel_client.aclose())


# Fragment of the debug line the handler logs when a disconnect ties with the
# handoff. It is the one observable that separates that branch from the one for
# a disconnect that arrives *before* the backend stream opens, which is
# otherwise identical in everything the caller can see.
_TIE_BRANCH_LOG_FRAGMENT = "as the backend stream"


async def _run_simultaneous_disconnect_and_handoff(
    envelope_output: io.StringIO,
) -> tuple[Response, list[httpx.Response], list[str]]:
    """Open an SSE backend stream and deliver the client's disconnect in the same event loop step.

    The ``asyncio.Barrier`` is what rendezvouses the two: the backend handler and
    the receive channel both wait on it, so neither can finish before the other
    has arrived. The receive channel is deliberately let there first (the handler
    yields once before rendezvousing), which leaves the handler as the party that
    releases the barrier and therefore runs on without yielding, while the
    receive channel's wakeup is already queued ahead of the ``asyncio.wait``
    waiter's. Both tasks therefore finish before that waiter resumes, putting
    both in its ``done`` set. Which branch actually ran is not assumed: the
    caller checks the branch's own log line.

    The receive channel delivers exactly one ``http.disconnect`` and then goes
    silent, which is what hypercorn does (``HTTPStream.handle`` queues one when
    the stream closes). Returns the handler's response, the backend responses
    the transport produced, and the chunks pulled from the backend stream.
    """
    handoff_barrier = asyncio.Barrier(2)
    backend_responses: list[httpx.Response] = []
    streamed_chunks: list[str] = []

    async def _backend_body() -> AsyncGenerator[bytes, None]:
        streamed_chunks.append("chunk")
        yield _SSE_KEEPALIVE_CHUNK

    async def _handle(request: httpx.Request) -> httpx.Response:
        # Hand the loop over first, so the client's disconnect is the party
        # already waiting at the rendezvous rather than the one releasing it.
        await asyncio.sleep(0)
        await handoff_barrier.wait()
        backend_response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_backend_body())
        backend_responses.append(backend_response)
        return backend_response

    is_body_delivered = False
    is_disconnect_delivered = False

    async def _receive() -> Message:
        nonlocal is_body_delivered, is_disconnect_delivered
        if not is_body_delivered:
            is_body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if is_disconnect_delivered:
            await asyncio.Event().wait()
        await handoff_barrier.wait()
        is_disconnect_delivered = True
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 18421),
    }
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handle), follow_redirects=False)
    try:
        response = await _forward_workspace_http(
            request=Request(scope, _receive),
            backend_url="http://stub-backend",
            http_client=http_client,
            agent_id=AgentId(),
            envelope_writer=EnvelopeWriter(output=envelope_output),
            stall_notice_seconds=_STALL_NOTICE_SECONDS,
            was_backend_refused=_never_refused,
        )
    finally:
        await http_client.aclose()
    return response, backend_responses, streamed_chunks


def test_sse_handoff_that_ties_with_the_client_disconnect_releases_the_backend_stream() -> None:
    """A disconnect that lands with the backend handoff must end the request, not start a stream.

    Both tasks can finish in the same ``asyncio.wait``, and only one
    ``http.disconnect`` is ever queued -- so a stream handed on here would be
    invisible to starlette's own disconnect watcher, while writes to a
    connection asyncio has already lost are discarded rather than failing or
    blocking. Neither the watcher nor the stall guard would end it, and a
    system interface's event stream does not end on its own, so the pooled
    backend connection would be pinned for the life of the process.
    """
    envelope_output = io.StringIO()
    logged_messages: list[str] = []
    sink_id = logger.add(logged_messages.append, level="DEBUG")
    try:
        response, backend_responses, streamed_chunks = asyncio.run(
            _run_simultaneous_disconnect_and_handoff(envelope_output)
        )
    finally:
        logger.remove(sink_id)

    assert response.status_code == 499
    # Nothing in the outcome tells the tie apart from a disconnect that simply
    # won outright: that branch also ends 499, also produces a backend response
    # (the transport builds one before the cancelled handoff unwinds) and also
    # ends up closed, because httpx closes the response itself when ``send`` is
    # cancelled. The branch's own log line is the discriminator.
    assert any(_TIE_BRANCH_LOG_FRAGMENT in message for message in logged_messages), (
        "the disconnect never tied with the handoff, so the branch under test did not run"
    )
    assert len(backend_responses) == 1
    # Proves the handler entered ``aclose`` on the stream it had just been
    # handed. Releasing the pooled slot itself is covered where a real pool
    # exists, in
    # ``test_forwarded_sse_stream_returns_its_pooled_backend_connection_when_a_client_write_fails``.
    assert backend_responses[0].is_closed
    assert streamed_chunks == [], "the stream was served to a client that had already gone"
    # A client hanging up says nothing about the backend's health.
    assert _envelope_lines(envelope_output) == []


def test_legacy_host_origin_html_navigation_redirects_to_the_agent_origin(tmp_path: Path) -> None:
    """A persisted pre-agent-keying URL (host-<hex> origin) 301s to the canonical agent origin."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, base_url=f"http://svc.{_TEST_HOST_ID}.localhost:18421", follow_redirects=False) as client:
        html_response = client.get(
            "/some/page?x=1",
            headers={"accept": "text/html"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
        non_html_response = client.get(
            "/api/data",
            headers={"accept": "application/json"},
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert html_response.status_code == 301
    assert html_response.headers["location"] == f"http://svc.{_TEST_AGENT_ID}.localhost:18421/some/page?x=1"
    # Non-navigations from a stale open page fail fast; a reload heals them.
    assert non_html_response.status_code == 404


def test_legacy_host_origin_websocket_closes_with_the_origin_moved_code(tmp_path: Path) -> None:
    """A websocket to a legacy host-<hex> origin closes with 4004 even though the agent resolves.

    Sockets cannot be redirected, so the shim closes them with a distinct code
    (4004, not the 1013 retry code) telling the opener to heal by reloading
    onto the canonical agent origin. Asserting 4004 rather than 1013 also
    proves the resolvable agent did not take the unresolved branch.
    """
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    resolver.update_services(instance_key, {"system_interface": "http://stub-backend"})
    preauth = "preauth-cookie-legacy-ws"
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
        preauth_cookie_value=preauth,
    )
    with TestClient(app, base_url=f"http://svc.{_TEST_HOST_ID}.localhost:18421") as client:
        # ``websocket_connect`` ignores ``base_url``, so the legacy origin must
        # be in the URL itself for the handler to route on the host header. The
        # handler closes the socket before accepting, which the test client
        # surfaces as a WebSocketDisconnect on connect.
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"ws://svc.{_TEST_HOST_ID}.localhost:18421/api/ws",
                headers={"cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}"},
            ):
                pass
    assert exc_info.value.code == 4004
    assert exc_info.value.reason == "Origin moved to its agent-keyed URL"


def test_goto_canonicalizes_a_legacy_host_coordinate(tmp_path: Path) -> None:
    """/goto/<host-id>/ (a persisted window URL) bridges onto the canonical agent origin."""
    auth_store = FileAuthStore(data_directory=tmp_path)
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    instance_key = _make_test_instance_key()
    resolver.add_known_agent(instance_key)
    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=SSHTunnelManager(),
        envelope_writer=EnvelopeWriter(output=io.StringIO()),
        listen_host="127.0.0.1",
        listen_port=18421,
    )
    cookie = create_session_cookie(auth_store.get_signing_key())
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            f"/goto/{_TEST_HOST_ID}/",
            cookies={MNGR_FORWARD_SESSION_COOKIE_NAME: cookie},
        )
    assert response.status_code == 302
    assert response.headers["Location"].startswith(f"http://{_TEST_AGENT_ID}.localhost:18421/_subdomain_auth?")
