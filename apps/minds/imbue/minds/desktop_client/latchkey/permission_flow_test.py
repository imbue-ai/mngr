import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.permission_flow import CredentialStatus
from imbue.minds.desktop_client.latchkey.permission_flow import GrantOutcome
from imbue.minds.desktop_client.latchkey.permission_flow import LatchkeyAuthBrowserRunner
from imbue.minds.desktop_client.latchkey.permission_flow import LatchkeyServicesInfoProbe
from imbue.minds.desktop_client.latchkey.permission_flow import MngrMessageSender
from imbue.minds.desktop_client.latchkey.permission_flow import PermissionFlowError
from imbue.minds.desktop_client.latchkey.permission_flow import PermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions_store import load_permissions
from imbue.minds.desktop_client.latchkey.permissions_store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId


def _make_services_info_binary(
    tmp_path: Path,
    name: str = "latchkey",
    *,
    credential_status: str = "valid",
    exit_code: int = 0,
) -> Path:
    """Build a fake latchkey that prints a services-info JSON payload."""
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"if sys.argv[1:3] != ['services', 'info']:\n"
        f"    print('unexpected args:', sys.argv, file=sys.stderr)\n"
        f"    sys.exit(99)\n"
        f"payload = {{\n"
        f'    "type": "built-in",\n'
        f'    "baseApiUrls": ["https://api.example.com"],\n'
        f'    "authOptions": ["browser", "set"],\n'
        f'    "credentialStatus": {credential_status!r},\n'
        f'    "setCredentialsExample": "...",\n'
        f'    "developerNotes": "...",\n'
        f"}}\n"
        f"print(json.dumps(payload, indent=2))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def _make_recording_binary(tmp_path: Path, name: str, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Build a fake binary that appends its argv to a report file and exits."""
    script = tmp_path / name
    report_path = tmp_path / f"{name}_report.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"report = {str(report_path)!r}\n"
        "with open(report, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"if {stderr!r}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def _read_recording(report_path: Path) -> list[dict[str, list[str] | str]]:
    """Parse the JSONL recording emitted by ``_make_recording_binary``.

    Each entry has ``argv`` (list of strings) and ``env_LATCHKEY_DIRECTORY``
    (string). The narrow return type avoids subscripting ``object`` in tests.
    """
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
    display_name="Slack",
    description="Slack messaging.",
    scope_schemas=("slack-api",),
    permission_schemas=(
        "slack-read-all",
        "slack-write-all",
        "slack-chat-read",
    ),
    default_permissions=("slack-read-all", "slack-write-all"),
)


# -- LatchkeyServicesInfoProbe --


def test_services_info_probe_returns_valid_when_status_is_valid(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="valid")
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(binary))

    assert probe.probe("slack") == CredentialStatus.VALID


def test_services_info_probe_returns_missing_when_status_is_missing(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="missing")
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(binary))

    assert probe.probe("slack") == CredentialStatus.MISSING


def test_services_info_probe_returns_invalid_when_status_is_invalid(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="invalid")
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(binary))

    assert probe.probe("slack") == CredentialStatus.INVALID


def test_services_info_probe_returns_unknown_when_process_fails(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, exit_code=1)
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(binary))

    assert probe.probe("slack") == CredentialStatus.UNKNOWN


def test_services_info_probe_returns_unknown_when_output_is_not_json(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text("#!/usr/bin/env python3\nprint('not json')\n")
    script.chmod(0o755)
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(script))

    assert probe.probe("slack") == CredentialStatus.UNKNOWN


def test_services_info_probe_returns_unknown_for_unrecognized_status(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="totally-new")
    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(binary))

    assert probe.probe("slack") == CredentialStatus.UNKNOWN


def test_services_info_probe_passes_latchkey_directory_through(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    report_path = tmp_path / "report"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(report_path)!r}, 'w') as f:\n"
        "    f.write(os.environ.get('LATCHKEY_DIRECTORY', ''))\n"
        "print(json.dumps({'credentialStatus': 'valid'}))\n"
    )
    script.chmod(0o755)
    latchkey_dir = tmp_path / "shared_latchkey"

    probe = LatchkeyServicesInfoProbe(latchkey_binary=str(script), latchkey_directory=latchkey_dir)
    probe.probe("slack")

    assert report_path.read_text() == str(latchkey_dir)


# -- LatchkeyAuthBrowserRunner --


def test_auth_browser_runner_reports_success_on_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "latchkey", exit_code=0)
    runner = LatchkeyAuthBrowserRunner(latchkey_binary=str(binary))

    is_success, detail = runner.run("slack")

    assert is_success is True
    assert detail == ""


def test_auth_browser_runner_reports_failure_on_non_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "latchkey", exit_code=1, stderr="user cancelled")
    runner = LatchkeyAuthBrowserRunner(latchkey_binary=str(binary))

    is_success, detail = runner.run("slack")

    assert is_success is False
    assert detail == "user cancelled"


