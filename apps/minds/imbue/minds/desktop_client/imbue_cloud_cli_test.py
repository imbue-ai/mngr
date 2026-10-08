import json
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthFailedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudLeaseActiveCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudWebLoginIncompleteCliError
from imbue.minds.desktop_client.imbue_cloud_cli import WEB_LOGIN_LISTEN_SECONDS
from imbue.minds.desktop_client.imbue_cloud_cli import WEB_LOGIN_SUBPROCESS_TIMEOUT_SECONDS
from imbue.minds.desktop_client.imbue_cloud_cli import WebLoginIncompleteReason
from imbue.minds.desktop_client.imbue_cloud_cli import _ACCOUNTS_URL_SUBPROCESS_ENV
from imbue.minds.desktop_client.imbue_cloud_cli import _CONNECTOR_URL_SUBPROCESS_ENV
from imbue.minds.desktop_client.imbue_cloud_cli import _parse_conflict_stored
from imbue.minds.desktop_client.imbue_cloud_cli import _parse_stderr_error_message
from imbue.minds.desktop_client.imbue_cloud_cli import classify_incomplete_web_login
from imbue.minds.desktop_client.supertokens_routes import _WEB_LOGIN_FLOW_TTL_SECONDS
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller


def test_expect_success_keeps_traceback_out_of_message_but_on_stderr() -> None:
    """A failing ``mngr imbue_cloud`` subprocess must not leak its stderr traceback
    into the exception *message* (routes surface ``str(exc)`` to API callers); the
    full output is preserved on ``.stderr`` for server-side logging/debugging."""
    cli = make_fake_imbue_cloud_cli()
    traceback_stderr = (
        "Traceback (most recent call last):\n"
        '  File "/x/httpx/_transports/default.py", line 101, in map_httpcore_exceptions\n'
        "httpx.ConnectError: [Errno -2] Name or service not known\n"
    )
    result = MngrCallResult(
        returncode=1,
        stdout="",
        stderr=traceback_stderr,
    )
    with pytest.raises(ImbueCloudCliError) as exc_info:
        cli._expect_success(result, "shares list")

    message = str(exc_info.value)
    assert "Traceback" not in message
    assert "httpx.ConnectError" not in message
    assert "shares list" in message
    # The full subprocess output is still available for server-side logging.
    assert "httpx.ConnectError" in exc_info.value.stderr


def test_expect_success_raises_typed_quota_error_with_server_message() -> None:
    """A structured quota refusal surfaces as the typed (terminal) error carrying the server's message."""
    cli = make_fake_imbue_cloud_cli()
    body = json.dumps(
        {
            "error": "Quota exceeded: this account allows 5 buckets and 5 are already in use.",
            "error_class": "ImbueCloudQuotaExceededError",
        },
        indent=2,
    )
    result = MngrCallResult(returncode=1, stdout="", stderr="some log line\n" + body + "\n")
    with pytest.raises(ImbueCloudQuotaExceededCliError) as exc_info:
        cli._expect_success(result, "bucket create")
    assert "allows 5 buckets" in str(exc_info.value)
    assert "bucket create" in str(exc_info.value)


def test_expect_success_raises_typed_auth_error_carrying_the_connector_status() -> None:
    """A rejected sign-in keeps the connector's verdict instead of collapsing to the exit-code message.

    ``mngr imbue_cloud auth signin`` exits 1 for a rejection, writing the
    connector's status + message as its JSON error body. The sign-in page shows
    real copy (and its "create one" sign-up path) only when that status
    survives, so it must not fall through to the generic branch.
    """
    cli = make_fake_imbue_cloud_cli()
    body = json.dumps(
        {
            "error": "Incorrect email or password",
            "error_class": "AuthFailed",
            "status": "WRONG_CREDENTIALS",
            "needs_email_verification": False,
        },
        indent=2,
    )
    result = MngrCallResult(returncode=1, stdout="", stderr="a log line first\n" + body + "\n")

    with pytest.raises(ImbueCloudAuthFailedCliError) as exc_info:
        cli._expect_success(result, "auth signin")

    assert exc_info.value.auth_status == "WRONG_CREDENTIALS"
    assert exc_info.value.auth_message == "Incorrect email or password"


