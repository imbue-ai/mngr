"""Remote-execution permission grant/deny flow (wire ``request_type == "remote-execution"``).

A sibling handler under :mod:`imbue.minds.desktop_client.latchkey.handlers`
for the most dangerous grant a workspace can ask for: letting every agent of
the asking workspace run commands on this computer, as the user, through the
desktop's ``POST /api/v1/remote-execution/<device id>`` route. A request names
either any command or one program (``payload.program``: a bare name or a
``~``-rooted or absolute path), and the dialog shows what that program resolves
to on this computer right now, so the user approves a file they can see rather
than a name.

Like :mod:`.accounts` the grant takes no approve override: approval calls
``POST /permission-requests/approve/<id>`` with no body and the gateway splices
the precomputed effect (one ``remote-execution-<device id>[:<program>]``
permission under ``latchkey-self``, pinned to this desktop's route and, for a
program, to the route's ``program`` query parameter) into the requesting agent's per-host
policy. Unlike every other handler, approval is
also gated on this device's master switch (Settings > This computer): while
the switch is off the detail payload says so, the dialog offers no Approve,
and a grant posted anyway is refused with 409, so a stale page or a scripted
post can never grant what the switch has not allowed.
"""

import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Final

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field

from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_REMOTE_EXECUTION
from imbue.minds.desktop_client.latchkey.gateway_client import RemoteExecutionRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.program_resolution import ResolvedProgram
from imbue.minds.desktop_client.program_resolution import resolve_program
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiRemoteExecutionPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import read_share_grants_from_agent
from imbue.minds.desktop_client.sharing_handler import does_grants_document_admit_anyone
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_inbox import workspace_agent_id_for_agent
from imbue.mngr.primitives import AgentId

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "run commands"

# The error code a refused approval carries, which the dialog and the agent-facing
# route both use for "the switch is off".
REMOTE_EXECUTION_DISABLED_CODE: Final[str] = "remote_execution_disabled"

# The dialog waits this long for a published workspace's grants document before
# it gives up on the shared-workspace warning.
_GRANTS_READ_TIMEOUT_SECONDS: Final[float] = 10.0


def is_agent_command_execution_allowed() -> bool:
    """Whether this device's master switch is on; an app with no settings storage reads as off."""
    minds_config = get_state().minds_config
    return minds_config is not None and minds_config.get_allow_agent_command_execution()


def _is_workspace_shared(agent_id: AgentId, backend_resolver: BackendResolverInterface) -> bool:
    """Whether the asking workspace is published and admits someone besides its owner.

    Publishing alone is not sharing: the create form publishes every workspace
    by default, and a published workspace with an empty grants document admits
    nobody. The grants document lives in the workspace, so a published
    workspace is asked for it (a bounded, best-effort exec; a workspace that is
    stopped or does not answer in time reads as not shared, since the warning
    is advisory).
    """
    state = get_state()
    forward_identity = state.forward_identity
    if forward_identity is None:
        return False
    workspace_agent_id = workspace_agent_id_for_agent(str(agent_id), backend_resolver)
    if not forward_identity.is_marked_shared(workspace_agent_id):
        return False
    mngr_caller = state.mngr_caller
    if mngr_caller is None:
        return False
    agent_address = build_agent_address(AgentId(workspace_agent_id), backend_resolver)
    try:
        grants_toml_text = read_share_grants_from_agent(agent_address, mngr_caller, _GRANTS_READ_TIMEOUT_SECONDS)
    except ShareInjectionError as e:
        logger.debug("Treating {} as not shared: its grants could not be read: {}", workspace_agent_id, e)
        return False
    return does_grants_document_admit_anyone(grants_toml_text)


def _describe_request(program: str | None) -> str:
    """What the request is for, as the notices and the inbox title say it."""
    return "run commands" if program is None else f"run {program}"


def _resolve_program_here(program: str | None) -> ResolvedProgram | None:
    """Where a requested program resolves on this computer now, through the user's login-shell PATH when known."""
    if program is None:
        return None
    login_shell_path = get_state().login_shell_path
    path_env = login_shell_path.read() if login_shell_path is not None else os.environ.get("PATH")
    return resolve_program(program, Path.home(), path_env)


