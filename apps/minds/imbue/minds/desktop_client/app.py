import asyncio
import json
import os
import queue
import shutil
import socket as socket_module
from collections.abc import AsyncGenerator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from typing import Final

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
from loguru import logger
from websockets import ClientConnection

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.api_v1 import create_api_v1_router
from imbue.minds.desktop_client.api_v1 import get_cf_client_with_auth
from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import MngrStreamManager
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.deps import BackendResolverDep
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.proxy import generate_backend_loading_html
from imbue.minds.desktop_client.proxy import generate_bootstrap_html
from imbue.minds.desktop_client.proxy import generate_service_worker_js
from imbue.minds.desktop_client.proxy import rewrite_cookie_path
from imbue.minds.desktop_client.proxy import rewrite_proxied_html
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
from imbue.minds.desktop_client.templates import render_agent_servers_page
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
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token as _load_tunnel_token
from imbue.minds.desktop_client.tunnel_token_store import save_tunnel_token as _save_tunnel_token
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServerName
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0


def _split_backend_url(backend_url: str) -> tuple[str, str]:
    """Split a backend URL into base URL and stored query string.

    Backend URLs may contain query parameters from URL-arg dispatch
    (e.g. ``http://127.0.0.1:PORT?arg=chat``). This function separates
    the base URL from the stored query so the proxy can correctly
    construct the final URL by combining stored and request query parts.
    """
    if "?" in backend_url:
        base, query = backend_url.split("?", 1)
        return base, query
    return backend_url, ""


def _build_proxy_url(base_url: str, path: str, *query_parts: str) -> str:
    """Build a proxy URL from base URL, path, and optional query string parts.

    Combines the base URL with the path, then appends any non-empty
    query parts joined by ``&``.
    """
    url = f"{base_url}/{path}"
    non_empty = [p for p in query_parts if p]
    if non_empty:
        url += f"?{'&'.join(non_empty)}"
    return url


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
    try:
        yield
    finally:
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
    auth_store: AuthStoreDep,
) -> Response:
    code = OneTimeCode(one_time_code)

    is_valid = auth_store.validate_and_consume_code(code=code)

    if not is_valid:
        html = render_auth_error_page(message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)

    # Set global session cookie
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


_DEFAULT_SERVER_PREFERENCE: tuple[str, ...] = ("system_interface", "web")


