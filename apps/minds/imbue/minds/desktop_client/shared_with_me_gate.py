"""The trust gate for workspaces shared with an account (specs/shared-with-me/spec.md, S12).

A row listed under "Shared with me" is someone else's workspace. Nothing on
this device may act on it as an owner would: no backups, settings, sharing,
keys, rename, lifecycle, sync, folder sync, terminal, exec, or latchkey
registration. Every such action names the workspace in its path, its query,
or its JSON body, so one ``before_request`` hook on the app refuses any
request naming a listed share's key, host id, or workspace id anywhere
there, except the shared-with-me routes themselves, so a handler that would
otherwise fail on the missing local agent refuses for the stated reason.
The SPA page routes name a workspace in their path too, but a page load
acts on nothing (the page that displays a shared workspace is served the
same way as any other), so they are never gated.
"""

from typing import Final

from flask import Flask
from flask import Response
from flask import request

from imbue.minds.desktop_client.api_auth import is_api_or_cookie_authenticated
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api import SPA_INDEX_ENDPOINT_PREFIX
from imbue.minds.desktop_client.ui_api_shared_with_me import SHARED_WITH_ME_ENDPOINT_PREFIX

NOT_OWNED_ERROR_CODE: Final[str] = "not_owned"
# The path parameters that name a workspace or its machine across the API and UI routes.
_GATED_PATH_KEYS: Final[tuple[str, ...]] = ("agent_id", "workspace_id", "host_id")
# The query parameters and JSON body fields that name one (``workspace`` is the overlays' and the
# mint's spelling; ``workspace_agent_id`` the help chat's and the bug report's).
_GATED_VALUE_KEYS: Final[tuple[str, ...]] = ("agent_id", "workspace_id", "host_id", "workspace", "workspace_agent_id")
# The JSON body fields that name several (the bulk update routes).
_GATED_LIST_KEYS: Final[tuple[str, ...]] = ("agent_ids",)


def _locally_hosted_coordinates() -> frozenset[str]:
    """The ids and host ids of every workspace discovery places on this device."""
    resolver = get_state().backend_resolver
    coordinates: set[str] = set()
    for agent_id in {*resolver.list_known_agent_ids(), *resolver.list_known_workspace_ids()}:
        coordinates.add(str(agent_id))
        display_info = resolver.get_agent_display_info(agent_id)
        if display_info is not None:
            coordinates.add(display_info.host_id)
    return frozenset(coordinates)


def _is_locally_owned(value: str, hosted: frozenset[str]) -> bool:
    """Whether this device hosts the workspace the coordinate names, or holds its owner's record of it.

    One device may hold both ends of a share: account A owns the workspace
    (hosted here, or held here as a record alone) and account B, also signed
    in here, is its recipient. The share is listed for B, but A's own actions
    on the workspace must keep working, so a coordinate the device knows as
    its own is never gated. A record only ever belongs to the owner, so the
    records cannot admit a workspace that is merely shared with an account here.
    """
    if value in hosted:
        return True
    session_store = get_state().session_store
    return session_store is not None and session_store.get_owner_user_id_for_workspace(value) is not None


def refuse_if_shared_workspace() -> Response | None:
    """Answer 403 ``not_owned`` when the request names a workspace shared with an account here; else None.

    An unauthenticated request is left to its handler's own 401, so the gate
    never tells a caller who cannot act here which ids are shared with an account.
    """
    endpoint = request.endpoint or ""
    if endpoint.startswith(SPA_INDEX_ENDPOINT_PREFIX):
        return None
    if endpoint.rsplit(".", 1)[-1].startswith(SHARED_WITH_ME_ENDPOINT_PREFIX):
        return None
    store = get_state().shared_with_me_store
    if store is None or not is_api_or_cookie_authenticated():
        return None
    named = _named_coordinates()
    if not named:
        return None
    # The listed set is read from the cache; the resolver scan and the record
    # lookups behind the owned check are paid only once a request names a listed share.
    listed = store.listed_share_coordinates()
    listed_hits = [value for value in named if value in listed]
    if not listed_hits:
        return None
    hosted = _locally_hosted_coordinates()
    if all(_is_locally_owned(value, hosted) for value in listed_hits):
        return None
    return make_json_error_response(
        "This workspace was shared with you by someone else; only its owner can do that.",
        403,
        code=NOT_OWNED_ERROR_CODE,
    )


def _named_coordinates() -> list[str]:
    """Every workspace or machine coordinate the request names: in its path, its query, or its JSON body."""
    view_args = request.view_args or {}
    named = [str(view_args[key]) for key in _GATED_PATH_KEYS if isinstance(view_args.get(key), str)]
    named.extend(value for key in _GATED_VALUE_KEYS if (value := request.args.get(key)))
    # Parsed as the owner handlers parse theirs (``force=True``), so a body sent
    # under another content type is inspected exactly where it would be acted on.
    body = request.get_json(silent=True, force=True)
    if isinstance(body, dict):
        named.extend(value for key in _GATED_VALUE_KEYS if isinstance(value := body.get(key), str) and value)
        for key in _GATED_LIST_KEYS:
            values = body.get(key)
            if isinstance(values, list):
                named.extend(value for value in values if isinstance(value, str))
    return named


def register_shared_workspace_gate(app: Flask) -> None:
    """Install the gate on the app: every request is checked before its handler runs."""
    app.before_request(refuse_if_shared_workspace)
