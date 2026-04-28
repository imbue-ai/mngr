import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId


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
    """Parse the JSONL recording emitted by ``_make_recording_binary``."""
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
    scope_schemas=("slack-api",),
    permission_schemas=(
        "any",
        "slack-read-all",
        "slack-write-all",
        "slack-chat-read",
    ),
)


def _make_latchkey_with_status(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
) -> Latchkey:
    """Build a ``Latchkey`` that uses two fake binaries.

    Both ``services info`` and ``auth browser`` call the same fake binary
    via ``latchkey_binary``. The binary inspects ``argv[0]`` (``services``
    or ``auth``) and either prints a JSON payload or appends to the
    auth-browser recording.
    """
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        f"    print(json.dumps({{'credentialStatus': {credential_status!r}}}))\n"
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
    return Latchkey(latchkey_binary=str(binary))


def _build_handler(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
) -> LatchkeyPermissionGrantHandler:
    latchkey = _make_latchkey_with_status(
        tmp_path,
        credential_status=credential_status,
        auth_browser_exit=auth_browser_exit,
        auth_browser_stderr=auth_browser_stderr,
    )
    mngr_binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog={_SLACK_SERVICE_INFO.name: _SLACK_SERVICE_INFO},
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
    )


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


# -- LatchkeyPermissionGrantHandler.grant --


def test_grant_with_valid_credentials_skips_auth_browser_and_writes_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()

    was_granted, message, _ = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    assert was_granted is True
    assert "granted" in message.lower()
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

    was_granted, _, _ = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert was_granted is True
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1
    assert auth_recording[0]["argv"] == ["auth", "browser", "slack"]


def test_grant_with_invalid_credentials_also_invokes_auth_browser(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="invalid", auth_browser_exit=0)

    was_granted, _, _ = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert was_granted is True
    auth_recording = _read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1


def test_grant_with_unknown_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    # services info exits 0 but with no recognized status -> UNKNOWN.
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
    mngr_binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_binary=str(binary)),
        services_catalog={_SLACK_SERVICE_INFO.name: _SLACK_SERVICE_INFO},
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
    )

    was_granted, _, _ = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert was_granted is True
    assert len(_read_recording(auth_recording)) == 1


def test_grant_treats_failed_browser_flow_as_deny_with_distinct_message(tmp_path: Path) -> None:
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_browser_exit=1,
        auth_browser_stderr="user cancelled",
    )
    agent_id = AgentId()

    was_granted, message, _ = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert was_granted is False
    assert "sign-in" in message.lower()
    assert "user cancelled" in message
    # permissions.json must NOT have been written.
    assert not permissions_path_for_agent(tmp_path, agent_id).exists()
    # A DENIED response event was appended (no separate AUTH_FAILED status).
    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.DENIED)
    # mngr message was still sent (the agent needs to be unblocked).
    mngr_recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1


def test_grant_rejects_empty_granted_permissions(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")

    with pytest.raises(LatchkeyPermissionFlowError):
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

    with pytest.raises(LatchkeyPermissionFlowError):
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


# -- LatchkeyPermissionGrantHandler.deny --


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
