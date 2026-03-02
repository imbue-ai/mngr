"""Tests for the destroy CLI command."""

import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.create import create
from imbue.mng.cli.destroy import destroy
from imbue.mng.cli.destroy import get_agent_name_from_session
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_destroy_single_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying a single agent."""
    agent_name = f"test-destroy-single-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 435782",
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

        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, "--force"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0, f"Destroy failed: {destroy_result.output}"
        assert "Destroyed agent:" in destroy_result.output

        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be destroyed",
        )


@pytest.mark.tmux
def test_destroy_single_agent_via_session(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying a single agent using the --session option."""
    agent_name = f"test-destroy-session-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 435783",
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

        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        destroy_result = cli_runner.invoke(
            destroy,
            ["--session", session_name, "--force"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0, f"Destroy failed: {destroy_result.output}"
        assert "Destroyed agent:" in destroy_result.output

        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be destroyed",
        )


@pytest.mark.tmux
def test_destroy_with_confirmation(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying a stopped agent with confirmation prompt.

    Stops the tmux session before calling destroy so the agent is not running,
    since non-force destroy blocks running agents.
    """
    agent_name = f"test-destroy-confirm-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 679415",
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

        assert create_result.exit_code == 0
        assert tmux_session_exists(session_name)

        # Stop the tmux session so the agent is not running (lifecycle state: STOPPED)
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=True)
        wait_for(
            lambda: not tmux_session_exists(session_name),
            timeout=5.0,
            error_message="Expected tmux session to be killed before destroy",
        )

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name],
            input="y\n",
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0
        assert "Are you sure you want to continue?" in destroy_result.output


@pytest.mark.tmux
def test_destroy_blocks_running_agent_without_force(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroying a running agent without --force is blocked with expected message."""
    agent_name = f"test-destroy-blocked-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 934827",
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

        assert create_result.exit_code == 0
        assert tmux_session_exists(session_name)

        # Attempt to destroy without --force (answer "y" to confirmation)
        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name],
            input="y\n",
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0
        assert "is running" in destroy_result.output
        assert "--force" in destroy_result.output

        # Agent should still be running (not destroyed)
        assert tmux_session_exists(session_name)


def test_destroy_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying a non-existent agent."""
    result = cli_runner.invoke(
        destroy,
        ["nonexistent-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0


@pytest.mark.tmux
def test_destroy_prints_errors_if_any_identifier_not_found(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy fails if any specified identifier doesn't match an agent.

    When multiple agents are specified and some don't exist, the command should:
    1. Fail without destroying any agents
    2. Include all missing identifiers in the error message
    """
    agent_name = f"test-destroy-partial-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    nonexistent_name1 = "nonexistent-agent-897231"
    nonexistent_name2 = "nonexistent-agent-643892"

    with tmux_session_cleanup(session_name):
        # Create one real agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 782341",
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
        assert create_result.exit_code == 0
        assert tmux_session_exists(session_name)

        # Try to destroy the real agent plus two non-existent ones
        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, nonexistent_name1, nonexistent_name2, "--force"],
            obj=plugin_manager,
            catch_exceptions=True,
        )

        # Command does not fail (because of the "--force" flag), but reports errors
        assert destroy_result.exit_code == 0

        # Error message should include both missing agent names
        error_message = destroy_result.output
        assert nonexistent_name1 in error_message
        assert nonexistent_name2 in error_message

        # The existing agent should NOT have been destroyed
        assert tmux_session_exists(session_name), "Existing agent should not be destroyed when some identifiers fail"


