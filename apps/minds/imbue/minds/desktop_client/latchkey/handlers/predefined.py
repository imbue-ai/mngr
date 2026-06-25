"""Predefined-permission grant/deny flow (``RequestType.LATCHKEY_PERMISSION``).

This module is one of the two sibling handlers under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the
flow for *predefined* (catalog-backed) permission requests: rendering
the per-permission dialog, probing credential status, running
``latchkey auth browser`` when needed, rewriting the per-host
``latchkey_permissions.json`` via the gateway extension, appending the
response event, and notifying the waiting agent via ``mngr message``.

The :mod:`.file_sharing` sibling handles file-sharing permission
requests (single path, yes/no decision). Both siblings share the
:class:`~.messaging.MngrMessageSender` helper.

Services that latchkey reports as not supporting browser sign-in fall
back to a manual flow: the grant is refused (the request stays pending),
the user is shown the suggested ``latchkey auth set`` invocation, and a
fresh Approve click re-runs ``latchkey services info`` to check whether
credentials have since become valid.

The route layer in ``app.py`` is intentionally thin: it authenticates,
looks up the request event by id, and dispatches by request type. All
the latchkey-specific work lives here.
"""

import html as html_module
import json
import shlex
from collections.abc import Sequence
from enum import auto
from pathlib import Path

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.templates import render_predefined_permission_dialog
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
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
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import permissions_path_for_host


class GrantOutcome(UpperCaseStrEnum):
    """Possible outcomes of attempting to apply a permission grant."""

    GRANTED = auto()
    DENIED = auto()
    NEEDS_MANUAL_CREDENTIALS = auto()
    FAILED = auto()


