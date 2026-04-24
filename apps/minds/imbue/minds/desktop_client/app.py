import asyncio
import concurrent.futures
import html
import json
import os
import queue
import re
import socket as socket_module
from collections.abc import AsyncGenerator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from urllib.parse import quote

import httpx
import paramiko
import websockets
import websockets.asyncio.client
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from websockets import ClientConnection

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.agent_creator import WORKSPACE_SERVER_SERVICE_NAME
from imbue.minds.desktop_client.api_v1 import create_api_v1_router
from imbue.minds.desktop_client.api_v1 import get_cf_client_with_auth
from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import MngrStreamManager
from imbue.minds.desktop_client.cloudflare_client import CloudflareClient
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import create_subdomain_auth_token
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_subdomain_auth_token
from imbue.minds.desktop_client.deps import BackendResolverDep
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayManager
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import SharingRequestEvent
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.ssh_tunnel import parse_url_host_port
from imbue.minds.desktop_client.supertokens_routes import create_supertokens_router
from imbue.minds.desktop_client.templates import render_accounts_page
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_creating_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates import workspace_accent
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token as _load_tunnel_token
from imbue.minds.desktop_client.tunnel_token_store import save_tunnel_token as _save_tunnel_token
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServiceName
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0


_EXCLUDED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "transfer-encoding",
        "content-encoding",
        "content-length",
    }
)


# -- Dependency injection helpers --


def _get_auth_store(request: Request) -> AuthStoreInterface:
    return request.app.state.auth_store


AuthStoreDep = Annotated[AuthStoreInterface, Depends(_get_auth_store)]


# -- Auth helpers --


def _is_authenticated(
    cookies: Mapping[str, str],
    auth_store: AuthStoreInterface,
) -> bool:
    """Check whether the user has a valid global session cookie."""
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = auth_store.get_signing_key()
    cookie_value = cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(
        cookie_value=cookie_value,
        signing_key=signing_key,
    )


# -- WebSocket forwarding helpers --


async def _forward_client_to_backend(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
) -> None:
    """Forward messages from the client WebSocket to the backend.

    Terminates via WebSocketDisconnect (client disconnects),
    ConnectionClosed (backend disconnects), or RuntimeError (Starlette
    raises this when receive() is called after a disconnect was already
    delivered).
    """
    try:
        while True:
            data = await client_websocket.receive()
            msg_type = data.get("type", "")
            if msg_type == "websocket.disconnect":
                break
            if "text" in data:
                await backend_ws.send(data["text"])
            elif "bytes" in data:
                await backend_ws.send(data["bytes"])
            else:
                logger.trace("Ignoring WebSocket message with no text or bytes: {}", msg_type)
    except WebSocketDisconnect:
        logger.trace("Client WebSocket disconnected")
    except RuntimeError as e:
        logger.trace("Client WebSocket receive error (likely post-disconnect): {}", e)
    except websockets.exceptions.ConnectionClosed:
        logger.debug("Backend WebSocket closed while forwarding client message")

    try:
        await backend_ws.close()
    except websockets.exceptions.ConnectionClosed:
        logger.trace("Backend WebSocket already closed during cleanup")


async def _forward_backend_to_client(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
    agent_id: AgentId,
) -> None:
    """Forward messages from the backend WebSocket to the client."""
    try:
        async for msg in backend_ws:
            if isinstance(msg, str):
                await client_websocket.send_text(msg)
            else:
                await client_websocket.send_bytes(msg)
    except websockets.exceptions.ConnectionClosed:
        logger.debug("Backend WebSocket closed for {}", agent_id)
    except RuntimeError as e:
        logger.trace("Client WebSocket send error (likely post-disconnect): {}", e)


# -- Lifespan --


@asynccontextmanager
async def _managed_lifespan(
    inner_app: FastAPI,
    is_externally_managed_client: bool,
) -> AsyncGenerator[None, None]:
    """Manage the httpx client and SSH tunnel lifecycles for the desktop client."""
    if not is_externally_managed_client:
        inner_app.state.http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
    inner_app.state.ssh_http_clients: dict[str, httpx.AsyncClient] = {}
    # Captured here so background callbacks (e.g. the mngr events refresh
    # dispatch) can schedule async work on the server's running loop via
    # asyncio.run_coroutine_threadsafe.
    inner_app.state.event_loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        # Clear the captured loop reference first so background callbacks that
        # race with shutdown see None and drop their events instead of trying
        # to schedule on a loop that is about to close.
        inner_app.state.event_loop = None
        for client in inner_app.state.ssh_http_clients.values():
            await client.aclose()
        inner_app.state.ssh_http_clients.clear()
        if not is_externally_managed_client:
            await inner_app.state.http_client.aclose()
        # Stop mngr observe/events subprocesses before cleaning up tunnels.
        # This runs inside uvicorn's lifespan shutdown, which happens BEFORE
        # uvicorn re-raises the captured SIGTERM signal. A finally block
        # around uvicorn.run() would never execute because uvicorn calls
        # signal.raise_signal(SIGTERM) after shutdown, killing the process.
        stream_manager: MngrStreamManager | None = inner_app.state.stream_manager
        if stream_manager is not None:
            logger.info("Stopping stream manager subprocesses...")
            stream_manager.stop()
            logger.info("Stream manager stopped.")
        latchkey_gateway_manager: LatchkeyGatewayManager | None = inner_app.state.latchkey_gateway_manager
        if latchkey_gateway_manager is not None:
            logger.info("Stopping latchkey gateway manager...")
            latchkey_gateway_manager.stop()
            logger.info("Latchkey gateway manager stopped.")
        tunnel_manager: SSHTunnelManager | None = inner_app.state.tunnel_manager
        if tunnel_manager is not None:
            tunnel_manager.cleanup()


# -- Route handlers (module-level, using Depends for dependency injection) --


def _handle_login(
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    code = OneTimeCode(one_time_code)

    # If user already has a valid session, redirect to landing page
    if _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=307, headers={"Location": "/"})

    # Render JS redirect to /authenticate (prevents prefetch consumption)
    html = render_login_redirect_page(one_time_code=code)
    return HTMLResponse(content=html)


