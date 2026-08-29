"""/ui/api routes owned by tranche T5 (inbox/latchkey/help).

The SPA inbox reads a typed card list + per-kind typed detail payloads and
submits grants/denies to the ``/requests/<id>/grant|deny`` routes in
``app.py`` (the handlers' form contracts are unchanged).
``_displayable_pending_requests`` deliberately duplicates the same-named
helper in ``app.py`` (still live there for the titlebar badge count):
importing it from ``app.py`` would be a circular import (``app`` imports
``ui_api`` imports this module).
"""

from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Final
from typing import Literal

from flask import Blueprint
from flask import Response
from flask import request
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.pending_requests import PendingRequestsInterface
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.notification_feed import PendingNotificationCard
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId


class UiInboxCard(FrozenModel):
    """One pending-request row in the SPA inbox's left list."""

    id: str = Field(description="Request event id (selection + grant/deny routes key on it)")
    kind_label: str = Field(description="Short lower-case request-kind label (e.g. 'permission')")
    ws_name: str = Field(description="Workspace display name the request came from")
    display_name: str = Field(description="Secondary label (e.g. the friendly service name)")
    accent: str = Field(description="Workspace accent hex for the card's selection edge")
    workspace_agent_id: str = Field(
        description=(
            "Agent id of the WORKSPACE the request belongs to -- the primary agent whose tile the user "
            "sees, resolved by workspace name. This is deliberately not the request's own ``agent_id``: "
            "latchkey requests are filed by the workspace's system-services sibling agent, so the two "
            "never match. Empty when the workspace could not be resolved."
        )
    )


class UiInboxListResponse(FrozenModel):
    """The SPA inbox's card list (most-recent-first, displayable requests only)."""

    cards: tuple[UiInboxCard, ...] = Field(description="Pending request cards in display order")


# Newest verdicts returned per lookup; mirrors
# MAX_PERMISSION_RESOLUTION_ENTRIES in static/embed_contract.js (the result is
# relayed into the workspace frame as one contract message).
_MAX_RESOLUTION_ENTRIES: Final[int] = 64


class UiPermissionResolutionVerdict(LowerCaseStrEnum):
    """A resolved request's verdict, as the embed contract spells it on the wire."""

    GRANTED = auto()
    DENIED = auto()


# The wire verdict for each recordable RequestStatus (which spells them
# GRANTED/DENIED); statuses outside this map are never sent.
_RESOLUTION_BY_STATUS: Final[dict[str, UiPermissionResolutionVerdict]] = {
    str(RequestStatus.GRANTED): UiPermissionResolutionVerdict.GRANTED,
    str(RequestStatus.DENIED): UiPermissionResolutionVerdict.DENIED,
}


class UiPermissionResolution(FrozenModel):
    """One answered permission request: its id and the user's verdict."""

    request_id: str = Field(description="The resolved request's event id (the gateway request id)")
    resolution: UiPermissionResolutionVerdict = Field(description="The verdict the user gave")


class UiPermissionResolutionsResponse(FrozenModel):
    """The workspace's recorded verdicts, newest last, capped at _MAX_RESOLUTION_ENTRIES."""

    resolutions: tuple[UiPermissionResolution, ...] = Field(description="Answered requests, oldest first")


class UiInboxUnavailableDetail(FrozenModel):
    """Detail payload for a request that can no longer be acted on."""

    kind: Literal["unavailable"] = "unavailable"
    message: str = Field(description="Supporting copy explaining why (may be empty)")


class UiInboxDetailResponse(FrozenModel):
    """Envelope for the right-pane detail payload (discriminated by ``detail.kind``)."""

    detail: RequestDetailPayload | UiInboxUnavailableDetail = Field(description="The per-kind detail payload")


def _json_response(payload: FrozenModel, status_code: int = 200) -> Response:
    return Response(payload.model_dump_json(), status=status_code, mimetype="application/json")


def displayable_pending_requests(
    pending_requests: PendingRequestsInterface | None,
    backend_resolver: BackendResolverInterface,
) -> list[StreamedPermissionRequest]:
    """Pending requests whose originating agent's host is currently resolvable.

    The one displayable-set derivation: the SPA inbox list, the titlebar
    badge, and the notification feed all read it, so they always agree.
    Requests from since-vanished workspaces would render as meaningless hex
    ids, so they are hidden until their host reappears.
    """
    pending = list(pending_requests.list_pending()) if pending_requests else []
    displayable: list[StreamedPermissionRequest] = []
    for req in pending:
        try:
            agent_id = AgentId(req.agent_id)
        except InvalidRandomIdError:
            continue
        if backend_resolver.get_agent_display_info(agent_id) is not None:
            displayable.append(req)
    return displayable


