"""FastAPI app for ``mngr forward``: auth + subdomain HTTP/WS forwarding.

Adapted from the subdomain-forwarding portions of
``apps/minds/imbue/minds/desktop_client/app.py``. The minds-specific routes
(create form, accounts, sharing, request inbox, telegram, chrome, etc.) all
stay in minds; the plugin only handles:

- the bare-origin login flow (``/login``, ``/authenticate``, ``/`` debug index)
- the ``/goto/<agent>/`` cookie-bridge to per-subdomain auth
- the ``/_subdomain_auth`` token-redemption handler on each subdomain
- byte-level HTTP forwarding for ``<agent-id>.localhost``
- WebSocket forwarding for ``<agent-id>.localhost``
- the host-header middleware that routes the above
"""

import asyncio
import ipaddress
import socket as socket_module
import threading
from collections.abc import AsyncGenerator
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import quote
from urllib.parse import urlsplit

import httpx
import paramiko
import websockets
import websockets.asyncio.client
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from loguru import logger
from websockets import ClientConnection

from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.auth import AuthStoreInterface
from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.cookie import verify_session_cookie
from imbue.mngr_forward.cookie import verify_subdomain_auth_token
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import FORWARD_SUBDOMAIN_PATTERN
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import OneTimeCode
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import parse_url_host_port

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0

_SUBDOMAIN_AUTH_PATH: Final[str] = "/_subdomain_auth"

_EXCLUDED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {"transfer-encoding", "content-encoding", "content-length"}
)

# WebSocket close-reasons are capped at 123 bytes by RFC 6455. Keep messages
# short; full diagnostic detail goes to ``logger.warning`` instead.
_WS_CLOSE_REASON_LOOPBACK_REFUSED: Final[str] = "Loopback fallback refused"


def _is_loopback_url(url: str) -> bool:
    """Return True if the URL's host is the local loopback (`localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`).

    Used by ``_handle_workspace_forward_*`` to decide whether the proxy is
    safe to dial without an SSH tunnel: a registered URL pointing at host
    loopback when no tunnel exists for the agent means the desktop client
    would silently serve whatever happens to be bound on the host's
    loopback at that port (a security issue, see PR 1482).
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    raw_host = parsed.hostname
    if raw_host is None:
        return False
    host = raw_host.lower()
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _build_jinja_env() -> Environment:
    return Environment(
        loader=PackageLoader("imbue.mngr_forward", "templates"),
        autoescape=select_autoescape(["html"]),
    )


def _render_login_page(env: Environment) -> str:
    return env.get_template("login.html").render()


def _render_login_redirect_page(env: Environment, one_time_code: OneTimeCode) -> str:
    return env.get_template("login_redirect.html").render(one_time_code=str(one_time_code))


def _render_auth_error_page(env: Environment, message: str) -> str:
    return env.get_template("auth_error.html").render(message=message)


def _render_index_page(
    env: Environment,
    agents: list[dict[str, Any]],
    port: int,
) -> str:
    return env.get_template("index.html").render(agents=agents, port=port)


# -- Auth helpers ----------------------------------------------------------


def _is_authenticated(
    cookies: Mapping[str, str],
    auth_store: AuthStoreInterface,
    preauth_cookie_value: str | None,
) -> bool:
    """Check whether the user has a valid global session cookie."""
    cookie_value = cookies.get(MNGR_FORWARD_SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    signing_key = auth_store.get_signing_key()
    return verify_session_cookie(
        cookie_value=cookie_value,
        signing_key=signing_key,
        preauth_cookie_value=preauth_cookie_value,
    )


def _parse_workspace_subdomain(host_header: str) -> AgentId | None:
    """Return the agent ID if ``host_header`` is ``agent-<hex>.localhost(:port)``."""
    if not host_header:
        return None
    match = FORWARD_SUBDOMAIN_PATTERN.match(host_header)
    if match is None:
        return None
    try:
        return AgentId(match.group(1))
    except ValueError:
        return None


def _unauthenticated_subdomain_response(request: Request, port: int) -> Response:
    """Redirect HTML navigations to the bare-origin landing page; 403 for everything else."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        location = f"http://localhost:{port}/"
        return Response(status_code=302, headers={"Location": location})
    return Response(status_code=403, content="Not authenticated")


# -- WebSocket forwarding helpers -----------------------------------------


