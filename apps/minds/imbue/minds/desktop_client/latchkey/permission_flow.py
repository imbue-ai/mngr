"""Glue between the desktop permission dialog and latchkey/permissions.json.

This module owns the full grant/deny flow:

1. ``LatchkeyServicesInfoProbe`` shells out to ``latchkey services info <svc>``
   and reads ``credentialStatus`` to decide whether a browser auth flow
   is needed before applying a grant.
2. ``LatchkeyAuthBrowserRunner`` launches ``latchkey auth browser <svc>``
   when credentials are missing/invalid and reports success or failure.
3. ``MngrMessageSender`` sends the resulting plain-English message back
   to the agent via ``mngr message <agent-id>``.
4. ``PermissionGrantHandler`` ties them together, atomically updating the
   per-agent ``permissions.json`` and writing the request response event.
"""

import json
import os
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.latchkey.gateway import LATCHKEY_BINARY
from imbue.minds.desktop_client.latchkey.permissions_store import LatchkeyPermissionsStoreError
from imbue.minds.desktop_client.latchkey.permissions_store import PermissionsConfig
from imbue.minds.desktop_client.latchkey.permissions_store import load_permissions
from imbue.minds.desktop_client.latchkey.permissions_store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.permissions_store import save_permissions
from imbue.minds.desktop_client.latchkey.permissions_store import set_permissions_for_service
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.mngr.primitives import AgentId

# Generous timeouts: the browser auth flow can wait on a real human to
# log in; services-info is normally instant but can stall on slow keychains.
_SERVICES_INFO_TIMEOUT_SECONDS: Final[float] = 15.0
_AUTH_BROWSER_TIMEOUT_SECONDS: Final[float] = 600.0
_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


class PermissionFlowError(Exception):
    """Base exception for permission-flow failures."""


class CredentialStatus(UpperCaseStrEnum):
    """Latchkey-reported credential state for a service.

    Mirrors detent's ``ApiCredentialStatus`` enum (``missing``, ``valid``,
    ``invalid``, ``unknown``) but normalized to the project's enum convention.
    """

    MISSING = auto()
    VALID = auto()
    INVALID = auto()
    UNKNOWN = auto()


_CREDENTIAL_STATUS_BY_LATCHKEY_VALUE: Final[dict[str, CredentialStatus]] = {
    "missing": CredentialStatus.MISSING,
    "valid": CredentialStatus.VALID,
    "invalid": CredentialStatus.INVALID,
    "unknown": CredentialStatus.UNKNOWN,
}


class GrantOutcome(UpperCaseStrEnum):
    """High-level outcome of a grant attempt."""

    GRANTED = auto()
    AUTH_FAILED = auto()


class GrantResult(FrozenModel):
    """Result of a ``PermissionGrantHandler.grant`` call.

    ``message`` is the plain-English text sent to the agent via
    ``mngr message`` and is also useful for surfacing errors in the UI.
    """

    outcome: GrantOutcome = Field(description="Whether the grant succeeded or stopped at auth.")
    message: str = Field(description="Plain-English message sent to the agent.")


def _build_env_with_latchkey_directory(latchkey_directory: Path | None) -> dict[str, str] | None:
    """Build an env override that pins ``LATCHKEY_DIRECTORY`` for the child.

    When ``latchkey_directory`` is ``None``, returns ``None`` so the child
    inherits the parent environment unchanged.
    """
    if latchkey_directory is None:
        return None
    env = dict(os.environ)
    env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    return env


