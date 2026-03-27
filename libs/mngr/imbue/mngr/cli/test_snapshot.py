"""Integration tests for the snapshot CLI command."""

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.snapshot import snapshot
from imbue.mngr.utils.testing import get_short_random_string

# =============================================================================
# Tests with real local agents
# =============================================================================


@pytest.mark.tmux
def test_snapshot_create_local_agent_rejects_unsupported_provider(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create fails for a local agent (unsupported provider)."""
    agent_name = f"test-snap-create-{get_short_random_string()}"
    create_test_agent(agent_name)

    result = cli_runner.invoke(
        snapshot,
        ["create", agent_name],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "does not support snapshots" in result.output


@pytest.mark.tmux
def test_snapshot_create_dry_run_jsonl_resolves_local_agent(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run with --format jsonl outputs structured data on stdout."""
    agent_name = f"test-snap-dryrun-jsonl-{get_short_random_string()}"
    create_test_agent(agent_name)

    result = cli_runner.invoke(
        snapshot,
        ["create", agent_name, "--dry-run", "--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "dry_run" in result.output
    assert agent_name in result.output


# =============================================================================
# Tests without agents (lightweight)
# =============================================================================


def test_snapshot_create_all_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create --all succeeds when no agents are running."""
    result = cli_runner.invoke(
        snapshot,
        ["create", "--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_snapshot_list_all_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot list --all succeeds when no agents are running."""
    result = cli_runner.invoke(
        snapshot,
        ["list", "--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_snapshot_create_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot create for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["create", "nonexistent-agent-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


@pytest.mark.tmux
def test_snapshot_create_on_error_continue_reports_failure(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --on-error continue reports the error and exits 1 (doesn't crash)."""
    agent_name = f"test-snap-onerror-cont-{get_short_random_string()}"
    create_test_agent(agent_name)

    result = cli_runner.invoke(
        snapshot,
        ["create", agent_name, "--on-error", "continue"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "does not support snapshots" in result.output or "Failed to create" in result.output


@pytest.mark.tmux
def test_snapshot_create_on_error_abort_reports_failure(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --on-error abort also fails (with abort message)."""
    agent_name = f"test-snap-onerror-abort-{get_short_random_string()}"
    create_test_agent(agent_name)

    result = cli_runner.invoke(
        snapshot,
        ["create", agent_name, "--on-error", "abort"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Aborted" in result.output or "does not support" in result.output


def test_snapshot_create_mixed_identifier_classified_as_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that a positional arg not matching any agent is treated as a host identifier.

    The identifier is classified as a host (no agent match), and since the local
    provider only accepts "localhost" as a host name, it fails with "not found".
    """
    result = cli_runner.invoke(
        snapshot,
        ["create", "not-an-agent-or-host-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Agent or host not found" in result.output


def test_snapshot_list_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot list for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["list", "nonexistent-agent-99999"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_snapshot_destroy_nonexistent_agent_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that snapshot destroy for a nonexistent agent raises an error."""
    result = cli_runner.invoke(
        snapshot,
        ["destroy", "nonexistent-agent-99999", "--all-snapshots", "--force"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
