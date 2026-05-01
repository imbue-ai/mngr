"""Latchkey-specific permission grant/deny flow.

This module owns everything that happens between a
``LatchkeyPermissionRequestEvent`` arriving on the inbox and the user's
decision being applied: rendering the dialog HTML, parsing the form
submission, probing credential status, running ``latchkey auth browser``
when needed, rewriting the per-agent ``latchkey_permissions.json``, appending the
response event, and notifying the waiting agent via ``mngr message``.

Services that latchkey reports as not supporting browser sign-in fall
back to a manual flow: the grant is refused (the request stays pending),
the user is shown the suggested ``latchkey auth set`` invocation, and a
fresh Approve click re-runs ``latchkey services info`` to check whether
credentials have since become valid.

The route layer in ``app.py`` is intentionally thin: it authenticates,
looks up the request event by id, and dispatches by request type. All
the latchkey-specific work lives here.
"""

import asyncio
import html as html_module
import json
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.core import CredentialStatus
from imbue.minds.desktop_client.latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.services_catalog import IMPLICIT_DEFAULT_PERMISSIONS
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import get_service_info
from imbue.minds.desktop_client.latchkey.store import LatchkeyPermissionsConfig
from imbue.minds.desktop_client.latchkey.store import LatchkeyStoreError
from imbue.minds.desktop_client.latchkey.store import granted_permissions_for_scope
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.latchkey.store import set_permissions_for_scope
from imbue.minds.desktop_client.latchkey.templates import render_latchkey_permission_dialog
from imbue.minds.desktop_client.request_events import LatchkeyPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.mngr.primitives import AgentId

_MNGR_MESSAGE_TIMEOUT_SECONDS: Final[float] = 30.0


class GrantOutcome(UpperCaseStrEnum):
    """Possible outcomes of attempting to apply a permission grant."""

    GRANTED = auto()
    DENIED = auto()
    NEEDS_MANUAL_CREDENTIALS = auto()


class GrantResult(FrozenModel):
    """Outcome of ``LatchkeyPermissionGrantHandler.grant``."""

    outcome: GrantOutcome = Field(description="Which branch the grant flow took.")
    message: str = Field(
        description=(
            "Plain-text user/agent-facing message. For ``GRANTED``/``DENIED`` it has "
            "already been delivered to the agent via ``mngr message``; for "
            "``NEEDS_MANUAL_CREDENTIALS`` it is shown only to the user."
        ),
    )
    response_event: RequestResponseEvent | None = Field(
        description=(
            "The freshly-appended response event when the request was resolved. "
            "``None`` for ``NEEDS_MANUAL_CREDENTIALS`` because the request stays pending."
        ),
    )
    set_credentials_example: str | None = Field(
        description=(
            "Suggested ``latchkey auth set`` invocation to show the user. Only set "
            "when ``outcome == NEEDS_MANUAL_CREDENTIALS``."
        ),
    )


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
                # ``-m`` and ``--`` are required: ``mngr message`` treats every
                # positional argument as an agent identifier (``nargs=-1``), so
                # passing the text as a positional would be parsed as a second
                # agent and the actual message content would be read from
                # stdin (silently empty in this subprocess context).
                command=[self.mngr_binary, "message", "-m", text, "--", str(agent_id)],
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


def _format_manual_credentials_message(service_display_name: str) -> str:
    return f"{service_display_name} does not support browser sign-in; manual credentials are required."


def _fallback_set_credentials_example(service_name: str) -> str:
    """Return a generic ``latchkey auth set`` invocation when latchkey didn't supply one."""
    return f'latchkey auth set {service_name} -H "Authorization: Bearer <token>"'


def _prepend_latchkey_directory(command: str, latchkey_directory: Path | None) -> str:
    """Prefix ``command`` with ``LATCHKEY_DIRECTORY=<dir>`` so the credential
    written by the user lands in the same store the desktop client uses.

    No-op when the desktop client doesn't pin a custom directory (then the
    user's terminal will inherit latchkey's own default, matching what the
    desktop client also does).
    """
    if latchkey_directory is None:
        return command
    return f"LATCHKEY_DIRECTORY={shlex.quote(str(latchkey_directory))} {command}"


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
        f'<form method="POST" action="/requests/{html_module.escape(request_id, quote=True)}/deny">'
        '<button type="submit">Deny</button></form>'
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=200)


