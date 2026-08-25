"""/ui/api routes owned by tranche T5 (inbox/latchkey/help).

The SPA inbox reads a typed card list + per-kind typed detail payloads and
submits grants/denies to the ``/requests/<id>/grant|deny`` routes in
``app.py`` (the handlers' form contracts are unchanged).
``_displayable_pending_requests`` deliberately duplicates the same-named
helper in ``app.py`` (still live there for the titlebar badge count):
importing it from ``app.py`` would be a circular import (``app`` imports
``ui_api`` imports this module).
"""

import json
import os
from typing import Literal

from flask import Blueprint
from flask import Response
from flask import request
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.notification_feed import PendingNotificationCard
from imbue.minds.desktop_client.request_events import LatchkeyAccountsPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyWorkspacePermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.state import get_state
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


class UiInboxUnavailableDetail(FrozenModel):
    """Detail payload for a request that can no longer be acted on."""

    kind: Literal["unavailable"] = "unavailable"
    message: str = Field(description="Supporting copy explaining why (may be empty)")


class UiInboxDetailResponse(FrozenModel):
    """Envelope for the right-pane detail payload (discriminated by ``detail.kind``)."""

    detail: RequestDetailPayload | UiInboxUnavailableDetail = Field(description="The per-kind detail payload")


def _is_inbox_request_authenticated() -> bool:
    """Session-cookie check; mirrors ``ui_api.is_ui_request_authenticated``.

    Duplicated (5 lines) because ``ui_api`` imports this module at load time,
    so importing back would be circular; a shared guard hoisted onto the /ui
    blueprint would remove the duplication.
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


def _json_error(message: str, status_code: int) -> Response:
    return Response(json.dumps({"error": message}), status=status_code, mimetype="application/json")


def displayable_pending_requests(
    inbox: RequestInbox | None,
    backend_resolver: BackendResolverInterface,
) -> list[RequestEvent]:
    """Pending requests whose originating agent's host is currently resolvable.

    Twin of the same-named helper in ``app.py`` (which feeds the titlebar
    badge count) -- keep the two in lockstep so the badge and the inbox list
    agree. Requests from since-vanished workspaces would render as
    meaningless hex ids, so they are hidden until their host reappears.
    """
    pending = inbox.get_pending_requests() if inbox else []
    displayable: list[RequestEvent] = []
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
    req: RequestEvent,
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
        id=str(req.event_id),
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


def _request_rationale(req: RequestEvent) -> str:
    """The request's human-readable rationale line; '' for kinds that carry none."""
    if isinstance(
        req,
        (
            LatchkeyPredefinedPermissionRequestEvent,
            LatchkeyFileSharingPermissionRequestEvent,
            LatchkeyWorkspacePermissionRequestEvent,
            LatchkeyAccountsPermissionRequestEvent,
        ),
    ):
        return req.rationale
    return ""


def _request_service_name(req: RequestEvent, handler: RequestEventHandler | None) -> str:
    """The catalog service name for the brand mark; '' for kinds that have none.

    Mirrors the ``service_name`` derivation of the Permissions pane's
    "Waiting on you" rows: only predefined-permission requests resolve to a
    catalog service.
    """
    if not isinstance(req, LatchkeyPredefinedPermissionRequestEvent):
        return ""
    if not isinstance(handler, LatchkeyPermissionGrantHandler):
        return ""
    info = handler.services_catalog.get_by_scope(req.scope)
    return info.name if info is not None else ""


def build_notification_card(
    req: RequestEvent,
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
        requested_at=str(req.timestamp),
        # display_name is empty only for unknown request kinds; the kind label
        # ("request") keeps those rows from rendering a blank headline.
        title=card.display_name or card.kind_label,
        body=_request_rationale(req),
        workspace_agent_id=card.workspace_agent_id,
        workspace_name=card.ws_name,
        workspace_accent=card.accent,
        service_name=_request_service_name(req, handler),
    )


def _handle_inbox_list() -> Response:
    if not _is_inbox_request_authenticated():
        return _json_error("Not authenticated", status_code=401)
    state = get_state()
    backend_resolver = state.backend_resolver
    handlers: tuple[RequestEventHandler, ...] = state.request_event_handlers
    pending = displayable_pending_requests(state.request_inbox, backend_resolver)
    primary_agent_id_by_ws_name = primary_agent_ids_by_workspace_name(backend_resolver)
    cards = tuple(
        _build_inbox_card(req, find_handler_for_event(handlers, req), backend_resolver, primary_agent_id_by_ws_name)
        for req in pending
    )
    return _json_response(UiInboxListResponse(cards=cards))


def _handle_inbox_detail(request_id: str) -> Response:
    if not _is_inbox_request_authenticated():
        return _json_error("Not authenticated", status_code=401)
    state = get_state()
    inbox: RequestInbox | None = state.request_inbox
    if inbox is None:
        return _json_response(UiInboxDetailResponse(detail=UiInboxUnavailableDetail(message="")))
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return _json_response(
            UiInboxDetailResponse(
                detail=UiInboxUnavailableDetail(message="It may have expired, or it was opened from an old link.")
            )
        )
    if inbox.is_request_resolved(request_id):
        return _json_response(
            UiInboxDetailResponse(detail=UiInboxUnavailableDetail(message="It has already been processed."))
        )
    handlers: tuple[RequestEventHandler, ...] = state.request_event_handlers
    handler = find_handler_for_event(handlers, req_event)
    if handler is None:
        return _json_error(f"No handler registered for request type {req_event.request_type!r}", status_code=500)
    payload = handler.build_request_detail_payload(req_event=req_event, backend_resolver=state.backend_resolver)
    return _json_response(UiInboxDetailResponse(detail=payload))


def register_inbox_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/inbox", view_func=_handle_inbox_list, methods=["GET"])
    blueprint.add_url_rule("/api/inbox/<request_id>/detail", view_func=_handle_inbox_detail, methods=["GET"])
