"""Predefined-permission grant/deny flow (wire ``request_type == "predefined"``).

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
back to a manual flow: the grant is refused (the request stays pending)
and the dialog is handed the service's suggested ``latchkey auth set``
invocation split into one labeled input per ``<placeholder>`` (see
:mod:`imbue.mngr_latchkey.credential_commands`). The next Approve click
carries the typed values, which minds substitutes into the command and
runs itself -- pinned to the account the dialog selected -- before
re-checking the credential status and continuing the grant. An example
that has no placeholders (or is not a latchkey command at all) yields a
prompt with no parameters, which the dialog renders as an error with no
Approve button.

The route layer in ``app.py`` is intentionally thin: it authenticates,
looks up the request event by id, and dispatches by request type. All
the latchkey-specific work lives here.
"""

import json
from collections.abc import Sequence
from enum import auto
from pathlib import Path

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_PREDEFINED
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.account_choices import DEFAULT_ACCOUNT_LABEL
from imbue.minds.desktop_client.latchkey.handlers.account_choices import NEW_ACCOUNT_FORM_VALUE
from imbue.minds.desktop_client.latchkey.handlers.account_choices import PermissionAccountChoice
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.recovery import maybe_recover_host_permissions
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.permission_toggles import group_permissions_by_area
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiManualCredentialsPrompt
from imbue.minds.desktop_client.request_handler import UiPermissionAccountChoice
from imbue.minds.desktop_client.request_handler import UiPredefinedPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnknownScopeDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_models import UiPermissionGrantGroup
from imbue.minds.desktop_client.ui_models import UiPermissionGrantRow
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.credential_commands import CredentialCommandError
from imbue.mngr_latchkey.credential_commands import ParsedCredentialCommand
from imbue.mngr_latchkey.credential_commands import build_credential_command_argv
from imbue.mngr_latchkey.credential_commands import describe_credential_command_failure
from imbue.mngr_latchkey.credential_commands import fallback_set_credentials_example
from imbue.mngr_latchkey.credential_commands import parse_credential_command_example
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import permissions_path_for_host


class GrantOutcome(UpperCaseStrEnum):
    """Possible outcomes of attempting to apply a permission grant."""

    GRANTED = auto()
    DENIED = auto()
    NEEDS_MANUAL_CREDENTIALS = auto()
    FAILED = auto()


class ManualCredentialSubmission(FrozenModel):
    """The credential-form values an Approve click carries back for a manual-credentials service."""

    value_by_parameter_name: dict[str, str] = Field(
        description="Value the user typed for each ``<placeholder>``; empty on the Approve click that opens the form.",
    )
    account_name: str = Field(
        description="Name for the new account the credentials belong to; empty unless the dialog asked for one.",
    )


def _services_info_or_assumed(latchkey: Latchkey, service_name: str) -> LatchkeyServiceInfo:
    """The service's ``services info`` probe, or this flow's assumption when it does not answer.

    Everything the dialog renders and grants is read off this probe, but the
    flow still has to offer *something* the user can act on when latchkey has
    no answer (``None``). It assumes the legacy shape: no stored accounts and
    empty ``auth_options``, which reads as "offer the browser sign-in". The
    Approve then simply runs the sign-in, which either works or reports its
    own failure -- refusing to render would leave the request stuck instead.
    """
    info = latchkey.services_info(service_name)
    if info is not None:
        return info
    logger.debug("No services-info answer for {}; the permission dialog assumes a browser sign-in", service_name)
    return LatchkeyServiceInfo(
        credential_status=CredentialStatus.UNKNOWN,
        accounts=(),
        auth_options=frozenset(),
        set_credentials_example=None,
    )


