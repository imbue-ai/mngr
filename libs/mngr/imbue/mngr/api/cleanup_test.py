"""Unit tests for cleanup API functions."""

from pathlib import Path

import pytest

from imbue.mngr.api.cleanup import execute_cleanup
from imbue.mngr.api.cleanup import find_agents_for_cleanup
from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.data_types import CleanupResult
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CleanupAction
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_test_agent_details


def test_execute_cleanup_dry_run_destroy_populates_destroyed_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run destroy should list all agent names in destroyed_agents."""
    agents = [
        make_test_agent_details("agent-alpha"),
        make_test_agent_details("agent-beta"),
        make_test_agent_details("agent-gamma"),
    ]

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.destroyed_agents == [
        AgentName("agent-alpha"),
        AgentName("agent-beta"),
        AgentName("agent-gamma"),
    ]
    assert result.stopped_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_stop_populates_stopped_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run stop should list all agent names in stopped_agents."""
    agents = [
        make_test_agent_details("agent-one"),
        make_test_agent_details("agent-two"),
    ]

    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.STOP,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.stopped_agents == [
        AgentName("agent-one"),
        AgentName("agent-two"),
    ]
    assert result.destroyed_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_with_no_agents_returns_empty_result(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run with an empty agent list should return an empty result."""
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.destroyed_agents == []
    assert result.stopped_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_returns_cleanup_result_type(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Dry-run should return a CleanupResult instance."""
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=[make_test_agent_details("test-agent")],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert isinstance(result, CleanupResult)


# --- Integration tests with real local provider ---


@pytest.mark.tmux
def test_find_agents_for_cleanup_returns_matching_agents(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_for_cleanup should return agents matching include filters."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-find-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99001"),
        ),
    )
    local_host.start_agents([agent.id])

    try:
        agents = find_agents_for_cleanup(
            mngr_ctx=temp_mngr_ctx,
            include_filters=('name == "cleanup-find-test"',),
            exclude_filters=(),
            error_behavior=ErrorBehavior.CONTINUE,
        )

        assert len(agents) == 1
        assert agents[0].name == AgentName("cleanup-find-test")
    finally:
        local_host.destroy_agent(agent)


def test_find_agents_for_cleanup_returns_empty_when_no_match(
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agents_for_cleanup should return empty list when no agents match."""
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "nonexistent-agent-xyz"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert agents == []


@pytest.mark.tmux
def test_execute_cleanup_destroy_on_online_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """execute_cleanup with DESTROY action should destroy agents on an online host."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-destroy-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99002"),
        ),
    )
    local_host.start_agents([agent.id])

    # Find the agent via the API
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Destroy it (non-dry-run)
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.DESTROY,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert AgentName("cleanup-destroy-test") in result.destroyed_agents
    assert result.stopped_agents == []

    # Verify the agent no longer exists on the host
    remaining = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(remaining) == 0


@pytest.mark.tmux
def test_execute_cleanup_stop_on_online_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """execute_cleanup with STOP action should stop agents on an online host."""

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cleanup-stop-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 99003"),
        ),
    )
    local_host.start_agents([agent.id])

    # Wait for agent to be alive before stop (race: tmux may not have started the
    # sleep process yet when get_lifecycle_state is called immediately)
    wait_for(
        lambda: agent.get_lifecycle_state() in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
        error_message="Expected agent lifecycle state to be RUNNING or WAITING",
    )

    # Find the agent via the API
    agents = find_agents_for_cleanup(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "cleanup-stop-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Stop it (non-dry-run)
    result = execute_cleanup(
        mngr_ctx=temp_mngr_ctx,
        agents=agents,
        action=CleanupAction.STOP,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert AgentName("cleanup-stop-test") in result.stopped_agents
    assert result.destroyed_agents == []

    # Verify the agent is now stopped
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    # Clean up
    local_host.destroy_agent(agent)
