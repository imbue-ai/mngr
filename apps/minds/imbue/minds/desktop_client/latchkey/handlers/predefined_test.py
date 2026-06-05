import json
import shlex
from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantOutcome
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionFlowError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.testing import SLACK_SERVICE_INFO
from imbue.minds.desktop_client.latchkey.handlers.testing import build_handler
from imbue.minds.desktop_client.latchkey.handlers.testing import build_handler_with_gateway_client
from imbue.minds.desktop_client.latchkey.handlers.testing import build_slack_services_catalog
from imbue.minds.desktop_client.latchkey.handlers.testing import make_recording_binary
from imbue.minds.desktop_client.latchkey.handlers.testing import read_recording
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host

# -- MngrMessageSender --


def test_mngr_message_sender_invokes_message_subcommand(tmp_path: Path) -> None:
    binary = make_recording_binary(tmp_path, "mngr", exit_code=0)
    sender = MngrMessageSender(mngr_binary=str(binary))
    agent_id = AgentId()

    sender.send(agent_id, "hello")

    recording = read_recording(tmp_path / "mngr_report.jsonl")
    # ``mngr message`` collects every positional into ``agents`` (nargs=-1),
    # so the message text MUST be passed via ``-m`` -- otherwise it would be
    # parsed as a second agent identifier and the message content would be
    # read from (silently empty) stdin in this subprocess context.
    assert recording == [{"argv": ["message", "-m", "hello", "--", str(agent_id)], "env_LATCHKEY_DIRECTORY": ""}]


def test_mngr_message_sender_swallows_delivery_failure_after_attempting_send(tmp_path: Path) -> None:
    binary = make_recording_binary(tmp_path, "mngr", exit_code=1, stderr="agent missing")
    sender = MngrMessageSender(mngr_binary=str(binary))

    # Must not raise even though the binary exits non-zero.
    sender.send(AgentId(), "hello")

    # ...and the failure was swallowed *after* a real delivery attempt: the
    # recording binary logs its argv before exiting 1, so exactly one
    # invocation proves send() actually ran the subprocess rather than
    # silently doing nothing.
    assert len(read_recording(tmp_path / "mngr_report.jsonl")) == 1


# -- LatchkeyPermissionGrantHandler.grant --


def test_grant_with_valid_credentials_skips_auth_browser_and_writes_permissions(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert "granted" in result.message.lower()
    assert result.set_credentials_example is None
    # Auth browser must not have been invoked.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()
    # The handler's gateway-call contract (correct path/rule_key/permissions) is
    # asserted in test_grant_calls_gateway_client_set_permission_and_delete_request;
    # the resulting on-disk merge is owned by the gateway extension and covered by
    # libs/mngr_latchkey's permissions_test.py, so we don't re-check the merged file here.
    # Response event was written and mngr message sent.
    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.GRANTED)
    mngr_recording = read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "message"


def test_grant_with_missing_credentials_invokes_auth_browser(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="missing", auth_browser_exit=0)
    agent_id = AgentId()

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=HostId(),
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = read_recording(tmp_path / "auth_latchkey_report.jsonl")
    assert len(auth_recording) == 1
    assert auth_recording[0]["argv"] == ["auth", "browser", "slack"]


def test_grant_with_invalid_credentials_also_invokes_auth_browser(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="invalid", auth_browser_exit=0)

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    auth_recording = read_recording(tmp_path / "auth_latchkey_report.jsonl")
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
    mngr_binary = make_recording_binary(tmp_path, "mngr", exit_code=0)
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary)),
        services_catalog=build_slack_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
        gateway_client=build_fake_gateway_client(),
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )

    assert result.outcome == GrantOutcome.GRANTED
    assert len(read_recording(auth_recording)) == 1


