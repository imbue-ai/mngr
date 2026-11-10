# The two endpoints the profiling mode ingests through: the page's own spans, and Chromium's trace.
#
# Registered on the ``/ui`` blueprint only while profiling is on, so the normal app has no
# such routes, and gated by the same session cookie as every other ``/ui/api`` route.

from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.profiling import get_profiler
from imbue.minds.desktop_client.profile_ingestion import CHROMIUM_TRACE_ADAPTER
from imbue.minds.desktop_client.profile_ingestion import chromium_trace_events
from imbue.minds.desktop_client.profile_ingestion import page_batch_to_trace_events
from imbue.minds.desktop_client.profile_ingestion import scrub_chromium_event
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiProfileEventsBatch

# The page caps each batch it posts at one megabyte; Chromium's trace is bounded by the shell's
# recording buffer, whose events serialize to several times their size as JSON.
MAX_EVENTS_BODY_BYTES: Final[int] = 1024 * 1024
MAX_CHROMIUM_BODY_BYTES: Final[int] = 256 * 1024 * 1024


def _read_body_within(limit: int) -> bytes | None:
    """The request body, or None once it is known to exceed the limit (the remainder is left unread)."""
    if request.content_length is not None and request.content_length > limit:
        return None
    body = request.stream.read(limit + 1)
    return None if len(body) > limit else body


def _handle_profile_events() -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    # Parsed as JSON whatever the content type says: a pagehide post comes through sendBeacon as text/plain.
    body = _read_body_within(MAX_EVENTS_BODY_BYTES)
    if body is None:
        return make_json_error_response("Profile events body too large", status_code=413)
    try:
        batch = UiProfileEventsBatch.model_validate_json(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed profile events body: {}", e)
        return make_json_error_response("Invalid profile events body", status_code=400)
    profiler = get_profiler()
    process = get_state().page_profile_sessions.process_for(batch.session_id)
    profiler.write_foreign_events(page_batch_to_trace_events(batch, process, profiler.clock_sync))
    return Response(status=204)


def _handle_profile_chromium() -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    body = _read_body_within(MAX_CHROMIUM_BODY_BYTES)
    if body is None:
        return make_json_error_response("Chromium trace body too large", status_code=413)
    try:
        document = CHROMIUM_TRACE_ADAPTER.validate_json(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed Chromium trace body: {}", e)
        return make_json_error_response("Invalid Chromium trace body", status_code=400)
    profiler = get_profiler()
    profiler.write_foreign_events([scrub_chromium_event(event) for event in chromium_trace_events(document)])
    # The shell posts this as the last thing before it stops the backend, so the file holds it at once.
    profiler.flush()
    return Response(status=204)


def register_profile_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/profile/events", view_func=_handle_profile_events, methods=["POST"])
    blueprint.add_url_rule("/api/profile/chromium", view_func=_handle_profile_chromium, methods=["POST"])
