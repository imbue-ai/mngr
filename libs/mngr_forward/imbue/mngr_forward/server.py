"""FastAPI app for ``mngr forward``: auth + workspace-origin HTTP/WS forwarding.

Adapted from the subdomain-forwarding portions of minds' desktop client.
Application-specific routes (create form, accounts, sharing, request inbox,
telegram, chrome, etc.) stay in the host application; the plugin only handles:

- the bare-origin login flow (``/login``, ``/authenticate``, ``/`` debug index)
- the ``/goto/<host-id>/`` cookie-bridge to workspace-domain auth
- the ``/_subdomain_auth`` token-redemption handler on each workspace origin
- byte-level HTTP forwarding for ``[<service>.]host-<hex>.localhost``
- WebSocket forwarding for ``[<service>.]host-<hex>.localhost``
- the host-header middleware that routes the above

Every workspace (host) owns a family of origins: the bare
``host-<hex>.localhost`` origin serves the configured shell service, and
each registered service owns ``<name>.host-<hex>.localhost`` (deeper labels
route to the same service -- they are the service's own sub-origin space).
One domain-scoped session cookie covers the whole family.
"""

import asyncio
import ipaddress
import re
import secrets
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
from fastapi.websockets import WebSocketState
from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from loguru import logger
from websockets import ClientConnection

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_forward.auth import AuthStoreInterface
from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.cookie import verify_session_cookie
from imbue.mngr_forward.cookie import verify_subdomain_auth_token
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailurePayload
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.embedding import EmbedderOrigin
from imbue.mngr_forward.embedding import build_frame_ancestors_policy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.errors import MngrForwardError
from imbue.mngr_forward.loading_page import render_loading_page
from imbue.mngr_forward.primitives import BROWSER_BRIDGE_PATH
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.primitives import OneTimeCode
from imbue.mngr_forward.primitives import ParsedForwardHost
from imbue.mngr_forward.primitives import ServiceLabel
from imbue.mngr_forward.primitives import parse_forward_host
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import parse_url_host_port

# How long a backend may go without answering before the plugin emits an
# advisory ``STALLED`` envelope. The request is deliberately *not* abandoned:
# the envelope only enrolls the agent for active probing, and the probe --
# not this timer -- decides whether the workspace is wedged. Cancelling here
# would additionally cap every proxied request at this value, which silently
# breaks any user app endpoint that legitimately takes longer.
_STALL_NOTICE_SECONDS: Final[float] = 30.0

# Backstop for a backend that goes silent. httpx applies this per read, not to
# the request as a whole, so it bounds the gap between bytes: a backend that
# answers headers and then keeps trickling never trips it, and is bounded only
# by its client staying connected. That is the first line of defence anyway --
# a request ends when its client disconnects, which is what keeps abandoned
# requests (minds' 2s health probe against a wedged backend) from accumulating
# against httpx's 100-connection pool. Nothing caps total request duration, by
# design: a cap there breaks any user app endpoint that legitimately runs long.
#
# The write side gets the same budget, and for the same reason: a backend that
# accepted the connection and then stopped reading the body is the mirror of one
# that stopped answering. A short budget there would be worse than on reads,
# because the body of a large upload is relayed byte-for-byte (over the SSH
# channel, for a remote agent), so a slow link legitimately spends minutes on it.
_PROXY_BACKSTOP_TIMEOUT_SECONDS: Final[float] = 600.0

# Establishing the connection and acquiring a pooled slot keep a short budget:
# unlike a slow response, neither is ever legitimately slow here (the dial is
# loopback or a local unix socket).
_PROXY_CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0

# SSE gets a tight read budget rather than the backstop. A silent stream is the
# chrome's fastest wedged-backend signal, and an SSE producer is expected to
# heartbeat, so a 30s gap here really is evidence of a problem rather than slow
# work. Its write side rides along on the same value, which costs nothing: an
# SSE request carries no body worth speaking of.
_SSE_READ_TIMEOUT_SECONDS: Final[float] = 30.0

# Timeout for the non-SSE forwarding path: short to connect, long to respond.
_PROXY_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
    pool=_PROXY_CONNECT_TIMEOUT_SECONDS,
    read=_PROXY_BACKSTOP_TIMEOUT_SECONDS,
    write=_PROXY_BACKSTOP_TIMEOUT_SECONDS,
)

# Timeout for the SSE forwarding path: the same short connect budget, and a read
# budget just as short.
_SSE_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
    pool=_PROXY_CONNECT_TIMEOUT_SECONDS,
    read=_SSE_READ_TIMEOUT_SECONDS,
    write=_SSE_READ_TIMEOUT_SECONDS,
)


_SUBDOMAIN_AUTH_PATH: Final[str] = "/_subdomain_auth"


