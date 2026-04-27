"""Generic permission grant/deny flow used by the requests inbox.

This module is meant to grow into a broader subsystem; for now it has a
single backend (``Latchkey`` + per-agent ``permissions.json``), but the
naming and layout are kept source-agnostic so additional backends can
slot in later without renaming everything.

The flow (from the user clicking Approve in the dialog):

1. Probe credential status via ``Latchkey.services_info``.
2. If credentials are not ``VALID``, run ``Latchkey.auth_browser``. A
   failed/cancelled browser flow is treated as a deny with an explanatory
   message -- there is no separate ``AUTH_FAILED`` status.
3. Atomically update the per-agent ``permissions.json``.
4. Append a response event to ``~/.minds/events/requests/events.jsonl``.
5. Notify the waiting agent via ``mngr message <agent-id>``.

Steps 4 and 5 are also what ``deny`` does, with no permissions or auth
work ahead of them.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.latchkey.core import CredentialStatus
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.store import LatchkeyStoreError
from imbue.minds.desktop_client.latchkey.store import PermissionsConfig
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.latchkey.store import set_permissions_for_service
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


class PermissionFlowError(Exception):
    """Raised for caller-facing programming errors (empty grants, unknown permissions)."""


class MngrMessageSender(MutableModel):
    """Wrapper around ``mngr message <agent-id> <text>``.

    Failures are logged at warning level but never raised: the response
    event has already been written, so an undelivered nudge is recoverable
    (the agent will eventually wake up on its own).
    """

    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to mngr binary.")

    def send(self, agent_id: AgentId, text: str) -> None:
        cg = ConcurrencyGroup(name="mngr-message")
        with cg:
            result = cg.run_process_to_completion(
                command=[self.mngr_binary, "message", str(agent_id), text],
                timeout=_MNGR_MESSAGE_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
        if result.returncode != 0:
            logger.warning(
                "mngr message to agent {} exited {}: {}",
                agent_id,
                result.returncode,
                result.stderr.strip(),
            )


def _format_granted_message(service_display_name: str, granted: Sequence[str]) -> str:
    permissions = ", ".join(granted)
    return (
        f"Your permission request for {service_display_name} was granted with the following "
        f"permissions: {permissions}. Please retry the call that was blocked."
    )


def _format_denied_message(service_display_name: str) -> str:
    return f"Your permission request for {service_display_name} was denied. Do not retry the blocked call."


def _format_auth_failed_message(service_display_name: str, detail: str) -> str:
    suffix = f" Reason: {detail}" if detail else ""
    return (
        f"Your permission request for {service_display_name} could not be completed because the user's "
        f"sign-in flow did not finish.{suffix} Do not retry yet; report this to the user."
    )


class PermissionGrantHandler(MutableModel):
    """Top-level orchestrator for grant / deny actions.

    Hold-time invariants when ``grant`` returns ``(True, message)``:

    * ``permissions.json`` reflects the new rule.
    * A ``GRANTED`` response event has been appended for ``request_event_id``.
    * ``mngr message`` has been attempted (failures logged).

    When ``grant`` returns ``(False, message)`` (failed sign-in):

    * ``permissions.json`` is unchanged.
    * A ``DENIED`` response event has been appended (the agent is told the
      reason via the message, not via a distinct status).
    * ``mngr message`` has been attempted.

    ``deny`` writes a ``DENIED`` response and notifies; nothing else.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ~/.minds).")
    latchkey: Latchkey = Field(description="Latchkey wrapper used to probe credentials and run sign-in flows.")
    mngr_message_sender: MngrMessageSender = Field(description="Sends mngr message to the waiting agent.")

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> tuple[bool, str]:
        """Apply a grant. Returns ``(was_granted, message_sent_to_agent)``."""
        if not granted_permissions:
            raise PermissionFlowError(
                "granted_permissions must be non-empty; the dialog must block empty grants",
            )

        # Reject permissions that the user couldn't have legitimately
        # selected from the dialog. This is defence-in-depth against a
        # crafted request.
        invalid = [p for p in granted_permissions if p not in service_info.permission_schemas]
        if invalid:
            raise PermissionFlowError(
                f"Granted permissions not in catalog for service '{service_info.name}': {invalid}",
            )

        status = self.latchkey.services_info(service_info.name)
        if status != CredentialStatus.VALID:
            logger.info(
                "Credentials for {} reported as {}; running latchkey auth browser",
                service_info.name,
                status,
            )
            is_success, detail = self.latchkey.auth_browser(service_info.name)
            if not is_success:
                # No separate AUTH_FAILED status: a failed sign-in is
                # surfaced as DENIED with a distinct message so the agent
                # can tell the user something went wrong.
                message = _format_auth_failed_message(service_info.display_name, detail)
                self._write_response_and_notify(
                    request_event_id=request_event_id,
                    agent_id=agent_id,
                    service_info=service_info,
                    status=RequestStatus.DENIED,
                    message=message,
                )
                return False, message

        # Apply the grant to permissions.json before writing the response
        # event so the agent can never observe a GRANTED response without
        # the corresponding rule being in effect.
        self._apply_grant_to_permissions_file(
            agent_id=agent_id,
            scope_schemas=service_info.scope_schemas,
            granted_permissions=granted_permissions,
        )

        granted_message = _format_granted_message(service_info.display_name, granted_permissions)
        self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            service_info=service_info,
            status=RequestStatus.GRANTED,
            message=granted_message,
        )
        return True, granted_message

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
    ) -> str:
        """Append a DENIED response and notify the agent. Returns the message sent."""
        message = _format_denied_message(service_info.display_name)
        self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            service_info=service_info,
            status=RequestStatus.DENIED,
            message=message,
        )
        return message

    def _apply_grant_to_permissions_file(
        self,
        agent_id: AgentId,
        scope_schemas: Sequence[str],
        granted_permissions: Sequence[str],
    ) -> None:
        path = permissions_path_for_agent(self.data_dir, agent_id)
        try:
            existing = load_permissions(path)
        except LatchkeyStoreError as e:
            logger.warning(
                "Existing permissions.json at {} is unreadable; replacing it: {}",
                path,
                e,
            )
            existing = PermissionsConfig()

        updated = set_permissions_for_service(
            existing,
            scope_schemas=scope_schemas,
            granted_permissions=granted_permissions,
        )
        save_permissions(path, updated)

    def _write_response_and_notify(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        status: RequestStatus,
        message: str,
    ) -> None:
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            service_name=service_info.name,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
