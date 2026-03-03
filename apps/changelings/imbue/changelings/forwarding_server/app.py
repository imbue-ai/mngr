import asyncio
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
from loguru import logger
from websockets import ClientConnection

from imbue.changelings.forwarding_server.auth import AuthStoreInterface
from imbue.changelings.forwarding_server.backend_resolver import BackendResolverInterface
from imbue.changelings.forwarding_server.cookie_manager import create_signed_cookie_value
from imbue.changelings.forwarding_server.cookie_manager import get_cookie_name_for_agent
from imbue.changelings.forwarding_server.cookie_manager import verify_signed_cookie_value
from imbue.changelings.forwarding_server.proxy import generate_bootstrap_html
from imbue.changelings.forwarding_server.proxy import generate_service_worker_js
from imbue.changelings.forwarding_server.proxy import rewrite_cookie_path
from imbue.changelings.forwarding_server.proxy import rewrite_proxied_html
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelError
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelManager
from imbue.changelings.forwarding_server.ssh_tunnel import parse_url_host_port
from imbue.changelings.forwarding_server.templates import render_agent_servers_page
from imbue.changelings.forwarding_server.templates import render_auth_error_page
from imbue.changelings.forwarding_server.templates import render_landing_page
from imbue.changelings.forwarding_server.templates import render_login_redirect_page
from imbue.changelings.primitives import OneTimeCode
from imbue.changelings.primitives import ServerName
from imbue.mng.primitives import AgentId

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


def _get_backend_resolver(request: Request) -> BackendResolverInterface:
    return request.app.state.backend_resolver


AuthStoreDep = Annotated[AuthStoreInterface, Depends(_get_auth_store)]
BackendResolverDep = Annotated[BackendResolverInterface, Depends(_get_backend_resolver)]


# -- Auth helpers --


def _check_auth_cookie(
    cookies: Mapping[str, str],
    agent_id: AgentId,
    auth_store: AuthStoreInterface,
) -> bool:
    """Check whether the given cookies contain a valid auth cookie for the agent."""
    signing_key = auth_store.get_signing_key()
    cookie_name = get_cookie_name_for_agent(agent_id)
    cookie_value = cookies.get(cookie_name)
    if cookie_value is None:
        return False
    verified = verify_signed_cookie_value(
        cookie_value=cookie_value,
        signing_key=signing_key,
    )
    return verified == agent_id


def _get_authenticated_agent_ids(
    cookies: Mapping[str, str],
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
) -> list[AgentId]:
    """Extract agent IDs from valid auth cookies."""
    signing_key = auth_store.get_signing_key()
    known_ids = auth_store.list_agent_ids_with_valid_codes()
    resolver_ids = backend_resolver.list_known_agent_ids()

    all_candidate_ids: set[str] = set()
    for agent_id in known_ids:
        all_candidate_ids.add(str(agent_id))
    for agent_id in resolver_ids:
        all_candidate_ids.add(str(agent_id))

    authenticated: list[AgentId] = []
    for candidate_id_str in sorted(all_candidate_ids):
        candidate_id = AgentId(candidate_id_str)
        cookie_name = get_cookie_name_for_agent(candidate_id)
        cookie_value = cookies.get(cookie_name)
        if cookie_value is not None:
            verified = verify_signed_cookie_value(
                cookie_value=cookie_value,
                signing_key=signing_key,
            )
            if verified == candidate_id:
                authenticated.append(candidate_id)

    return authenticated


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
    """Manage the httpx client and SSH tunnel lifecycles for the forwarding server."""
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
        tunnel_manager: SSHTunnelManager | None = inner_app.state.tunnel_manager
        if tunnel_manager is not None:
            tunnel_manager.cleanup()


# -- Route handlers (module-level, using Depends for dependency injection) --