# Strict shape for the /goto/{host_id} path segment: the exact (lowercased)
# ``host-<32hex>`` coordinate FORWARD_SUBDOMAIN_PATTERN routes.
_GOTO_HOST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"host-[a-f0-9]{32}")

# One sub-origin label of a service's deeper origin space: the per-label
# charset FORWARD_SUBDOMAIN_PATTERN routes (looser than ServiceLabel, which
# applies only to the service name itself).
_SUB_ORIGIN_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_-]+")

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


def _append_partitioned_to_last_set_cookie(response: Response) -> None:
    """Append ``; Partitioned`` to the most recently written ``Set-Cookie`` header.

    ``http.cookies`` (which starlette's ``set_cookie`` serializes through)
    only learned the ``Partitioned`` attribute in Python 3.14, so on earlier
    interpreters the attribute must be appended to the raw header.
    """
    for index in range(len(response.raw_headers) - 1, -1, -1):
        header_key, header_value = response.raw_headers[index]
        if header_key == b"set-cookie":
            response.raw_headers[index] = (header_key, header_value + b"; Partitioned")
            return
    raise MngrForwardError("No Set-Cookie header to mark Partitioned")


def _set_forward_session_cookie(
    response: Response,
    cookie_value: str,
    use_http2: bool,
    domain: str | None = None,
) -> None:
    """Set the plugin session cookie with embedding-compatible attributes.

    On the TLS path (``use_http2``) the cookie is ``SameSite=None; Secure;
    Partitioned`` so it is sent from inside a cross-site iframe (the minds
    chrome embeds workspace origins; the top-level page is a different site).
    ``Partitioned`` keys the jar by the embedding top-level site in browsers
    that block unpartitioned third-party cookies; browsers without CHIPS
    support ignore the attribute. The plain-HTTP path keeps ``Lax``
    (``SameSite=None`` requires ``Secure``), so embedding is unsupported
    there -- top-level navigation keeps working as before.
    """
    if use_http2:
        response.set_cookie(
            key=MNGR_FORWARD_SESSION_COOKIE_NAME,
            value=cookie_value,
            path="/",
            domain=domain,
            httponly=True,
            samesite="none",
            secure=True,
        )
        # starlette's ``partitioned=`` kwarg needs the Python 3.14 SimpleCookie,
        # so the attribute is appended to the just-written header directly.
        _append_partitioned_to_last_set_cookie(response)
    else:
        response.set_cookie(
            key=MNGR_FORWARD_SESSION_COOKIE_NAME,
            value=cookie_value,
            path="/",
            domain=domain,
            httponly=True,
            samesite="lax",
            secure=False,
        )


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


def _unauthenticated_subdomain_response(
    request: Request,
    port: int,
    use_http2: bool,
    host_info: ParsedForwardHost,
) -> Response:
    """Redirect HTML navigations to the workspace's /goto/ bridge; 403 for everything else.

    The bridge re-mints a fresh workspace auth token using the bare-origin
    session cookie (which the host app refreshes on every restart) and
    sets a new domain-scoped workspace cookie before bouncing the browser
    back. Without this, a workspace cookie that fails verification (stale
    signing key after a host-app restart, expired window) would land the
    user on the bare-origin landing instead of self-healing into the
    workspace.

    Service origins (``<name>.host-<hex>.localhost``) carry their label
    chain through the bridge via ``service`` so the final bounce returns to
    the exact origin (and path) that was requested; the cookie the bridge
    sets is domain-scoped to ``host-<hex>.localhost`` so one hop covers the
    shell and every service subtree.
    """
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return Response(status_code=403, content="Not authenticated")
    scheme = "https" if use_http2 else "http"
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    location = f"{scheme}://localhost:{port}/goto/{host_info.host_id_str}/?next={quote(next_url, safe='')}"
    if host_info.service_labels is not None:
        location = f"{location}&service={host_info.service_labels}"
    return Response(status_code=302, headers={"Location": location})


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


def _select_ws_receive_payload(event: Mapping[str, Any]) -> str | bytes | None:
    """Return the payload of an ASGI ``websocket.receive`` event (text or bytes), or None.

    Per the ASGI spec exactly one of ``text``/``bytes`` is non-None. We select by
    value rather than key presence because hypercorn always includes BOTH keys
    (setting the unused one to None), so a key-presence check would pick the
    co-present ``text: None`` on a binary frame and forward ``None`` -- which
    raises ``TypeError`` in the ``websockets`` client and kills the socket
    (uvicorn omitted the unused key, which masked this). Returns None for an
    event carrying neither payload, which the caller skips.
    """
    text = event.get("text")
    if text is not None:
        return text
    return event.get("bytes")


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
            payload = _select_ws_receive_payload(data)
            if payload is not None:
                await backend_ws.send(payload)
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
            timeout=_PROXY_TIMEOUT,
        )
        ssh_http_clients[socket_path_str] = client
        return client


# -- HTTP forwarding -------------------------------------------------------


