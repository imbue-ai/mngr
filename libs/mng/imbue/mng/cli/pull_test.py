"""Unit tests for pull CLI command."""

from pathlib import Path
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mng.cli.pull import PullCliOptions
from imbue.mng.cli.pull import pull
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.main import cli
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance


def test_pull_cli_options_can_be_instantiated() -> None:
    """Test that PullCliOptions can be instantiated with all required fields."""
    opts = PullCliOptions(
        source_pos=None,
        destination_pos=None,
        source=None,
        source_agent=None,
        source_host=None,
        source_path=None,
        destination=None,
        dry_run=False,
        stop=False,
        delete=False,
        sync_mode="files",
        exclude=(),
        uncommitted_changes="fail",
        target_branch=None,
        target=None,
        target_agent=None,
        target_host=None,
        target_path=None,
        stdin=False,
        include=(),
        include_gitignored=False,
        include_file=None,
        exclude_file=None,
        rsync_arg=(),
        rsync_args=None,
        branch=(),
        all_branches=False,
        tags=False,
        force_git=False,
        merge=False,
        rebase=False,
        uncommitted_source=None,
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
    assert opts.sync_mode == "files"
    assert opts.dry_run is False
    assert opts.delete is False
    assert opts.uncommitted_changes == "fail"


def test_pull_cli_options_has_all_fields() -> None:
    """Test that PullCliOptions has all expected fields."""
    assert hasattr(PullCliOptions, "__annotations__")
    annotations = PullCliOptions.__annotations__
    assert "source" in annotations
    assert "source_agent" in annotations
    assert "source_host" in annotations
    assert "source_path" in annotations
    assert "destination" in annotations
    assert "dry_run" in annotations
    assert "stop" in annotations
    assert "delete" in annotations
    assert "sync_mode" in annotations
    assert "exclude" in annotations
    assert "target_branch" in annotations


def test_pull_command_is_registered() -> None:
    """Test that pull command is registered in the CLI group."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "Pull files or git commits from an agent" in result.output


def test_pull_command_help_shows_options() -> None:
    """Test that pull --help shows all options."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "--source-agent" in result.output
    assert "--source-path" in result.output
    assert "--destination" in result.output
    assert "--dry-run" in result.output
    assert "--stop" in result.output
    assert "--delete" in result.output
    assert "--sync-mode" in result.output
    assert "--exclude" in result.output


def test_pull_command_sync_mode_choices() -> None:
    """Test that sync-mode shows valid choices."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pull", "--help"])
    assert result.exit_code == 0
    assert "files" in result.output
    assert "git" in result.output
    assert "full" in result.output


def test_find_agent_by_name_or_id_raises_for_empty_agents(temp_mng_ctx: MngContext) -> None:
    """Test that find_agent_by_name_or_id raises UserInputError for unknown agent."""
    agents_by_host: dict[HostReference, list[AgentReference]] = {}

    with pytest.raises(UserInputError, match="No agent found with name or ID"):
        find_and_maybe_start_agent_by_name_or_id("nonexistent-agent", agents_by_host, temp_mng_ctx, "test")


def test_find_agent_by_name_or_id_raises_agent_not_found_for_valid_id(temp_mng_ctx: MngContext) -> None:
    """Test that find_agent_by_name_or_id raises AgentNotFoundError for valid but nonexistent ID."""
    agents_by_host: dict[HostReference, list[AgentReference]] = {}

    # Generate a valid agent ID that doesn't exist in the empty agents_by_host
    nonexistent_id = AgentId.generate()

    with pytest.raises(AgentNotFoundError):
        find_and_maybe_start_agent_by_name_or_id(str(nonexistent_id), agents_by_host, temp_mng_ctx, "test")


@pytest.mark.tmux
def test_find_agent_by_name_or_id_raises_for_multiple_matches(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that find_agent_by_name_or_id raises for multiple agents with same name."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))

    agent_name = AgentName("duplicate-pull-test-agent")

    # Create two real agents with the same name on the local host
    agent1 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            agent_type=AgentTypeName("generic"),
            name=agent_name,
            command=CommandString("sleep 47291"),
        ),
    )
    agent2 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            agent_type=AgentTypeName("generic"),
            name=agent_name,
            command=CommandString("sleep 47292"),
        ),
    )

    # Build references matching the real host and agents
    host_ref = HostReference(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref1 = AgentReference(
        agent_id=agent1.id,
        agent_name=agent1.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref2 = AgentReference(
        agent_id=agent2.id,
        agent_name=agent2.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )

    agents_by_host = {host_ref: [agent_ref1, agent_ref2]}

    try:
        with pytest.raises(UserInputError, match="Multiple agents found"):
            find_and_maybe_start_agent_by_name_or_id(str(agent_name), agents_by_host, temp_mng_ctx, "test")
    finally:
        local_host.stop_agents([agent1.id, agent2.id])


def _create_stopped_agent_with_references(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_name: AgentName,
    command: CommandString,
) -> tuple[OnlineHostInterface, AgentReference, dict[HostReference, list[AgentReference]]]:
    """Create an agent, stop it, and return the host, agent ref, and agents_by_host mapping."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName("localhost")))

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            agent_type=AgentTypeName("generic"),
            name=agent_name,
            command=command,
        ),
    )

    # Stop the agent so it's in STOPPED state
    local_host.stop_agents([agent.id])

    host_ref = HostReference(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = AgentReference(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )
    agents_by_host = {host_ref: [agent_ref]}

    return local_host, agent_ref, agents_by_host


@pytest.mark.tmux
def test_find_agent_with_skip_agent_state_check_succeeds_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that skip_agent_state_check=True allows finding a stopped agent without error."""
    agent_name = AgentName("stopped-find-test-agent")
    local_host, agent_ref, agents_by_host = _create_stopped_agent_with_references(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47293")
    )

    # With skip_agent_state_check=True, should succeed even though agent is stopped
    found_agent, found_host = find_and_maybe_start_agent_by_name_or_id(
        str(agent_name), agents_by_host, temp_mng_ctx, "test", skip_agent_state_check=True
    )
    assert found_agent.id == agent_ref.agent_id
    assert found_host.id == local_host.id


@pytest.mark.tmux
def test_find_agent_without_skip_raises_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that without skip_agent_state_check, a stopped agent raises UserInputError."""
    agent_name = AgentName("stopped-find-test-agent-2")
    _local_host, _agent_ref, agents_by_host = _create_stopped_agent_with_references(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47294")
    )

    # Without skip_agent_state_check, should raise for stopped agent
    with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
        find_and_maybe_start_agent_by_name_or_id(str(agent_name), agents_by_host, temp_mng_ctx, "test")


def test_pull_target_branch_requires_git_mode(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --target-branch requires --sync-mode=git."""
    result = cli_runner.invoke(
        pull,
        ["nonexistent-pull-agent-222", "--target-branch", "main"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "--target-branch can only be used with --sync-mode=git" in result.output
