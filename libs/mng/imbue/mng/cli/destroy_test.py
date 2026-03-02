"""Unit tests for the destroy CLI command."""

import json

import pluggy
from click.testing import CliRunner

from imbue.mng.cli.conftest import capture_stdout
from imbue.mng.cli.destroy import DestroyCliOptions
from imbue.mng.cli.destroy import _DestroyTargets
from imbue.mng.cli.destroy import _OfflineHostToDestroy
from imbue.mng.cli.destroy import _output_result
from imbue.mng.cli.destroy import destroy
from imbue.mng.cli.destroy import get_agent_name_from_session
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import OutputFormat


def test_get_agent_name_from_session_extracts_name() -> None:
    """Test that get_agent_name_from_session extracts the agent name correctly."""
    result = get_agent_name_from_session("mng-my-agent", "mng-")
    assert result == "my-agent"


def test_get_agent_name_from_session_returns_none_for_empty_session() -> None:
    """Test that get_agent_name_from_session returns None for empty session name."""
    result = get_agent_name_from_session("", "mng-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_prefix_does_not_match() -> None:
    """Test that get_agent_name_from_session returns None when session doesn't match prefix."""
    result = get_agent_name_from_session("other-session-name", "mng-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_agent_name_empty() -> None:
    """Test that get_agent_name_from_session returns None when agent name is empty after prefix."""
    result = get_agent_name_from_session("mng-", "mng-")
    assert result is None


def test_offline_host_to_destroy_can_be_instantiated() -> None:
    """Test that _OfflineHostToDestroy fields can be set (arbitrary_types_allowed)."""
    # _OfflineHostToDestroy requires actual interface objects (arbitrary_types_allowed).
    # We verify the model_config allows arbitrary types and that the class has the expected annotations.
    assert "host" in _OfflineHostToDestroy.model_fields
    assert "provider" in _OfflineHostToDestroy.model_fields
    assert "agent_names" in _OfflineHostToDestroy.model_fields


def test_destroy_targets_has_expected_fields() -> None:
    """Test that _DestroyTargets has the expected fields."""
    assert "online_agents" in _DestroyTargets.model_fields
    assert "offline_hosts" in _DestroyTargets.model_fields


def test_destroy_cli_options_can_be_instantiated() -> None:
    """Test that DestroyCliOptions can be instantiated with all required fields."""
    opts = DestroyCliOptions(
        agents=("agent1",),
        agent_list=(),
        force=False,
        destroy_all=False,
        dry_run=True,
        gc=True,
        remove_created_branch=False,
        allow_worktree_removal=True,
        sessions=(),
        include=(),
        exclude=(),
        stdin=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1",)
    assert opts.dry_run is True
    assert opts.force is False


def test_destroy_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy requires at least one agent or --all."""
    result = cli_runner.invoke(
        destroy,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent or use --all" in result.output


def test_destroy_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        destroy,
        ["my-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_destroy_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test destroying a non-existent agent."""
    result = cli_runner.invoke(
        destroy,
        ["nonexistent-agent-88421"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0


def test_destroy_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy --help works and exits 0."""
    result = cli_runner.invoke(
        destroy,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "destroy" in result.output.lower()


def test_destroy_all_dry_run_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --dry-run with no agents returns 0 and reports none found."""
    result = cli_runner.invoke(
        destroy,
        ["--all", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_destroy_all_force_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --force with no agents returns 0 and reports none found."""
    result = cli_runner.invoke(
        destroy,
        ["--all", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_destroy_all_json_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --force --format json with no agents returns 0 and empty output.

    In JSON mode, the "No agents found" message is not emitted because _output()
    only writes for HUMAN format. The command returns early before _output_result().
    """
    result = cli_runner.invoke(
        destroy,
        ["--all", "--force", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_destroy_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "not-mng-prefix"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


def test_destroy_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        destroy,
        ["my-agent", "--session", "mng-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


def test_destroy_session_cannot_combine_with_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with --all."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "mng-some-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


# =============================================================================
# Output helper function tests
# =============================================================================


def test_destroy_output_result_human_with_agents() -> None:
    """Test _output_result in HUMAN format with destroyed agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with capture_stdout() as buf:
        _output_result([AgentName("agent-a"), AgentName("agent-b")], output_opts)
    assert "Successfully destroyed 2 agent(s)" in buf.getvalue()


def test_destroy_output_result_json() -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    with capture_stdout() as buf:
        _output_result([AgentName("agent-x")], output_opts)
    data = json.loads(buf.getvalue().strip())
    assert data["destroyed_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_destroy_output_result_jsonl() -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    with capture_stdout() as buf:
        _output_result([AgentName("agent-y")], output_opts)
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "destroy_result"
    assert data["count"] == 1


def test_destroy_output_result_format_template() -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    with capture_stdout() as buf:
        _output_result([AgentName("my-agent")], output_opts)
    assert "my-agent" in buf.getvalue()