def _schedule_stall_notice(
    envelope_writer: EnvelopeWriter, agent_id: AgentId, delay_seconds: float
) -> asyncio.TimerHandle:
    """Arm the advisory ``STALLED`` envelope for a request that has not answered yet.

    The caller must cancel the returned handle once the backend responds (or
    the attempt ends). Firing does not touch the request.
    """
    return asyncio.get_running_loop().call_later(
        delay_seconds,
        _emit_backend_failure,
        envelope_writer,
        agent_id,
        SystemInterfaceBackendFailureReason.STALLED,
        None,
    )


async def _wait_for_client_disconnect(request: Request) -> None:
    """Block until the ASGI server reports that the client went away.

    The request body is fully read before this runs, so the next message on
    the channel is the disconnect itself (hypercorn puts one there when the
    stream closes). ``Request.is_disconnected`` cannot serve this purpose: it
    polls inside an already-cancelled scope and so answers immediately.

    Anything else on the channel is skipped rather than treated as a
    disconnect: mistaking one would abandon a request whose client is still
    waiting, which is the failure this whole path exists to avoid.

    Over HTTP/1.1 the message means read EOF, not strictly "stopped waiting":
    hypercorn emits it once its socket read ends, so a client that half-closes
    its request direction and then waits for the response is treated as gone.
    That is accepted rather than worked around -- h11 cannot express the
    difference, and no client the forward serves half-closes. HTTP/2 does
    distinguish the two, and reports a disconnect only on stream reset or
    connection close.
    """
    message = await request.receive()
    while message["type"] != "http.disconnect":
        message = await request.receive()


