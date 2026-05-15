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
    """Passing conflicting positional and --source values is rejected."""
    result = cli_runner.invoke(
        pair,
        ["agent-name", "--source", "/nonexistent/path/12345"],
        obj=plugin_manager,
    )
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


def test_pair_host_only_no_path_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`@HOST` (no agent, no path) is incomplete: there is no work_dir to fall back to."""
    result = cli_runner.invoke(
        pair,
        ["@localhost"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    # The error from determine_resolved_path mentions both "path" and "agent".
    assert "path" in result.output.lower()


def test_pair_host_with_nonexistent_path_fails_after_resolution(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`@HOST:PATH` resolves the host directly (no agent picker) and then validates the path.

    Using a path that doesn't exist ensures we exercise the @HOST:PATH wiring without
    actually starting a unison process.
    """
    result = cli_runner.invoke(
        pair,
        ["@localhost:/nonexistent/path/for-pair-test-99999"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower() or "no such" in result.output.lower()


def test_pair_host_with_nonexistent_host_fails_at_resolution(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """`@NONEXISTENT_HOST:PATH` fails at host resolution with a clear error."""
    result = cli_runner.invoke(
        pair,
        ["@nonexistent-host-for-pair-test-99999:/tmp"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "host" in result.output.lower()