def test_expect_success_auth_body_without_a_status_stays_a_plain_error() -> None:
    """The plugin's own malformed-response guard emits no status; it is not a connector verdict."""
    cli = make_fake_imbue_cloud_cli()
    body = json.dumps({"error": "Auth response missing required fields", "error_class": "AuthFailed"}, indent=2)
    result = MngrCallResult(returncode=1, stdout="", stderr=body + "\n")

    with pytest.raises(ImbueCloudAuthFailedCliError) as exc_info:
        cli._expect_success(result, "auth signin")

    assert exc_info.value.auth_status == "ERROR"
    assert exc_info.value.auth_message == "Auth response missing required fields"


def test_expect_success_unstructured_failure_is_not_reported_as_an_auth_verdict() -> None:
    """A crash / unreachable connector must not masquerade as a credential verdict."""
    cli = make_fake_imbue_cloud_cli()
    result = MngrCallResult(returncode=1, stdout="", stderr="httpx.ConnectError: Name or service not known\n")

    with pytest.raises(ImbueCloudCliError) as exc_info:
        cli._expect_success(result, "auth signin")

    assert not isinstance(exc_info.value, ImbueCloudAuthFailedCliError)


def test_web_login_timeouts_stay_coherent_and_cover_a_slow_browser_leg() -> None:
    """The three coupled web-login deadlines must stay ordered, with a listen window a slow sign-in fits in.

    The subprocess-kill deadline must exceed the listen window, so the plugin's own
    timeout message surfaces instead of a kill; the flow-status TTL must cover the
    whole subprocess lifetime, so the polling frontend never reports the flow
    expired while a sign-in is still in progress.
    """
    assert WEB_LOGIN_LISTEN_SECONDS >= 30 * 60
    assert WEB_LOGIN_SUBPROCESS_TIMEOUT_SECONDS > WEB_LOGIN_LISTEN_SECONDS
    assert _WEB_LOGIN_FLOW_TTL_SECONDS >= WEB_LOGIN_SUBPROCESS_TIMEOUT_SECONDS


def test_auth_login_asks_the_plugin_to_listen_for_the_whole_window() -> None:
    body = json.dumps({"user_id": "u-1", "email": "a@b.com"})
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=body))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    cli.auth_login()

    argv = caller.calls[0]
    assert argv[argv.index("--listen-timeout") + 1] == str(WEB_LOGIN_LISTEN_SECONDS)
    assert caller.recorded_calls[0].timeout == WEB_LOGIN_SUBPROCESS_TIMEOUT_SECONDS


def test_parse_stderr_error_message_survives_surrounding_log_lines() -> None:
    body = json.dumps({"error": "the message", "error_class": "SomeError"}, indent=2)
    stderr = "2026-07-12 10:00:00 | WARNING | noisy {braced} log line\n" + body + "\ntrailing\n"
    assert _parse_stderr_error_message(stderr) == "the message"
    assert _parse_stderr_error_message("no json here\n") is None


def test_sync_record_delete_raises_the_typed_lease_active_error_on_the_connectors_refusal() -> None:
    """The connector's tombstone-first 409 (``code: lease_active``) surfaces as its own error type."""
    body = json.dumps(
        {
            "error": (
                'Connector error 409: {"detail":{"code":"lease_active","message":"workspace record agent-1 '
                'still holds a cloud lease; destroy the workspace instead of removing its record"}}'
            ),
            "error_class": "ImbueCloudConnectorError",
        },
        indent=2,
    )
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout="", stderr="a log line\n" + body + "\n"))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudLeaseActiveCliError) as exc_info:
        cli.sync_record_delete("owner@example.com", "agent-1")

    assert "destroy it instead" in str(exc_info.value)
    assert "lease_active" in exc_info.value.stderr


