import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.account_choices import NEW_ACCOUNT_FORM_VALUE
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import EMPTY_MANUAL_CREDENTIAL_SUBMISSION
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantOutcome
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.predefined import ManualCredentialSubmission
from imbue.minds.desktop_client.latchkey.handlers.predefined import _build_account_choices
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.desktop_client.request_handler import UiPredefinedPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnknownScopeDetail
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.credential_commands import CredentialCommandParameter
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

# An entered ConcurrencyGroup for the handlers built in this module. Handlers
# now require one (their message sender dispatches the nudge on a tracked
# background thread); an autouse fixture provides it so each handler-building
# helper does not have to thread it through.
_MESSAGE_CONCURRENCY_GROUP: dict[str, ConcurrencyGroup | None] = {"cg": None}


@pytest.fixture(autouse=True)
def _entered_message_concurrency_group() -> Iterator[None]:
    cg = ConcurrencyGroup(name="predefined-test-messages")
    with cg:
        _MESSAGE_CONCURRENCY_GROUP["cg"] = cg
        try:
            yield
        finally:
            _MESSAGE_CONCURRENCY_GROUP["cg"] = None


def _message_sender() -> MngrMessageSender:
    """Build a recording message sender bound to the test's concurrency group."""
    cg = _MESSAGE_CONCURRENCY_GROUP["cg"]
    assert cg is not None
    return MngrMessageSender(mngr_caller=RecordingMngrCaller(), concurrency_group=cg, retry_delays_seconds=())


def _recorded_caller(handler: LatchkeyPermissionGrantHandler) -> RecordingMngrCaller:
    caller = handler.mngr_message_sender.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    return caller


def _recorded_mngr_argvs(handler: LatchkeyPermissionGrantHandler) -> list[list[str]]:
    """Return the argv of each ``mngr`` call the handler's message sender made (no wait)."""
    return _recorded_caller(handler).calls


def _wait_for_recorded_mngr_argvs(handler: LatchkeyPermissionGrantHandler, timeout: float = 5.0) -> list[list[str]]:
    """Wait for the handler's background ``mngr message`` to run, then return its argv."""
    caller = _recorded_caller(handler)
    assert caller.called_event.wait(timeout), "background mngr message send did not run"
    return caller.calls


def _read_recording(report_path: Path) -> list[dict[str, list[str] | str]]:
    """Parse the JSONL recording emitted by the fake latchkey binary."""
    if not report_path.exists():
        return []
    parsed: list[dict[str, list[str] | str]] = []
    for line in report_path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        argv_raw = raw["argv"]
        env_raw = raw["env_LATCHKEY_DIRECTORY"]
        assert isinstance(argv_raw, list)
        assert all(isinstance(a, str) for a in argv_raw)
        assert isinstance(env_raw, str)
        parsed.append({"argv": [str(a) for a in argv_raw], "env_LATCHKEY_DIRECTORY": env_raw})
    return parsed


_SLACK_SERVICE_INFO = ServicePermissionInfo(
    name="slack",
    scope="slack-api",
    display_name="Slack",
    service_display_name="Slack",
    permission_schemas=(
        "any",
        "slack-read-all",
        "slack-write-all",
        "slack-chat-read",
    ),
)


_SLACK_AVAILABLE_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "description": "Any interaction with the Slack API.",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all"},
                {"name": "slack-chat-read"},
            ],
        },
    ],
}


def _build_slack_services_catalog() -> ServicesCatalog:
    """Return a :class:`ServicesCatalog` pre-seeded with the Slack fixture.

    Uses an explicit catalog payload so we don't depend on the real
    ``services.json`` data file.
    """
    return ServicesCatalog.from_catalog_payload(_SLACK_AVAILABLE_PAYLOAD)


_DEFAULT_AUTH_OPTIONS_JSON: str = json.dumps(["browser", "set"])
_DEFAULT_SET_EXAMPLE: str = 'latchkey auth set slack -H "Authorization: Bearer xoxb-your-token"'


# Account the fake binary reports once a browser sign-in has completed,
# modelling latchkey storing the credentials under whichever account the user
# logged in as.
_SIGNED_IN_ACCOUNT: str = "signed-in@example.com"


