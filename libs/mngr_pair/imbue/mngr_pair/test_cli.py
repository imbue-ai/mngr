"""Integration tests for the pair CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr_pair.cli import pair


def test_pair_positional_and_named_source_conflict(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Providing different values for SOURCE and --source is an error."""
    result = cli_runner.invoke(
        pair,
        ["agent-name", "--source", "other-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "cannot" in result.output.lower() or "error" in result.output.lower()


def test_pair_source_as_path_raises_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Using --source with a path correctly requires the path to exist."""
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
    """Pairing with a nonexistent agent shows an appropriate error."""
    result = cli_runner.invoke(
        pair,
        ["nonexistent-agent-12345"],
        obj=plugin_manager,
    )
    # Should fail because agent doesn't exist
    assert result.exit_code != 0


def test_pair_nonexistent_agent_on_specific_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`AGENT@HOST` syntax pins the host and fails with a nonexistent agent."""
    result = cli_runner.invoke(
        pair,
        ["some-agent@localhost"],
        obj=plugin_manager,
    )
    # Should fail because agent doesn't exist on the specified host
    assert result.exit_code != 0


def test_pair_host_only_source_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`@HOST` (no agent) is incomplete and must be rejected with a clear message."""
    result = cli_runner.invoke(
        pair,
        ["@localhost"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "agent" in result.output.lower()


def test_pair_host_with_path_but_no_agent_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`@HOST:PATH` (no agent) is rejected: pair syncs through an agent."""
    result = cli_runner.invoke(
        pair,
        ["@localhost:/tmp"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "agent" in result.output.lower()