def test_auth_browser_runner_uses_auth_browser_subcommand(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "latchkey", exit_code=0)
    runner = LatchkeyAuthBrowserRunner(latchkey_binary=str(binary))

    runner.run("slack")

    recording = _read_recording(tmp_path / "latchkey_report.jsonl")
    assert recording == [{"argv": ["auth", "browser", "slack"], "env_LATCHKEY_DIRECTORY": ""}]


# -- MngrMessageSender --


def test_mngr_message_sender_invokes_message_subcommand(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)
    sender = MngrMessageSender(mngr_binary=str(binary))
    agent_id = AgentId()

    sender.send(agent_id, "hello")

    recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert recording == [{"argv": ["message", str(agent_id), "hello"], "env_LATCHKEY_DIRECTORY": ""}]


def test_mngr_message_sender_does_not_raise_on_failure(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "mngr", exit_code=1, stderr="agent missing")
    sender = MngrMessageSender(mngr_binary=str(binary))

    # No assertion needed: this must not raise.
    sender.send(AgentId(), "hello")


# -- PermissionGrantHandler.grant --


def _build_handler(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
) -> PermissionGrantHandler:
    services_info_binary = tmp_path / "services_info_latchkey"
    services_info_binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "payload = {\n"
        f'    "credentialStatus": {credential_status!r},\n'
        "}\n"
        "print(json.dumps(payload))\n"
    )
    services_info_binary.chmod(0o755)

    auth_binary = _make_recording_binary(
        tmp_path,
        "auth_latchkey",
        exit_code=auth_browser_exit,
        stderr=auth_browser_stderr,
    )
    mngr_binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)

    return PermissionGrantHandler(
        data_dir=tmp_path,
        services_info_probe=LatchkeyServicesInfoProbe(latchkey_binary=str(services_info_binary)),
        auth_browser_runner=LatchkeyAuthBrowserRunner(latchkey_binary=str(auth_binary)),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
    )


def test_grant_with_valid_credentials_skips_auth_browser_and_writes_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    assert result.outcome == GrantOutcome.GRANTED
    # Auth browser must not have been invoked.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
    # Permissions file reflects the new rule.
    config = load_permissions(permissions_path_for_agent(tmp_path, agent_id))
    assert config.rules == ({"slack-api": ["slack-read-all", "slack-write-all"]},)
    # Response event was written and mngr message sent.
    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.GRANTED)
    mngr_recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "message"


def test_grant_with_missing_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="missing", auth_browser_exit=0)
    agent_id = AgentId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
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
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1


def test_grant_with_unknown_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    # services info exits 0 but with no recognized status -> UNKNOWN.
    services_info_binary = tmp_path / "services_info_latchkey"
    services_info_binary.write_text("#!/usr/bin/env python3\nprint('not json')\n")
    services_info_binary.chmod(0o755)
    auth_binary = _make_recording_binary(tmp_path, "auth_latchkey", exit_code=0)
    mngr_binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)

    handler = PermissionGrantHandler(
        data_dir=tmp_path,
        services_info_probe=LatchkeyServicesInfoProbe(latchkey_binary=str(services_info_binary)),
        auth_browser_runner=LatchkeyAuthBrowserRunner(latchkey_binary=str(auth_binary)),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1


def test_grant_returns_auth_failed_when_browser_flow_fails(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_browser_exit=1,
        auth_browser_stderr="user cancelled",
    )
    agent_id = AgentId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.AUTH_FAILED
    # permissions.json must NOT have been written.
    assert not permissions_path_for_agent(tmp_path, agent_id).exists()
    # AUTH_FAILED response event was appended.
    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.AUTH_FAILED)
    # mngr message was still sent (the agent needs to be unblocked).
    mngr_recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1


def test_grant_rejects_empty_granted_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    with pytest.raises(PermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=(),
        )

    # Defence-in-depth: nothing should have been written.
    assert load_response_events(tmp_path) == []


def test_grant_rejects_permissions_outside_catalog(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    with pytest.raises(PermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=("not-a-real-permission",),
        )

    assert load_response_events(tmp_path) == []


def test_grant_replaces_existing_rule_for_same_scope(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()

    handler.grant(
        request_event_id="evt-1",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )
    handler.grant(
        request_event_id="evt-2",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    config = load_permissions(permissions_path_for_agent(tmp_path, agent_id))
    assert config.rules == ({"slack-api": ["slack-read-all", "slack-write-all"]},)


# -- PermissionGrantHandler.deny --


def test_deny_writes_response_event_without_touching_permissions_file(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()

    handler.deny(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
    )

    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.DENIED)
    # No permissions file should have been created.
    assert not permissions_path_for_agent(tmp_path, agent_id).exists()
    # The auth-browser binary must not have been invoked either.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()


def test_deny_sends_mngr_message(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    handler.deny(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        service_info=_SLACK_SERVICE_INFO,
    )

    mngr_recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert "denied" in argv[2].lower()
