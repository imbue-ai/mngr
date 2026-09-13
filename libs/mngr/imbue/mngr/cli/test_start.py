"""Integration tests for the start CLI command."""

import json
from collections.abc import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.start import start
from imbue.mngr.cli.stop import stop
from imbue.mngr.utils.testing import tmux_session_exists


@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_start_restart_running_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    mngr_test_prefix: str,
) -> None:
    """start --restart on a running agent should stop it and start it fresh.

    The timeout is raised because the sequential tmux create/stop/restart
    operations can exceed the default 10s on a loaded CI runner.
    """
    create_test_agent("restart-running-agent", "sleep 140101")
    session_name = f"{mngr_test_prefix}restart-running-agent"
    assert tmux_session_exists(session_name)

    result = cli_runner.invoke(
        start,
        ["restart-running-agent", "--restart"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Restarted agent: restart-running-agent" in result.output
    assert tmux_session_exists(session_name)


@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_start_restart_stopped_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    mngr_test_prefix: str,
) -> None:
    """start --restart on a stopped agent should simply start it.

    The timeout is raised because the four sequential tmux create/stop/restart/
    readiness operations can exceed the default 10s on a loaded CI runner.
    """
    create_test_agent("restart-stopped-agent", "sleep 140102")
    session_name = f"{mngr_test_prefix}restart-stopped-agent"

    # Stop the agent first
    stop_result = cli_runner.invoke(
        stop,
        ["restart-stopped-agent"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert stop_result.exit_code == 0
    assert not tmux_session_exists(session_name)

    # Restart the stopped agent
    result = cli_runner.invoke(
        start,
        ["restart-stopped-agent", "--restart"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Restarted agent: restart-stopped-agent" in result.output
    assert tmux_session_exists(session_name)


@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_start_reports_the_host_as_not_started_when_it_was_already_online(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
) -> None:
    """A start against an already-online host must report that it booted nothing.

    ``was_host_started`` is about the host, not the agent list beside it, and a
    named agent is started whatever state it is in -- so a start always reports
    agents. The local host is always online, which is what pins the two apart:
    one agent started, no host started. That is the reading a caller which
    dispatched a start to revive an unresponsive workspace acts on, since a
    start that booted nothing cannot explain what happens next.
    """
    create_test_agent("already-online-host-agent", "sleep 140103")

    result = cli_runner.invoke(
        start,
        ["already-online-host-agent", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["started_agents"] == ["already-online-host-agent"]
    assert data["was_host_started"] is False
