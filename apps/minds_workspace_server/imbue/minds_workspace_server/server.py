import json
import os
import queue
import signal
import socket
import threading
import traceback
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger as _loguru_logger
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.minds_workspace_server.agent_discovery import AgentInfo
from imbue.minds_workspace_server.agent_discovery import discover_agents
from imbue.minds_workspace_server.agent_discovery import read_claude_config_dir_from_env_file
from imbue.minds_workspace_server.agent_discovery import send_message
from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.event_queues import AgentEventQueues
from imbue.minds_workspace_server.iframe_log_writer import IframeLogWriter
from imbue.minds_workspace_server.models import AgentCreationError
from imbue.minds_workspace_server.models import AgentListItem
from imbue.minds_workspace_server.models import AgentListResponse
from imbue.minds_workspace_server.models import CreateAgentResponse
from imbue.minds_workspace_server.models import CreateChatRequest
from imbue.minds_workspace_server.models import CreateWorktreeRequest
from imbue.minds_workspace_server.models import DestroyAgentResponse
from imbue.minds_workspace_server.models import ErrorResponse
from imbue.minds_workspace_server.models import IframeLogsRequest
from imbue.minds_workspace_server.models import IframeLogsResponse
from imbue.minds_workspace_server.models import RandomNameResponse
from imbue.minds_workspace_server.models import SendMessageRequest
from imbue.minds_workspace_server.models import SendMessageResponse
from imbue.minds_workspace_server.plugins import get_plugin_manager
from imbue.minds_workspace_server.request_writer import write_refresh_request
from imbue.minds_workspace_server.service_dispatcher import register_service_routes
from imbue.minds_workspace_server.session_watcher import AgentSessionWatcher
from imbue.minds_workspace_server.sharing_proxy import SharingProxyError
from imbue.minds_workspace_server.sharing_proxy import get_sharing_status
from imbue.minds_workspace_server.sharing_proxy import request_sharing_edit
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster

_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

logger = _loguru_logger

STATIC_DIRECTORY = Path(__file__).parent / "static"

_FRONTEND_NOT_BUILT_HTML = (
    "<html><body><p>Frontend not built. Run <code>npm run build</code> in <code>frontend/</code>.</p></body></html>"
)

# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan.

    Reads ``application.state.preconfigured_agent_manager`` (set up by
    ``create_application``). When present, the lifespan reuses that
    manager and does not call ``start()`` / ``stop()`` -- this is the
    hook tests use to seed the service registry without spawning the
    real ``mngr observe`` pipeline. When absent, the lifespan builds a
    fresh manager and owns its lifecycle.
    """
    event_queues = AgentEventQueues()
    application.state.event_queues = event_queues
    application.state.watchers = {}

    preconfigured_agent_manager: AgentManager | None = application.state.preconfigured_agent_manager
    if preconfigured_agent_manager is None:
        broadcaster = WebSocketBroadcaster()
        agent_manager = AgentManager.build(broadcaster)
        agent_manager.start()
    else:
        agent_manager = preconfigured_agent_manager
        broadcaster = agent_manager.broadcaster

    application.state.broadcaster = broadcaster
    application.state.agent_manager = agent_manager

    # Single shared httpx client for the /service/<name>/ forwarding layer.
    application.state.http_client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
    )

    # Thread-safe JSONL writer for iframe console logs forwarded from the
    # Electron desktop client. Owned by the app so the file handle and
    # rotation state outlive individual requests.
    iframe_log_path = _get_host_dir() / "logs" / "iframe" / "events.jsonl"
    application.state.iframe_log_writer = IframeLogWriter(file_path=iframe_log_path)

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=event_queues.broadcast)

    is_main_thread = threading.current_thread() is threading.main_thread()
    original_sigint_handler = None

    if is_main_thread:
        original_sigint_handler = signal.getsignal(signal.SIGINT)

        def _graceful_shutdown_handler(signum: int, frame: object) -> None:
            event_queues.shutdown()
            broadcaster.shutdown()
            if preconfigured_agent_manager is None:
                agent_manager.stop()
            _stop_all_watchers(application)
            handler = original_sigint_handler
            if callable(handler):
                handler(signum, frame)  # type: ignore[arg-type]

        signal.signal(signal.SIGINT, _graceful_shutdown_handler)

    yield

    event_queues.shutdown()
    broadcaster.shutdown()
    if preconfigured_agent_manager is None:
        agent_manager.stop()
    _stop_all_watchers(application)
    await application.state.http_client.aclose()
    application.state.iframe_log_writer.close()
    if is_main_thread and original_sigint_handler is not None:
        signal.signal(signal.SIGINT, original_sigint_handler)


def _stop_all_watchers(application: FastAPI) -> None:
    watchers: dict[str, AgentSessionWatcher] = getattr(application.state, "watchers", {})
    for watcher in watchers.values():
        watcher.stop()
    watchers.clear()


def _get_or_create_watcher(request: Request, agent_info: AgentInfo) -> AgentSessionWatcher:
    """Get an existing watcher for an agent, or create one."""
    watchers: dict[str, AgentSessionWatcher] = request.app.state.watchers
    event_queues: AgentEventQueues = request.app.state.event_queues

    if agent_info.id in watchers:
        return watchers[agent_info.id]

    def on_events(agent_id: str, events: list[dict[str, Any]]) -> None:
        for event in events:
            event_queues.broadcast(agent_id, event)

    watcher = AgentSessionWatcher(
        agent_id=agent_info.id,
        agent_state_dir=agent_info.agent_state_dir,
        claude_config_dir=agent_info.claude_config_dir,
        on_events=on_events,
    )
    watchers[agent_info.id] = watcher
    watcher.start()
    return watcher


def _inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    meta_tag = f'<meta name="minds-workspace-server-base-path" content="{root_path}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _read_host_name() -> str:
    """Read the host name from $MNGR_HOST_DIR/data.json, falling back to socket.gethostname()."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        data_path = Path(host_dir) / "data.json"
        if data_path.exists():
            try:
                data = json.loads(data_path.read_text())
                name = data.get("host_name")
                if name:
                    return str(name)
            except (json.JSONDecodeError, OSError):
                pass
    return socket.gethostname()


def _inject_hostname_meta_tag(html_content: str) -> str:
    hostname = _read_host_name()
    meta_tag = f'<meta name="minds-workspace-server-hostname" content="{hostname}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _inject_plugin_script_tags(html_content: str, plugin_basenames: list[str], root_path: str) -> str:
    script_tags = "\n".join(f'<script src="{root_path}/plugins/{basename}"></script>' for basename in plugin_basenames)
    return html_content.replace("</body>", f"{script_tags}\n</body>")


def _index(request: Request) -> Response:
    index_path = STATIC_DIRECTORY / "index.html"
    if index_path.exists():
        config: Config = request.app.state.config
        root_path = request.scope.get("root_path", "").rstrip("/")
        html_content = index_path.read_text()
        html_content = _inject_base_path_meta_tag(html_content, root_path)
        html_content = _inject_hostname_meta_tag(html_content)
        html_content = _inject_agent_id_meta_tag(html_content)
        if config.javascript_plugin_basenames:
            html_content = _inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return HTMLResponse(html_content)
    return HTMLResponse(_FRONTEND_NOT_BUILT_HTML)


def _favicon() -> Response:
    favicon_path = STATIC_DIRECTORY / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=404)


def _discover_with_filters(request: Request) -> list[AgentInfo]:
    """Discover agents using the app-level filter configuration."""
    return discover_agents(
        provider_names=request.app.state.provider_names,
        include_filters=request.app.state.include_filters,
        exclude_filters=request.app.state.exclude_filters,
    )


def _list_agents_endpoint(request: Request) -> JSONResponse:
    """List all mngr-managed agents."""
    agents = _discover_with_filters(request)
    items = [AgentListItem(id=agent.id, name=agent.name, state=agent.state) for agent in agents]
    return JSONResponse(content=AgentListResponse(agents=items).model_dump())


def _get_host_dir() -> Path:
    """Get the mngr host directory from the environment."""
    return Path(os.environ.get("MNGR_HOST_DIR", str(Path.home() / ".mngr")))


