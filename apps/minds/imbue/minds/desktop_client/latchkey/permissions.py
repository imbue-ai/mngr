"""Latchkey-specific permission grant/deny flow.

This module owns everything that happens between a
``LatchkeyPermissionRequestEvent`` arriving on the inbox and the user's
decision being applied: rendering the dialog HTML, parsing the form
submission, probing credential status, running ``latchkey auth browser``
when needed, rewriting the per-agent ``permissions.json``, appending the
response event, and notifying the waiting agent via ``mngr message``.

The route layer in ``app.py`` is intentionally thin: it authenticates,
looks up the request event by id, and dispatches by request type. All
the latchkey-specific work lives here.
"""

import asyncio
import html as html_module
import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.core import CredentialStatus
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.services_catalog import IMPLICIT_DEFAULT_PERMISSIONS
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import get_service_info
from imbue.minds.desktop_client.latchkey.store import LatchkeyStoreError
from imbue.minds.desktop_client.latchkey.store import PermissionsConfig
from imbue.minds.desktop_client.latchkey.store import granted_permissions_for_service
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.latchkey.store import set_permissions_for_service
from imbue.minds.desktop_client.request_events import LatchkeyPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.templates import render_permission_dialog
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


class LatchkeyPermissionFlowError(Exception):
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