def _connect_backend_websocket(
    ws_url: str,
    subprotocols: list[str],
    tunnel_socket_path: Path | None,
) -> "websockets.asyncio.client.connect":
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


async def _forward_client_to_backend(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
) -> None:
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


# -- HTTP/WS tunnel helpers -----------------------------------------------


def _get_tunnel_socket_path(
    tunnel_manager: SSHTunnelManager,
    backend_url: str,
    ssh_info: RemoteSSHInfo | None,
) -> Path | None:
    if ssh_info is None:
        return None
    remote_host, remote_port = parse_url_host_port(backend_url)
    return tunnel_manager.get_tunnel_socket_path(
        ssh_info=ssh_info,
        remote_host=remote_host,
        remote_port=remote_port,
    )


def _get_tunnel_http_client(
    tunnel_manager: SSHTunnelManager,
    backend_url: str,
    ssh_info: RemoteSSHInfo | None,
    ssh_http_clients: dict[str, httpx.AsyncClient],
    ssh_http_clients_lock: threading.Lock,
) -> httpx.AsyncClient | None:
    """Return a cached httpx client tied to the per-tunnel Unix socket, or None for direct.

    The client is cached on ``ssh_http_clients`` (owned by the FastAPI app's
    lifespan) keyed by the tunnel socket path, so its connection pool is reused
    across requests and aclose'd exactly once on shutdown. Constructing a new
    client per request would leak the underlying transport + pool every call.

    The lookup-and-insert is guarded by ``ssh_http_clients_lock`` because the
    function runs in the default executor's thread pool (via
    ``run_in_executor``), so two concurrent requests to the same backend would
    otherwise both miss the cache, both construct a fresh ``AsyncClient``, and
    one of the clients would be orphaned and never ``aclose``'d on shutdown --
    leaking its transport + connection pool.
    """
    socket_path = _get_tunnel_socket_path(tunnel_manager, backend_url, ssh_info)
    if socket_path is None:
        return None
    socket_path_str = str(socket_path)
    with ssh_http_clients_lock:
        cached = ssh_http_clients.get(socket_path_str)
        if cached is not None:
            return cached
        transport = httpx.AsyncHTTPTransport(uds=socket_path_str)
        client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
        ssh_http_clients[socket_path_str] = client
        return client


# -- HTTP forwarding -------------------------------------------------------


