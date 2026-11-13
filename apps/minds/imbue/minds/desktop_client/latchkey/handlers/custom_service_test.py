"""Unit tests for :class:`CustomServiceGrantHandler`."""

import json
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from pydantic import Field
from pydantic import JsonValue
from pydantic import SecretStr
from werkzeug.test import TestResponse

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import CustomServiceLogin
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_CUSTOM_SERVICE
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.custom_service import CustomServiceGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.machine_latchkey import machine_latchkey_for_host
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import load_response_events
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.minds.desktop_client.latchkey.testing import RecordedSetPermissionCall
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import leave_grant_on_this_computer
from imbue.minds.desktop_client.request_handler import UiCustomServicePermissionDetail
from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import create_custom_service_permission_request
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_scope_schema
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.core import read_registered_services
from imbue.mngr_latchkey.custom_services import DomainWarning
from imbue.mngr_latchkey.custom_services import LoginFlow
from imbue.mngr_latchkey.custom_services import Scheme
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.custom_services import registration_credential_header
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.store import permissions_path_for_host


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, chat_id: AgentId, text: str, exec_agent_address: str) -> None:
        self.sent_messages.append((str(chat_id), text))


class _StubLatchkey(Latchkey):
    """A ``Latchkey`` whose sign-in is scripted, so no browser or subprocess runs.

    Registration is left real: it writes ``config.json`` under the test's own
    directory, which is the behaviour these tests most want to observe.
    """

    is_sign_in_successful: bool = Field(default=True)
    accounts_after_sign_in: tuple[str, ...] = Field(default=("me@example.com",))
    sign_in_calls: list[tuple[str, bool]] = Field(
        default_factory=list,
        description="Every ``auth_browser`` call as ``(service_name, is_system_browser)``.",
    )
    system_browser_services: frozenset[str] = Field(
        default=frozenset(),
        description="Services whose ``services_info`` reports the ``systemBrowserLogin`` capability.",
    )
    is_auth_set_successful: bool = Field(default=True)
    auth_set_calls: list[tuple[str, tuple[str, ...]]] = Field(default_factory=list)

    def auth_browser(
        self,
        service_name: str,
        *,
        is_ephemeral: bool = False,
        account: str | None = None,
        is_system_browser: bool = False,
    ) -> tuple[bool, str]:
        self.sign_in_calls.append((service_name, is_system_browser))
        return (True, "") if self.is_sign_in_successful else (False, "the window was closed")

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
        return LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID,
            accounts=tuple(
                ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
                for account in self.accounts_after_sign_in
            ),
            auth_options=frozenset({"browser"}),
            # What latchkey reports for a generic registered service, which is
            # what a custom service with no browser sign-in is.
            set_credentials_example=f'latchkey auth set {service_name} -H "Authorization: Bearer <token>"',
            is_system_browser_login_supported=service_name in self.system_browser_services,
        )

    def auth_set_credentials(self, service_name: str, argv: Sequence[str]) -> tuple[bool, str]:
        self.auth_set_calls.append((service_name, tuple(argv)))
        return (True, "") if self.is_auth_set_successful else (False, "that doesn't look like a valid token")


_HOST_ID = HostId()
_DOMAIN = "api.example.com"
_SERVICE_NAME = "custom_https_api_example_com"


