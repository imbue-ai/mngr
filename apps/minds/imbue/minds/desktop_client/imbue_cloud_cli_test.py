import json
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.imbue_cloud_cli import _CONNECTOR_URL_SUBPROCESS_ENV
from imbue.minds.desktop_client.imbue_cloud_cli import _parse_conflict_stored
from imbue.minds.desktop_client.imbue_cloud_cli import _parse_stderr_error_message
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller


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
        cli._expect_success(result, "tunnels list")

    message = str(exc_info.value)
    assert "Traceback" not in message
    assert "httpx.ConnectError" not in message
    assert "tunnels list" in message
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


def test_parse_stderr_error_message_survives_surrounding_log_lines() -> None:
    body = json.dumps({"error": "the message", "error_class": "SomeError"}, indent=2)
    stderr = "2026-07-12 10:00:00 | WARNING | noisy {braced} log line\n" + body + "\ntrailing\n"
    assert _parse_stderr_error_message(stderr) == "the message"
    assert _parse_stderr_error_message("no json here\n") is None


def test_run_routes_through_mngr_caller_with_home_cwd_and_connector_env() -> None:
    """``ImbueCloudCli`` hands each subcommand to its ``MngrCaller`` prefixed with
    ``imbue_cloud``, runs it from ``$HOME``, and layers the connector URL onto the
    env so the plugin reaches the right backend."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps([])))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    cli.list_tunnels(account="owner@example.com")

    assert len(caller.recorded_calls) == 1
    recorded = caller.recorded_calls[0]
    assert recorded.argv == ("imbue_cloud", "tunnels", "list", "--account", "owner@example.com")
    assert recorded.cwd == Path.home()
    # The trailing slash is stripped so the plugin builds clean URLs.
    assert recorded.env_overrides == {_CONNECTOR_URL_SUBPROCESS_ENV: "https://connector.example"}


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


def test_find_tunnel_for_agent_uses_find_by_agent_subcommand() -> None:
    """``find_tunnel_for_agent`` delegates to the connector's O(1) ``tunnels
    find-by-agent`` lookup rather than listing every tunnel, and parses the
    returned tunnel JSON."""
    tunnel_json = {"tunnel_name": "owner--abc123", "tunnel_id": "t-1", "services": ["web"]}
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(tunnel_json)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    tunnel = cli.find_tunnel_for_agent(account="owner@example.com", agent_id="agent-abc123")

    assert tunnel is not None
    assert tunnel.tunnel_name == "owner--abc123"
    assert tunnel.services == ("web",)
    recorded = caller.recorded_calls[0]
    assert recorded.argv == (
        "imbue_cloud",
        "tunnels",
        "find-by-agent",
        "agent-abc123",
        "--account",
        "owner@example.com",
    )


def test_find_tunnel_for_agent_returns_none_when_plugin_emits_null() -> None:
    """When no tunnel exists for the agent, the plugin emits the JSON literal
    ``null`` and ``find_tunnel_for_agent`` maps it to ``None``."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(None)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    assert cli.find_tunnel_for_agent(account="owner@example.com", agent_id="agent-abc123") is None


def test_enable_sharing_malformed_output_error_omits_the_tunnel_token() -> None:
    """A malformed enable-sharing payload (well-formed tunnel half, broken
    service half) must not leak the cloudflared token into the exception
    message -- it reaches the sharing UI's 502 body and the logs."""
    body = {
        "tunnel": {"tunnel_name": "owner--abc123", "tunnel_id": "t-1", "token": "SECRET-TUNNEL-TOKEN"},
        "service": "nope",
    }
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=json.dumps(body)))
    cli = ImbueCloudCli(mngr_caller=caller, connector_url=AnyUrl("https://connector.example/"))

    with pytest.raises(ImbueCloudCliError) as exc_info:
        cli.enable_sharing(
            account="owner@example.com",
            agent_id="agent-abc123",
            service_name="web",
            service_url="http://localhost:8080",
            policy={"emails": ["a@b.com"]},
        )

    message = str(exc_info.value)
    assert "SECRET-TUNNEL-TOKEN" not in message
    # The shape (keys) stays in the message so the failure is still debuggable.
    assert "service" in message
    assert "tunnel" in message
