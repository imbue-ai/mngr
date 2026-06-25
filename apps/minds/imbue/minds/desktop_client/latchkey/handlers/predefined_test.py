import json
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantOutcome
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import permissions_path_for_host

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
    return MngrMessageSender(mngr_caller=RecordingMngrCaller(), concurrency_group=cg)


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


def _make_latchkey_with_status(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = _DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
) -> Latchkey:
    """Build a ``Latchkey`` that uses two fake binaries.

    Both ``services info`` and ``auth browser`` call the same fake binary
    via ``latchkey_binary``. The binary inspects ``argv[0]`` (``services``
    or ``auth``) and either prints a JSON payload or appends to the
    auth-browser recording. ``auth_options_json`` controls the
    ``authOptions`` array latchkey reports; pass ``json.dumps(["set"])``
    to simulate a service that doesn't support browser sign-in.
    """
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    services_payload = json.dumps(
        {
            "credentialStatus": credential_status,
            "authOptions": json.loads(auth_options_json),
            "setCredentialsExample": set_credentials_example,
        }
    )
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        f"    print({services_payload!r})\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"    if {auth_browser_stderr!r}:\n"
        f"        sys.stderr.write({auth_browser_stderr!r})\n"
        f"    sys.exit({auth_browser_exit})\n"
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
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = _DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
) -> LatchkeyPermissionGrantHandler:
    latchkey = _make_latchkey_with_status(
        tmp_path,
        credential_status=credential_status,
        auth_browser_exit=auth_browser_exit,
        auth_browser_stderr=auth_browser_stderr,
        auth_options_json=auth_options_json,
        set_credentials_example=set_credentials_example,
        latchkey_directory=latchkey_directory,
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
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert "granted" in result.message.lower()
    assert result.set_credentials_example is None
    # Auth browser must not have been invoked.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
    # Permissions file reflects the new rule and is keyed by host (not agent).
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk == {"rules": [{"slack-api": ["slack-read-all", "slack-write-all"]}]}
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
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1


def test_grant_with_unknown_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    # services info exits 0 but with no recognized status -> UNKNOWN.
    # No authOptions either, so the grant falls back to the legacy browser
    # behaviour rather than refusing.
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        "    print('not json')\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        "    sys.exit(0)\n"
    )
    binary.chmod(0o755)
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary)),
        services_catalog=_build_slack_services_catalog(),
        mngr_message_sender=_message_sender(),
        gateway_client=build_fake_gateway_client(),
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert len(_read_recording(auth_recording)) == 1


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
        )

    assert load_response_events(tmp_path) == []


def test_grant_replaces_existing_rule_for_same_scope(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    handler.grant(
        request_event_id="evt-1",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )
    handler.grant(
        request_event_id="evt-2",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).read_text())
    assert on_disk == {"rules": [{"slack-api": ["slack-read-all", "slack-write-all"]}]}


# -- LatchkeyPermissionGrantHandler.grant: NEEDS_MANUAL_CREDENTIALS path --


def test_grant_refuses_when_browser_auth_unsupported_and_returns_set_example(tmp_path: Path) -> None:
    base_example = 'latchkey auth set coolify -H "Authorization: Bearer <token>"'
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=base_example,
    )
    agent_id = AgentId()
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    # ``latchkey_directory`` is required on ``Latchkey`` now so the
    # ``LATCHKEY_DIRECTORY=`` prefix is always added so the user's
    # terminal-run ``latchkey auth set`` writes credentials into the same
    # store the desktop client uses.
    assert result.set_credentials_example is not None
    assert result.set_credentials_example.startswith("LATCHKEY_DIRECTORY=")
    assert result.set_credentials_example.endswith(f" {base_example}")
    assert result.response_event is None
    # The browser flow must not have been invoked.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
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
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.set_credentials_example is not None
    assert "latchkey auth set slack" in result.set_credentials_example