def test_grant_failed_browser_flow_stays_pending_without_denying(tmp_path: Path) -> None:
    handler = build_handler(
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
        service_info=SLACK_SERVICE_INFO,
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
    assert not (tmp_path / "mngr_report.jsonl").exists()


def test_grant_rejects_empty_granted_permissions(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="valid")

    with pytest.raises(LatchkeyPermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            host_id=HostId(),
            service_info=SLACK_SERVICE_INFO,
            granted_permissions=(),
        )

    # Defence-in-depth: nothing should have been written.
    assert load_response_events(tmp_path) == []


def test_grant_rejects_permissions_outside_catalog(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="valid")

    with pytest.raises(LatchkeyPermissionFlowError):
        handler.grant(
            request_event_id="evt-abc",
            agent_id=AgentId(),
            host_id=HostId(),
            service_info=SLACK_SERVICE_INFO,
            granted_permissions=("not-a-real-permission",),
        )

    assert load_response_events(tmp_path) == []


def test_grant_re_grants_same_scope_routes_both_grants_through_gateway(tmp_path: Path) -> None:
    """Re-granting the same scope forwards BOTH grants to the gateway client.

    The handler does not merge or dedup locally; each grant POSTs the full
    permission set the user selected and lets the gateway extension replace
    the rule in place. Replace-not-append of the on-disk file is covered
    authoritatively in libs/mngr_latchkey's permissions_test.py.
    """
    fake_client = FakeLatchkeyGatewayClient()
    handler = build_handler_with_gateway_client(tmp_path, fake_client)
    agent_id = AgentId()
    host_id = HostId()

    handler.grant(
        request_event_id="evt-1",
        agent_id=agent_id,
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )
    handler.grant(
        request_event_id="evt-2",
        agent_id=agent_id,
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all", "slack-write-all"),
    )

    # Both grants target the same scope's rule, carrying the permissions the
    # user picked on each click; the gateway extension owns the replace merge.
    assert [call.rule_key for call in fake_client.set_calls] == ["slack-api", "slack-api"]
    assert fake_client.set_calls[0].granted_permissions == ("slack-read-all",)
    assert fake_client.set_calls[1].granted_permissions == ("slack-read-all", "slack-write-all")


# -- LatchkeyPermissionGrantHandler.grant: NEEDS_MANUAL_CREDENTIALS path --


def test_grant_refuses_when_browser_auth_unsupported_and_returns_set_example(tmp_path: Path) -> None:
    base_example = 'latchkey auth set coolify -H "Authorization: Bearer <token>"'
    handler = build_handler(
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
        service_info=SLACK_SERVICE_INFO,
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
    assert not (tmp_path / "mngr_report.jsonl").exists()


def test_grant_falls_back_to_generic_example_when_latchkey_omits_one(tmp_path: Path) -> None:
    handler = build_handler(
        tmp_path,
        credential_status="missing",
        auth_options_json=json.dumps(["set"]),
        set_credentials_example="",
    )

    result = handler.grant(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=SLACK_SERVICE_INFO,
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
    handler = build_handler(
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
        service_info=SLACK_SERVICE_INFO,
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
    handler = build_handler(
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
        service_info=SLACK_SERVICE_INFO,
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
    mngr_binary = make_recording_binary(tmp_path, "mngr", exit_code=0)
    handler = LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary)),
        services_catalog=build_slack_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
        gateway_client=build_fake_gateway_client(),
    )
    agent_id = AgentId()
    host_id = HostId()

    first = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )
    assert first.outcome == GrantOutcome.NEEDS_MANUAL_CREDENTIALS

    # User runs the suggested command -- modelled by writing the sentinel.
    sentinel.write_text("")

    second = handler.grant(
        request_event_id="evt-abc",
        agent_id=agent_id,
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
        granted_permissions=("slack-read-all",),
    )
    assert second.outcome == GrantOutcome.GRANTED
    assert second.response_event is not None


# -- LatchkeyPermissionGrantHandler.deny --


def test_deny_writes_response_event_without_touching_permissions_file(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="valid")
    agent_id = AgentId()
    host_id = HostId()

    handler.deny(
        request_event_id="evt-abc",
        agent_id=agent_id,
        scope=SLACK_SERVICE_INFO.scope,
        display_name=SLACK_SERVICE_INFO.display_name,
    )

    responses = load_response_events(tmp_path)
    assert len(responses) == 1
    assert responses[0].status == str(RequestStatus.DENIED)
    # No permissions file should have been created on either path.
    assert not permissions_path_for_host(tmp_path / "mngr_latchkey", host_id).exists()
    # The auth-browser binary must not have been invoked either.
    assert not (tmp_path / "auth_latchkey_report.jsonl").exists()


def test_deny_sends_mngr_message(tmp_path: Path) -> None:
    handler = build_handler(tmp_path, credential_status="valid")

    handler.deny(
        request_event_id="evt-abc",
        agent_id=AgentId(),
        scope=SLACK_SERVICE_INFO.scope,
        display_name=SLACK_SERVICE_INFO.display_name,
    )

    mngr_recording = read_recording(tmp_path / "mngr_report.jsonl")
    assert len(mngr_recording) == 1
    argv = mngr_recording[0]["argv"]
    assert isinstance(argv, list)
    assert "denied" in argv[2].lower()


def test_grant_calls_gateway_client_set_permission_and_delete_request(tmp_path: Path) -> None:
    """The handler routes the on-disk write through the gateway extension and clears the pending request."""
    fake_client = FakeLatchkeyGatewayClient()
    handler = build_handler_with_gateway_client(tmp_path, fake_client)
    host_id = HostId()

    result = handler.grant(
        request_event_id="evt-xyz",
        agent_id=AgentId(),
        host_id=host_id,
        service_info=SLACK_SERVICE_INFO,
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
    handler = build_handler_with_gateway_client(tmp_path, fake_client)

    handler.deny(
        request_event_id="evt-deny",
        agent_id=AgentId(),
        scope=SLACK_SERVICE_INFO.scope,
        display_name=SLACK_SERVICE_INFO.display_name,
    )

    assert fake_client.set_calls == ()
    assert fake_client.deleted_request_ids == ("evt-deny",)


# The gateway extension's merge semantics -- replacing a repeated scope's rule
# in place and preserving sibling top-level keys like ``schemas`` verbatim --
# are owned server-side by permissions.mjs and tested authoritatively in
# libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permissions_test.py
# (test_post_rule_replaces_existing_rule_for_same_scope and
# test_post_rule_preserves_other_top_level_keys). The desktop handler only
# forwards the grant, so it is asserted here via fake_client.set_calls rather
# than by re-checking a merged on-disk file (which would only exercise the
# in-process FakeLatchkeyGatewayClient's mirror of that JS behaviour).