def test_run_routes_through_mngr_caller_with_home_cwd_and_connector_env() -> None:
    """``ImbueCloudCli`` hands each subcommand to its ``MngrCaller`` prefixed with
    ``imbue_cloud``, runs it from ``$HOME``, and layers the connector URL onto the
    env so the plugin reaches the right backend."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps({"state": "none"})))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    cli.get_share_status(account="owner@example.com", host_id="host-abc")

    assert len(caller.recorded_calls) == 1
    recorded = caller.recorded_calls[0]
    assert recorded.argv == ("imbue_cloud", "shares", "status", "host-abc", "--account", "owner@example.com")
    assert recorded.cwd == Path.home()
    # The trailing slash is stripped so the plugin builds clean URLs.
    assert recorded.env_overrides == {_CONNECTOR_URL_SUBPROCESS_ENV: "https://connector.example"}


def test_run_passes_the_accounts_origin_env_when_configured() -> None:
    """The accounts origin rides into the subprocess env so ``auth login`` opens
    the hosted page on the origin where Google OAuth and session cookies work
    (a flow started on the connector host strands the nonce cookie and fails)."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps({"state": "none"})))
    cli = ImbueCloudCli(
        mngr_caller=caller,
        connector_url=AnyUrl("https://connector.example/"),
        accounts_base_url=AnyUrl("https://accounts.example.com/"),
    )

    cli.get_share_status(account="owner@example.com", host_id="host-abc")

    recorded = caller.recorded_calls[0]
    assert recorded.env_overrides == {
        _CONNECTOR_URL_SUBPROCESS_ENV: "https://connector.example",
        _ACCOUNTS_URL_SUBPROCESS_ENV: "https://accounts.example.com",
    }


def test_parse_conflict_stored_survives_surrounding_log_lines() -> None:
    """The indent-formatted error body may be preceded by log lines containing
    braces and followed by trailing output; the stored row must still parse."""
    body = json.dumps({"error": "conflict", "error_class": "X", "stored": {"host_id": "h1", "revision": 4}}, indent=2)
    stderr = (
        "2026-07-12 10:00:00 | WARNING | retrying {attempt 1} after HTTP 409\n" + body + "\nsome trailing log line\n"
    )
    assert _parse_conflict_stored(stderr) == {"host_id": "h1", "revision": 4}


def test_parse_conflict_stored_returns_none_without_a_stored_row() -> None:
    # The active-agent-conflict shape carries no stored row.
    body = json.dumps({"error": "another ACTIVE record exists", "stored": None}, indent=2)
    assert _parse_conflict_stored(body) is None
    # Brace-free stderr (no JSON document at all) parses to None too.
    assert _parse_conflict_stored("plain traceback text\nwithout any json\n") is None


def test_get_share_status_parses_the_per_relay_login_stamps() -> None:
    # `shares status` output carries per-relay login stamps (an ops signal);
    # ShareCliInfo forbids unknown keys, so it must model the field explicitly.
    body = {
        "host_id": "host-abc",
        "workspace_domain": "host-abc.owner1234.us1.shares.example",
        "region": "us1",
        "state": "active",
        "relay_endpoints": [{"relay_id": "relay-" + "1" * 16, "endpoint": "relay-us1.shares.example:7000"}],
        "relays": [{"relay_id": "relay-" + "1" * 16, "last_login_at": "2026-07-29 01:02:03+00:00"}],
        "last_tunnel_login_at": "2026-07-29 01:02:03+00:00",
        "cert_not_after": None,
    }
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(body)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    info = cli.get_share_status(account="owner@example.com", host_id="host-abc")

    assert info is not None
    assert [(entry.relay_id, entry.last_login_at) for entry in info.relays] == [
        ("relay-" + "1" * 16, "2026-07-29 01:02:03+00:00")
    ]


def test_create_share_malformed_output_error_omits_the_relay_token() -> None:
    """A malformed shares-create payload (a body with a relay token but no
    workspace_domain) must not leak the relay token into the exception
    message -- it reaches the sharing UI's error body and the logs."""
    body = {"host_id": "host-abc", "relay_token": "SECRET-RELAY-TOKEN"}
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(body)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudCliError) as exc_info:
        cli.create_share(account="owner@example.com", host_id="host-abc")

    message = str(exc_info.value)
    assert "SECRET-RELAY-TOKEN" not in message
    # The shape (keys) stays in the message so the failure is still debuggable.
    assert "relay_token" in message
    assert "host_id" in message


def test_list_share_relays_parses_the_relay_map() -> None:
    body = {
        "relays": {"us1": ["relay-us1.example:7000", "relay-us1b.example:7000"], "us2": ["relay-us2.example:7000"]}
    }
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(body)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    relays = cli.list_share_relays(account="owner@example.com")

    assert relays == {"us1": ("relay-us1.example:7000", "relay-us1b.example:7000"), "us2": ("relay-us2.example:7000",)}
    assert caller.recorded_calls[0].argv == ("imbue_cloud", "shares", "relays", "--account", "owner@example.com")


