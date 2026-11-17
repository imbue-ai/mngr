"""The home page's per-workspace assets (specs/home-screen-redesign, 4.10 and 4.12).

Three routes under ``/ui/api/workspaces/<agent_id>``: an app's icon, the
workspace's cached wallpaper, and the refresh that fetches the wallpaper from
the loaded workspace. Both images are served immutable under a version the
SPA appends as a query, so a changed icon or wallpaper is a new URL.
"""

import json
from typing import Final

from flask import Blueprint
from flask import Response

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.workspace_apps import accepted_workspace_app_icon
from imbue.mngr.primitives import AgentId

_IMMUTABLE_CACHE_CONTROL: Final[str] = "private, max-age=31536000, immutable"
# An icon is untrusted workspace content; even navigated to directly it may run nothing.
_ICON_CONTENT_SECURITY_POLICY: Final[str] = "default-src 'none'; style-src 'unsafe-inline'"


def _parsed_agent_id(agent_id: str) -> AgentId | None:
    try:
        return AgentId(agent_id)
    except InvalidRandomIdError:
        return None


def _handle_app_icon(agent_id: str, app_name: str) -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    parsed_agent_id = _parsed_agent_id(agent_id)
    if parsed_agent_id is None:
        return make_json_error_response("Unknown workspace", 404)
    icon = accepted_workspace_app_icon(get_state().backend_resolver, parsed_agent_id, app_name)
    if not icon:
        return make_json_error_response("No icon for this app", 404)
    return make_response(
        content=icon,
        media_type="image/svg+xml",
        headers={"Cache-Control": _IMMUTABLE_CACHE_CONTROL, "Content-Security-Policy": _ICON_CONTENT_SECURITY_POLICY},
    )


def _handle_wallpaper(agent_id: str) -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    if _parsed_agent_id(agent_id) is None:
        return make_json_error_response("Unknown workspace", 404)
    cache = get_state().wallpaper_cache
    image = cache.read_image(agent_id) if cache is not None else None
    if image is None:
        return make_json_error_response("No wallpaper is cached for this workspace", 404)
    return make_response(
        content=image.body, media_type=image.content_type, headers={"Cache-Control": _IMMUTABLE_CACHE_CONTROL}
    )


def _handle_wallpaper_refresh(agent_id: str) -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    if _parsed_agent_id(agent_id) is None:
        return make_json_error_response("Unknown workspace", 404)
    refresher = get_state().wallpaper_refresher
    if refresher is None:
        return make_json_error_response("Wallpapers cannot be read on this app", 503)
    is_started = refresher.request_refresh(agent_id)
    return make_response(
        content=json.dumps({"agent_id": agent_id, "is_started": is_started}),
        status_code=202,
        media_type="application/json",
    )


def register_workspace_asset_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/apps/<app_name>/icon.svg", view_func=_handle_app_icon, methods=["GET"]
    )
    blueprint.add_url_rule("/api/workspaces/<agent_id>/wallpaper", view_func=_handle_wallpaper, methods=["GET"])
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/wallpaper/refresh", view_func=_handle_wallpaper_refresh, methods=["POST"]
    )
