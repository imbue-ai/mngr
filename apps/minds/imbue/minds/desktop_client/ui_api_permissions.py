"""/ui/api routes for the workspace options panel's Permissions pane.

One read endpoint serves the whole pane -- every grantable permission for one
workspace as a toggle, plus the pending requests waiting on the user -- and two
writes flip a single toggle each. A third drops one connector account's grants
for this workspace, and a fourth connects a service that latchkey cannot sign
in to through a browser, by running its own credential command over the values
the user typed into the pane (the browser-sign-in half of Add connection is the
settings page's own route). A fifth signs an account out: it clears the stored
credential of the store this workspace's machine reads, so the account is gone
for every workspace that reads the same store -- that one alone for a machine
of its own, every local workspace for this computer's. A sixth turns desktop
egress on or off for one service: whether a remote workspace's requests to it
are sent out through this computer.

Every write posts exactly one flip. The SERVER then recomputes the affected
rule's COMPLETE permission set from the workspace's current permissions file
and writes that back through the gateway (never a diff -- see
``latchkey/permission_toggles.py``); an emptied set deletes the rule.
Recomputing server-side is what keeps a buggy or hostile client from
clobbering the unrelated baseline permissions that share the ``latchkey-self``
rule. Each write returns the refreshed view, so the client renders the state
the server actually wrote rather than guessing at the result of its flip.

This module is named for the **screen** it serves, not for one resource, and
its routes span two. Everything above is under ``/permissions`` and is
latchkey: read the gateway's policy, flip a grant, push it to the workspace's
machine. Three more routes sit under ``/folder-syncs`` and touch latchkey not
at all (see :mod:`folder_sync`) -- ``toggle`` starts or stops an ``mngr pair``
subprocess for a folder that is already shared, ``discard-copy`` deletes a
copy the machine set aside, and the ``folder-syncs`` read answers the pane's
poll from this process's own memory. They are served from here because they
are drawn in the same card as the access above them and answered with the
same refreshed payload; they are addressed separately because a sync is not a
permission, and a URL that said it was would be wrong about who owns it.

For a **remote** workspace every one of the latchkey routes talks to that
workspace's own machine, and blocks until it answers: the read fetches the
machine's credentials and policy (one round trip) so the pane shows what its
agents actually have, and each write pushes the change there before answering,
so a 200 means the machine has taken it and an error means nothing on the
machine changed. A failed write leaves this computer's copy ahead of the
machine only until the next read, which adopts the machine's own state back
over it.

The read never fails on an unreachable latchkey gateway (or an unreachable
machine): it answers with an empty view carrying ``permissions_unavailable``,
which the pane renders as its "can't load permissions" notice. That flag is the
pane's only way to tell "could not load" apart from "nothing granted yet", so
it must never be conflated with an empty payload.
"""

from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync import FolderSyncState
from imbue.minds.desktop_client.folder_sync import FolderSyncStatus
from imbue.minds.desktop_client.folder_sync import WORKSPACE_INACTIVE_SYNC_DIRECTORY
from imbue.minds.desktop_client.folder_sync import WORKSPACE_SYNC_DIRECTORY
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncRecord
from imbue.minds.desktop_client.latchkey.gateway_client import AccountsRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import CustomServiceRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingAccess
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.gateway_client import WorkspaceRequestPayload
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.machine_latchkey import is_machine_store_of_its_own
from imbue.minds.desktop_client.latchkey.machine_latchkey import machine_latchkey_for_workspace
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.permission_overview import FILE_SHARING_PERMISSION_PREFIX
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import disconnect_account
from imbue.minds.desktop_client.latchkey.permission_overview import parse_file_sharing_permission
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_workspace
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import WorkspacePermissionsView
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_connector_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_desktop_egress_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_self_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_permissions_view
from imbue.minds.desktop_client.latchkey.permission_toggles import connect_service_with_credentials
from imbue.minds.desktop_client.latchkey.permission_toggles import resolve_workspace_host_id
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_inbox import displayable_pending_requests
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiAvailableConnection
from imbue.minds.desktop_client.ui_models import UiConnectBrowserRequest
from imbue.minds.desktop_client.ui_models import UiConnectCredentialsRequest
from imbue.minds.desktop_client.ui_models import UiConnectorDisconnectRequest
from imbue.minds.desktop_client.ui_models import UiConnectorRevokeAllRequest
from imbue.minds.desktop_client.ui_models import UiConnectorToggleRequest
from imbue.minds.desktop_client.ui_models import UiDesktopEgressToggleRequest
from imbue.minds.desktop_client.ui_models import UiPathSync
from imbue.minds.desktop_client.ui_models import UiPermissionConnection
from imbue.minds.desktop_client.ui_models import UiSelfPermissionToggle
from imbue.minds.desktop_client.ui_models import UiSelfToggleRequest
from imbue.minds.desktop_client.ui_models import UiSharedPath
from imbue.minds.desktop_client.ui_models import UiSharedPathRemoveRequest
from imbue.minds.desktop_client.ui_models import UiSharedPathRequest
from imbue.minds.desktop_client.ui_models import UiWaitingPermissionRequest
from imbue.minds.desktop_client.ui_models import UiWorkspacePermissions
from imbue.minds.errors import FolderSyncError
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host