async def _forward_workspace_http(
    request: Request,
    backend_url: str,
    http_client: httpx.AsyncClient,
    agent_id: AgentId,
    envelope_writer: EnvelopeWriter,
    stall_notice_seconds: float,
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
        backend_request = http_client.build_request(
            method=request.method, url=url, headers=headers, content=body, timeout=_SSE_TIMEOUT
        )
        try:
            backend_response = await http_client.send(backend_request, stream=True)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            # ``RemoteProtocolError`` here means the backend disconnected
            # before sending headers -- typical when the system interface
            # died between the SSH tunnel accepting the unix-socket
            # connection and the channel-open completing. Same recovery
            # signal as a connect-time failure.
            logger.debug("Failed to reach the backend for {} at {}: {}", agent_id, backend_url, e)
            _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
            return _service_unavailable_response(request)
        except httpx.ReadError as e:
            logger.warning("Lost the backend connection for {} at {}: {}", agent_id, backend_url, e)
            _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.SSE_EOF, None)
            return Response(status_code=502, content="Backend connection lost")
        except httpx.TimeoutException as e:
            # A wedged-but-listening backend produces a TimeoutException
            # rather than ConnectError. Surface this as CONNECT_ERROR so a
            # consumer still treats the agent as failing, matching the
            # behaviour for a backend that returns a 504.
            logger.warning("Timed out reaching the backend for {} at {}: {}", agent_id, backend_url, e)
            _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
            return Response(status_code=504, content="Backend stream timed out")

        async def _stream() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in backend_response.aiter_bytes():
                    yield chunk
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                logger.warning("Backend SSE stream failed for {}: {}", request.url.path, e)
                _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.SSE_EOF, None)
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

    # The stall notice fires *without* cancelling: a backend that is merely
    # slow (a user app's own long-running endpoint) stays in flight, while a
    # genuinely wedged one still gets enrolled for probing at the 30s mark.
    stall_notice = _schedule_stall_notice(envelope_writer, agent_id, stall_notice_seconds)
    # What ends the request is the client giving up, not a clock: race the
    # backend against the disconnect so an abandoned request releases its
    # pooled connection (and, for a remote agent, the SSH channel and relay
    # thread behind it) as soon as nobody is waiting for it.
    backend_task = asyncio.create_task(
        http_client.request(method=request.method, url=url, headers=headers, content=body)
    )
    disconnect_task = asyncio.create_task(_wait_for_client_disconnect(request))
    try:
        done, _pending = await asyncio.wait({backend_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        # A backend that finished at all is in ``done``, even if it lost the
        # race by a hair: ``asyncio.wait`` partitions on ``done()`` right before
        # returning and nothing runs between that and here, so it reports every
        # task that finished before its waiter resumed. Missing from ``done``
        # therefore means still in flight, and its outcome below is the only
        # one there is to read.
        if backend_task not in done:
            # Finishing is not the same as reporting a disconnect: if reading
            # the ASGI channel raised instead, that must propagate rather than
            # abandon a request whose client is in fact still waiting.
            disconnect_task.result()
            # Nothing reads this response -- the socket is already closed -- so
            # it exists only to end the ASGI exchange. No envelope either: a
            # client that gave up is evidence about the client, not the backend.
            logger.debug("Client disconnected before the backend for {} answered at {}", agent_id, backend_url)
            return Response(status_code=499, content="Client disconnected")
        backend_response = backend_task.result()
    except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
        # System interface may not yet be listening, or it may have closed the
        # connection before sending headers (typical during startup). Surface
        # a 503 (and the failure envelope below) so a consumer can react (e.g.
        # navigate the user to its recovery UI); non-HTML callers can interpret
        # the 503 programmatically. Logged at debug, since a workspace that has
        # not finished starting is the expected source of it.
        logger.debug("Failed to reach the backend for {} at {}: {}", agent_id, backend_url, e)
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
        return _service_unavailable_response(request)
    except httpx.ReadError as e:
        # ReadError fires after the connection was established, so this is a
        # mid-response failure (same shape as SSE_EOF on the streaming path),
        # not a connect-time failure -- hence warning: unlike a connect failure,
        # a workspace that is merely still starting does not produce this.
        logger.warning("Lost the backend connection for {} at {}: {}", agent_id, backend_url, e)
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.SSE_EOF, None)
        return Response(status_code=502, content="Backend connection lost")
    except httpx.TimeoutException as e:
        # Either the dial timed out, or the backstop expired on a backend that
        # never answered at all. Both are genuine failures (the stall notice
        # already covers merely-slow), so surface CONNECT_ERROR.
        # Logged at warning while the connect failure above is not: a workspace
        # still starting refuses the connection or closes it, so one that
        # accepted it and then stayed silent is a different condition.
        logger.warning("Timed out reaching the backend for {} at {}: {}", agent_id, backend_url, e)
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
        return Response(status_code=504, content="Backend timed out")
    finally:
        stall_notice.cancel()
        # Cancelling the loser is what actually releases the backend
        # connection; a no-op on a task that already finished. Both are
        # cancelled here rather than after the race because ``asyncio.wait``,
        # unlike ``gather``, leaves them running if this handler is itself
        # cancelled (server shutdown).
        backend_task.cancel()
        disconnect_task.cancel()

    if not 200 <= backend_response.status_code < 300:
        # Any non-2xx response is surfaced as a single ``ERROR_RESPONSE`` signal
        # carrying the status code. The plugin forwards the response unchanged
        # and does not interpret which codes matter -- the consumer decides
        # whether (and how) to react to a given status.
        _emit_backend_failure(
            envelope_writer,
            agent_id,
            SystemInterfaceBackendFailureReason.ERROR_RESPONSE,
            backend_response.status_code,
        )

    response = Response(content=backend_response.content, status_code=backend_response.status_code)
    for header_key, header_value in backend_response.headers.multi_items():
        if header_key.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        response.headers.append(header_key, header_value)
    return response


def _emit_backend_failure(
    envelope_writer: EnvelopeWriter,
    agent_id: AgentId,
    reason: SystemInterfaceBackendFailureReason,
    status_code: int | None,
) -> None:
    """Emit a ``system_interface_backend_failure`` envelope on best-effort basis.

    The plugin never lets envelope-emission errors break a forwarded
    request -- if stdout is gone (parent died) we just log and continue.
    """
    try:
        payload = SystemInterfaceBackendFailurePayload(agent_id=agent_id, reason=reason, status_code=status_code)
        envelope_writer.emit_system_interface_backend_failure(payload)
    except (OSError, ValueError) as e:
        logger.trace("Could not emit system_interface_backend_failure envelope for {}: {}", agent_id, e)


# The proxy loader: the canonical "Loading workspace" page that re-attempts the
# workspace until the backend answers. A downstream consumer can reuse
# ``render_loading_page`` so its own loading page renders identically.
#
# It polls in the background (fetch) rather than full-reloading via a
# ``<meta http-equiv="refresh">``. A full-page reload of this view steals OS
# focus from any sibling Electron WebContentsView overlaying it -- e.g. the
# minds bug-report modal -- on every tick, which makes the overlay's inputs
# impossible to type into (the text already entered survives, but the textarea
# loses focus each second). Electron has no per-view focus-on-navigation /
# focusable control for WebContentsView to opt out of this; see
# https://github.com/electron/electron/issues/42578. Polling leaves the page
# (and so the focused overlay) untouched while waiting and only navigates once
# the workspace is actually reachable -- which also keeps the spinner smooth.
_LOADING_POLL_SCRIPT: Final[str] = """\
    <script>
      (function () {
        var INTERVAL_MS = 1000;
        function poll() {
          fetch(window.location.href, { credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
            .then(function (resp) {
              // 503 is this loader (the backend is still unreachable): keep
              // waiting. Any other status -- or an opaque redirect -- means the
              // workspace is answering, so navigate to render it for real.
              if (resp.status === 503) {
                setTimeout(poll, INTERVAL_MS);
              } else {
                window.location.reload();
              }
            }, function () {
              setTimeout(poll, INTERVAL_MS);
            });
        }
        setTimeout(poll, INTERVAL_MS);
      })();
    </script>
"""
_SERVICE_UNAVAILABLE_HTML = render_loading_page(body_extra=_LOADING_POLL_SCRIPT)


def _service_unavailable_response(request: Request) -> Response:
    """Return a 503 (styled loading page for browsers, plain text otherwise).

    Recovery navigation is driven by a consumer off the per-agent
    ``system_interface_backend_failure`` envelope, not by the plugin. That
    separation keeps the plugin origin-agnostic: it does not need to know
    where any consumer is listening. For browsers that hit the plugin
    directly (including users landing here mid-restart), we serve a styled
    loading page that polls the workspace in the background and reloads once
    it answers, so the experience is not a blank flash.
    """
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html:
        return HTMLResponse(content=_SERVICE_UNAVAILABLE_HTML, status_code=503)
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
    host_info: ParsedForwardHost,
    auth_store: AuthStoreInterface,
    use_http2: bool,
) -> Response:
    """Redeem a /goto/ token and set the workspace session cookie.

    The cookie is scoped with ``Domain=host-<hex>.localhost`` (the parsed
    ``workspace_domain`` of whichever origin the bridge ran on), so a single
    bridge hop authenticates the bare shell origin and every service origin
    (``<name>.host-<hex>.localhost``) at any depth.
    """
    token = request.query_params.get("token", "")
    next_url = _sanitize_next_url(request.query_params.get("next", "/"))
    signing_key = auth_store.get_signing_key()
    if not verify_subdomain_auth_token(
        token=token, signing_key=signing_key, workspace_host_id=str(host_info.host_id_str)
    ):
        return Response(status_code=403, content="Invalid or expired subdomain auth token")
    cookie_value = create_session_cookie(signing_key=signing_key)
    response = Response(status_code=302, headers={"Location": next_url})
    _set_forward_session_cookie(
        response,
        cookie_value=cookie_value,
        use_http2=use_http2,
        domain=str(host_info.workspace_domain),
    )
    return response