def _resolved_workspace_accent(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str:
    stored = backend_resolver.get_workspace_color(agent_id)
    return stored if stored is not None else DEFAULT_WORKSPACE_COLOR


def _build_inbox_card(
    req: StreamedPermissionRequest,
    handler: RequestEventHandler | None,
    backend_resolver: BackendResolverInterface,
    primary_agent_id_by_ws_name: dict[str, str],
) -> UiInboxCard:
    if handler is not None:
        kind_label = handler.kind_label()
        display_name = handler.display_name_for_event(req)
    else:
        # Unknown request type: still surfaced so the user sees something is
        # wrong (it cannot be rendered or resolved without a handler).
        kind_label = "request"
        display_name = ""
    parsed_id = AgentId(req.agent_id)
    ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
    if not ws_name:
        info = backend_resolver.get_agent_display_info(parsed_id)
        ws_name = info.agent_name if info else req.agent_id[:16]
    # Accent follows the homepage tile for the workspace: requests are filed
    # by the system-services sibling agent, so resolve through the
    # user-facing agent that shares the workspace name.
    primary_agent_id_str = primary_agent_id_by_ws_name.get(ws_name)
    accent = (
        _resolved_workspace_accent(backend_resolver, AgentId(primary_agent_id_str))
        if primary_agent_id_str is not None
        else DEFAULT_WORKSPACE_COLOR
    )
    return UiInboxCard(
        id=req.request_id,
        kind_label=kind_label,
        ws_name=ws_name,
        display_name=display_name,
        accent=accent,
        workspace_agent_id=primary_agent_id_str or "",
    )


def primary_agent_ids_by_workspace_name(backend_resolver: BackendResolverInterface) -> dict[str, str]:
    """First-seen primary agent id per workspace name.

    Latchkey requests are filed by the workspace's system-services sibling
    agent, so card derivations resolve accent and deep-link identity through
    the user-facing primary agent that shares the workspace name.
    """
    primary_agent_id_by_ws_name: dict[str, str] = {}
    for aid in backend_resolver.list_known_workspace_ids():
        wn = backend_resolver.get_workspace_name(aid)
        if wn and wn not in primary_agent_id_by_ws_name:
            primary_agent_id_by_ws_name[wn] = str(aid)
    return primary_agent_id_by_ws_name


def _request_service_name(req: StreamedPermissionRequest, handler: RequestEventHandler | None) -> str:
    """The catalog service name for the brand mark; '' for kinds that have none.

    Mirrors the ``service_name`` derivation of the Permissions pane's
    "Waiting on you" rows: only predefined-permission requests resolve to a
    catalog service.
    """
    if not isinstance(req.payload, PredefinedRequestPayload):
        return ""
    if not isinstance(handler, LatchkeyPermissionGrantHandler):
        return ""
    info = handler.services_catalog.get_by_scope(req.payload.scope)
    return info.name if info is not None else ""


def build_notification_card(
    req: StreamedPermissionRequest,
    handlers: tuple[RequestEventHandler, ...],
    backend_resolver: BackendResolverInterface,
    primary_agent_id_by_ws_name: dict[str, str],
) -> PendingNotificationCard:
    """Feed-input display fields for one displayable pending request.

    Composes the inbox-card derivation (title, workspace name, accent, agent
    id) with the rationale and brand-mark service the feed rows additionally
    render, so a notification entry always matches the inbox card it mirrors.
    """
    handler = find_handler_for_event(handlers, req)
    card = _build_inbox_card(req, handler, backend_resolver, primary_agent_id_by_ws_name)
    return PendingNotificationCard(
        request_id=card.id,
        # The gateway record carries no timestamp; first sight of the request
        # here is when it starts existing for notification purposes.
        requested_at=datetime.now(timezone.utc).isoformat(),
        # display_name is empty only for unknown request kinds; the kind label
        # ("request") keeps those rows from rendering a blank headline.
        title=card.display_name or card.kind_label,
        body=req.rationale,
        workspace_agent_id=card.workspace_agent_id,
        workspace_name=card.ws_name,
        workspace_accent=card.accent,
        service_name=_request_service_name(req, handler),
    )


def _handle_inbox_list() -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    state = get_state()
    backend_resolver = state.backend_resolver
    handlers: tuple[RequestEventHandler, ...] = state.request_event_handlers
    pending = displayable_pending_requests(state.pending_requests, backend_resolver)
    primary_agent_id_by_ws_name = primary_agent_ids_by_workspace_name(backend_resolver)
    cards = tuple(
        _build_inbox_card(req, find_handler_for_event(handlers, req), backend_resolver, primary_agent_id_by_ws_name)
        for req in pending
    )
    return _json_response(UiInboxListResponse(cards=cards))


def _handle_inbox_resolutions() -> Response:
    """The verdicts of one workspace's permission requests, for its frame's cards.

    Read by the shell whenever it (re)loads a workspace frame, and pushed into
    the frame as a PERMISSION_RESOLUTIONS snapshot -- so a page rebuilt after
    a verdict was given while it was not live never offers Approve/Deny for a
    decided request. The source is the inbox's response list (seeded at
    startup from the on-disk response event log, the one record of a verdict
    that is always written). ``workspace`` is the frame's agent-scoped id, and
    only that workspace's own requests are answered (matched by workspace
    name, since responses record the system-services sibling that filed the
    request) -- verdicts must not read across workspace boundaries.
    """
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    try:
        workspace_agent_id = AgentId(request.args.get("workspace", ""))
    except InvalidRandomIdError:
        return make_json_error_response("workspace must be an agent id", status_code=400)
    state = get_state()
    pending: PendingRequestsInterface | None = state.pending_requests
    asking_ws_name = state.backend_resolver.get_workspace_name(workspace_agent_id)
    if pending is None or not asking_ws_name:
        return _json_response(UiPermissionResolutionsResponse(resolutions=()))
    # Later responses win, though a request should only ever be answered once.
    resolution_by_id: dict[str, UiPermissionResolutionVerdict] = {}
    for response_event in pending.responses():
        resolution = _RESOLUTION_BY_STATUS.get(response_event.status)
        if resolution is None:
            continue
        try:
            filer_id = AgentId(response_event.agent_id)
        except InvalidRandomIdError:
            continue
        if state.backend_resolver.get_workspace_name(filer_id) != asking_ws_name:
            continue
        resolution_by_id[response_event.request_event_id] = resolution
    newest_capped = list(resolution_by_id.items())[-_MAX_RESOLUTION_ENTRIES:]
    resolutions = tuple(
        UiPermissionResolution(request_id=request_id, resolution=resolution)
        for request_id, resolution in newest_capped
    )
    return _json_response(UiPermissionResolutionsResponse(resolutions=resolutions))


def _handle_inbox_detail(request_id: str) -> Response:
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", status_code=401)
    state = get_state()
    pending: PendingRequestsInterface | None = state.pending_requests
    if pending is None:
        return _json_response(UiInboxDetailResponse(detail=UiInboxUnavailableDetail(message="")))
    if pending.is_resolved(request_id):
        return _json_response(
            UiInboxDetailResponse(detail=UiInboxUnavailableDetail(message="It has already been processed."))
        )
    permission_request = pending.get_pending(request_id)
    if permission_request is None:
        return _json_response(
            UiInboxDetailResponse(
                detail=UiInboxUnavailableDetail(message="It may have expired, or it was opened from an old link.")
            )
        )
    handlers: tuple[RequestEventHandler, ...] = state.request_event_handlers
    handler = find_handler_for_event(handlers, permission_request)
    if handler is None:
        return make_json_error_response(
            f"No handler registered for request type {permission_request.request_type!r}", status_code=500
        )
    payload = handler.build_request_detail_payload(
        permission_request=permission_request, backend_resolver=state.backend_resolver
    )
    return _json_response(UiInboxDetailResponse(detail=payload))


def register_inbox_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/inbox", view_func=_handle_inbox_list, methods=["GET"])
    blueprint.add_url_rule("/api/inbox/resolutions", view_func=_handle_inbox_resolutions, methods=["GET"])
    blueprint.add_url_rule("/api/inbox/<request_id>/detail", view_func=_handle_inbox_detail, methods=["GET"])