def json_response(payload: FrozenModel, status_code: int = 200) -> Response:
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


def _push_permissions_to_machine() -> Callable[[str], None]:
    """How a permissions edit made here reaches the workspace's own machine.

    Blocks until the machine has taken the edit and raises
    :class:`MachineOperationError` when it has not, so a write route answers
    only once the policy is enforceable where the agent runs. An app built
    without an operator (a minimal setup, a test) can reach no machine at all,
    so there is nothing to push and the local edit is the whole change.
    """
    operator = get_state().machine_operator
    if operator is None:
        return lambda workspace_agent_id: None
    return operator.push_permissions


def _push_permissions_and_desktop_egress_rules_to_machine() -> Callable[[str], None]:
    """How a desktop egress edit made here reaches the workspace's own machine.

    The same contract as :func:`_push_permissions_to_machine`, for the write
    that edits the rules file as well as the policy: both are pushed in one
    round trip.
    """
    operator = get_state().machine_operator
    if operator is None:
        return lambda workspace_agent_id: None
    return operator.push_permissions_and_desktop_egress_rules


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
    if isinstance(payload, CustomServiceRequestPayload):
        # The domain, like everywhere else a custom service is named. There is
        # no brand mark for one -- it is a connection to a domain, not a vendor
        # -- so the row falls back to the category glyph.
        return payload.domain, req.rationale, ""
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


def _read_machine_state(agent_id: str) -> None:
    """Bring this computer's copies of a workspace's machine up to date before the pane is built.

    The pane shows what the workspace's own machine holds -- which accounts it
    is connected to and what its agents may do with them -- so it is read from
    the machine every time it is opened, in one round trip, rather than from
    whatever this computer last wrote. A local workspace has nothing to read:
    its agents run here, on the copies this computer keeps.

    Raises:
        MachineOperationError: when the machine cannot be reached, which the
            pane reports as "permissions can't be loaded" rather than showing a
            stale answer as if it were current.
    """
    operator = get_state().machine_operator
    if operator is not None:
        operator.refresh(agent_id)


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
            machine_latchkey=machine_latchkey_for_workspace(handler.latchkey, get_state().backend_resolver, agent_id),
            workspace_agent_id=agent_id,
            device_id=get_state().device_id,
        )
    except (PermissionToggleError, PermissionOverviewError, LatchkeyGatewayClientError) as e:
        logger.warning("Could not build the workspace permissions view for {}: {}", agent_id, e)
        return None


def _collapse_home(path: str, home_dir: str) -> str:
    """``path`` with the home directory written as ``~``, the way the user typed it."""
    if home_dir and (path == home_dir or path.startswith(f"{home_dir}/")):
        return f"~{path[len(home_dir) :]}"
    return path


