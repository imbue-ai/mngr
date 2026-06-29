"""Accounts permission grant/deny flow (``RequestType.ACCOUNTS_PERMISSION``).

This module is a sibling handler under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the flow for
*accounts* permission requests: an agent asking the user to let it list the
device's signed-in accounts (``GET /api/v1/accounts``) so it can discover an
account id/email to associate a workspace with.

Like the :mod:`.file_sharing` sibling -- and unlike :mod:`.workspace` -- the
grant is all-or-nothing with no parameters: there are no verb checkboxes, no
target, and nothing to edit before approving. Approval calls
``POST /permission-requests/approve/<id>`` on the gateway's
``permission-requests`` extension with no override body, so the gateway splices
the precomputed effect (a single fixed ``minds-accounts-read`` permission under
the pre-existing ``latchkey-self`` scope) into the requesting agent's per-host
``latchkey_permissions.json``. Denial drops the pending record via
``DELETE /permission-requests/<id>``.
"""

import json
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
from imbue.minds.desktop_client.latchkey.handlers.templates import render_accounts_permission_dialog
from imbue.minds.desktop_client.request_events import LatchkeyAccountsPermissionRequestEvent
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

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "account access"


def _format_granted_message() -> str:
    return "Your request to list this device's signed-in accounts was granted."


def _format_denied_message() -> str:
    return "Your request to list this device's signed-in accounts was denied."


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


class AccountsPermissionGrantHandler(RequestEventHandler):
    """Per-``RequestType.ACCOUNTS_PERMISSION`` handler.

    Thin, like the file-sharing sibling: it renders the yes/no dialog, asks the
    gateway to approve (no override -- the effect is fixed) or delete the pending
    request via :class:`LatchkeyGatewayClient`, writes the response event, and
    notifies the waiting agent via ``mngr message``.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ``~/.minds``).")
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to call ``POST /permission-requests/approve/<id>`` and "
            "``DELETE /permission-requests/<id>`` on the gateway's bundled "
            "``permission-requests`` extension."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Sends ``mngr message`` nudges to the waiting agent on resolution.",
    )

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return str(RequestType.ACCOUNTS_PERMISSION)

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        if not isinstance(req_event, LatchkeyAccountsPermissionRequestEvent):
            return ""
        return "Account access"

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        if not isinstance(req_event, LatchkeyAccountsPermissionRequestEvent):
            return "<p>Unsupported request type</p>"
        parsed_agent_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_agent_id, fallback=req_event.agent_id)
        return render_accounts_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            mngr_forward_origin=mngr_forward_origin,
        )

    def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyAccountsPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)

        # All-or-nothing grant: no override body, so the gateway applies the
        # precomputed fixed effect (the ``minds-accounts-read`` permission)
        # verbatim.
        try:
            self.gateway_client.approve_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not approve accounts request {} via gateway: {}", request_event_id, e)
            return _json_error(
                f"Could not approve the accounts request through the latchkey gateway: {e}",
                status_code=502,
            )

        message = _format_granted_message()
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
        if not isinstance(req_event, LatchkeyAccountsPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        # DELETE tolerates 404 -- if the request is already gone we still want to
        # write the response event and notify the agent.
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE accounts request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )

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
        """Persist the response event and ping the agent. Returns the new event."""
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.ACCOUNTS_PERMISSION),
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