def test_grant_prefixes_set_example_with_latchkey_directory_when_pinned(tmp_path: Path) -> None:
    """User-facing command must write into the same store the desktop client uses.

    The desktop client passes ``LATCHKEY_DIRECTORY`` to all its own latchkey
    invocations; if we don't tell the user to do the same, ``latchkey auth
    set`` writes credentials into ``~/.latchkey`` while the desktop client
    keeps reading from the pinned directory and the second Approve click
    still reports ``MISSING``.
    """
    pinned = tmp_path / "pinned latchkey dir"
    pinned.mkdir()
    base_example = 'latchkey auth set slack -H "Authorization: Bearer <token>"'
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=base_example,
        latchkey_directory=pinned,
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.set_credentials_example is not None
    # The directory contains a space, so the path must be shell-quoted to
    # survive a copy-paste into a terminal.
    expected_prefix = f"LATCHKEY_DIRECTORY={shlex.quote(str(pinned))} "
    assert result.set_credentials_example.startswith(expected_prefix)
    assert result.set_credentials_example.endswith(base_example)


def test_grant_prefixes_set_example_with_pinned_latchkey_directory(tmp_path: Path) -> None:
    """The suggested command always carries the pinned ``LATCHKEY_DIRECTORY=``.

    ``Latchkey`` requires a ``latchkey_directory``, so the user's
    terminal-run ``latchkey auth set`` must point at the same store
    the desktop client uses; otherwise upstream latchkey would write
    credentials to its own default ``~/.latchkey`` and the desktop
    client would never see them.
    """
    base_example = 'latchkey auth set slack -H "Authorization: Bearer <token>"'
    pinned = tmp_path / "shared-latchkey"
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example=base_example,
        latchkey_directory=pinned,
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS
    assert result.set_credentials_example == f"LATCHKEY_DIRECTORY={pinned} {base_example}"


def test_grant_re_checks_credentials_on_second_call_after_manual_setup(tmp_path: Path) -> None:
    """Simulate the user running ``latchkey auth set`` between two Approve clicks.

    The fake binary flips ``credentialStatus`` from ``missing`` to ``valid``
    after a sentinel file appears, modelling the user running the suggested
    command. The first ``grant`` call must return
    ``NEEDS_MANUAL_CREDENTIALS`` and the second call (after the sentinel
    is written) must return ``GRANTED``.
    """
    binary = tmp_path / "latchkey"
    sentinel = tmp_path / "creds_set"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"sentinel = {str(sentinel)!r}\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        "    status = 'valid' if os.path.exists(sentinel) else 'missing'\n"
        "    print(json.dumps({'credentialStatus': status, 'authOptions': ['set'], 'setCredentialsExample': 'latchkey auth set slack ...'}))\n"
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
    )
    assert first.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS

    # User runs the suggested command -- modelled by writing the sentinel.
    sentinel.write_text("")

    second = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
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
    assert "denied" in argv[2].lower()


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
    )

    assert result.outcome == GrantOutcome.GRANTED
    # One set_permission_rule call per scope, pointed at the canonical
    # per-host file under the plugin data dir.
    assert len(fake_client.set_calls) == 1
    call = fake_client.set_calls[0]
    assert call.rule_key == "slack-api"
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
    (typo, stale catalog, etc.); the rendered detail fragment
    (:func:`_render_unknown_scope_fragment`) offers Deny as the only
    action. The deny HTTP path must therefore still tear down the
    pending request, append a DENIED response event, and notify the
    agent -- using the raw scope string in place of a catalog
    display name.
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
    assert "denied" in argv[2].lower()
    assert "not-in-catalog-scope" in argv[2]


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
    )

    on_disk = json.loads(host_path.read_text())
    assert on_disk["schemas"] == baseline["schemas"]
    assert {"latchkey-self": baseline["rules"][0]["latchkey-self"]} in on_disk["rules"]
    assert {"slack-api": ["slack-read-all"]} in on_disk["rules"]