def _build_shared_paths(
    toggles: Sequence[UiSelfPermissionToggle],
    manager: FolderSyncManager | None,
    home_dir: str,
    agent_id: str,
) -> tuple[UiSharedPath, ...]:
    """Collapse the granted ``minds-file-server-*`` names into one row per path.

    A path can be held at both access modes at once, and WRITE already implies
    READ, so the row shows the wider of the two. Revoked names are dropped
    entirely: what this pane lists is what is shared, and removing something
    takes it off the list rather than leaving a switched-off reminder.
    """
    access_by_path: dict[str, FileSharingAccess] = {}
    for toggle in toggles:
        if not toggle.is_granted:
            continue
        parsed = parse_file_sharing_permission(toggle.permission)
        if parsed is None:
            continue
        access, path = parsed
        if access == "write" or path not in access_by_path:
            access_by_path[path] = FileSharingAccess.WRITE if access == "write" else FileSharingAccess.READ
    remembered = (
        {} if manager is None else {record.local_path: record for record in manager.remembered_for_agent(agent_id)}
    )
    rows = []
    for path in sorted(access_by_path):
        status = None if manager is None else manager.status_for_path(agent_id, path)
        # A running sync is the live answer; otherwise the record is, so a
        # folder whose sync is off still shows what became of its copy.
        record = remembered.get(path)
        desired = None if manager is None else manager.desired_activity_for(agent_id, path)
        sync = (
            ui_path_sync(status, desired or FolderSyncActivity.ACTIVE)
            if status is not None
            else (None if record is None or manager is None else ui_remembered_sync(record, manager.device_id))
        )
        # A sync the app believes is on, with nothing behind it, is a failure
        # however quietly it happened -- so the row says so rather than
        # reporting a bare STOPPED with no reason and no way to act on it.
        if sync is not None and manager is not None and sync.activity == FolderSyncActivity.ACTIVE:
            sync = _with_restore_failure(sync, manager.restore_failure_for(agent_id, path))
        # A folder part-way to where the user just sent it shows how far it has
        # got, over whatever its last settled state was.
        in_flight = None if manager is None else manager.in_flight_state_for(agent_id, path)
        if sync is not None and in_flight is not None:
            sync = sync.model_copy_update(to_update(sync.field_ref().state, in_flight))
        rows.append(
            UiSharedPath(
                path=path,
                path_label=_collapse_home(path, home_dir),
                access=access_by_path[path],
                sync=sync,
                sync_overlap_warning=sync_overlap_warning(manager, path, agent_id),
                # Asked of the manager rather than worked out here, so the
                # option is greyed out for exactly the folders a click would
                # be refused for. Asked of every row, including one that has a
                # sync of its own: having been synced before says nothing about
                # whether it may be synced now, and a folder whose copy was
                # removed while its parent started syncing is exactly the row
                # that must not be offered.
                sync_unavailable_reason=(
                    "" if manager is None else manager.reason_sync_is_unavailable(agent_id, path)
                ),
            )
        )
    return tuple(rows)


def _load_permissions_payload(agent_id: str) -> UiWorkspacePermissions:
    """The pane's payload for a *read*: the machine's own state, fetched and then rendered.

    Only the read fetches. A write already made the machine match this
    computer's copies, so the payload it answers with is built from them
    directly (:func:`build_permissions_payload`) rather than paying a second
    round trip to be told what was just pushed.
    """
    try:
        _read_machine_state(agent_id)
    except MachineOperationError as e:
        logger.warning("Could not read the machine state of workspace {}: {}", agent_id, e)
        return _unavailable_permissions_payload(agent_id)
    return build_permissions_payload(agent_id)


def _unavailable_permissions_payload(agent_id: str) -> UiWorkspacePermissions:
    """The payload the pane renders as its "can't load permissions" notice."""
    return UiWorkspacePermissions(
        host_id="",
        connections=(),
        available_connections=(),
        shared_paths=(),
        workspace_toggles=(),
        waiting_requests=_build_waiting_requests(agent_id),
        permissions_unavailable=True,
        is_sync_supported=get_state().folder_sync_manager is not None,
        # No connection renders from this payload, so nothing reads it; the
        # shared reading is the one that overstates rather than understates what
        # a sign-out would reach.
        is_credential_store_shared=True,
    )


