"""Integration tests for the start CLI command."""

from collections.abc import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.start import start
from imbue.mngr.cli.stop import stop
from imbue.mngr.utils.testing import get_tmux_pane_pids
from imbue.mngr.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_start_restart_running_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    mngr_test_prefix: str,
) -> None:
    """start --restart on a running agent should stop it and start it fresh."""
    create_test_agent("restart-running-agent", "sleep 140101")
    session_name = f"{mngr_test_prefix}restart-running-agent"
    assert tmux_session_exists(session_name)
    pids_before = get_tmux_pane_pids(session_name)
    assert pids_before != ()

    result = cli_runner.invoke(
        start,
        ["restart-running-agent", "--restart"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Restarted agent: restart-running-agent" in result.output
    assert tmux_session_exists(session_name)
    # --restart must actually stop and re-launch: the pane process is replaced,
    # so the pane PID set must differ. A no-op that skipped the stop step would
    # keep the same PIDs and this assertion would catch it.
    pids_after = get_tmux_pane_pids(session_name)
    assert pids_after != ()
    assert set(pids_after).isdisjoint(pids_before)


@pytest.mark.tmux
@pytest.mark.flaky
def test_start_restart_stopped_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    mngr_test_prefix: str,
) -> None:
    """start --restart on a stopped agent should simply start it.

    Marked flaky: this exercises four sequential tmux agent-lifecycle operations
    (create, stop, restart, readiness wait) against a tight per-test timeout, so it
    can intermittently exceed the limit on a loaded CI runner.
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
