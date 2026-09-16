"""/ui/api routes for keeping a shared folder in sync with a workspace's machine.

Three routes under ``/api/workspaces/<agent_id>/folder-syncs``: ``toggle``
turns syncing on or off, ``discard-copy`` deletes a copy the machine set
aside, and a ``GET`` on the collection answers the pane's poll. None of them
touches latchkey. The folder is already shared -- these only decide whether a
copy of it is kept level with the machine, by starting or stopping an
``mngr pair`` subprocess (see :mod:`folder_sync`), and the machine is never
asked anything: the sync is this computer's own subprocess, so its state is
known here.

That is why they are addressed separately from the grants. They are still
*drawn* in the same card, which is why the two writes answer with the whole
permissions payload rather than with sync data alone: the pane adopts one
response whatever it just changed.

The dependency between this module and :mod:`ui_api_permissions` points one
way, here to there, and is kept that way deliberately. Two facts about a sync
are really facts about the grant -- which way changes travel, and that changing
the access carries a running sync onto it -- so the functions that read them
live with the grants, and this module imports one of them. Nothing over there
imports from here except the route registration below, which the blueprint
calls directly.
"""

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_permissions import build_permissions_payload
from imbue.minds.desktop_client.ui_api_permissions import json_response
from imbue.minds.desktop_client.ui_api_permissions import sync_direction_for
from imbue.minds.desktop_client.ui_api_permissions import sync_overlap_warning
from imbue.minds.desktop_client.ui_api_permissions import ui_path_sync
from imbue.minds.desktop_client.ui_api_permissions import ui_remembered_sync
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiFolderSyncDiscardCopyRequest
from imbue.minds.desktop_client.ui_models import UiFolderSyncRetryRequest
from imbue.minds.desktop_client.ui_models import UiFolderSyncRow
from imbue.minds.desktop_client.ui_models import UiFolderSyncToggleRequest
from imbue.minds.desktop_client.ui_models import UiFolderSyncs
from imbue.minds.errors import FolderSyncError
from imbue.mngr.primitives import AgentId


def _require_folder_sync_manager() -> Response | FolderSyncManager:
    """The manager, or the 503 the route answers with when this build has none."""
    manager = get_state().folder_sync_manager
    if manager is None:
        return make_json_error_response("Keeping a shared path in sync is unavailable in this build.", status_code=503)
    return manager


def _with_in_flight(row: UiFolderSyncRow, manager: FolderSyncManager, agent_id: str) -> UiFolderSyncRow:
    """Overlay what a sync is in the middle of, which outranks its settled state."""
    in_flight = manager.in_flight_state_for(agent_id, row.path)
    if row.sync is None or in_flight is None:
        return row
    return row.model_copy_update(
        to_update(row.field_ref().sync, row.sync.model_copy_update(to_update(row.sync.field_ref().state, in_flight)))
    )