def _make_latchkey_with_status(
    tmp_path: Path,
    *,
    credential_status: str,
    credential_account: str = "",
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = _DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
    signed_in_account: str = _SIGNED_IN_ACCOUNT,
    auth_set_exit: int = 0,
    auth_set_stderr: str = "",
    connected_credential_status: str = "valid",
) -> Latchkey:
    """Build a ``Latchkey`` backed by one stateful fake binary.

    ``services info``, ``auth browser`` and the ``auth set`` family all call
    the same fake binary via ``latchkey_binary``. The binary inspects its argv
    and either prints a JSON payload or appends to the matching recording.
    ``auth_options_json`` controls the ``authOptions`` array latchkey reports;
    pass ``json.dumps(["set"])`` to simulate a service that doesn't support
    browser sign-in. ``credential_account`` names the account the initial
    ``credential_status`` belongs to.

    A *successful* ``auth browser`` (storing ``signed_in_account``) or ``auth
    set`` (storing whatever ``--account`` named) makes every later ``services
    info`` report that account with ``connected_credential_status`` -- what
    real latchkey does once credentials exist, and what the grant flow reads
    back to learn which account it just connected.
    """
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    set_recording = tmp_path / "set_latchkey_report.jsonl"
    signed_in_marker = tmp_path / "signed_in_account"
    # latchkey 3.0.0 reports per-account credentials keyed by account name (the
    # default account keyed by ``""``); a ``missing`` service has an empty
    # ``credentials`` object rather than a top-level status.
    credentials = (
        {}
        if credential_status == "missing"
        else {credential_account: {"credentialType": "rawCurl", "credentialStatus": credential_status}}
    )
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"credentials = json.loads({json.dumps(json.dumps(credentials))})\n"
        f"marker = {str(signed_in_marker)!r}\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        "    if os.path.exists(marker):\n"
        "        with open(marker) as f:\n"
        "            credentials[f.read()] = {'credentialType': 'oauth',\n"
        f"                                     'credentialStatus': {connected_credential_status!r}}}\n"
        "    print(json.dumps({\n"
        "        'credentials': credentials,\n"
        f"        'authOptions': json.loads({json.dumps(auth_options_json)}),\n"
        f"        'setCredentialsExample': {set_credentials_example!r},\n"
        "    }))\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"    if {auth_browser_stderr!r}:\n"
        f"        sys.stderr.write({auth_browser_stderr!r})\n"
        f"    if {auth_browser_exit} == 0:\n"
        "        with open(marker, 'w') as f:\n"
        f"            f.write({signed_in_account!r})\n"
        f"    sys.exit({auth_browser_exit})\n"
        "elif 'auth' in argv and argv[argv.index('auth') + 1] in ('set', 'set-nocurl'):\n"
        f"    with open({str(set_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"    if {auth_set_stderr!r}:\n"
        f"        sys.stderr.write({auth_set_stderr!r})\n"
        f"    if {auth_set_exit} == 0:\n"
        "        account = argv[argv.index('--account') + 1] if '--account' in argv else ''\n"
        "        with open(marker, 'w') as f:\n"
        "            f.write(account)\n"
        f"    sys.exit({auth_set_exit})\n"
        "else:\n"
        "    sys.stderr.write('unexpected argv: ' + repr(argv))\n"
        "    sys.exit(99)\n"
    )
    binary.chmod(0o755)
    # ``latchkey_directory`` is required on ``Latchkey``; default to ``tmp_path``
    # for tests that don't care about the credential-store location.
    return Latchkey(latchkey_binary=str(binary), latchkey_directory=latchkey_directory or tmp_path)


def _build_handler(
    tmp_path: Path,
    *,
    credential_status: str,
    credential_account: str = "",
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = _DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
    signed_in_account: str = _SIGNED_IN_ACCOUNT,
    auth_set_exit: int = 0,
    auth_set_stderr: str = "",
    connected_credential_status: str = "valid",
) -> LatchkeyPermissionGrantHandler:
    latchkey = _make_latchkey_with_status(
        tmp_path,
        credential_status=credential_status,
        credential_account=credential_account,
        auth_browser_exit=auth_browser_exit,
        auth_browser_stderr=auth_browser_stderr,
        auth_options_json=auth_options_json,
        set_credentials_example=set_credentials_example,
        latchkey_directory=latchkey_directory,
        signed_in_account=signed_in_account,
        auth_set_exit=auth_set_exit,
        auth_set_stderr=auth_set_stderr,
        connected_credential_status=connected_credential_status,
    )
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=build_fake_gateway_client(),
    )


# -- LatchkeyPermissionGrantHandler.grant --


def test_grant_with_valid_credentials_skips_auth_browser_and_writes_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert "granted" in result.message.lower()
    assert result.manual_credentials is None
    # Auth browser must not have been invoked.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
    # Permissions file reflects the new rule and is keyed by host (not agent).
    # The rule is scoped to the account that was granted (here the unnamed
    # default one), so no other Slack account inherits it.
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    rule_key = account_scope_key("slack-api", "")
    assert on_disk["rules"] == [{rule_key: ["slack-read-all", "slack-write-all"]}]
    assert rule_key in on_disk["schemas"]
    # Response event was written and mngr message sent.
    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.GRANTED)
    mngr_argvs = _wait_for_recorded_mngr_argvs(handler)
    assert len(mngr_argvs) == 1
    argv = mngr_argvs[0]
    assert argv[0] == "message"


def test_grant_with_missing_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="missing", auth_browser_exit=0)
    agent_id = AgentId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1
    assert auth_recording[0]["argv"] == ["auth", "browser", "slack"]


def test_grant_with_invalid_credentials_also_invokes_auth_browser(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="invalid", auth_browser_exit=0)

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1


def test_grant_with_unknown_credentials_proceeds_without_invoking_auth_browser(tmp_path: Path) -> None:
    # UNKNOWN means latchkey can't vouch for the credential either way
    # (a rawCurl credential it can't validate, or a catalog scope that is
    # not a registered latchkey service at all, like the minds-internal
    # scopes served by a gateway extension); the grant must proceed
    # without prompting the user or invoking browser, regardless of
    # advertised auth options.
    handler = _build_handler(tmp_path, credential_status="unknown")

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    # Browser flow must not fire: UNKNOWN is treated as "trust the
    # caller", not "we need to (re-)authenticate".
    assert _read_recording(tmp_path / "auth_latchkey_report.jsonl") == []


