"""Integration tests for the list CLI command."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.create import create
from imbue.mngr.cli.list import list_command
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import wait_for_agent_session


def test_list_command_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command when no agents exist."""
    result = cli_runner.invoke(
        list_command,
        [],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_list_command_json_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with JSON format when no agents exist."""
    result = cli_runner.invoke(
        list_command,
        ["--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert '"agents": []' in result.output


@pytest.mark.tmux
def test_list_command_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command shows created agent."""
    agent_name = f"test-list-cli-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent first
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110001",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List agents
        result = cli_runner.invoke(
            list_command,
            [],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output


@pytest.mark.tmux
def test_list_command_json_format_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with JSON format shows agent data."""
    agent_name = f"test-list-json-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110002",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List agents in JSON format
        result = cli_runner.invoke(
            list_command,
            ["--format", "json"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert '"agents":' in result.output
        assert agent_name in result.output


@pytest.mark.tmux
def test_list_command_jsonl_format_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with JSONL format streams agent data."""
    agent_name = f"test-list-jsonl-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110003",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List agents in JSONL format
        result = cli_runner.invoke(
            list_command,
            ["--format", "jsonl"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        # JSONL format should have agent data as a single line
        assert agent_name in result.output


@pytest.mark.tmux
def test_list_command_with_include_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with include filter."""
    agent_name = f"test-list-filter-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110004",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with matching filter
        result = cli_runner.invoke(
            list_command,
            ["--include", f'name == "{agent_name}"'],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output


@pytest.mark.tmux
def test_list_command_with_exclude_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with exclude filter."""
    agent_name = f"test-list-exclude-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110005",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with exclusion filter
        result = cli_runner.invoke(
            list_command,
            ["--exclude", f'name == "{agent_name}"'],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name not in result.output


@pytest.mark.tmux
def test_list_command_with_host_provider_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with host.provider CEL filter.

    This test verifies that the standard CEL dot notation 'host.provider' works correctly.
    Nested dictionaries are automatically converted to CEL-compatible objects via json_to_cel().
    """
    agent_name = f"test-list-host-provider-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent (will be on local provider)
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110006",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with host.provider filter - should find the agent
        result = cli_runner.invoke(
            list_command,
            ["--include", 'host.provider == "local"'],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output

        # List with non-matching host.provider filter - should NOT find the agent
        result_no_match = cli_runner.invoke(
            list_command,
            ["--include", 'host.provider == "docker"'],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result_no_match.exit_code == 0
        assert agent_name not in result_no_match.output


@pytest.mark.tmux
def test_list_command_with_host_name_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with host.name CEL filter.

    Verifies that the standard CEL dot notation 'host.name' works in CEL filters.
    """
    agent_name = f"test-list-host-name-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110007",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with host.name filter - local host is named "localhost"
        result = cli_runner.invoke(
            list_command,
            ["--include", 'host.name == "localhost"'],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output


@pytest.mark.tmux
def test_list_command_with_basic_fields(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with basic field selection."""
    agent_name = f"test-list-fields-basic-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110008",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with specific fields
        result = cli_runner.invoke(
            list_command,
            ["--fields", "id,name"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "ID" in result.output
        assert "NAME" in result.output
        assert agent_name in result.output
        # Should not show default fields like STATE or STATUS
        assert "STATE" not in result.output
        assert "STATUS" not in result.output


@pytest.mark.tmux
def test_list_command_with_nested_fields(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with nested field selection."""
    agent_name = f"test-list-fields-nested-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110009",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with nested fields
        result = cli_runner.invoke(
            list_command,
            ["--fields", "name,host.name,host.provider_name"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "NAME" in result.output
        assert "HOST" in result.output
        assert "PROVIDER" in result.output
        assert agent_name in result.output
        assert "local" in result.output


@pytest.mark.tmux
def test_list_command_with_host_and_provider_fields(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with host.name and host.provider_name fields."""
    agent_name = f"test-list-fields-host-provider-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110010",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with host.name and host.provider_name fields
        result = cli_runner.invoke(
            list_command,
            ["--fields", "name,host.state,state,host.name,host.provider_name"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "NAME" in result.output
        assert "HOST STATE" in result.output
        assert "STATE" in result.output
        assert "HOST" in result.output
        assert "PROVIDER" in result.output
        assert agent_name in result.output
        # States should show in uppercase
        assert AgentLifecycleState.RUNNING.value in result.output or AgentLifecycleState.STOPPED.value in result.output


@pytest.mark.tmux
def test_list_command_with_invalid_fields(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with invalid field shows empty column."""
    agent_name = f"test-list-fields-invalid-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110011",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with invalid field
        result = cli_runner.invoke(
            list_command,
            ["--fields", "name,invalid_field"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        # Should not fail, just show empty column
        assert result.exit_code == 0
        assert "NAME" in result.output
        assert "INVALID_FIELD" in result.output
        assert agent_name in result.output


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_list_command_with_running_filter_alias(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --running filter alias."""
    agent_name = f"test-list-running-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create a running agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110012",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # Create the "active" file so the agent is considered RUNNING.
        # Without this file, the agent would be in WAITING state.
        host_dir = Path(os.environ["MNGR_HOST_DIR"])
        json_result = cli_runner.invoke(
            list_command,
            ["--format", "json"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        data = json.loads(json_result.output)
        agents = data["agents"]
        agent_id = next(a["id"] for a in agents if a["name"] == agent_name)
        # The host_dir is the mngr data directory directly
        active_file = host_dir / "agents" / agent_id / "active"
        active_file.write_text("")

        # List with --running should show the agent
        result = cli_runner.invoke(
            list_command,
            ["--running"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output


def test_list_command_with_stopped_filter_alias(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --stopped filter alias (no agents to find)."""
    # Without any stopped agents, this should return no agents
    result = cli_runner.invoke(
        list_command,
        ["--stopped"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # The host dir is isolated and empty, so --stopped matches nothing and the human
    # output is exactly the "No agents found" sentinel (see _emit_human_output /
    # _StreamingHumanRenderer.finish in list.py).
    assert "No agents found" in result.output


@pytest.mark.tmux
def test_list_command_with_local_filter_alias(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --local filter alias."""
    agent_name = f"test-list-local-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create a local agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110013",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with --local should show the agent
        result = cli_runner.invoke(
            list_command,
            ["--local"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output


# tmux session cleanup occasionally exceeds the 10s default.
@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_list_command_with_remote_filter_alias(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --remote filter alias (excludes local agents)."""
    agent_name = f"test-list-remote-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create a local agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110014",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # Positive control: with no provider filter, the local agent IS listed. Without
        # this, the --remote assertion below would pass even if --remote were broken and
        # silently returned nothing.
        unfiltered_result = cli_runner.invoke(
            list_command,
            [],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert unfiltered_result.exit_code == 0
        assert agent_name in unfiltered_result.output

        # List with --remote should NOT show the local agent
        result = cli_runner.invoke(
            list_command,
            ["--remote"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name not in result.output


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_list_command_with_limit(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --limit option.

    Note: The limit is applied after fetching all results from providers.
    This test verifies that only the specified number of agents are displayed.
    """
    agent_name_1 = f"test-list-limit-1-{uuid4().hex}"
    agent_name_2 = f"test-list-limit-2-{uuid4().hex}"
    session_name_1 = f"{mngr_test_prefix}{agent_name_1}"
    session_name_2 = f"{mngr_test_prefix}{agent_name_2}"

    with tmux_session_cleanup(session_name_1):
        with tmux_session_cleanup(session_name_2):
            # Create first agent
            create_result_1 = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_1,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110015",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_1.exit_code == 0
            wait_for_agent_session(session_name_1)

            # Create second agent
            create_result_2 = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_2,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110016",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_2.exit_code == 0
            wait_for_agent_session(session_name_2)

            # List with --limit 1 should show only one agent
            result = cli_runner.invoke(
                list_command,
                ["--limit", "1"],
                obj=plugin_manager,
                catch_exceptions=False,
            )

            assert result.exit_code == 0
            # Count how many of our test agents are shown
            # (only one should be shown)
            agent_count = sum(1 for name in [agent_name_1, agent_name_2] if name in result.output)
            assert agent_count == 1


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_list_command_with_limit_json_format(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --limit option in JSON format.

    Creates two agents and asserts --limit 1 caps the JSON output to a single agent.
    The JSON path is the batch (non-streaming) path, where the limit is applied
    deterministically after sorting (agents_to_display[: limit] in list.py), so the
    count is exactly 1 regardless of which agent sorts first.
    """
    agent_name_1 = f"test-list-limit-json-1-{uuid4().hex}"
    agent_name_2 = f"test-list-limit-json-2-{uuid4().hex}"
    session_name_1 = f"{mngr_test_prefix}{agent_name_1}"
    session_name_2 = f"{mngr_test_prefix}{agent_name_2}"

    with tmux_session_cleanup(session_name_1):
        with tmux_session_cleanup(session_name_2):
            create_result_1 = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_1,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110017",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_1.exit_code == 0
            wait_for_agent_session(session_name_1)

            create_result_2 = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_2,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110024",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_2.exit_code == 0
            wait_for_agent_session(session_name_2)

            # List with --limit 1 --format json
            result = cli_runner.invoke(
                list_command,
                ["--limit", "1", "--format", "json"],
                obj=plugin_manager,
                catch_exceptions=False,
            )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data["agents"]) == 1


@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_list_command_with_sort_by_name(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --sort option by name."""
    agent_name_a = f"aaa-list-sort-{uuid4().hex}"
    agent_name_z = f"zzz-list-sort-{uuid4().hex}"
    session_name_a = f"{mngr_test_prefix}{agent_name_a}"
    session_name_z = f"{mngr_test_prefix}{agent_name_z}"

    with tmux_session_cleanup(session_name_a):
        with tmux_session_cleanup(session_name_z):
            # Create agents in reverse alphabetical order
            create_result_z = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_z,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110018",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_z.exit_code == 0
            wait_for_agent_session(session_name_z)

            create_result_a = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_a,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110019",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_a.exit_code == 0
            wait_for_agent_session(session_name_a)

            # List sorted by name ascending
            result = cli_runner.invoke(
                list_command,
                ["--sort", "name"],
                obj=plugin_manager,
                catch_exceptions=False,
            )

            assert result.exit_code == 0
            # Agent 'aaa' should appear before 'zzz'
            pos_a = result.output.find(agent_name_a)
            pos_z = result.output.find(agent_name_z)
            assert pos_a != -1
            assert pos_z != -1
            assert pos_a < pos_z, "Agent 'aaa' should appear before 'zzz' in ascending order"


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_list_command_with_sort_descending(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --sort option in descending order."""
    agent_name_a = f"aaa-list-desc-{uuid4().hex}"
    agent_name_z = f"zzz-list-desc-{uuid4().hex}"
    session_name_a = f"{mngr_test_prefix}{agent_name_a}"
    session_name_z = f"{mngr_test_prefix}{agent_name_z}"

    with tmux_session_cleanup(session_name_a):
        with tmux_session_cleanup(session_name_z):
            # Create both agents
            create_result_a = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_a,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110020",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_a.exit_code == 0
            wait_for_agent_session(session_name_a)

            create_result_z = cli_runner.invoke(
                create,
                [
                    "--name",
                    agent_name_z,
                    "--type",
                    "command",
                    "--source",
                    str(temp_work_dir),
                    "--transfer=none",
                    "--no-connect",
                    "--no-ensure-clean",
                    "--",
                    "sleep",
                    "110021",
                ],
                obj=plugin_manager,
                catch_exceptions=False,
            )
            assert create_result_z.exit_code == 0
            wait_for_agent_session(session_name_z)

            # List sorted by name descending
            result = cli_runner.invoke(
                list_command,
                ["--sort", "name desc"],
                obj=plugin_manager,
                catch_exceptions=False,
            )

            assert result.exit_code == 0
            # Agent 'zzz' should appear before 'aaa'
            pos_a = result.output.find(agent_name_a)
            pos_z = result.output.find(agent_name_z)
            assert pos_a != -1
            assert pos_z != -1
            assert pos_z < pos_a, "Agent 'zzz' should appear before 'aaa' in descending order"


@pytest.mark.tmux
def test_list_command_with_provider_filter(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --provider filter."""
    agent_name = f"test-list-provider-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent on the local provider
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110022",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with --provider local - should find the agent
        result = cli_runner.invoke(
            list_command,
            ["--provider", "local"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output

        # List with --provider nonexistent - should not find any agents
        result_empty = cli_runner.invoke(
            list_command,
            ["--provider", "nonexistent"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result_empty.exit_code == 0
        assert agent_name not in result_empty.output
        assert "No agents found" in result_empty.output


def test_list_command_format_template_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --format template when no agents exist produces silent output."""
    result = cli_runner.invoke(
        list_command,
        ["--format", "{name}\\t{state}"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # Template mode should produce no output for empty results (scripting-friendly)
    assert "No agents found" not in result.output


@pytest.mark.tmux
def test_list_command_format_template_with_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --format template shows template-expanded output."""
    agent_name = f"test-list-template-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "sleep",
                "110023",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0
        wait_for_agent_session(session_name)

        # List with --format template string
        result = cli_runner.invoke(
            list_command,
            ["--format", "{name}\\t{state}"],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert agent_name in result.output
        # The output should contain a tab-separated line with agent name and state
        assert f"{agent_name}\t" in result.output


def test_list_command_format_template_invalid_syntax(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that an invalid format template produces a clear error."""
    result = cli_runner.invoke(
        list_command,
        ["--format", "{"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Invalid format template" in result.output


def test_list_command_format_json(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --format json."""
    result = cli_runner.invoke(
        list_command,
        ["--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert '"agents": []' in result.output


def test_list_command_format_jsonl(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test list command with --format jsonl emits no JSONL records when no agents exist.

    Unlike `gc`, the list command's JSONL path (`_list_jsonl` in list.py) only writes a
    line per agent (and per error); it does NOT emit a trailing "summary" event. With an
    isolated, empty host dir there are no agents and no errors, so no JSONL line is
    written at all.
    """
    result = cli_runner.invoke(
        list_command,
        ["--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # Every non-blank line of JSONL output must be a JSON object; with no agents there
    # should be none at all (in particular, no agent records and no error events).
    json_lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert json_lines == []