def build_permissions_payload(agent_id: str) -> UiWorkspacePermissions:
    """The pane's full payload from this computer's copies, degrading to the unavailable flag.

    The Ui models are revalidated from the engine models' dumps, so a field
    added or renamed upstream fails here (``extra=forbid``) rather than
    silently disappearing from the wire.
    """
    manager = get_state().folder_sync_manager
    home_dir = str(manager.home_dir) if manager is not None else ""
    view = _build_permissions_view_or_none(agent_id)
    if view is None:
        return _unavailable_permissions_payload(agent_id)
    return UiWorkspacePermissions(
        host_id=view.host_id,
        connections=tuple(
            UiPermissionConnection.model_validate(connection.model_dump()) for connection in view.connections
        ),
        available_connections=tuple(
            UiAvailableConnection.model_validate(entry.model_dump()) for entry in view.available_connections
        ),
        shared_paths=_build_shared_paths(
            tuple(UiSelfPermissionToggle.model_validate(toggle.model_dump()) for toggle in view.file_sharing_toggles),
            manager,
            home_dir,
            agent_id,
        ),
        workspace_toggles=tuple(
            UiSelfPermissionToggle.model_validate(toggle.model_dump()) for toggle in view.workspace_toggles
        ),
        waiting_requests=_build_waiting_requests(agent_id),
        permissions_unavailable=False,
        is_sync_supported=manager is not None,
        is_credential_store_shared=view.is_credential_store_shared,
    )


def _write_prelude(agent_id: str) -> Response | tuple[dict[str, Any], LatchkeyPermissionGrantHandler]:
    """Auth + agent-id + JSON-body + handler lookup shared by every write route.

    Returns an error :class:`Response` (401 unauthenticated, 404 malformed
    workspace id, 400 invalid body, 503 with no permission handler wired), or
    ``(body, handler)`` on success.
    """
    if not is_ui_request_authenticated():
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
    :class:`LatchkeyGatewayClientError` (gateway unreachable) and
    :class:`MachineOperationError` (the workspace's own machine would not take
    the change) -> 502.

    A write that reached the machine leaves this computer's copies and the
    machine saying the same thing, so the answer is built from the copies
    rather than read back over the network. One that did *not* leaves them
    disagreeing until the next read of the pane, which adopts the machine's own
    state -- so the walk-back costs nothing and happens exactly where it shows.

    Whatever the call returns is discarded -- the refreshed view is the answer,
    never the write's own report -- so the parameter is typed for any return,
    the way ``app.py``'s ``_apply_revoke`` is.
    """
    try:
        apply_toggle()
    except (PermissionToggleError, PermissionOverviewError) as e:
        return make_json_error_response(str(e), status_code=400)
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not apply the permission change through the latchkey gateway: {}", e)
        return make_json_error_response(f"Could not apply the change through the latchkey gateway: {e}", 502)
    except MachineOperationError as e:
        logger.warning("Could not apply the permission change on the machine of {}: {}", agent_id, e)
        return make_json_error_response(str(e), 502)
    return json_response(build_permissions_payload(agent_id))


def _connect_service_on_machine(agent_id: str, service_name: str, account: str) -> None:
    """Hand an account just connected here to a *remote* workspace's machine.

    The credential was stored in this computer's copy of that machine's store
    first -- that is where a sign-in can land -- and this is what puts it where
    the workspace's agents will actually use it. Blocks until it lands there.

    A local workspace's agents run on this computer's credentials, so there is
    nothing to carry and the local change is the whole change; the same is true
    of an app built with no operator at all (a minimal setup, a test).

    Raises:
        MachineOperationError: when the machine does not take the credential.
    """
    operator = get_state().machine_operator
    if operator is not None:
        operator.connect_service(agent_id, service_name, account)


def _handle_workspace_permissions(agent_id: str) -> Response:
    """GET /ui/api/workspaces/<agent_id>/permissions: the Permissions pane's full payload.

    For a remote workspace this reads its machine first, so the pane shows the
    accounts that machine is connected to and the policy its gateway enforces
    -- not what this computer last wrote toward it.
    """
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    try:
        AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    return json_response(_load_permissions_payload(agent_id))


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
            push_permissions_to_machine=_push_permissions_to_machine(),
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
            push_permissions_to_machine=_push_permissions_to_machine(),
        ),
    )


def _handle_desktop_egress_toggle(agent_id: str) -> Response:
    """POST .../permissions/desktop-egress-toggle: send one service's requests through this computer, or stop."""
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        toggle_request = UiDesktopEgressToggleRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed desktop-egress-toggle body: {}", e)
        return make_json_error_response("service_name and enabled are required.", 400)
    return _apply_and_refresh(
        agent_id,
        lambda: apply_desktop_egress_toggle(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
            service_name=toggle_request.service_name,
            enabled=toggle_request.enabled,
            device_id=get_state().device_id,
            push_permissions_and_desktop_egress_rules_to_machine=(
                _push_permissions_and_desktop_egress_rules_to_machine()
            ),
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

    return _apply_and_refresh(agent_id, lambda: _connect_credentials_and_carry(handler, agent_id, connect_request))


def _connect_credentials_and_carry(
    handler: LatchkeyPermissionGrantHandler, agent_id: str, connect_request: UiConnectCredentialsRequest
) -> None:
    """Store typed-in credentials, then hand the account they landed under to the machine.

    Stored here first -- this computer's copy of the machine's store is where
    the credentials are assembled -- and then handed to the machine, which is
    where having them counts. It is scoped to the account the credentials
    landed under, so the machine's other accounts of the service stay exactly
    as it holds them.
    """
    account = connect_service_with_credentials(
        latchkey=machine_latchkey_for_workspace(handler.latchkey, get_state().backend_resolver, agent_id),
        services_catalog=handler.services_catalog,
        service_name=connect_request.service_name,
        value_by_parameter_name=connect_request.value_by_parameter_name,
        account_name=connect_request.account_name,
    )
    _connect_service_on_machine(agent_id, connect_request.service_name, account)


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
            push_permissions_to_machine=_push_permissions_to_machine(),
        ),
    )


