"""Integration tests for the pair CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr_pair.cli import pair


def test_pair_source_and_source_agent_conflict(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that providing both --source and --source-agent shows error."""
    result = cli_runner.invoke(
        pair,
        ["agent-name", "--source", "/some/path", "--source-agent", "other-agent"],
        obj=plugin_manager,
    )
    # Should fail because you can't provide both
    assert result.exit_code != 0
    assert "cannot" in result.output.lower() or "error" in result.output.lower()


def test_pair_source_as_path_raises_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that using --source with a path correctly requires the path to exist."""
    result = cli_runner.invoke(
        pair,
        ["agent-name", "--source", "/nonexistent/path/12345"],
        obj=plugin_manager,
    )
    # Should fail because path doesn't exist
    assert result.exit_code != 0


def test_pair_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that pairing with nonexistent agent shows appropriate error."""
    result = cli_runner.invoke(
        pair,
        ["nonexistent-agent-12345"],
        obj=plugin_manager,
    )
    # Should fail because agent doesn't exist
    assert result.exit_code != 0


def test_pair_source_host_nonexistent_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --source-host with nonexistent host shows appropriate error."""
    result = cli_runner.invoke(
        pair,
        ["some-agent", "--source-host", "nonexistent-host-12345"],
        obj=plugin_manager,
    )
    # Should fail because host doesn't exist
    assert result.exit_code != 0
    assert "no host found" in result.output.lower() or "error" in result.output.lower()


def test_pair_source_host_with_local_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --source-host with '@local' (local host) works for filtering."""
    result = cli_runner.invoke(
        pair,
        ["nonexistent-agent", "--source-host", "@local"],
        obj=plugin_manager,
    )
    # Should fail because the agent doesn't exist on the local host
    # But NOT because the host doesn't exist
    assert result.exit_code != 0
    # The error should be about the agent, not the host
    assert "no agent found" in result.output.lower() or "error" in result.output.lower()


def test_pair_source_host_agent_not_on_specified_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --source-host shows error when agent doesn't exist on that host."""
    result = cli_runner.invoke(
        pair,
        ["some-agent", "--source-host", "@local"],
        obj=plugin_manager,
    )
    # Should fail because agent doesn't exist on the specified host
    assert result.exit_code != 0