def _handle_authenticate(
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    code = OneTimeCode(one_time_code)

    is_valid = auth_store.validate_and_consume_code(code=code)

    if not is_valid:
        html = render_auth_error_page(message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)

    # Set a host-only session cookie on the bare origin. We do NOT try to
    # share the cookie across `<agent-id>.localhost` subdomains via
    # ``Domain=localhost`` -- both curl and Chromium treat ``localhost`` as
    # a public suffix and refuse to send such cookies to subdomains. Each
    # subdomain gets its own cookie set on first visit, minted via the
    # ``/goto/{agent_id}/`` auth-bridge redirect below.
    signing_key = auth_store.get_signing_key()
    cookie_value = create_session_cookie(signing_key=signing_key)

    response = Response(status_code=307, headers={"Location": "/"})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


def _handle_landing_page(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        html = render_login_page()
        return HTMLResponse(content=html)

    all_agent_ids = backend_resolver.list_known_workspace_ids()

    if all_agent_ids:
        telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
        telegram_status: dict[str, bool] | None = None
        if telegram_orchestrator is not None:
            telegram_status = {str(aid): telegram_orchestrator.agent_has_telegram(aid) for aid in all_agent_ids}
        agent_names: dict[str, str] = {}
        for aid in all_agent_ids:
            ws_name = backend_resolver.get_workspace_name(aid)
            if ws_name:
                agent_names[str(aid)] = ws_name
            else:
                info = backend_resolver.get_agent_display_info(aid)
                agent_names[str(aid)] = info.agent_name if info else str(aid)
        html = render_landing_page(
            accessible_agent_ids=all_agent_ids,
            telegram_status_by_agent_id=telegram_status,
            agent_names=agent_names,
        )
        return HTMLResponse(content=html)

    # No agents discovered yet. If discovery is still in progress, show a
    # "Discovering agents..." page with auto-refresh. Once discovery has
    # completed with no agents found, show the create form so the user can
    # create their first agent instead of polling forever.
    if not backend_resolver.has_completed_initial_discovery():
        html = render_landing_page(accessible_agent_ids=(), is_discovering=True)
        return HTMLResponse(content=html)

    git_url = request.query_params.get("git_url", "")
    branch = request.query_params.get("branch", "")
    html = render_create_form(git_url=git_url, branch=branch)
    return HTMLResponse(content=html)


def _connect_backend_websocket(
    ws_url: str,
    subprotocols: list[str],
    tunnel_socket_path: Path | None,
) -> websockets.asyncio.client.connect:
    """Create a websockets connect context manager, optionally through an SSH tunnel.

    When tunnel_socket_path is provided, connects via a Unix domain socket that
    tunnels through SSH to the remote backend. Otherwise, connects directly.
    """
    ws_subprotocols = [websockets.Subprotocol(s) for s in subprotocols] if subprotocols else None

    if tunnel_socket_path is not None:
        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        try:
            sock.connect(str(tunnel_socket_path))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return websockets.connect(ws_url, subprotocols=ws_subprotocols, sock=sock)

    return websockets.connect(ws_url, subprotocols=ws_subprotocols)


# -- SSH tunnel helpers --


def _get_tunnel_socket_path(
    tunnel_manager: SSHTunnelManager | None,
    agent_id: AgentId,
    backend_url: str,
    backend_resolver: BackendResolverInterface,
) -> Path | None:
    """Get the Unix socket path for tunneling to a remote backend, or None for local."""
    if tunnel_manager is None:
        return None

    ssh_info = backend_resolver.get_ssh_info(agent_id)
    if ssh_info is None:
        return None

    remote_host, remote_port = parse_url_host_port(backend_url)
    return tunnel_manager.get_tunnel_socket_path(
        ssh_info=ssh_info,
        remote_host=remote_host,
        remote_port=remote_port,
    )


def _get_tunnel_http_client(
    app: FastAPI,
    agent_id: AgentId,
    backend_url: str,
    backend_resolver: BackendResolverInterface,
) -> httpx.AsyncClient | None:
    """Get an httpx client configured for SSH tunneling, or None for direct connection.

    Creates a fresh client each time to avoid stale connections when SSH
    tunnels are recreated after a broken pipe.
    """
    tunnel_manager: SSHTunnelManager | None = app.state.tunnel_manager
    socket_path = _get_tunnel_socket_path(tunnel_manager, agent_id, backend_url, backend_resolver)
    if socket_path is None:
        return None

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=_PROXY_TIMEOUT_SECONDS,
    )


# -- Auth bridge: bare origin -> per-subdomain session cookie --


def _handle_goto_workspace(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Redirect an authenticated user from the bare origin to a workspace subdomain,
    carrying a short-lived signed token that sets the subdomain's session cookie
    on first landing.

    Flow:
      1. Landing page click fetches ``/goto/<agent-id>/``.
      2. This handler verifies the bare-origin session cookie (fails back to
         ``/`` for unauth users).
      3. Mints a short-lived token and 302s to
         ``http://<agent-id>.localhost:PORT/_subdomain_auth?token=...&next=/``.
      4. The subdomain's ``/_subdomain_auth`` handler sets the subdomain cookie.

    We route through this bridge because ``Domain=localhost`` cookies don't
    cross from ``localhost`` into ``<agent>.localhost`` (public-suffix rule).
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=302, headers={"Location": "/"})

    try:
        parsed_id = AgentId(agent_id)
    except ValueError:
        return Response(status_code=404)

    signing_key = auth_store.get_signing_key()
    token = create_subdomain_auth_token(signing_key=signing_key, agent_id=str(parsed_id))

    # Preserve the user's desired landing path on the subdomain.
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/"):
        next_url = "/"

    host_header = request.headers.get("host", "")
    port = host_header.split(":")[-1] if ":" in host_header else str(request.app.state.auth_server_port or 8420)

    encoded_next = quote(next_url, safe="")
    location = f"http://{parsed_id}.localhost:{port}{_SUBDOMAIN_AUTH_PATH}?token={token}&next={encoded_next}"
    return Response(status_code=302, headers={"Location": location})


# -- Subdomain forwarding to per-workspace minds_workspace_server --

_WORKSPACE_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(agent-[a-f0-9]+)\.(?:localhost|127\.0\.0\.1)(?::\d+)?$",
    re.IGNORECASE,
)


def _workspace_pending_html_response() -> HTMLResponse:
    """HTML page shown when the workspace server is not yet answering.

    Used for both "resolver has no URL yet" and "URL exists but the server
    behind it is not serving HTTP yet" (the latter happens during the
    workspace server's own boot window, where the TCP socket may be
    accept()ing before the FastAPI app is fully initialized and the
    forwarder sees ``ConnectError`` / ``RemoteProtocolError``).

    The ``<meta http-equiv="refresh">`` makes the browser reload every
    second until the forwarder can actually proxy. No JS needed, and no
    loop runs once forwarding succeeds.
    """
    return HTMLResponse(
        content=(
            "<!doctype html><html><head>"
            '<meta http-equiv="refresh" content="1">'
            "</head><body>"
            "<p>Workspace server not yet available. Retrying...</p>"
            "</body></html>"
        )
    )


def _request_accepts_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _parse_workspace_subdomain(host_header: str) -> AgentId | None:
    """Return the agent ID if ``host_header`` is ``<agent-id>.localhost(:port)``.

    Returns None for bare ``localhost``, ``127.0.0.1``, or unparseable values;
    those requests are served by the desktop client's own routes.
    """
    if not host_header:
        return None
    match = _WORKSPACE_SUBDOMAIN_PATTERN.match(host_header)
    if match is None:
        return None
    try:
        return AgentId(match.group(1))
    except ValueError:
        return None