async def _handle_workspace_forward_http(
    request: Request,
    host_info: ParsedForwardHost,
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    tunnel_manager: SSHTunnelManager,
    http_client: httpx.AsyncClient,
    ssh_http_clients: dict[str, httpx.AsyncClient],
    ssh_http_clients_lock: threading.Lock,
    preauth_cookie_value: str | None,
    listen_port: int,
    allow_host_loopback: bool,
    envelope_writer: EnvelopeWriter,
    use_http2: bool,
    stall_notice_seconds: float,
) -> Response:
    if request.url.path == _SUBDOMAIN_AUTH_PATH:
        return _handle_subdomain_auth_bridge(request, host_info, auth_store, use_http2)

    if not _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        return _unauthenticated_subdomain_response(request, listen_port, use_http2, host_info)

    agent_id = resolver.resolve_agent_for_host(str(host_info.host_id_str))
    if agent_id is None:
        # No known agent on this host yet (discovery still warming up, or the
        # host is gone). There is no agent to attribute a failure envelope to,
        # so just serve the auto-retrying loader.
        return _service_unavailable_response(request)

    if host_info.service_name is None:
        # Bare origin: redirect HTML navigations to the shell service's own
        # label origin, keeping the local grammar identical to a share (where
        # the bare domain cannot be served at all). Non-HTML requests (the
        # workspace readiness probe, assets) fall through to serving the shell
        # directly, so nothing that is not a top-level navigation is disrupted.
        shell_label = resolver.shell_origin_label(agent_id)
        if shell_label is not None and "text/html" in request.headers.get("accept", ""):
            scheme = "https" if use_http2 else "http"
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            location = f"{scheme}://{shell_label}.{host_info.workspace_domain}:{listen_port}{next_path}"
            return Response(status_code=302, headers={"Location": location})
        target = resolver.resolve(agent_id)
    else:
        target = resolver.resolve_by_origin_label(agent_id, host_info.service_name)
    if target is None:
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.UNRESOLVED, None)
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
        # A stopped container fails here (its SSH endpoint is gone) rather
        # than at the resolver -- the resolver still holds a stale entry.
        # Emit a backend-failure envelope so a consumer can react (e.g. drive
        # its own recovery UI), and serve the same styled loader as the
        # UNRESOLVED path instead of raw error text.
        logger.warning("SSH tunnel setup failed for {}: {}", agent_id, e)
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
        return _service_unavailable_response(request)

    if tunnel_client is None and _is_loopback_url(backend_url) and not allow_host_loopback:
        # A loopback registered URL with no SSH tunnel is what a stopped
        # container looks like once discovery drops its SSH info: there is
        # nothing safe to dial. Treat it exactly like the SSH-tunnel setup
        # failure above -- emit a backend-failure envelope so a consumer can
        # react, and serve the styled loader instead of raw 502 error text.
        # (When allow_host_loopback is set the agent really runs on the host,
        # so that case never reaches here.)
        logger.warning(
            "Refusing to dial host loopback for agent {}: registered URL {} has no SSH tunnel "
            "(pass --allow-host-loopback if the agent really runs on the host).",
            agent_id,
            backend_url,
        )
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
        return _service_unavailable_response(request)

    active_client = tunnel_client or http_client
    return await _forward_workspace_http(
        request=request,
        backend_url=backend_url,
        http_client=active_client,
        agent_id=agent_id,
        envelope_writer=envelope_writer,
        stall_notice_seconds=stall_notice_seconds,
    )


