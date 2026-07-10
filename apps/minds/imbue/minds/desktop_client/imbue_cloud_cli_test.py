import json
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import _CONNECTOR_URL_SUBPROCESS_ENV
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
        cli._expect_success(result, "tunnels list")  # noqa: SLF001 - exercising the private error-surfacing path

    message = str(exc_info.value)
    assert "Traceback" not in message
    assert "httpx.ConnectError" not in message
    assert "tunnels list" in message
    # The full subprocess output is still available for server-side logging.
    assert "httpx.ConnectError" in exc_info.value.stderr


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
