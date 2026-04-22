"""Route handlers for ``/service/<name>/...`` forwarding inside minds_workspace_server.

Mirrors the pattern used by the desktop client's ``/forwarding/...`` routes
but strictly local (all target services run on 127.0.0.1 inside the same
workspace, so no SSH tunnel logic is needed) and without agent-id in the
path (one workspace per workspace_server process).

Responsibilities:
- First-navigation HTML requests serve a bootstrap page that registers a
  scoped service worker at ``/service/<name>/``. The SW then transparently
  prepends the prefix to fetches issued by the service's own frontend.
- Subsequent HTTP requests forward to the backend, rewriting absolute
  paths in HTML and scoping ``Set-Cookie`` headers under the prefix.
- WebSocket requests forward bidirectionally with subprotocol passthrough.
- Requests for unknown or not-yet-registered services show the
  auto-retrying loading page (HTML accept) or return 502 (otherwise).
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Final

import httpx
import websockets
import websockets.asyncio.client
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from loguru import logger
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets import ClientConnection

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.primitives import ServiceName
from imbue.minds_workspace_server.proxy import generate_backend_loading_html
from imbue.minds_workspace_server.proxy import generate_bootstrap_html
from imbue.minds_workspace_server.proxy import generate_service_worker_js
from imbue.minds_workspace_server.proxy import rewrite_cookie_path
from imbue.minds_workspace_server.proxy import rewrite_location_header
from imbue.minds_workspace_server.proxy import rewrite_proxied_html

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0

_EXCLUDED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "transfer-encoding",
        "content-encoding",
        "content-length",
    }
)


def _sw_cookie_name(service_name: str) -> str:
    return f"sw_installed_{service_name}"


def _make_loading_html(current_service: ServiceName, agent_manager: AgentManager) -> str:
    other_services = tuple(
        ServiceName(name) for name in agent_manager.list_service_names() if name != str(current_service)
    )
    return generate_backend_loading_html(
        current_service=current_service,
        other_services=other_services,
    )


async def _forward_http_request(
    request: Request,
    backend_url: str,
    path: str,
    service_name: str,
    http_client: httpx.AsyncClient,
) -> httpx.Response | Response:
    """Forward an HTTP request to the backend, returning the backend response or an error Response."""
    proxy_url = f"{backend_url.rstrip('/')}/{path}"
    if request.url.query:
        proxy_url = f"{proxy_url}?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    try:
        return await http_client.request(
            method=request.method,
            url=proxy_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.warning("Backend connection refused for service {}", service_name)
        return Response(status_code=502, content="Backend connection refused")
    except httpx.ReadError:
        logger.warning("Backend connection lost for service {}", service_name)
        return Response(status_code=502, content="Backend connection lost")
    except httpx.RemoteProtocolError:
        logger.warning("Backend disconnected without response for service {} (likely still starting)", service_name)
        return Response(status_code=502, content="Backend disconnected without response")
    except httpx.TimeoutException:
        logger.warning("Backend request timed out for service {}", service_name)
        return Response(status_code=504, content="Backend request timed out")


async def _forward_http_request_streaming(
    request: Request,
    backend_url: str,
    path: str,
    service_name: str,
    http_client: httpx.AsyncClient,
) -> Response:
    """Forward an HTTP request and stream the response back without buffering.

    Used for SSE (Server-Sent Events) endpoints where the backend sends data
    incrementally and the client needs to receive it as it arrives.
    """
    proxy_url = f"{backend_url.rstrip('/')}/{path}"
    if request.url.query:
        proxy_url = f"{proxy_url}?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    try:
        backend_stream = http_client.stream(
            method=request.method,
            url=proxy_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.debug("Backend connection refused for service {} (streaming)", service_name)
        return Response(status_code=502, content="Backend connection refused")

    async def _stream_generator() -> AsyncGenerator[bytes, None]:
        try:
            async with backend_stream as response:
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.ConnectError:
            logger.debug("Backend connection lost during streaming for service {}", service_name)
        except httpx.ReadError:
            logger.debug("Backend read error during streaming for service {}", service_name)
        except httpx.RemoteProtocolError:
            logger.debug("Backend disconnected without response during streaming for service {}", service_name)
        except httpx.TimeoutException:
            logger.debug("Backend stream timed out for service {}", service_name)

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
    service_name: ServiceName,
) -> Response:
    """Transform a backend httpx response into a FastAPI Response with header/content rewriting."""
    resp_headers: dict[str, list[str]] = {}
    for header_key, header_value in backend_response.headers.multi_items():
        header_key_lower = header_key.lower()
        if header_key_lower in _EXCLUDED_RESPONSE_HEADERS:
            continue
        if header_key_lower == "set-cookie":
            rewritten_value = rewrite_cookie_path(
                set_cookie_header=header_value,
                service_name=service_name,
            )
        elif header_key_lower == "location":
            rewritten_value = rewrite_location_header(
                location_header=header_value,
                service_name=service_name,
            )
        else:
            rewritten_value = header_value
        resp_headers.setdefault(header_key, [])
        resp_headers[header_key].append(rewritten_value)

    content: str | bytes = backend_response.content

    content_type = backend_response.headers.get("content-type", "")
    if "text/html" in content_type:
        html_text = backend_response.text
        rewritten_html = rewrite_proxied_html(
            html_content=html_text,
            service_name=service_name,
        )
        content = rewritten_html.encode()

    response = Response(content=content, status_code=backend_response.status_code)
    for header_key, header_values in resp_headers.items():
        for header_value in header_values:
            response.headers.append(header_key, header_value)
    return response


async def _handle_service_sw_js(service_name: str) -> Response:
    """Serve the scoped service worker script for a service."""
    return Response(
        content=generate_service_worker_js(ServiceName(service_name)),
        media_type="application/javascript",
    )


async def _handle_service_http(
    service_name: str,
    path: str,
    request: Request,
) -> Response:
    """Handle an HTTP request under ``/service/<name>/<path>``."""
    parsed_service = ServiceName(service_name)
    agent_manager: AgentManager = request.app.state.agent_manager

    if path == "__sw.js":
        return await _handle_service_sw_js(service_name)

    is_navigation = request.headers.get("sec-fetch-mode") == "navigate"

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(content=_make_loading_html(parsed_service, agent_manager))
        return Response(status_code=502, content=f"Service '{service_name}' not registered")

    sw_cookie = request.cookies.get(_sw_cookie_name(service_name))

    if is_navigation and not sw_cookie:
        return HTMLResponse(generate_bootstrap_html(parsed_service))

    http_client: httpx.AsyncClient = request.app.state.http_client

    accept = request.headers.get("accept", "")
    is_likely_sse = "text/event-stream" in accept

    if is_likely_sse:
        return await _forward_http_request_streaming(
            request=request,
            backend_url=backend_url,
            path=path,
            service_name=service_name,
            http_client=http_client,
        )

    result = await _forward_http_request(
        request=request,
        backend_url=backend_url,
        path=path,
        service_name=service_name,
        http_client=http_client,
    )

    if isinstance(result, Response):
        if result.status_code >= 500 and "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(content=_make_loading_html(parsed_service, agent_manager))
        return result

    return _build_proxy_response(
        backend_response=result,
        service_name=parsed_service,
    )


async def _forward_client_to_backend(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
) -> None:
    """Forward messages from the client WebSocket to the backend."""
    connected = True
    try:
        while connected:
            data = await client_websocket.receive()
            msg_type = data.get("type", "")
            if msg_type == "websocket.disconnect":
                connected = False
            elif "text" in data:
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
    service_name: str,
) -> None:
    """Forward messages from the backend WebSocket to the client."""
    try:
        async for msg in backend_ws:
            if isinstance(msg, str):
                await client_websocket.send_text(msg)
            else:
                await client_websocket.send_bytes(msg)
    except websockets.exceptions.ConnectionClosed:
        logger.debug("Backend WebSocket closed for service {}", service_name)
    except RuntimeError as e:
        logger.trace("Client WebSocket send error (likely post-disconnect): {}", e)


async def _handle_service_websocket(
    websocket: WebSocket,
    service_name: str,
    path: str,
) -> None:
    """Proxy a WebSocket connection under ``/service/<name>/<path>`` to the backend service."""
    agent_manager: AgentManager = websocket.app.state.agent_manager

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        await websocket.close(code=4004, reason=f"Unknown service: {service_name}")
        return

    ws_backend = backend_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    ws_url = f"{ws_backend}/{path}"
    if websocket.url.query:
        ws_url = f"{ws_url}?{websocket.url.query}"

    client_subprotocol_header = websocket.headers.get("sec-websocket-protocol")
    subprotocols: list[str] = []
    if client_subprotocol_header:
        subprotocols = [s.strip() for s in client_subprotocol_header.split(",")]
    ws_subprotocols = [websockets.Subprotocol(s) for s in subprotocols] if subprotocols else None

    try:
        async with websockets.connect(ws_url, subprotocols=ws_subprotocols) as backend_ws:
            await websocket.accept(subprotocol=backend_ws.subprotocol)
            await asyncio.gather(
                _forward_client_to_backend(client_websocket=websocket, backend_ws=backend_ws),
                _forward_backend_to_client(
                    client_websocket=websocket, backend_ws=backend_ws, service_name=service_name
                ),
            )
    except (ConnectionRefusedError, OSError, TimeoutError) as connection_error:
        logger.debug("Backend WebSocket connection failed for service {}: {}", service_name, connection_error)
        try:
            await websocket.close(code=1011, reason="Backend connection failed")
        except RuntimeError:
            logger.trace("WebSocket already closed when trying to send error for service {}", service_name)


def register_service_routes(application: FastAPI) -> None:
    """Register ``/service/<name>/...`` HTTP + WebSocket routes on the application."""
    application.add_api_route(
        "/service/{service_name}/{path:path}",
        _handle_service_http,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )

    @application.websocket("/service/{service_name}/{path:path}")
    async def service_websocket(websocket: WebSocket, service_name: str, path: str) -> None:
        await _handle_service_websocket(websocket=websocket, service_name=service_name, path=path)