class GrantResult(FrozenModel):
    """Outcome of ``LatchkeyPermissionGrantHandler.grant``."""

    outcome: GrantOutcome = Field(description="Which branch the grant flow took.")
    message: str = Field(
        description=(
            "Plain-text user/agent-facing message. For ``GRANTED`` it has "
            "already been delivered to the agent via ``mngr message``; for "
            "``FAILED`` and ``NEEDS_MANUAL_CREDENTIALS`` it is shown only to the user "
            "(the request stays pending, so the agent is not notified)."
        ),
    )
    response_event: RequestResponseEvent | None = Field(
        description=(
            "The freshly-appended response event when the request was resolved. "
            "``None`` for ``FAILED`` and ``NEEDS_MANUAL_CREDENTIALS`` because the request stays pending."
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


def _format_granted_message(service_display_name: str, granted: Sequence[str]) -> str:
    permissions = ", ".join(granted)
    return (
        f"Your permission request for {service_display_name} was granted with the following "
        f"permissions: {permissions}."
    )


def _format_denied_message(service_display_name: str) -> str:
    return f"Your permission request for {service_display_name} was denied."


def _format_auth_failed_message(service_display_name: str, detail: str) -> str:
    suffix = f" Reason: {detail}" if detail else ""
    return (
        f"Sign-in to {service_display_name} did not complete, so the permission could not be "
        f"granted at the moment.{suffix}"
    )


def _format_manual_credentials_message(service_display_name: str) -> str:
    return f"{service_display_name} does not support browser sign-in; manual credentials are required."


def _fallback_set_credentials_example(service_name: str) -> str:
    """Return a generic ``latchkey auth set`` invocation when latchkey didn't supply one."""
    return f'latchkey auth set {service_name} -H "Authorization: Bearer <token>"'


def _prepend_latchkey_directory(command: str, latchkey_directory: Path) -> str:
    """Prefix ``command`` with ``LATCHKEY_DIRECTORY=<dir>`` so the credential
    the user writes from their terminal lands in the same store the
    desktop client uses.

    Without the prefix the user's terminal-run ``latchkey`` would write
    credentials to its own default (``~/.latchkey``) and the desktop
    client (which runs latchkey with ``LATCHKEY_DIRECTORY`` set) would
    never see them.
    """
    return f"LATCHKEY_DIRECTORY={shlex.quote(str(latchkey_directory))} {command}"


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


def _resolve_host_id(
    backend_resolver: BackendResolverInterface,
    agent_id: AgentId,
) -> HostId | None:
    """Resolve the host an agent runs on, or ``None`` when discovery hasn't caught up.

    Latchkey permissions are stored per-host (see :func:`permissions_path_for_host`):
    every agent on the same host shares the same gateway wiring and the
    same ``latchkey_permissions.json``. The handler maps the incoming
    agent_id (carried by the permission request event) to its host_id
    via the backend resolver, which has the discovery-stream view of
    which agents live on which hosts. Returns ``None`` when the host
    id isn't known yet (e.g. agent freshly created and discovery
    stream hasn't pushed an update) or when the resolver reports the
    placeholder ``"localhost"`` string used by static / in-memory
    backend resolvers in tests.
    """
    info = backend_resolver.get_agent_display_info(agent_id)
    if info is None:
        return None
    try:
        return HostId(info.host_id)
    except ValueError:
        # Static / in-memory resolvers (e.g. ``StaticBackendResolver``
        # used by tests) report ``"localhost"`` here; that does not
        # match the ``host-<32 hex>`` HostId format. Treat it as
        # "unknown host" so callers skip the existing-grants lookup
        # rather than crash on every dialog render.
        logger.debug(
            "Backend resolver reported non-HostId host {!r} for agent {}; treating as unknown",
            info.host_id,
            agent_id,
        )
        return None


def _render_unknown_scope_fragment(request_id: str, scope: str) -> str:
    """Render a deny-only detail fragment when the requested scope isn't in the catalog.

    No catalog entry means we have no permissions to offer the user; the
    only action that makes sense from here is Deny. Shaped to share the
    inbox shell's deny submission JS: the fragment emits a
    ``#permissions-form`` whose ``action`` targets ``/requests/<id>/grant``
    so the shell's ``submitPermissionDeny`` helper (which rewrites
    ``/grant`` to ``/deny``) auto-advances the inbox after the user clicks
    Deny. There is no Approve button and no ``name="permissions"`` input
    because no permissions are on offer; the form's action URL is only
    used as the deny URL template.
    """
    escaped_scope = html_module.escape(scope)
    escaped_request_id = html_module.escape(request_id, quote=True)
    return (
        '<div class="permissions-detail">'
        '<h1 class="text-xl font-semibold text-zinc-900 leading-tight">Unknown scope</h1>'
        '<p class="mt-2 text-zinc-600">'
        f"The agent requested permissions under scope <code>{escaped_scope}</code>, "
        "but this scope is not in the latchkey service catalog. "
        "The request can only be denied from here."
        "</p>"
        '<form id="permissions-form" method="POST" '
        f'action="/requests/{escaped_request_id}/grant" class="mt-6">'
        '<div class="flex gap-2 mt-5 justify-end">'
        '<button type="button" onclick="submitPermissionDeny()" '
        'class="inline-flex items-center justify-center px-3.5 py-2 rounded-md font-medium text-sm '
        'bg-red-50 text-red-600 border border-red-200 hover:bg-red-100 cursor-pointer">Deny</button>'
        "</div></form>"
        "</div>"
    )


class LatchkeyPermissionGrantHandler(RequestEventHandler):
    """Top-level orchestrator for ``LatchkeyPredefinedPermissionRequestEvent`` handling.

    Owns the latchkey services catalog and exposes both pure-logic methods
    (``grant`` / ``deny``, easy to unit-test) and the HTTP-aware
    :class:`RequestEventHandler` entry points the route dispatcher in
    ``app.py`` calls into.

    Hold-time invariants when ``grant`` returns ``GrantOutcome.GRANTED``:

    * ``latchkey_permissions.json`` reflects the new rule.
    * A ``GRANTED`` response event has been appended for ``request_event_id``.
    * ``mngr message`` has been attempted (failures logged).

    When ``grant`` returns ``GrantOutcome.FAILED`` (the browser sign-in
    flow -- including the one-off ``latchkey auth browser-prepare`` step --
    did not complete):

    * ``latchkey_permissions.json`` is unchanged.
    * No response event has been written; the request stays pending so a
      fresh Approve click can retry the sign-in. A failed approval is a
      transient failure, not a denial -- it is surfaced to the user in the
      dialog rather than recorded as a resolution.
    * No ``mngr message`` has been sent (the agent stays blocked, waiting).

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
    services_catalog: ServicesCatalog = Field(
        description=(
            "Lazy in-memory snapshot of the latchkey services catalog, read from the bundled "
            "``services.json`` data file that ships with mngr_latchkey."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(description="Sends mngr message to the waiting agent.")
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to apply permission grants and remove pending requests through the "
            "gateway's bundled ``permissions`` / ``permission-requests`` extensions."
        ),
    )

    # -- Pure logic (unit-testable) ------------------------------------------

    def grant(
        self,
        request_event_id: str,
        agent_id: AgentId,
        host_id: HostId,
        service_info: ServicePermissionInfo,
        granted_permissions: Sequence[str],
    ) -> GrantResult:
        """Apply a grant, falling back to a manual-credentials flow when needed.

        ``host_id`` is the agent's host: latchkey permissions are stored
        per-host (every agent on the host shares one
        ``latchkey_permissions.json``) so the grant updates the file at
        :func:`permissions_path_for_host`. ``agent_id`` is still needed
        for the response event and the ``mngr message`` nudge.

        ``service_info`` is the catalog entry resolved from the request's
        ``scope`` schema (e.g. ``slack-api`` -> ``ServicePermissionInfo``
        for ``slack``). It supplies the human-readable display name, the
        latchkey service name for ``services_info`` / ``auth_browser``,
        and the legal permission set used to validate the dialog form.

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
                "Credentials for {} reported as {}; running browser sign-in",
                service_info.name,
                latchkey_service_info.credential_status,
            )
            # ``auth_browser`` owns all of the auth-flow logic, including the
            # Minds Google OAuth client preference for ``google-*`` services.
            is_success, detail = self.latchkey.auth_browser(service_info.name)
            if not is_success:
                # The browser sign-in (or its one-off ``auth
                # browser-prepare`` step) did not complete. Treat this as a
                # FAILED approval, not a denial: leave the request pending
                # (no response event, gateway record untouched, agent not
                # notified) so a fresh Approve click can retry, and surface
                # the reason to the user in the dialog.
                return GrantResult(
                    outcome=GrantOutcome.FAILED,
                    message=_format_auth_failed_message(service_info.display_name, detail),
                    response_event=None,
                    set_credentials_example=None,
                )

        # Apply the grant to latchkey_permissions.json before writing the response
        # event so the agent can never observe a GRANTED response without
        # the corresponding rule being in effect.
        self._apply_grant_to_permissions_file(
            host_id=host_id,
            scope=service_info.scope,
            granted_permissions=granted_permissions,
        )

        granted_message = _format_granted_message(service_info.display_name, granted_permissions)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            scope=service_info.scope,
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
        scope: str,
        display_name: str,
    ) -> tuple[str, RequestResponseEvent]:
        """Append a DENIED response and notify the agent. Returns ``(message, response_event)``.

        ``scope`` is the Detent scope schema the request was filed under;
        it goes into the response event for informational purposes (the
        inbox joins responses to requests on ``request_event_id``).
        ``display_name`` is the human-readable service name shown in the
        agent-facing message.
        """
        message = _format_denied_message(display_name)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            scope=scope,
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
        """Friendly service name for the inbox list card.

        Falls back to the raw scope schema when no catalog entry matches
        (or when the event is somehow not a latchkey permission request,
        which shouldn't happen given the dispatcher).
        """
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return ""
        info = self.services_catalog.get_by_scope(req_event.scope)
        return info.display_name if info is not None else req_event.scope

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        """Render the inbox right-pane fragment for a latchkey permission request.

        Falls back to a deny-only fragment when the requested service is
        not in the catalog, since there are no permissions to offer.
        """
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return "<p>Unsupported request type</p>"
        service_info = self.services_catalog.get_by_scope(req_event.scope)
        if service_info is None:
            return _render_unknown_scope_fragment(
                request_id=str(req_event.event_id),
                scope=req_event.scope,
            )

        parsed_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_id, fallback=req_event.agent_id)
        host_id = _resolve_host_id(backend_resolver, parsed_id)
        pre_checked = self._initial_checked_permissions(host_id, service_info, req_event.permissions)

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

        return render_predefined_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            service=service_info,
            checked_permissions=pre_checked,
            will_open_browser=will_open_browser,
            mngr_forward_origin=mngr_forward_origin,
        )

    def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Drive the grant flow from the dialog form submission."""
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        service_info = self.services_catalog.get_by_scope(req_event.scope)
        if service_info is None:
            return _json_error(
                f"Scope '{req_event.scope}' is not in the gateway catalog",
                status_code=400,
            )

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
        try:
            grant_result = self.grant(
                request_event_id=request_event_id,
                agent_id=parsed_agent_id,
                host_id=host_id,
                service_info=service_info,
                granted_permissions=granted_permissions,
            )
        except LatchkeyPermissionFlowError as e:
            return _json_error(str(e), status_code=400)
        except LatchkeyGatewayClientError as e:
            # The grant flow could not reach the gateway's permissions
            # extension; surface that as a 502 so the dialog can show a
            # meaningful error instead of a generic 500.
            logger.warning("Could not apply latchkey permission grant via gateway: {}", e)
            return _json_error(
                f"Could not apply grant through the latchkey gateway: {e}",
                status_code=502,
            )

        # The grant call may have appended a response event to
        # ~/.minds/events/requests/events.jsonl; mirror it into the
        # in-memory inbox so the inbox modal reflects the resolution
        # without needing a desktop-client restart. The manual-credentials
        # branch leaves the request pending, so there is nothing to mirror.
        if grant_result.response_event is not None:
            self._mirror_response_into_inbox(grant_result.response_event)

        response_payload: dict[str, str] = {
            "outcome": str(grant_result.outcome),
            "message": grant_result.message,
        }
        if grant_result.set_credentials_example is not None:
            response_payload["set_credentials_example"] = grant_result.set_credentials_example
        return make_response(
            content=json.dumps(response_payload),
            media_type="application/json",
        )

    def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Drive the deny flow from the dialog form submission."""
        if not isinstance(req_event, LatchkeyPredefinedPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        service_info = self.services_catalog.get_by_scope(req_event.scope)
        if service_info is None:
            # Even invalid permission requests can be denied.
            display_name = req_event.scope
        else:
            display_name = service_info.display_name

        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        _, response_event = self.deny(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            scope=req_event.scope,
            display_name=display_name,
        )
        self._mirror_response_into_inbox(response_event)
        return make_response(
            content=json.dumps({"outcome": "DENIED"}),
            media_type="application/json",
        )

    # -- Internals -----------------------------------------------------------

    def _initial_checked_permissions(
        self,
        host_id: HostId | None,
        service_info: ServicePermissionInfo,
        requested_permissions: Sequence[str],
    ) -> tuple[str, ...]:
        """Pick the initial checkbox state for the dialog.

        The pre-check is the union of (a) permissions already granted
        for this scope on this host (so the dialog doubles as a revoke
        UI) and (b) the permissions the agent requested, both
        intersected with the catalog's known permission schemas for the
        scope. Approving without modification grants exactly that union.

        The catch-all ``any`` schema is intentionally not in the
        pre-check: the user must opt into it explicitly. If both the
        existing grants and the agent's request are empty (or fall
        entirely outside the catalog), the pre-check is empty and the
        Approve button stays disabled until the user ticks something.

        ``host_id`` is ``None`` when the agent's host cannot be resolved
        (transient discovery gap); in that case we skip the existing-
        grants lookup rather than fail the page render -- the user can
        still click Approve, which re-resolves the host before writing
        the grant.
        """
        existing: tuple[str, ...] = ()
        if host_id is not None:
            path = permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
            try:
                granted = self.gateway_client.get_granted_permissions_for_scopes(
                    path,
                    (service_info.scope,),
                )
            except LatchkeyGatewayClientError as e:
                logger.warning(
                    "Could not load permissions for host {} via the gateway extension; pre-check will "
                    "reflect only the agent's request: {}",
                    host_id,
                    e,
                )
            else:
                existing = tuple(p for p in service_info.permission_schemas if p in granted)
        # Preserve catalog order and deduplicate. ``dict.fromkeys``
        # gives an order-preserving set so a permission that appears in
        # both ``existing`` and ``requested_permissions`` is checked once.
        requested_set = set(requested_permissions)
        union = tuple(dict.fromkeys(p for p in service_info.permission_schemas if p in existing or p in requested_set))
        return union

    def _apply_grant_to_permissions_file(
        self,
        host_id: HostId,
        scope: str,
        granted_permissions: Sequence[str],
    ) -> None:
        """Apply a grant by POSTing through the gateway's ``permissions`` extension.

        The extension owns the actual write to
        ``<plugin_data_dir>/hosts/<host_id>/latchkey_permissions.json``;
        we just tell it which scope to upsert.
        """
        path = permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
        self.gateway_client.set_permission_rule(
            permissions_file_path=path,
            rule_key=scope,
            granted_permissions=granted_permissions,
        )

    def _write_response_and_notify(
        self,
        request_event_id: str,
        agent_id: AgentId,
        scope: str,
        status: RequestStatus,
        message: str,
    ) -> RequestResponseEvent:
        """Persist the response event to disk, drop the gateway record, and notify the agent.

        Returns the newly-created event so callers can mirror it into the
        in-memory inbox without re-creating it (and getting a fresh event_id).

        Three things happen in order:

        1. Issue ``DELETE /permission-requests/<request_event_id>`` so
           the gateway forgets the pending entry (a future reconnect of
           the follow stream must not redeliver an already-resolved
           request). Failure is logged but does not abort: the user
           cares more about the agent getting unblocked than about a
           stale on-disk file the gateway will clean up next restart.
        2. Append the response event to the on-disk JSONL so the inbox
           survives a desktop-client restart.
        3. Send the agent a ``mngr message`` nudge.
        """
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE permission request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            scope=scope,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
        return response_event

    def _mirror_response_into_inbox(
        self,
        response_event: RequestResponseEvent,
    ) -> None:
        """Mirror the on-disk response event into the in-memory inbox.

        The on-disk event-sourcing log is the source of truth; this update
        is just so the inbox modal doesn't show the resolved request as
        still pending until the next desktop-client restart.

        Also wakes the chrome SSE so the new ``requests`` payload is pushed
        right away -- otherwise the inbox would keep showing the resolved
        card for up to 30s while the SSE poll waits for its next tick.
        """
        inbox: RequestInbox | None = get_state().request_inbox
        if inbox is None:
            return
        get_state().request_inbox = inbox.add_response(response_event)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
