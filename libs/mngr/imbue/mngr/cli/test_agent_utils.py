"""Integration tests for agent_utils.

These tests create a real agent via the CLI, then exercise
select_agent_interactively_with_host and find_agent_for_command end-to-end.
The only thing monkeypatched is the urwid TUI (select_agent_interactively),
since it requires an interactive terminal. Everything else -- list_agents,
discover_hosts_and_agents, find_one_agent -- runs against real data on disk.
"""

import time

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.agent_utils import ensure_host_and_agent_started
from imbue.mngr.cli.agent_utils import ensure_host_started_and_resolve_agent
from imbue.mngr.cli.agent_utils import find_agent_for_command
from imbue.mngr.cli.agent_utils import select_agent_interactively_with_host
from imbue.mngr.cli.stop import stop
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost


@pytest.mark.tmux
def test_select_agent_interactively_with_host_returns_selected_agent(
    create_test_agent,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent, returns the (DiscoveredHost, DiscoveredAgent) tuple."""
    agent_name = f"test-select-agent-{int(time.time())}"
    create_test_agent(agent_name, "sleep 564738")

    # Monkeypatch only the TUI -- return the first agent from the list.
    monkeypatch.setattr(
        "imbue.mngr.cli.agent_utils.select_agent_interactively",
        lambda agents: agents[0],
    )

    result = select_agent_interactively_with_host(temp_mngr_ctx)

    assert result is not None
    host_ref, agent_ref = result
    assert isinstance(host_ref, DiscoveredHost)
    assert isinstance(agent_ref, DiscoveredAgent)
    assert agent_ref.agent_name == AgentName(agent_name)


@pytest.mark.tmux
def test_select_agent_interactively_with_host_returns_none_when_user_quits(
    create_test_agent,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent present, returns None when the TUI returns None (user quit)."""
    agent_name = f"test-select-quit-{int(time.time())}"
    create_test_agent(agent_name, "sleep 564739")

    monkeypatch.setattr(
        "imbue.mngr.cli.agent_utils.select_agent_interactively",
        lambda agents: None,
    )

    result = select_agent_interactively_with_host(temp_mngr_ctx)

    assert result is None


@pytest.mark.tmux
def test_find_agent_for_command_plus_resolve_finds_stopped_agent(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agent_for_command + ensure_host_started_and_resolve_agent works on a stopped agent.

    Regression test: commands like provision and rename need the host
    online but do not need the agent process running. Using the
    "resolve_agent" helper instead of "ensure_host_and_agent_started"
    leaves the agent state untouched.
    """
    agent_name = f"test-find-stopped-{int(time.time())}"
    create_test_agent(agent_name, "sleep 564740")

    # Stop the agent
    stop_result = cli_runner.invoke(stop, [agent_name], obj=plugin_manager, catch_exceptions=False)
    assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

    result = find_agent_for_command(
        mngr_ctx=temp_mngr_ctx,
        address=AgentAddress(agent=AgentName(agent_name)),
        host_filter=None,
    )

    assert result is not None
    host_ref, agent_ref = result
    agent, host = ensure_host_started_and_resolve_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=True,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(agent, AgentInterface)
    assert isinstance(host, OnlineHostInterface)
    assert agent.name == AgentName(agent_name)


@pytest.mark.tmux
def test_ensure_host_and_agent_started_raises_for_stopped_agent_without_auto_start(
    cli_runner: CliRunner,
    create_test_agent,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
) -> None:
    """ensure_host_and_agent_started raises for a stopped agent when allow_auto_start=False.

    Verifies the default behavior for commands that require the agent to
    be running (connect, capture): a stopped agent causes UserInputError
    when auto-start is disabled.
    """
    agent_name = f"test-find-stopped-err-{int(time.time())}"
    create_test_agent(agent_name, "sleep 564741")

    # Stop the agent
    stop_result = cli_runner.invoke(stop, [agent_name], obj=plugin_manager, catch_exceptions=False)
    assert stop_result.exit_code == 0, f"Stop failed with: {stop_result.output}"

    result = find_agent_for_command(
        mngr_ctx=temp_mngr_ctx,
        address=AgentAddress(agent=AgentName(agent_name)),
        host_filter=None,
    )
    assert result is not None
    host_ref, agent_ref = result

    with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
        ensure_host_and_agent_started(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=False,
            mngr_ctx=temp_mngr_ctx,
        )
