"""Accounts permission grant/deny flow (wire ``request_type == "accounts"``).

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
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.latchkey.gateway_client import AccountsRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_ACCOUNTS
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiAccountsPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.mngr.primitives import AgentId

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "account access"


def _format_granted_message() -> str:
    return "Your request to list this device's signed-in accounts was granted."


def _format_denied_message() -> str:
    return "Your request to list this device's signed-in accounts was denied."


class AccountsPermissionGrantHandler(RequestEventHandler):
    """Handler for accounts permission requests.

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
        return REQUEST_TYPE_ACCOUNTS

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        if not isinstance(permission_request.payload, AccountsRequestPayload):
            return ""
        return "Account access"

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        if not isinstance(permission_request.payload, AccountsRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        parsed_agent_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(
            backend_resolver, parsed_agent_id, fallback=permission_request.agent_id
        )
        return UiAccountsPermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            rationale=permission_request.rationale,
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        if not isinstance(permission_request.payload, AccountsRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)

        # All-or-nothing grant: no override body, so the gateway applies the
        # precomputed fixed effect (the ``minds-accounts-read`` permission)
        # verbatim.
        try:
            self.gateway_client.approve_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not approve accounts request {} via gateway: {}", request_event_id, e)
            return make_json_error_response(
                f"Could not approve the accounts request through the latchkey gateway: {e}",
                status_code=502,
            )

        message = _format_granted_message()
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
        if not isinstance(permission_request.payload, AccountsRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
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