def _handle_connector_disconnect(agent_id: str) -> Response:
    """POST .../permissions/connector-disconnect: clear one connector account's credential here.

    Unlike ``connector-revoke-all``, which drops this machine's grants and
    leaves the account connected, this clears the stored credential itself. It
    is the credential of the store *this machine* reads: a machine of its own
    holds it alone, so signing out there says nothing about the same account
    anywhere else, while this computer's store is shared by every local
    machine, so signing out of one of those signs out of all of them. Either
    way only this workspace's grants -- which now have nothing behind them --
    are stripped.

    The catalog is checked before anything is cleared: the clear is the
    destructive half, and :func:`revoke_service_account_for_workspace`
    would otherwise only reject an unknown service after the credential was
    already gone. A refused ``auth clear`` is latchkey failing rather than a bad
    request, so it answers 502 the way the settings page's Disconnect does. The
    strip runs on the request thread, so the refreshed view this returns is the
    file's actual state -- the pane decides where to land from the connection's
    absence.

    For a machine of its own, the store this computer keeps is only a scratch
    copy: the clear lands there and is then carried to the machine, whose own
    copy is the one its agents use, and this does not answer until it has. A
    machine that refuses the clear fails the whole disconnect, so the account
    is never shown as signed out while the machine can still use it.
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
        machine_latchkey = machine_latchkey_for_workspace(handler.latchkey, get_state().backend_resolver, agent_id)
        disconnect_account(machine_latchkey, disconnect_request.service_name, disconnect_request.account)
        if is_machine_store_of_its_own(handler.latchkey, machine_latchkey):
            # The credential is also the machine's -- what was just cleared is
            # only this computer's copy of it, and the machine's agents would
            # keep using their own -- so the clear is carried there too.
            _disconnect_account_on_machine(agent_id, disconnect_request.service_name, disconnect_request.account)
    except (PermissionOverviewError, PermissionToggleError, MachineOperationError) as e:
        # The account key is a personal identifier, so the service and
        # latchkey's own detail are all that is logged.
        logger.warning("Could not disconnect from {}: {}", disconnect_request.service_name, e)
        return make_json_error_response(str(e), 502)

    return _apply_and_refresh(
        agent_id,
        lambda: revoke_service_account_for_workspace(
            backend_resolver=get_state().backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            workspace_agent_id=agent_id,
            service_name=disconnect_request.service_name,
            account=disconnect_request.account,
            push_permissions_to_machine=_push_permissions_to_machine(),
        ),
    )


def _handle_shared_path(agent_id: str) -> Response:
    """POST .../permissions/shared-path: share a path, or change its access.

    Adding and re-grading are one write: both end with the agent holding one
    access mode on one path. The grant is filed with the gateway as a
    permission request against the *workspace's* permissions file and approved
    in the same breath, so the permission is computed by the same code that
    computes an agent's -- see
    :meth:`LatchkeyGatewayClient.create_file_sharing_request`.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        share_request = UiSharedPathRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed shared-path body: {}", e)
        return make_json_error_response("path is required, and access must be READ or WRITE.", status_code=400)
    host_id = resolve_workspace_host_id(get_state().backend_resolver, agent_id)
    if host_id is None:
        return make_json_error_response("Could not work out which machine to share with.", status_code=400)
    permissions_path = permissions_path_for_host(handler.latchkey.plugin_data_dir, host_id)
    try:
        request_id = handler.gateway_client.create_file_sharing_request(
            agent_id=agent_id,
            path=share_request.path,
            access=share_request.access,
            target=permissions_path,
        )
        handler.gateway_client.approve_permission_request(request_id)
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not share {} through the latchkey gateway: {}", share_request.path, e)
        return make_json_error_response(f"Could not share that path: {e}", status_code=502)
    # Narrowing to read means the wider grant has to go, or the agent keeps it.
    if share_request.access == FileSharingAccess.READ:
        _revoke_file_sharing_names(agent_id, handler, share_request.path, (FileSharingAccess.WRITE,))
    # Unconditionally, and never left to the revoke above: that one is a no-op
    # for a path being shared for the first time, and a no-op flip returns
    # without pushing anything (see ``apply_self_toggle``). The gateway spliced
    # this grant into this computer's copy only, and the next read of the pane
    # adopts the machine's policy back over it -- so a grant the machine is
    # never told about is deleted moments after it is made, taking its row with
    # it.
    try:
        _push_permissions_to_machine()(agent_id)
    except MachineOperationError as e:
        logger.warning("Shared {} but could not apply it on the machine: {}", share_request.path, e)
        return make_json_error_response(str(e), status_code=502)
    _carry_access_to_any_sync(agent_id, share_request.path)
    return json_response(build_permissions_payload(agent_id))