def _unauthenticated_subdomain_response(request: Request) -> Response:
    """Redirect to the bare-origin landing page for HTML navigations; 403 otherwise.

    The landing page (``/``) renders the login prompt for unauthenticated
    users. We deliberately do not redirect to ``/login`` because that route
    requires a ``one_time_code`` query parameter -- sending a browser there
    without one yields a 422 validation error. Users get their OTP from the
    terminal output of the desktop client, not from this redirect.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        auth_port = request.app.state.auth_server_port or 8420
        location = f"http://localhost:{auth_port}/"
        return Response(status_code=302, headers={"Location": location})
    return Response(status_code=403, content="Not authenticated")


async def _forward_workspace_http(
    request: Request,
    workspace_backend_url: str,
    http_client: httpx.AsyncClient,
) -> Response:
    """Byte-forward an HTTP request to a workspace_server URL.

    Streams SSE responses (detected by the client's ``accept: text/event-stream``),
    buffers everything else. Does NOT rewrite body or headers: the workspace_server
    already emits /service/<name>/ prefixed URLs, scoped cookies, and the SW shim.
    """
    base = workspace_backend_url.rstrip("/")
    path = request.url.path.lstrip("/")
    url = f"{base}/{path}" if path else base + "/"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)

    # Strip the desktop client's session cookie so agent-controlled workspace
    # servers cannot extract and reuse it against other agents.
    raw_cookie = headers.get("cookie")
    if raw_cookie is not None:
        stripped = "; ".join(
            c.strip() for c in raw_cookie.split(";") if not c.strip().startswith(SESSION_COOKIE_NAME + "=")
        )
        if stripped:
            headers["cookie"] = stripped
        else:
            del headers["cookie"]

    body = await request.body()

    accept = request.headers.get("accept", "")
    is_likely_sse = "text/event-stream" in accept

    if is_likely_sse:
        backend_request = http_client.build_request(method=request.method, url=url, headers=headers, content=body)
        try:
            backend_response = await http_client.send(backend_request, stream=True)
        except httpx.ConnectError:
            return Response(status_code=502, content="Workspace server connection refused")
        except httpx.TimeoutException:
            return Response(status_code=504, content="Workspace server stream timed out")

        async def _stream() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in backend_response.aiter_bytes():
                    yield chunk
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                logger.warning("Workspace server SSE stream failed for {}: {}", request.url.path, e)
            finally:
                await backend_response.aclose()

        media_type = backend_response.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            _stream(),
            status_code=backend_response.status_code,
            media_type=media_type,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        backend_response = await http_client.request(method=request.method, url=url, headers=headers, content=body)
    except httpx.ConnectError:
        # TCP-level failure ("nothing listening yet") -- indistinguishable from a
        # still-booting server at the HTTP layer, so treat it the same as the
        # "no URL yet" branch: let HTML navigations auto-refresh.
        if _request_accepts_html(request):
            return _workspace_pending_html_response()
        return Response(status_code=502, content="Workspace server connection refused")
    except httpx.ReadError:
        return Response(status_code=502, content="Workspace server connection lost")
    except httpx.RemoteProtocolError:
        # The TCP socket accepted the connection but the server closed it
        # before sending an HTTP response. This is exactly what uvicorn/ASGI
        # does during startup between ``bind()`` and the app being ready to
        # serve. Treat it as transient and auto-refresh for HTML navigations.
        if _request_accepts_html(request):
            return _workspace_pending_html_response()
        return Response(status_code=502, content="Workspace server disconnected without response")
    except httpx.TimeoutException:
        return Response(status_code=504, content="Workspace server timed out")

    response = Response(content=backend_response.content, status_code=backend_response.status_code)
    for header_key, header_value in backend_response.headers.multi_items():
        if header_key.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        response.headers.append(header_key, header_value)
    return response


_SUBDOMAIN_AUTH_PATH: Final[str] = "/_subdomain_auth"


def _handle_subdomain_auth_bridge(request: Request, agent_id: AgentId) -> Response:
    """Validate an inbound ``/_subdomain_auth`` token and set a subdomain cookie.

    The bare-origin ``/goto/{agent_id}/`` handler mints a short-lived signed
    token and redirects the browser to ``http://<agent_id>.localhost:PORT/
    _subdomain_auth?token=...&next=/...``. That's this handler. We verify the
    token was issued for this specific agent, then set a host-only session
    cookie on the subdomain and redirect to ``next``. Subsequent requests on
    this subdomain carry the cookie and pass the normal auth check.

    We do this dance because ``Domain=localhost`` cookies don't propagate to
    subdomains in Chromium / curl (localhost is treated as a public suffix).
    """
    auth_store: AuthStoreInterface = request.app.state.auth_store
    token = request.query_params.get("token", "")
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/"):
        next_url = "/"
    signing_key = auth_store.get_signing_key()
    if not verify_subdomain_auth_token(token=token, signing_key=signing_key, agent_id=str(agent_id)):
        return Response(status_code=403, content="Invalid or expired subdomain auth token")

    cookie_value = create_session_cookie(signing_key=signing_key)
    response = Response(status_code=302, headers={"Location": next_url})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


async def _handle_workspace_forward_http(request: Request) -> Response:
    """Forward an HTTP request arriving at ``<agent-id>.localhost:8420`` to that
    workspace's minds_workspace_server. Called from subdomain-routing middleware.
    """
    host_header = request.headers.get("host", "")
    agent_id = _parse_workspace_subdomain(host_header)
    if agent_id is None:
        return Response(status_code=404)

    # Auth-bridge: /_subdomain_auth?token=... sets the subdomain cookie. It
    # must be handled BEFORE the auth check because there's no cookie yet.
    if request.url.path == _SUBDOMAIN_AUTH_PATH:
        return _handle_subdomain_auth_bridge(request, agent_id)

    auth_store: AuthStoreInterface = request.app.state.auth_store
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return _unauthenticated_subdomain_response(request)

    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    if agent_id not in backend_resolver.list_known_workspace_ids():
        return Response(status_code=404, content=f"Unknown workspace: {agent_id}")

    workspace_url = backend_resolver.get_backend_url(agent_id, WORKSPACE_SERVER_SERVICE_NAME)
    if workspace_url is None:
        if _request_accepts_html(request):
            return _workspace_pending_html_response()
        return Response(status_code=503, content="Workspace server not yet available")

    try:
        tunnel_client = await asyncio.get_running_loop().run_in_executor(
            None, _get_tunnel_http_client, request.app, agent_id, workspace_url, backend_resolver
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.warning("SSH tunnel setup failed for workspace {}: {}", agent_id, e)
        return Response(status_code=502, content=f"SSH tunnel to remote workspace failed: {e}")

    active_client = tunnel_client or request.app.state.http_client
    return await _forward_workspace_http(
        request=request, workspace_backend_url=workspace_url, http_client=active_client
    )


async def _handle_workspace_forward_websocket(websocket: WebSocket) -> None:
    """Forward a WebSocket upgrade arriving at ``<agent-id>.localhost:8420`` to the
    workspace's minds_workspace_server. Auth still honored via the session cookie.
    """
    host_header = websocket.headers.get("host", "")
    agent_id = _parse_workspace_subdomain(host_header)
    if agent_id is None:
        await websocket.close(code=4004, reason="Unknown host")
        return

    auth_store: AuthStoreInterface = websocket.app.state.auth_store
    if not _is_authenticated(cookies=websocket.cookies, auth_store=auth_store):
        await websocket.close(code=4003, reason="Not authenticated")
        return

    backend_resolver: BackendResolverInterface = websocket.app.state.backend_resolver
    if agent_id not in backend_resolver.list_known_workspace_ids():
        await websocket.close(code=4004, reason=f"Unknown workspace: {agent_id}")
        return

    workspace_url = backend_resolver.get_backend_url(agent_id, WORKSPACE_SERVER_SERVICE_NAME)
    if workspace_url is None:
        await websocket.close(code=1013, reason="Workspace server not yet available")
        return

    try:
        tunnel_socket_path = await asyncio.get_running_loop().run_in_executor(
            None,
            _get_tunnel_socket_path,
            websocket.app.state.tunnel_manager,
            agent_id,
            workspace_url,
            backend_resolver,
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.debug("SSH tunnel setup failed for workspace WS {}: {}", agent_id, e)
        try:
            await websocket.close(code=1011, reason="SSH tunnel failed")
        except RuntimeError:
            pass
        return

    ws_backend = workspace_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    path = websocket.url.path.lstrip("/")
    ws_url = f"{ws_backend}/{path}" if path else ws_backend + "/"
    if websocket.url.query:
        ws_url = f"{ws_url}?{websocket.url.query}"

    client_subprotocol_header = websocket.headers.get("sec-websocket-protocol")
    subprotocols: list[str] = []
    if client_subprotocol_header:
        subprotocols = [s.strip() for s in client_subprotocol_header.split(",")]

    try:
        backend_ws_conn = _connect_backend_websocket(
            ws_url=ws_url, subprotocols=subprotocols, tunnel_socket_path=tunnel_socket_path
        )
        async with backend_ws_conn as backend_ws:
            await websocket.accept(subprotocol=backend_ws.subprotocol)
            await asyncio.gather(
                _forward_client_to_backend(client_websocket=websocket, backend_ws=backend_ws),
                _forward_backend_to_client(client_websocket=websocket, backend_ws=backend_ws, agent_id=agent_id),
            )
    except (ConnectionRefusedError, OSError, TimeoutError, SSHTunnelError, paramiko.SSHException) as connection_error:
        logger.debug("Backend WebSocket connection failed for workspace {}: {}", agent_id, connection_error)
        try:
            await websocket.close(code=1011, reason="Backend connection failed")
        except RuntimeError:
            pass


# -- Agent creation route handlers --


async def _handle_create_form_submit(request: Request, auth_store: AuthStoreDep) -> Response:
    """Handle form submission to create a new agent."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    form = await request.form()
    git_url = str(form.get("git_url", "")).strip()
    agent_name = str(form.get("agent_name", "")).strip()
    branch = str(form.get("branch", "")).strip()
    # HTML checkboxes submit their value only when checked; absence means unchecked.
    include_env_file = form.get("include_env_file") is not None
    try:
        launch_mode = LaunchMode(str(form.get("launch_mode", LaunchMode.LOCAL.value)))
    except ValueError:
        launch_mode = LaunchMode.LOCAL
    if not git_url:
        html = render_create_form(git_url="", agent_name=agent_name, branch=branch, launch_mode=launch_mode)
        return HTMLResponse(content=html, status_code=400)

    agent_id = agent_creator.start_creation(
        git_url,
        agent_name=agent_name,
        branch=branch,
        launch_mode=launch_mode,
        include_env_file=include_env_file,
    )
    return Response(status_code=303, headers={"Location": "/creating/{}".format(agent_id)})


def _handle_create_page(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Show the create form page (GET /create)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    git_url = request.query_params.get("git_url", "")
    branch = request.query_params.get("branch", "")
    html = render_create_form(git_url=git_url, branch=branch)
    return HTMLResponse(content=html)


async def _handle_create_agent_api(request: Request, auth_store: AuthStoreDep) -> Response:
    """API endpoint for creating an agent (POST /api/create-agent).

    Accepts JSON body with git_url. Returns JSON with agent_id and status.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response(
            status_code=400,
            content='{"error": "Invalid JSON body"}',
            media_type="application/json",
        )
    git_url = str(body.get("git_url", "")).strip()
    agent_name = str(body.get("agent_name", "")).strip()
    branch = str(body.get("branch", "")).strip()
    include_env_file = bool(body.get("include_env_file", False))
    try:
        launch_mode = LaunchMode(str(body.get("launch_mode", LaunchMode.LOCAL.value)))
    except ValueError:
        return Response(
            status_code=400,
            content='{"error": "Invalid launch_mode"}',
            media_type="application/json",
        )
    if not git_url:
        return Response(
            status_code=400,
            content='{"error": "git_url is required"}',
            media_type="application/json",
        )

    agent_id = agent_creator.start_creation(
        git_url,
        agent_name=agent_name,
        branch=branch,
        launch_mode=launch_mode,
        include_env_file=include_env_file,
    )
    return Response(
        content=json.dumps({"agent_id": str(agent_id), "status": "CLONING"}),
        media_type="application/json",
    )


def _handle_creation_status_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """API endpoint for checking agent creation status."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    parsed_id = AgentId(agent_id)
    info = agent_creator.get_creation_info(parsed_id)
    if info is None:
        return Response(
            status_code=404,
            content='{"error": "Unknown agent creation"}',
            media_type="application/json",
        )

    result = {"agent_id": str(info.agent_id), "status": str(info.status)}
    if info.redirect_url is not None:
        result["redirect_url"] = info.redirect_url
    if info.error is not None:
        result["error"] = info.error
    return Response(content=json.dumps(result), media_type="application/json")


def _handle_creating_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Show the creating progress page (GET /creating/{agent_id})."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    parsed_id = AgentId(agent_id)
    info = agent_creator.get_creation_info(parsed_id)
    if info is None:
        return Response(status_code=404, content="Unknown agent creation")

    if info.status == AgentCreationStatus.DONE and info.redirect_url is not None:
        return Response(status_code=307, headers={"Location": info.redirect_url})

    html = render_creating_page(agent_id=parsed_id, info=info)
    return HTMLResponse(content=html)


async def _stream_creation_logs(
    log_queue: queue.Queue[str],
    agent_creator: AgentCreator,
    agent_id: AgentId,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE events from a creation log queue."""
    streaming = True
    while streaming:
        try:
            line = await asyncio.get_running_loop().run_in_executor(None, log_queue.get, True, 1.0)
        except (queue.Empty, TimeoutError, OSError):
            yield ": keepalive\n\n"
            continue

        if line == LOG_SENTINEL:
            streaming = False
            info = agent_creator.get_creation_info(agent_id)
            if info is not None:
                result = {"status": str(info.status)}
                if info.redirect_url is not None:
                    result["redirect_url"] = info.redirect_url
                if info.error is not None:
                    result["error"] = info.error
                result["_type"] = "done"
                yield "data: {}\n\n".format(json.dumps(result))
                # Yield a final keepalive so the done event is flushed to the
                # browser in its own TCP segment, separate from the stream close.
                yield ": end\n\n"
        else:
            yield "data: {}\n\n".format(json.dumps({"log": line}))


async def _handle_creation_logs_sse(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """SSE endpoint that streams creation logs for an agent."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    parsed_id = AgentId(agent_id)
    log_queue = agent_creator.get_log_queue(parsed_id)
    if log_queue is None:
        return Response(status_code=404, content="Unknown agent creation")

    return StreamingResponse(
        _stream_creation_logs(log_queue, agent_creator, parsed_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Telegram setup route handlers --


async def _handle_telegram_setup(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Start Telegram bot setup for an agent (POST /api/agents/{agent_id}/telegram/setup)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return Response(
            status_code=501,
            content='{"error": "Telegram setup not configured"}',
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)

    # Use agent_id as the agent name for bot naming (best we have without additional lookups)
    agent_name = str(parsed_id)[:8]
    try:
        body = await request.json()
        agent_name = str(body.get("agent_name", agent_name)).strip() or agent_name
    except (json.JSONDecodeError, ValueError):
        pass

    telegram_orchestrator.start_setup(agent_id=parsed_id, agent_name=agent_name)
    return Response(
        content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.CHECKING_CREDENTIALS)}),
        media_type="application/json",
    )


def _handle_telegram_status(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Get Telegram setup status for an agent (GET /api/agents/{agent_id}/telegram/status)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return Response(
            status_code=501,
            content='{"error": "Telegram setup not configured"}',
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)
    info = telegram_orchestrator.get_setup_info(parsed_id)

    if info is None:
        # No active setup -- check if already set up
        is_active = telegram_orchestrator.agent_has_telegram(parsed_id)
        if is_active:
            return Response(
                content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.DONE)}),
                media_type="application/json",
            )
        return Response(
            status_code=404,
            content='{"error": "No Telegram setup in progress for this agent"}',
            media_type="application/json",
        )

    result: dict[str, str | None] = {
        "agent_id": str(info.agent_id),
        "status": str(info.status),
    }
    if info.error is not None:
        result["error"] = info.error
    if info.bot_username is not None:
        result["bot_username"] = info.bot_username
    return Response(content=json.dumps(result), media_type="application/json")


# -- Chrome (persistent shell) route handlers --


def _handle_chrome_page(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Serve the persistent chrome page (title bar + sidebar + content iframe).

    This route is unauthenticated -- the chrome renders for all users. The sidebar
    shows an empty state for unauthenticated users; the SSE stream populates it
    after authentication.
    """
    user_agent = request.headers.get("user-agent", "")
    is_mac = "Macintosh" in user_agent or "Mac OS" in user_agent

    authenticated = _is_authenticated(cookies=request.cookies, auth_store=auth_store)
    initial_workspaces = _build_workspace_list(backend_resolver) if authenticated else []

    html = render_chrome_page(
        is_mac=is_mac,
        is_authenticated=authenticated,
        initial_workspaces=initial_workspaces,
    )
    return HTMLResponse(content=html)


def _handle_chrome_sidebar(request: Request) -> Response:
    """Serve the standalone sidebar page for the Electron sidebar WebContentsView."""
    html = render_sidebar_page()
    return HTMLResponse(content=html)


async def _handle_chrome_events(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """SSE endpoint that streams workspace list and auth status changes to the chrome.

    The chrome subscribes to this on load. If unauthenticated, sends an auth_required
    event. Once authenticated, sends the current workspace list and pushes updates
    whenever the backend resolver's data changes (driven by MngrStreamManager's
    discovery and events streams).
    """
    authenticated = _is_authenticated(cookies=request.cookies, auth_store=auth_store)

    async def _event_generator() -> AsyncGenerator[str, None]:
        if not authenticated:
            yield "data: {}\n\n".format(json.dumps({"type": "auth_required"}))
            return

        # Use an asyncio.Event to wake up when the resolver's data changes.
        # The resolver fires callbacks from background threads, so we use
        # call_soon_threadsafe to signal the event on the event loop.
        change_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_change() -> None:
            loop.call_soon_threadsafe(change_event.set)

        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.add_on_change_callback(_on_change)

        try:
            # Send initial workspace list and request count
            session_store: MultiAccountSessionStore | None = request.app.state.session_store
            last_workspace_data = _build_workspace_list(backend_resolver, session_store)
            yield "data: {}\n\n".format(json.dumps({"type": "workspaces", "workspaces": last_workspace_data}))
            inbox: RequestInbox | None = request.app.state.request_inbox
            last_request_count = inbox.get_pending_count() if inbox else 0
            yield "data: {}\n\n".format(json.dumps({"type": "request_count", "count": last_request_count}))

            # Wait for changes and push updates until client disconnects
            connected = not await request.is_disconnected()
            while connected:
                # Wait for a change signal or timeout (timeout for disconnect checks)
                change_event.clear()
                try:
                    await asyncio.wait_for(change_event.wait(), timeout=30.0)
                except TimeoutError:
                    pass

                connected = not await request.is_disconnected()
                if not connected:
                    break

                current_data = _build_workspace_list(backend_resolver, session_store)
                if current_data != last_workspace_data:
                    last_workspace_data = current_data
                    yield "data: {}\n\n".format(json.dumps({"type": "workspaces", "workspaces": current_data}))

                inbox = request.app.state.request_inbox
                current_request_count = inbox.get_pending_count() if inbox else 0
                if current_request_count != last_request_count:
                    last_request_count = current_request_count
                    yield "data: {}\n\n".format(json.dumps({"type": "request_count", "count": current_request_count}))
        finally:
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.remove_on_change_callback(_on_change)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _build_workspace_list(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
) -> list[dict[str, str]]:
    """Build a JSON-serializable list of workspaces from the backend resolver.

    Each entry carries a deterministic "accent" CSS color derived from the
    agent id so the chrome and sidebar can render a per-workspace accent
    without running a digest in JS.
    """
    agent_ids = backend_resolver.list_known_workspace_ids()
    workspaces: list[dict[str, str]] = []
    for aid in agent_ids:
        ws_name = backend_resolver.get_workspace_name(aid)
        if not ws_name:
            info = backend_resolver.get_agent_display_info(aid)
            ws_name = info.agent_name if info else str(aid)
        entry: dict[str, str] = {"id": str(aid), "name": ws_name, "accent": workspace_accent(str(aid))}
        if session_store is not None:
            account = session_store.get_account_for_workspace(str(aid))
            if account is not None:
                entry["account"] = account.email
        workspaces.append(entry)
    return workspaces


# -- Account management routes --


def _handle_accounts_page(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Render the manage accounts page."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    minds_config: MindsConfig | None = request.app.state.minds_config
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    html = render_accounts_page(accounts=accounts, default_account_id=default_account_id)
    return HTMLResponse(content=html)


async def _handle_set_default_account(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Set the default account for new workspaces."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    form = await request.form()
    user_id = str(form.get("user_id", ""))
    minds_config: MindsConfig | None = request.app.state.minds_config
    if minds_config and user_id:
        minds_config.set_default_account_id(user_id)
    return Response(status_code=303, headers={"Location": "/accounts"})


async def _handle_account_logout(
    user_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Log out a specific account."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store:
        session_store.remove_session(user_id)
    return Response(status_code=303, headers={"Location": "/accounts"})


# -- Workspace settings routes --


def _handle_workspace_settings(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render workspace settings page with account, sharing, telegram, and delete options."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    current_account = session_store.get_account_for_workspace(agent_id) if session_store else None
    accounts = session_store.list_accounts() if session_store else []

    ws_name = backend_resolver.get_workspace_name(AgentId(agent_id))
    if not ws_name:
        info = backend_resolver.get_agent_display_info(AgentId(agent_id))
        ws_name = info.agent_name if info else agent_id

    servers = [str(s) for s in backend_resolver.list_services_for_agent(AgentId(agent_id))]

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    telegram_state: str | None = None
    if telegram_orchestrator is not None:
        telegram_state = "active" if telegram_orchestrator.agent_has_telegram(AgentId(agent_id)) else "pending"

    html = render_workspace_settings(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
    )
    return HTMLResponse(content=html)


async def _handle_workspace_associate(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Associate a workspace with an account."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    form = await request.form()
    user_id = str(form.get("user_id", ""))
    redirect_url = str(form.get("redirect", ""))
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store and user_id:
        session_store.associate_workspace(user_id, agent_id)
    location = redirect_url if redirect_url else f"/workspace/{agent_id}/settings"
    return Response(status_code=303, headers={"Location": location})


async def _handle_workspace_disassociate(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Disassociate a workspace from its account and tear down tunnels."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store:
        account = session_store.get_account_for_workspace(agent_id)
        if account:
            # Tear down Cloudflare tunnel
            cf_client, _ = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
            if cf_client is not None:
                try:
                    cf_client.delete_tunnel(AgentId(agent_id))
                except (httpx.HTTPError, ValueError, OSError) as e:
                    logger.warning("Failed to delete tunnel during disassociation: {}", e)
            session_store.disassociate_workspace(str(account.user_id), agent_id)
    return Response(status_code=303, headers={"Location": f"/workspace/{agent_id}/settings"})


# -- Requests panel routes --


def _handle_requests_panel(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Render the right-side requests inbox panel."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return HTMLResponse(content="<p>Not authenticated</p>")
    inbox: RequestInbox | None = request.app.state.request_inbox
    pending = inbox.get_pending_requests() if inbox else []
    minds_config: MindsConfig | None = request.app.state.minds_config
    auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True

    cards = []
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    for req in pending:
        service_name = req.service_name if isinstance(req, SharingRequestEvent) else ""
        parsed_id = AgentId(req.agent_id)
        ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
        if not ws_name:
            info = backend_resolver.get_agent_display_info(parsed_id)
            ws_name = info.agent_name if info else req.agent_id[:16]
        event_id = str(req.event_id)
        # Encode as JSON for safe embedding in the JS call, then HTML-escape
        # the result so it is also safe inside the double-quoted onclick
        # attribute. This is defense-in-depth: req.agent_id is validated as
        # an AgentId above, but req.event_id is only required to be a
        # non-empty string by its type, and relying on upstream validation
        # at each interpolation site is fragile.
        event_id_attr = html.escape(json.dumps(event_id), quote=True)
        agent_id_attr = html.escape(json.dumps(req.agent_id), quote=True)
        cards.append(
            f'<div class="req-card" onclick="navigateToRequest({event_id_attr}, {agent_id_attr})">'
            f'<div style="font-size:13px;color:#e2e8f0;font-weight:500;">sharing: {ws_name}</div>'
            f'<div style="font-size:12px;color:#64748b;margin-top:2px;">{service_name}</div></div>'
        )

    html_content = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Requests</title>'
        "<style>body{font-family:-apple-system,sans-serif;background:#0f172a;color:#cbd5e1;"
        "margin:0;padding:0;overflow-y:auto;height:100vh;}"
        "h2{font-size:15px;color:#e2e8f0;padding:12px;margin:0;border-bottom:1px solid #334155;}"
        ".req-card{padding:10px 12px;margin:2px 0;cursor:pointer;border-radius:6px;transition:background 100ms;}"
        ".req-card:hover{background:rgba(255,255,255,0.06);}"
        "</style></head>"
        f"<body>"
        f"<script>"
        f"function navigateToRequest(eventId, agentId) {{"
        f"  if (window.minds && window.minds.navigateToRequest) {{"
        f"    window.minds.navigateToRequest(agentId, eventId);"
        f"  }} else if (window.minds) {{"
        f'    window.minds.navigateContent("/requests/" + eventId);'
        f"  }} else {{"
        f'    window.top.location = "/requests/" + eventId;'
        f"  }}"
        f"}}"
        f"</script>"
        f"<h2>Requests ({len(pending)})</h2>"
        f"<div>{''.join(cards) if cards else '<p style=padding:12px;color:#64748b;>No pending requests.</p>'}</div>"
        f'<div style="position:fixed;bottom:0;left:0;right:0;padding:12px;border-top:1px solid #334155;'
        f'background:#0f172a;">'
        f'<label style="font-size:12px;color:#94a3b8;cursor:pointer;">'
        f'<input type="checkbox" {"checked" if auto_open else ""} '
        f"onchange=\"fetch('/_chrome/requests-auto-open',{{method:'POST',headers:{{'Content-Type':"
        f"'application/json'}},body:JSON.stringify({{enabled:this.checked}})}})\"> "
        f"Auto-open on new request</label></div>"
        "</body></html>"
    )
    return HTMLResponse(content=html_content)


async def _handle_requests_auto_open(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Toggle the auto-open setting for the requests panel."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    minds_config: MindsConfig | None = request.app.state.minds_config
    if minds_config:
        try:
            body = await request.json()
            enabled = body.get("enabled", True)
            minds_config.set_auto_open_requests_panel(bool(enabled))
        except (json.JSONDecodeError, ValueError):
            pass
    return Response(status_code=200, content='{"ok": true}', media_type="application/json")


def _resolve_ws_name_and_account(
    agent_id: str,
    request: Request,
    backend_resolver: BackendResolverInterface,
) -> tuple[str, str, bool, list[object]]:
    """Resolve workspace name, account email, has_account flag, and accounts list."""
    parsed_id = AgentId(agent_id)
    ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
    if not ws_name:
        info = backend_resolver.get_agent_display_info(parsed_id)
        ws_name = info.agent_name if info else agent_id
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    account = session_store.get_account_for_workspace(agent_id) if session_store else None
    account_email = account.email if account else ""
    has_account = account is not None
    accounts = session_store.list_accounts() if session_store else []
    return ws_name, account_email, has_account, accounts


def _handle_request_page(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render the request editing page using the shared sharing editor."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is None:
        return HTMLResponse(content="<p>Request inbox not available</p>", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return HTMLResponse(content="<p>Request not found</p>", status_code=404)

    is_sharing = isinstance(req_event, SharingRequestEvent)
    service_name = req_event.service_name if is_sharing else ""
    emails: list[str] = []
    if is_sharing:
        emails.extend(req_event.suggested_emails)
    emails = list(dict.fromkeys(emails))

    ws_name, account_email, has_account, accounts = _resolve_ws_name_and_account(
        req_event.agent_id,
        request,
        backend_resolver,
    )

    html = render_sharing_editor(
        agent_id=req_event.agent_id,
        service_name=service_name,
        title=f"Sharing Request: {service_name}",
        initial_emails=emails,
        is_request=True,
        request_id=request_id,
        has_account=has_account,
        accounts=accounts,
        redirect_url=f"/requests/{request_id}",
        ws_name=ws_name,
        account_email=account_email,
    )
    return HTMLResponse(content=html)


def _handle_sharing_page(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render the sharing editor page for direct editing (from workspace settings)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    ws_name, account_email, has_account, accounts = _resolve_ws_name_and_account(
        agent_id,
        request,
        backend_resolver,
    )

    html = render_sharing_editor(
        agent_id=agent_id,
        service_name=service_name,
        title=f"Sharing: {service_name}",
        is_request=False,
        has_account=has_account,
        accounts=accounts,
        redirect_url=f"/sharing/{agent_id}/{service_name}",
        ws_name=ws_name,
        account_email=account_email,
    )
    return HTMLResponse(content=html)


async def _handle_sharing_enable(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Enable or update sharing for a server. Handles both request approval and direct editing."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    form = await request.form()
    emails_json = str(form.get("emails", "[]"))
    try:
        emails = json.loads(emails_json)
    except json.JSONDecodeError:
        emails = []

    sharing_succeeded = False
    cf_client, error_response = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
    if cf_client is not None:
        parsed_id = AgentId(agent_id)
        parsed_service = ServiceName(service_name)
        backend_url = backend_resolver.get_backend_url(parsed_id, parsed_service)
        if backend_url:
            paths: WorkspacePaths = request.app.state.api_v1_paths
            stored_token = _load_tunnel_token(paths.data_dir, parsed_id)
            if stored_token is None:
                token, _ = cf_client.create_tunnel(parsed_id)
                if token:
                    _save_tunnel_token(paths.data_dir, parsed_id, token)
                    inject_tunnel_token_into_agent(parsed_id, token)
            cf_client.add_service(parsed_id, parsed_service, backend_url)
            sharing_succeeded = True
            # Apply auth rules if emails were provided
            if emails:
                rules: list[dict[str, object]] = [
                    {"action": "allow", "include": [{"email": {"email": e}} for e in emails]},
                ]
                cf_client.set_service_auth(parsed_id, str(parsed_service), rules)

    # If there's a pending request for this agent/server, mark it as granted only if sharing succeeded
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is not None and sharing_succeeded:
        for req in inbox.get_pending_requests():
            if isinstance(req, SharingRequestEvent) and req.agent_id == agent_id and req.service_name == service_name:
                paths = request.app.state.api_v1_paths
                response_event = create_request_response_event(
                    request_event_id=str(req.event_id),
                    status=RequestStatus.GRANTED,
                    agent_id=agent_id,
                    request_type=req.request_type,
                    service_name=service_name,
                )
                append_response_event(paths.data_dir, response_event)
                request.app.state.request_inbox = inbox.add_response(response_event)
                break

    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


async def _handle_sharing_disable(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Disable sharing for a server."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    cf_client, _ = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
    if cf_client is not None:
        cf_client.remove_service(AgentId(agent_id), service_name)

    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


def _handle_sharing_status_api(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """JSON API to get current sharing status for the editor JS."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")

    cf_client, error_response = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
    if error_response is not None:
        return Response(
            content=json.dumps({"enabled": False, "url": None, "auth_rules": []}),
            media_type="application/json",
        )
    if cf_client is None:
        return Response(
            content=json.dumps({"enabled": False, "url": None, "auth_rules": []}),
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)
    services = cf_client.list_services(parsed_id)
    if services is None:
        default_rules = cf_client.get_tunnel_auth(parsed_id) or []
        return Response(
            content=json.dumps({"enabled": False, "url": None, "auth_rules": default_rules}),
            media_type="application/json",
        )

    hostname = services.get(service_name)
    if hostname:
        auth_rules = cf_client.get_service_auth(parsed_id, service_name)
        if auth_rules is None:
            auth_rules = cf_client.get_tunnel_auth(parsed_id) or []
        return Response(
            content=json.dumps({"enabled": True, "url": f"https://{hostname}", "auth_rules": auth_rules}),
            media_type="application/json",
        )

    default_rules = cf_client.get_tunnel_auth(parsed_id) or []
    return Response(
        content=json.dumps({"enabled": False, "url": None, "auth_rules": default_rules}),
        media_type="application/json",
    )