def test_grant_with_unknown_credentials_and_set_only_auth_proceeds(tmp_path: Path) -> None:
    # A service whose credential latchkey stores but cannot validate:
    # ``credentialStatus=unknown`` with ``authOptions=["set"]`` (no
    # browser flow). Treating UNKNOWN as needs-setup would return
    # NEEDS_MANUAL_CREDENTIALS even though credentials are in place; the
    # grant must succeed without any user-visible re-setup prompt.
    handler = _build_handler(
        tmp_path,
        credential_status="unknown",
        auth_options_json=json.dumps(["set"]),
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert result.manual_credentials is None
    assert _read_recording(tmp_path / "auth_latchkey_report.jsonl") == []


def test_detail_payload_with_unknown_credentials_does_not_promise_browser(tmp_path: Path) -> None:
    # The dialog's progress notice must match what ``grant()`` will
    # actually do: UNKNOWN credential status proceeds straight to the
    # grant (no ``latchkey auth browser``), so the payload must not
    # promise a sign-in browser. Empty auth options mirror the real
    # degraded-UNKNOWN case (``services info`` fails for a non-latchkey
    # scope) and hit the legacy "no auth options" fallback, which would
    # falsely promise a browser under a plain not-VALID test.
    handler = _build_handler(
        tmp_path,
        credential_status="unknown",
        auth_options_json=json.dumps([]),
    )
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        rationale="need to read a channel",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    assert payload.will_open_browser is False


def test_grant_failed_browser_flow_stays_pending_without_denying(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_browser_exit=1,
        auth_browser_stderr="user cancelled",
    )
    agent_id = AgentId()
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    # A failed sign-in is a FAILED outcome, not a denial.
    assert result.outcome == GrantOutcome.FAILED
    assert "sign-in" in result.message.lower()
    assert "user cancelled" in result.message
    # The request stays pending: no resolving response event is returned.
    assert result.response_event is None
    # latchkey_permissions.json must NOT have been written.
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    # No response event was appended, so the request is not auto-denied and
    # remains pending for the user to retry from the dialog.
    assert load_response_events(tmp_path) == []
    # No mngr message was sent: the agent stays blocked, waiting on the
    # still-pending request rather than being told it was resolved.
    assert _recorded_mngr_argvs(handler) == []


def test_grant_rejects_empty_granted_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    with pytest.raises(LatchkeyPermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            host_id=HostId(),
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=(),
            account_choice="",
            manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
        )

    # Defence-in-depth: nothing should have been written.
    assert load_response_events(tmp_path) == []


def test_grant_rejects_permissions_outside_catalog(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    with pytest.raises(LatchkeyPermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            host_id=HostId(),
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=("not-a-real-permission",),
            account_choice="",
            manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
        )

    assert load_response_events(tmp_path) == []


def test_grant_replaces_existing_rule_for_same_scope_and_account(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    handler.grant(
        request_event_id="evt-1",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )
    handler.grant(
        request_event_id="evt-2",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", ""): ["slack-read-all", "slack-write-all"]}]


# -- LatchkeyPermissionGrantHandler.grant: NEEDS_MANUAL_CREDENTIALS path --


_AWS_SET_EXAMPLE: str = "latchkey auth set-nocurl aws <access-key-id> <secret-access-key>"

_AWS_CREDENTIAL_VALUES: dict[str, str] = {
    "access-key-id": "AKIA-3f9e21",
    "secret-access-key": "secret-6a4c07",
}


def _submission(
    value_by_parameter_name: dict[str, str],
    account_name: str = "",
) -> ManualCredentialSubmission:
    return ManualCredentialSubmission(value_by_parameter_name=value_by_parameter_name, account_name=account_name)


def _read_set_recording(tmp_path: Path) -> list[dict[str, list[str] | str]]:
    """Parse the recording of the ``latchkey auth set`` invocations the fake binary saw."""
    return _read_recording(tmp_path / "set_latchkey_report.jsonl")


def test_grant_asks_for_the_command_parameters_when_browser_auth_unsupported(tmp_path: Path) -> None:
    """An Approve with nothing typed re-states the credential form rather than granting."""
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.manual_credentials is not None
    assert result.manual_credentials.parameters == (
        CredentialCommandParameter(name="access-key-id", label="Access key id"),
        CredentialCommandParameter(name="secret-access-key", label="Secret access key"),
    )
    # The latchkey command behind the form is never surfaced to the user.
    assert "latchkey" not in result.manual_credentials.message
    assert "latchkey" not in result.message
    assert result.response_event is None
    # Neither the browser flow nor the credential command may run before the
    # user has typed anything.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
    assert _read_set_recording(tmp_path) == []
    # The request must remain pending: no response event, no permissions
    # file, no mngr message.
    assert load_response_events(tmp_path) == []
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    assert _recorded_mngr_argvs(handler) == []


def test_grant_falls_back_to_generic_example_when_latchkey_omits_one(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example="",
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.manual_credentials is not None
    # The generic fallback command asks for a bearer token.
    assert result.manual_credentials.parameters == (CredentialCommandParameter(name="token", label="Token"),)


def test_grant_returns_an_empty_form_when_the_example_has_no_parameters(tmp_path: Path) -> None:
    """A command with nothing to fill in is an error the dialog shows without an Approve."""
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example="latchkey auth set slack --from-keychain",
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.manual_credentials is not None
    assert result.manual_credentials.parameters == ()
    assert "cannot work out which credentials to ask for" in result.message
    assert "latchkey" not in result.message
    assert _read_set_recording(tmp_path) == []


def test_grant_runs_the_filled_in_credential_command_and_then_grants(tmp_path: Path) -> None:
    """Approve substitutes the typed values, runs the command, and grants."""
    latchkey_directory = tmp_path / "shared-latchkey"
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
        latchkey_directory=latchkey_directory,
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert result.manual_credentials is None
    # The command ran with the values substituted, pinned to the selected
    # (here: default) account, against the desktop client's own store.
    recording = _read_set_recording(tmp_path)
    assert len(recording) == 1
    assert recording[0]["argv"] == [
        "--account",
        "",
        "auth",
        "set-nocurl",
        "aws",
        "AKIA-3f9e21",
        "secret-6a4c07",
    ]
    assert recording[0]["env_LATCHKEY_DIRECTORY"] == str(latchkey_directory)
    # ... and the grant landed for that account.
    on_disk = json.loads(permissions_path_for_host(latchkey_directory / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", ""): ["slack-read-all"]}]


def test_grant_stores_manual_credentials_under_the_selected_account(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="invalid",
        credential_account="alice@x",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="alice@x",
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert _read_set_recording(tmp_path)[0]["argv"][:2] == ["--account", "alice@x"]
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", "alice@x"): ["slack-read-all"]}]


def test_grant_asks_for_an_account_name_when_adding_a_second_account(tmp_path: Path) -> None:
    """A new account of a service that already has one must be named by the user."""
    handler = _build_handler(
        tmp_path,
        credential_status="valid",
        credential_account="alice@x",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
    )
    host_id = HostId()

    # The dialog knows to ask for the name from the account choice itself.
    choices, _ = _build_account_choices(
        (ServiceAccountCredential(account="alice@x", credential_status=CredentialStatus.VALID),),
        None,
        is_browser_auth_supported=False,
    )
    new_account_choice = next(choice for choice in choices if choice.value == NEW_ACCOUNT_FORM_VALUE)
    assert new_account_choice.is_account_name_needed is True

    # Submitting the values without a name re-shows the form rather than
    # silently overwriting the unnamed default account.
    unnamed = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice=NEW_ACCOUNT_FORM_VALUE,
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )
    assert unnamed.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert "Enter a name" in unnamed.message
    assert _read_set_recording(tmp_path) == []

    named = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice=NEW_ACCOUNT_FORM_VALUE,
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES, account_name="  bob@x  "),
    )
    assert named.outcome == GrantOutcome.GRANTED
    assert _read_set_recording(tmp_path)[0]["argv"][:2] == ["--account", "bob@x"]
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", "bob@x"): ["slack-read-all"]}]


def test_grant_re_shows_the_form_when_a_value_is_left_blank(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=_submission({"access-key-id": "AKIA-3f9e21", "secret-access-key": "   "}),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert "Secret access key" in result.message
    assert result.manual_credentials is not None
    assert len(result.manual_credentials.parameters) == 2
    assert _read_set_recording(tmp_path) == []


def test_grant_re_shows_the_form_when_the_credential_command_fails(tmp_path: Path) -> None:
    """The service's own complaint about a value is surfaced, its usage lines are not."""
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
        auth_set_exit=1,
        # Verbatim from latchkey's AWS service: the second line prints the
        # placeholder rather than an example value, which would only confuse
        # someone looking at an input labelled with that same placeholder.
        auth_set_stderr=(
            "Error: The provided access key ID doesn't look like an AWS access key ID "
            "(expected to start with AKIA or ASIA).\nExample: <access-key-id>"
        ),
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.message == (
        "Slack rejected those credentials: The provided access key ID doesn't look like an AWS "
        "access key ID (expected to start with AKIA or ASIA)."
    )
    assert result.manual_credentials is not None
    assert len(result.manual_credentials.parameters) == 2
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    assert load_response_events(tmp_path) == []


def test_grant_reports_a_generic_failure_when_the_command_says_nothing_useful(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
        auth_set_exit=1,
        auth_set_stderr="Usage: latchkey auth set-nocurl <service_name>",
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.message == "Storing the Slack credentials failed."
    assert "latchkey" not in result.message


def test_grant_re_shows_the_form_when_the_stored_credentials_are_rejected(tmp_path: Path) -> None:
    """Well-formed but wrong credentials pass the store and only fail the service call.

    ``auth set`` only validates the shape of what it is handed, so a credential
    that is mistyped in a way the shape check accepts -- or that was since
    revoked, rotated or expired -- is stored happily and reports ``invalid``
    when latchkey actually calls the service. Nothing may be granted then.
    """
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=_AWS_SET_EXAMPLE,
        connected_credential_status="invalid",
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=_submission(_AWS_CREDENTIAL_VALUES),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert "did not accept those credentials" in result.message
    assert "revoked, rotated or expired" in result.message
    # The check is a live call, so an unreachable service reads the same way.
    assert "could not reach" in result.message
    # The values did reach latchkey, and the form comes back to be corrected.
    assert len(_read_set_recording(tmp_path)) == 1
    assert result.manual_credentials is not None
    assert len(result.manual_credentials.parameters) == 2
    # Nothing was granted and the request stays pending.
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    assert load_response_events(tmp_path) == []
    assert _recorded_mngr_argvs(handler) == []


def test_grant_re_checks_credentials_on_second_call_after_manual_setup(tmp_path: Path) -> None:
    """Simulate the user establishing credentials outside minds between two Approve clicks.

    The fake binary flips its ``credentials`` object from empty (no accounts) to
    a single valid account after a sentinel file appears, modelling credentials
    that appeared some other way (e.g. the user ran ``latchkey auth set``
    themselves). The first ``grant`` call must return
    ``NEEDS_MANUAL_CREDENTIALS`` and the second call (after the sentinel
    is written) must return ``GRANTED`` without running any command.
    """
    binary = tmp_path / "latchkey"
    sentinel = tmp_path / "creds_set"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"sentinel = {str(sentinel)!r}\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        "    credentials = {'': {'credentialType': 'rawCurl', 'credentialStatus': 'valid'}} if os.path.exists(sentinel) else {}\n"
        "    print(json.dumps({'credentials': credentials, 'authOptions': ['set'],\n"
        "                      'setCredentialsExample': 'latchkey auth set slack -H \"Authorization: Bearer <token>\"'}))\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('unexpected argv: ' + repr(argv))\n"
        "sys.exit(99)\n"
    )
    binary.chmod(0o755)
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary)),
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=build_fake_gateway_client(),
    )
    agent_id = AgentId()
    host_id = HostId()

    first = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )
    assert first.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS

    # Credentials appear from outside minds -- modelled by writing the sentinel.
    sentinel.write_text("")

    second = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )
    assert second.outcome == GrantOutcome.GRANTED
    assert second.response_event is not None


# -- LatchkeyPermissionGrantHandler.deny --


def test_deny_writes_response_event_without_touching_permissions_file(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    handler.deny(
        request_event_id="evt-abc",
        agent_id=agent_id,
        scope=_SLACK_SERVICE_INFO.scope,
        display_name=_SLACK_SERVICE_INFO.display_name,
    )

    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.DENIED)
    # No permissions file should have been created on either path.
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    # The auth-browser binary must not have been invoked either.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()


def test_deny_sends_mngr_message(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    handler.deny(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        scope=_SLACK_SERVICE_INFO.scope,
        display_name=_SLACK_SERVICE_INFO.display_name,
    )

    mngr_argvs = _wait_for_recorded_mngr_argvs(handler)
    assert len(mngr_argvs) == 1
    argv = mngr_argvs[0]
    assert "denied" in argv[argv.index("-m") + 1].lower()


def test_grant_calls_gateway_client_set_permission_and_delete_request(tmp_path: Path) -> None:
    """The handler routes the on-disk write through the gateway extension and clears the pending request."""
    fake_client = FakeLatchkeyGatewayClient()
    latchkey = _make_latchkey_with_status(tmp_path, credential_status="valid")
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=fake_client,
    )
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-xyz",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    # One set_permission_rule call per scope, pointed at the canonical
    # per-host file under the plugin data dir.
    assert len(fake_client.set_calls) == 1
    call = fake_client.set_calls[0]
    assert call.rule_key == account_scope_key("slack-api", "")
    assert call.granted_permissions == ("slack-read-all",)
    assert call.permissions_file_path == permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)
    # The pending request is removed from the gateway queue exactly once.
    assert fake_client.deleted_request_ids == ("evt-xyz",)


