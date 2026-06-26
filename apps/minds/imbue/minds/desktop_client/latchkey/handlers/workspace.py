"""Cross-workspace permission grant/deny flow (``RequestType.WORKSPACE_PERMISSION``).

This module is the third sibling handler under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the flow for
*workspace* permission requests: an agent in one workspace asking to act on the
minds cross-workspace management API (``/api/v1/workspaces/...``) -- listing,
reading, creating, destroying, starting/stopping, exporting backups, and
establishing SSH access against *other* workspaces.

Unlike the :mod:`.predefined` (catalog-backed) and :mod:`.file_sharing`
siblings, a workspace grant is *target-scoped*: the verbs that act on a single
workspace (destroy / lifecycle / backups-export / ssh) are gated by a per-host
``anyOf`` allowlist of target workspace ids that accumulates one approved target
at a time. The dialog lets the user pick which verbs to grant and -- when the
request names a target workspace and the chosen verbs include a targeted one --
whether the grant applies to that one workspace ("selected") or to all
workspaces ("all"). The actual write is done by
:func:`imbue.mngr_latchkey.workspace_permissions.grant_workspace_permissions`,
which unions the verbs into the host's ``minds-workspaces`` rule and accumulates
the approved target into each targeted verb's schema.

The grant is applied directly to the *requesting* agent's per-host
``latchkey_permissions.json`` (every agent on a host shares one), then the
pending request is dropped from the gateway via
``DELETE /permission-requests/<id>`` -- mirroring the predefined handler, which
also applies its grant out-of-band and then deletes the gateway record (the
``workspace`` request type is therefore never applied via the gateway's
``/approve`` endpoint).
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
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.templates import render_workspace_permission_dialog
from imbue.minds.desktop_client.request_events import LatchkeyWorkspacePermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.workspace_permissions import MINDS_WORKSPACES_SCOPE
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS
from imbue.mngr_latchkey.workspace_permissions import grant_workspace_permissions

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "workspace access"

# Form fields. ``permissions`` carries the checked verb names (shared with the
# other dialogs so the inbox shell's Approve gating works). ``target_scope``
# carries the all-vs-selected radio choice.
_TARGET_SCOPE_FIELD: Final[str] = "target_scope"
_TARGET_SCOPE_SELECTED: Final[str] = "selected"
_TARGET_SCOPE_ALL: Final[str] = "all"

_VERB_DISPLAY_BY_PERMISSION: Final[dict[str, str]] = {verb.permission: verb.display_name for verb in WORKSPACE_VERBS}


def _json_error(message: str, status_code: int) -> Response:
    return make_response(
        content=json.dumps({"error": message}),
        media_type="application/json",
        status_code=status_code,
    )


def _resolve_workspace_name(
    backend_resolver: BackendResolverInterface,
    agent_id: AgentId,
    fallback: str,
) -> str:
    ws_name = backend_resolver.get_workspace_name(agent_id) or ""
    if ws_name:
        return ws_name
    info = backend_resolver.get_agent_display_info(agent_id)
    return info.agent_name if info else fallback


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
    return _resolve_workspace_name(backend_resolver, parsed, fallback=target_workspace_id)


def _resolve_host_id(
    backend_resolver: BackendResolverInterface,
    agent_id: AgentId,
) -> HostId | None:
    """Resolve the host an agent runs on, or ``None`` when discovery hasn't caught up.

    Mirrors the predefined handler: workspace permissions are stored per-host, so
    the grant updates the *requesting* agent's host file. Returns ``None`` when
    the host id isn't known yet or when the resolver reports the placeholder
    ``"localhost"`` used by static / in-memory backend resolvers in tests.
    """
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is None:
        return None
    try:
        return HostId(info.host_id)
    except ValueError:
        logger.debug(
            "Backend resolver reported non-HostId host {!r} for agent {}; treating as unknown",
            info.host_id,
            agent_id,
        )
        return None


def _format_granted_message(granted: Sequence[str], target_label: str) -> str:
    verbs = ", ".join(_VERB_DISPLAY_BY_PERMISSION.get(verb, verb) for verb in granted)
    return f"Your cross-workspace permission request was granted ({verbs}) for {target_label}."


def _format_denied_message() -> str:
    return "Your cross-workspace permission request was denied."


class WorkspacePermissionGrantHandler(RequestEventHandler):
    """Per-``RequestType.WORKSPACE_PERMISSION`` handler.

    Renders the verb + all-vs-selected dialog, applies the approved grant to the
    requesting agent's per-host permissions file via
    :func:`grant_workspace_permissions`, drops the pending gateway record, writes
    the response event, and notifies the waiting agent via ``mngr message``.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ``~/.minds``).")
    latchkey: Latchkey = Field(
        description="Latchkey wrapper; used for its ``plugin_data_dir`` (where per-host permissions live).",
    )
    gateway_client: LatchkeyGatewayClient = Field(
        description="HTTP client used to ``DELETE /permission-requests/<id>`` once a request is resolved.",
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Sends ``mngr message`` nudges to the waiting agent on resolution.",
    )

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return str(RequestType.WORKSPACE_PERMISSION)

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        if not isinstance(req_event, LatchkeyWorkspacePermissionRequestEvent):
            return ""
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        target_name = _resolve_target_name(backend_resolver, req_event.target_workspace_id)
        return f"Workspace access: {target_name}" if target_name else "Workspace access"

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        if not isinstance(req_event, LatchkeyWorkspacePermissionRequestEvent):
            return "<p>Unsupported request type</p>"
        parsed_agent_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_agent_id, fallback=req_event.agent_id)
        target_name = _resolve_target_name(backend_resolver, req_event.target_workspace_id)
        requested = set(req_event.permissions)
        # Pre-check the verbs the agent requested (intersected with the known
        # verb catalog); the user may broaden or narrow in the dialog.
        checked = tuple(verb.permission for verb in WORKSPACE_VERBS if verb.permission in requested)
        # Offer the all-vs-selected choice whenever the request names a target
        # workspace; the targeted verbs (if any are granted) honor it, and the
        # non-targeted verbs ignore it.
        return render_workspace_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            verbs=WORKSPACE_VERBS,
            checked_permissions=checked,
            target_workspace_id=req_event.target_workspace_id,
            target_workspace_name=target_name,
            show_target_choice=bool(target_name),
            mngr_forward_origin=mngr_forward_origin,
        )

    def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyWorkspacePermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)

        form = request.form
        granted_permissions = tuple(str(v) for v in form.getlist("permissions"))
        if not granted_permissions:
            return _json_error(
                "At least one permission must be selected to approve the request.",
                status_code=400,
            )

        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        host_id = _resolve_host_id(backend_resolver, parsed_agent_id)
        if host_id is None:
            return _json_error(
                f"Could not resolve host for agent {parsed_agent_id}; cannot apply grant.",
                status_code=503,
            )

        # Resolve the target the targeted verbs apply to. "selected" pins the
        # request's target workspace; "all" (or a missing target) grants
        # broadly.
        target_scope = form.get(_TARGET_SCOPE_FIELD, _TARGET_SCOPE_ALL)
        target_workspace_id: AgentId | None = None
        if target_scope == _TARGET_SCOPE_SELECTED and req_event.target_workspace_id:
            try:
                target_workspace_id = AgentId(req_event.target_workspace_id)
            except ValueError:
                return _json_error(
                    f"Invalid target workspace id: {req_event.target_workspace_id}",
                    status_code=400,
                )

        try:
            grant_workspace_permissions(
                self.latchkey.plugin_data_dir,
                host_id,
                granted_permissions,
                target_workspace_id,
            )
        except LatchkeyStoreError as e:
            logger.warning("Could not apply minds-workspaces grant for agent {}: {}", parsed_agent_id, e)
            return _json_error(f"Could not apply the cross-workspace grant: {e}", status_code=400)

        target_label = (
            _resolve_target_name(backend_resolver, req_event.target_workspace_id) or "the selected workspace"
            if target_workspace_id is not None
            else "all workspaces"
        )
        message = _format_granted_message(granted_permissions, target_label)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.GRANTED,
            message=message,
        )
        self._mirror_response_into_inbox(response_event)
        return make_response(
            content=json.dumps({"outcome": "GRANTED", "message": message}),
            media_type="application/json",
        )

    def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyWorkspacePermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        message = _format_denied_message()
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.DENIED,
            message=message,
        )
        self._mirror_response_into_inbox(response_event)
        return make_response(
            content=json.dumps({"outcome": "DENIED", "message": message}),
            media_type="application/json",
        )

    # -- Internals -----------------------------------------------------------

    def _write_response_and_notify(
        self,
        request_event_id: str,
        agent_id: AgentId,
        status: RequestStatus,
        message: str,
    ) -> RequestResponseEvent:
        """Drop the gateway record, persist the response event, and notify the agent."""
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE workspace permission request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.WORKSPACE_PERMISSION),
            scope=MINDS_WORKSPACES_SCOPE,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
        return response_event

    def _mirror_response_into_inbox(
        self,
        response_event: RequestResponseEvent,
    ) -> None:
        """Mirror the on-disk response event into the in-memory inbox (and wake the SSE)."""
        inbox: RequestInbox | None = get_state().request_inbox
        if inbox is None:
            return
        get_state().request_inbox = inbox.add_response(response_event)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