async def _handle_workspace_forward_websocket(
    websocket: WebSocket,
    host_info: ParsedForwardHost,
    auth_store: AuthStoreInterface,
    resolver: ForwardResolver,
    tunnel_manager: SSHTunnelManager,
    preauth_cookie_value: str | None,
    allow_host_loopback: bool,
    envelope_writer: EnvelopeWriter,
) -> None:
    if not _is_authenticated(
        cookies=websocket.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        await websocket.close(code=4003, reason="Not authenticated")
        return

    agent_id = resolver.resolve_agent_for_host(str(host_info.host_id_str))
    if agent_id is None:
        await websocket.close(code=1013, reason="Backend not yet available")
        return

    # A websocket to a service origin routes by its label; to the bare origin
    # it maps to the shell (no redirect -- there is no navigation to redirect).
    if host_info.service_name is None:
        target = resolver.resolve(agent_id)
    else:
        target = resolver.resolve_by_origin_label(agent_id, host_info.service_name)
    if target is None:
        # Mirror the HTTP path: an unresolved backend is a backend failure a
        # consumer must hear about. A loaded SPA whose only live channel is a
        # websocket would otherwise leave minds blind to the dead workspace.
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.UNRESOLVED, None)
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
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
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
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
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
            logger.info("WS forward established for {} path={}", agent_id, websocket.url.path)
            client_to_backend = asyncio.create_task(
                _forward_client_to_backend(client_websocket=websocket, backend_ws=backend_ws)
            )
            backend_to_client = asyncio.create_task(
                _forward_backend_to_client(client_websocket=websocket, backend_ws=backend_ws, agent_id=agent_id)
            )
            # Race the two legs instead of awaiting both: when the backend leg
            # dies, the client->backend task would otherwise keep blocking on a
            # send-quiet client (the system interface's /api/ws sends nothing
            # after registration), leaving the client's socket half-open
            # forever -- the browser never learns the backend is gone and
            # silently stops receiving all real-time updates.
            try:
                done, pending = await asyncio.wait(
                    {client_to_backend, backend_to_client},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # asyncio.wait, unlike gather, does not cancel the awaited
                # tasks if this coroutine is itself cancelled (e.g. at server
                # shutdown), so cancel both here; the relay tasks must not
                # outlive the handler. A no-op on tasks that already finished.
                client_to_backend.cancel()
                backend_to_client.cancel()
            # return_exceptions captures the pending task's expected
            # CancelledError while still re-raising a cancellation of this
            # handler itself; anything else the task raised during teardown is
            # unexpected and must at least be logged.
            pending_results = await asyncio.gather(*pending, return_exceptions=True)
            for pending_result in pending_results:
                if isinstance(pending_result, BaseException) and not isinstance(
                    pending_result, asyncio.CancelledError
                ):
                    logger.warning("WS relay task for {} raised during teardown: {!r}", agent_id, pending_result)
            first_ended = "client" if client_to_backend in done else "backend"
            for task in done:
                # The relay helpers swallow all expected disconnect errors
                # themselves, so anything raised here is an unexpected bug and
                # must propagate instead of being dropped with the task object.
                task.result()
        # Close the client leg explicitly so the browser observes the close and
        # reconnects. A no-op if the client already disconnected.
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=1011, reason="Backend WebSocket closed")
            except RuntimeError:
                pass
        logger.info("WS forward ended for {} path={} ({} leg ended first)", agent_id, websocket.url.path, first_ended)
    except (
        ConnectionRefusedError,
        OSError,
        TimeoutError,
        SSHTunnelError,
        paramiko.SSHException,
        # A backend that dies part-way through the opening handshake surfaces as
        # ``InvalidHandshake``, which descends from ``WebSocketException`` rather
        # than ``OSError``. Caught at ``InvalidHandshake`` so a backend serving
        # the loader page instead of a 101 (``InvalidStatus``) lands here too,
        # but deliberately NOT at the wider ``WebSocketException``:
        # ``ConnectionClosed`` names a failure during relaying, which the block
        # above must keep propagating.
        websockets.exceptions.InvalidHandshake,
    ) as connection_error:
        logger.debug("Backend WS connection failed for {}: {}", agent_id, connection_error)
        _emit_backend_failure(envelope_writer, agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, None)
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
    use_http2: bool,
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
    _set_forward_session_cookie(response, cookie_value=cookie_value, use_http2=use_http2)
    return response