def _handle_login(
    agent_id: str,
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    parsed_id = AgentId(agent_id)
    code = OneTimeCode(one_time_code)

    # If user already has a valid cookie, redirect to landing page
    if _check_auth_cookie(cookies=request.cookies, agent_id=parsed_id, auth_store=auth_store):
        return Response(status_code=307, headers={"Location": "/"})

    # Render JS redirect to /authenticate (prevents prefetch consumption)
    html = render_login_redirect_page(agent_id=parsed_id, one_time_code=code)
    return HTMLResponse(content=html)


def _handle_authenticate(
    agent_id: str,
    one_time_code: str,
    auth_store: AuthStoreDep,
) -> Response:
    parsed_id = AgentId(agent_id)
    code = OneTimeCode(one_time_code)

    is_valid = auth_store.validate_and_consume_code(agent_id=parsed_id, code=code)

    if not is_valid:
        html = render_auth_error_page(message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)

    # Set signed cookie
    signing_key = auth_store.get_signing_key()
    cookie_value = create_signed_cookie_value(agent_id=parsed_id, signing_key=signing_key)
    cookie_name = get_cookie_name_for_agent(parsed_id)

    response = Response(status_code=307, headers={"Location": f"/agents/{parsed_id}/"})
    response.set_cookie(
        key=cookie_name,
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
    authenticated_ids = _get_authenticated_agent_ids(
        cookies=request.cookies,
        auth_store=auth_store,
        backend_resolver=backend_resolver,
    )
    html = render_landing_page(accessible_agent_ids=authenticated_ids)
    return HTMLResponse(content=html)


def _handle_agent_servers_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Show a listing of all available servers for a given agent."""
    parsed_id = AgentId(agent_id)

    if not _check_auth_cookie(cookies=request.cookies, agent_id=parsed_id, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated for this changeling")

    server_names = backend_resolver.list_servers_for_agent(parsed_id)
    html = render_agent_servers_page(agent_id=parsed_id, server_names=server_names)
    return HTMLResponse(content=html)


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
    proxy_url = f"{backend_url}/{path}"
    if request.url.query:
        proxy_url += f"?{request.url.query}"

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
        logger.debug("Backend connection refused for {} server {}", agent_id, server_name)
        return Response(status_code=502, content="Backend connection refused")
    except httpx.TimeoutException:
        logger.debug("Backend request timed out for {} server {}", agent_id, server_name)
        return Response(status_code=504, content="Backend request timed out")


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

    # Check auth (per-agent, not per-server)
    if not _check_auth_cookie(cookies=request.cookies, agent_id=parsed_id, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated for this changeling")

    # Serve the service worker script
    if path == "__sw.js":
        return Response(
            content=generate_service_worker_js(parsed_id, parsed_server),
            media_type="application/javascript",
        )

    backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
    if backend_url is None:
        return Response(
            status_code=502,
            content=f"Backend unavailable for agent {agent_id}, server {server_name}",
        )

    # Check if SW is installed via cookie (scoped per server)
    sw_cookie = request.cookies.get(f"sw_installed_{agent_id}_{server_name}")
    is_navigation = request.headers.get("sec-fetch-mode") == "navigate"

    # First HTML navigation without SW -> serve bootstrap
    if is_navigation and not sw_cookie:
        return HTMLResponse(generate_bootstrap_html(parsed_id, parsed_server))

    # Determine if this backend needs SSH tunneling (run in executor to avoid blocking event loop
    # during SSH handshake which can take several seconds)
    try:
        tunnel_client = await asyncio.get_running_loop().run_in_executor(
            None, _get_tunnel_http_client, request.app, parsed_id, backend_url, backend_resolver
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.debug("SSH tunnel setup failed for {} server {}: {}", agent_id, server_name, e)
        return Response(status_code=502, content=f"SSH tunnel to remote backend failed: {e}")

    # Forward request to backend
    result = await _forward_http_request(
        request=request,
        backend_url=backend_url,
        path=path,
        agent_id=agent_id,
        server_name=server_name,
        http_client=tunnel_client,
    )

    # If forwarding returned an error Response directly, return it
    if isinstance(result, Response):
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

    # Check auth (per-agent)
    if not _check_auth_cookie(cookies=websocket.cookies, agent_id=parsed_id, auth_store=auth_store):
        await websocket.close(code=4003, reason="Not authenticated")
        return

    backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
    if backend_url is None:
        await websocket.close(code=4004, reason=f"Unknown server: {agent_id}/{server_name}")
        return

    ws_backend = backend_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_backend}/{path}"
    if websocket.url.query:
        ws_url += f"?{websocket.url.query}"

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
    """Get the Unix socket path for tunneling to a remote backend, or None for local.

    Returns None if the agent is local (no SSH info) or no tunnel manager is configured.
    """
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

    Caches httpx clients per Unix socket path for reuse across requests.
    """
    tunnel_manager: SSHTunnelManager | None = app.state.tunnel_manager
    socket_path = _get_tunnel_socket_path(tunnel_manager, agent_id, backend_url, backend_resolver)
    if socket_path is None:
        return None

    clients: dict[str, httpx.AsyncClient] = app.state.ssh_http_clients
    key = str(socket_path)
    if key not in clients:
        transport = httpx.AsyncHTTPTransport(uds=key)
        clients[key] = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
    return clients[key]


# -- App factory --


def create_forwarding_server(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    tunnel_manager: SSHTunnelManager | None = None,
) -> FastAPI:
    """Create the forwarding server FastAPI application.

    When tunnel_manager is provided, the server can proxy traffic to remote agents
    by tunneling through SSH. Without it, only local agents are reachable.
    """
    is_externally_managed_client = http_client is not None

    @asynccontextmanager
    async def _lifespan(inner_app: FastAPI) -> AsyncGenerator[None, None]:
        async with _managed_lifespan(inner_app=inner_app, is_externally_managed_client=is_externally_managed_client):
            yield

    app = FastAPI(lifespan=_lifespan)

    app.state.auth_store = auth_store
    app.state.backend_resolver = backend_resolver
    app.state.tunnel_manager = tunnel_manager
    if http_client is not None:
        app.state.http_client = http_client

    # Register routes
    app.get("/login")(_handle_login)
    app.get("/authenticate")(_handle_authenticate)
    app.get("/")(_handle_landing_page)

    # Agent server listing page: /agents/{agent_id}/
    app.get("/agents/{agent_id}/")(_handle_agent_servers_page)

    # Proxy routes: /agents/{agent_id}/{server_name}/{path:path}
    app.api_route(
        "/agents/{agent_id}/{server_name}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )(_handle_proxy_http)

    # WebSocket route needs manual dependency wiring since Depends doesn't work on WS
    @app.websocket("/agents/{agent_id}/{server_name}/{path:path}")
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