def _handle_path_sync(agent_id: str) -> Response:
    """POST .../folder-syncs/toggle: turn syncing on or off for one shared folder.

    A flip like the toggles above it, and answered the same way -- with the
    refreshed view -- so the pane renders one payload however it changed. It
    does not touch latchkey: the path is already shared, and this only decides
    whether a copy of it is kept level with the machine.
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
    try:
        sync_request = UiFolderSyncToggleRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed folder-sync toggle body: {}", e)
        return make_json_error_response(
            "path and enabled are required, and conflict must be a known value.", status_code=400
        )
    manager = _require_folder_sync_manager()
    if isinstance(manager, Response):
        return manager
    try:
        if sync_request.enabled:
            manager.start(
                agent_id=agent_id,
                raw_local_path=sync_request.path,
                direction=sync_direction_for(agent_id, sync_request.path),
                conflict=sync_request.conflict,
            )
        else:
            # Stopping sets the machine's copy aside and leaves the row saying
            # so, which is what the Delete-copy button acts on; only the
            # running process is dropped from the in-memory list.
            manager.stop(agent_id, sync_request.path)
            manager.forget(agent_id, sync_request.path)
    except FolderSyncError as e:
        return make_json_error_response(str(e), status_code=400)
    return json_response(build_permissions_payload(agent_id))


def _handle_folder_syncs(agent_id: str) -> Response:
    """GET .../folder-syncs: the sync half of every row, and nothing else.

    What the pane polls while a sync is running. Everything it answers with is
    already in this process: a running sync reports itself through its
    ``mngr pair`` subprocess, and a stopped one is remembered on disk. So this
    touches neither the latchkey gateway nor the workspace's machine -- which
    the full permissions read does, over SSH, taking about a second. Polling
    that one put a remote round trip between the user and every click. Being
    local is also what lets the pane tick once a second, which is as often as
    ``mngr pair`` reports a transfer's progress.
    """
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    try:
        AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    manager = get_state().folder_sync_manager
    if manager is None:
        return json_response(UiFolderSyncs(), 200)
    rows = tuple(
        UiFolderSyncRow(
            path=record.local_path,
            sync=ui_remembered_sync(record, manager.device_id),
            overlap_warning=sync_overlap_warning(manager, record.local_path, agent_id),
        )
        for record in manager.remembered_for_agent(agent_id)
    )
    # A running sync is the live answer, and overrides what was remembered.
    live = {
        status.spec.local_path: ui_path_sync(
            status, manager.desired_activity_for(agent_id, status.spec.local_path) or FolderSyncActivity.ACTIVE
        )
        for status in manager.list_for_agent(agent_id)
    }
    merged: list[UiFolderSyncRow] = []
    for row in rows:
        sync = live.pop(row.path, None) or row.sync
        merged.append(
            _with_in_flight(
                UiFolderSyncRow(path=row.path, sync=sync, overlap_warning=row.overlap_warning), manager, agent_id
            )
        )
    for path, sync in live.items():
        merged.append(
            _with_in_flight(
                UiFolderSyncRow(path=path, sync=sync, overlap_warning=sync_overlap_warning(manager, path, agent_id)),
                manager,
                agent_id,
            )
        )
    return json_response(UiFolderSyncs(rows=tuple(merged)), 200)


def _handle_path_copy_discard(agent_id: str) -> Response:
    """POST .../folder-syncs/discard-copy: delete a set-aside copy for good.

    Only ever offered for a folder whose sync is off, and only once the pane
    can show that a copy is being kept -- deleting files is not something to
    fold into turning a switch off. Answered with the refreshed view, like
    every other write here.
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
    try:
        discard_request = UiFolderSyncDiscardCopyRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed discard-copy body: {}", e)
        return make_json_error_response("path is required.", status_code=400)
    manager = _require_folder_sync_manager()
    if isinstance(manager, Response):
        return manager
    try:
        manager.discard_copy(agent_id, discard_request.path)
    except FolderSyncError as e:
        return make_json_error_response(str(e), status_code=400)
    return json_response(build_permissions_payload(agent_id))


def _handle_folder_sync_retry(agent_id: str) -> Response:
    """POST .../folder-syncs/retry: bring a failed sync up again.

    Its own route rather than a second meaning for ``toggle``: the checkbox is
    already on, so there is no flip to send, and a toggle that meant "on, and I
    mean it this time" would be a different request wearing the same name.
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
    try:
        retry_request = UiFolderSyncRetryRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed folder-sync retry body: {}", e)
        return make_json_error_response("path is required.", status_code=400)
    manager = _require_folder_sync_manager()
    if isinstance(manager, Response):
        return manager
    try:
        manager.retry(agent_id, retry_request.path)
    except FolderSyncError as e:
        return make_json_error_response(str(e), status_code=400)
    return json_response(build_permissions_payload(agent_id))


def register_folder_sync_routes(blueprint: Blueprint) -> None:
    """Register the folder-sync routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/folder-syncs",
        view_func=_handle_folder_syncs,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/folder-syncs/toggle",
        view_func=_handle_path_sync,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/folder-syncs/retry",
        view_func=_handle_folder_sync_retry,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/folder-syncs/discard-copy",
        view_func=_handle_path_copy_discard,
        methods=["POST"],
    )
