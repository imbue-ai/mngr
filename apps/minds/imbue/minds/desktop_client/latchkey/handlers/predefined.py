"""Predefined-permission grant/deny flow (``RequestType.LATCHKEY_PERMISSION``).

This module is one of the two sibling handlers under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the
flow for *predefined* (catalog-backed) permission requests: rendering
the account + per-permission dialog, probing credential status, running
``latchkey auth browser`` when needed, rewriting the per-host
``latchkey_permissions.json`` via the gateway extension, appending the
response event, and notifying the waiting agent via ``mngr message``.

Grants are *per account*: the dialog always resolves to exactly one
latchkey account (an existing one or a freshly signed-in one) and the
rule it writes is keyed ``<scope>:<account>`` (see
:mod:`imbue.mngr_latchkey.account_scopes`), so a second account of the
same service gets no access until it is granted in its own right.

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
from imbue.minds.desktop_client.latchkey.handlers.templates import DEFAULT_ACCOUNT_LABEL
from imbue.minds.desktop_client.latchkey.handlers.templates import NEW_ACCOUNT_FORM_VALUE
from imbue.minds.desktop_client.latchkey.handlers.templates import PermissionAccountChoice
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
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
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


def _format_granted_message(service_display_name: str, granted: Sequence[str], account: str) -> str:
    permissions = ", ".join(granted)
    account_clause = "" if account == DEFAULT_ACCOUNT else f" for account '{account}'"
    return (
        f"Your permission request for {service_display_name} was granted{account_clause} with the "
        f"following permissions: {permissions}."
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


def _supports_browser_auth(latchkey_service_info: LatchkeyServiceInfo) -> bool:
    """True when ``latchkey auth browser`` is the right way to fix credentials.

    Either latchkey explicitly advertises a browser flow, or it returned
    no ``authOptions`` at all and we don't actually know (legacy
    fallback: keep the old always-run-browser behaviour).
    """
    return LATCHKEY_AUTH_OPTION_BROWSER in latchkey_service_info.auth_options or not latchkey_service_info.auth_options


def _account_label(account: str) -> str:
    """Render a latchkey account key as a user-facing label (the default one is unnamed)."""
    return DEFAULT_ACCOUNT_LABEL if account == DEFAULT_ACCOUNT else account


def _sorted_accounts(accounts: Sequence[ServiceAccountCredential]) -> tuple[ServiceAccountCredential, ...]:
    """Order accounts for display: named ones alphabetically, the unnamed default last.

    Matches the connectors settings page so the same service lists its accounts
    in the same order everywhere.
    """
    return tuple(
        sorted(accounts, key=lambda entry: (entry.account == DEFAULT_ACCOUNT, entry.account.lower())),
    )


def _needs_account_credential_setup(credential_status: CredentialStatus) -> bool:
    """True when one account's credentials must be (re-)established before granting.

    Only intervene when latchkey is certain the credentials are absent
    (MISSING) or known-broken (INVALID). VALID obviously proceeds. UNKNOWN also
    proceeds: it covers both generic ``rawCurl`` credentials latchkey has no way
    to verify, and catalog scopes that are not registered latchkey services at
    all (e.g. the minds-internal scopes served by a gateway extension that
    injects its own credential -- ``latchkey services info`` fails and degrades
    to UNKNOWN). Treating UNKNOWN as "needs setup" would prompt the user for
    credentials that either already exist or were never theirs to manage; if a
    credential is in fact stale, the downstream API call will fail and surface a
    real error instead.
    """
    return credential_status in (CredentialStatus.MISSING, CredentialStatus.INVALID)


def _build_account_choices(
    accounts: Sequence[ServiceAccountCredential],
    requested_account: str | None,
) -> tuple[tuple[PermissionAccountChoice, ...], str]:
    """Build the dialog's account radio list and the value to preselect.

    Every account currently signed in to the service is offered, plus the
    always-available "new account" choice.

    An agent may name an account that is *not* signed in -- a typo, an account
    the user has on the service but never connected here, or one whose
    credentials were since cleared. That account is offered as its own choice
    (and preselected) rather than dropped, because dropping it silently would
    grant a *different* account on the next Approve click while the agent keeps
    using the one it asked for, and stays blocked. Picking it runs the sign-in
    flow and grants whichever account latchkey ends up storing (see
    :meth:`LatchkeyPermissionGrantHandler._account_after_sign_in`), which is the
    requested one whenever the user does sign in as it.

    The preselection is otherwise the first signed-in account, or the
    new-account choice when nothing is signed in.
    """
    ordered = _sorted_accounts(accounts)
    choices = [
        PermissionAccountChoice(
            value=entry.account,
            label=_account_label(entry.account),
            hint="needs sign-in" if _needs_account_credential_setup(entry.credential_status) else "",
        )
        for entry in ordered
    ]
    is_requested_signed_in = any(entry.account == requested_account for entry in ordered)
    if requested_account is not None and not is_requested_signed_in:
        choices.append(
            PermissionAccountChoice(
                value=requested_account,
                label=_account_label(requested_account),
                hint="not connected yet -- opens a browser sign-in",
            )
        )
    choices.append(
        PermissionAccountChoice(
            value=NEW_ACCOUNT_FORM_VALUE,
            # "Sign in" only reads right when it is the single option.
            label="Sign in" if len(choices) == 0 else "Use a new account",
            hint="opens a browser sign-in",
        )
    )
    if requested_account is not None:
        selected = requested_account
    elif ordered:
        selected = ordered[0].account
    else:
        selected = NEW_ACCOUNT_FORM_VALUE
    return tuple(choices), selected


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
        account_choice: str,
    ) -> GrantResult:
        """Apply a grant for one account, signing in / falling back as needed.

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

        ``account_choice`` is the dialog's account radio: either an account
        latchkey has stored for the service (the unnamed default account is
        the empty string) or :data:`NEW_ACCOUNT_FORM_VALUE`, which signs a
        fresh one in. Whatever it resolves to is the *only* account the
        resulting rule grants (see :mod:`imbue.mngr_latchkey.account_scopes`).

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

        resolved = self._resolve_account_for_grant(service_info, account_choice)
        if isinstance(resolved, GrantResult):
            # Credentials could not be established (sign-in cancelled, manual
            # credentials required, ...): the request stays pending.
            return resolved

        # Apply the grant to latchkey_permissions.json before writing the response
        # event so the agent can never observe a GRANTED response without
        # the corresponding rule being in effect.
        self._apply_grant_to_permissions_file(
            host_id=host_id,
            scope=service_info.scope,
            account=resolved,
            granted_permissions=granted_permissions,
        )

        granted_message = _format_granted_message(service_info.display_name, granted_permissions, resolved)
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

    def _resolve_account_for_grant(
        self,
        service_info: ServicePermissionInfo,
        account_choice: str,
    ) -> str | GrantResult:
        """Turn the dialog's account choice into the concrete account to grant.

        Returns the account name on success, or the :class:`GrantResult` the
        caller must return when credentials could not be established (which
        always leaves the request pending so the user can retry).

        Three paths:

        * the chosen account is signed in and its credentials are usable ->
          nothing to do;
        * the chosen account is signed in but its credentials are
          missing/invalid -> re-run the browser sign-in for that account (or
          ask for manual credentials when the service has no browser flow);
        * the user picked "new account" (or an account latchkey no longer
          knows about, e.g. a stale dialog) -> run the sign-in and read back
          which account it stored.

        The sign-in uses latchkey's *ephemeral* browser mode only when the
        service already has at least one account: that mode exists so an
        additional account is not silently bound to the session an existing
        one left behind. The first sign-in for a service deliberately uses the
        normal browser state, which is what the user expects when they have
        never connected the service before.
        """
        latchkey_service_info = self.latchkey.services_info(service_info.name)
        accounts_by_name = {entry.account: entry for entry in latchkey_service_info.accounts}
        # A submitted value that names a stored account always *is* that account;
        # only a value matching nothing (the new-account choice, or a stale
        # dialog naming a since-removed account) starts a sign-in.
        chosen = accounts_by_name.get(account_choice)
        if chosen is not None and not _needs_account_credential_setup(chosen.credential_status):
            return chosen.account

        if not _supports_browser_auth(latchkey_service_info):
            # No browser flow: refuse the grant and ask the user to set
            # credentials manually. The request stays pending so a follow-up
            # Approve click re-checks the status.
            logger.info(
                "Credentials for {} account {!r} reported as unusable; latchkey does not advertise a "
                "browser flow, asking user to run 'latchkey auth set'",
                service_info.name,
                account_choice,
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

        accounts_before = frozenset(accounts_by_name)
        if chosen is not None:
            logger.info(
                "Credentials for {} account {!r} reported as {}; running browser sign-in",
                service_info.name,
                chosen.account,
                chosen.credential_status,
            )
            # ``auth_browser`` owns all of the auth-flow logic, including the
            # Minds Google OAuth client preference for ``google-*`` services.
            is_success, detail = self.latchkey.auth_browser(service_info.name, account=chosen.account)
        elif accounts_before:
            logger.info("Adding a new {} account through the permission dialog", service_info.name)
            is_success, detail = self.latchkey.add_account(service_info.name)
        else:
            logger.info("Signing in to {} for the first time through the permission dialog", service_info.name)
            is_success, detail = self.latchkey.auth_browser(service_info.name)
        if not is_success:
            # The browser sign-in (or its one-off ``auth browser-prepare``
            # step) did not complete. Treat this as a FAILED approval, not a
            # denial: leave the request pending (no response event, gateway
            # record untouched, agent not notified) so a fresh Approve click
            # can retry, and surface the reason to the user in the dialog.
            return GrantResult(
                outcome=GrantOutcome.FAILED,
                message=_format_auth_failed_message(service_info.display_name, detail),
                response_event=None,
                set_credentials_example=None,
            )
        return self._account_after_sign_in(service_info, accounts_before, chosen)

    def _account_after_sign_in(
        self,
        service_info: ServicePermissionInfo,
        accounts_before: frozenset[str],
        chosen: ServiceAccountCredential | None,
    ) -> str | GrantResult:
        """Read back which account a completed sign-in actually stored.

        latchkey stores the credentials under whichever account the user logged
        in as, which need not be the one the dialog asked for, so the account
        is resolved from the store rather than assumed: a newly-appeared
        account wins, otherwise the account we asked to refresh (if it is still
        there), otherwise the service's only account. Anything else is
        ambiguous and fails the approval rather than granting the wrong
        account.
        """
        accounts_after = tuple(entry.account for entry in self.latchkey.services_info(service_info.name).accounts)
        added = [account for account in accounts_after if account not in accounts_before]
        if len(added) == 1:
            return added[0]
        if chosen is not None and chosen.account in accounts_after:
            return chosen.account
        if len(accounts_after) == 1:
            return accounts_after[0]
        logger.warning(
            "Could not tell which {} account the sign-in stored (before={}, after={}); not granting",
            service_info.name,
            sorted(accounts_before),
            sorted(accounts_after),
        )
        return GrantResult(
            outcome=GrantOutcome.FAILED,
            message=(
                f"Could not tell which {service_info.display_name} account was signed in, so the "
                "permission was not granted. Try approving again and picking the account explicitly."
            ),
            response_event=None,
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

        # One ``services info`` call feeds both the account picker and the
        # progress notice below.
        latchkey_service_info = self.latchkey.services_info(service_info.name)
        account_choices, selected_account = _build_account_choices(latchkey_service_info.accounts, req_event.account)
        # Existing grants are per account, so the pre-check reflects the
        # preselected one; switching accounts in the dialog does not re-fetch
        # (the user can still adjust the checkboxes by hand).
        pre_checked = self._initial_checked_permissions(host_id, service_info, req_event.permissions, selected_account)

        # Match ``grant()``: ``latchkey auth browser`` runs when the selected
        # account has no usable credentials (or a brand-new account is being
        # signed in) AND the service either advertises a browser flow or
        # returns no auth options at all (legacy fallback). Computed up front
        # so the dialog's progress notice tells the truth about whether to
        # expect a browser pop-up. If the selection or the status changes
        # between render and submit, the user may see a slightly inaccurate
        # notice for one cycle; the actual outcome is unaffected.
        selected_status_by_account = {
            entry.account: entry.credential_status for entry in latchkey_service_info.accounts
        }
        selected_status = selected_status_by_account.get(selected_account)
        will_open_browser = (
            selected_status is None or _needs_account_credential_setup(selected_status)
        ) and _supports_browser_auth(latchkey_service_info)

        return render_predefined_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            service=service_info,
            checked_permissions=pre_checked,
            account_choices=account_choices,
            selected_account_value=selected_account,
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
        # The dialog always preselects an account radio, so an absent field
        # means the form was not the one we rendered.
        account_choice = form.get("account")
        if account_choice is None:
            return _json_error(
                "An account must be selected to approve the request.",
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
                account_choice=str(account_choice),
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
        account: str,
    ) -> tuple[str, ...]:
        """Pick the initial checkbox state for the dialog.

        The pre-check is the union of (a) permissions already granted
        for this scope *and account* on this host (so the dialog doubles
        as a revoke UI) and (b) the permissions the agent requested, both
        intersected with the catalog's known permission schemas for the
        scope. Approving without modification grants exactly that union.
        ``account`` is the dialog's preselected one; the new-account choice
        (and any account with no grants yet) simply contributes nothing.

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
                config = self.gateway_client.get_permissions_config(path)
            except LatchkeyGatewayClientError as e:
                logger.warning(
                    "Could not load permissions for host {} via the gateway extension; pre-check will "
                    "reflect only the agent's request: {}",
                    host_id,
                    e,
                )
            else:
                # Which rule grants this (service, account) pair is resolved from
                # the file's schemas, never from a rule key's name.
                granted = {
                    permission
                    for grant in self.services_catalog.list_service_account_grants(config)
                    if grant.service_name == service_info.name and grant.account == account
                    for permission in grant.permissions
                }
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
        account: str,
        granted_permissions: Sequence[str],
    ) -> None:
        """Apply a grant by POSTing through the gateway's ``permissions`` extension.

        The extension owns the actual write to
        ``<plugin_data_dir>/hosts/<host_id>/latchkey_permissions.json`` but
        authors nothing: :func:`build_account_grant` composes the rule key, the
        permission list, and the schema that gates the scope on ``account``, and
        we hand all three over so the grant applies to ``account`` and to no
        other account of the service.
        """
        path = permissions_path_for_host(self.latchkey.plugin_data_dir, host_id)
        rule_key, permissions, schemas = build_account_grant(scope, account, granted_permissions)
        self.gateway_client.set_permission_rule(
            permissions_file_path=path,
            rule_key=rule_key,
            granted_permissions=permissions,
            schemas=schemas,
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
