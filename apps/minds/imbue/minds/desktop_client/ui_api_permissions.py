"""/ui/api routes for the workspace options panel's Permissions pane.

One read endpoint serves the whole pane -- every grantable permission for one
workspace as a toggle, plus the pending requests waiting on the user -- and two
writes flip a single toggle each. A third drops one connector account's grants
for this workspace, and a fourth connects a service that latchkey cannot sign
in to through a browser, by running its own credential command over the values
the user typed into the pane (the browser-sign-in half of Add connection is the
settings page's own route). A fifth signs an account out: unlike the others it
is not scoped to this workspace at all -- it clears the stored credential, so
the account is gone for every machine and its grants are stripped from every
one of them.

Every write posts exactly one flip. The SERVER then recomputes the affected
rule's COMPLETE permission set from the workspace's current permissions file
and writes that back through the gateway (never a diff -- see
``latchkey/permission_toggles.py``); an emptied set deletes the rule.
Recomputing server-side is what keeps a buggy or hostile client from
clobbering the unrelated baseline permissions that share the ``latchkey-self``
rule. Each write returns the refreshed view, so the client renders the state
the server actually wrote rather than guessing at the result of its flip.

The read never fails on an unreachable latchkey gateway: it answers with an
empty view carrying ``permissions_unavailable``, which the pane renders as its
"can't load permissions" notice. That flag is the pane's only way to tell
"could not load" apart from "nothing granted yet", so it must never be
conflated with an empty payload.
"""

import os
from collections.abc import Callable
from typing import Any
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import AccountsRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.gateway_client import WorkspaceRequestPayload
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import disconnect_account
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_workspace
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import WorkspacePermissionsView
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_connector_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_self_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_permissions_view
from imbue.minds.desktop_client.latchkey.permission_toggles import connect_service_with_credentials
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_inbox import displayable_pending_requests
from imbue.minds.desktop_client.ui_models import UiAvailableConnection
from imbue.minds.desktop_client.ui_models import UiConnectCredentialsRequest
from imbue.minds.desktop_client.ui_models import UiConnectorDisconnectRequest
from imbue.minds.desktop_client.ui_models import UiConnectorRevokeAllRequest
from imbue.minds.desktop_client.ui_models import UiConnectorToggleRequest
from imbue.minds.desktop_client.ui_models import UiPermissionConnection
from imbue.minds.desktop_client.ui_models import UiSelfPermissionToggle
from imbue.minds.desktop_client.ui_models import UiSelfToggleRequest
from imbue.minds.desktop_client.ui_models import UiWaitingPermissionRequest
from imbue.minds.desktop_client.ui_models import UiWorkspacePermissions
from imbue.mngr.primitives import AgentId


