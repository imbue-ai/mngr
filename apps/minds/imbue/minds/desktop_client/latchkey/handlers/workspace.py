"""Cross-workspace permission grant/deny flow (wire ``request_type == "workspace"``).

This module is the third sibling handler under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the flow for
*workspace* permission requests: an agent in one workspace asking to act on the
minds cross-workspace management API (``/api/v1/workspaces/...``) -- listing,
reading, creating, destroying, starting/stopping, exporting backups, and
establishing SSH access against *other* workspaces.

Unlike the :mod:`.predefined` (catalog-backed) sibling, a workspace grant is
*target-scoped*: the verbs that act on a single workspace (destroy / lifecycle /
backups-export / ssh) are gated per target workspace id. The dialog lets the
user pick which verbs to grant and -- when the request names a target workspace
-- whether the targeted verbs apply to that one workspace ("selected") or to all
workspaces ("all").

The grant is applied exactly like the :mod:`.file_sharing` sibling: the
precomputed (or override-recomputed) ``effect`` is spliced into the requesting
agent's per-host ``latchkey_permissions.json`` by the gateway's
``permission-requests`` extension via ``POST /permission-requests/approve/<id>``
(which also drops the pending record). The handler sends an override body
carrying the user's dialog choices (``{permissions, target_workspace_id}``) so
the gateway recomputes the effect: for a "selected" grant each targeted verb
becomes a uniquely-named per-target schema (so repeated grants accumulate
targets through the gateway's schema-by-name merge), and for an "all" grant a
broad schema. Denial drops the pending record via
``DELETE /permission-requests/<id>``.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_WORKSPACE
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.gateway_client import WorkspaceRequestPayload
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.request_handler import UiWorkspacePermissionDetail
from imbue.minds.desktop_client.request_handler import UiWorkspaceVerbChoice
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "machine access"

# Form fields. ``permissions`` carries the checked verb names (shared with the
# other dialogs so the inbox shell's Approve gating works). ``target_scope``
# carries the all-vs-selected radio choice.
_TARGET_SCOPE_FIELD: Final[str] = "target_scope"
_TARGET_SCOPE_SELECTED: Final[str] = "selected"
_TARGET_SCOPE_ALL: Final[str] = "all"

_VERB_DISPLAY_BY_PERMISSION: Final[dict[str, str]] = {verb.permission: verb.display_name for verb in WORKSPACE_VERBS}


def _resolve_target_name(
    backend_resolver: BackendResolverInterface,
    target_workspace_id: str | None,
) -> str | None:
    """Resolve a friendly name for the request's target workspace, if any.

    Returns ``None`` when the request names no target. Falls back to the raw id
    when the target is unknown to discovery (e.g. a destroyed-but-backed-up
    workspace).
    """
    if not target_workspace_id:
        return None
    try:
        parsed = AgentId(target_workspace_id)
    except ValueError:
        return target_workspace_id
    return resolve_workspace_display_name(backend_resolver, parsed, fallback=target_workspace_id)


def _format_granted_message(granted: Sequence[str], target_label: str) -> str:
    verbs = ", ".join(_VERB_DISPLAY_BY_PERMISSION.get(verb, verb) for verb in granted)
    return f"Your cross-workspace permission request was granted ({verbs}) for {target_label}."


def _format_denied_message() -> str:
    return "Your cross-workspace permission request was denied."


class WorkspacePermissionGrantHandler(RequestEventHandler):
    """Handler for cross-workspace (minds-workspaces) permission requests.

    Renders the verb + all-vs-selected dialog, approves the request through the
    gateway's ``POST /permission-requests/approve/<id>`` endpoint (sending the
    user's dialog choices as an override body so the gateway recomputes and
    splices the effect into the requesting agent's per-host permissions file),
    writes the response event, and notifies the waiting agent via
    ``mngr message``. Denial drops the pending record via ``DELETE``.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ``~/.minds``).")
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to ``POST /permission-requests/approve/<id>`` (grant) and "
            "``DELETE /permission-requests/<id>`` (deny) on the gateway's bundled extension."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Sends ``mngr message`` nudges to the waiting agent on resolution.",
    )

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return REQUEST_TYPE_WORKSPACE

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        payload = permission_request.payload
        if not isinstance(payload, WorkspaceRequestPayload):
            return ""
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        target_name = _resolve_target_name(backend_resolver, payload.target_workspace_id)
        return f"Workspace access: {target_name}" if target_name else "Machine access"

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        payload = permission_request.payload
        if not isinstance(payload, WorkspaceRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        parsed_agent_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(
            backend_resolver, parsed_agent_id, fallback=permission_request.agent_id
        )
        target_name = _resolve_target_name(backend_resolver, payload.target_workspace_id)
        requested = set(payload.permissions)
        checked = tuple(verb.permission for verb in WORKSPACE_VERBS if verb.permission in requested)
        return UiWorkspacePermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            rationale=permission_request.rationale,
            verbs=tuple(
                UiWorkspaceVerbChoice(
                    permission=verb.permission,
                    display_name=verb.display_name,
                    description=verb.description,
                    is_targeted=verb.is_targeted,
                )
                for verb in WORKSPACE_VERBS
            ),
            checked_permissions=checked,
            target_workspace_id=payload.target_workspace_id,
            target_workspace_name=target_name,
            show_target_choice=bool(target_name),
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        payload = permission_request.payload
        if not isinstance(payload, WorkspaceRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)

        form = request.form
        granted_permissions = tuple(str(v) for v in form.getlist("permissions"))
        if not granted_permissions:
            return make_json_error_response(
                "At least one permission must be selected to approve the request.",
                status_code=400,
            )

        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver

        # Resolve the target the targeted verbs apply to. "selected" pins the
        # request's target workspace; "all" (or a missing target) grants
        # broadly. The gateway recomputes the effect from this override and
        # writes it to the request's stored ``target`` permissions file (the
        # requesting agent's per-host file, reached via its opaque handle).
        target_scope = form.get(_TARGET_SCOPE_FIELD, _TARGET_SCOPE_ALL)
        target_workspace_id: str | None = None
        if target_scope == _TARGET_SCOPE_SELECTED and payload.target_workspace_id:
            target_workspace_id = payload.target_workspace_id

        try:
            self.gateway_client.approve_permission_request(
                request_event_id,
                override_body={
                    "permissions": list(granted_permissions),
                    "target_workspace_id": target_workspace_id,
                },
            )
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not approve minds-workspaces request {} via gateway: {}", request_event_id, e)
            return make_json_error_response(
                f"Could not approve the cross-workspace request through the latchkey gateway: {e}",
                status_code=502,
            )

        target_label = (
            _resolve_target_name(backend_resolver, payload.target_workspace_id) or "the selected machine"
            if target_workspace_id is not None
            else "all machines"
        )
        message = _format_granted_message(granted_permissions, target_label)
        resolve_request(
            self.mngr_message_sender,
            self.data_dir,
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.GRANTED,
            message=message,
        )
        return make_response(
            content=json.dumps({"outcome": "GRANTED", "message": message}),
            media_type="application/json",
        )

    def apply_deny_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        payload = permission_request.payload
        if not isinstance(payload, WorkspaceRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        # DELETE tolerates 404 -- if the request is already gone we still want to
        # write the response event and notify the agent.
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE machine permission request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )
        message = _format_denied_message()
        resolve_request(
            self.mngr_message_sender,
            self.data_dir,
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.DENIED,
            message=message,
        )
        return make_response(
            content=json.dumps({"outcome": "DENIED", "message": message}),
            media_type="application/json",
        )

    # -- Internals -----------------------------------------------------------