def _handle_browser_bridge(
    request: Request,
    auth_store: AuthStoreInterface,
    browser_bridge_token: str | None,
    use_http2: bool,
) -> Response:
    """Redeem the spawn-time browser-bridge token and set the bare-origin cookie.

    A host application (minds) that spawned the plugin with
    ``--browser-bridge-token`` 302s an already-authenticated browser here so
    it obtains a bare-origin plugin session without consuming an OTP -- the
    browser twin of the Electron shell's programmatic preauth cookie
    injection. The token compare is constant-time; when the flag was never
    passed the route does not exist (404).
    """
    if browser_bridge_token is None:
        return Response(status_code=404)
    token = request.query_params.get("token", "")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, and the
    # token comes straight from the query string.
    if not token or not secrets.compare_digest(token.encode("utf-8"), browser_bridge_token.encode("utf-8")):
        return Response(status_code=403, content="Invalid browser bridge token")
    next_url = _sanitize_next_url(request.query_params.get("next", "/"))
    signing_key = auth_store.get_signing_key()
    cookie_value = create_session_cookie(signing_key=signing_key)
    response = Response(status_code=302, headers={"Location": next_url})
    _set_forward_session_cookie(response, cookie_value=cookie_value, use_http2=use_http2)
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
        host_id = resolver.get_host_for_agent(agent_id)
        target = resolver.resolve(agent_id)
        if host_id is None:
            agents.append(
                {
                    "agent_id": str(agent_id),
                    "host_id": "",
                    "is_unresolved": True,
                    "reason": "(no host known yet)",
                }
            )
        elif target is None:
            agents.append(
                {
                    "agent_id": str(agent_id),
                    "host_id": host_id,
                    "is_unresolved": True,
                    "reason": "(no service URL yet)",
                }
            )
        else:
            agents.append({"agent_id": str(agent_id), "host_id": host_id, "is_unresolved": False, "reason": ""})
    html = _render_index_page(env, agents=agents, port=listen_port)
    return HTMLResponse(content=html)


def _handle_goto_workspace(
    host_id: str,
    request: Request,
    auth_store: AuthStoreInterface,
    preauth_cookie_value: str | None,
    listen_port: int,
    use_http2: bool,
) -> Response:
    if not _is_authenticated(
        cookies=request.cookies,
        auth_store=auth_store,
        preauth_cookie_value=preauth_cookie_value,
    ):
        return Response(status_code=302, headers={"Location": "/"})
    # ``HostId`` alone is too lax for a value interpolated into the redirect
    # hostname (``int(hex, 16)`` accepts newlines / underscores / ``0x`` and
    # preserves case, which the lowercased subdomain parse then never matches),
    # so require the exact ``host-<32hex>`` shape the subdomain pattern routes.
    normalized_host_id = host_id.lower()
    if _GOTO_HOST_ID_PATTERN.fullmatch(normalized_host_id) is None:
        return Response(status_code=404)
    parsed_id = HostId(normalized_host_id)
    # An optional ``service`` carries the dotted label chain of the origin
    # that bounced here (e.g. ``svc`` or ``sub.svc``) so the bridge -- and
    # its final redirect -- run on that exact origin. Only the LAST label is a
    # service name; deeper labels are that service's own sub-origin space,
    # which FORWARD_SUBDOMAIN_PATTERN routes with the looser hostname-label
    # charset (a sub-origin like ``a--b`` must bounce back to its own origin,
    # not 404 mid-login). Anything outside either rule is a crafted URL.
    service_param = request.query_params.get("service", "")
    service_host_prefix = ""
    if service_param:
        *sub_origin_labels, service_label = service_param.split(".")
        if any(_SUB_ORIGIN_LABEL_PATTERN.fullmatch(label) is None for label in sub_origin_labels):
            return Response(status_code=404)
        try:
            validated_service = ServiceLabel(service_label)
        except ValueError:
            return Response(status_code=404)
        service_host_prefix = ".".join([*sub_origin_labels, str(validated_service)]) + "."
    signing_key = auth_store.get_signing_key()
    token = create_subdomain_auth_token(signing_key=signing_key, workspace_host_id=str(parsed_id))
    next_url = _sanitize_next_url(request.query_params.get("next", "/"))
    encoded_next = quote(next_url, safe="")
    scheme = "https" if use_http2 else "http"
    location = (
        f"{scheme}://{service_host_prefix}{parsed_id}.localhost:{listen_port}"
        f"{_SUBDOMAIN_AUTH_PATH}?token={token}&next={encoded_next}"
    )
    return Response(status_code=302, headers={"Location": location})


