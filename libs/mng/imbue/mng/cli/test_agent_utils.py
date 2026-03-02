"""Integration tests for agent_utils.

These tests create a real agent via the CLI, then exercise
select_agent_interactively_with_host and find_agent_for_command end-to-end.
The only thing monkeypatched is the urwid TUI (select_agent_interactively),
since it requires an interactive terminal. Everything else -- list_agents,
load_all_agents_grouped_by_host, find_and_maybe_start_agent_by_name_or_id --
runs against real data on disk.
"""

import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.agent_utils import select_agent_interactively_with_host
from imbue.mng.cli.stop import stop
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentName
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import tmux_session_cleanup


@pytest.mark.tmux
def test_select_agent_interactively_with_host_returns_selected_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent, returns the (AgentInterface, OnlineHostInterface) tuple."""
    agent_name = f"test-select-agent-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name, agent_cmd="sleep 564738"
        )

        # Monkeypatch only the TUI -- return the first agent from the list.
        monkeypatch.setattr(
            "imbue.mng.cli.agent_utils.select_agent_interactively",
            lambda agents: agents[0],
        )

        result = select_agent_interactively_with_host(temp_mng_ctx)

        assert result is not None
        agent, host = result
        assert isinstance(agent, AgentInterface)
        assert isinstance(host, OnlineHostInterface)
        assert agent.name == AgentName(agent_name)


@pytest.mark.tmux
def test_select_agent_interactively_with_host_returns_none_when_user_quits(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent present, returns None when the TUI returns None (user quit)."""
    agent_name = f"test-select-quit-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name, agent_cmd="sleep 564739"
        )

        monkeypatch.setattr(
            "imbue.mng.cli.agent_utils.select_agent_interactively",
            lambda agents: None,
        )

        result = select_agent_interactively_with_host(temp_mng_ctx)

        assert result is None


@pytest.mark.tmux
def test_find_agent_for_command_with_stopped_agent_and_skip_agent_state_check(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
) -> None:
    """find_agent_for_command with skip_agent_state_check finds a stopped agent.

    Regression test: provision needs the host online but does not need the
    agent process running. Without skip_agent_state_check, a stopped agent
    raises UserInputError.
    """
    agent_name = f"test-find-stopped-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name, agent_cmd="sleep 564740"
        )

        # Stop the agent
        stop_result = cli_runner.invoke(stop, [agent_name], obj=plugin_manager, catch_exceptions=False)
        assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

        # With skip_agent_state_check=True, should find the stopped agent
        result = find_agent_for_command(
            mng_ctx=temp_mng_ctx,
            agent_identifier=agent_name,
            command_usage="test",
            host_filter=None,
            is_start_desired=True,
            skip_agent_state_check=True,
        )

        assert result is not None
        agent, host = result
        assert isinstance(agent, AgentInterface)
        assert isinstance(host, OnlineHostInterface)
        assert agent.name == AgentName(agent_name)


@pytest.mark.tmux
def test_find_agent_for_command_raises_for_stopped_agent_without_skip(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    temp_mng_ctx: MngContext,
) -> None:
    """find_agent_for_command without skip_agent_state_check raises for stopped agent.

    Verifies the default behavior: when skip_agent_state_check is False
    (the default), a stopped agent causes UserInputError.
    """
    agent_name = f"test-find-stopped-err-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name, agent_cmd="sleep 564741"
        )

        # Stop the agent
        stop_result = cli_runner.invoke(stop, [agent_name], obj=plugin_manager, catch_exceptions=False)
        assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

        # Without skip_agent_state_check, should raise for stopped agent
        with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
            find_agent_for_command(
                mng_ctx=temp_mng_ctx,
                agent_identifier=agent_name,
                command_usage="test",
                host_filter=None,
            )
