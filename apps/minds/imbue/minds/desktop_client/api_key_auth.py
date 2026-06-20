"""Bearer-token authentication for ``/api/v1/...`` endpoints.

Every ``minds run`` has exactly one central ``MINDS_API_KEY``,
freshly generated in memory at startup (see
:mod:`imbue.minds.desktop_client.api_key_store`). The latchkey
gateway's bundled ``minds-api-proxy`` extension injects it as
``Authorization: Bearer <key>`` on every request it proxies into the
desktop client, so the request-handling code only has to confirm the
header carries the same key.

Agent identity, when a route needs it, comes from the URL path segment
(``/api/v1/agents/<agent_id>/...``) -- not from the bearer token. The
latchkey gateway's per-host permissions file constrains which agent ids
an incoming caller can address, so a request that reaches us with a
valid token and a given ``<agent_id>`` in the path has already been
authorized by the gateway as "this agent_id lives on the caller's host".

Two consumer shapes are exposed:

* :func:`require_minds_api_key` -- a Flask view decorator. Aborts the
  request with 401 unless the central key is present; the view itself
  takes no extra argument (the agent id, if relevant, is in the path).
* :func:`is_request_authenticated` -- raw header check usable by the
  WSGI auth gate wrapping the WebDAV mount (see
  :mod:`imbue.minds.desktop_client.webdav`), which can't take a decorator.
"""

import json
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec

from flask import Response
from flask import request

from imbue.minds.desktop_client.api_key_store import is_valid_minds_api_key
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state

_BEARER_PREFIX = "Bearer "

_ViewParams = ParamSpec("_ViewParams")


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Return the token after ``Bearer `` or ``None`` if absent / malformed."""
    if authorization is None:
        return None
    if not authorization.startswith(_BEARER_PREFIX):
        return None
    token = authorization[len(_BEARER_PREFIX) :]
    return token or None


def is_request_authenticated(authorization_header_value: str | None, expected_key: str) -> bool:
    """Return ``True`` iff ``authorization_header_value`` carries the central key.

    Pure function so the WebDAV WSGI gate and the Flask decorator
    below can share the same check. ``expected_key`` is normally
    ``get_state().minds_api_key``; we accept it as an argument so this
    module has no module-level state.
    """
    token = _extract_bearer_token(authorization_header_value)
    if token is None:
        return False
    return is_valid_minds_api_key(token, expected_key)


def require_minds_api_key(view: Callable[_ViewParams, Response]) -> Callable[_ViewParams, Response]:
    """Flask view decorator: abort 401 unless the request carries the central key.

    The wrapped view takes no extra argument (the central-key model carries
    no per-call identity). Routes that need an agent id pick it up from the
    URL path via the usual ``<agent_id>`` path parameter. Fails closed when
    the key is unset (e.g. tests that don't populate it).

    On failure it returns a JSON ``{"error": ...}`` 401 with a
    ``WWW-Authenticate: Bearer`` challenge -- matching the JSON error shape
    of the rest of ``/api/v1`` and the WebDAV gate, rather than Flask's
    default HTML ``abort(401)`` page (which is what the FastAPI dependency's
    ``HTTPException(401)`` JSON body was, so this preserves wire parity).
    """

    @wraps(view)
    def wrapper(*args: _ViewParams.args, **kwargs: _ViewParams.kwargs) -> Response:
        expected_key = get_state().minds_api_key
        if expected_key is None or not is_request_authenticated(request.headers.get("authorization"), expected_key):
            return make_response(
                content=json.dumps({"error": "Missing or invalid Authorization header"}),
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return view(*args, **kwargs)

    return wrapper
