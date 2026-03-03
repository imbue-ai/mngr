"""Integration tests for the cleanup CLI command."""

import json
import time
from contextlib import ExitStack
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.cleanup import cleanup
from imbue.mng.cli.create import create
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists

# =============================================================================
# Tests with no agents (lightweight, no tmux)
# =============================================================================


def test_cleanup_dry_run_json_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run --yes --format json outputs valid JSON when no agents exist."""
    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    output = json.loads(result.output.strip())
    assert output["agents"] == []


def test_cleanup_dry_run_jsonl_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run --yes --format jsonl outputs valid JSONL when no agents exist."""
    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes", "--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    for line in lines:
        parsed = json.loads(line)
        assert "event" in parsed


def test_cleanup_stop_action_dry_run(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --stop --dry-run --yes works."""
    result = cli_runner.invoke(
        cleanup,
        ["--stop", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_with_older_than_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --older-than filter is accepted."""
    result = cli_runner.invoke(
        cleanup,
        ["--older-than", "7d", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_with_idle_for_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --idle-for filter is accepted."""
    result = cli_runner.invoke(
        cleanup,
        ["--idle-for", "1h", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_with_provider_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --provider filter is accepted."""
    result = cli_runner.invoke(
        cleanup,
        ["--provider", "local", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_with_agent_type_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --agent-type filter is accepted."""
    result = cli_runner.invoke(
        cleanup,
        ["--agent-type", "claude", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_with_combined_filters(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that multiple filters can be combined."""
    result = cli_runner.invoke(
        cleanup,
        ["--older-than", "7d", "--provider", "local", "--agent-type", "claude", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cleanup_alias_clean(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that the cleanup command works when invoked directly."""
    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


# =============================================================================
# Tests with real agents (create then cleanup)
# =============================================================================


def _create_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    agent_name: str,
    temp_work_dir: Path,
) -> None:
    """Create a local agent via the CLI. Asserts success."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            "sleep 728451",
            "--source",
            str(temp_work_dir),
            "--no-connect",
            "--await-ready",
            "--no-copy-work-dir",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"Create failed: {result.output}"


@pytest.mark.tmux
def test_cleanup_destroy_single_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cleanup --yes destroys a real agent."""
    agent_name = f"test-cleanup-destroy-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "destroyed" in cleanup_result.output.lower()

        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be destroyed by cleanup",
        )


@pytest.mark.tmux
def test_cleanup_dry_run_with_real_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cleanup --dry-run --yes lists agents but does not destroy them."""
    agent_name = f"test-cleanup-dryrun-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--dry-run", "--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "would destroy" in cleanup_result.output.lower()
        assert agent_name in cleanup_result.output

        # Agent should still exist after dry-run
        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message="Agent session should still exist after dry-run",
        )


@pytest.mark.tmux
def test_cleanup_stop_action_with_real_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cleanup --stop --yes stops a running agent."""
    agent_name = f"test-cleanup-stop-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--stop", "--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "stopped" in cleanup_result.output.lower()


@pytest.mark.tmux
def test_cleanup_destroy_multiple_agents(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cleanup --yes destroys multiple agents at once."""
    timestamp = int(time.time())
    agent_name1 = f"test-cleanup-multi1-{timestamp}"
    agent_name2 = f"test-cleanup-multi2-{timestamp}"
    session_name1 = f"{mng_test_prefix}{agent_name1}"
    session_name2 = f"{mng_test_prefix}{agent_name2}"

    with ExitStack() as stack:
        stack.enter_context(tmux_session_cleanup(session_name1))
        stack.enter_context(tmux_session_cleanup(session_name2))

        _create_agent(cli_runner, plugin_manager, agent_name1, temp_work_dir)
        _create_agent(cli_runner, plugin_manager, agent_name2, temp_work_dir)

        wait_for(
            lambda: tmux_session_exists(session_name1) and tmux_session_exists(session_name2),
            error_message="Expected both tmux sessions to exist",
        )

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "destroyed" in cleanup_result.output.lower()

        wait_for(
            lambda: not tmux_session_exists(session_name1) and not tmux_session_exists(session_name2),
            error_message="Expected both tmux sessions to be destroyed by cleanup",
        )


@pytest.mark.tmux
def test_cleanup_destroy_with_provider_filter_matches(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --provider local matches and destroys local agents."""
    agent_name = f"test-cleanup-provfilt-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--provider", "local", "--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "destroyed" in cleanup_result.output.lower()

        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be destroyed by cleanup with --provider local",
        )


@pytest.mark.tmux
def test_cleanup_destroy_with_provider_filter_excludes(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --provider nonexistent does not destroy local agents."""
    agent_name = f"test-cleanup-provexcl-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--provider", "nonexistent-provider-849271", "--yes"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        assert "no agents found" in cleanup_result.output.lower()

        # Agent should still exist (filter excluded it)
        assert tmux_session_exists(session_name), "Agent should not be destroyed when provider filter doesn't match"


@pytest.mark.tmux
def test_cleanup_destroy_json_output_with_real_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cleanup --yes --format json outputs structured result with real agent."""
    agent_name = f"test-cleanup-json-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        _create_agent(cli_runner, plugin_manager, agent_name, temp_work_dir)
        assert tmux_session_exists(session_name)

        cleanup_result = cli_runner.invoke(
            cleanup,
            ["--yes", "--format", "json"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert cleanup_result.exit_code == 0
        output = json.loads(cleanup_result.output.strip())
        assert agent_name in output["destroyed_agents"]
        assert output["destroyed_count"] >= 1

        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be destroyed",
        )