def test_deny_calls_gateway_delete_permission_request_only(tmp_path: Path) -> None:
    """Deny tears down the pending gateway record but never POSTs permissions."""
    fake_client = FakeLatchkeyGatewayClient()
    latchkey = _make_latchkey_with_status(tmp_path, credential_status="valid")
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=fake_client,
    )

    handler.deny(
        request_event_id="evt-deny",
        agent_id=AgentId(),
        scope=_SLACK_SERVICE_INFO.scope,
        display_name=_SLACK_SERVICE_INFO.display_name,
    )

    assert fake_client.set_calls == ()
    assert fake_client.deleted_request_ids == ("evt-deny",)


def _build_authenticated_client(
    tmp_path: Path,
    handler: LatchkeyPermissionGrantHandler,
    inbox: RequestInbox,
) -> FlaskClient:
    """Wire ``handler`` into a desktop-client app with a valid session cookie.

    Mirrors the helper used by ``file_sharing_test.py`` so the
    HTTP-level deny test below exercises the same dispatcher path the
    real desktop client uses.
    """
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface = StaticBackendResolver(url_by_agent_and_service={})
    paths = WorkspacePaths(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        request_inbox=inbox,
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.set_cookie(SESSION_COOKIE_NAME, cookie_value)
    return client


def test_apply_deny_request_succeeds_for_unknown_scope(tmp_path: Path) -> None:
    """Deny must work even when the request's scope is not in the gateway catalog.

    An agent can file a permission request under an unknown scope
    (typo, stale catalog, etc.); the detail payload
    (``UiUnknownScopeDetail``) offers Deny as the only action. The deny
    HTTP path must therefore still tear down the pending request,
    append a DENIED response event, and notify the agent -- using the
    raw scope string in place of a catalog display name.
    """
    fake_client = FakeLatchkeyGatewayClient()
    handler = _build_handler(tmp_path, credential_status="valid")
    # Swap in a gateway client that records delete calls so we can
    # assert the pending request was torn down.
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=handler.latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=handler.mngr_message_sender,
        gateway_client=fake_client,
    )
    agent_id = AgentId()
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(agent_id),
        scope="not-in-catalog-scope",
        rationale="please",
    )
    inbox = RequestInbox().add_request(event)
    client = _build_authenticated_client(tmp_path, handler, inbox)

    response = client.post(f"/requests/{event.event_id}/deny")

    assert response.status_code == 200
    assert response.get_json() == {"outcome": "DENIED"}
    # Gateway DELETE for the pending request must have been issued.
    assert fake_client.deleted_request_ids == (str(event.event_id),)
    # Response event was appended on disk, carrying the raw scope.
    response_events = load_response_events(tmp_path)
    assert len(response_events) == 1
    assert response_events[0].status == str(RequestStatus.DENIED)
    assert response_events[0].scope == "not-in-catalog-scope"
    # Agent was notified; the message falls back to the raw scope as
    # the display name since no catalog entry exists.
    mngr_argvs = _wait_for_recorded_mngr_argvs(handler)
    assert len(mngr_argvs) == 1
    argv = mngr_argvs[0]
    message_text = argv[argv.index("-m") + 1]
    assert "denied" in message_text.lower()
    assert "not-in-catalog-scope" in message_text