def _handle_shared_path_remove(agent_id: str) -> Response:
    """POST .../permissions/shared-path-remove: stop sharing a path entirely.

    Both access modes go, and any sync on the path is stopped with them --
    leaving one running would keep copying files the agent is no longer allowed
    to ask for -- and the machine's copy is deleted, since no row would be left
    to offer that from.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        remove_request = UiSharedPathRemoveRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed shared-path-remove body: {}", e)
        return make_json_error_response("path is required.", status_code=400)
    manager = get_state().folder_sync_manager
    if manager is not None:
        manager.forget_shared_path(agent_id, remove_request.path)
    try:
        _revoke_file_sharing_names(
            agent_id, handler, remove_request.path, (FileSharingAccess.READ, FileSharingAccess.WRITE)
        )
    except (PermissionToggleError, PermissionOverviewError) as e:
        return make_json_error_response(str(e), status_code=400)
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not stop sharing {} through the latchkey gateway: {}", remove_request.path, e)
        return make_json_error_response(f"Could not stop sharing that path: {e}", status_code=502)
    return json_response(build_permissions_payload(agent_id))


def _set_file_sharing_name(
    agent_id: str,
    handler: LatchkeyPermissionGrantHandler,
    path: str,
    access: FileSharingAccess,
    enabled: bool,
) -> None:
    """Turn one ``minds-file-server-<access>-<path>`` name on or off.

    Carried to the workspace's own machine like every other permissions edit:
    the file server the name governs runs where the agent does, so a grant this
    computer alone knows about is one the agent cannot use and a revoke it
    alone knows about is one the agent still has.
    """
    apply_self_toggle(
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        latchkey=handler.latchkey,
        workspace_agent_id=agent_id,
        permission=f"{FILE_SHARING_PERMISSION_PREFIX}{str(access).lower()}-{path}",
        enabled=enabled,
        push_permissions_to_machine=_push_permissions_to_machine(),
    )


def _revoke_file_sharing_names(
    agent_id: str,
    handler: LatchkeyPermissionGrantHandler,
    path: str,
    accesses: Sequence[FileSharingAccess],
) -> None:
    """Turn off the ``minds-file-server-<access>-<path>`` names for ``path``.

    A name the file never carried is simply not in the recomputed rule, so
    revoking one that was never granted writes nothing.
    """
    for access in accesses:
        _set_file_sharing_name(agent_id, handler, path, access, enabled=False)


def _handle_connect_browser(agent_id: str) -> Response:
    """POST .../permissions/connect-browser: connect a service by signing in, for this machine.

    The Add connection pane's action for a service latchkey *can* sign in to
    through a browser. The browser runs on the user's computer either way --
    there is nothing to open on a VPS -- but the account it establishes is
    stored in the asking machine's own credentials, which is what makes it that
    machine's connection rather than this computer's.

    Blocks for as long as the sign-in takes, and grants nothing: the account
    joins the pane with no permissions, exactly as typed-in credentials do.
    """
    prelude = _write_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    try:
        connect_request = UiConnectBrowserRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed connect-browser body: {}", e)
        return make_json_error_response("service_name is required.", 400)
    if not handler.services_catalog.get(connect_request.service_name):
        return make_json_error_response(f"Unknown service '{connect_request.service_name}'.", 400)
    try:
        machine_latchkey = machine_latchkey_for_workspace(handler.latchkey, get_state().backend_resolver, agent_id)
    except PermissionOverviewError as e:
        return make_json_error_response(str(e), 400)
    accounts_before = _stored_accounts(machine_latchkey, connect_request.service_name)
    is_success, detail = machine_latchkey.add_account(connect_request.service_name)
    if not is_success:
        # latchkey's own reason, which names a cancelled or failed sign-in; the
        # pane shows it beside the service it was trying to connect.
        return make_json_error_response(detail or "Sign-in did not complete.", 502)
    # The sign-in does not report which account it established, so it is read
    # off the store: the one account that was not there before. A sign-in that
    # added none (the user re-authenticated an account the store already had)
    # leaves nothing to scope by, so the whole service is handed over.
    added_accounts = _stored_accounts(machine_latchkey, connect_request.service_name) - accounts_before
    account = next(iter(added_accounts)) if len(added_accounts) == 1 else ""
    try:
        _connect_service_on_machine(agent_id, connect_request.service_name, account)
    except MachineOperationError as e:
        return make_json_error_response(str(e), 502)
    return json_response(build_permissions_payload(agent_id))


def _disconnect_account_on_machine(agent_id: str, service_name: str, account: str) -> None:
    """Clear one account from a *remote* workspace's own machine, blocking until it is gone.

    Raises:
        MachineOperationError: when the machine does not take the clear, which
            fails the whole sign-out rather than leaving the account looking
            gone while the machine can still use it.
    """
    operator = get_state().machine_operator
    if operator is not None:
        operator.disconnect_account(agent_id, service_name, account)


def _stored_accounts(machine_latchkey: Latchkey, service_name: str) -> frozenset[str]:
    """The accounts a machine's store holds for one service, read without a network round-trip."""
    return frozenset(entry.account for entry in machine_latchkey.auth_list(is_offline=True).get(service_name, ()))