def _find_agent(agent_id: str, request: Request) -> AgentInfo | None:
    """Find a specific agent by ID.

    Uses the AgentManager's already-loaded state instead of running a full
    mngr discovery on every request.  Falls back to the agent state directory
    for claude_config_dir resolution.
    """
    agent_manager: AgentManager = request.app.state.agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        return None

    host_dir = _get_host_dir()
    agent_state_dir = host_dir / "agents" / agent_id
    claude_config_dir = read_claude_config_dir_from_env_file(agent_state_dir)

    return AgentInfo(
        id=agent_state.id,
        name=agent_state.name,
        state=agent_state.state,
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        labels=agent_state.labels,
        work_dir=agent_state.work_dir,
    )


def _agent_not_found_response(agent_id: str) -> JSONResponse:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return JSONResponse(content=error.model_dump(), status_code=404)


def _get_events(agent_id: str, request: Request) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = _get_or_create_watcher(request, agent_info)

    # Check for backfill parameters
    before_event_id = request.query_params.get("before")
    limit_str = request.query_params.get("limit", str(_DEFAULT_TAIL_COUNT))
    try:
        limit = int(limit_str)
    except ValueError:
        limit = _DEFAULT_TAIL_COUNT

    if before_event_id:
        events = watcher.get_backfill_events(before_event_id, limit=limit)
    else:
        # Return only main-session events (not subagent events)
        events = watcher.get_all_events()

    return JSONResponse(content={"events": events})


def _stream_events(agent_id: str, request: Request) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    _get_or_create_watcher(request, agent_info)

    event_queues: AgentEventQueues = request.app.state.event_queues
    event_queue = event_queues.register(agent_id)

    def event_generator() -> Iterator[str]:
        keepalive_counter = 0
        try:
            while not event_queues.is_shutdown:
                try:
                    event = event_queue.get(timeout=1)
                    keepalive_counter = 0
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    keepalive_counter += 1
                    if keepalive_counter >= 8:
                        keepalive_counter = 0
                        yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            event_queues.unregister(agent_id, event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _send_message_endpoint(agent_id: str, send_message_request: SendMessageRequest, request: Request) -> JSONResponse:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    success = send_message(agent_info.name, send_message_request.message)
    if not success:
        error = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}' (0 successful agents)")
        return JSONResponse(content=error.model_dump(), status_code=500)

    return JSONResponse(content=SendMessageResponse(status="ok").model_dump())


def _get_subagent_events(agent_id: str, subagent_session_id: str, request: Request) -> Response:
    """Get events for a specific subagent session."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = _get_or_create_watcher(request, agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return JSONResponse(content={"events": events, "metadata": metadata})


def _stream_subagent_events(agent_id: str, subagent_session_id: str, request: Request) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    _get_or_create_watcher(request, agent_info)

    event_queues: AgentEventQueues = request.app.state.event_queues
    event_queue = event_queues.register(agent_id)

    def event_generator() -> Iterator[str]:
        keepalive_counter = 0
        try:
            while not event_queues.is_shutdown:
                try:
                    event = event_queue.get(timeout=1)
                    if event is None:
                        break
                    # Only forward events from this subagent's session
                    if event.get("session_id") == subagent_session_id:
                        keepalive_counter = 0
                        yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    keepalive_counter += 1
                    if keepalive_counter >= 8:
                        keepalive_counter = 0
                        yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            event_queues.unregister(agent_id, event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


_LAYOUT_FILENAME = "layout.json"


def _primary_agent_layout_dir() -> Path | None:
    """Return the workspace layout directory for this workspace's primary agent.

    The workspace_server always serves a single workspace (its own primary
    agent); the layout lives at $MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/.
    Returns None if either env var is missing, which should only happen in
    dev/test setups that don't care about persistence.
    """
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        return None
    return _get_host_dir() / "agents" / agent_id / "workspace_layout"


def _get_layout() -> Response:
    """Get the saved workspace layout for this workspace's primary agent."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return JSONResponse(content=None, status_code=404)

    layout_file = layout_dir / _LAYOUT_FILENAME
    if not layout_file.exists():
        return JSONResponse(content=None, status_code=404)

    try:
        layout_data = json.loads(layout_file.read_text())
        return JSONResponse(content=layout_data)
    except (json.JSONDecodeError, OSError):
        return JSONResponse(content=None, status_code=404)


