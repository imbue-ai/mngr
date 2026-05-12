"""Unit tests for pull CLI command."""

from pathlib import Path
from typing import cast

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.find import materialize_agent
from imbue.mngr.cli.pull import PullCliOptions
from imbue.mngr.cli.pull import pull
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.main import cli
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


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


def _create_stopped_agent_with_ref(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_name: AgentName,
    command: CommandString,
) -> tuple[OnlineHostInterface, DiscoveredHost, DiscoveredAgent]:
    """Create an agent, stop it, and return the host plus discovered refs."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

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

    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = DiscoveredAgent(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )
    return local_host, host_ref, agent_ref


@pytest.mark.tmux
def test_materialize_agent_with_skip_agent_state_check_succeeds_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """skip_agent_state_check=True materializes a stopped agent without error."""
    agent_name = AgentName("stopped-find-test-agent")
    local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47293")
    )

    found_agent, found_host = materialize_agent(
        host_ref,
        agent_ref,
        temp_mngr_ctx,
        skip_agent_state_check=True,
    )
    assert found_agent.id == agent_ref.agent_id
    assert found_host.id == local_host.id


@pytest.mark.tmux
def test_materialize_agent_without_skip_raises_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Without skip_agent_state_check, materializing a stopped agent raises UserInputError."""
    agent_name = AgentName("stopped-find-test-agent-2")
    _local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47294")
    )

    with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
        materialize_agent(host_ref, agent_ref, temp_mngr_ctx)


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
