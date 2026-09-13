"""Custom-service grant/deny flow (wire ``request_type == "custom-service"``).

A sibling handler under :mod:`imbue.minds.desktop_client.latchkey.handlers`. It
owns the flow for a request to reach a domain the asking workspace's latchkey
has no service for: the agent names the origin, and one Approve *creates* the
connection if this computer does not have it yet and grants the asking
workspace access to it.

"If this computer does not have it yet" is the common case, not a corner: a
second workspace wanting an origin some earlier one connected sees exactly what
the first did (its own gateway has no service for it), so this is the only
request it can make. The service is then kept as it is registered here -- the
request's login is only what the agent guessed -- and approving connects the
workspace to it.

Approve does three things, in this order:

1. **Register**, by merging the service into latchkey's own ``config.json``,
   unless it is already there. This has to happen first: a sign-in is executed
   by the gateway against its own registry, so latchkey cannot log in to a
   service it has not been told about. (It reads registrations per request, so
   no restart is involved -- see
   :data:`imbue.mngr_latchkey.core.LATCHKEY_MIN_VERSION`.)
2. **Connect an account**, either by the browser sign-in the request described
   (one of latchkey's generic login flows)
   or -- when the request carries no login flow -- by running the service's own
   ``latchkey auth set`` with values the user typed in. "No login flow" does not
   mean "no credentials": latchkey refuses a request to a registered service
   with nothing stored, so something has to be established either way.

   The credential form cannot be part of the dialog's first render the way it is
   for a catalog service, because the service does not exist yet and so has no
   ``setCredentialsExample`` to build one from. Registration is what makes it
   answerable, so the first Approve registers and then comes back asking for the
   credentials (``NEEDS_MANUAL_CREDENTIALS``), and the second one supplies them.
   That is the same two-step the ``predefined`` flow already falls into when a
   service turns out to need typed credentials.
3. **Grant**, by writing the account-scoped rule into the asking agent's
   per-host ``latchkey_permissions.json`` through ``POST /permissions/rules``
   and then dropping the gateway's pending record -- the same write
   :mod:`.predefined` makes. The one difference is that the scope's own
   definition travels with the rule: a custom scope is not a detent builtin,
   and a rule referring to a definition the file does not have fails detent's
   whole check for that host.

A failure at any step leaves the request **pending** with no response event, so
the user can fix the problem and click Approve again. That is the same contract
:mod:`.predefined` has, and it matters here because the flow has more ways to
stop halfway.

The account handling is much simpler than :mod:`.predefined`'s. That handler has
to reconcile a dialog's account picker against whatever latchkey has stored; a
service being *created* has no accounts at all, so there is nothing to pick
between and the only question is how to establish the first one. A retry after
a failed sign-in finds the service registered and goes straight to the sign-in.
"""

import json
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.latchkey.gateway_client import CustomServiceRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_CUSTOM_SERVICE
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.recovery import maybe_recover_host_permissions
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.machine_latchkey import machine_latchkey_for_host
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiCustomServicePermissionDetail
from imbue.minds.desktop_client.request_handler import UiManualCredentialsPrompt
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import read_registered_services
from imbue.mngr_latchkey.credential_commands import CredentialCommandError
from imbue.mngr_latchkey.credential_commands import ParsedCredentialCommand
from imbue.mngr_latchkey.credential_commands import build_credential_command_argv
from imbue.mngr_latchkey.credential_commands import describe_credential_command_failure
from imbue.mngr_latchkey.credential_commands import fallback_set_credentials_example
from imbue.mngr_latchkey.credential_commands import parse_credential_command_example
from imbue.mngr_latchkey.custom_services import CustomServiceError
from imbue.mngr_latchkey.custom_services import base_api_url_for_domain
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.custom_services import custom_service_label
from imbue.mngr_latchkey.custom_services import custom_service_name
from imbue.mngr_latchkey.custom_services import domain_warning
from imbue.mngr_latchkey.custom_services import registration_login_url
from imbue.mngr_latchkey.custom_services import validate_domain
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import permissions_path_for_host

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "new connection"


def _format_granted_message(base_api_url: str) -> str:
    return (
        f"Your request to store credentials for {base_api_url} was granted. They are stored securely and "
        f"this machine can use them; requests to {base_api_url} will have them attached."
    )


def _format_denied_message(base_api_url: str) -> str:
    return f"Your request to store credentials for {base_api_url} was denied. Nothing was stored."


def _sign_in_url(existing: Mapping[str, JsonValue] | None, payload: CustomServiceRequestPayload) -> str | None:
    """Where the browser sign-in goes, or ``None`` when credentials have to be typed.

    A service this computer already has keeps its registration, so its sign-in
    is what runs and what the dialog names; the request's ``login`` counts only
    for a service being created.
    """
    if existing is not None:
        return registration_login_url(existing)
    return None if payload.login is None else payload.login.url