async def _handle_request_grant(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Grant a request by redirecting to the sharing enable handler."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is None:
        return HTMLResponse(content="Request inbox not available", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return HTMLResponse(content="Request not found", status_code=404)

    if isinstance(req_event, SharingRequestEvent):
        return await _handle_sharing_enable(
            req_event.agent_id, req_event.service_name, request, auth_store, backend_resolver
        )

    return Response(status_code=303, headers={"Location": "/"})


async def _handle_request_deny(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Deny a request and write a response event."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is None:
        return HTMLResponse(content="Request inbox not available", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return HTMLResponse(content="Request not found", status_code=404)

    paths: WorkspacePaths = request.app.state.api_v1_paths
    response_event = create_request_response_event(
        request_event_id=request_id,
        status=RequestStatus.DENIED,
        agent_id=req_event.agent_id,
        request_type=req_event.request_type,
        service_name=req_event.service_name if isinstance(req_event, SharingRequestEvent) else None,
    )
    append_response_event(paths.data_dir, response_event)
    request.app.state.request_inbox = inbox.add_response(response_event)

    return Response(status_code=303, headers={"Location": "/"})


_request_event_apps: dict[int, FastAPI] = {}
_refresh_event_apps: dict[int, FastAPI] = {}


def _handle_request_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Process an incoming request event and add it to the app's inbox."""
    event = parse_request_event(raw_line)
    if event is None:
        return
    for app in _request_event_apps.values():
        current_inbox: RequestInbox | None = app.state.request_inbox
        if current_inbox is not None:
            app.state.request_inbox = current_inbox.add_request(event)
            logger.info("Request event from agent {}: {}", agent_id_str, event.request_type)


def _parse_refresh_service_name(raw_line: str) -> str | None:
    """Extract service_name from a refresh event line, or None if unparseable."""
    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    service_name = data.get("service_name")
    if not isinstance(service_name, str) or not service_name:
        return None
    return service_name


async def _dispatch_refresh_broadcast(app: FastAPI, agent_id: AgentId, service_name: str) -> None:
    """POST to the agent's workspace server so it emits a refresh_service WS broadcast.

    Resolves the ``system_interface`` backend URL for the agent (going through
    an SSH tunnel automatically for remote agents) and calls
    ``/api/refresh-service/{service_name}/broadcast``. Errors are logged but
    swallowed -- a missed refresh is never worth crashing on.
    """
    backend_resolver: BackendResolverInterface = app.state.backend_resolver
    backend_url = backend_resolver.get_backend_url(agent_id, WORKSPACE_SERVER_SERVICE_NAME)
    if backend_url is None:
        logger.debug(
            "No system_interface backend for agent {}; dropping refresh for service {}",
            agent_id,
            service_name,
        )
        return

    url = f"{backend_url.rstrip('/')}/api/refresh-service/{service_name}/broadcast"
    # Tunnel setup performs a blocking SSH handshake for remote agents, so
    # run it in a thread pool to avoid stalling the desktop client's event
    # loop (mirrors the approach used by the HTTP proxy path).
    try:
        tunnel_client = await asyncio.get_running_loop().run_in_executor(
            None, _get_tunnel_http_client, app, agent_id, backend_url, backend_resolver
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.warning("Refresh broadcast tunnel setup for {} failed: {}", url, e)
        return
    http_client = tunnel_client or app.state.http_client
    try:
        response = await http_client.post(url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Refresh broadcast POST to {} failed: {}", url, e)
    finally:
        if tunnel_client is not None:
            await tunnel_client.aclose()


def _log_refresh_dispatch_result(
    future: concurrent.futures.Future[None], agent_id_str: str, service_name: str
) -> None:
    """Surface any exception stashed on a scheduled refresh-dispatch future.

    ``run_coroutine_threadsafe`` stores exceptions on the returned
    ``concurrent.futures.Future``; if nothing calls ``.exception()`` they are
    never logged. This callback runs when the coroutine finishes and logs
    anything other than cancellation.
    """
    try:
        exc = future.exception()
    except asyncio.CancelledError:
        logger.debug("Refresh dispatch cancelled for agent {} service {}", agent_id_str, service_name)
        return
    if exc is not None:
        logger.warning("Refresh dispatch failed for agent {} service {}: {}", agent_id_str, service_name, exc)


def _handle_refresh_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Fan a refresh event out to every registered app's workspace server.

    Runs on the mngr-events reader thread, so the async POST is scheduled
    on each app's captured event loop via run_coroutine_threadsafe.
    """
    service_name = _parse_refresh_service_name(raw_line)
    if service_name is None:
        logger.debug("Ignoring malformed refresh event from {}: {}", agent_id_str, raw_line[:200])
        return
    agent_id = AgentId(agent_id_str)
    for app in _refresh_event_apps.values():
        # event_loop is set to None in create_desktop_client and populated by
        # _managed_lifespan on startup. In production, stream_manager.start()
        # (which feeds this callback) runs before uvicorn.run(app) starts the
        # lifespan, so there is a brief window during which refresh events
        # can arrive before the loop is captured. Drop such events rather
        # than crashing the reader thread with AttributeError. The same guard
        # also covers loops that have already been closed (e.g. the app was
        # torn down but its entry in _refresh_event_apps has not yet been
        # removed) -- scheduling on a closed loop would raise RuntimeError
        # and leak an unawaited coroutine.
        loop: asyncio.AbstractEventLoop | None = app.state.event_loop
        if loop is None or loop.is_closed():
            logger.debug(
                "Dropping refresh for agent {} service {}: app event loop unavailable",
                agent_id_str,
                service_name,
            )
            continue
        future = asyncio.run_coroutine_threadsafe(_dispatch_refresh_broadcast(app, agent_id, service_name), loop)
        future.add_done_callback(lambda f, aid=agent_id_str, sn=service_name: _log_refresh_dispatch_result(f, aid, sn))
        logger.info("Scheduled refresh broadcast for agent {} service {}", agent_id_str, service_name)


# -- App factory --


def create_desktop_client(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    tunnel_manager: SSHTunnelManager | None = None,
    latchkey_gateway_manager: LatchkeyGatewayManager | None = None,
    agent_creator: AgentCreator | None = None,
    cloudflare_client: CloudflareClient | None = None,
    telegram_orchestrator: TelegramSetupOrchestrator | None = None,
    notification_dispatcher: NotificationDispatcher | None = None,
    paths: WorkspacePaths | None = None,
    minds_config: MindsConfig | None = None,
    stream_manager: MngrStreamManager | None = None,
    session_store: MultiAccountSessionStore | None = None,
    auth_backend_client: AuthBackendClient | None = None,
    request_inbox: RequestInbox | None = None,
    server_port: int = 0,
    output_format: OutputFormat | None = None,
) -> FastAPI:
    """Create the desktop client FastAPI application.

    When tunnel_manager is provided, the server can proxy traffic to remote agents
    by tunneling through SSH. Without it, only local agents are reachable.

    When agent_creator is provided, the server can create new agents from git URLs
    via the /create form and /api/create-agent API.

    When cloudflare_client is provided, the servers page shows global forwarding
    URLs and toggle controls.

    When telegram_orchestrator is provided, the landing page shows Telegram setup
    buttons and the /api/agents/{agent_id}/telegram/* endpoints are available.

    When paths is provided, the /api/v1/ REST API router is mounted with API
    key authentication. The notification endpoint within the router additionally
    requires notification_dispatcher to be provided; without it that endpoint
    returns 501.
    """
    is_externally_managed_client = http_client is not None

    @asynccontextmanager
    async def _lifespan(inner_app: FastAPI) -> AsyncGenerator[None, None]:
        async with _managed_lifespan(inner_app=inner_app, is_externally_managed_client=is_externally_managed_client):
            yield

    app = FastAPI(lifespan=_lifespan)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        logger.error("Unhandled exception on {} {}: {}", request.method, request.url.path, exc, exc_info=exc)
        return Response(status_code=500, content=f"Internal Server Error: {exc}")

    @app.middleware("http")
    async def _subdomain_forwarding_middleware(request: Request, call_next: Any) -> Response:
        """Dispatch ``<agent-id>.localhost:PORT/*`` to the workspace_server byte-forward.

        Bare ``localhost`` / ``127.0.0.1`` traffic falls through to the normal
        desktop-client routes via ``call_next``. Unknown subdomains return 404.
        """
        host_header = request.headers.get("host", "")
        agent_id = _parse_workspace_subdomain(host_header)
        if agent_id is None:
            return await call_next(request)
        return await _handle_workspace_forward_http(request)

    app.state.auth_store = auth_store
    app.state.backend_resolver = backend_resolver
    app.state.tunnel_manager = tunnel_manager
    app.state.latchkey_gateway_manager = latchkey_gateway_manager
    app.state.stream_manager = stream_manager
    app.state.agent_creator = agent_creator
    app.state.cloudflare_client = cloudflare_client
    app.state.telegram_orchestrator = telegram_orchestrator
    app.state.notification_dispatcher = notification_dispatcher
    app.state.session_store = session_store
    app.state.auth_backend_client = auth_backend_client
    app.state.minds_config = minds_config
    app.state.request_inbox = request_inbox
    app.state.auth_server_port = server_port
    app.state.auth_output_format = output_format or OutputFormat.JSONL
    # Populated with the running loop by _managed_lifespan on startup. Defined
    # up-front as None so background callbacks fired before startup (e.g. mngr
    # events produced between stream_manager.start() and uvicorn.run()) see a
    # valid attribute and can choose to drop the event instead of crashing.
    app.state.event_loop = None
    if paths is not None:
        app.state.api_v1_paths = paths
    if http_client is not None:
        app.state.http_client = http_client

    # Register callback to process incoming request events from agents
    if isinstance(backend_resolver, MngrCliBackendResolver):
        _request_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_request_callback(_handle_request_event_callback)
        _refresh_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_refresh_callback(_handle_refresh_event_callback)

    # Mount the auth routes (proxy to the remote_service_connector auth backend)
    if session_store is not None and auth_backend_client is not None:
        supertokens_router = create_supertokens_router(
            session_store=session_store,
            auth_backend_client=auth_backend_client,
            server_port=server_port,
            output_format=output_format or OutputFormat.JSONL,
        )
        app.include_router(supertokens_router)

    # Mount the REST API v1 router
    if paths is not None:
        api_v1_router = create_api_v1_router()
        app.include_router(api_v1_router, prefix="/api/v1")

    # Static assets: Tailwind Play CDN JS + hand-written tokens.css +
    # per-page JS. The Tailwind JS is fetched once by `just minds-tailwind`
    # (plain curl, no build step) and is gitignored; if it's missing, the
    # mount still works and the server logs a hint at startup.
    _static_dir = Path(__file__).resolve().parent / "static"
    if not (_static_dir / "tailwind.js").exists():
        logger.warning("Missing static/tailwind.js. Run `just minds-tailwind` from the repo root to fetch it.")
    app.mount("/_static", StaticFiles(directory=str(_static_dir)), name="static")

    # Chrome (persistent shell) routes
    app.get("/_chrome")(_handle_chrome_page)
    app.get("/_chrome/sidebar")(_handle_chrome_sidebar)
    app.get("/_chrome/events")(_handle_chrome_events)

    # Register routes
    app.get("/login")(_handle_login)
    app.get("/authenticate")(_handle_authenticate)
    app.get("/")(_handle_landing_page)

    # Auth bridge: same-origin redirect to a workspace subdomain that
    # installs a subdomain-scoped session cookie on first visit.
    app.get("/goto/{agent_id}/")(_handle_goto_workspace)

    # Account management routes
    app.get("/accounts")(_handle_accounts_page)
    app.post("/accounts/set-default")(_handle_set_default_account)
    app.post("/accounts/{user_id}/logout")(_handle_account_logout)

    # Workspace settings routes
    app.get("/workspace/{agent_id}/settings")(_handle_workspace_settings)
    app.post("/workspace/{agent_id}/associate")(_handle_workspace_associate)
    app.post("/workspace/{agent_id}/disassociate")(_handle_workspace_disassociate)

    # Request inbox routes
    app.get("/_chrome/requests-panel")(_handle_requests_panel)
    app.post("/_chrome/requests-auto-open")(_handle_requests_auto_open)
    app.get("/requests/{request_id}")(_handle_request_page)
    app.post("/requests/{request_id}/grant")(_handle_request_grant)
    app.post("/requests/{request_id}/deny")(_handle_request_deny)

    # Sharing editor routes (used by both request approval and direct editing)
    app.get("/sharing/{agent_id}/{service_name}")(_handle_sharing_page)
    app.post("/sharing/{agent_id}/{service_name}/enable")(_handle_sharing_enable)
    app.post("/sharing/{agent_id}/{service_name}/disable")(_handle_sharing_disable)
    app.get("/api/sharing-status/{agent_id}/{service_name}")(_handle_sharing_status_api)

    # Agent creation routes
    app.get("/create")(_handle_create_page)
    app.post("/create")(_handle_create_form_submit)
    app.post("/api/create-agent")(_handle_create_agent_api)
    app.get("/api/create-agent/{agent_id}/status")(_handle_creation_status_api)
    app.get("/api/create-agent/{agent_id}/logs")(_handle_creation_logs_sse)
    app.get("/creating/{agent_id}")(_handle_creating_page)

    # Telegram setup routes
    app.post("/api/agents/{agent_id}/telegram/setup")(_handle_telegram_setup)
    app.get("/api/agents/{agent_id}/telegram/status")(_handle_telegram_status)

    # Catch-all WebSocket route for ``<agent-id>.localhost:PORT/*``. For
    # requests arriving on the bare-origin host, the handler closes the WS
    # with a 4004 since those paths aren't routed by any other handler.
    @app.websocket("/{path:path}")
    async def subdomain_forwarding_websocket(websocket: WebSocket, path: str) -> None:
        host_header = websocket.headers.get("host", "")
        if _parse_workspace_subdomain(host_header) is None:
            await websocket.close(code=4004, reason="Not found")
            return
        await _handle_workspace_forward_websocket(websocket)

    return app