def test_grant_preserves_existing_schemas_block_in_permissions_file(tmp_path: Path) -> None:
    """A grant must rewrite ``rules`` only; the agent baseline ``schemas`` block survives.

    The real gateway extension does ``{...file, rules: <new>}``, so the
    inline schema definitions the per-agent baseline writes for the
    ``latchkey-self`` access remain intact across user-driven grants.
    The fake client mirrors that behaviour; this test pins it.
    """
    fake_client = FakeLatchkeyGatewayClient()
    latchkey = _make_latchkey_with_status(tmp_path, credential_status="valid")
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=fake_client,
    )
    host_id = HostId()
    host_path = permissions_path_for_host(tmp_path / "mngr_latchkey", host_id)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    baseline = {
        "rules": [
            {
                "latchkey-self": ["latchkey-self-create-permission-request"],
            },
        ],
        "schemas": {
            "latchkey-self": {"properties": {"domain": {"const": "latchkey-self.invalid"}}},
        },
    }
    host_path.write_text(json.dumps(baseline))

    handler.grant(
        request_event_id="evt-pres",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    on_disk = json.loads(host_path.read_text())
    # The baseline schemas survive; the grant only adds the generated schema
    # backing its own account-scoped rule key.
    rule_key = account_scope_key("slack-api", "")
    assert on_disk["schemas"]["latchkey-self"] == baseline["schemas"]["latchkey-self"]
    assert {"latchkey-self": baseline["rules"][0]["latchkey-self"]} in on_disk["rules"]
    assert {rule_key: ["slack-read-all"]} in on_disk["rules"]


# -- Per-account grants ---------------------------------------------------------


def _make_multi_account_latchkey(
    tmp_path: Path,
    accounts: dict[str, str],
    *,
    signed_in_account: str = _SIGNED_IN_ACCOUNT,
) -> Latchkey:
    """Build a ``Latchkey`` whose fake binary reports several stored accounts.

    ``accounts`` maps account name to its ``credentialStatus``. A successful
    ``auth browser`` records its argv (so tests can assert on ``--account``)
    and adds ``signed_in_account`` as a valid account, mirroring latchkey
    storing the credentials under whoever logged in.
    """
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    marker = tmp_path / "signed_in_account"
    credentials = {
        account: {"credentialType": "oauth", "credentialStatus": status} for account, status in accounts.items()
    }
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"credentials = json.loads({json.dumps(json.dumps(credentials))})\n"
        f"marker = {str(marker)!r}\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        "    if os.path.exists(marker):\n"
        "        with open(marker) as f:\n"
        "            credentials[f.read()] = {'credentialType': 'oauth', 'credentialStatus': 'valid'}\n"
        "    print(json.dumps({'credentials': credentials, 'authOptions': ['browser'], "
        "'setCredentialsExample': ''}))\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser-prepare']:\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': "
        "os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        "    with open(marker, 'w') as f:\n"
        f"        f.write({signed_in_account!r})\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('unexpected argv: ' + repr(argv))\n"
        "sys.exit(99)\n"
    )
    binary.chmod(0o755)
    return Latchkey(latchkey_binary=str(binary), latchkey_directory=tmp_path)


