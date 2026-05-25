"""REST API v1 router for the minds desktop client.

Every route under ``/api/v1/`` requires ``Authorization: Bearer <key>``
where ``<key>`` is the central :mod:`api_key_store` minds API key. The
latchkey gateway's bundled ``minds-api-proxy`` extension injects that
header on every forwarded request, so a caller (an agent in a
workspace) reaches us by hitting ``$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...``.

Agent identity, when a route needs it, comes from the URL path's
``{agent_id}`` parameter -- *not* from the bearer token. The gateway's
per-host permissions file is what gates which agent ids a given caller
can talk about: at agent creation time the desktop client narrows the
host's permission rule to ``/minds-api-proxy/api/v1/agents/<agent_id>/...``,
so a request that reaches a route with a given ``{agent_id}`` has
already been authorized by the gateway as "this is an agent that lives
on the caller's host".
"""

import json

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import Response

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_auth import MindsApiAuthDep
from imbue.minds.desktop_client.deps import BackendResolverDep
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.telegram.credential_store import load_agent_bot_credentials
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _json_error(message: str, status_code: int) -> Response:
    return _json_response({"error": message}, status_code=status_code)


# -- Telegram routes --


async def _handle_telegram_setup(
    agent_id: str,
    request: Request,
    _auth: MindsApiAuthDep,
) -> Response:
    """Start Telegram bot setup for an agent."""
    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return _json_error("Telegram setup not configured", 501)

    parsed_id = AgentId(agent_id)

    agent_name = str(parsed_id)[:8]
    try:
        body = await request.json()
        raw_name = body.get("agent_name", agent_name)
        agent_name = str(raw_name).strip() if raw_name else agent_name
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    telegram_orchestrator.start_setup(agent_id=parsed_id, agent_name=agent_name)
    return _json_response(
        {
            "agent_id": str(parsed_id),
            "status": str(TelegramSetupStatus.CHECKING_CREDENTIALS),
        }
    )


def _handle_telegram_status(
    agent_id: str,
    _auth: MindsApiAuthDep,
    request: Request,
) -> Response:
    """Get Telegram setup status for an agent."""
    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return _json_error("Telegram setup not configured", 501)

    parsed_id = AgentId(agent_id)
    info = telegram_orchestrator.get_setup_info(parsed_id)

    if info is None:
        is_active = telegram_orchestrator.agent_has_telegram(parsed_id)
        if is_active:
            paths: WorkspacePaths = request.app.state.api_v1_paths
            credentials = load_agent_bot_credentials(paths.data_dir, parsed_id)
            result: dict[str, object] = {
                "agent_id": str(parsed_id),
                "status": str(TelegramSetupStatus.DONE),
            }
            if credentials is not None and credentials.bot_username is not None:
                result["bot_username"] = credentials.bot_username
            return _json_response(result)
        return _json_error("No Telegram setup in progress for this agent", 404)

    result: dict[str, object] = {
        "agent_id": str(info.agent_id),
        "status": str(info.status),
    }
    if info.error is not None:
        result["error"] = info.error
    if info.bot_username is not None:
        result["bot_username"] = info.bot_username
    return _json_response(result)


# -- Notification route --


async def _handle_notification(
    agent_id: str,
    request: Request,
    _auth: MindsApiAuthDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Send a notification on behalf of the named agent."""
    dispatcher: NotificationDispatcher | None = request.app.state.notification_dispatcher
    if dispatcher is None:
        return _json_error("Notification dispatch not configured", 501)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("Invalid JSON body", 400)

    if not isinstance(body, dict):
        return _json_error("Request body must be a JSON object", 400)

    message = body.get("message")
    if not message or not isinstance(message, str):
        return _json_error("'message' field is required and must be a string", 400)

    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _json_error("'title' field must be a string", 400)
    urgency_str = body.get("urgency", "NORMAL")
    try:
        urgency = NotificationUrgency(urgency_str.upper())
    except (ValueError, AttributeError):
        return _json_error(f"Invalid urgency: {urgency_str}. Must be one of: low, normal, critical", 400)

    parsed_agent_id = AgentId(agent_id)
    notification_request = NotificationRequest(
        message=message,
        title=title,
        urgency=urgency,
    )

    agent_info = backend_resolver.get_agent_display_info(parsed_agent_id)
    agent_display_name = agent_info.agent_name if agent_info else str(parsed_agent_id)

    dispatcher.dispatch(notification_request, agent_display_name)
    return _json_response({"ok": True})


# -- Router factory --


def create_api_v1_router() -> APIRouter:
    """Create the /api/v1/ router with all REST API endpoints."""
    router = APIRouter()

    # Telegram
    router.post("/agents/{agent_id}/telegram")(_handle_telegram_setup)
    router.get("/agents/{agent_id}/telegram")(_handle_telegram_status)

    # Notifications (per-agent so the gateway's per-host permission file
    # can restrict each caller to its own agent ids).
    router.post("/agents/{agent_id}/notifications")(_handle_notification)

    return router