def _format_granted_message(hostname: str, program: str | None) -> str:
    return (
        f"Your request to {_describe_request(program)} on {hostname} was granted. Use it sparingly: the user can "
        "revoke it at any time, and every command you run there is recorded."
    )


def _format_denied_message(program: str | None) -> str:
    return (
        f"Your request to {_describe_request(program)} on the user's computer was denied. Do not ask again unless "
        "the user raises it."
    )


class RemoteExecutionGrantHandler(RequestEventHandler):
    """Handler for remote-execution permission requests.

    Renders the warning dialog's detail, approves through the gateway's
    ``POST /permission-requests/approve/<id>`` (no override: the effect is
    fixed) once this device's master switch allows it, pushes the policy to
    the workspace's machine, writes the response event, and nudges the
    request's chat with the verdict. Denial drops the pending record.
    """

    data_dir: Path = Field(frozen=True, description="Imbue Studio data directory (typically ``~/.minds``).")
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to call ``POST /permission-requests/approve/<id>`` and "
            "``DELETE /permission-requests/<id>`` on the gateway's bundled ``permission-requests`` extension."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Nudges the request's chat with the verdict on resolution (see :mod:`.messaging`).",
    )
    push_permissions_to_machine: Callable[[str], None] = Field(
        description=(
            "Pushes the freshly-spliced policy to the workspace's own machine, blocking until it lands "
            "there, and raising MachineOperationError when it does not. A no-op for a workspace whose "
            "agents run on this computer."
        ),
    )

    def handles_request_type(self) -> str:
        return REQUEST_TYPE_REMOTE_EXECUTION

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        if not isinstance(permission_request.payload, RemoteExecutionRequestPayload):
            return ""
        described = _describe_request(permission_request.payload.program)
        return f"{described[0].upper()}{described[1:]} on this computer"

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        if not isinstance(permission_request.payload, RemoteExecutionRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        parsed_agent_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(
            backend_resolver, parsed_agent_id, fallback=permission_request.agent_id
        )
        program = permission_request.payload.program
        resolved = _resolve_program_here(program)
        return UiRemoteExecutionPermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            rationale=permission_request.rationale,
            device_hostname=socket.gethostname(),
            program=program,
            resolved_program_path=str(resolved.path) if resolved is not None and resolved.path is not None else None,
            is_program_interpreter=resolved is not None and resolved.is_interpreter,
            is_setting_enabled=is_agent_command_execution_allowed(),
            is_workspace_shared=_is_workspace_shared(parsed_agent_id, backend_resolver),
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        if not isinstance(permission_request.payload, RemoteExecutionRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        program = permission_request.payload.program

        # The master switch is checked here as well as in the dialog: the dialog
        # offers no Approve while it is off, so a post that arrives anyway is a
        # stale page or a script, and the request stays pending.
        if not is_agent_command_execution_allowed():
            return make_json_error_response(
                "Agents are not allowed to run commands on this computer. Turn on 'Allow agents to run commands "
                "on this computer' under Settings > This computer first.",
                status_code=409,
                code=REMOTE_EXECUTION_DISABLED_CODE,
            )

        # No override body: the gateway applies the precomputed fixed effect
        # (one device-pinned permission, program-pinned when asked) verbatim.
        try:
            self.gateway_client.approve_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not approve remote-execution request {} via gateway: {}", request_event_id, e)
            return make_json_error_response(
                f"Could not approve the request through the latchkey gateway: {e}",
                status_code=502,
            )
        # A remote workspace's machine enforces its own copy of the policy, so
        # the edit is pushed there before the grant is called done; a machine
        # that will not take it leaves the request unresolved here.
        try:
            self.push_permissions_to_machine(str(parsed_agent_id))
        except MachineOperationError as e:
            logger.warning("Could not apply the remote-execution grant on the machine of {}: {}", parsed_agent_id, e)
            return make_json_error_response(str(e), status_code=502)

        logger.info("Allowed the agents of {} to {} on this computer", parsed_agent_id, _describe_request(program))
        message = _format_granted_message(socket.gethostname(), program)
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
        if not isinstance(permission_request.payload, RemoteExecutionRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        program = permission_request.payload.program
        # DELETE tolerates 404: a request already gone still gets its response
        # event written and its agent told.
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE remote-execution request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )

        message = _format_denied_message(program)
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
