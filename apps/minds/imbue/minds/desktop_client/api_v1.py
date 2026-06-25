"""REST API v1 blueprint for the minds desktop client.

Every route under ``/api/v1/`` requires ``Authorization: Bearer <key>``
where ``<key>`` is the central :mod:`api_key_store` minds API key. The
latchkey gateway's bundled ``minds-api-proxy`` extension injects that
header on every forwarded request, so a caller (an agent in a
workspace) reaches us by hitting ``$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...``.

Agent identity, when a route needs it, comes from the URL path's
``<agent_id>`` parameter -- *not* from the bearer token. The gateway's
per-host permissions file is what gates which agent ids a given caller
can talk about: at agent creation time the desktop client narrows the
host's permission rule to ``/minds-api-proxy/api/v1/agents/<agent_id>/...``,
so a request that reaches a route with a given ``<agent_id>`` has
already been authorized by the gateway as "this is an agent that lives
on the caller's host".
"""

import json

from flask import Blueprint
from flask import Response
from flask import request

from imbue.minds.desktop_client.api_key_auth import require_minds_api_key
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.mngr.primitives import AgentId


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return make_response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _json_error(message: str, status_code: int) -> Response:
    return _json_response({"error": message}, status_code=status_code)


# -- Notification route --


@require_minds_api_key
def _handle_notification(agent_id: str) -> Response:
    """Send a notification on behalf of the named agent."""
    dispatcher: NotificationDispatcher | None = get_state().notification_dispatcher
    if dispatcher is None:
        return _json_error("Notification dispatch not configured", 501)

    # force=True parses the body regardless of Content-Type, matching the old
    # FastAPI ``await request.json()`` (which ignored the header).
    body = request.get_json(silent=True, force=True)
    if body is None:
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

    agent_info = get_state().backend_resolver.get_agent_display_info(parsed_agent_id)
    agent_display_name = agent_info.agent_name if agent_info else str(parsed_agent_id)

    dispatcher.dispatch(notification_request, agent_display_name)
    return _json_response({"ok": True})


# -- Blueprint factory --


def create_api_v1_blueprint() -> Blueprint:
    """Create the /api/v1/ blueprint with all REST API endpoints."""
    blueprint = Blueprint("api_v1", __name__, url_prefix="/api/v1")

    # Notifications (per-agent so the gateway's per-host permission file
    # can restrict each caller to its own agent ids).
    blueprint.add_url_rule("/agents/<agent_id>/notifications", view_func=_handle_notification, methods=["POST"])

    return blueprint
