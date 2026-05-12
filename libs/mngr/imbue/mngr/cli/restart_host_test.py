"""Unit and integration tests for the restart-host CLI command."""

import subprocess
from collections.abc import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.restart_host import restart_host
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import tmux_session_exists


def test_restart_host_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """restart-host with no agent arg exits non-zero with usage error."""
    result = cli_runner.invoke(
        restart_host,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def _get_session_pids(session_name: str) -> set[str]:
    """Return the set of pane PIDs for a tmux session, or empty if it's gone."""
    result = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", f"={session_name}", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


@pytest.mark.tmux
def test_restart_host_bounces_local_agent_tmux_session(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
) -> None:
    """End-to-end: restart-host on a local agent kills and recreates the tmux session.

    On the local provider, ``provider.supports_shutdown_hosts`` is False, so
    restart-host falls back to ``stop_agents`` + ``start_agents`` on the
    OnlineHost. The tmux session's pane PIDs should change after the call,
    proving the agent was actually bounced rather than left untouched.
    """
    session_name = create_test_agent("restart-host-test-agent", "sleep 423423")

    wait_for(
        lambda: bool(_get_session_pids(session_name)),
        timeout=10.0,
        error_message=f"Expected pane PIDs for session {session_name} before restart",
    )
    pids_before = _get_session_pids(session_name)
    assert pids_before, "Session should have pane PIDs before restart"

    result = cli_runner.invoke(
        restart_host,
        ["restart-host-test-agent"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "Restarted host for agent: restart-host-test-agent" in result.output

    # After restart, the session should exist again, but with new pane PIDs
    # (the old tmux pane processes were SIGTERM'd and replaced by fresh ones).
    wait_for(
        lambda: tmux_session_exists(session_name),
        timeout=10.0,
        error_message=f"Expected session {session_name} to be recreated after restart",
    )
    wait_for(
        lambda: bool(_get_session_pids(session_name) - pids_before),
        timeout=10.0,
        error_message=(
            f"Expected new pane PIDs after restart-host for session {session_name}; "
            f"still seeing only the pre-restart PIDs {pids_before}"
        ),
    )