class _Harness:
    """The handler plus every double it was built with, so tests can look at each."""

    def __init__(
        self,
        tmp_path: Path,
        latchkey: _StubLatchkey | None = None,
        carry_grant_to_machine: Callable[[str, str, str], None] = leave_grant_on_this_computer,
        published_metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.metadata_reads: list[str] = []
        documents: Mapping[str, JsonValue] = published_metadata if published_metadata is not None else {}

        def fetch_well_known_json(url: str) -> JsonValue | None:
            self.metadata_reads.append(url)
            return documents.get(url)

        self.sender = _RecordingMessageSender(
            concurrency_group=ConcurrencyGroup(name="recording-message-sender-unused"), sent_messages=[]
        )
        self.gateway: FakeLatchkeyGatewayClient = build_fake_gateway_client()
        self.latchkey = latchkey if latchkey is not None else _StubLatchkey(latchkey_directory=tmp_path / "latchkey")
        self.latchkey.latchkey_directory.mkdir(parents=True, exist_ok=True)
        self.handler = CustomServiceGrantHandler(
            data_dir=tmp_path,
            latchkey=self.latchkey,
            gateway_client=self.gateway,
            mngr_message_sender=self.sender,
            carry_grant_to_machine=carry_grant_to_machine,
            fetch_well_known_json=fetch_well_known_json,
        )

    @property
    def config_path(self) -> Path:
        return self.latchkey.latchkey_directory / "config.json"

    @property
    def permissions_path(self) -> Path:
        return permissions_path_for_host(self.latchkey.plugin_data_dir, _HOST_ID)

    def register(self, domain: str = _DOMAIN, scheme: str = "https", login_url: str | None = None) -> None:
        """Register the service ahead of the request, as an earlier workspace's approval would have."""
        registration = build_custom_service_registration(
            domain,
            scheme=scheme,
            login_url=login_url,
            login_flow=None if login_url is None else LoginFlow.COOKIE_CAPTURE,
            login_flow_params=None if login_url is None else {"cookieKeys": ["session"]},
        )
        self.latchkey.register_custom_service(
            self.latchkey.latchkey_directory, f"custom_{scheme}_{domain.replace('.', '_')}", registration
        )

    def registration(self, service_name: str = _SERVICE_NAME) -> dict[str, object]:
        return json.loads(self.config_path.read_text())["registeredServices"][service_name]


def _make_event(
    agent_id: AgentId,
    domain: str = _DOMAIN,
    has_login: bool = True,
    scheme: Scheme = Scheme.HTTPS,
    header: str | None = None,
    credential_instructions: str | None = None,
) -> StreamedPermissionRequest:
    if not has_login:
        return create_custom_service_permission_request(
            agent_id=str(agent_id),
            domain=domain,
            rationale="needs the widget API",
            scheme=scheme,
            header=header,
            credential_instructions=credential_instructions,
        )
    return create_custom_service_permission_request(
        agent_id=str(agent_id),
        domain=domain,
        rationale="needs the widget API",
        scheme=scheme,
        login=CustomServiceLogin(
            url=f"https://{domain}/login",
            flow=LoginFlow.COOKIE_CAPTURE,
            flow_params={"cookieKeys": ["session"], "cookieUrl": f"https://{domain}/"},
        ),
    )


def _build_authenticated_client(
    tmp_path: Path,
    handler: CustomServiceGrantHandler,
    inbox: StaticPendingRequests,
    backend_resolver: BackendResolverInterface | None = None,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    if backend_resolver is None:
        # Every pending request's agent resolves to one host: the grant needs a
        # host to carry its result to, exactly as the predefined flow does.
        backend_resolver = FixedHostBackendResolver(
            url_by_agent_and_service={},
            fixed_host_id=_HOST_ID,
            known_agent_ids=tuple(AgentId(pending.agent_id) for pending in inbox.pending),
        )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path),
        pending_requests=inbox,
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _detail(
    harness: _Harness, event: StreamedPermissionRequest, backend_resolver: BackendResolverInterface | None = None
) -> UiCustomServicePermissionDetail:
    payload = harness.handler.build_request_detail_payload(
        permission_request=event,
        backend_resolver=(
            backend_resolver if backend_resolver is not None else StaticBackendResolver(url_by_agent_and_service={})
        ),
    )
    if not isinstance(payload, UiCustomServicePermissionDetail):
        pytest.fail(f"expected a custom-service detail payload, got {payload!r}")
    return payload


def _grant(client: FlaskClient, event_id: str, manual_credentials: str | None = None) -> TestResponse:
    data = {} if manual_credentials is None else {"manual_credentials": manual_credentials}
    return client.post(f"/requests/{event_id}/grant", data=data)


def test_handler_claims_custom_service_request_type(tmp_path: Path) -> None:
    assert _Harness(tmp_path).handler.handles_request_type() == REQUEST_TYPE_CUSTOM_SERVICE


def test_grant_registers_the_service_then_writes_the_rule_for_the_connected_account(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    agent_id = AgentId()
    event = _make_event(agent_id)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id)
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"

    # Registered, in latchkey's own config, under the derived name.
    registration = harness.registration()
    assert registration["baseApiUrl"] == "https://api.example.com/"
    assert registration["loginFlow"] == {
        "name": "cookie-capture",
        "params": {"cookieKeys": ["session"], "cookieUrl": "https://api.example.com/"},
    }
    # The bundled service is still there: registering only ever adds.
    assert "claude-ai" in json.loads(harness.config_path.read_text())["registeredServices"]

    # The rule is written for the account the sign-in actually stored, on the
    # agent's per-host file, and carries the scope's own definition: the file
    # must have nothing to resolve elsewhere.
    rule_key = account_scope_key(_SERVICE_NAME, "me@example.com")
    assert harness.gateway.set_calls == (
        RecordedSetPermissionCall(
            permissions_file_path=harness.permissions_path,
            rule_key=rule_key,
            granted_permissions=("any",),
            schemas={
                _SERVICE_NAME: build_custom_service_scope_schema(_DOMAIN, "https"),
                rule_key: build_account_scope_schema(_SERVICE_NAME, "me@example.com"),
            },
        ),
    )
    assert harness.gateway.deleted_request_ids == (event.request_id,)

    assert [entry.status for entry in load_response_events(tmp_path)] == ["GRANTED"]
    assert harness.sender.sent_messages == [
        (
            str(agent_id),
            format_resolution_notice(response.get_json()["message"], event.request_id, RequestStatus.GRANTED),
        )
    ]


def test_a_second_workspace_connects_to_the_existing_service_as_it_is(tmp_path: Path) -> None:
    """The common case: an origin some earlier workspace already connected.

    The second workspace's gateway has no service for it, so this is the only
    request it can make, and it must not be refused. The service is kept as
    registered here -- the request's login is only what the agent guessed --
    and the workspace is signed in through it and granted.
    """
    harness = _Harness(tmp_path)
    harness.register(login_url="https://api.example.com/sso")
    event = _make_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    detail = _detail(harness, event)
    assert detail.is_already_registered is True
    # The dialog names the sign-in that will actually run.
    assert detail.login_url == "https://api.example.com/sso"

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.registration()["loginUrl"] == "https://api.example.com/sso"
    assert harness.latchkey.sign_in_calls == [(_SERVICE_NAME, False)]
    assert [call.rule_key for call in harness.gateway.set_calls] == [
        account_scope_key(_SERVICE_NAME, "me@example.com")
    ]


def test_an_existing_service_without_a_sign_in_asks_for_credentials_whatever_the_request_says(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.register()
    event = _make_event(AgentId(), has_login=True)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _detail(harness, event).login_url is None
    assert _grant(client, event.request_id).get_json()["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert harness.latchkey.sign_in_calls == []
    # Still registered without a sign-in: the request did not add one.
    assert "loginUrl" not in harness.registration()


def test_a_failed_sign_in_leaves_the_request_pending_and_is_never_a_denial(tmp_path: Path) -> None:
    """The contract that makes Approve safe to click again.

    A half-finished approval must not be recorded as the user having said no --
    the agent would then be told it was denied for something the user actually
    tried to grant.
    """
    latchkey = _StubLatchkey(latchkey_directory=tmp_path / "latchkey", is_sign_in_successful=False)
    harness = _Harness(tmp_path, latchkey=latchkey)
    event = _make_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id)
    assert response.status_code == 502
    # Nothing was granted, nothing was recorded, and the agent was not told.
    assert harness.gateway.set_calls == ()
    assert harness.gateway.deleted_request_ids == ()
    assert load_response_events(tmp_path) == []
    assert harness.sender.sent_messages == []


def test_deny_records_the_denial_and_registers_nothing(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    agent_id = AgentId()
    event = _make_event(agent_id)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = client.post(f"/requests/{event.request_id}/deny")
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    assert harness.gateway.deleted_request_ids == (event.request_id,)
    # Registration happens on approve, so a denied request leaves no trace of
    # the service it proposed.
    assert not harness.config_path.is_file()
    assert [entry.status for entry in load_response_events(tmp_path)] == ["DENIED"]
    assert harness.sender.sent_messages == [
        (
            str(agent_id),
            format_resolution_notice(response.get_json()["message"], event.request_id, RequestStatus.DENIED),
        )
    ]


def test_grant_refuses_a_domain_that_is_no_longer_acceptable(tmp_path: Path) -> None:
    # The record was validated when the gateway created it, but it has been on
    # disk since; re-validating means a request that should never have been
    # storable cannot be approved into a registration.
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), domain="latchkey-self.invalid", has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).status_code == 400
    assert not harness.config_path.is_file()
    assert load_response_events(tmp_path) == []


def test_detail_payload_carries_only_the_domain_the_url_and_the_rationale(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    payload = _detail(harness, _make_event(AgentId()))

    assert payload.domain == "api.example.com"
    assert payload.is_already_registered is False
    # ``example.com`` is a reserved name, and the dialog says so.
    assert payload.domain_warning == DomainWarning.UNREACHABLE
    assert payload.base_api_url == "https://api.example.com/"
    assert payload.login_url == "https://api.example.com/login"
    assert payload.rationale == "needs the widget API"
    # Nothing on the payload is agent-authored display text: the label is the
    # domain, so an agent cannot present this connection as something else.
    assert not hasattr(payload, "display_name")


def test_detail_payload_has_no_login_url_without_a_sign_in(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    assert _detail(harness, _make_event(AgentId(), has_login=False)).login_url is None


def test_grant_without_a_login_flow_asks_for_credentials_before_granting(tmp_path: Path) -> None:
    """The first Approve registers and comes back asking, rather than granting nothing usable.

    A registered service with no stored credentials is a hard 400 from latchkey
    on every request, so granting at this point would produce a connection that
    exists, reads as granted, and cannot be used.
    """
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id)
    body = response.get_json()
    assert response.status_code == 200
    assert body["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    # The inputs come from what latchkey says this service takes, which it can
    # only answer once the service is registered.
    assert [parameter["name"] for parameter in body["manual_credentials"]["parameters"]] == ["token"]
    assert harness.config_path.is_file()
    # Nothing granted, nothing recorded, and the agent not told: still pending.
    assert harness.gateway.set_calls == ()
    assert load_response_events(tmp_path) == []
    assert harness.sender.sent_messages == []


def test_grant_stores_the_typed_credentials_then_grants(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id, json.dumps({"token": "secret-token"}))
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "GRANTED"

    service_name, argv = harness.latchkey.auth_set_calls[0]
    assert service_name == _SERVICE_NAME
    # The secret reaches latchkey as an argv element, never a shell string.
    assert "secret-token" in " ".join(argv)
    # Typed credentials are filed under latchkey's default (unnamed) account.
    assert [call.rule_key for call in harness.gateway.set_calls] == [account_scope_key(_SERVICE_NAME, "")]
    assert [entry.status for entry in load_response_events(tmp_path)] == ["GRANTED"]


def test_rejected_credentials_re_ask_rather_than_fail(tmp_path: Path) -> None:
    # A mistyped token is one field away from being right, so the form comes
    # back carrying the reason instead of the request failing outright.
    latchkey = _StubLatchkey(latchkey_directory=tmp_path / "latchkey", is_auth_set_successful=False)
    harness = _Harness(tmp_path, latchkey=latchkey)
    event = _make_event(AgentId(), has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id, json.dumps({"token": "wrong"}))
    body = response.get_json()
    assert response.status_code == 200
    assert body["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert "valid token" in body["message"]
    assert load_response_events(tmp_path) == []
    assert harness.sender.sent_messages == []


def test_the_credential_form_carries_the_agents_instructions_on_every_ask(tmp_path: Path) -> None:
    # Where the key comes from is what the user needs while the form is open, so
    # it rides on the form: on the first ask and again after a rejected attempt.
    latchkey = _StubLatchkey(latchkey_directory=tmp_path / "latchkey", is_auth_set_successful=False)
    harness = _Harness(tmp_path, latchkey=latchkey)
    instructions = "Settings -> Apps -> API token; copy the token that starts with pk_."
    event = _make_event(AgentId(), has_login=False, credential_instructions=instructions)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    first = _grant(client, event.request_id).get_json()
    rejected = _grant(client, event.request_id, json.dumps({"token": "wrong"})).get_json()

    assert first["manual_credentials"]["instructions"] == instructions
    assert "valid token" in rejected["message"]
    assert rejected["manual_credentials"]["instructions"] == instructions


def test_the_credential_form_has_no_instructions_when_the_agent_gave_none(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["manual_credentials"]["instructions"] is None


def test_a_login_flow_never_asks_for_typed_credentials(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=True)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.latchkey.auth_set_calls == []
    assert harness.latchkey.sign_in_calls == [(_SERVICE_NAME, False)]


def test_grant_carries_the_connected_account_to_the_workspace_machine(tmp_path: Path) -> None:
    # What the machine is handed is the account the sign-in resolved plus the
    # policy the grant was written into, as one request -- the same handover a
    # predefined grant makes; the carry reads the registration off the machine
    # store the approval wrote it to.
    carried: list[tuple[str, str, str]] = []

    def record(workspace_agent_id: str, service_name: str, account: str) -> None:
        carried.append((workspace_agent_id, service_name, account))

    harness = _Harness(tmp_path, carry_grant_to_machine=record)
    event = _make_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert carried == [(event.agent_id, _SERVICE_NAME, "me@example.com")]


def test_a_grant_that_cannot_be_queued_for_the_machine_stays_pending(tmp_path: Path) -> None:
    # A machine that does not take the grant means the grant did not happen:
    # no response event, no nudge, and Approve can be retried.
    def refuse(workspace_agent_id: str, service_name: str, account: str) -> None:
        raise MachineOperationError("the machine is unreachable")

    harness = _Harness(tmp_path, carry_grant_to_machine=refuse)
    event = _make_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id)

    assert response.status_code == 502
    assert "unreachable" in response.get_json()["error"]
    # The record stays with the gateway too, so the request is still pending there.
    assert harness.gateway.deleted_request_ids == ()
    assert load_response_events(tmp_path) == []
    assert harness.sender.sent_messages == []


def test_grant_needs_the_agent_to_resolve_to_a_host(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    event = _make_event(AgentId())
    client = _build_authenticated_client(
        tmp_path,
        harness.handler,
        StaticPendingRequests(pending=(event,)),
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    response = _grant(client, event.request_id)

    assert response.status_code == 503
    # Nothing was created for a grant that could not be placed.
    assert harness.latchkey.sign_in_calls == []
    assert not harness.config_path.is_file()


def test_an_http_service_is_shown_and_registered_as_http(tmp_path: Path) -> None:
    # The scheme is the one thing the agent adds to the domain, and it is the
    # difference between credentials sent encrypted and in the clear -- so the
    # dialog shows the origin, and the registration is reached over it.
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), domain="intranet.acme-widgets.com", has_login=False, scheme=Scheme.HTTP)

    detail = _detail(harness, event)
    assert detail.base_api_url == "http://intranet.acme-widgets.com/"
    # A private network's own subdomain is an ordinary name: nothing to warn about.
    assert detail.domain_warning is None

    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))
    assert _grant(client, event.request_id).get_json()["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert (
        harness.registration("custom_http_intranet_acme-widgets_com")["baseApiUrl"]
        == "http://intranet.acme-widgets.com/"
    )


def test_a_named_header_is_registered_shown_and_used_for_the_typed_token(tmp_path: Path) -> None:
    """A service whose key goes in its own header: the registration remembers it, the
    dialog shows it, and the typed token is stored under that header rather than as a
    bearer, whatever latchkey's generic example says."""
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=False, header="X-Api-Key: {token}")
    assert _detail(harness, event).credential_header == "X-Api-Key: {token}"
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    first = _grant(client, event.request_id)
    assert first.get_json()["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert harness.registration()["mindsCredentialHeader"] == "X-Api-Key: {token}"
    assert [parameter["name"] for parameter in first.get_json()["manual_credentials"]["parameters"]] == ["token"]

    second = _grant(client, event.request_id, json.dumps({"token": "k-42"}))
    assert second.get_json()["outcome"] == "GRANTED"
    _, argv = harness.latchkey.auth_set_calls[0]
    assert argv[-2:] == ("-H", "X-Api-Key: k-42")


def test_a_second_workspace_sees_and_uses_the_existing_services_header(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.latchkey.register_custom_service(
        harness.latchkey.latchkey_directory,
        _SERVICE_NAME,
        build_custom_service_registration(_DOMAIN, "https", credential_header="X-Api-Key: {token}"),
    )
    # The request guesses a bearer header (none named); the registration's wins.
    event = _make_event(AgentId(), has_login=False)
    assert _detail(harness, event).credential_header == "X-Api-Key: {token}"
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))
    _grant(client, event.request_id)
    response = _grant(client, event.request_id, json.dumps({"token": "k-7"}))
    assert response.get_json()["outcome"] == "GRANTED"
    assert harness.latchkey.auth_set_calls[0][1][-1] == "X-Api-Key: k-7"


def test_the_bearer_default_is_shown_and_a_login_flow_shows_no_header(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    assert (
        _detail(harness, _make_event(AgentId(), has_login=False)).credential_header == "Authorization: Bearer {token}"
    )
    assert _detail(harness, _make_event(AgentId(), has_login=True)).credential_header is None


# Trello's MCP server delegates to Atlassian's authorization server: a layout
# only the server's own metadata can vouch for.
_MCP_DOMAIN = "mcp.trello.com"
_MCP_SERVICE_NAME = "custom_https_mcp_trello_com"
_MCP_ENDPOINT = "https://mcp.trello.com/mcp"
_MCP_AUTHORIZE = "https://auth.atlassian.com/authorize"
_MCP_TOKEN = "https://auth.atlassian.com/oauth/token"
_MCP_REGISTER = "https://auth.atlassian.com/dcr/register"
_MCP_PUBLISHED: Mapping[str, JsonValue] = {
    "https://mcp.trello.com/.well-known/oauth-protected-resource/mcp": {
        "authorization_servers": ["https://auth.atlassian.com"]
    },
    "https://auth.atlassian.com/.well-known/oauth-authorization-server": {
        "authorization_endpoint": _MCP_AUTHORIZE,
        "token_endpoint": _MCP_TOKEN,
        "registration_endpoint": _MCP_REGISTER,
    },
}


def _make_mcp_event(
    agent_id: AgentId,
    domain: str = _MCP_DOMAIN,
    mcp_endpoint: str = _MCP_ENDPOINT,
    login_url: str = _MCP_AUTHORIZE,
    token_endpoint: str = _MCP_TOKEN,
    registration_endpoint: str = _MCP_REGISTER,
) -> StreamedPermissionRequest:
    return create_custom_service_permission_request(
        agent_id=str(agent_id),
        domain=domain,
        rationale="needs the boards",
        login=CustomServiceLogin(
            url=login_url,
            flow=LoginFlow.MCP,
            flow_params={
                "mcpEndpoint": mcp_endpoint,
                "tokenEndpoint": token_endpoint,
                "registrationEndpoint": registration_endpoint,
            },
        ),
    )


def test_detail_payload_reports_the_system_browser_for_an_mcp_sign_in_before_registration(tmp_path: Path) -> None:
    """The service does not exist yet, so the browser is read off the request's login flow."""
    harness = _Harness(tmp_path, published_metadata=_MCP_PUBLISHED)

    assert _detail(harness, _make_mcp_event(AgentId())).is_system_browser_login_supported is True
    assert _detail(harness, _make_event(AgentId(), has_login=True)).is_system_browser_login_supported is False


def test_detail_payload_asks_latchkey_about_a_registered_services_browser(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.register(login_url="https://api.example.com/sso")
    event = _make_event(AgentId())

    assert _detail(harness, event).is_system_browser_login_supported is False
    harness.latchkey.system_browser_services = frozenset({_SERVICE_NAME})
    assert _detail(harness, event).is_system_browser_login_supported is True


def test_a_sign_in_runs_in_the_system_browser_when_latchkey_reports_the_capability(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, published_metadata=_MCP_PUBLISHED)
    harness.latchkey.system_browser_services = frozenset({_MCP_SERVICE_NAME})
    event = _make_mcp_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.latchkey.sign_in_calls == [(_MCP_SERVICE_NAME, True)]


def test_an_mcp_sign_in_the_server_vouches_for_is_registered_and_signed_in(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, published_metadata=_MCP_PUBLISHED)
    event = _make_mcp_event(AgentId())
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.metadata_reads == list(_MCP_PUBLISHED)
    assert harness.registration(_MCP_SERVICE_NAME)["loginFlow"] == {
        "name": "mcp",
        "params": {"mcpEndpoint": _MCP_ENDPOINT, "tokenEndpoint": _MCP_TOKEN, "registrationEndpoint": _MCP_REGISTER},
    }
    assert harness.latchkey.sign_in_calls == [(_MCP_SERVICE_NAME, False)]


def test_an_mcp_sign_in_the_server_contradicts_is_denied_with_what_it_publishes(tmp_path: Path) -> None:
    # The real sign-in page with a token endpoint of the agent's own: the
    # authorization code would go to the agent. Nothing the user can do makes
    # the request right, so it is denied and the agent is told what to ask
    # for instead.
    harness = _Harness(tmp_path, published_metadata=_MCP_PUBLISHED)
    agent_id = AgentId()
    event = _make_mcp_event(agent_id, token_endpoint="https://evil.test/oauth/token")
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    response = _grant(client, event.request_id)
    assert response.status_code == 200
    assert response.get_json()["outcome"] == "DENIED"
    message = response.get_json()["message"]
    assert "could not be trusted" in message
    assert _MCP_TOKEN in message
    assert not harness.config_path.is_file()
    assert harness.latchkey.sign_in_calls == []
    assert harness.gateway.deleted_request_ids == (event.request_id,)
    assert [entry.status for entry in load_response_events(tmp_path)] == ["DENIED"]
    assert harness.sender.sent_messages == [
        (str(agent_id), format_resolution_notice(message, event.request_id, RequestStatus.DENIED))
    ]


def test_an_mcp_server_publishing_nothing_gets_the_domain_rule(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    on_domain = _make_mcp_event(
        AgentId(),
        domain="mcp.otter.ai",
        mcp_endpoint="https://mcp.otter.ai/mcp",
        login_url="https://otter.ai/oauth2/authorize",
        token_endpoint="https://otter.ai/oauth/token",
        registration_endpoint="https://otter.ai/oauth/register",
    )
    off_domain = _make_mcp_event(AgentId())
    client = _build_authenticated_client(
        tmp_path, harness.handler, StaticPendingRequests(pending=(on_domain, off_domain))
    )

    assert _grant(client, on_domain.request_id).get_json()["outcome"] == "GRANTED"
    denied = _grant(client, off_domain.request_id).get_json()
    assert denied["outcome"] == "DENIED"
    assert "publishes no OAuth metadata" in denied["message"]
    assert "login_url must be on mcp.trello.com" in denied["message"]
    assert harness.latchkey.sign_in_calls == [("custom_https_mcp_otter_ai", False)]


def test_an_existing_registration_reads_no_metadata(tmp_path: Path) -> None:
    # A second workspace's request connects to the service as registered; its
    # own sign-in is never used, so there is nothing to verify.
    harness = _Harness(tmp_path)
    harness.register(_MCP_DOMAIN, login_url="https://mcp.trello.com/login")
    event = _make_mcp_event(AgentId(), token_endpoint="https://evil.test/oauth/token")
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.metadata_reads == []


def _store_registration(store_dir: Path, service_name: str) -> dict[str, Any]:
    return json.loads((store_dir / "config.json").read_text())["registeredServices"][service_name]


def _give_the_host_a_machine_of_its_own(harness: _Harness) -> Path:
    """Record a key for the host, which is what makes it a remote machine with a store of its own, and return the store."""
    store_machine_encryption_key(harness.latchkey.plugin_data_dir, _HOST_ID, SecretStr("machine-key-7d2e"))
    return machine_latchkey_for_host(harness.latchkey, _HOST_ID).latchkey_directory


def test_a_remote_workspaces_service_is_registered_in_its_machines_store(tmp_path: Path) -> None:
    """A custom service belongs to the machine whose agents use it, so it is registered in that machine's store.

    The desktop's own config -- the services on this computer's machine -- is
    untouched, and the desktop gateway's catalog, which validates the requests
    the machine's gateway forwards here, names the new service all the same.
    """
    harness = _Harness(tmp_path)
    store_dir = _give_the_host_a_machine_of_its_own(harness)
    event = _make_event(AgentId(), has_login=False)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "NEEDS_MANUAL_CREDENTIALS"

    assert _store_registration(store_dir, _SERVICE_NAME)["baseApiUrl"] == "https://api.example.com/"
    assert _SERVICE_NAME not in read_registered_services(harness.latchkey.latchkey_directory)
    materialized = json.loads((harness.latchkey.latchkey_directory / "extensions" / "services.json").read_text())
    assert _SERVICE_NAME in materialized


def test_a_service_on_this_computers_machine_does_not_count_as_registered_for_a_remote_one(tmp_path: Path) -> None:
    """The same origin on two machines is two services: the remote one is created as its own request describes it."""
    harness = _Harness(tmp_path)
    harness.register()
    store_dir = _give_the_host_a_machine_of_its_own(harness)
    agent_id = AgentId()
    event = _make_event(agent_id, has_login=False, header="X-Api-Key: {token}")
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    resolver = FixedHostBackendResolver(
        url_by_agent_and_service={}, fixed_host_id=_HOST_ID, known_agent_ids=(agent_id,)
    )
    detail = _detail(harness, event, resolver)
    assert detail.is_already_registered is False
    assert detail.credential_header == "X-Api-Key: {token}"

    assert _grant(client, event.request_id).get_json()["outcome"] == "NEEDS_MANUAL_CREDENTIALS"
    assert registration_credential_header(_store_registration(store_dir, _SERVICE_NAME)) == "X-Api-Key: {token}"
    assert "mindsCredentialHeader" not in harness.registration()