class LatchkeyPermissionGrantHandler(RequestEventHandler):
    """Top-level orchestrator for ``LatchkeyPermissionRequestEvent`` handling.

    Owns the latchkey services catalog and exposes both pure-logic methods
    (``grant`` / ``deny``, easy to unit-test) and the HTTP-aware
    :class:`RequestEventHandler` entry points the route dispatcher in
    ``app.py`` calls into.

    Hold-time invariants when ``grant`` returns ``GrantOutcome.GRANTED``:

    * ``latchkey_permissions.json`` reflects the new rule.
    * A ``GRANTED`` response event has been appended for ``request_event_id``.
    * ``mngr message`` has been attempted (failures logged).

    When ``grant`` returns ``GrantOutcome.DENIED`` (failed browser sign-in):

    * ``latchkey_permissions.json`` is unchanged.
    * A ``DENIED`` response event has been appended (the agent is told the
      reason via the message, not via a distinct status).
    * ``mngr message`` has been attempted.

    When ``grant`` returns ``GrantOutcome.NEEDS_MANUAL_CREDENTIALS`` (the
    service has no valid credentials and latchkey doesn't expose a browser
    flow for it):

    * ``latchkey_permissions.json`` is unchanged.
    * No response event has been written; the request stays pending so the
      user can run the suggested ``latchkey auth set`` command and click
      Approve again.
    * No ``mngr message`` has been sent.

    ``deny`` writes a ``DENIED`` response and notifies; nothing else.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ~/.minds).")
    latchkey: Latchkey = Field(description="Latchkey wrapper used to probe credentials and run sign-in flows.")
    services_catalog: Mapping[str, ServicePermissionInfo] = Field(
        description=(
            "Catalog mapping latchkey service names to detent permission info. Empty if loading failed at startup."
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
    ) -> GrantResult:
        """Apply a grant, falling back to a manual-credentials flow when needed.

        The HTTP layer mirrors any non-None ``response_event`` into the
        in-memory inbox so it doesn't have to reload from disk, and
        surfaces ``message`` to both the agent (via ``mngr message``) and
        the dialog UI.
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

        latchkey_service_info = self.latchkey.services_info(service_info.name)
        if latchkey_service_info.credential_status != CredentialStatus.VALID:
            # If latchkey advertises a browser flow (or returned no
            # ``authOptions`` at all and we don't actually know), keep the
            # legacy behaviour and run it. Otherwise refuse the grant and
            # ask the user to set credentials manually -- the request
            # stays pending so a follow-up Approve click re-checks status.
            is_browser_supported = (
                LATCHKEY_AUTH_OPTION_BROWSER in latchkey_service_info.auth_options
                or not latchkey_service_info.auth_options
            )
            if not is_browser_supported:
                logger.info(
                    "Credentials for {} reported as {}; latchkey does not advertise a browser flow, "
                    "asking user to run 'latchkey auth set'",
                    service_info.name,
                    latchkey_service_info.credential_status,
                )
                return GrantResult(
                    outcome=GrantOutcome.NEEDS_MANUAL_CREDENTIALS,
                    message=_format_manual_credentials_message(service_info.display_name),
                    response_event=None,
                    set_credentials_example=_prepend_latchkey_directory(
                        latchkey_service_info.set_credentials_example
                        or _fallback_set_credentials_example(service_info.name),
                        self.latchkey.latchkey_directory,
                    ),
                )
            logger.info(
                "Credentials for {} reported as {}; running latchkey auth browser",
                service_info.name,
                latchkey_service_info.credential_status,
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
                return GrantResult(
                    outcome=GrantOutcome.DENIED,
                    message=message,
                    response_event=response_event,
                    set_credentials_example=None,
                )

        # Apply the grant to latchkey_permissions.json before writing the response
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
        return GrantResult(
            outcome=GrantOutcome.GRANTED,
            message=granted_message,
            response_event=response_event,
            set_credentials_example=None,
        )

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

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return str(RequestType.LATCHKEY_PERMISSION)

    def kind_label(self) -> str:
        return "permission"

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        """Friendly service name for the requests-panel card.

        Falls back to the raw service name when the service isn't in
        the loaded catalog (or when the event is somehow not a latchkey
        permission request, which shouldn't happen given the dispatcher).
        """
        if not isinstance(req_event, LatchkeyPermissionRequestEvent):
            return ""
        info = get_service_info(self.services_catalog, req_event.service_name)
        return info.display_name if info is not None else req_event.service_name

    def render_request_page(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
    ) -> Response:
        """Render the dialog HTML for a latchkey permission request.

        Falls back to a deny-only page when the requested service is not
        in the catalog, since there are no permissions to offer.
        """
        if not isinstance(req_event, LatchkeyPermissionRequestEvent):
            return HTMLResponse(content="<p>Unsupported request type</p>", status_code=500)
        service_info = get_service_info(self.services_catalog, req_event.service_name)
        if service_info is None:
            return _render_unknown_service_page(
                request_id=str(req_event.event_id),
                service_name=req_event.service_name,
            )

        parsed_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_id, fallback=req_event.agent_id)
        pre_checked = self._initial_checked_permissions(parsed_id, service_info)

        # Match ``grant()``: ``latchkey auth browser`` runs only when
        # credentials are not VALID AND the service either advertises a
        # browser flow or returns no auth options at all (legacy fallback).
        # Computed up front so the dialog's progress notice tells the
        # truth about whether to expect a browser pop-up. If the status
        # changes between render and submit (rare), the user may see a
        # slightly inaccurate notice for one cycle; the actual outcome
        # is unaffected.
        latchkey_service_info = self.latchkey.services_info(service_info.name)
        will_open_browser = latchkey_service_info.credential_status != CredentialStatus.VALID and (
            LATCHKEY_AUTH_OPTION_BROWSER in latchkey_service_info.auth_options
            or not latchkey_service_info.auth_options
        )

        rendered = render_latchkey_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            service=service_info,
            checked_permissions=pre_checked,
            will_open_browser=will_open_browser,
        )
        return HTMLResponse(content=rendered)

    async def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Drive the grant flow from the dialog form submission."""
        if not isinstance(req_event, LatchkeyPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
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
            grant_result = await asyncio.get_running_loop().run_in_executor(
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

        # The grant call may have appended a response event to
        # ~/.minds/events/requests/events.jsonl; mirror it into the
        # in-memory inbox so the requests panel reflects the resolution
        # without needing a desktop-client restart. The manual-credentials
        # branch leaves the request pending, so there is nothing to mirror.
        if grant_result.response_event is not None:
            self._mirror_response_into_inbox(request, grant_result.response_event)

        response_payload: dict[str, str] = {
            "outcome": str(grant_result.outcome),
            "message": grant_result.message,
        }
        if grant_result.set_credentials_example is not None:
            response_payload["set_credentials_example"] = grant_result.set_credentials_example
        return Response(
            content=json.dumps(response_payload),
            media_type="application/json",
        )

    async def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Drive the deny flow from the dialog form submission."""
        if not isinstance(req_event, LatchkeyPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
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

        granted: set[str] = set()
        for scope in service_info.scope_schemas:
            granted.update(granted_permissions_for_scope(config, scope))
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
                "Existing latchkey_permissions.json at {} is unreadable; replacing it: {}",
                path,
                e,
            )
            existing = LatchkeyPermissionsConfig()

        updated = existing
        for scope in scope_schemas:
            updated = set_permissions_for_scope(
                updated,
                scope=scope,
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

        Also wakes the chrome SSE so the new ``request_count`` is pushed
        right away -- otherwise the panel would keep showing the resolved
        card for up to 30s while the SSE poll waits for its next tick.
        """
        inbox: RequestInbox | None = request.app.state.request_inbox
        if inbox is None:
            return
        request.app.state.request_inbox = inbox.add_response(response_event)
        backend_resolver = getattr(request.app.state, "backend_resolver", None)
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
