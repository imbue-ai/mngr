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

* :data:`MindsApiAuthDep` -- FastAPI dependency. Doesn't return anything
  meaningful (the agent id, if relevant, is in the path); raises 401 on
  failure.
* :func:`is_request_authenticated` -- raw header check usable by the
  ASGI auth gate wrapping the WebDAV mount (see
  :mod:`imbue.minds.desktop_client.webdav`), which can't take a FastAPI
  dependency.
"""

from typing import Annotated

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request

from imbue.minds.desktop_client.api_key_store import is_valid_minds_api_key

_BEARER_PREFIX = "Bearer "


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

    Pure function so the WebDAV ASGI gate and the FastAPI dependency
    below can share the same check. ``expected_key`` is normally
    ``app.state.minds_api_key``; we accept it as an argument so this
    module has no module-level state.
    """
    token = _extract_bearer_token(authorization_header_value)
    if token is None:
        return False
    return is_valid_minds_api_key(token, expected_key)


def _authenticate_minds_api(request: Request) -> None:
    """FastAPI dependency: 401 unless the request carries the central key.

    Returns ``None`` because the central-key model carries no per-call
    identity. Routes that need an agent id pick it up from the URL path
    via the usual ``{agent_id}`` path parameter.
    """
    expected_key: str = request.app.state.minds_api_key
    if not is_request_authenticated(request.headers.get("authorization"), expected_key):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")


MindsApiAuthDep = Annotated[None, Depends(_authenticate_minds_api)]