def _build_handler_for_latchkey(tmp_path: Path, latchkey: Latchkey) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=build_fake_gateway_client(),
    )


def test_grant_for_one_account_does_not_touch_another_accounts_rule(tmp_path: Path) -> None:
    """Two accounts of the same service hold independent grants."""
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid", "bob@x": "valid"})
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    host_id = HostId()

    handler.grant(
        request_event_id="evt-1",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="alice@x",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )
    handler.grant(
        request_event_id="evt-2",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-write-all",),
        account_choice="bob@x",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [
        {account_scope_key("slack-api", "alice@x"): ["slack-read-all"]},
        {account_scope_key("slack-api", "bob@x"): ["slack-write-all"]},
    ]
    # No browser sign-in: both accounts already had usable credentials.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()


def test_grant_with_new_account_choice_grants_the_account_that_signed_in(tmp_path: Path) -> None:
    """Picking "new account" signs in and grants whichever account latchkey stored."""
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid"}, signed_in_account="carol@x")
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-new",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice=NEW_ACCOUNT_FORM_VALUE,
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert "carol@x" in result.message
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    # Only the freshly-connected account is granted; alice keeps no grant.
    assert on_disk["rules"] == [{account_scope_key("slack-api", "carol@x"): ["slack-read-all"]}]


def test_grant_re_signs_in_a_specific_stale_account(tmp_path: Path) -> None:
    """An account whose credentials went invalid is re-authenticated by name."""
    latchkey = _make_multi_account_latchkey(
        tmp_path, {"alice@x": "invalid", "bob@x": "valid"}, signed_in_account="alice@x"
    )
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-stale",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="alice@x",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    # latchkey was told which account to re-authenticate, so it reuses that
    # account's stored client rather than the service-level preparation.
    recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert recording[0]["argv"] == ["auth", "browser", "slack", "--account", "alice@x"]
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", "alice@x"): ["slack-read-all"]}]


def test_grant_fails_when_the_signed_in_account_is_ambiguous(tmp_path: Path) -> None:
    """If we cannot tell which account was connected, nothing is granted."""

    # The fake completes the sign-in but stores nothing, so the account list is
    # unchanged and carries more than one candidate.
    def _make_binary() -> Latchkey:
        binary = tmp_path / "latchkey"
        credentials = {
            "alice@x": {"credentialType": "oauth", "credentialStatus": "valid"},
            "bob@x": {"credentialType": "oauth", "credentialStatus": "valid"},
        }
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            "if argv[:2] == ['services', 'info']:\n"
            f"    print(json.dumps({{'credentials': json.loads({json.dumps(json.dumps(credentials))}), "
            "'authOptions': ['browser'], 'setCredentialsExample': ''}))\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        )
        binary.chmod(0o755)
        return Latchkey(latchkey_binary=str(binary), latchkey_directory=tmp_path)

    handler = _build_handler_for_latchkey(tmp_path, _make_binary())
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-ambiguous",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice=NEW_ACCOUNT_FORM_VALUE,
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.FAILED
    assert result.response_event is None
    # Nothing was granted and the request stays pending.
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    assert load_response_events(tmp_path) == []


def test_detail_payload_offers_every_account_plus_a_new_one(tmp_path: Path) -> None:
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid", "bob@x": "valid"})
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        rationale="need to read a channel",
        account="bob@x",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    account_values = [choice.value for choice in payload.account_choices]
    assert "alice@x" in account_values
    assert "bob@x" in account_values
    assert NEW_ACCOUNT_FORM_VALUE in account_values
    # The account the agent asked for is the preselected one.
    assert payload.selected_account_value == "bob@x"


def test_detail_payload_offers_a_requested_account_that_is_not_connected(tmp_path: Path) -> None:
    """An agent may name an account nobody has signed in to; the dialog must say so.

    Dropping it would silently preselect a *different* account, so approving
    would grant one account while the agent kept using another and stayed
    blocked.
    """
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid"})
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        rationale="need to read a channel",
        account="bob@x",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    # The un-connected account is offered *and* preselected, next to the
    # already-connected one, and is flagged as needing sign-in.
    account_values = [choice.value for choice in payload.account_choices]
    assert "alice@x" in account_values
    assert "bob@x" in account_values
    assert payload.selected_account_value == "bob@x"
    bob_choice = next(choice for choice in payload.account_choices if choice.value == "bob@x")
    assert "not connected yet" in bob_choice.hint
    # Approving will therefore open a browser rather than granting silently.
    assert payload.will_open_browser is True


def test_grant_for_a_requested_account_that_is_not_connected_signs_it_in(tmp_path: Path) -> None:
    """Approving the un-connected choice signs in and grants the stored account."""
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid"}, signed_in_account="bob@x")
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-not-connected",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="bob@x",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    # The user signed in as the account the agent asked for, so that is what got
    # granted -- and alice@x is untouched.
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", "bob@x"): ["slack-read-all"]}]


def test_grant_for_an_unconnected_account_follows_the_account_actually_signed_in(tmp_path: Path) -> None:
    """If the user signs in as someone else, the grant follows reality."""
    latchkey = _make_multi_account_latchkey(tmp_path, {"alice@x": "valid"}, signed_in_account="carol@x")
    handler = _build_handler_for_latchkey(tmp_path, latchkey)
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-other",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
        account_choice="bob@x",
        manual_credentials=EMPTY_MANUAL_CREDENTIAL_SUBMISSION,
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert "carol@x" in result.message
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk["rules"] == [{account_scope_key("slack-api", "carol@x"): ["slack-read-all"]}]


def test_build_account_choices_keeps_a_single_sign_in_option_when_nothing_is_connected() -> None:
    """With no accounts and no request, the only option is the sign-in one."""
    choices, selected = _build_account_choices((), None, is_browser_auth_supported=True)

    assert [(choice.value, choice.label) for choice in choices] == [(NEW_ACCOUNT_FORM_VALUE, "Sign in")]
    assert choices[0].hint == "opens a browser sign-in"
    assert selected == NEW_ACCOUNT_FORM_VALUE


def _offered_permissions(payload: UiPredefinedPermissionDetail) -> tuple[str, ...]:
    """Every permission the dialog offers, in the order its groups render them."""
    return tuple(row.permission for group in payload.permission_groups for row in group.rows)


def test_build_account_choices_promises_a_credential_form_without_browser_auth() -> None:
    """A service with no browser flow must not promise a browser sign-in."""
    choices, _ = _build_account_choices((), "alice@x", is_browser_auth_supported=False)

    assert [(choice.value, choice.label, choice.hint) for choice in choices] == [
        ("alice@x", "alice@x", "not connected yet — asks you for credentials"),
        (NEW_ACCOUNT_FORM_VALUE, "+ Add account", "asks you for credentials"),
    ]


@pytest.mark.parametrize(
    ("is_browser_auth_supported", "expected_hint"),
    [(True, "needs sign-in"), (False, "needs credentials")],
)
def test_build_account_choices_hints_at_what_a_stale_stored_account_needs(
    is_browser_auth_supported: bool,
    expected_hint: str,
) -> None:
    """A stored account whose credentials went bad needs whatever the service supports.

    Without a browser flow it is filled into the dialog's credential form, just
    like a brand-new account, so its hint must not say "sign in" either.
    """
    accounts = (
        ServiceAccountCredential(account=DEFAULT_ACCOUNT, credential_status=CredentialStatus.INVALID),
        ServiceAccountCredential(account="alice@x", credential_status=CredentialStatus.VALID),
    )

    choices, selected = _build_account_choices(accounts, None, is_browser_auth_supported=is_browser_auth_supported)

    hint_by_value = {choice.value: choice.hint for choice in choices}
    assert hint_by_value[DEFAULT_ACCOUNT] == expected_hint
    # A usable account needs nothing, so it says nothing.
    assert hint_by_value["alice@x"] == ""
    assert selected == "alice@x"


def test_build_request_detail_payload_mirrors_the_fragment_derivation(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="missing")
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="need to read a channel",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    assert payload.request_id == str(event.event_id)
    assert payload.scope == "slack-api"
    assert "slack-read-all" in _offered_permissions(payload)
    assert "slack-read-all" in payload.checked_permissions
    assert payload.rationale == "need to read a channel"
    # MISSING credentials + a browser auth option: approving will pop a
    # browser sign-in, exactly as the fragment's progress notice promises.
    assert payload.will_open_browser is True
    assert payload.new_account_value
    account_values = [choice.value for choice in payload.account_choices]
    assert payload.selected_account_value in account_values


def test_build_request_detail_payload_groups_permissions_for_the_dialog(tmp_path: Path) -> None:
    """Offered permissions arrive grouped, labelled, full access first and the wildcard last.

    The dialog renders labels only, so every row must carry one, and the
    catch-all must be flagged so the client can keep it exclusive and set it
    apart visually.
    """
    handler = _build_handler(tmp_path, credential_status="valid")
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=("slack-chat-read",),
        rationale="need to read a channel",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    assert [(group.heading, group.is_extras) for group in payload.permission_groups] == [
        ("Full access", False),
        ("Chat", False),
        ("Extras", True),
    ]
    # Every offered permission is grouped exactly once, and the wildcard is
    # the sole occupant of the trailing group.
    offered = _offered_permissions(payload)
    catalog_info = handler.services_catalog.get_by_scope("slack-api")
    if catalog_info is None:
        pytest.fail("expected the test catalog to expose the slack-api scope")
    assert sorted(offered) == sorted(catalog_info.permission_schemas)
    assert len(offered) == len(set(offered))
    assert [row.permission for row in payload.permission_groups[-1].rows] == [WILDCARD_PERMISSION_NAME]
    # Labels are human-readable and never echo the schema name.
    rows_by_permission = {row.permission: row for group in payload.permission_groups for row in group.rows}
    assert rows_by_permission["slack-chat-read"].label == "Read chat"
    assert rows_by_permission[WILDCARD_PERMISSION_NAME].label == "Everything (unrestricted)"
    assert all(row.label != row.permission for row in rows_by_permission.values())
    # Only the catch-all is flagged as the wildcard.
    assert [row.permission for row in rows_by_permission.values() if row.is_wildcard] == [WILDCARD_PERMISSION_NAME]


def test_build_request_detail_payload_carries_catalog_descriptions_on_the_rows(tmp_path: Path) -> None:
    """A described permission carries that description on its own row.

    The simple view shows it under the label, so it has to travel with the
    row rather than in a separate lookup table. Permissions the catalog does
    not describe carry an empty string, not a missing key.
    """
    handler = _build_handler(tmp_path, credential_status="valid")
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="need to read a channel",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    rows_by_permission = {row.permission: row for group in payload.permission_groups for row in group.rows}
    assert rows_by_permission["slack-read-all"].description == "All read operations across the Slack API."
    assert rows_by_permission["slack-write-all"].description == ""


class _FixedHostResolver(StaticBackendResolver):
    """Static resolver reporting one parseable ``HostId`` for every agent.

    The default ``StaticBackendResolver`` reports the ``"localhost"``
    placeholder, which is not a valid :class:`HostId`, so the pre-check
    derivation skips its existing-grants lookup. This subclass lets tests
    exercise that lookup against a seeded per-host permissions file.
    """

    fixed_host_id: HostId = Field(description="Host id the resolver reports for every agent.")

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=str(self.fixed_host_id))