def _handle_agent_default_redirect(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Redirect to the agent's canonical primary server.

    Picks the first registered server name that matches our preference list
    ("system_interface" -- legacy local/docker templates, or "web" -- current
    forever-claude-template). Falls back to any registered server if neither
    is present, and to the servers-listing page if the agent has none yet.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    parsed_id = AgentId(agent_id)
    known_servers = backend_resolver.list_servers_for_agent(parsed_id)
    known_server_names = {str(s) for s in known_servers}

    preferred: str | None = None
    for candidate in _DEFAULT_SERVER_PREFERENCE:
        if candidate in known_server_names:
            preferred = candidate
            break
    if preferred is None and known_servers:
        preferred = str(known_servers[0])

    if preferred is None:
        # Nothing registered yet; send the user to the servers-listing page
        # which itself renders a loading state until discovery catches up.
        return Response(status_code=307, headers={"Location": f"/forwarding/{agent_id}/servers/"})

    return Response(status_code=307, headers={"Location": f"/forwarding/{agent_id}/{preferred}/"})


async def _handle_agent_servers_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Show a listing of all available servers for a given agent."""
    parsed_id = AgentId(agent_id)

    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    server_names = backend_resolver.list_servers_for_agent(parsed_id)

    cf_client: CloudflareForwardingClient | None = request.app.state.cloudflare_client
    cf_services: dict[str, str] | None = None
    if cf_client is not None:
        cf_services = await asyncio.get_running_loop().run_in_executor(None, cf_client.list_services, parsed_id)

    html = render_agent_servers_page(agent_id=parsed_id, server_names=server_names, cf_services=cf_services)
    return HTMLResponse(content=html)


async def _handle_toggle_global(
    agent_id: str,
    server_name: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Toggle global cloudflare forwarding for a specific server on an agent."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    cf_client, error_response = get_cf_client_with_auth(request)
    if error_response is not None:
        return error_response
    assert cf_client is not None

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response(status_code=400, content='{"error": "Invalid JSON"}', media_type="application/json")

    enabled = body.get("enabled", True)
    parsed_id = AgentId(agent_id)

    loop = asyncio.get_running_loop()
    if enabled:
        parsed_server = ServerName(server_name)
        backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
        if backend_url is None:
            return Response(
                status_code=404,
                content='{"error": "Server not found locally"}',
                media_type="application/json",
            )
        success = await loop.run_in_executor(None, cf_client.add_service, parsed_id, server_name, backend_url)
    else:
        success = await loop.run_in_executor(None, cf_client.remove_service, parsed_id, server_name)

    if success:
        return Response(content='{"ok": true}', media_type="application/json")
    return Response(
        status_code=502,
        content='{"error": "Cloudflare API call failed"}',
        media_type="application/json",
    )


async def _forward_http_request(
    request: Request,
    backend_url: str,
    path: str,
    agent_id: str,
    server_name: str,
    http_client: httpx.AsyncClient | None,
) -> httpx.Response | Response:
    """Forward an HTTP request to the backend, returning the backend response or an error Response.

    When http_client is not None, uses it instead of the app's default client. This is
    used for SSH-tunneled connections where the client is configured with UDS transport.
    """
    base_url, stored_query = _split_backend_url(backend_url)
    proxy_url = _build_proxy_url(base_url, path, stored_query, request.url.query)

    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    active_http_client = http_client or request.app.state.http_client
    try:
        return await active_http_client.request(
            method=request.method,
            url=proxy_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.warning("Backend connection refused for {} server {}", agent_id, server_name)
        return Response(status_code=502, content="Backend connection refused")
    except httpx.ReadError:
        logger.warning("Backend connection lost for {} server {}", agent_id, server_name)
        return Response(status_code=502, content="Backend connection lost")
    except httpx.RemoteProtocolError:
        # Raised when the SSH tunnel accepts the connection but closes it without
        # sending a response, which happens when the SSH channel-open to the
        # backend port fails (e.g. uvicorn inside the agent container hasn't
        # finished binding to its port yet). Returning 502 lets the caller show
        # the auto-retrying loading page for HTML requests.
        logger.warning(
            "Backend disconnected without response for {} server {} (likely still starting up)", agent_id, server_name
        )
        return Response(status_code=502, content="Backend disconnected without response")
    except httpx.TimeoutException:
        logger.warning("Backend request timed out for {} server {}", agent_id, server_name)
        return Response(status_code=504, content="Backend request timed out")


async def _forward_http_request_streaming(
    request: Request,
    backend_url: str,
    path: str,
    agent_id: str,
    server_name: str,
    http_client: httpx.AsyncClient | None,
) -> Response:
    """Forward an HTTP request and stream the response back without buffering.

    Used for SSE (Server-Sent Events) endpoints where the backend sends data
    incrementally and the client needs to receive it as it arrives.
    """
    base_url, stored_query = _split_backend_url(backend_url)
    proxy_url = _build_proxy_url(base_url, path, stored_query, request.url.query)

    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    active_http_client = http_client or request.app.state.http_client

    try:
        backend_stream = active_http_client.stream(
            method=request.method,
            url=proxy_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.debug("Backend connection refused for {} server {} (streaming)", agent_id, server_name)
        return Response(status_code=502, content="Backend connection refused")

    async def _stream_generator() -> AsyncGenerator[bytes, None]:
        try:
            async with backend_stream as response:
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.ConnectError:
            logger.debug("Backend connection lost during streaming for {} server {}", agent_id, server_name)
        except httpx.ReadError:
            logger.debug("Backend read error during streaming for {} server {}", agent_id, server_name)
        except httpx.RemoteProtocolError:
            logger.debug(
                "Backend disconnected without response during streaming for {} server {}", agent_id, server_name
            )
        except httpx.TimeoutException:
            logger.debug("Backend stream timed out for {} server {}", agent_id, server_name)

    return StreamingResponse(
        _stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _build_proxy_response(
    backend_response: httpx.Response,
    agent_id: AgentId,
    server_name: ServerName,
) -> Response:
    """Transform a backend httpx response into a FastAPI Response with header/content rewriting."""
    # Build response headers, dropping hop-by-hop headers
    resp_headers: dict[str, list[str]] = {}
    for header_key, header_value in backend_response.headers.multi_items():
        if header_key.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        if header_key.lower() == "set-cookie":
            header_value = rewrite_cookie_path(
                set_cookie_header=header_value,
                agent_id=agent_id,
                server_name=server_name,
            )
        resp_headers.setdefault(header_key, [])
        resp_headers[header_key].append(header_value)

    content: str | bytes = backend_response.content

    # Rewrite HTML responses (absolute paths, base tag, WS shim)
    content_type = backend_response.headers.get("content-type", "")
    if "text/html" in content_type:
        html_text = backend_response.text
        rewritten_html = rewrite_proxied_html(
            html_content=html_text,
            agent_id=agent_id,
            server_name=server_name,
        )
        content = rewritten_html.encode()

    response = Response(content=content, status_code=backend_response.status_code)
    for header_key, header_values in resp_headers.items():
        for header_value in header_values:
            response.headers.append(header_key, header_value)
    return response


def _make_loading_html(
    agent_id: AgentId,
    server_name: ServerName,
    backend_resolver: BackendResolverInterface,
) -> str:
    """Build loading-page HTML with fallback links to other available servers."""
    other_servers = tuple(s for s in backend_resolver.list_servers_for_agent(agent_id) if s != server_name)
    return generate_backend_loading_html(
        agent_id=agent_id,
        current_server=server_name,
        other_servers=other_servers,
    )


async def _handle_proxy_http(
    agent_id: str,
    server_name: str,
    path: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    parsed_id = AgentId(agent_id)
    parsed_server = ServerName(server_name)

    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    # Serve the service worker script
    if path == "__sw.js":
        return Response(
            content=generate_service_worker_js(parsed_id, parsed_server),
            media_type="application/javascript",
        )

    is_navigation = request.headers.get("sec-fetch-mode") == "navigate"

    backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
    if backend_url is None:
        # Diagnostics: nothing in the logs today tells us *why* we fell into
        # the loading page. Surface which agent/server missed and what the
        # resolver actually knows about.
        known_servers = backend_resolver.list_servers_for_agent(parsed_id)
        logger.warning(
            "Proxy loading-page fallback: agent_id={} server={} not resolvable. known_servers={} path={}",
            agent_id,
            server_name,
            known_servers,
            path,
        )
        # Return immediately instead of holding the connection open.
        # For HTML-accepting requests, return a loading page that retries
        # client-side after a short delay. This avoids saturating the
        # browser's per-origin connection pool (typically 6 for HTTP/1.1)
        # when a stale tab is pointed at an unavailable backend.
        request_accept = request.headers.get("accept", "")
        if "text/html" in request_accept:
            return HTMLResponse(content=_make_loading_html(parsed_id, parsed_server, backend_resolver))
        return Response(
            status_code=502,
            content="Backend unavailable for agent {}, server {}".format(agent_id, server_name),
        )

    assert backend_url is not None
    resolved_backend_url = backend_url

    # Check if SW is installed via cookie (scoped per server)
    sw_cookie = request.cookies.get(f"sw_installed_{agent_id}_{server_name}")

    # First HTML navigation without SW -> serve bootstrap
    if is_navigation and not sw_cookie:
        return HTMLResponse(generate_bootstrap_html(parsed_id, parsed_server))

    # Determine if this backend needs SSH tunneling (run in executor to avoid blocking event loop
    # during SSH handshake which can take several seconds)
    try:
        tunnel_client = await asyncio.get_running_loop().run_in_executor(
            None, _get_tunnel_http_client, request.app, parsed_id, resolved_backend_url, backend_resolver
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.warning("SSH tunnel setup failed for {} server {}: {}", agent_id, server_name, e)
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(content=_make_loading_html(parsed_id, parsed_server, backend_resolver))
        return Response(status_code=502, content=f"SSH tunnel to remote backend failed: {e}")

    # Check if this request expects a streaming response (SSE).
    # If Accept includes text/event-stream, use streaming proxy to avoid
    # buffering the entire response before forwarding.
    accept_header = request.headers.get("accept", "")
    is_likely_sse = "text/event-stream" in accept_header or (request.method == "POST" and "api/chat/send" in path)

    if is_likely_sse:
        return await _forward_http_request_streaming(
            request=request,
            backend_url=resolved_backend_url,
            path=path,
            agent_id=agent_id,
            server_name=server_name,
            http_client=tunnel_client,
        )

    # Forward request to backend
    result = await _forward_http_request(
        request=request,
        backend_url=resolved_backend_url,
        path=path,
        agent_id=agent_id,
        server_name=server_name,
        http_client=tunnel_client,
    )

    # If forwarding returned an error Response (e.g. backend not ready yet),
    # show the auto-retrying loading page for HTML requests instead of a
    # dead-end 502 that requires manual reload.
    if isinstance(result, Response):
        if result.status_code >= 500 and "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(content=_make_loading_html(parsed_id, parsed_server, backend_resolver))
        return result

    return _build_proxy_response(
        backend_response=result,
        agent_id=parsed_id,
        server_name=parsed_server,
    )


async def _handle_proxy_websocket(
    websocket: WebSocket,
    agent_id: str,
    server_name: str,
    path: str,
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    tunnel_manager: SSHTunnelManager | None,
) -> None:
    parsed_id = AgentId(agent_id)
    parsed_server = ServerName(server_name)

    if not _is_authenticated(cookies=websocket.cookies, auth_store=auth_store):
        await websocket.close(code=4003, reason="Not authenticated")
        return

    backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
    if backend_url is None:
        await websocket.close(code=4004, reason=f"Unknown server: {agent_id}/{server_name}")
        return

    base_url, stored_query = _split_backend_url(backend_url)
    ws_backend = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = _build_proxy_url(ws_backend, path, stored_query, websocket.url.query)

    # Forward subprotocols from the client to the backend so that
    # protocol-specific servers (e.g. ttyd which requires "tty") work correctly.
    client_subprotocol_header = websocket.headers.get("sec-websocket-protocol")
    subprotocols: list[str] = []
    if client_subprotocol_header:
        subprotocols = [s.strip() for s in client_subprotocol_header.split(",")]

    # Check if this backend needs SSH tunneling (run in executor to avoid blocking event loop)
    try:
        tunnel_socket_path = await asyncio.get_running_loop().run_in_executor(
            None, _get_tunnel_socket_path, tunnel_manager, parsed_id, backend_url, backend_resolver
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.debug("SSH tunnel setup failed for WS {}/{}: {}", agent_id, server_name, e)
        try:
            await websocket.close(code=1011, reason="SSH tunnel to remote backend failed")
        except RuntimeError:
            logger.trace("WebSocket already closed when trying to send tunnel error for {}", agent_id)
        return

    try:
        backend_ws_conn = _connect_backend_websocket(
            ws_url=ws_url,
            subprotocols=subprotocols,
            tunnel_socket_path=tunnel_socket_path,
        )
        async with backend_ws_conn as backend_ws:
            # Accept the client connection with the subprotocol the backend agreed on
            await websocket.accept(subprotocol=backend_ws.subprotocol)

            await asyncio.gather(
                _forward_client_to_backend(
                    client_websocket=websocket,
                    backend_ws=backend_ws,
                ),
                _forward_backend_to_client(
                    client_websocket=websocket,
                    backend_ws=backend_ws,
                    agent_id=parsed_id,
                ),
            )

    except (ConnectionRefusedError, OSError, TimeoutError, SSHTunnelError, paramiko.SSHException) as connection_error:
        logger.debug(
            "Backend WebSocket connection failed for {}/{}: {}",
            agent_id,
            server_name,
            connection_error,
        )
        try:
            await websocket.close(code=1011, reason="Backend connection failed")
        except RuntimeError:
            logger.trace("WebSocket already closed when trying to send error for {}", agent_id)


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

    # Block creating an agent with a name that's already taken. We previously
    # let the underlying `mngr create --reuse --update` path handle this,
    # which appeared to "work" (the agent got reused) but broke on the
    # git-mirror push -- mirror push with --prune wants to delete all
    # non-matching refs, including the currently-checked-out branch on the
    # existing agent's bare repo, which git refuses. The user-visible symptom
    # was a cryptic "refusing to delete the current branch" failure rather
    # than "name is taken, pick a different one". Fail fast at the API layer
    # instead.
    if agent_name:
        backend_resolver = request.app.state.backend_resolver
        existing_names: set[str] = set()
        for existing_id in backend_resolver.list_known_workspace_ids():
            existing_name = backend_resolver.get_workspace_name(existing_id)
            if existing_name is not None:
                existing_names.add(existing_name)
        if agent_name in existing_names:
            return Response(
                status_code=409,
                content=json.dumps(
                    {
                        "error": (
                            "An agent named '{}' already exists. "
                            "Pick a different name, or destroy the existing one first."
                        ).format(agent_name)
                    }
                ),
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
    """Build a JSON-serializable list of workspaces from the backend resolver."""
    agent_ids = backend_resolver.list_known_workspace_ids()
    workspaces: list[dict[str, str]] = []
    for aid in agent_ids:
        ws_name = backend_resolver.get_workspace_name(aid)
        if not ws_name:
            info = backend_resolver.get_agent_display_info(aid)
            ws_name = info.agent_name if info else str(aid)
        entry: dict[str, str] = {"id": str(aid), "name": ws_name}
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

    servers = [str(s) for s in backend_resolver.list_servers_for_agent(AgentId(agent_id))]

    # Telegram section
    telegram_section = ""
    telegram_js = ""
    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is not None:
        has_telegram = telegram_orchestrator.agent_has_telegram(AgentId(agent_id))
        if has_telegram:
            telegram_section = '<p style="color:#16a34a;">Telegram is active for this workspace.</p>'
        else:
            telegram_section = (
                f'<button class="btn btn-primary" id="tg-btn" '
                f"onclick=\"setupTelegram('{agent_id}')\">Setup Telegram</button>"
            )
            telegram_js = (
                "async function setupTelegram(agentId) {"
                '  var btn = document.getElementById("tg-btn");'
                '  btn.disabled = true; btn.textContent = "Setting up...";'
                "  try {"
                '    var resp = await fetch("/api/agents/" + agentId + "/telegram/setup", {method: "POST"});'
                '    if (!resp.ok) { var data = await resp.json(); alert("Failed: " + (data.error || resp.statusText));'
                '      btn.disabled = false; btn.textContent = "Setup Telegram"; return; }'
                "    var interval = setInterval(async function() {"
                '      try { var r = await fetch("/api/agents/" + agentId + "/telegram/status");'
                "        if (!r.ok) return; var d = await r.json();"
                '        if (d.status === "DONE") { clearInterval(interval);'
                '          btn.textContent = "Telegram active"; btn.style.color = "#16a34a"; }'
                '        else if (d.status === "FAILED") { clearInterval(interval);'
                '          btn.textContent = "Setup failed"; btn.disabled = false; }'
                "        else { btn.textContent = d.status; }"
                "      } catch (e) {}"
                "    }, 2000);"
                '  } catch (e) { alert("Failed: " + e.message); btn.disabled = false; btn.textContent = "Setup Telegram"; }'
                "}"
            )

    html = render_workspace_settings(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_section=telegram_section,
        telegram_js=telegram_js,
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
    _disassociate_and_teardown_cf(request, agent_id)
    return Response(status_code=303, headers={"Location": f"/workspace/{agent_id}/settings"})


def _disassociate_and_teardown_cf(request: Request, agent_id: str) -> None:
    """Tear down the Cloudflare tunnel for a workspace and detach it from its
    supertokens account if associated. Shared by /disassociate and /delete.

    Best-effort: any failure here is logged, not raised -- the caller wants to
    proceed regardless.
    """
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store is None:
        return
    account = session_store.get_account_for_workspace(agent_id)
    if account is None:
        return
    cf_client, _ = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
    if cf_client is not None:
        try:
            cf_client.delete_tunnel(AgentId(agent_id))
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.warning("Failed to delete CF tunnel for {}: {}", agent_id, e)
    try:
        session_store.disassociate_workspace(str(account.user_id), agent_id)
    except (OSError, ValueError) as e:
        logger.warning("Failed to disassociate workspace {}: {}", agent_id, e)


async def _handle_workspace_delete(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Destroy a workspace: run `mngr destroy`, remove local state, tear down
    tunnels and account association.

    Returns JSON {"status": "DELETED", "redirect_url": "/"} on success, or
    {"error": "..."} with a 4xx/5xx on failure. This is async-ish: the mngr
    subprocess can take 10-30s for Lima VMs, so the caller UI should show a
    spinner while this runs.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(
            status_code=403, content='{"error": "Not authenticated"}', media_type="application/json"
        )

    # 1. Disassociate + tear down CF tunnel first so the UI stops trying to
    #    route to an agent we're about to kill.
    _disassociate_and_teardown_cf(request, agent_id)

    # 2. Shell out to `mngr destroy` for the real VM / host / git-branch
    #    cleanup. --force skips confirmation prompts and stops running
    #    agents (the user just clicked the Danger Zone button). --gc cleans
    #    up any orphaned resources the agent was holding.
    #    --remove-created-branch deletes the mngr/<name> branch the agent
    #    was working on so the source template's git repo doesn't
    #    accumulate branches.
    destroy_cmd = [
        MNGR_BINARY,
        "destroy",
        agent_id,
        "--force",
        "--gc",
        "--remove-created-branch",
    ]
    logger.info("Running: {}", " ".join(destroy_cmd))
    cg = ConcurrencyGroup(name="workspace-delete")
    with cg:
        result = cg.run_process_to_completion(
            command=destroy_cmd,
            is_checked_after=False,
        )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "mngr destroy failed"
        logger.error("Failed to destroy workspace {}: {}", agent_id, err)
        return Response(
            status_code=500,
            content=json.dumps({"error": "Failed to destroy workspace: {}".format(err)}),
            media_type="application/json",
        )

    # 3. Remove the per-agent minds data directory (api_key_hash, etc.). The
    #    mngr destroy above handles everything on the mngr side; this covers
    #    the minds-side state that mngr doesn't know about.
    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is not None:
        agent_dir = agent_creator.paths.data_dir / "agents" / agent_id
        if agent_dir.is_dir():
            try:
                shutil.rmtree(agent_dir)
            except OSError as e:
                logger.warning("Failed to remove minds data dir for {}: {}", agent_id, e)

    return Response(
        content=json.dumps({"status": "DELETED", "redirect_url": "/"}),
        media_type="application/json",
    )


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
        server_name = req.server_name if isinstance(req, SharingRequestEvent) else ""
        parsed_id = AgentId(req.agent_id)
        ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
        if not ws_name:
            info = backend_resolver.get_agent_display_info(parsed_id)
            ws_name = info.agent_name if info else req.agent_id[:16]
        event_id = str(req.event_id)
        cards.append(
            f'<div class="req-card" onclick="navigateToRequest(\'{event_id}\')">'
            f'<div style="font-size:13px;color:#e2e8f0;font-weight:500;">sharing: {ws_name}</div>'
            f'<div style="font-size:12px;color:#64748b;margin-top:2px;">{server_name}</div></div>'
        )

    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Requests</title>'
        "<style>body{font-family:-apple-system,sans-serif;background:#0f172a;color:#cbd5e1;"
        "margin:0;padding:0;overflow-y:auto;height:100vh;}"
        "h2{font-size:15px;color:#e2e8f0;padding:12px;margin:0;border-bottom:1px solid #334155;}"
        ".req-card{padding:10px 12px;margin:2px 0;cursor:pointer;border-radius:6px;transition:background 100ms;}"
        ".req-card:hover{background:rgba(255,255,255,0.06);}"
        "</style></head>"
        f"<body>"
        f"<script>"
        f"function navigateToRequest(eventId) {{"
        f'  if (window.minds) window.minds.navigateContent("/requests/" + eventId);'
        f'  else window.top.location = "/requests/" + eventId;'
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
    return HTMLResponse(content=html)


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
    server_name = req_event.server_name if is_sharing else ""
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
        server_name=server_name,
        title=f"Sharing Request: {server_name}",
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
    server_name: str,
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
        server_name=server_name,
        title=f"Sharing: {server_name}",
        is_request=False,
        has_account=has_account,
        accounts=accounts,
        redirect_url=f"/sharing/{agent_id}/{server_name}",
        ws_name=ws_name,
        account_email=account_email,
    )
    return HTMLResponse(content=html)


async def _handle_sharing_enable(
    agent_id: str,
    server_name: str,
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
        parsed_server = ServerName(server_name)
        backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
        if backend_url:
            paths: WorkspacePaths = request.app.state.api_v1_paths
            stored_token = _load_tunnel_token(paths.data_dir, parsed_id)
            if stored_token is None:
                token, _ = cf_client.create_tunnel(parsed_id)
                if token:
                    _save_tunnel_token(paths.data_dir, parsed_id, token)
                    inject_tunnel_token_into_agent(parsed_id, token)
            cf_client.add_service(parsed_id, parsed_server, backend_url)
            sharing_succeeded = True
            # Apply auth rules if emails were provided
            if emails:
                rules: list[dict[str, object]] = [
                    {"action": "allow", "include": [{"email": {"email": e}} for e in emails]},
                ]
                cf_client.set_service_auth(parsed_id, str(parsed_server), rules)

    # If there's a pending request for this agent/server, mark it as granted only if sharing succeeded
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is not None and sharing_succeeded:
        for req in inbox.get_pending_requests():
            if isinstance(req, SharingRequestEvent) and req.agent_id == agent_id and req.server_name == server_name:
                paths = request.app.state.api_v1_paths
                response_event = create_request_response_event(
                    request_event_id=str(req.event_id),
                    status=RequestStatus.GRANTED,
                    agent_id=agent_id,
                    request_type=req.request_type,
                    server_name=server_name,
                )
                append_response_event(paths.data_dir, response_event)
                request.app.state.request_inbox = inbox.add_response(response_event)
                break

    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{server_name}"})


async def _handle_sharing_disable(
    agent_id: str,
    server_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Disable sharing for a server."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    cf_client, _ = get_cf_client_with_auth(request, agent_id=AgentId(agent_id))
    if cf_client is not None:
        cf_client.remove_service(AgentId(agent_id), server_name)

    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{server_name}"})


def _handle_sharing_status_api(
    agent_id: str,
    server_name: str,
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

    hostname = services.get(server_name)
    if hostname:
        auth_rules = cf_client.get_service_auth(parsed_id, server_name)
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
            req_event.agent_id, req_event.server_name, request, auth_store, backend_resolver
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
        server_name=req_event.server_name if isinstance(req_event, SharingRequestEvent) else None,
    )
    append_response_event(paths.data_dir, response_event)
    request.app.state.request_inbox = inbox.add_response(response_event)

    return Response(status_code=303, headers={"Location": "/"})


_request_event_apps: dict[int, FastAPI] = {}


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


# -- App factory --


def create_desktop_client(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    tunnel_manager: SSHTunnelManager | None = None,
    agent_creator: AgentCreator | None = None,
    cloudflare_client: CloudflareForwardingClient | None = None,
    telegram_orchestrator: TelegramSetupOrchestrator | None = None,
    notification_dispatcher: NotificationDispatcher | None = None,
    paths: WorkspacePaths | None = None,
    minds_config: MindsConfig | None = None,
    stream_manager: MngrStreamManager | None = None,
    session_store: MultiAccountSessionStore | None = None,
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

    app.state.auth_store = auth_store
    app.state.backend_resolver = backend_resolver
    app.state.tunnel_manager = tunnel_manager
    app.state.stream_manager = stream_manager
    app.state.agent_creator = agent_creator
    app.state.cloudflare_client = cloudflare_client
    app.state.telegram_orchestrator = telegram_orchestrator
    app.state.notification_dispatcher = notification_dispatcher
    app.state.session_store = session_store
    app.state.minds_config = minds_config
    app.state.request_inbox = request_inbox
    app.state.auth_server_port = server_port
    app.state.auth_output_format = output_format or OutputFormat.JSONL
    if paths is not None:
        app.state.api_v1_paths = paths
    if http_client is not None:
        app.state.http_client = http_client

    # Register callback to process incoming request events from agents
    if isinstance(backend_resolver, MngrCliBackendResolver):
        _request_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_request_callback(_handle_request_event_callback)

    # Mount the SuperTokens auth routes
    if session_store is not None:
        supertokens_router = create_supertokens_router(
            session_store=session_store,
            server_port=server_port,
            output_format=output_format or OutputFormat.JSONL,
        )
        app.include_router(supertokens_router)

    # Mount the REST API v1 router
    if paths is not None:
        api_v1_router = create_api_v1_router()
        app.include_router(api_v1_router, prefix="/api/v1")

    # Chrome (persistent shell) routes
    app.get("/_chrome")(_handle_chrome_page)
    app.get("/_chrome/sidebar")(_handle_chrome_sidebar)
    app.get("/_chrome/events")(_handle_chrome_events)

    # Register routes
    app.get("/login")(_handle_login)
    app.get("/authenticate")(_handle_authenticate)
    app.get("/")(_handle_landing_page)

    # Account management routes
    app.get("/accounts")(_handle_accounts_page)
    app.post("/accounts/set-default")(_handle_set_default_account)
    app.post("/accounts/{user_id}/logout")(_handle_account_logout)

    # Workspace settings routes
    app.get("/workspace/{agent_id}/settings")(_handle_workspace_settings)
    app.post("/workspace/{agent_id}/associate")(_handle_workspace_associate)
    app.post("/workspace/{agent_id}/disassociate")(_handle_workspace_disassociate)
    app.post("/workspace/{agent_id}/delete")(_handle_workspace_delete)

    # Request inbox routes
    app.get("/_chrome/requests-panel")(_handle_requests_panel)
    app.post("/_chrome/requests-auto-open")(_handle_requests_auto_open)
    app.get("/requests/{request_id}")(_handle_request_page)
    app.post("/requests/{request_id}/grant")(_handle_request_grant)
    app.post("/requests/{request_id}/deny")(_handle_request_deny)

    # Sharing editor routes (used by both request approval and direct editing)
    app.get("/sharing/{agent_id}/{server_name}")(_handle_sharing_page)
    app.post("/sharing/{agent_id}/{server_name}/enable")(_handle_sharing_enable)
    app.post("/sharing/{agent_id}/{server_name}/disable")(_handle_sharing_disable)
    app.get("/api/sharing-status/{agent_id}/{server_name}")(_handle_sharing_status_api)

    # Agent creation routes
    app.get("/create")(_handle_create_page)
    app.post("/create")(_handle_create_form_submit)
    app.post("/api/create-agent")(_handle_create_agent_api)
    app.get("/api/create-agent/{agent_id}/status")(_handle_creation_status_api)
    app.get("/api/create-agent/{agent_id}/logs")(_handle_creation_logs_sse)
    app.get("/creating/{agent_id}")(_handle_creating_page)

    # Agent default page: redirect to web server
    app.get("/forwarding/{agent_id}/")(_handle_agent_default_redirect)

    # Agent server listing page: /forwarding/{agent_id}/servers/
    app.get("/forwarding/{agent_id}/servers/")(_handle_agent_servers_page)

    # Toggle global forwarding for a server
    app.post("/forwarding/{agent_id}/servers/{server_name}/global")(_handle_toggle_global)

    # Telegram setup routes
    app.post("/api/agents/{agent_id}/telegram/setup")(_handle_telegram_setup)
    app.get("/api/agents/{agent_id}/telegram/status")(_handle_telegram_status)

    # Proxy routes: /forwarding/{agent_id}/{server_name}/{path:path}
    app.api_route(
        "/forwarding/{agent_id}/{server_name}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )(_handle_proxy_http)

    # WebSocket route needs manual dependency wiring since Depends doesn't work on WS
    @app.websocket("/forwarding/{agent_id}/{server_name}/{path:path}")
    async def proxy_websocket(websocket: WebSocket, agent_id: str, server_name: str, path: str) -> None:
        await _handle_proxy_websocket(
            websocket=websocket,
            agent_id=agent_id,
            server_name=server_name,
            path=path,
            auth_store=auth_store,
            backend_resolver=backend_resolver,
            tunnel_manager=tunnel_manager,
        )

    return app