@pytest.mark.parametrize(
    "malformed_body",
    [
        {"unexpected": True},
        # The old region -> single-endpoint shape: a non-list per-region value.
        {"relays": {"us1": "relay-us1.example:7000"}},
    ],
)
def test_list_share_relays_raises_on_malformed_output(malformed_body: dict[str, object]) -> None:
    """A body without a relays map (or without an endpoint list per region) is a broken plugin contract."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(malformed_body)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudCliError, match="Malformed shares relays output"):
        cli.list_share_relays(account="owner@example.com")


def test_auth_resend_verification_reports_cooldown_suppression() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=json.dumps({"sent": False, "email": "a@b.com"}))
    )
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    assert cli.auth_resend_verification("a@b.com") is False
    assert caller.recorded_calls[0].argv == ("imbue_cloud", "auth", "resend-verification", "--account", "a@b.com")


def test_auth_resend_verification_raises_on_malformed_output() -> None:
    """A body without a 'sent' bool is a broken plugin contract, not a cooldown suppression."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps({"email": "a@b.com"})))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudCliError, match="Malformed auth resend-verification output"):
        cli.auth_resend_verification("a@b.com")


def test_auth_is_email_verified_reads_the_verdict() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=json.dumps({"verified": True, "email": "a@b.com"}))
    )
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    assert cli.auth_is_email_verified("a@b.com") is True
    assert caller.recorded_calls[0].argv == ("imbue_cloud", "auth", "is-verified", "--account", "a@b.com")


def test_auth_is_email_verified_raises_on_malformed_output() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps({"email": "a@b.com"})))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudCliError, match="Malformed auth is-verified output"):
        cli.auth_is_email_verified("a@b.com")


def test_expect_success_raises_typed_email_not_verified_error_with_the_email() -> None:
    """A structured verification refusal surfaces typed, carrying the address the link goes to."""
    cli = make_fake_imbue_cloud_cli()
    body = json.dumps(
        {
            "error": "This action requires a verified email address (alice@example.com).",
            "error_class": "ImbueCloudEmailNotVerifiedError",
            "code": "email_not_verified",
            "email": "alice@example.com",
        },
        indent=2,
    )
    result = MngrCallResult(returncode=1, stdout="", stderr="a log line first\n" + body + "\n")

    with pytest.raises(ImbueCloudEmailNotVerifiedCliError) as exc_info:
        cli._expect_success(result, "account set-plan")

    assert exc_info.value.email == "alice@example.com"
    assert "verified email" in str(exc_info.value)


def test_show_machine_parses_the_sizes_payload_and_records_the_argv() -> None:
    payload = {
        "host_db_id": "row-1",
        "host_id": "host-" + "a" * 32,
        "host_name": "sunny",
        "status": "stopped",
        "memory_units": 8,
        "target_memory_units": 16,
        "disk_gb": 28,
        "target_disk_gb": None,
        "is_restart_needed_to_apply": True,
    }
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(payload)))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    machine = cli.show_machine("owner@example.com", "host-" + "a" * 32)

    assert machine is not None
    assert machine.memory_units == 8
    assert machine.target_memory_units == 16
    assert machine.disk_gb == 28
    assert machine.is_restart_needed_to_apply is True
    assert caller.calls[0][:3] == ["imbue_cloud", "machines", "show"]


def test_list_machines_parses_the_account_listing_with_its_stop_kinds() -> None:
    payload = [
        {"host_db_id": "row-1", "host_id": "host-" + "a" * 32, "host_name": "sunny", "status": "stopped"},
        {
            "host_db_id": "row-2",
            "host_id": "host-" + "b" * 32,
            "host_name": "held",
            "status": "stopped",
            "stop_kind": "maintenance",
        },
    ]
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(payload)))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    machines = cli.list_machines("owner@example.com")

    assert [(machine.host_id, machine.stop_kind) for machine in machines] == [
        ("host-" + "a" * 32, None),
        ("host-" + "b" * 32, "maintenance"),
    ]
    assert caller.calls[0][:4] == ["imbue_cloud", "machines", "show", "--account"]


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        ({"machines": []}, "listing: Input should be a valid list"),
        (
            [
                {"host_db_id": "row-1", "host_id": "host-" + "a" * 32, "host_name": "sunny", "status": "stopped"},
                "held",
            ],
            "1: Input should be a valid dictionary",
        ),
        ([{"host_db_id": "row-1", "host_id": "host-" + "a" * 32, "status": "stopped"}], "0.host_name: Field required"),
    ],
)
def test_list_machines_raises_rather_than_reading_an_unexpected_shape_as_an_empty_account(
    payload: object, expected_detail: str
) -> None:
    # The stop-kind tracker keeps a hold it could not re-read only when the
    # listing raises; an empty (or shortened) list would clear it.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(payload)))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    with pytest.raises(ImbueCloudCliError, match=expected_detail):
        cli.list_machines("owner@example.com")


