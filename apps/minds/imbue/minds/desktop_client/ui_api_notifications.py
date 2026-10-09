"""/ui/api routes for acting on the notification feed.

The feed itself travels over the ``/ui/ws`` channel; these routes are the
renderer's way to change it: reading a workspace's messages (it was navigated
to), clearing one entry, or clearing everything. Each write wakes the publisher
through the feed's own change hook, so every open window sees the result on the
next frame.
"""

from flask import Blueprint
from flask import Response

from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated


def _feed_or_error() -> NotificationFeed | Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    feed = get_state().notification_feed
    if feed is None:
        return make_json_error_response("Notification feed is not configured", status_code=503)
    return feed


def _ok() -> Response:
    return Response('{"ok": true}', status=200, mimetype="application/json")


def _handle_workspace_read(workspace_agent_id: str) -> Response:
    """POST /ui/api/notifications/workspace/<id>/read: the user is looking at this workspace."""
    feed = _feed_or_error()
    if isinstance(feed, Response):
        return feed
    feed.mark_workspace_read(workspace_agent_id)
    return _ok()


def _handle_clear(entry_id: str) -> Response:
    """POST /ui/api/notifications/<id>/clear: remove one entry."""
    feed = _feed_or_error()
    if isinstance(feed, Response):
        return feed
    feed.clear(entry_id)
    return _ok()


def _handle_clear_all() -> Response:
    """POST /ui/api/notifications/clear-all: empty the feed, receipts included."""
    feed = _feed_or_error()
    if isinstance(feed, Response):
        return feed
    feed.clear_all()
    return _ok()


def register_notification_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/notifications/clear-all", view_func=_handle_clear_all, methods=["POST"])
    blueprint.add_url_rule(
        "/api/notifications/workspace/<workspace_agent_id>/read", view_func=_handle_workspace_read, methods=["POST"]
    )
    blueprint.add_url_rule("/api/notifications/<entry_id>/clear", view_func=_handle_clear, methods=["POST"])
