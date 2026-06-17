"""Integration tests for the start CLI command."""

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

    Same flaky profile as the stopped-agent variant: sequential tmux
    agent-lifecycle operations against a tight per-test timeout, so the default
    10s is too tight on a loaded CI runner. 30s matches the other multi-step CLI
    lifecycle tests; ``flaky`` still lets offload retry.
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

    Marked flaky with a raised timeout: this exercises four sequential tmux
    agent-lifecycle operations (create, stop, restart, readiness wait), one more
    than the running-agent variant, so the default 10s per-test timeout is too
    tight on a loaded CI runner (it timed out there). 30s matches the ceiling used
    by the other multi-step CLI lifecycle tests; ``flaky`` still lets offload retry.
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