def _is_permissions_request_authenticated() -> bool:
    """The same signed-cookie check as ui_api.is_ui_request_authenticated.

    Local twin because ui_api imports this module (registration), so importing
    back would be circular; the final cleanup phase should hoist one shared
    guard onto the /ui blueprint.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key)


def _json_response(payload: FrozenModel, status_code: int = 200) -> Response:
    return Response(payload.model_dump_json(), status=status_code, mimetype="application/json")


def _find_permission_grant_handler() -> LatchkeyPermissionGrantHandler | None:
    """The registered predefined-permission handler, which owns the gateway client, catalog, and latchkey.

    ``None`` in minimal setups (some tests, degraded startup); the pane then
    renders its unavailable notice rather than an empty permission set.
    """
    for handler in get_state().request_event_handlers:
        if isinstance(handler, LatchkeyPermissionGrantHandler):
            return handler
    return None


def _waiting_request_title_and_service(
    req: StreamedPermissionRequest,
    handler: LatchkeyPermissionGrantHandler | None,
) -> tuple[str, str, str]:
    """``(title, reason, service_name)`` for one pending request row.

    ``service_name`` is the catalog service whose brand mark leads the row and
    is empty for the kinds that have none (those fall back to a category glyph).
    """
    payload = req.payload
    if isinstance(payload, PredefinedRequestPayload):
        info = handler.services_catalog.get_by_scope(payload.scope) if handler is not None else None
        if info is None:
            return payload.scope, req.rationale, ""
        return info.display_name, req.rationale, info.name
    if isinstance(payload, FileSharingRequestPayload):
        return "Local files", req.rationale, ""
    if isinstance(payload, WorkspaceRequestPayload):
        return "Other machines", req.rationale, ""
    if isinstance(payload, AccountsRequestPayload):
        return "Device accounts", req.rationale, ""
    assert_never(payload)


def _build_waiting_requests(agent_id: str) -> tuple[UiWaitingPermissionRequest, ...]:
    """The "Waiting on you" rows for one workspace, oldest first.

    Matches on workspace *name* rather than agent id: latchkey requests are
    filed by the workspace's ``system-services`` sibling agent, which resolves
    to the same workspace as the user-facing one the pane was opened from.
    """
    state = get_state()
    backend_resolver = state.backend_resolver
    handler = _find_permission_grant_handler()
    try:
        parsed_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return ()
    ws_name = backend_resolver.get_workspace_name(parsed_agent_id) or ""
    if not ws_name:
        return ()
    rows: list[UiWaitingPermissionRequest] = []
    # Pending requests arrive most-recent-first; the strip reads oldest first
    # (the request the agent has been blocked on longest leads).
    for req in reversed(displayable_pending_requests(state.pending_requests, backend_resolver)):
        if (backend_resolver.get_workspace_name(AgentId(req.agent_id)) or "") != ws_name:
            continue
        title, reason, service_name = _waiting_request_title_and_service(req, handler)
        rows.append(
            UiWaitingPermissionRequest(
                id=req.request_id,
                title=title,
                reason=reason,
                service_name=service_name,
            )
        )
    return tuple(rows)


def _build_permissions_view_or_none(agent_id: str) -> WorkspacePermissionsView | None:
    """The engine's view for one workspace, or ``None`` when it cannot be loaded.

    ``None`` covers every unavailable case -- no predefined-permission handler
    wired, an unresolvable workspace host, or an unreachable latchkey gateway --
    and becomes ``permissions_unavailable`` on the wire.
    """
    handler = _find_permission_grant_handler()
    if handler is None:
        return None
    try:
        return build_workspace_permissions_view(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
        )
    except (PermissionToggleError, LatchkeyGatewayClientError) as e:
        logger.warning("Could not build the workspace permissions view for {}: {}", agent_id, e)
        return None


def _build_permissions_payload(agent_id: str) -> UiWorkspacePermissions:
    """The pane's full payload, degrading to the unavailable flag when the view cannot be built.

    The Ui models are revalidated from the engine models' dumps, so a field
    added or renamed upstream fails here (``extra=forbid``) rather than
    silently disappearing from the wire.
    """
    waiting_requests = _build_waiting_requests(agent_id)
    view = _build_permissions_view_or_none(agent_id)
    if view is None:
        return UiWorkspacePermissions(
            host_id="",
            connections=(),
            available_connections=(),
            file_sharing_toggles=(),
            workspace_toggles=(),
            waiting_requests=waiting_requests,
            permissions_unavailable=True,
        )
    return UiWorkspacePermissions(
        host_id=view.host_id,
        connections=tuple(
            UiPermissionConnection.model_validate(connection.model_dump()) for connection in view.connections
        ),
        available_connections=tuple(
            UiAvailableConnection.model_validate(entry.model_dump()) for entry in view.available_connections
        ),
        file_sharing_toggles=tuple(
            UiSelfPermissionToggle.model_validate(toggle.model_dump()) for toggle in view.file_sharing_toggles
        ),
        workspace_toggles=tuple(
            UiSelfPermissionToggle.model_validate(toggle.model_dump()) for toggle in view.workspace_toggles
        ),
        waiting_requests=waiting_requests,
        permissions_unavailable=False,
    )


def _write_prelude(agent_id: str) -> Response | tuple[dict[str, Any], LatchkeyPermissionGrantHandler]:
    """Auth + agent-id + JSON-body + handler lookup shared by every write route.

    Returns an error :class:`Response` (401 unauthenticated, 404 malformed
    workspace id, 400 invalid body, 503 with no permission handler wired), or
    ``(body, handler)`` on success.
    """
    if not _is_permissions_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    try:
        AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_json_error_response("Invalid JSON body", 400)
    handler = _find_permission_grant_handler()
    if handler is None:
        return make_json_error_response("Permission management is unavailable", 503)
    return body, handler


def _apply_and_refresh(agent_id: str, apply_toggle: Callable[[], object]) -> Response:
    """Run one flip and answer with the refreshed view, or map its failure to a status code.

    :class:`PermissionToggleError` / :class:`PermissionOverviewError` (unknown
    scope or service, non-grantable permission, unresolvable workspace) -> 400;
    :class:`LatchkeyGatewayClientError` (gateway unreachable) -> 502.

    Whatever the call returns is discarded -- the refreshed view is the answer,
    never the write's own report -- so the parameter is typed for any return,
    the way ``app.py``'s ``_apply_revoke`` is.
    """
    try:
        apply_toggle()
    except (PermissionToggleError, PermissionOverviewError) as e:
        return make_json_error_response(str(e), 400)
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not apply the permission change through the latchkey gateway: {}", e)
        return make_json_error_response(f"Could not apply the change through the latchkey gateway: {e}", 502)
    return _json_response(_build_permissions_payload(agent_id))


def _handle_workspace_permissions(agent_id: str) -> Response:
    """GET /ui/api/workspaces/<agent_id>/permissions: the Permissions pane's full payload."""
    if not _is_permissions_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    try:
        AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    return _json_response(_build_permissions_payload(agent_id))


