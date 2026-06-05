"""Integration tests for the interactive path of find_agent_by_address_or_interactively.

These tests require a real agent and an interactive context. The urwid
TUI itself is monkeypatched (it requires a real terminal), but
list_agents and find_one_agent run against real on-disk data.

Tests of the non-interactive paths and of the ensure_* helpers live in
agent_utils_test.py as plain unit tests.
"""

from uuid import uuid4

import click
import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost


@pytest.mark.tmux
def test_find_agent_by_address_or_interactively_returns_selected_agent(
    create_test_agent,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent and no address, returns the refs of the agent chosen by the TUI."""
    agent_name = f"test-select-agent-{uuid4().hex}"
    create_test_agent(agent_name, "sleep 564738")
    interactive_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().is_interactive, True))

    # Monkeypatch only the TUI. Before returning a choice, assert the created
    # agent was actually handed to the selector -- this guards the listing step,
    # so the test fails if list_agents returns the wrong set of candidates.
    def fake_select(agents):
        assert AgentName(agent_name) in {agent.name for agent in agents}
        return agents[0]

    monkeypatch.setattr(
        "imbue.mngr.cli.agent_utils.select_agent_interactively",
        fake_select,
    )

    host_ref, agent_ref = find_agent_by_address_or_interactively(
        mngr_ctx=interactive_ctx,
        address=None,
        host_filter=None,
    )

    assert isinstance(host_ref, DiscoveredHost)
    assert isinstance(agent_ref, DiscoveredAgent)
    assert agent_ref.agent_name == AgentName(agent_name)


@pytest.mark.tmux
def test_find_agent_by_address_or_interactively_raises_abort_when_user_quits(
    create_test_agent,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real agent present, raises click.Abort when the TUI returns None (user quit)."""
    agent_name = f"test-select-quit-{uuid4().hex}"
    create_test_agent(agent_name, "sleep 564739")
    interactive_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().is_interactive, True))

    monkeypatch.setattr(
        "imbue.mngr.cli.agent_utils.select_agent_interactively",
        lambda agents: None,
    )

    with pytest.raises(click.Abort):
        find_agent_by_address_or_interactively(
            mngr_ctx=interactive_ctx,
            address=None,
            host_filter=None,
        )
