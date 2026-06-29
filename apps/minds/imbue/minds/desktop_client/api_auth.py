"""Shared auth + JSON-response helpers for the ``/api/v1`` surface.

Extracted from ``api_v1`` so that lower modules (``api_schema``, and the spectree
wiring in ``api_spec``) can depend on the auth decorator without importing the
handler module -- which would otherwise create an ``api_schema`` -> ``api_v1``
import cycle.
"""

import json
import os
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec

from flask import Response
from flask import request

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.api_key_auth import is_request_authenticated
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state

_ViewParams = ParamSpec("_ViewParams")


def json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return make_response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def json_error(message: str, status_code: int) -> Response:
    return json_response({"error": message}, status_code=status_code)


def json_field_error(message: str, field: str, status_code: int = 400) -> Response:
    """A 400-style validation error that also names the offending form field.

    The browser create page reads the ``field`` key to render the message inline
    next to the right input; agents (the other caller of this route) simply
    ignore the extra key, so it stays backward compatible.
    """
    return json_response({"error": message, "field": field}, status_code=status_code)


def handle_invalid_random_id(error: InvalidRandomIdError) -> Response:
    """Map a malformed workspace/operation id in the URL path to a 400 instead of a 500.

    Every ``/workspaces/<id>`` route constructs an ``AgentId``/``CreationId`` from
    the raw path param up front; a malformed id raises ``InvalidRandomIdError``
    (a ``ValueError``). Registered blueprint-wide so that surfaces as a clean 400
    rather than an uncaught 500.
    """
    return json_error(f"Invalid id: {error}", 400)


def _is_cookie_authenticated() -> bool:
    """Whether the request carries a valid desktop-client session cookie.

    Mirrors the bare-origin app's own session check (same signing key + cookie
    name) so the browser UI can call these routes with its session cookie.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=get_state().auth_store.get_signing_key())


def _is_bearer_authenticated() -> bool:
    """Whether the request carries the central minds API bearer key."""
    expected_key = get_state().minds_api_key
    return expected_key is not None and is_request_authenticated(request.headers.get("authorization"), expected_key)


def require_api_or_cookie_auth(view: Callable[_ViewParams, Response]) -> Callable[_ViewParams, Response]:
    """Allow either the central bearer key (agents, via the gateway) or the session cookie (the UI).

    The cross-workspace routes are reached both by in-workspace agents (which
    present the gateway-injected bearer) and by the desktop UI itself (which
    presents the session cookie), so they accept either credential.
    """

    @wraps(view)
    def wrapper(*args: _ViewParams.args, **kwargs: _ViewParams.kwargs) -> Response:
        if _is_bearer_authenticated() or _is_cookie_authenticated():
            return view(*args, **kwargs)
        return json_error("Not authenticated", 401)

    return wrapper