@pytest.mark.tmux
def test_destroy_dry_run(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test dry-run mode for destroy command."""
    agent_name = f"test-destroy-dryrun-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 541286",
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

        assert create_result.exit_code == 0
        assert tmux_session_exists(session_name)

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, "--dry-run"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0
        assert "Would destroy:" in destroy_result.output

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message="Agent session should still exist after dry-run",
        )


@pytest.mark.tmux
def test_destroy_multiple_agents(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying multiple agents at once."""
    timestamp = int(time.time())
    agent_name1 = f"test-destroy-multi1-{timestamp}"
    agent_name2 = f"test-destroy-multi2-{timestamp}"
    session_name1 = f"{mng_test_prefix}{agent_name1}"
    session_name2 = f"{mng_test_prefix}{agent_name2}"

    with ExitStack() as stack:
        stack.enter_context(tmux_session_cleanup(session_name1))
        stack.enter_context(tmux_session_cleanup(session_name2))

        for agent_name in [agent_name1, agent_name2]:
            result = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name,
                    "--agent-cmd",
                    "sleep 892736",
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
            assert result.exit_code == 0

        wait_for(
            lambda: tmux_session_exists(session_name1),
            error_message=f"Expected tmux session {session_name1} to exist",
        )
        wait_for(
            lambda: tmux_session_exists(session_name2),
            error_message=f"Expected tmux session {session_name2} to exist",
        )

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name1, agent_name2, "--force"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0

        wait_for(
            lambda: not tmux_session_exists(session_name1) and not tmux_session_exists(session_name2),
            error_message="Expected both tmux sessions to be destroyed",
        )


# =============================================================================
# Tests for get_agent_name_from_session()
# =============================================================================


def test_get_agent_name_from_session_empty_session() -> None:
    """Test get_agent_name_from_session returns None for empty session name."""
    result = get_agent_name_from_session("", "mng-")
    assert result is None


def test_get_agent_name_from_session_wrong_prefix() -> None:
    """Test get_agent_name_from_session returns None when session doesn't match prefix."""
    result = get_agent_name_from_session("other-session", "mng-")
    assert result is None


def test_get_agent_name_from_session_success() -> None:
    """Test get_agent_name_from_session extracts agent name correctly."""
    result = get_agent_name_from_session("mng-my-agent", "mng-")
    assert result == "my-agent"


def test_get_agent_name_from_session_custom_prefix() -> None:
    """Test get_agent_name_from_session works with custom prefix."""
    result = get_agent_name_from_session("custom-prefix-agent-name", "custom-prefix-")
    assert result == "agent-name"


def test_get_agent_name_from_session_only_prefix() -> None:
    """Test get_agent_name_from_session returns None when session is just the prefix."""
    result = get_agent_name_from_session("mng-", "mng-")
    assert result is None


# =============================================================================
# Tests for --session CLI flag
# =============================================================================


def test_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        destroy,
        ["my-agent", "--session", "mng-some-agent", "--force"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


def test_session_cannot_combine_with_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with --all."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "mng-some-agent", "--all", "--force"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


def test_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "other-session-name", "--force"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


@pytest.mark.parametrize(
    "session_name,prefix,expected_agent",
    [
        ("mng-test-agent", "mng-", "test-agent"),
        ("mng-another", "mng-", "another"),
        ("prefix-foo", "prefix-", "foo"),
    ],
)
def test_get_agent_name_from_session_various_inputs(session_name: str, prefix: str, expected_agent: str) -> None:
    """Test get_agent_name_from_session with various valid inputs."""
    result = get_agent_name_from_session(session_name, prefix)
    assert result == expected_agent


# =============================================================================
# Tests for --remove-created-branch
# =============================================================================


def _git_branch_exists(repo_path: Path, branch_name: str) -> bool:
    """Check if a git branch exists in the repo."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "branch", "--list", branch_name],
        capture_output=True,
        text=True,
    )
    return branch_name in result.stdout


@pytest.mark.tmux
def test_destroy_remove_created_branch_deletes_branch(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --remove-created-branch deletes the git branch after destroying a worktree agent."""
    agent_name = f"test-rm-branch-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    branch_name = f"mng/{agent_name}-local"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 135790",
                "--source",
                str(temp_git_repo),
                "--no-connect",
                "--await-ready",
                "--worktree",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"
        assert _git_branch_exists(temp_git_repo, branch_name), f"Expected branch {branch_name} to exist after create"

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, "--force", "--remove-created-branch"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0, f"Destroy failed: {destroy_result.output}"
        assert "Destroyed agent:" in destroy_result.output
        assert f"Deleted branch: {branch_name}" in destroy_result.output
        assert not _git_branch_exists(temp_git_repo, branch_name), (
            f"Expected branch {branch_name} to be deleted after destroy --remove-created-branch"
        )


@pytest.mark.tmux
def test_destroy_without_remove_created_branch_leaves_branch(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy without --remove-created-branch leaves the git branch intact."""
    agent_name = f"test-keep-branch-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    branch_name = f"mng/{agent_name}-local"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 246801",
                "--source",
                str(temp_git_repo),
                "--no-connect",
                "--await-ready",
                "--worktree",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"
        assert _git_branch_exists(temp_git_repo, branch_name)

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, "--force"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0, f"Destroy failed: {destroy_result.output}"
        # Branch should still exist
        assert _git_branch_exists(temp_git_repo, branch_name), (
            f"Expected branch {branch_name} to still exist after destroy without --remove-created-branch"
        )


@pytest.mark.tmux
def test_destroy_remove_created_branch_graceful_when_no_branch(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --remove-created-branch is a no-op when agent has no created_branch_name."""
    agent_name = f"test-no-branch-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 357912",
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

        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        destroy_result = cli_runner.invoke(
            destroy,
            [agent_name, "--force", "--remove-created-branch"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert destroy_result.exit_code == 0, f"Destroy failed: {destroy_result.output}"
        assert "Destroyed agent:" in destroy_result.output