def _handle_connector_toggle(agent_id: str) -> Response:
    """POST .../permissions/connector-toggle: flip one catalog permission for a (scope, account) rule."""
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        toggle_request = UiConnectorToggleRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed connector-toggle body: {}", e)
        return make_json_error_response("scope, account, permission and enabled are required.", 400)
    return _apply_and_refresh(
        agent_id,
        lambda: apply_connector_toggle(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
            scope=toggle_request.scope,
            account=toggle_request.account,
            permission=toggle_request.permission,
            enabled=toggle_request.enabled,
        ),
    )


def _handle_self_toggle(agent_id: str) -> Response:
    """POST .../permissions/self-toggle: flip one Local files / Other machines permission."""
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        toggle_request = UiSelfToggleRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed self-toggle body: {}", e)
        return make_json_error_response("permission and enabled are required.", 400)
    return _apply_and_refresh(
        agent_id,
        lambda: apply_self_toggle(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
            permission=toggle_request.permission,
            enabled=toggle_request.enabled,
        ),
    )


def _handle_connect_credentials(agent_id: str) -> Response:
    """POST .../permissions/connect-credentials: connect a service by storing typed-in credentials.

    The Add connection pane's action for a service latchkey cannot sign in to
    through a browser. Nothing is granted: the account joins the pane with no
    permissions, exactly as a completed sign-in does, and the refreshed view is
    the answer either way.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        connect_request = UiConnectCredentialsRequest.model_validate(body)
    except ValidationError as e:
        # The values themselves are the user's credentials, so only the shape
        # of the failure is reportable.
        logger.debug("Rejected a malformed connect-credentials body: {} field(s) invalid", e.error_count())
        return make_json_error_response("service_name and value_by_parameter_name are required.", 400)
    return _apply_and_refresh(
        agent_id,
        lambda: connect_service_with_credentials(
            latchkey=handler.latchkey,
            services_catalog=handler.services_catalog,
            service_name=connect_request.service_name,
            value_by_parameter_name=connect_request.value_by_parameter_name,
            account_name=connect_request.account_name,
        ),
    )


def _handle_connector_revoke_all(agent_id: str) -> Response:
    """POST .../permissions/connector-revoke-all: drop one connector account's grants for this workspace.

    Same effect as the settings page's per-workspace revoke; the service's
    other accounts, its grants on other workspaces, and the stored credentials
    are untouched.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        revoke_request = UiConnectorRevokeAllRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed connector-revoke-all body: {}", e)
        return make_json_error_response("service_name and account are required.", 400)
    return _apply_and_refresh(
        agent_id,
        lambda: revoke_service_account_for_workspace(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
            service_name=revoke_request.service_name,
            account=revoke_request.account,
        ),
    )


def _handle_connector_disconnect(agent_id: str) -> Response:
    """POST .../permissions/connector-disconnect: clear one connector account's credential everywhere.

    Unlike ``connector-revoke-all``, which drops this machine's grants and
    leaves the account connected, this clears the stored credential itself: the
    account is gone for every machine, so its grants -- which now have nothing
    behind them -- are stripped from every active workspace's host file, not
    just this one's.

    The catalog is checked before anything is cleared: the clear is the
    destructive half, and :func:`revoke_service_account_for_all_workspaces`
    would otherwise only reject an unknown service after the credential was
    already gone. A refused ``auth clear`` is latchkey failing rather than a bad
    request, so it answers 502 the way the settings page's Disconnect does. The
    cross-workspace strip runs on the request thread, so the refreshed view this
    returns is the file's actual state -- the pane decides where to land from
    the connection's absence, which a backgrounded strip would not yet show.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        disconnect_request = UiConnectorDisconnectRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed connector-disconnect body: {}", e)
        return make_json_error_response("service_name and account are required.", 400)
    if not handler.services_catalog.get(disconnect_request.service_name):
        return make_json_error_response(f"Unknown service '{disconnect_request.service_name}'.", 400)
    try:
        disconnect_account(handler.latchkey, disconnect_request.service_name, disconnect_request.account)
    except PermissionOverviewError as e:
        # The account key is a personal identifier, so the service and
        # latchkey's own detail are all that is logged.
        logger.warning("Could not disconnect from {}: {}", disconnect_request.service_name, e)
        return make_json_error_response(str(e), 502)

    # The host count the strip answers with is discarded, the same way the
    # settings page's Disconnect discards it.
    return _apply_and_refresh(
        agent_id,
        lambda: revoke_service_account_for_all_workspaces(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            service_name=disconnect_request.service_name,
            account=disconnect_request.account,
        ),
    )


def register_permissions_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions",
        view_func=_handle_workspace_permissions,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/connector-toggle",
        view_func=_handle_connector_toggle,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/self-toggle",
        view_func=_handle_self_toggle,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/connector-revoke-all",
        view_func=_handle_connector_revoke_all,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/connector-disconnect",
        view_func=_handle_connector_disconnect,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/connect-credentials",
        view_func=_handle_connect_credentials,
        methods=["POST"],
    )