def _seed_default_account_grant(tmp_path: Path, host_id: HostId, permissions: tuple[str, ...]) -> None:
    """Write the per-host permissions file production would write for a default-account Slack grant.

    Goes through :func:`build_account_grant` so the file carries the generated
    schema pinning the rule to the unnamed default account -- which is what the
    pre-check's grant reader inspects (it never interprets rule keys).
    """
    rule_key, granted, schemas = build_account_grant("slack-api", "", permissions)
    save_permissions(
        permissions_path_for_host(tmp_path / "mngr_latchkey", host_id),
        LatchkeyPermissionsConfig(rules=({rule_key: list(granted)},), schemas=schemas),
    )


def test_build_request_detail_payload_pre_checks_the_union_of_existing_grants_and_request(tmp_path: Path) -> None:
    """The pre-check is existing grants for the selected account plus the agent's request.

    Approving without modification grants the union: what the agent is asking
    for on top of what its account already has. Permissions that are neither
    granted nor requested stay unchecked.
    """
    handler = _build_handler(tmp_path, credential_status="valid")
    host_id = HostId()
    _seed_default_account_grant(tmp_path, host_id, ("slack-chat-read",))
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=("slack-write-all",),
        rationale="need to post an update",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=_FixedHostResolver(url_by_agent_and_service={}, fixed_host_id=host_id),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    # Catalog order is preserved: the requested permission precedes the
    # previously-granted one because that is their catalog order. The
    # never-requested ``slack-read-all`` (and the wildcard) stay unchecked.
    assert payload.checked_permissions == ("slack-write-all", "slack-chat-read")