async def _save_layout(request: Request) -> Response:
    """Save the workspace layout for this workspace's primary agent."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return JSONResponse(content=error.model_dump(), status_code=500)

    try:
        body = await request.body()
        # Validate it's valid JSON
        json.loads(body)
    except (json.JSONDecodeError, ValueError):
        error = ErrorResponse(detail="Invalid JSON in request body")
        return JSONResponse(content=error.model_dump(), status_code=400)

    layout_dir.mkdir(parents=True, exist_ok=True)
    layout_file = layout_dir / _LAYOUT_FILENAME
    layout_file.write_bytes(body)

    return JSONResponse(content={"status": "ok"})


async def _get_screen_capture(agent_id: str, request: Request) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_info.name}"
    include_scrollback = request.query_params.get("scrollback", "false").lower() == "true"
    scrollback_flag = ["-S", "-"] if include_scrollback else []
    command = ["tmux", "capture-pane", "-t", session_name, *scrollback_flag, "-p"]

    def _run_capture() -> tuple[bool, str]:
        result = run_local_command_modern_version(
            command=command,
            cwd=None,
            is_checked=False,
            timeout=5.0,
        )
        succeeded = result.returncode == 0
        return succeeded, result.stdout if succeeded else result.stderr

    success, output = await run_in_threadpool(_run_capture)
    if not success:
        return JSONResponse(
            content={"screen": None, "error": f"tmux session not found: {session_name}"},
            status_code=200,
        )
    return JSONResponse(content={"screen": output})


def _serve_static_file(basename: str, request: Request) -> Response:
    config: Config = request.app.state.config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        error = ErrorResponse(detail=f"Static file '{basename}' not found")
        return JSONResponse(content=error.model_dump(), status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        error = ErrorResponse(detail=f"Static file not found on disk: {file_path}")
        return JSONResponse(content=error.model_dump(), status_code=404)
    return FileResponse(file_path)


def _random_name_endpoint(request: Request) -> JSONResponse:
    """Generate a random agent name."""
    agent_manager: AgentManager = request.app.state.agent_manager
    name = agent_manager.generate_random_name()
    return JSONResponse(content=RandomNameResponse(name=name).model_dump())


async def _create_worktree_agent(request: Request) -> JSONResponse:
    """Create a new worktree agent."""
    agent_manager: AgentManager = request.app.state.agent_manager
    body = await request.json()

    try:
        create_request = CreateWorktreeRequest(**body)
        agent_name = create_request.name
        selected_agent_id = create_request.selected_agent_id or agent_manager.get_own_agent_id()
        agent_id = agent_manager.create_worktree_agent(agent_name, selected_agent_id)
        return JSONResponse(
            content=CreateAgentResponse(agent_id=agent_id).model_dump(),
            status_code=201,
        )
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=400)


async def _create_chat_agent(request: Request) -> JSONResponse:
    """Create a new chat agent in the primary agent's work directory."""
    agent_manager: AgentManager = request.app.state.agent_manager
    body = await request.json()

    try:
        create_request = CreateChatRequest(**body)
        agent_id = agent_manager.create_chat_agent(create_request.name)
        return JSONResponse(
            content=CreateAgentResponse(agent_id=agent_id).model_dump(),
            status_code=201,
        )
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=400)


async def _ws_endpoint(websocket: WebSocket) -> None:
    """Unified WebSocket for agent state and application updates."""
    await websocket.accept()
    agent_manager: AgentManager = websocket.app.state.agent_manager
    ws_broadcaster: WebSocketBroadcaster = websocket.app.state.broadcaster

    client_queue = ws_broadcaster.register()
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "agents_updated",
                    "agents": agent_manager.get_agents_serialized(),
                }
            )
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "applications_updated",
                    "applications": agent_manager.get_applications_serialized(),
                }
            )
        )

        for proto in agent_manager.get_proto_agents():
            await websocket.send_text(json.dumps({"type": "proto_agent_created", **proto}))

        shutdown = False
        while not shutdown:
            try:
                message = await run_in_threadpool(client_queue.get, timeout=1.0)
                if message is None:
                    shutdown = True
                else:
                    await websocket.send_text(message)
            except queue.Empty:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        ws_broadcaster.unregister(client_queue)