async def _forward_workspace_http(
    request: Request,
    backend_url: str,
    http_client: httpx.AsyncClient,
) -> Response:
    base = backend_url.rstrip("/")
    path = request.url.path.lstrip("/")
    url = f"{base}/{path}" if path else base + "/"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    raw_cookie = headers.get("cookie")
    if raw_cookie is not None:
        # Strip our session cookie so agent-controlled backends can't lift it.
        stripped = "; ".join(
            c.strip()
            for c in raw_cookie.split(";")
            if not c.strip().startswith(MNGR_FORWARD_SESSION_COOKIE_NAME + "=")
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
            return _service_unavailable_response(request)
        except httpx.TimeoutException:
            return Response(status_code=504, content="Backend stream timed out")

        async def _stream() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in backend_response.aiter_bytes():
                    yield chunk
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                logger.warning("Backend SSE stream failed for {}: {}", request.url.path, e)
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
    except (httpx.ConnectError, httpx.RemoteProtocolError):
        # Workspace-server may not yet be listening, or it may have closed the
        # connection before sending headers (typical during startup). HTML
        # navigations get the auto-refreshing retry page so the user lands on
        # something useful instead of a hard 502; non-HTML callers get a plain
        # 503 they can interpret programmatically.
        return _service_unavailable_response(request)
    except httpx.ReadError:
        return Response(status_code=502, content="Backend connection lost")
    except httpx.TimeoutException:
        return Response(status_code=504, content="Backend timed out")

    response = Response(content=backend_response.content, status_code=backend_response.status_code)
    for header_key, header_value in backend_response.headers.multi_items():
        if header_key.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        response.headers.append(header_key, header_value)
    return response


def _service_unavailable_response(request: Request) -> Response:
    """Return a 503 with the auto-refreshing HTML page for HTML navigations."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            content=(
                "<!doctype html><html><head>"
                '<meta http-equiv="refresh" content="1">'
                "</head><body>"
                "<p>Backend not yet available. Retrying...</p>"
                "</body></html>"
            ),
            status_code=503,
        )
    return Response(status_code=503, content="Backend not yet available")


# -- Subdomain handlers ---------------------------------------------------


def _sanitize_next_url(value: str) -> str:
    """Return ``value`` if it is a same-origin path; otherwise ``"/"``.

    A same-origin redirect target must start with a single ``/`` and must
    not start with ``//`` or ``/\\`` -- those forms are protocol-relative
    URLs that browsers interpret as cross-origin, which would let an
    attacker craft ``?next=//evil.com`` to bounce an authenticated user
    off-origin.
    """
    if not value.startswith("/"):
        return "/"
    if value.startswith("//") or value.startswith("/\\"):
        return "/"
    return value


def _handle_subdomain_auth_bridge(
    request: Request,
    agent_id: AgentId,
    auth_store: AuthStoreInterface,
) -> Response:
    token = request.query_params.get("token", "")
    next_url = _sanitize_next_url(request.query_params.get("next", "/"))
    signing_key = auth_store.get_signing_key()
    if not verify_subdomain_auth_token(token=token, signing_key=signing_key, agent_id=str(agent_id)):
        return Response(status_code=403, content="Invalid or expired subdomain auth token")
    cookie_value = create_session_cookie(signing_key=signing_key)
    response = Response(status_code=302, headers={"Location": next_url})
    response.set_cookie(
        key=MNGR_FORWARD_SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


async def _handle_workspace_forward_http(
    request: Request,
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    tunnel_manager: SSHTunnelManager,
    http_client: httpx.AsyncClient,
    ssh_http_clients: dict[str, httpx.AsyncClient],
    ssh_http_clients_lock: threading.Lock,
    preauth_cookie_value: str | None,
    listen_port: int,
    allow_host_loopback: bool,
) -> Response:
    host_header = request.headers.get("host", "")
    agent_id = _parse_workspace_subdomain(host_header)
    if agent_id is None:
        return Response(status_code=404)

    if request.url.path == _SUBDOMAIN_AUTH_PATH:
        return _handle_subdomain_auth_bridge(request, agent_id, auth_store)

    if not _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        return _unauthenticated_subdomain_response(request, listen_port)

    target = resolver.resolve(agent_id)
    if target is None:
        return _service_unavailable_response(request)

    backend_url = str(target.url)
    try:
        tunnel_client = await asyncio.get_running_loop().run_in_executor(
            None,
            _get_tunnel_http_client,
            tunnel_manager,
            backend_url,
            target.ssh_info,
            ssh_http_clients,
            ssh_http_clients_lock,
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.warning("SSH tunnel setup failed for {}: {}", agent_id, e)
        return Response(status_code=502, content=f"SSH tunnel failed: {e}")

    if tunnel_client is None and _is_loopback_url(backend_url) and not allow_host_loopback:
        logger.warning(
            "Refusing to dial host loopback for agent {}: registered URL {} has no SSH tunnel "
            "(pass --allow-host-loopback if the agent really runs on the host).",
            agent_id,
            backend_url,
        )
        return Response(
            status_code=502,
            content=(
                f"workspace server unreachable: no SSH tunnel available for agent {agent_id}; "
                f"refusing to dial host loopback at {backend_url}"
            ),
        )

    active_client = tunnel_client or http_client
    return await _forward_workspace_http(request=request, backend_url=backend_url, http_client=active_client)


async def _handle_workspace_forward_websocket(
    websocket: WebSocket,
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    tunnel_manager: SSHTunnelManager,
    preauth_cookie_value: str | None,
    allow_host_loopback: bool,
) -> None:
    host_header = websocket.headers.get("host", "")
    agent_id = _parse_workspace_subdomain(host_header)
    if agent_id is None:
        await websocket.close(code=4004, reason="Unknown host")
        return

    if not _is_authenticated(
        cookies=websocket.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        await websocket.close(code=4003, reason="Not authenticated")
        return

    target = resolver.resolve(agent_id)
    if target is None:
        await websocket.close(code=1013, reason="Backend not yet available")
        return

    backend_url = str(target.url)
    try:
        tunnel_socket_path = await asyncio.get_running_loop().run_in_executor(
            None,
            _get_tunnel_socket_path,
            tunnel_manager,
            backend_url,
            target.ssh_info,
        )
    except (SSHTunnelError, paramiko.SSHException, OSError) as e:
        logger.debug("SSH tunnel setup failed for WS {}: {}", agent_id, e)
        try:
            await websocket.close(code=1011, reason="SSH tunnel failed")
        except RuntimeError:
            pass
        return

    if tunnel_socket_path is None and _is_loopback_url(backend_url) and not allow_host_loopback:
        logger.warning(
            "Refusing WS to host loopback for agent {}: registered URL {} has no SSH tunnel "
            "(pass --allow-host-loopback if the agent really runs on the host).",
            agent_id,
            backend_url,
        )
        try:
            await websocket.close(code=1013, reason=_WS_CLOSE_REASON_LOOPBACK_REFUSED)
        except RuntimeError:
            pass
        return

    ws_backend = backend_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
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
    except (
        ConnectionRefusedError,
        OSError,
        TimeoutError,
        SSHTunnelError,
        paramiko.SSHException,
    ) as connection_error:
        logger.debug("Backend WS connection failed for {}: {}", agent_id, connection_error)
        try:
            await websocket.close(code=1011, reason="Backend connection failed")
        except RuntimeError:
            pass


# -- Bare-origin handlers --------------------------------------------------


def _handle_login(
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreInterface,
    env: Environment,
    preauth_cookie_value: str | None,
) -> Response:
    if _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        return Response(status_code=307, headers={"Location": "/"})
    if not one_time_code or not one_time_code.strip():
        html = _render_auth_error_page(env, message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)
    code = OneTimeCode(one_time_code)
    html = _render_login_redirect_page(env, code)
    return HTMLResponse(content=html)


def _handle_authenticate(
    one_time_code: str,
    auth_store: AuthStoreInterface,
    env: Environment,
) -> Response:
    if not one_time_code or not one_time_code.strip():
        html = _render_auth_error_page(env, message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)
    code = OneTimeCode(one_time_code)
    is_valid = auth_store.validate_and_consume_code(code=code)
    if not is_valid:
        html = _render_auth_error_page(env, message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)
    signing_key = auth_store.get_signing_key()
    cookie_value = create_session_cookie(signing_key=signing_key)
    response = Response(status_code=307, headers={"Location": "/"})
    response.set_cookie(
        key=MNGR_FORWARD_SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


def _handle_debug_index(
    request: Request,
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    env: Environment,
    preauth_cookie_value: str | None,
    listen_port: int,
) -> Response:
    if not _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        html = _render_login_page(env)
        return HTMLResponse(content=html)
    agents = []
    for agent_id in resolver.list_known_agent_ids():
        target = resolver.resolve(agent_id)
        if target is None:
            agents.append(
                {
                    "agent_id": str(agent_id),
                    "is_unresolved": True,
                    "reason": "(no service URL yet)",
                }
            )
        else:
            agents.append({"agent_id": str(agent_id), "is_unresolved": False, "reason": ""})
    html = _render_index_page(env, agents=agents, port=listen_port)
    return HTMLResponse(content=html)


def _handle_goto_workspace(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreInterface,
    preauth_cookie_value: str | None,
    listen_port: int,
) -> Response:
    if not _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        return Response(status_code=302, headers={"Location": "/"})
    try:
        parsed_id = AgentId(agent_id)
    except ValueError:
        return Response(status_code=404)
    signing_key = auth_store.get_signing_key()
    token = create_subdomain_auth_token(signing_key=signing_key, agent_id=str(parsed_id))
    next_url = _sanitize_next_url(request.query_params.get("next", "/"))
    encoded_next = quote(next_url, safe="")
    location = f"http://{parsed_id}.localhost:{listen_port}{_SUBDOMAIN_AUTH_PATH}?token={token}&next={encoded_next}"
    return Response(status_code=302, headers={"Location": location})


# -- App factory + lifespan ------------------------------------------------


@asynccontextmanager
async def _managed_lifespan(
    inner_app: FastAPI,
    on_listening: Callable[[], None] | None,
) -> AsyncGenerator[None, None]:
    inner_app.state.http_client = httpx.AsyncClient(follow_redirects=False, timeout=_PROXY_TIMEOUT_SECONDS)
    # Per-tunnel httpx clients are cached here so they outlive a single request
    # and their connection pools are reused. Lifespan teardown aclose's them
    # all; without this every request to a remote agent would leak a fresh
    # AsyncClient + AsyncHTTPTransport. The lock guards the cache's
    # check-then-set against concurrent executor threads (the cache lookup
    # runs via run_in_executor) so two concurrent requests to the same
    # backend don't both construct + insert their own AsyncClient and
    # orphan one of them.
    inner_app.state.ssh_http_clients = {}
    inner_app.state.ssh_http_clients_lock = threading.Lock()
    if on_listening is not None:
        try:
            on_listening()
        except (OSError, RuntimeError) as e:
            logger.warning("on_listening callback failed: {}", e)
    try:
        yield
    finally:
        for ssh_client in inner_app.state.ssh_http_clients.values():
            try:
                await ssh_client.aclose()
            except (OSError, RuntimeError) as e:
                logger.trace("Error closing per-tunnel httpx client: {}", e)
        inner_app.state.ssh_http_clients.clear()
        await inner_app.state.http_client.aclose()


def create_forward_app(
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    tunnel_manager: SSHTunnelManager,
    envelope_writer: EnvelopeWriter,
    listen_host: str,
    listen_port: int,
    preauth_cookie_value: str | None = None,
    on_listening: Callable[[], None] | None = None,
    allow_host_loopback: bool = False,
) -> FastAPI:
    """Create the FastAPI app for ``mngr forward``.

    ``allow_host_loopback`` opts the proxy in to dialing host loopback when an
    agent's registered URL is loopback and no SSH tunnel exists. The default
    of ``False`` is the safe one: any non-DEV agent whose SSH info hasn't
    been published gets a 502 instead of silently serving whatever else is
    bound on the host's loopback at the registered port. Pass ``True`` only
    for setups that intentionally run agents directly on the host (the
    legacy ``LaunchMode.DEV`` flow).
    """
    env = _build_jinja_env()

    app = FastAPI(
        title="mngr forward",
        lifespan=lambda inner: _managed_lifespan(inner, on_listening),
    )
    app.state.auth_store = auth_store
    app.state.resolver = resolver
    app.state.tunnel_manager = tunnel_manager
    app.state.envelope_writer = envelope_writer
    app.state.listen_host = listen_host
    app.state.listen_port = listen_port
    app.state.preauth_cookie_value = preauth_cookie_value
    app.state.allow_host_loopback = allow_host_loopback

    @app.middleware("http")
    async def _subdomain_routing_middleware(request: Request, call_next: Any) -> Response:
        host_header = request.headers.get("host", "")
        agent_id = _parse_workspace_subdomain(host_header)
        if agent_id is None:
            return await call_next(request)
        return await _handle_workspace_forward_http(
            request=request,
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=tunnel_manager,
            http_client=app.state.http_client,
            ssh_http_clients=app.state.ssh_http_clients,
            ssh_http_clients_lock=app.state.ssh_http_clients_lock,
            preauth_cookie_value=preauth_cookie_value,
            listen_port=listen_port,
            allow_host_loopback=allow_host_loopback,
        )

    @app.get("/login")
    def _login(one_time_code: str, request: Request) -> Response:
        return _handle_login(
            one_time_code=one_time_code,
            request=request,
            auth_store=auth_store,
            env=env,
            preauth_cookie_value=preauth_cookie_value,
        )

    @app.get("/authenticate")
    def _authenticate(one_time_code: str) -> Response:
        return _handle_authenticate(
            one_time_code=one_time_code,
            auth_store=auth_store,
            env=env,
        )

    @app.get("/")
    def _index(request: Request) -> Response:
        return _handle_debug_index(
            request=request,
            auth_store=auth_store,
            resolver=resolver,
            env=env,
            preauth_cookie_value=preauth_cookie_value,
            listen_port=listen_port,
        )

    @app.get("/goto/{agent_id}/")
    @app.get("/goto/{agent_id}")
    def _goto(agent_id: str, request: Request) -> Response:
        return _handle_goto_workspace(
            agent_id=agent_id,
            request=request,
            auth_store=auth_store,
            preauth_cookie_value=preauth_cookie_value,
            listen_port=listen_port,
        )

    @app.websocket("/{path:path}")
    async def _subdomain_ws(websocket: WebSocket, path: str) -> None:
        del path
        host_header = websocket.headers.get("host", "")
        if _parse_workspace_subdomain(host_header) is None:
            await websocket.close(code=4004, reason="Unknown host")
            return
        await _handle_workspace_forward_websocket(
            websocket=websocket,
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=tunnel_manager,
            preauth_cookie_value=preauth_cookie_value,
            allow_host_loopback=allow_host_loopback,
        )

    return app
