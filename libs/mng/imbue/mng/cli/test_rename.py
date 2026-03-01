import json
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.create import create
from imbue.mng.cli.rename import rename
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


def _create_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_name: str,
) -> Host:
    """Create an agent via the provider API without starting it (no tmux session)."""
    host = local_provider.get_host(HostName("localhost"))
    host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName(agent_name),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847293"),
        ),
    )
    return host


def test_rename_stopped_agent_updates_data_json(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming a stopped agent updates data.json."""
    agent_name = f"test-rename-stopped-{uuid4().hex}"
    new_name = f"test-renamed-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Rename failed: {result.output}"
    assert "Renamed agent:" in result.output
    assert new_name in result.output

    # Verify data.json was updated
    agents = host.get_agents()
    agent_names = [str(a.name) for a in agents]
    assert new_name in agent_names
    assert agent_name not in agent_names


@pytest.mark.tmux
def test_rename_running_agent_renames_tmux_session(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming a running agent also renames the tmux session."""
    agent_name = f"test-rename-running-{uuid4().hex}"
    new_name = f"test-renamed-running-{uuid4().hex}"
    old_session_name = f"{mng_test_prefix}{agent_name}"
    new_session_name = f"{mng_test_prefix}{new_name}"

    with tmux_session_cleanup(old_session_name), tmux_session_cleanup(new_session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 493817",
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
        assert tmux_session_exists(old_session_name)

        rename_result = cli_runner.invoke(
            rename,
            [agent_name, new_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert rename_result.exit_code == 0, f"Rename failed: {rename_result.output}"
        assert "Renamed agent:" in rename_result.output

        # The old session should be gone, the new one should exist
        assert tmux_session_exists(new_session_name), "New tmux session should exist"
        assert not tmux_session_exists(old_session_name), "Old tmux session should not exist"


def test_rename_dry_run_does_not_change_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run shows what would happen without actually renaming."""
    agent_name = f"test-rename-dry-{uuid4().hex}"
    new_name = f"test-dry-renamed-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name, "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Dry-run failed: {result.output}"
    assert "Would rename" in result.output

    # Verify agent was NOT renamed
    agents = host.get_agents()
    agent_names = [str(a.name) for a in agents]
    assert agent_name in agent_names
    assert new_name not in agent_names


def test_rename_agent_not_found_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming a non-existent agent fails."""
    result = cli_runner.invoke(
        rename,
        ["nonexistent-agent-xyz", "new-name"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0


def test_rename_to_existing_name_fails(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming to an existing agent's name fails."""
    agent_name_1 = f"test-rename-dup1-{uuid4().hex}"
    agent_name_2 = f"test-rename-dup2-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name_1)
    _create_stopped_agent(local_provider, temp_work_dir, agent_name_2)

    result = cli_runner.invoke(
        rename,
        [agent_name_1, agent_name_2],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_rename_to_same_name_is_no_op(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that renaming to the same name is a no-op."""
    agent_name = f"test-rename-noop-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, agent_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "already named" in result.output


def test_rename_with_agent_id(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test renaming an agent using its ID instead of name."""
    agent_name = f"test-rename-byid-{uuid4().hex}"
    new_name = f"test-renamed-byid-{uuid4().hex}"

    host = _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    # Find the agent ID
    agents = host.get_agents()
    agent = next(a for a in agents if str(a.name) == agent_name)
    agent_id = str(agent.id)

    result = cli_runner.invoke(
        rename,
        [agent_id, new_name],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"Rename by ID failed: {result.output}"
    assert "Renamed agent:" in result.output

    # Verify the rename happened
    agents_after = host.get_agents()
    agent_names = [str(a.name) for a in agents_after]
    assert new_name in agent_names


def test_rename_json_output(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test rename with --format json produces valid JSON output."""
    agent_name = f"test-rename-json-{uuid4().hex}"
    new_name = f"test-renamed-json-{uuid4().hex}"

    _create_stopped_agent(local_provider, temp_work_dir, agent_name)

    result = cli_runner.invoke(
        rename,
        [agent_name, new_name, "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"JSON rename failed: {result.output}"
    output = json.loads(result.output.strip())
    assert output["old_name"] == agent_name
    assert output["new_name"] == new_name
    assert "agent_id" in output
