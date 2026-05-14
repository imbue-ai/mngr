import json
from pathlib import Path

import pytest
from starlette.responses import HTMLResponse

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permissions import GrantOutcome
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import create_latchkey_permission_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host


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


def _make_latchkey_with_status(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    latchkey_directory: Path | None = None,
) -> Latchkey:
    """Build a ``Latchkey`` backed by a fake ``latchkey`` binary.

    The fake binary only needs to answer ``services info`` -- the grant
    flow no longer runs ``auth browser`` (the agent drives auth itself
    via the gateway), and the dialog's ``render_request_page`` probes
    ``services info`` to decide whether to warn the user about a Chrome
    sign-in window. ``auth_options_json`` controls the ``authOptions``
    array; pass ``json.dumps(["set"])`` to simulate a service that
    doesn't advertise browser sign-in.
    """
    binary = tmp_path / "latchkey"
    services_payload = json.dumps(
        {
            "credentialStatus": credential_status,
            "authOptions": json.loads(auth_options_json),
        }
    )
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        f"    print({services_payload!r})\n"
        "    sys.exit(0)\n"
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
    auth_options_json: str = _DEFAULT_AUTH_OPTIONS_JSON,
    latchkey_directory: Path | None = None,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
) -> LatchkeyPermissionGrantHandler:
    """Build a handler for tests; pass ``gateway_client`` to assert on its recorded calls."""
    latchkey = _make_latchkey_with_status(
        tmp_path,
        credential_status=credential_status,
        auth_options_json=auth_options_json,
        latchkey_directory=latchkey_directory,
    )
    mngr_binary = _make_recording_binary(tmp_path, "mngr", exit_code=0)
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog={_SLACK_SERVICE_INFO.name: _SLACK_SERVICE_INFO},
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
        gateway_client=gateway_client if gateway_client is not None else build_fake_gateway_client(),
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


def test_grant_writes_scope_to_per_host_file_regardless_of_credential_status(tmp_path: Path) -> None:
    """Cred acquisition is agent-driven now; the grant flow writes the scope and exits.

    Smoke across all four credential states latchkey can report -- they
    should all produce ``GRANTED`` since the handler no longer runs
    ``auth browser``. The scope rule lands in the per-host
    ``latchkey_permissions.json`` (every agent on the host shares it).
    """
    for status in ("valid", "missing", "invalid", "unknown"):
        per_run_dir = tmp_path / status
        per_run_dir.mkdir()
        handler = _build_handler(per_run_dir, credential_status=status)
        host_id = HostId()

        result = handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            host_id=host_id,
            service_info=_SLACK_SERVICE_INFO,
            granted_permissions=("slack-read-all", "slack-write-all"),
        )

        assert result.outcome == GrantOutcome.GRANTED, f"status={status}"
        assert "granted" in result.message.lower(), f"status={status}"
        # Scope grant landed in the per-host permissions file.
        on_disk = json.loads(permissions_path_for_host(per_run_dir / "mngr_latchkey", host_id).read_text())
        assert on_disk == {"rules": [{"slack-api": ["slack-read-all", "slack-write-all"]}]}, f"status={status}"
        # Response event was written and mngr message sent.
        responses = load_response_events(per_run_dir)
        assert len(responses) == 1, f"status={status}"
        assert responses[0].status == str(RequestStatus.GRANTED), f"status={status}"
        mngr_recording = _read_recording(per_run_dir / "mngr_report.jsonl")
        assert len(mngr_recording) == 1, f"status={status}"
        argv = mngr_recording[0]["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "message", f"status={status}"


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


def test_render_request_page_notes_grants_are_shared_per_host(tmp_path: Path) -> None:
    """Dialog must tell the user the grant applies to every agent on the host.

    Latchkey state (gateway URL, password, JWT, permissions config) is
    keyed per-host, so every agent that runs on this workspace's host
    inherits the same grants. The dialog surfaces that scope so the user
    isn't surprised by it.
    """
    handler = _build_handler(tmp_path, credential_status="valid")

    html = _render_dialog_html(handler)

    # Short bracket note next to the workspace link.
    assert "grants apply to every agent on this host" in html
    # Reinforced in the form body so users who skim past the header still see it.
    assert "shared across every agent running on this workspace's host" in html


# -- LatchkeyPermissionGrantHandler.deny --


def test_deny_writes_response_event_without_touching_permissions_file(tmp_path: Path) -> None:
    handler = _build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    handler.deny(
        request_event_id="evt-abc",
        agent_id=agent_id,
        service_info=_SLACK_SERVICE_INFO,
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
        service_info=_SLACK_SERVICE_INFO,
    )

    mngr_recording = _read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert "denied" in argv[2].lower()


def test_grant_calls_gateway_client_set_permission_and_delete_request(tmp_path: Path) -> None:
    """The handler routes the on-disk write through the gateway extension and clears the pending request."""
    fake_client = build_fake_gateway_client()
    handler = _build_handler(tmp_path, credential_status="valid", gateway_client=fake_client)
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
    fake_client = build_fake_gateway_client()
    handler = _build_handler(tmp_path, credential_status="valid", gateway_client=fake_client)

    handler.deny(
        request_event_id="evt-deny",
        agent_id=AgentId(),
        service_info=_SLACK_SERVICE_INFO,
    )

    assert fake_client.set_calls == ()
    assert fake_client.deleted_request_ids == ("evt-deny",)


def test_grant_preserves_existing_schemas_block_in_permissions_file(tmp_path: Path) -> None:
    """A grant must rewrite ``rules`` only; the agent baseline ``schemas`` block survives.

    The real gateway extension does ``{...file, rules: <new>}``, so the
    inline schema definitions the per-agent baseline writes for the
    ``latchkey-self`` access remain intact across user-driven grants.
    The fake client mirrors that behaviour; this test pins it.
    """
    fake_client = build_fake_gateway_client()
    handler = _build_handler(tmp_path, credential_status="valid", gateway_client=fake_client)
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