def test_show_machine_returns_none_when_the_invocation_fails() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout="", stderr="NotFound"))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    assert cli.show_machine("owner@example.com", "host-" + "b" * 32) is None


def test_sync_records_pull_reads_the_records_and_shared_agent_ids_object() -> None:
    agent_id = "agent-" + "a" * 32
    payload = {"records": [{"agent_id": agent_id, "revision": 1}, "not-a-record"], "shared_agent_ids": [agent_id, 7]}
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(payload)))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    pulled = cli.sync_records_pull("owner@example.com")

    assert pulled.records == ({"agent_id": agent_id, "revision": 1},)
    assert pulled.shared_agent_ids == (agent_id,)
    assert caller.calls[0][:4] == ["imbue_cloud", "sync", "records", "pull"]


def test_sync_records_pull_reads_a_listing_without_the_shared_marker_as_nothing_shared() -> None:
    """The records object of a CLI too old to report shared_agent_ids still parses."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=json.dumps({"records": [{"agent_id": "x"}]}))
    )
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    pulled = cli.sync_records_pull("owner@example.com")

    assert pulled.records == ({"agent_id": "x"},)
    assert pulled.shared_agent_ids == ()


def test_sync_records_pull_raises_on_an_unexpected_shape() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps([{"agent_id": "x"}])))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    with pytest.raises(ImbueCloudCliError, match="sync records pull"):
        cli.sync_records_pull("owner@example.com")


def _plugin_failure(error_class: str) -> MngrCallResult:
    body = json.dumps({"error": f"plugin said {error_class}", "error_class": error_class}, indent=2)
    return MngrCallResult(returncode=1, stdout="", stderr="Open this URL in your browser\n" + body + "\n")


@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (_plugin_failure("LoginTimeout"), WebLoginIncompleteReason.TIMED_OUT),
        (
            MngrCallResult(returncode=-1, is_timed_out=True, stderr="timed out after 630s"),
            WebLoginIncompleteReason.TIMED_OUT,
        ),
        (_plugin_failure("LoginStateMismatch"), WebLoginIncompleteReason.CALLBACK_REJECTED),
        (_plugin_failure("LoginCallbackMissingCode"), WebLoginIncompleteReason.CALLBACK_REJECTED),
        (_plugin_failure("ImbueCloudDeviceCodeRefusedError"), WebLoginIncompleteReason.CODE_REFUSED),
        # An unreachable connector is a real connection problem, not an incomplete sign-in.
        (_plugin_failure("ImbueCloudAuthError"), None),
        (MngrCallResult(returncode=1, stdout="", stderr="Traceback (most recent call last):\n"), None),
        (MngrCallResult(returncode=0, stdout=json.dumps({"email": "a@b.com"}), stderr=""), None),
    ],
)
def test_classify_incomplete_web_login(
    result: MngrCallResult, expected_reason: WebLoginIncompleteReason | None
) -> None:
    assert classify_incomplete_web_login(result) == expected_reason


def test_auth_login_raises_the_typed_incomplete_error_for_a_refused_code() -> None:
    caller = RecordingMngrCaller(result=_plugin_failure("ImbueCloudDeviceCodeRefusedError"))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudWebLoginIncompleteCliError) as exc_info:
        cli.auth_login()

    assert exc_info.value.reason == WebLoginIncompleteReason.CODE_REFUSED
    assert "plugin said ImbueCloudDeviceCodeRefusedError" in str(exc_info.value)


# The invitation surface (specs/inviting-granted-visitors/spec.md)


def _refusal_result(code: str) -> MngrCallResult:
    body = json.dumps(
        {"error": f"refused: {code}", "error_class": "ImbueCloudShareRefusedError", "code": code}, indent=2
    )
    return MngrCallResult(returncode=1, stdout="", stderr=body)


def test_push_share_grants_hands_the_document_over_a_temp_file_and_reads_the_count(tmp_path: Path) -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=json.dumps({"host_id": "host-1", "appended": 3}))
    )
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)
    document = {"workspace": {"users": [], "emails": ["bob@example.com"], "email_domains": []}, "services": {}}

    appended = cli.push_share_grants(account="owner@example.com", host_id="host-1", document=document)

    assert appended == 3
    (argv,) = caller.calls
    assert argv[:4] == ["imbue_cloud", "shares", "push-grants", "host-1"]
    document_path = Path(argv[argv.index("--document-file") + 1])
    assert not document_path.exists()


def test_push_share_grants_and_invite_raise_the_typed_refusal_with_its_code() -> None:
    caller = RecordingMngrCaller(result=_refusal_result("grants_out_of_date"))
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    with pytest.raises(ImbueCloudShareRefusedCliError) as invite_exc:
        cli.invite_grantee(
            account="owner@example.com",
            host_id="host-1",
            user_id=None,
            email="bob@example.com",
            app="notes",
            link="https://notes-x.host-1.owner.us1.shares.example/",
            workspace_name="alpha",
            app_display_name="Notes",
        )
    with pytest.raises(ImbueCloudShareRefusedCliError) as push_exc:
        cli.push_share_grants(account="owner@example.com", host_id="host-1", document={})

    assert invite_exc.value.code == "grants_out_of_date"
    assert push_exc.value.code == "grants_out_of_date"
    invite_argv = caller.calls[0]
    assert invite_argv[:4] == ["imbue_cloud", "shares", "invite", "host-1"]
    assert (
        "--email" in invite_argv
        and "--app" in invite_argv
        and "--link" in invite_argv
        and "--workspace-name" in invite_argv
        and invite_argv[invite_argv.index("--app-display-name") + 1] == "Notes"
    )
    assert "--user-id" not in invite_argv


def test_invite_parses_the_outcome_and_outcomes_parse_each_entry() -> None:
    caller = ScriptedMngrCaller(
        results=(
            MngrCallResult(
                returncode=0, stdout=json.dumps({"outcome": "invited", "invited_at": "2026-09-30T12:00:00+00:00"})
            ),
            MngrCallResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {"kind": "email", "value": "bob@example.com", "app": "system_interface", "outcome": "invited"},
                        {"kind": "user", "value": "u-2", "app": "notes", "outcome": None, "joined_at": None},
                    ]
                ),
            ),
        )
    )
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    result = cli.invite_grantee(
        account="owner@example.com",
        host_id="host-1",
        user_id="u-2",
        email=None,
        app=None,
        link=None,
        workspace_name=None,
        app_display_name=None,
    )
    outcomes = cli.list_invitation_outcomes(account="owner@example.com", host_id="host-1")

    assert result.outcome == "invited"
    assert result.invited_at == "2026-09-30T12:00:00+00:00"
    assert [entry.outcome for entry in outcomes] == ["invited", None]
    assert caller.calls[1][:4] == ["imbue_cloud", "shares", "invitation-outcomes", "host-1"]


def test_notification_preferences_round_trip_through_the_plugin() -> None:
    caller = ScriptedMngrCaller(
        results=(
            MngrCallResult(returncode=0, stdout=json.dumps({"email_enabled": True, "in_app_enabled": True})),
            MngrCallResult(returncode=0, stdout=json.dumps({"email_enabled": False, "in_app_enabled": True})),
        )
    )
    cli = ImbueCloudCli(connector_url=AnyUrl("https://connector.example"), mngr_caller=caller)

    before = cli.get_notification_preferences(account="owner@example.com")
    after = cli.set_notification_preferences(account="owner@example.com", email_enabled=False, in_app_enabled=True)

    assert before.email_enabled is True
    assert after.email_enabled is False
    assert caller.calls[0][:5] == ["imbue_cloud", "account", "notification-preferences", "show", "--account"]
    assert caller.calls[1][:6] == [
        "imbue_cloud",
        "account",
        "notification-preferences",
        "set",
        "--no-email",
        "--in-app",
    ]