# -- App factory + lifespan ------------------------------------------------


@asynccontextmanager
async def _managed_lifespan(
    inner_app: FastAPI,
    on_listening: Callable[[], None] | None,
) -> AsyncGenerator[None, None]:
    inner_app.state.http_client = httpx.AsyncClient(follow_redirects=False, timeout=_PROXY_TIMEOUT)
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
    use_http2: bool = False,
    browser_bridge_token: str | None = None,
    embedder_origins: tuple[EmbedderOrigin, ...] = (),
    stall_notice_seconds: float = _STALL_NOTICE_SECONDS,
) -> FastAPI:
    """Create the FastAPI app for ``mngr forward``.

    ``allow_host_loopback`` opts the proxy in to dialing host loopback when an
    agent's registered URL is loopback and no SSH tunnel exists. The default
    of ``False`` is the safe one: any non-DEV agent whose SSH info hasn't
    been published gets a 502 instead of silently serving whatever else is
    bound on the host's loopback at the registered port. Pass ``True`` only
    for setups that intentionally run agents directly on the host (the
    legacy ``LaunchMode.DEV`` flow).

    ``stall_notice_seconds`` is how long a backend may go without answering a
    buffered request before an advisory ``STALLED`` envelope is emitted (the
    SSE path arms no such timer -- it still fails outright at its own read
    budget). It never abandons the request: what ends one is its client
    disconnecting, behind which ``_PROXY_BACKSTOP_TIMEOUT_SECONDS`` bounds only
    how long the backend may stay silent, not the request as a whole.

    ``use_http2`` reflects whether the server terminates TLS (and negotiates
    HTTP/2); when set, the client-facing URLs this app constructs use
    ``https``/``wss`` and its session cookie is marked ``Secure``. It does not
    itself enable TLS -- the serve path does -- but the two must agree so the
    URLs the browser is told to visit match the scheme the socket speaks.

    ``browser_bridge_token`` enables the ``/_bridge`` route (see
    ``_handle_browser_bridge``); ``embedder_origins`` extends the
    ``frame-ancestors`` policy appended to every proxied workspace response
    beyond the default 'self' + workspace-family deny-external posture.
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
    app.state.use_http2 = use_http2

    @app.middleware("http")
    async def _subdomain_routing_middleware(request: Request, call_next: Any) -> Response:
        host_header = request.headers.get("host", "")
        host_info = parse_forward_host(host_header)
        if host_info is None:
            return await call_next(request)
        response = await _handle_workspace_forward_http(
            request=request,
            host_info=host_info,
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=tunnel_manager,
            http_client=app.state.http_client,
            ssh_http_clients=app.state.ssh_http_clients,
            ssh_http_clients_lock=app.state.ssh_http_clients_lock,
            preauth_cookie_value=preauth_cookie_value,
            listen_port=listen_port,
            allow_host_loopback=allow_host_loopback,
            envelope_writer=envelope_writer,
            use_http2=use_http2,
            stall_notice_seconds=stall_notice_seconds,
        )
        # The proxy owns embedding policy for every workspace origin: APPEND a
        # frame-ancestors CSP header (never modify what the service sent --
        # multiple CSP headers compose by intersection). This is the one
        # narrowly-blessed deviation from pure byte-forwarding.
        response.headers.append(
            "content-security-policy",
            build_frame_ancestors_policy(
                host_info=host_info,
                listen_port=listen_port,
                use_http2=use_http2,
                embedder_origins=embedder_origins,
            ),
        )
        return response

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
            use_http2=use_http2,
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

    @app.get(BROWSER_BRIDGE_PATH)
    def _browser_bridge(request: Request) -> Response:
        return _handle_browser_bridge(
            request=request,
            auth_store=auth_store,
            browser_bridge_token=browser_bridge_token,
            use_http2=use_http2,
        )

    @app.get("/goto/{host_id}/")
    @app.get("/goto/{host_id}")
    def _goto(host_id: str, request: Request) -> Response:
        return _handle_goto_workspace(
            host_id=host_id,
            request=request,
            auth_store=auth_store,
            preauth_cookie_value=preauth_cookie_value,
            listen_port=listen_port,
            use_http2=use_http2,
        )

    @app.websocket("/{path:path}")
    async def _subdomain_ws(websocket: WebSocket, path: str) -> None:
        del path
        host_header = websocket.headers.get("host", "")
        host_info = parse_forward_host(host_header)
        if host_info is None:
            await websocket.close(code=4004, reason="Unknown host")
            return
        await _handle_workspace_forward_websocket(
            websocket=websocket,
            host_info=host_info,
            auth_store=auth_store,
            resolver=resolver,
            tunnel_manager=tunnel_manager,
            preauth_cookie_value=preauth_cookie_value,
            allow_host_loopback=allow_host_loopback,
            envelope_writer=envelope_writer,
        )

    return app