# --- The sync half of a Local files row ---------------------------------------
#
# Not routes: the three ``folder-syncs/`` routes live in
# :mod:`ui_api_folder_syncs`. What is left here is what the *permissions*
# payload needs in order to draw a row -- each row carries the sync on its
# path, so these fill it -- plus the two functions that read a grant to decide
# something about a sync. Those two stay because the fact they read is the
# grant: which way changes travel is the access said again, and changing the
# access is what carries a running sync onto it. Keeping them here is what
# leaves the dependency between the two modules pointing one way.


def ui_path_sync(status: FolderSyncStatus, activity: FolderSyncActivity) -> UiPathSync:
    """The sync half of a row for a folder whose sync process still exists.

    ``activity`` is where the user asked the folder to end up, which is not
    always where it is: a folder on its way out still has a running process for
    a moment, and the radio must follow the click rather than flick back to the
    state the folder has not left yet.
    """
    return UiPathSync(
        activity=activity,
        state=status.state,
        message=status.message,
        direction=status.spec.direction,
        conflict=status.spec.conflict,
        workspace_path=f"~/{WORKSPACE_SYNC_DIRECTORY}/{status.spec.workspace_path}",
        bytes_done=status.bytes_done,
        bytes_total=status.bytes_total,
    )


def ui_remembered_sync(record: FolderSyncRecord, device_id: str) -> UiPathSync:
    """The sync half of a row for a folder that was synced and is not now.

    ``state`` is STOPPED because no process is running; what the pane actually
    reads is ``activity``, which says whether the machine is still holding the
    copy. The path is reported for an INACTIVE copy so the user can find it,
    and left empty for a discarded one, where there is nothing to find.
    """
    workspace_path = f"{device_id}/{record.local_path.lstrip('/')}"
    is_set_aside = record.activity == FolderSyncActivity.INACTIVE
    return UiPathSync(
        activity=record.activity,
        state=FolderSyncState.STOPPED,
        message="",
        direction=record.direction,
        conflict=record.conflict,
        workspace_path=(f"~/{WORKSPACE_INACTIVE_SYNC_DIRECTORY}/{workspace_path}" if is_set_aside else ""),
    )