def test_build_request_detail_payload_offers_but_never_auto_checks_the_wildcard(tmp_path: Path) -> None:
    """The catch-all ``any`` schema is offered as an option but never auto-added to the pre-check.

    Neither the agent's request for a specific permission nor an existing
    specific grant may pull the wildcard in; the user must opt into it
    explicitly.
    """
    handler = _build_handler(tmp_path, credential_status="valid")
    host_id = HostId()
    _seed_default_account_grant(tmp_path, host_id, ("slack-chat-read",))
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="need to read a channel",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=_FixedHostResolver(url_by_agent_and_service={}, fixed_host_id=host_id),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    assert WILDCARD_PERMISSION_NAME in _offered_permissions(payload)
    assert WILDCARD_PERMISSION_NAME not in payload.checked_permissions
    assert set(payload.checked_permissions) == {"slack-read-all", "slack-chat-read"}


def test_build_request_detail_payload_with_empty_request_and_no_grants_pre_checks_nothing(tmp_path: Path) -> None:
    """With nothing granted and nothing requested, the pre-check is empty.

    The Approve button then stays disabled until the user ticks a permission
    by hand.
    """
    handler = _build_handler(tmp_path, credential_status="valid")
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="slack-api",
        permissions=(),
        rationale="no specific permissions named",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=_FixedHostResolver(url_by_agent_and_service={}, fixed_host_id=HostId()),
    )

    if not isinstance(payload, UiPredefinedPermissionDetail):
        pytest.fail(f"expected a predefined detail payload, got {payload!r}")
    assert payload.checked_permissions == ()


def test_build_request_detail_payload_reports_unknown_scopes(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    event = create_latchkey_predefined_permission_request_event(
        agent_id=str(AgentId()),
        scope="not-a-real-scope",
        rationale="whatever",
    )

    payload = handler.build_request_detail_payload(
        req_event=event,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
    )

    if not isinstance(payload, UiUnknownScopeDetail):
        pytest.fail(f"expected a unknown_scope detail payload, got {payload!r}")
    assert payload.scope == "not-a-real-scope"