class LatchkeyServicesInfoProbe(MutableModel):
    """Wrapper around ``latchkey services info <service>``.

    The result is parsed as JSON (latchkey emits pretty JSON to stdout for
    this command) and the ``credentialStatus`` field is normalized to
    ``CredentialStatus``. Any failure (process error, malformed output,
    unrecognized status string) yields ``CredentialStatus.UNKNOWN`` so the
    caller falls back to launching the browser flow.
    """

    latchkey_binary: str = Field(default=LATCHKEY_BINARY, frozen=True, description="Path to latchkey binary.")
    latchkey_directory: Path | None = Field(
        default=None,
        frozen=True,
        description="Optional override for LATCHKEY_DIRECTORY in the child environment.",
    )

    def probe(self, service_name: str) -> CredentialStatus:
        env = _build_env_with_latchkey_directory(self.latchkey_directory)
        cg = ConcurrencyGroup(name="latchkey-services-info")
        with cg:
            result = cg.run_process_to_completion(
                command=[self.latchkey_binary, "services", "info", service_name],
                timeout=_SERVICES_INFO_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=env,
            )
        if result.returncode != 0:
            logger.warning(
                "latchkey services info {} exited {}: {}",
                service_name,
                result.returncode,
                result.stderr.strip(),
            )
            return CredentialStatus.UNKNOWN

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse 'latchkey services info {}' output as JSON: {}", service_name, e)
            return CredentialStatus.UNKNOWN

        if not isinstance(payload, dict):
            logger.warning("'latchkey services info {}' returned non-object JSON", service_name)
            return CredentialStatus.UNKNOWN

        raw_status = payload.get("credentialStatus")
        if not isinstance(raw_status, str):
            logger.warning(
                "'latchkey services info {}' did not include a credentialStatus string",
                service_name,
            )
            return CredentialStatus.UNKNOWN

        status = _CREDENTIAL_STATUS_BY_LATCHKEY_VALUE.get(raw_status)
        if status is None:
            logger.warning(
                "Unrecognized credentialStatus {!r} from 'latchkey services info {}'",
                raw_status,
                service_name,
            )
            return CredentialStatus.UNKNOWN
        return status


class LatchkeyAuthBrowserRunner(MutableModel):
    """Wrapper around ``latchkey auth browser <service>``.

    Returns ``True`` when latchkey reports success (exit code 0). Any
    non-zero exit -- whether from a cancelled browser flow, network
    failure, or something else -- is logged and reported as a failure
    so the caller can surface ``AUTH_FAILED`` to the agent.
    """

    latchkey_binary: str = Field(default=LATCHKEY_BINARY, frozen=True, description="Path to latchkey binary.")
    latchkey_directory: Path | None = Field(
        default=None,
        frozen=True,
        description="Optional override for LATCHKEY_DIRECTORY in the child environment.",
    )

    def run(self, service_name: str) -> tuple[bool, str]:
        """Return ``(is_success, stderr_or_message)``."""
        env = _build_env_with_latchkey_directory(self.latchkey_directory)
        cg = ConcurrencyGroup(name="latchkey-auth-browser")
        with cg:
            result = cg.run_process_to_completion(
                command=[self.latchkey_binary, "auth", "browser", service_name],
                timeout=_AUTH_BROWSER_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=env,
            )
        if result.returncode == 0:
            logger.info("latchkey auth browser {} succeeded", service_name)
            return True, ""
        message = result.stderr.strip() or result.stdout.strip() or "latchkey auth browser failed"
        logger.warning(
            "latchkey auth browser {} exited {}: {}",
            service_name,
            result.returncode,
            message,
        )
        return False, message


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

    Hold-time invariants when ``grant`` returns:

    * On ``GRANTED``: ``permissions.json`` reflects the new rule and a
      ``GRANTED`` response event has been appended for ``request_event_id``.
    * On ``AUTH_FAILED``: ``permissions.json`` is unchanged and an
      ``AUTH_FAILED`` response event has been appended.
    * In both cases, ``mngr message`` has been attempted (failures logged).
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ~/.minds).")
    services_info_probe: LatchkeyServicesInfoProbe = Field(description="Probes latchkey credential status.")
    auth_browser_runner: LatchkeyAuthBrowserRunner = Field(description="Runs the browser auth flow.")
    mngr_message_sender: MngrMessageSender = Field(description="Sends mngr message to the waiting agent.")

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> GrantResult:
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

        status = self.services_info_probe.probe(service_info.name)
        if status != CredentialStatus.VALID:
            logger.info(
                "Credentials for {} reported as {}; running latchkey auth browser",
                service_info.name,
                status,
            )
            is_success, detail = self.auth_browser_runner.run(service_info.name)
            if not is_success:
                self._write_response_and_notify(
                    request_event_id=request_event_id,
                    agent_id=agent_id,
                    service_info=service_info,
                    status=RequestStatus.AUTH_FAILED,
                    message=_format_auth_failed_message(service_info.display_name, detail),
                )
                return GrantResult(
                    outcome=GrantOutcome.AUTH_FAILED,
                    message=_format_auth_failed_message(service_info.display_name, detail),
                )

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
        return GrantResult(outcome=GrantOutcome.GRANTED, message=granted_message)

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
    ) -> str:
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
        except LatchkeyPermissionsStoreError as e:
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
