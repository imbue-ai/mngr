import json
from loguru import logger as _loguru_logger
import queue
import signal
import threading
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from imbue.claude_web_chat.agent_discovery import AgentInfo
from imbue.claude_web_chat.agent_discovery import discover_agents
from imbue.claude_web_chat.agent_discovery import send_message
from imbue.claude_web_chat.config import Config
from imbue.claude_web_chat.event_queues import AgentEventQueues
from imbue.claude_web_chat.models import AgentListItem
from imbue.claude_web_chat.models import AgentListResponse
from imbue.claude_web_chat.models import ErrorResponse
from imbue.claude_web_chat.models import SendMessageRequest
from imbue.claude_web_chat.models import SendMessageResponse
from imbue.claude_web_chat.plugins import get_plugin_manager
from imbue.claude_web_chat.session_watcher import AgentSessionWatcher

logger = _loguru_logger

STATIC_DIRECTORY = Path(__file__).parent / "static"

_FRONTEND_NOT_BUILT_HTML = (
    "<html><body><p>Frontend not built."
    " Run <code>npm run build</code> in <code>frontend/</code>.</p></body></html>"
)

# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    event_queues = AgentEventQueues()
    application.state.event_queues = event_queues
    application.state.watchers = {}

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=event_queues.broadcast)

    is_main_thread = threading.current_thread() is threading.main_thread()
    original_sigint_handler = None

    if is_main_thread:
        original_sigint_handler = signal.getsignal(signal.SIGINT)

        def _graceful_shutdown_handler(signum: int, frame: object) -> None:
            event_queues.shutdown()
            _stop_all_watchers(application)
            handler = original_sigint_handler
            if callable(handler):
                handler(signum, frame)  # type: ignore[arg-type]

        signal.signal(signal.SIGINT, _graceful_shutdown_handler)

    yield

    event_queues.shutdown()
    _stop_all_watchers(application)
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
    meta_tag = f'<meta name="claude-web-chat-base-path" content="{root_path}">'
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
        if config.javascript_plugin_basenames:
            html_content = _inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return HTMLResponse(html_content)
    return HTMLResponse(_FRONTEND_NOT_BUILT_HTML)


def _favicon() -> Response:
    favicon_path = STATIC_DIRECTORY / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=404)


def _list_agents_endpoint() -> JSONResponse:
    """List all mngr-managed agents."""
    agents = discover_agents()
    items = [
        AgentListItem(id=agent.id, name=agent.name, state=agent.state)
        for agent in agents
    ]
    return JSONResponse(content=AgentListResponse(agents=items).model_dump())


def _find_agent(agent_id: str) -> AgentInfo | None:
    """Find a specific agent by ID."""
    agents = discover_agents()
    for agent in agents:
        if agent.id == agent_id:
            return agent
    return None


def _agent_not_found_response(agent_id: str) -> JSONResponse:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return JSONResponse(content=error.model_dump(), status_code=404)


def _get_events(agent_id: str, request: Request) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id)
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
        # Return all events on initial load
        events = watcher.get_all_events()

    return JSONResponse(content={"events": events})


def _stream_events(agent_id: str, request: Request) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id)
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


def _send_message_endpoint(agent_id: str, send_message_request: SendMessageRequest) -> JSONResponse:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    success = send_message(agent_info.name, send_message_request.message)
    if not success:
        error = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}'")
        return JSONResponse(content=error.model_dump(), status_code=500)

    return JSONResponse(content=SendMessageResponse(status="ok").model_dump())


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


def create_application(config: Config | None = None) -> FastAPI:
    application = FastAPI(lifespan=_lifespan)
    application.state.config = config or Config()

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.endpoint(app=application)

    application.add_api_route("/", _index, methods=["GET"])
    application.add_api_route("/favicon.ico", _favicon, methods=["GET"])
    application.add_api_route("/api/agents", _list_agents_endpoint, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/events", _get_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/stream", _stream_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/message", _send_message_endpoint, methods=["POST"])
    application.add_api_route("/plugins/{basename}", _serve_static_file, methods=["GET"])

    assets_directory = STATIC_DIRECTORY / "assets"
    if assets_directory.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

    application.add_api_route("/{path:path}", _index, methods=["GET"])

    return application