def _drop_gateway_record(gateway_client: LatchkeyGatewayClient, request_event_id: str) -> None:
    """Delete the gateway's pending record; a failure is logged, not fatal.

    The recorded verdict outranks the gateway's stale record everywhere pending
    state is read, and the next restart cleans up.
    """
    try:
        gateway_client.delete_permission_request(request_event_id)
    except LatchkeyGatewayClientError as e:
        logger.warning(
            "Could not DELETE custom-service request {} from gateway; relying on next-restart cleanup: {}",
            request_event_id,
            e,
        )


def _parse_typed_credentials(raw_values: str | None) -> dict[str, str]:
    """Read the credential form's values out of the submitted form.

    The dialog sends them as one JSON object so the field names can be whatever
    the service's own command asks for. Anything unparseable reads as "nothing
    typed yet", which re-shows the form rather than failing the approval.
    """
    if not raw_values:
        return {}
    try:
        parsed = json.loads(raw_values)
    except json.JSONDecodeError as e:
        # Only our own dialog posts this field, so malformed JSON means a bug
        # or a hand-made request rather than a user mistake -- say so rather
        # than swallowing it, but still re-show the form so the flow recovers.
        logger.warning("Ignoring unparseable credential form values: {}", e)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Ignoring credential form values that are not a JSON object")
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def _needs_credentials(prompt: UiManualCredentialsPrompt, message: str | None = None) -> Response:
    """Leave the request pending and (re-)show the credential form.

    Deliberately not an error outcome: nothing has gone wrong, the flow simply
    needs input it could not ask for before the service existed. ``message``
    replaces the prompt's instruction when an attempt was rejected, so the
    dialog explains the failure instead of repeating the instruction.
    """
    shown = prompt if message is None else prompt.model_copy_update(to_update(prompt.field_ref().message, message))
    return make_response(
        content=json.dumps(
            {
                "outcome": "NEEDS_MANUAL_CREDENTIALS",
                "message": shown.message,
                "manual_credentials": shown.model_dump(mode="json"),
            }
        ),
        media_type="application/json",
    )