async def _proto_agent_logs_endpoint(websocket: WebSocket) -> None:
    """WebSocket for streaming proto-agent creation logs."""
    await websocket.accept()
    agent_manager: AgentManager = websocket.app.state.agent_manager
    agent_id = websocket.path_params.get("agent_id", "")

    log_queue = agent_manager.get_log_queue(agent_id)
    if log_queue is None:
        await websocket.send_text(json.dumps({"done": True, "success": False, "error": "Proto-agent not found"}))
        await websocket.close()
        return

    try:
        finished = False
        while not finished:
            try:
                message = await run_in_threadpool(log_queue.get, timeout=1.0)
                if message is None:
                    finished = True
                else:
                    await websocket.send_text(message)
            except queue.Empty:
                continue
    except WebSocketDisconnect:
        pass


async def _destroy_agent(agent_id: str, request: Request) -> JSONResponse:
    """Destroy an agent by running mngr destroy --force."""
    agent_manager: AgentManager = request.app.state.agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return JSONResponse(content=error.model_dump(), status_code=404)

    agent_name = agent_state.name

    def _run_destroy() -> tuple[bool, str]:
        result = run_local_command_modern_version(
            command=["mngr", "destroy", agent_name, "--force"],
            cwd=None,
            is_checked=False,
            timeout=30.0,
        )
        succeeded = result.returncode == 0
        output = result.stdout.strip() if succeeded else result.stderr.strip()
        return succeeded, output

    success, output = await run_in_threadpool(_run_destroy)
    if not success:
        error = ErrorResponse(detail=f"Failed to destroy agent '{agent_name}': {output}")
        return JSONResponse(content=error.model_dump(), status_code=500)

    # Remove the agent from the workspace server's tracked state immediately
    # so the frontend reflects the destruction without waiting for mngr observe.
    agent_manager.remove_agent(agent_id)

    return JSONResponse(content=DestroyAgentResponse(status="ok").model_dump())


async def _get_sharing_status_endpoint(service_name: str) -> JSONResponse:
    """Get the Cloudflare forwarding status for a server."""
    try:
        status = await run_in_threadpool(get_sharing_status, service_name)
        return JSONResponse(content=status.model_dump())
    except SharingProxyError as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=502)


async def _request_sharing_edit_endpoint(service_name: str) -> JSONResponse:
    """Create a sharing request event for editing sharing settings.

    Writes a request event to requests/events.jsonl so the desktop client
    can handle the actual sharing changes. Returns success immediately.
    """
    try:
        await run_in_threadpool(request_sharing_edit, service_name, True)
        return JSONResponse(content={"ok": True, "message": "Sharing request sent"})
    except (SharingProxyError, RuntimeError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=502)


async def _refresh_service_request_endpoint(service_name: str) -> JSONResponse:
    """Append a refresh-service event to the agent's refresh events file.

    Called by agents inside the container to tell the minds desktop client
    that an open web-service tab should reload. The desktop client picks the
    event up via ``mngr event --follow`` and POSTs back to the broadcast
    endpoint below.
    """
    try:
        await run_in_threadpool(write_refresh_request, service_name)
        return JSONResponse(content={"ok": True})
    except (RuntimeError, OSError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=500)


async def _refresh_service_broadcast_endpoint(service_name: str, request: Request) -> JSONResponse:
    """Broadcast a refresh_service WebSocket message for the given service_name.

    Called by the desktop client after it observes a refresh event on the
    mngr event stream. Locked to loopback clients since no authentication
    exists between the desktop client and the workspace server inside the
    container.
    """
    client_host = request.client.host if request.client is not None else ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="refresh-service broadcast is only callable from loopback")
        return JSONResponse(content=error.model_dump(), status_code=403)

    broadcaster: WebSocketBroadcaster = request.app.state.broadcaster
    broadcaster.broadcast_refresh_service(service_name)
    return JSONResponse(content={"ok": True})


