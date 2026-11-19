"""/ui/api routes for this computer's resource warnings.

The warnings travel over the ``/ui/ws`` channel, pushed when they or the levels change; the settings section,
which shows the readings, reads them here while it is open, and saves the levels here.
"""

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.minds.desktop_client.device_resource_monitor import DeviceResourceMonitor
from imbue.minds.desktop_client.device_resources import DeviceResourceSettings
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated


def _monitor_or_error() -> DeviceResourceMonitor | Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    monitor = get_state().device_resource_monitor
    if monitor is None:
        return make_json_error_response("Resource warnings are not available in this build.", status_code=503)
    return monitor


def _handle_view() -> Response:
    """GET /ui/api/device-resources: the readings and warnings as they stand, for the open settings section."""
    monitor = _monitor_or_error()
    if isinstance(monitor, Response):
        return monitor
    return Response(monitor.view().model_dump_json(), status=200, mimetype="application/json")


def _handle_settings() -> Response:
    """POST /ui/api/device-resources/settings: save the warning levels; answered with the warnings they raise."""
    monitor = _monitor_or_error()
    if isinstance(monitor, Response):
        return monitor
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_json_error_response("Invalid JSON body", status_code=400)
    try:
        settings = DeviceResourceSettings.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected malformed resource warning levels: {}", e)
        return make_json_error_response(
            "The warning levels must be within range: disk 0.5 to 500 GB, memory and CPU 50 to 99%.",
            status_code=400,
        )
    return Response(monitor.update_settings(settings).model_dump_json(), status=200, mimetype="application/json")


def register_device_resource_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/device-resources", view_func=_handle_view, methods=["GET"])
    blueprint.add_url_rule("/api/device-resources/settings", view_func=_handle_settings, methods=["POST"])