def _with_restore_failure(sync: UiPathSync, failure: str) -> UiPathSync:
    """Report a sync that could not be brought back at launch as the failure it is."""
    if not failure or sync.state != FolderSyncState.STOPPED:
        return sync
    return sync.model_copy_update(
        to_update(sync.field_ref().state, FolderSyncState.FAILED),
        to_update(sync.field_ref().message, failure),
    )


def sync_overlap_warning(manager: FolderSyncManager | None, path: str, agent_id: str) -> str:
    """What to say about another workspace already syncing this folder, or one around it.

    Allowed, so this is a warning rather than a refusal: the copies live under
    different machines' home directories and cannot collide. What the user
    cannot see from this pane is the cost -- the same bytes cross the network
    once per workspace -- or that each row's clash rule settles only its own
    pairing, so with several in play the result depends on which sync reaches a
    change first.
    """
    if manager is None:
        return ""
    overlapping = manager.overlapping_paths_in_other_workspaces(path, agent_id)
    if not overlapping:
        return ""
    if overlapping == (path,):
        subject = "This folder is also synced with another workspace."
    else:
        subject = "A folder that overlaps this one is also synced with another workspace."
    return (
        f"{subject} That works, but it means more network use, and when the same file is changed "
        "in two places at once the result may be unpredictable."
    )


def sync_direction_for(agent_id: str, path: str) -> FolderSyncDirection:
    """Which way a folder's changes travel, from the access its agent was granted.

    Not a separate choice, because it would be the same choice twice. Read means
    the agent may look at the folder, so changes travel from this computer to
    the workspace; read and write means it may change the folder too, so they
    travel both ways. Deriving it here rather than taking it from the request is
    what keeps the two from disagreeing -- including when the access is changed
    on a folder that is already syncing.

    Defaults to the narrower direction for a path with no grant to read, which
    cannot normally happen: a path is only syncable once it is shared.
    """
    view = _build_permissions_view_or_none(agent_id)
    if view is None:
        return FolderSyncDirection.TO_WORKSPACE
    for toggle in view.file_sharing_toggles:
        if not toggle.is_granted:
            continue
        parsed = parse_file_sharing_permission(toggle.permission)
        if parsed is not None and parsed[1] == path and parsed[0] == "write":
            return FolderSyncDirection.BOTH
    return FolderSyncDirection.TO_WORKSPACE


def _carry_access_to_any_sync(agent_id: str, path: str) -> None:
    """Re-assert a running sync's direction after its access changed.

    The direction follows the access, so widening or narrowing one has to move
    the other. The manager replaces just the unison process when only the
    settings differ, so this costs nothing when the direction did not change.
    """
    manager = get_state().folder_sync_manager
    if manager is None or manager.desired_activity_for(agent_id, path) != FolderSyncActivity.ACTIVE:
        return
    try:
        manager.start(
            agent_id=agent_id,
            raw_local_path=path,
            direction=sync_direction_for(agent_id, path),
            conflict=manager.desired_conflict_for(agent_id, path),
        )
    except FolderSyncError as e:
        logger.warning("Changed the access on {} but could not carry it to its sync: {}", path, e)


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
        "/api/workspaces/<agent_id>/permissions/desktop-egress-toggle",
        view_func=_handle_desktop_egress_toggle,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/self-toggle",
        view_func=_handle_self_toggle,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/connect-browser",
        view_func=_handle_connect_browser,
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
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/shared-path",
        view_func=_handle_shared_path,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/permissions/shared-path-remove",
        view_func=_handle_shared_path_remove,
        methods=["POST"],
    )
