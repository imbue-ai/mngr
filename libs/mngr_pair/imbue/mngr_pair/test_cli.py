"""Integration tests for the pair CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr_pair.cli import pair


def test_pair_source_and_source_agent_conflict_is_rejected(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Specifying --source and --source-agent with different agents is rejected.

    The conflict is only detected when --source actually carries an agent that
    differs from --source-agent, so this passes two distinct agent names.
    """
    result = cli_runner.invoke(
        pair,
        ["--source", "agent-a", "--source-agent", "agent-b"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both --source and --source-agent" in result.output


def test_pair_source_and_source_agent_agreeing_does_not_conflict(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--source and --source-agent naming the same agent must NOT raise the conflict.

    This pins the other branch of the equality check: when the two agree, the
    command proceeds to agent resolution (and fails there, not on the conflict).
    """
    result = cli_runner.invoke(
        pair,
        ["--source", "agent-same", "--source-agent", "agent-same"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both --source and --source-agent" not in result.output
    assert "Could not find agent with ID or name: agent-same" in result.output


def test_pair_source_without_agent_requires_an_agent_to_be_specified(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """A path-only --source carries no agent, so the command reports a missing agent.

    (Non-interactive invocation cannot prompt, so it errors rather than asking.)
    """
    result = cli_runner.invoke(
        pair,
        ["--source", "/nonexistent/path/12345"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No agent specified" in result.output


def test_pair_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Pairing with a nonexistent agent reports that the agent could not be found."""
    result = cli_runner.invoke(
        pair,
        ["nonexistent-agent-12345"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Could not find agent with ID or name: nonexistent-agent-12345" in result.output


def test_pair_source_host_nonexistent_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--source-host with a nonexistent host reports that no host matched."""
    result = cli_runner.invoke(
        pair,
        ["some-agent", "--source-host", "nonexistent-host-12345"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No hosts found matching nonexistent-host-12345" in result.output


def test_pair_source_host_localhost_resolves_then_fails_on_missing_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--source-host localhost resolves the host; the failure is about the agent.

    This demonstrates that the local host filter works: the error is the
    agent-not-found message, not a host-resolution error.
    """
    result = cli_runner.invoke(
        pair,
        ["nonexistent-agent", "--source-host", "localhost"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Could not find agent with ID or name: nonexistent-agent" in result.output
    assert "No hosts found" not in result.output
