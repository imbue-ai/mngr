import json
from pathlib import Path

import pytest
from starlette.responses import HTMLResponse

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.permissions import GrantOutcome
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_permission_request_event
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
        "elif argv[:2] == ['auth', 'browser-prepare']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
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
    return Latchkey(latchkey_binary=str(binary), latchkey_directory=latchkey_directory)


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
    # ``mngr message`` collects every positional into ``agents`` (nargs=-1),
    # so the message text MUST be passed via ``-m`` -- otherwise it would be
    # parsed as a second agent identifier and the message content would be
    # read from (silently empty) stdin in this subprocess context.
    assert recording == [{"argv": ["message", "-m", "hello", "--", str(agent_id)], "env_LATCHKEY_DIRECTORY": ""}]


def test_mngr_message_sender_does_not_raise_on_failure(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, "mngr", exit_code=1, stderr="agent missing")
    sender = MngrMessageSender(mngr_binary=str(binary))

    # No assertion needed: this must not raise.
    sender.send(AgentId(), "hello")


# -- LatchkeyPermissionGrantHandler.grant --


def test_grant_grants_scope_regardless_of_credential_status(tmp_path: Path) -> None:
    """Cred acquisition is agent-driven now; the grant flow writes the scope and exits.

    Smoke across all four credential states latchkey can report -- they
    should all produce ``GRANTED`` since the handler no longer runs
    ``auth browser``.
    """
    for status in ("valid", "missing", "invalid", "unknown"):
        per_run_dir = tmp_path / status
        per_run_dir.mkdir()
        handler = _build_handler(per_run_dir, credential_status=status)
        agent_id = AgentId()

        result = handler.grant(
            request_event_id="evt-abc",
            agent_id=agent_id,
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=("slack-read-all",),
        )

        assert result.outcome == GrantOutcome.GRANTED, f"status={status}"
        # Handler must not have invoked auth-browser / browser-prepare;
        # the agent drives those itself via the gateway.
        assert not (per_run_dir / "auth_latchkey_report.jsonl").exists(), f"status={status}"
        # Scope grant landed in the per-agent permissions file.
        config = load_permissions(permissions_path_for_agent(per_run_dir, agent_id))
        assert config.rules == ({"slack-api": ["slack-read-all"]},), f"status={status}"


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


# -- LatchkeyPermissionGrantHandler.render_request_page --


def _render_dialog_html(handler: LatchkeyPermissionGrantHandler) -> str:
    """Run ``render_request_page`` for a fixed Slack request and return its HTML."""
    request = create_latchkey_permission_request_event(
        agent_id=str(AgentId()),
        service_name=_SLACK_SERVICE_INFO.name,
        rationale="need slack access",
    )
    backend_resolver = StaticBackendResolver(url_by_agent_and_service={})
    response = handler.render_request_page(
        req_event=request,
        backend_resolver=backend_resolver,
        mngr_forward_origin="http://localhost:8421",
    )
    assert isinstance(response, HTMLResponse)
    # ``Response.body`` is typed ``bytes | memoryview[int]``; ``bytes()``
    # round-trips both into a plain ``bytes`` we can decode.
    return bytes(response.body).decode("utf-8")


def test_render_request_page_omits_browser_notice_when_credentials_valid(tmp_path: Path) -> None:
    """Valid credentials skip ``latchkey auth browser``; the dialog must not falsely promise one."""
    handler = _build_handler(tmp_path, credential_status="valid")

    html = _render_dialog_html(handler)

    assert "opening a browser window" not in html
    assert "Granting permission" in html


def test_render_request_page_shows_browser_notice_when_credentials_missing(tmp_path: Path) -> None:
    """Missing credentials with browser auth supported -> dialog warns about the browser pop-up."""
    handler = _build_handler(tmp_path, credential_status="missing")

    html = _render_dialog_html(handler)

    assert "opening a browser window" in html


def test_render_request_page_omits_browser_notice_when_browser_auth_unsupported(tmp_path: Path) -> None:
    """Service that only supports manual creds -> dialog must not promise a browser pop-up."""
    handler = _build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
    )

    html = _render_dialog_html(handler)

    assert "opening a browser window" not in html
    assert "Granting permission" in html


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
