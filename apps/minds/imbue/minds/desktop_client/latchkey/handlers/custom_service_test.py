"""Unit tests for :class:`CustomServiceGrantHandler`."""

import json
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from pydantic import Field
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
from imbue.mngr_latchkey.custom_services import DomainWarning
from imbue.mngr_latchkey.custom_services import LoginFlow
from imbue.mngr_latchkey.custom_services import Scheme
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.store import permissions_path_for_host


class _RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    concurrency_group: ConcurrencyGroup | None = None
    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, agent_id: AgentId, text: str) -> None:
        self.sent_messages.append((str(agent_id), text))


class _StubLatchkey(Latchkey):
    """A ``Latchkey`` whose sign-in is scripted, so no browser or subprocess runs.

    Registration is left real: it writes ``config.json`` under the test's own
    directory, which is the behaviour these tests most want to observe.
    """

    is_sign_in_successful: bool = Field(default=True)
    accounts_after_sign_in: tuple[str, ...] = Field(default=("me@example.com",))
    sign_in_calls: list[str] = Field(default_factory=list)
    is_auth_set_successful: bool = Field(default=True)
    auth_set_calls: list[tuple[str, tuple[str, ...]]] = Field(default_factory=list)

    def auth_browser(
        self, service_name: str, *, is_ephemeral: bool = False, account: str | None = None
    ) -> tuple[bool, str]:
        self.sign_in_calls.append(service_name)
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
    ) -> None:
        self.sender = _RecordingMessageSender(sent_messages=[])
        self.gateway: FakeLatchkeyGatewayClient = build_fake_gateway_client()
        self.latchkey = latchkey if latchkey is not None else _StubLatchkey(latchkey_directory=tmp_path / "latchkey")
        self.latchkey.latchkey_directory.mkdir(parents=True, exist_ok=True)
        self.handler = CustomServiceGrantHandler(
            data_dir=tmp_path,
            latchkey=self.latchkey,
            gateway_client=self.gateway,
            mngr_message_sender=self.sender,
            carry_grant_to_machine=carry_grant_to_machine,
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
        self.latchkey.register_custom_service(f"custom_{scheme}_{domain.replace('.', '_')}", registration)

    def registration(self, service_name: str = _SERVICE_NAME) -> dict[str, object]:
        return json.loads(self.config_path.read_text())["registeredServices"][service_name]


def _make_event(
    agent_id: AgentId,
    domain: str = _DOMAIN,
    has_login: bool = True,
    scheme: Scheme = Scheme.HTTPS,
) -> StreamedPermissionRequest:
    if not has_login:
        return create_custom_service_permission_request(
            agent_id=str(agent_id), domain=domain, rationale="needs the widget API", scheme=scheme
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


def _detail(harness: _Harness, event: StreamedPermissionRequest) -> UiCustomServicePermissionDetail:
    payload = harness.handler.build_request_detail_payload(
        permission_request=event, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
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
    assert harness.latchkey.sign_in_calls == [_SERVICE_NAME]
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


def test_a_login_flow_never_asks_for_typed_credentials(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    event = _make_event(AgentId(), has_login=True)
    client = _build_authenticated_client(tmp_path, harness.handler, StaticPendingRequests(pending=(event,)))

    assert _grant(client, event.request_id).get_json()["outcome"] == "GRANTED"
    assert harness.latchkey.auth_set_calls == []
    assert harness.latchkey.sign_in_calls == [_SERVICE_NAME]


def test_grant_carries_the_connected_account_to_the_workspace_machine(tmp_path: Path) -> None:
    # The registration stays on this computer (config.json is shared into every
    # machine store by link); what the machine is handed is the account the
    # sign-in resolved plus the policy the grant was written into, as one
    # request -- the same handover a predefined grant makes.
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