# What ``grant`` is given when the dialog submitted no credential form at all
# (every request except a second Approve click on a manual-credentials service).
EMPTY_MANUAL_CREDENTIAL_SUBMISSION: ManualCredentialSubmission = ManualCredentialSubmission(
    value_by_parameter_name={},
    account_name="",
)


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
    manual_credentials: UiManualCredentialsPrompt | None = Field(
        description=(
            "The credential form the dialog must render (with its message) before the next Approve "
            "click. Only set when ``outcome == NEEDS_MANUAL_CREDENTIALS``."
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


class ManualCredentialsForm(FrozenModel):
    """A service's credential command as the dialog needs it: what to ask for, and what to run."""

    prompt: UiManualCredentialsPrompt = Field(description="What the dialog renders: the inputs and their heading.")
    parsed_command: ParsedCredentialCommand | None = Field(
        description="Command the typed values are substituted into; ``None`` when the example is unusable.",
    )


def _build_manual_credentials_form(
    service_name: str,
    service_display_name: str,
    set_credentials_example: str | None,
) -> ManualCredentialsForm:
    """Turn a service's suggested credential command into the dialog's input form.

    The command itself is an implementation detail the user never sees: only
    its ``<placeholder>`` parameters become inputs. A command that cannot be
    turned into inputs yields a parameter-less prompt, which the dialog renders
    as an error with no Approve.
    """
    command_example = set_credentials_example or fallback_set_credentials_example(service_name)
    try:
        parsed_command = parse_credential_command_example(command_example)
    except CredentialCommandError as e:
        logger.warning("Could not build a credential form for {} from its suggested command: {}", service_name, e)
        return ManualCredentialsForm(
            prompt=UiManualCredentialsPrompt(
                parameters=(),
                message=(
                    f"{service_display_name} does not support browser sign-in, and Minds cannot work out "
                    "which credentials to ask for. It has to be connected some other way."
                ),
            ),
            parsed_command=None,
        )
    return ManualCredentialsForm(
        prompt=UiManualCredentialsPrompt(
            parameters=parsed_command.parameters,
            message=(
                f"{service_display_name} does not support browser sign-in, so Minds needs its credentials. "
                "Get them from the provider and fill them in -- Approve stores them and grants the permission."
            ),
        ),
        parsed_command=parsed_command,
    )


def _manual_credentials_result(message: str, prompt: UiManualCredentialsPrompt) -> GrantResult:
    """Build the result that leaves the request pending and re-shows the credential form.

    ``message`` replaces the prompt's own instruction, so the dialog explains
    what went wrong with the attempt instead of repeating the instruction.
    """
    return GrantResult(
        outcome=GrantOutcome.NEEDS_MANUAL_CREDENTIALS,
        message=message,
        manual_credentials=prompt.model_copy_update(to_update(prompt.field_ref().message, message)),
    )


def _manual_credentials_account(
    account_choice: str,
    submitted_account_name: str,
    is_account_name_required: bool,
) -> str:
    """Resolve which latchkey account manually-entered credentials belong to.

    The new-account choice resolves to latchkey's unnamed default account,
    which is what the first account of a service is; once a service has one,
    the dialog collects a name for the next (see ``is_account_name_required``).
    """
    if is_account_name_required:
        return submitted_account_name.strip()
    if account_choice == NEW_ACCOUNT_FORM_VALUE:
        return DEFAULT_ACCOUNT
    return account_choice


def _account_label(account: str) -> str:
    """Render a latchkey account key as a user-facing label (the default one is unnamed)."""
    return DEFAULT_ACCOUNT_LABEL if account == DEFAULT_ACCOUNT else account


def _first_connection_label(is_browser_auth_supported: bool) -> str:
    """Label for the only choice a never-connected service offers."""
    return "Sign in" if is_browser_auth_supported else "Connect"


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
    is_browser_auth_supported: bool,
) -> tuple[tuple[PermissionAccountChoice, ...], str]:
    """Build the dialog's account radio list and the value to preselect.

    Every account currently signed in to the service is offered, plus the
    always-available "new account" choice.

    ``is_browser_auth_supported`` shapes every hint and
    ``is_account_name_needed``: picking an account with no usable credentials
    either opens a browser sign-in or fills in the dialog's credential form
    (see :meth:`LatchkeyPermissionGrantHandler._establish_manual_credentials`),
    and no hint may promise the wrong one.

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
    # What picking an unusable account leads to. A service with no browser flow
    # asks for credentials in the dialog, so nothing may say "sign in" -- not
    # for a new account, and not for a stored one whose credentials went bad.
    connect_hint = "opens a browser sign-in" if is_browser_auth_supported else "asks you for credentials"
    needs_setup_hint = "needs sign-in" if is_browser_auth_supported else "needs credentials"
    ordered = _sorted_accounts(accounts)
    choices = [
        PermissionAccountChoice(
            value=entry.account,
            label=_account_label(entry.account),
            hint=needs_setup_hint if _needs_account_credential_setup(entry.credential_status) else "",
            is_credential_setup_needed=_needs_account_credential_setup(entry.credential_status),
            is_account_name_needed=False,
        )
        for entry in ordered
    ]
    is_requested_signed_in = any(entry.account == requested_account for entry in ordered)
    if requested_account is not None and not is_requested_signed_in:
        choices.append(
            PermissionAccountChoice(
                value=requested_account,
                label=_account_label(requested_account),
                hint=f"not connected yet — {connect_hint}",
                is_credential_setup_needed=True,
                is_account_name_needed=False,
            )
        )
    choices.append(
        PermissionAccountChoice(
            value=NEW_ACCOUNT_FORM_VALUE,
            # "Sign in" only reads right when it is the single option, and only
            # when signing in is what actually happens. Otherwise this is the
            # trailing "+ Add account" entry of the dialog's account dropdown.
            label=_first_connection_label(is_browser_auth_supported) if len(choices) == 0 else "+ Add account",
            hint=connect_hint,
            is_credential_setup_needed=True,
            # Latchkey names an account from the sign-in; a manual connection
            # cannot, so the user names it -- except for a service's first
            # account, which is latchkey's unnamed default.
            is_account_name_needed=not is_browser_auth_supported and bool(ordered),
        )
    )
    if requested_account is not None:
        selected = requested_account
    elif ordered:
        selected = ordered[0].account
    else:
        selected = NEW_ACCOUNT_FORM_VALUE
    return tuple(choices), selected


def _build_permission_grant_groups(service_info: ServicePermissionInfo) -> tuple[UiPermissionGrantGroup, ...]:
    """Group every permission the dialog offers under the heading it belongs to.

    Shares :func:`group_permissions_by_area` with the workspace Permissions
    tab, so a service's permissions carry the same labels and sit under the
    same headings wherever they are shown. Every row is display-ready: the
    dialog renders labels only, never the schema names it submits.
    """
    return tuple(
        UiPermissionGrantGroup(
            heading=group.heading,
            is_extras=group.is_extras,
            rows=tuple(
                UiPermissionGrantRow(
                    permission=classified.permission,
                    label=classified.label,
                    description=service_info.description_by_permission_name.get(classified.permission, ""),
                    is_wildcard=classified.permission == WILDCARD_PERMISSION_NAME,
                )
                for classified in group.permissions
            ),
        )
        for group in group_permissions_by_area(service_info)
    )


def _parse_manual_credentials_form(raw_values: str | None, account_name: str) -> ManualCredentialSubmission:
    """Parse the credential-form fields of a grant submission.

    ``raw_values`` is the dialog's JSON object of ``<placeholder>`` name to the
    value the user typed; it is absent on every submission that did not come
    from a credential form. Raises :class:`LatchkeyPermissionFlowError` when it
    is present but not such an object.
    """
    if raw_values is None:
        return EMPTY_MANUAL_CREDENTIAL_SUBMISSION
    try:
        payload = json.loads(raw_values)
    except json.JSONDecodeError as e:
        raise LatchkeyPermissionFlowError(f"The submitted credential values are not valid JSON: {e}") from e
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise LatchkeyPermissionFlowError("The submitted credential values must be a JSON object of strings.")
    return ManualCredentialSubmission(
        value_by_parameter_name={str(key): str(value) for key, value in payload.items()},
        account_name=account_name,
    )


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


class LatchkeyPermissionGrantHandler(RequestEventHandler):
    """Top-level orchestrator for predefined (catalog-backed) permission requests.

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
      user can fill in the returned credential form and click Approve
      again (the Approve that carries the values runs the command and,
      if the credentials check out, grants).
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
        manual_credentials: ManualCredentialSubmission,
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

        ``manual_credentials`` carries what the user typed into the credential
        form a previous ``NEEDS_MANUAL_CREDENTIALS`` result asked for; it is
        :data:`EMPTY_MANUAL_CREDENTIAL_SUBMISSION` on every other call.

        The resolve epilogue durably records and indexes the verdict;
        ``message`` is surfaced to both the agent (via ``mngr message``)
        and the dialog UI.
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

        resolved = self._resolve_account_for_grant(service_info, account_choice, manual_credentials)
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
        self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            status=RequestStatus.GRANTED,
            message=granted_message,
        )
        return GrantResult(
            outcome=GrantOutcome.GRANTED,
            message=granted_message,
            manual_credentials=None,
        )

    def _resolve_account_for_grant(
        self,
        service_info: ServicePermissionInfo,
        account_choice: str,
        manual_credentials: ManualCredentialSubmission,
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
          go through the manual credential form when the service has no
          browser flow);
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
        latchkey_service_info = _services_info_or_assumed(self.latchkey, service_info.name)
        accounts_by_name = {entry.account: entry for entry in latchkey_service_info.accounts}
        # A submitted value that names a stored account always *is* that account;
        # only a value matching nothing (the new-account choice, or a stale
        # dialog naming a since-removed account) starts a sign-in.
        chosen = accounts_by_name.get(account_choice)
        if chosen is not None and not _needs_account_credential_setup(chosen.credential_status):
            return chosen.account

        if not latchkey_service_info.is_browser_auth_supported:
            # No browser flow: collect the credentials in the dialog instead
            # and store them ourselves. Until they check out the request stays
            # pending.
            logger.info(
                "Credentials for {} account {!r} reported as unusable and latchkey advertises no "
                "browser flow; collecting them from the permission dialog",
                service_info.name,
                account_choice,
            )
            return self._establish_manual_credentials(
                service_info=service_info,
                latchkey_service_info=latchkey_service_info,
                account_choice=account_choice,
                manual_credentials=manual_credentials,
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
                manual_credentials=None,
            )
        return self._account_after_sign_in(service_info, accounts_before, chosen)

    def _establish_manual_credentials(
        self,
        service_info: ServicePermissionInfo,
        latchkey_service_info: LatchkeyServiceInfo,
        account_choice: str,
        manual_credentials: ManualCredentialSubmission,
    ) -> str | GrantResult:
        """Collect and store credentials for a service latchkey cannot sign in to.

        Returns the account the credentials were stored under once latchkey
        reports them as usable, or the :class:`GrantResult` that re-shows the
        credential form (with whatever went wrong as its message) and leaves
        the request pending.

        The command comes from the service itself (``setCredentialsExample``)
        and is never shown to the user: only its ``<placeholder>`` parameters
        are, as inputs. A service whose example carries none of those -- or is
        not a latchkey invocation at all -- yields a prompt with no parameters,
        which the dialog renders as a plain error.
        """
        form = _build_manual_credentials_form(
            service_name=service_info.name,
            service_display_name=service_info.display_name,
            set_credentials_example=latchkey_service_info.set_credentials_example,
        )
        prompt = form.prompt
        if form.parsed_command is None:
            # Nothing to ask for: the dialog shows the reason and offers no Approve.
            return _manual_credentials_result(message=prompt.message, prompt=prompt)

        # The dialog renders the form as soon as such an account is selected,
        # so an Approve with nothing typed can only be a stale submission.
        if not manual_credentials.value_by_parameter_name:
            return _manual_credentials_result(message=prompt.message, prompt=prompt)

        # A new account can only be named by the user when the service already
        # has one; the first account of a service is latchkey's unnamed default.
        is_account_name_required = account_choice == NEW_ACCOUNT_FORM_VALUE and bool(latchkey_service_info.accounts)
        account = _manual_credentials_account(
            account_choice,
            manual_credentials.account_name,
            is_account_name_required,
        )
        if is_account_name_required and not account:
            return _manual_credentials_result(
                message=f"Enter a name for the new {service_info.display_name} account.",
                prompt=prompt,
            )

        try:
            argv = build_credential_command_argv(
                form.parsed_command,
                manual_credentials.value_by_parameter_name,
                account,
            )
        except CredentialCommandError as e:
            return _manual_credentials_result(
                message=f"The {service_info.display_name} credentials are incomplete: {e}.",
                prompt=prompt,
            )

        is_success, detail = self.latchkey.auth_set_credentials(service_info.name, argv)
        if not is_success:
            # The service itself usually says which value it did not like; its
            # usage lines (and any crash noise) are not worth showing.
            described_failure = describe_credential_command_failure(detail)
            return _manual_credentials_result(
                message=(
                    f"{service_info.display_name} rejected those credentials: {described_failure}"
                    if described_failure
                    else f"Storing the {service_info.display_name} credentials failed."
                ),
                prompt=prompt,
            )
        return self._account_after_manual_credentials(
            service_info=service_info,
            prompt=prompt,
            account=account,
        )

    def _account_after_manual_credentials(
        self,
        service_info: ServicePermissionInfo,
        prompt: UiManualCredentialsPrompt,
        account: str,
    ) -> str | GrantResult:
        """Check that the credentials we just stored are usable before granting.

        ``latchkey auth set`` only validates the *shape* of what it is handed,
        so credentials that are well-formed but wrong -- mistyped, revoked,
        rotated, expired -- are stored happily and only fail when latchkey
        actually calls the service. This re-read does exactly that call (the
        online ``services info``), so an unusable status re-shows the form
        rather than granting a permission the agent could never exercise.

        That call is also how an unreachable service reads, so the message says
        so: latchkey reports a failed check as ``invalid`` either way. The
        credentials stay stored regardless, so a later Approve re-checks them.
        """
        stored_by_account = {
            entry.account: entry for entry in _services_info_or_assumed(self.latchkey, service_info.name).accounts
        }
        stored = stored_by_account.get(account)
        if stored is None or _needs_account_credential_setup(stored.credential_status):
            logger.info(
                "Stored {} credentials for account {!r} are still unusable ({}); re-showing the form",
                service_info.name,
                account,
                stored.credential_status if stored is not None else "not stored",
            )
            return _manual_credentials_result(
                message=(
                    f"{service_info.display_name} did not accept those credentials. They may be mistyped or "
                    f"no longer valid (revoked, rotated or expired), or Minds could not reach "
                    f"{service_info.display_name} to check them."
                ),
                prompt=prompt,
            )
        return account

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
        accounts_after = tuple(
            entry.account for entry in _services_info_or_assumed(self.latchkey, service_info.name).accounts
        )
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
            manual_credentials=None,
        )

    def deny(
        self,
        request_event_id: str,
        agent_id: AgentId,
        display_name: str,
    ) -> str:
        """Record a DENIED verdict and notify the agent. Returns the human-facing message.

        ``display_name`` is the human-readable service name shown in the
        agent-facing message.
        """
        message = _format_denied_message(display_name)
        self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=agent_id,
            status=RequestStatus.DENIED,
            message=message,
        )
        return message

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return REQUEST_TYPE_PREDEFINED

    def kind_label(self) -> str:
        return "permission"

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        """Friendly service name for the inbox list card.

        Falls back to the raw scope schema when no catalog entry matches
        (or when the request is somehow not a predefined one, which
        shouldn't happen given the dispatcher).
        """
        payload = permission_request.payload
        if not isinstance(payload, PredefinedRequestPayload):
            return ""
        info = self.services_catalog.get_by_scope(payload.scope)
        return info.display_name if info is not None else payload.scope

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        payload = permission_request.payload
        if not isinstance(payload, PredefinedRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        service_info = self.services_catalog.get_by_scope(payload.scope)
        if service_info is None:
            return UiUnknownScopeDetail(request_id=permission_request.request_id, scope=payload.scope)

        parsed_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(backend_resolver, parsed_id, fallback=permission_request.agent_id)
        host_id = _resolve_host_id(backend_resolver, parsed_id)

        latchkey_service_info = _services_info_or_assumed(self.latchkey, service_info.name)
        account_choices, selected_account = _build_account_choices(
            latchkey_service_info.accounts,
            payload.account,
            is_browser_auth_supported=latchkey_service_info.is_browser_auth_supported,
        )
        pre_checked = self._initial_checked_permissions(host_id, service_info, payload.permissions, selected_account)
        selected_status_by_account = {
            entry.account: entry.credential_status for entry in latchkey_service_info.accounts
        }
        selected_status = selected_status_by_account.get(selected_account)
        will_open_browser = (
            selected_status is None or _needs_account_credential_setup(selected_status)
        ) and latchkey_service_info.is_browser_auth_supported

        return UiPredefinedPermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            rationale=permission_request.rationale,
            scope=service_info.scope,
            display_name=service_info.display_name,
            service_name=service_info.name,
            permission_groups=_build_permission_grant_groups(service_info),
            checked_permissions=tuple(pre_checked),
            account_choices=tuple(
                UiPermissionAccountChoice(
                    value=choice.value,
                    label=choice.label,
                    hint=choice.hint,
                    is_credential_setup_needed=choice.is_credential_setup_needed,
                    is_account_name_needed=choice.is_account_name_needed,
                )
                for choice in account_choices
            ),
            selected_account_value=selected_account,
            new_account_value=NEW_ACCOUNT_FORM_VALUE,
            wildcard_permission=WILDCARD_PERMISSION_NAME,
            will_open_browser=will_open_browser,
            # The dialog renders the form up front, as soon as an account that
            # needs credentials is selected, so approving is a single click.
            manual_credentials=(
                None
                if latchkey_service_info.is_browser_auth_supported
                else _build_manual_credentials_form(
                    service_name=service_info.name,
                    service_display_name=service_info.display_name,
                    set_credentials_example=latchkey_service_info.set_credentials_example,
                ).prompt
            ),
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        """Drive the grant flow from the dialog form submission."""
        payload = permission_request.payload
        if not isinstance(payload, PredefinedRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        # A host whose canonical permissions file was never materialized must
        # be repaired before the grant, or the approval lands in a file the
        # agent's gateway JWT does not resolve to.
        maybe_recover_host_permissions(self.latchkey, get_state().backend_resolver, permission_request)
        service_info = self.services_catalog.get_by_scope(payload.scope)
        if service_info is None:
            return make_json_error_response(
                f"Scope '{payload.scope}' is not in the gateway catalog",
                status_code=400,
            )

        form = request.form
        granted_permissions = tuple(str(v) for v in form.getlist("permissions"))
        if not granted_permissions:
            return make_json_error_response(
                "At least one permission must be selected to approve the request.",
                status_code=400,
            )
        # The dialog always preselects an account radio, so an absent field
        # means the form was not the one we rendered.
        account_choice = form.get("account")
        if account_choice is None:
            return make_json_error_response(
                "An account must be selected to approve the request.",
                status_code=400,
            )
        try:
            manual_credentials = _parse_manual_credentials_form(
                raw_values=form.get("manual_credentials"),
                account_name=str(form.get("account_name", "")),
            )
        except LatchkeyPermissionFlowError as e:
            return make_json_error_response(str(e), status_code=400)

        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        host_id = _resolve_host_id(backend_resolver, parsed_agent_id)
        if host_id is None:
            return make_json_error_response(
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
                manual_credentials=manual_credentials,
            )
        except LatchkeyPermissionFlowError as e:
            return make_json_error_response(str(e), status_code=400)
        except LatchkeyGatewayClientError as e:
            # The grant flow could not reach the gateway's permissions
            # extension; surface that as a 502 so the dialog can show a
            # meaningful error instead of a generic 500.
            logger.warning("Could not apply latchkey permission grant via gateway: {}", e)
            return make_json_error_response(
                f"Could not apply grant through the latchkey gateway: {e}",
                status_code=502,
            )

        response_payload: dict[str, JsonValue] = {
            "outcome": str(grant_result.outcome),
            "message": grant_result.message,
        }
        if grant_result.manual_credentials is not None:
            response_payload["manual_credentials"] = grant_result.manual_credentials.model_dump(mode="json")
        return make_response(
            content=json.dumps(response_payload),
            media_type="application/json",
        )

    def apply_deny_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        """Drive the deny flow from the dialog form submission."""
        payload = permission_request.payload
        if not isinstance(payload, PredefinedRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        service_info = self.services_catalog.get_by_scope(payload.scope)
        if service_info is None:
            # Even invalid permission requests can be denied.
            display_name = payload.scope
        else:
            display_name = service_info.display_name

        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        self.deny(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            display_name=display_name,
        )
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
        status: RequestStatus,
        message: str,
    ) -> None:
        """Drop the gateway's pending record, then run the shared resolve epilogue.

        The DELETE comes first so a future reconnect of the follow stream
        cannot redeliver an already-resolved request; failure is logged but
        does not abort (the recorded verdict outranks the gateway's stale
        record everywhere pending state is read).
        """
        try:
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE permission request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )
        resolve_request(
            self.mngr_message_sender,
            self.data_dir,
            request_event_id=request_event_id,
            agent_id=agent_id,
            status=status,
            message=message,
        )