def _json_error(message: str, status_code: int) -> Response:
    return Response(
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


def _render_unknown_service_page(request_id: str, service_name: str) -> Response:
    """Render a deny-only page when the service isn't in the catalog.

    No catalog entry means we have no permissions to offer the user; the
    only sensible action is to send the request straight to deny.
    """
    body = (
        "<!DOCTYPE html><html><body><h1>Unknown service</h1>"
        f"<p>The agent requested permission for <code>{html_module.escape(service_name)}</code>, "
        "but this service is not in the minds permission catalog. The request can only be denied "
        "from here.</p>"
        f'<form method="POST" action="/requests/{html_module.escape(request_id, quote=True)}/permission/deny">'
        '<button type="submit">Deny</button></form>'
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=200)


class LatchkeyPermissionGrantHandler(MutableModel):
    """Top-level orchestrator for ``LatchkeyPermissionRequestEvent`` handling.

    Owns the latchkey services catalog and exposes both pure-logic methods
    (``grant`` / ``deny``, easy to unit-test) and HTTP-aware entry points
    (``render_request_page`` / ``apply_grant_request`` / ``apply_deny_request``)
    that the route dispatcher in ``app.py`` calls into.

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
    services_catalog: Mapping[str, ServicePermissionInfo] = Field(
        description=(
            "Catalog mapping latchkey service names to detent permission info. "
            "Empty if loading failed at startup."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(description="Sends mngr message to the waiting agent.")

    # -- Pure logic (unit-testable) ------------------------------------------

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> tuple[bool, str, RequestResponseEvent]:
        """Apply a grant.

        Returns ``(was_granted, message, response_event)`` where
        ``response_event`` is the freshly-appended on-disk record. The
        HTTP layer mirrors the same event into the in-memory inbox so it
        doesn't have to reload from disk, and surfaces ``message`` to
        both the agent (via ``mngr message``) and the dialog UI.
        """
        if not granted_permissions:
            raise LatchkeyPermissionFlowError(
                "granted_permissions must be non-empty; the dialog must block empty grants",
            )

        # Reject permissions that the user couldn't have legitimately
        # selected from the dialog. This is defence-in-depth against a
        # crafted request.
        invalid = [p for p in granted_permissions if p not in service_info.permission_schemas]
        if invalid:
            raise LatchkeyPermissionFlowError(
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
                response_event = self._write_response_and_notify(
                    request_event_id=request_event_id,
                    agent_id=agent_id,
                    service_info=service_info,
                    status=RequestStatus.DENIED,
                    message=message,
                )
                return False, message, response_event

        # Apply the grant to permissions.json before writing the response
        # event so the agent can never observe a GRANTED response without
        # the corresponding rule being in effect.
        self._apply_grant_to_permissions_file(
            agent_id=agent_id,
            scope_schemas=service_info.scope_schemas,
            granted_permissions=granted_permissions,
        )

        granted_message = _format_granted_message(service_info.display_name, granted_permissions)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            service_info=service_info,
            status=RequestStatus.GRANTED,
            message=granted_message,
        )
        return True, granted_message, response_event

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
    ) -> tuple[str, RequestResponseEvent]:
        """Append a DENIED response and notify the agent. Returns ``(message, response_event)``."""
        message = _format_denied_message(service_info.display_name)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            service_info=service_info,
            status=RequestStatus.DENIED,
            message=message,
        )
        return message, response_event

    # -- HTTP-aware entry points ---------------------------------------------

    def render_request_page(
        self,
        req_event: LatchkeyPermissionRequestEvent,
        backend_resolver: BackendResolverInterface,
    ) -> Response:
        """Render the dialog HTML for a latchkey permission request.

        Falls back to a deny-only page when the requested service is not
        in the catalog, since there are no permissions to offer.
        """
        service_info = get_service_info(self.services_catalog, req_event.service_name)
        if service_info is None:
            return _render_unknown_service_page(
                request_id=str(req_event.event_id),
                service_name=req_event.service_name,
            )

        parsed_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_id, fallback=req_event.agent_id)
        pre_checked = self._initial_checked_permissions(parsed_id, service_info)

        rendered = render_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            service=service_info,
            checked_permissions=pre_checked,
        )
        return HTMLResponse(content=rendered)

    async def apply_grant_request(
        self,
        request: Request,
        req_event: LatchkeyPermissionRequestEvent,
    ) -> Response:
        """Drive the grant flow from the dialog form submission."""
        service_info = get_service_info(self.services_catalog, req_event.service_name)
        if service_info is None:
            return _json_error(
                f"Service '{req_event.service_name}' is not in the catalog",
                status_code=400,
            )

        form = await request.form()
        granted_permissions = tuple(str(v) for v in form.getlist("permissions"))
        if not granted_permissions:
            return _json_error(
                "At least one permission must be selected to approve the request.",
                status_code=400,
            )

        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        try:
            was_granted, message, response_event = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.grant(
                    request_event_id=request_event_id,
                    agent_id=parsed_agent_id,
                    service_info=service_info,
                    granted_permissions=granted_permissions,
                ),
            )
        except LatchkeyPermissionFlowError as e:
            return _json_error(str(e), status_code=400)

        # The grant call has already appended the response event to
        # ~/.minds/events/requests/events.jsonl; mirror the same event
        # into the in-memory inbox so the requests panel reflects the
        # resolution without needing a desktop-client restart.
        self._mirror_response_into_inbox(request, response_event)

        return Response(
            content=json.dumps(
                {
                    "outcome": "GRANTED" if was_granted else "DENIED",
                    "message": message,
                }
            ),
            media_type="application/json",
        )

    async def apply_deny_request(
        self,
        request: Request,
        req_event: LatchkeyPermissionRequestEvent,
    ) -> Response:
        """Drive the deny flow from the dialog form submission."""
        service_info = get_service_info(self.services_catalog, req_event.service_name)
        if service_info is None:
            return _json_error(
                f"Service '{req_event.service_name}' is not in the catalog",
                status_code=400,
            )

        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        _, response_event = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.deny(
                request_event_id=request_event_id,
                agent_id=parsed_agent_id,
                service_info=service_info,
            ),
        )
        self._mirror_response_into_inbox(request, response_event)
        return Response(
            content=json.dumps({"outcome": "DENIED"}),
            media_type="application/json",
        )

    def display_name_for_event(self, req_event: LatchkeyPermissionRequestEvent) -> str:
        """Friendly service name for the requests-panel card. Falls back to the raw service name."""
        info = get_service_info(self.services_catalog, req_event.service_name)
        return info.display_name if info is not None else req_event.service_name

    # -- Internals -----------------------------------------------------------

    def _initial_checked_permissions(
        self,
        agent_id: AgentId,
        service_info: ServicePermissionInfo,
    ) -> tuple[str, ...]:
        """Pick the initial checkbox state for the dialog.

        If any permissions are already granted for this service, those
        are used so the dialog doubles as a revoke UI; otherwise the
        implicit catch-all default (``any``) is pre-checked.
        """
        path = permissions_path_for_agent(self.data_dir, agent_id)
        try:
            config = load_permissions(path)
        except LatchkeyStoreError as e:
            logger.warning(
                "Could not load permissions for {}; using implicit defaults: {}",
                agent_id,
                e,
            )
            return IMPLICIT_DEFAULT_PERMISSIONS

        granted_by_scope = granted_permissions_for_service(config, service_info.scope_schemas)
        granted: set[str] = set()
        for permissions in granted_by_scope.values():
            granted.update(permissions)
        granted_in_catalog = tuple(p for p in service_info.permission_schemas if p in granted)
        if granted_in_catalog:
            return granted_in_catalog
        return IMPLICIT_DEFAULT_PERMISSIONS

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
    ) -> RequestResponseEvent:
        """Persist the response event to disk and send the agent a notification.

        Returns the newly-created event so callers can mirror it into the
        in-memory inbox without re-creating it (and getting a fresh event_id).
        """
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            service_name=service_info.name,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
        return response_event

    def _mirror_response_into_inbox(
        self,
        request: Request,
        response_event: RequestResponseEvent,
    ) -> None:
        """Mirror the on-disk response event into the in-memory inbox.

        The on-disk event-sourcing log is the source of truth; this update
        is just so the requests panel doesn't show the resolved request as
        still pending until the next desktop-client restart.
        """
        inbox: RequestInbox | None = request.app.state.request_inbox
        if inbox is None:
            return
        request.app.state.request_inbox = inbox.add_response(response_event)