def _inject_agent_id_meta_tag(html_content: str) -> str:
    """Inject the primary agent ID as a meta tag for the frontend."""
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    meta_tag = f'<meta name="minds-workspace-server-agent-id" content="{agent_id}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


async def _post_iframe_logs(request: Request) -> JSONResponse:
    """Persist a batch of renderer console logs forwarded by the Electron client.

    The Electron main process captures ``console-message`` events for
    agent-owned service iframes (URLs starting with ``/service/<name>/``)
    and POSTs them here. Records are appended to
    ``<host_dir>/logs/iframe/events.jsonl`` via ``IframeLogWriter``.
    """
    try:
        body = await request.json()
        payload = IframeLogsRequest(**body)
    except (ValueError, TypeError) as exc:
        error = ErrorResponse(detail=f"Invalid iframe-logs payload: {exc}")
        return JSONResponse(content=error.model_dump(), status_code=400)

    writer: IframeLogWriter = request.app.state.iframe_log_writer
    records_as_dicts = [record.model_dump() for record in payload.records]
    written = await run_in_threadpool(writer.write_records, records_as_dicts)
    return JSONResponse(content=IframeLogsResponse(written=written).model_dump())


def create_application(
    config: Config | None = None,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    agent_manager: AgentManager | None = None,
) -> FastAPI:
    application = FastAPI(lifespan=_lifespan)

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.url.path, exc, "".join(tb))
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error: {exc}"},
        )

    application.state.preconfigured_agent_manager = agent_manager
    application.state.config = config or Config()
    application.state.provider_names = provider_names
    application.state.include_filters = include_filters
    application.state.exclude_filters = exclude_filters

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.endpoint(app=application)

    application.add_api_route("/", _index, methods=["GET"])
    application.add_api_route("/favicon.ico", _favicon, methods=["GET"])
    application.add_api_route("/api/agents", _list_agents_endpoint, methods=["GET"])
    application.add_api_route("/api/agents/create-worktree", _create_worktree_agent, methods=["POST"])
    application.add_api_route("/api/agents/create-chat", _create_chat_agent, methods=["POST"])
    application.add_api_route("/api/random-name", _random_name_endpoint, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/events", _get_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/stream", _stream_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/message", _send_message_endpoint, methods=["POST"])
    application.add_api_route("/api/layout", _get_layout, methods=["GET"])
    application.add_api_route("/api/layout", _save_layout, methods=["POST"])
    application.add_api_route("/api/agents/{agent_id}/screen", _get_screen_capture, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/destroy", _destroy_agent, methods=["POST"])
    application.add_api_route("/api/sharing/{service_name}", _get_sharing_status_endpoint, methods=["GET"])
    application.add_api_route("/api/sharing/{service_name}/request", _request_sharing_edit_endpoint, methods=["POST"])
    application.add_api_route("/api/iframe-logs", _post_iframe_logs, methods=["POST"])
    application.add_api_route(
        "/api/refresh-service/{service_name}", _refresh_service_request_endpoint, methods=["POST"]
    )
    application.add_api_route(
        "/api/refresh-service/{service_name}/broadcast", _refresh_service_broadcast_endpoint, methods=["POST"]
    )
    application.add_api_route(
        "/api/agents/{agent_id}/subagents/{subagent_session_id}/events", _get_subagent_events, methods=["GET"]
    )
    application.add_api_route(
        "/api/agents/{agent_id}/subagents/{subagent_session_id}/stream", _stream_subagent_events, methods=["GET"]
    )
    application.add_api_websocket_route("/api/ws", _ws_endpoint)
    application.add_api_websocket_route("/api/proto-agents/{agent_id}/logs", _proto_agent_logs_endpoint)
    application.add_api_route("/plugins/{basename}", _serve_static_file, methods=["GET"])

    assets_directory = STATIC_DIRECTORY / "assets"
    if assets_directory.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

    # Service forwarding routes: /service/<name>/... forwards to the service's
    # local backend (from runtime/applications.toml) with path rewriting,
    # cookie scoping, WS shim, and a scoped service worker.
    register_service_routes(application)

    application.add_api_route("/{path:path}", _index, methods=["GET"])

    return application