class CustomServiceGrantHandler(RequestEventHandler):
    """Handler for custom-service permission requests."""

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ``~/.minds``).")
    latchkey: Latchkey = Field(
        description=(
            "This computer's latchkey. Registration goes through it: ``config.json`` is shared into every "
            "machine store by symlink, so writing it through a store would replace the link with a copy. "
            "The sign-in that connects the first account runs against the machine's own store instead "
            "(see :func:`machine_latchkey_for_host`)."
        ),
    )
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to call ``POST /permissions/rules`` on the gateway's bundled ``permissions`` "
            "extension and ``DELETE /permission-requests/<id>`` on its ``permission-requests`` extension."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Sends ``mngr message`` nudges to the waiting agent on resolution.",
    )
    carry_grant_to_machine: Callable[[str, str, str], None] = Field(
        description=(
            "Carries the granted account (workspace agent id, service name, account) and the freshly-edited "
            "per-host policy to the asking agent's own machine, returning once the machine has taken them -- "
            "the same handover the predefined handler makes. The registration the machine also needs travels "
            "in the same script, ahead of the credential, as this computer's snapshot of the machine's "
            "config -- its gateway cannot route a request to a service its config does not name. A no-op "
            "for a workspace whose agents run on this computer."
        ),
    )

    def handles_request_type(self) -> str:
        return REQUEST_TYPE_CUSTOM_SERVICE

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        if not isinstance(permission_request.payload, CustomServiceRequestPayload):
            return ""
        # The origin, like everywhere else this service is named.
        return custom_service_label(permission_request.payload.domain, permission_request.payload.scheme)

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        payload = permission_request.payload
        if not isinstance(payload, CustomServiceRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        parsed_agent_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(
            backend_resolver, parsed_agent_id, fallback=permission_request.agent_id
        )
        existing = self._existing_registration(custom_service_name(payload.domain, payload.scheme))
        return UiCustomServicePermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            domain=payload.domain,
            is_already_registered=existing is not None,
            domain_warning=domain_warning(payload.domain),
            base_api_url=base_api_url_for_domain(payload.domain, payload.scheme),
            login_url=_sign_in_url(existing, payload),
            rationale=permission_request.rationale,
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        payload = permission_request.payload
        if not isinstance(payload, CustomServiceRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        # A host whose canonical permissions file was never materialized must
        # be repaired before the grant, or the approval lands in a file the
        # agent's gateway JWT does not resolve to.
        maybe_recover_host_permissions(self.latchkey, get_state().backend_resolver, permission_request)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        host_id = resolve_workspace_host_id(get_state().backend_resolver, str(parsed_agent_id))
        if host_id is None:
            return make_json_error_response(
                f"Could not resolve host for agent {parsed_agent_id}; cannot apply grant.", status_code=503
            )

        # Re-validate the domain rather than trust the persisted record. This
        # checks the grammar only; whether some other scope covers the domain
        # is not checked anywhere, and a duplicate is inert rather than
        # dangerous (detent stops at the first matching scope).
        try:
            domain = validate_domain(payload.domain)
        except CustomServiceError as e:
            logger.warning("Refusing custom-service request {}: {}", request_event_id, e)
            return make_json_error_response(f"This connection cannot be created: {e}", status_code=400)
        service_name = custom_service_name(domain, payload.scheme)

        existing = self._existing_registration(service_name)
        if existing is None:
            try:
                self._register_service(service_name, payload)
            except LatchkeyError as e:
                logger.warning("Could not register custom service {}: {}", service_name, e)
                return make_json_error_response(f"Could not create the connection to {domain}: {e}", status_code=500)

        # Credentials belong to the machine the agent runs on, so the sign-in
        # (and everything it reads back) happens against that machine's own
        # store -- never the desktop's, unless the agent runs here. The
        # registration above is the one thing that stays with the desktop.
        try:
            machine_latchkey = machine_latchkey_for_host(self.latchkey, host_id)
        except PermissionOverviewError as e:
            logger.warning("Could not open the machine store for host {}: {}", host_id, e)
            return make_json_error_response(f"Could not create the connection to {domain}: {e}", status_code=500)

        if _sign_in_url(existing, payload) is not None:
            account = self._sign_in_with_browser(machine_latchkey, service_name)
            if account is None:
                # The sign-in did not complete. The request stays pending with
                # no response event, so Approve can simply be clicked again --
                # and is never recorded as a denial.
                return make_json_error_response(
                    f"Could not sign in to {domain}, so the connection was not granted. You can try approving again.",
                    status_code=502,
                )
        else:
            # No browser sign-in, so the credentials have to be typed. They can
            # only be asked for once the service is registered (that is what
            # gives it a ``setCredentialsExample``), so the first Approve lands
            # here with nothing filled in and comes back with the form.
            typed_values = _parse_typed_credentials(request.form.get("manual_credentials"))
            prompt = self._credential_prompt(machine_latchkey, service_name, domain)
            if not prompt.parameters:
                return _needs_credentials(prompt)
            if not typed_values or any(not typed_values.get(p.name, "").strip() for p in prompt.parameters):
                return _needs_credentials(prompt)
            failure = self._store_typed_credentials(machine_latchkey, service_name, typed_values, prompt)
            if failure is not None:
                # The values were rejected. Keep the form up with the reason in
                # place of its instruction, exactly as the predefined flow does.
                return _needs_credentials(prompt, message=failure)
            account = DEFAULT_ACCOUNT

        try:
            self._write_grant(host_id, service_name, domain, payload.scheme, account)
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not write the grant for custom-service request {}: {}", request_event_id, e)
            return make_json_error_response(
                f"Connected to {domain}, but the permission could not be written through the latchkey gateway: {e}",
                status_code=502,
            )

        # A remote workspace's machine is where both halves of the grant count:
        # its gateway injects the credentials its own store holds and checks the
        # agent's next request against its own policy copy. They go over as one
        # script, config first, then credential, then policy, and the
        # grant is reported only once the machine has taken all of it. A
        # machine that will not leaves the request pending for a retry.
        try:
            self.carry_grant_to_machine(str(parsed_agent_id), service_name, account)
        except MachineOperationError as e:
            logger.warning("The workspace's machine did not take the custom-service grant for host {}: {}", host_id, e)
            return make_json_error_response(
                f"Connected to {domain} here, but the workspace's machine did not take the change: {e}. "
                "You can try approving again.",
                status_code=502,
            )

        # The record goes before the verdict is written, so a reconnect of the
        # follow stream cannot redeliver an already-resolved request.
        _drop_gateway_record(self.gateway_client, request_event_id)
        message = _format_granted_message(base_api_url_for_domain(domain, payload.scheme))
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
        payload = permission_request.payload
        if not isinstance(payload, CustomServiceRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        # Nothing to undo: registration happens on approve, so a denied request
        # leaves no trace of the proposed service.
        _drop_gateway_record(self.gateway_client, request_event_id)

        message = _format_denied_message(base_api_url_for_domain(payload.domain, payload.scheme))
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

    def _existing_registration(self, service_name: str) -> Mapping[str, JsonValue] | None:
        """This computer's registration of the service, or ``None`` if it has none."""
        registration = read_registered_services(self.latchkey.latchkey_directory).get(service_name)
        return registration if isinstance(registration, dict) else None

    def _register_service(self, service_name: str, payload: CustomServiceRequestPayload) -> None:
        """Merge the service into latchkey's ``config.json``.

        The merge preserves every registration it does not own, which is what
        lets `config.json` be the only place a custom service lives.
        """
        login = payload.login
        registration = build_custom_service_registration(
            payload.domain,
            scheme=payload.scheme,
            login_url=None if login is None else login.url,
            login_flow=None if login is None else login.flow,
            login_flow_params=None if login is None else login.flow_params,
        )
        self.latchkey.register_custom_service(service_name, registration)

    def _write_grant(self, host_id: HostId, service_name: str, domain: str, scheme: str, account: str) -> None:
        """Write the account-scoped rule, and the scope it refers to, into the agent's per-host policy.

        The permission is detent's catch-all ``any``: the scope is already
        pinned to one origin, and a custom service has no sub-permissions to
        choose between.
        """
        rule_key, permissions, schemas = build_account_grant(
            service_name, account, (WILDCARD_PERMISSION_NAME,), build_custom_service_scope_schema(domain, scheme)
        )
        self.gateway_client.set_permission_rule(
            permissions_file_path=permissions_path_for_host(self.latchkey.plugin_data_dir, host_id),
            rule_key=rule_key,
            granted_permissions=permissions,
            schemas=schemas,
        )

    def _sign_in_with_browser(self, machine_latchkey: Latchkey, service_name: str) -> str | None:
        """Run the service's browser sign-in and read back which account it stored, or ``None`` on failure.

        Latchkey files the credentials under whoever actually logged in, so the
        account is read back rather than assumed.
        """
        is_success, detail = machine_latchkey.auth_browser(service_name)
        if not is_success:
            logger.warning("Browser sign-in to {} did not complete: {}", service_name, detail)
            return None
        info = machine_latchkey.services_info(service_name)
        accounts = tuple(entry.account for entry in info.accounts) if info is not None else ()
        if len(accounts) == 1:
            return accounts[0]
        if not accounts:
            logger.warning("Sign-in to {} reported success but stored no account", service_name)
            return None
        logger.warning("Sign-in to {} stored more than one account ({}); not granting", service_name, accounts)
        return None

    def _credential_prompt(
        self, machine_latchkey: Latchkey, service_name: str, domain: str
    ) -> UiManualCredentialsPrompt:
        """The form asking for the credentials a service with no browser sign-in needs.

        Built from the service's own ``setCredentialsExample`` now that it is
        registered, so the inputs are whatever latchkey says this service takes.
        A generic registered service reports a bearer-token command, which
        becomes a single labelled input; the command itself is never shown,
        since it is an implementation detail the user should not have to know.
        """
        info = machine_latchkey.services_info(service_name, is_offline=True)
        example = (info.set_credentials_example if info is not None else None) or fallback_set_credentials_example(
            service_name
        )
        try:
            parsed = parse_credential_command_example(example)
        except CredentialCommandError as e:
            logger.warning("Could not build a credential form for {}: {}", service_name, e)
            return UiManualCredentialsPrompt(
                parameters=(),
                message=(
                    f"{domain} has no browser sign-in, and Minds cannot work out which credentials to "
                    "ask for. It has to be connected some other way."
                ),
            )
        return UiManualCredentialsPrompt(
            parameters=parsed.parameters,
            message=(
                f"{domain} has no browser sign-in, so Minds needs its credentials. Get them from the "
                "provider and fill them in -- Approve stores them and creates the connection."
            ),
        )

    def _store_typed_credentials(
        self,
        machine_latchkey: Latchkey,
        service_name: str,
        values: Mapping[str, str],
        prompt: UiManualCredentialsPrompt,
    ) -> str | None:
        """Run the service's own ``auth set`` with the typed values. Returns the reason it failed, or ``None``.

        The filled-in command carries the user's secrets, so it is passed as an
        argv list (never a shell string) and is never logged.
        """
        info = machine_latchkey.services_info(service_name, is_offline=True)
        example = (info.set_credentials_example if info is not None else None) or fallback_set_credentials_example(
            service_name
        )
        try:
            parsed: ParsedCredentialCommand = parse_credential_command_example(example)
            argv = build_credential_command_argv(parsed, dict(values), DEFAULT_ACCOUNT)
        except CredentialCommandError as e:
            logger.warning("Could not build the credential command for {}: {}", service_name, e)
            return str(e)
        is_success, detail = machine_latchkey.auth_set_credentials(service_name, argv)
        if not is_success:
            return describe_credential_command_failure(detail)
        del prompt
        return None
