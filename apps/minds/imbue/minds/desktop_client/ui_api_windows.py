"""/ui/api routes about the desktop windows themselves: which account a window is on, and moving it to another.

``POST /ui/api/windows/switch-account`` answers the route the asking window
navigates to for the account it picked in the titlebar's switcher: that
account's most recently opened workspace that no other connected window shows,
else the account's hub. Which workspaces the other windows show comes from
their ``client_state`` reports; which were opened most recently comes from the
``[recent]`` lists in ``config.toml``, fed by the same reports.

``GET /ui/api/windows/route-account`` answers which account a SPA route belongs
to, by the rules the bootstrap seed follows (``resolve_request_account_id``):
the SPA asks it for a workspace route whose row its own account's list does
not carry (a step back onto a workspace of the account the window just left),
since the association that names the owner lives in the records here.
"""

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_account_scope import choose_switch_route
from imbue.minds.desktop_client.ui_account_scope import is_valid_account_id
from imbue.minds.desktop_client.ui_account_scope import resolve_window_account_id
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiRouteAccountResponse
from imbue.minds.desktop_client.ui_models import UiSwitchAccountRequest
from imbue.minds.desktop_client.ui_models import UiSwitchAccountResponse
from imbue.minds.desktop_client.ui_models import UiWindowKind


def _workspace_account_id(state: DesktopClientState, workspace_any_id: str) -> str | None:
    """The account a workspace (by either coordinate) is associated with, or None for none."""
    if state.session_store is None:
        return None
    return state.session_store.get_owner_user_id_for_workspace(workspace_any_id)


def _create_attempt_account_id(state: DesktopClientState, create_attempt_id: str) -> str | None:
    """The account a create attempt was submitted under, or None for none or an unknown attempt."""
    agent_creator = state.agent_creator
    store = agent_creator.pending_create_attempt_store if agent_creator is not None else None
    record = store.read_record(create_attempt_id) if store is not None else None
    if record is None:
        return None
    return record.request.account_id or None


def resolve_request_account_id(state: DesktopClientState, path: str, query: str) -> str:
    """The account a served URL's window shows at first paint (see ``resolve_window_account_id``)."""
    return resolve_window_account_id(
        path,
        query,
        resolve_workspace_account_id=lambda any_id: _workspace_account_id(state, any_id),
        resolve_create_attempt_account_id=lambda attempt_id: _create_attempt_account_id(state, attempt_id),
        recent_account_ids=state.minds_config.get_recent_account_ids() if state.minds_config is not None else [],
    )


def _handle_route_account() -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    path = request.args.get("path", "")
    if not path.startswith("/"):
        return make_json_error_response("Invalid route path", status_code=400)
    account_id = resolve_request_account_id(get_state(), path, request.args.get("query", ""))
    return Response(
        UiRouteAccountResponse(account_id=account_id).model_dump_json(), status=200, mimetype="application/json"
    )


def _handle_switch_account() -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    try:
        body = UiSwitchAccountRequest.model_validate(request.get_json(silent=True, force=True) or {})
    except ValidationError as e:
        logger.debug("Rejected a malformed switch-account body: {}", e)
        return make_json_error_response("Invalid JSON body", status_code=400)
    if not is_valid_account_id(body.account_id):
        return make_json_error_response("Invalid account id", status_code=400)
    state = get_state()
    publisher = state.ui_publisher
    if publisher is None:
        return make_json_error_response("The UI publisher is not configured", status_code=503)
    # The account's live rows, from the same derivation its windows list; a
    # workspace whose destroy is running or failed is still listed but its
    # row only opens the destroying page.
    snapshot = publisher.build_snapshot(body.account_id)
    destroying_agent_ids = frozenset(snapshot.workspaces.destroying_agent_ids)
    enterable_workspace_ids = frozenset(
        entry.id
        for entry in snapshot.workspaces.workspaces
        if not entry.is_remote and entry.create_attempt_state == "" and entry.id not in destroying_agent_ids
    )
    # Tabs are not deduplicated in a browser, so only desktop windows hold a workspace back.
    shown_elsewhere = (
        frozenset(
            client_state.workspace_agent_id
            for client_state in state.ui_channel_broadcaster.get_connected_client_states()
            if client_state.window_kind == UiWindowKind.MAIN
            and client_state.client_id != body.client_id
            and client_state.workspace_agent_id
        )
        if body.is_desktop
        else frozenset()
    )
    recent_workspace_ids = (
        state.minds_config.get_recent_workspace_ids(body.account_id) if state.minds_config is not None else []
    )
    route = choose_switch_route(body.account_id, recent_workspace_ids, enterable_workspace_ids, shown_elsewhere)
    return Response(UiSwitchAccountResponse(route=route).model_dump_json(), status=200, mimetype="application/json")


def register_window_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/windows/route-account", view_func=_handle_route_account, methods=["GET"])
    blueprint.add_url_rule("/api/windows/switch-account", view_func=_handle_switch_account, methods=["POST"])
