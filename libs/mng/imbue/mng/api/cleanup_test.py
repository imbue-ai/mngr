"""Unit tests for cleanup API functions."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.api.cleanup import execute_cleanup
from imbue.mng.api.cleanup import find_agents_for_cleanup
from imbue.mng.api.create import CreateAgentOptions
from imbue.mng.api.data_types import CleanupResult
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CleanupAction
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import ProviderInstanceName


def _make_test_agent_info(name: str = "test-agent") -> AgentInfo:
    """Create a minimal AgentInfo for testing cleanup API functions."""
    return AgentInfo(
        id=AgentId.generate(),
        name=AgentName(name),
        type="generic",
        command=CommandString("sleep 100"),
        work_dir=Path("/tmp/test"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.RUNNING,
        host=HostInfo(
            id=HostId.generate(),
            name="test-host",
            provider_name=ProviderInstanceName("local"),
        ),
    )


def test_execute_cleanup_dry_run_destroy_populates_destroyed_agents(
    temp_mng_ctx: MngContext,
) -> None:
    """Dry-run destroy should list all agent names in destroyed_agents."""
    agents = [
        _make_test_agent_info("agent-alpha"),
        _make_test_agent_info("agent-beta"),
        _make_test_agent_info("agent-gamma"),
    ]

    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
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
    temp_mng_ctx: MngContext,
) -> None:
    """Dry-run stop should list all agent names in stopped_agents."""
    agents = [
        _make_test_agent_info("agent-one"),
        _make_test_agent_info("agent-two"),
    ]

    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
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
    temp_mng_ctx: MngContext,
) -> None:
    """Dry-run with an empty agent list should return an empty result."""
    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
        agents=[],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert result.destroyed_agents == []
    assert result.stopped_agents == []
    assert result.errors == []


def test_execute_cleanup_dry_run_returns_cleanup_result_type(
    temp_mng_ctx: MngContext,
) -> None:
    """Dry-run should return a CleanupResult instance."""
    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
        agents=[_make_test_agent_info("test-agent")],
        action=CleanupAction.DESTROY,
        is_dry_run=True,
        error_behavior=ErrorBehavior.ABORT,
    )

    assert isinstance(result, CleanupResult)


# --- Integration tests with real local provider ---


@pytest.mark.tmux
def test_find_agents_for_cleanup_returns_matching_agents(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
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
            mng_ctx=temp_mng_ctx,
            include_filters=('name == "cleanup-find-test"',),
            exclude_filters=(),
            error_behavior=ErrorBehavior.CONTINUE,
        )

        assert len(agents) == 1
        assert agents[0].name == AgentName("cleanup-find-test")
    finally:
        local_host.destroy_agent(agent)


def test_find_agents_for_cleanup_returns_empty_when_no_match(
    temp_mng_ctx: MngContext,
) -> None:
    """find_agents_for_cleanup should return empty list when no agents match."""
    agents = find_agents_for_cleanup(
        mng_ctx=temp_mng_ctx,
        include_filters=('name == "nonexistent-agent-xyz"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert agents == []


@pytest.mark.tmux
def test_execute_cleanup_destroy_on_online_host(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
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
        mng_ctx=temp_mng_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Destroy it (non-dry-run)
    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
        agents=agents,
        action=CleanupAction.DESTROY,
        is_dry_run=False,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    assert AgentName("cleanup-destroy-test") in result.destroyed_agents
    assert result.stopped_agents == []

    # Verify the agent no longer exists on the host
    remaining = find_agents_for_cleanup(
        mng_ctx=temp_mng_ctx,
        include_filters=('name == "cleanup-destroy-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(remaining) == 0


@pytest.mark.tmux
def test_execute_cleanup_stop_on_online_host(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
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

    # Verify agent is alive before stop (sleep commands enter WAITING state)
    state_before = agent.get_lifecycle_state()
    assert state_before in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING)

    # Find the agent via the API
    agents = find_agents_for_cleanup(
        mng_ctx=temp_mng_ctx,
        include_filters=('name == "cleanup-stop-test"',),
        exclude_filters=(),
        error_behavior=ErrorBehavior.CONTINUE,
    )
    assert len(agents) == 1

    # Stop it (non-dry-run)
    result = execute_cleanup(
        mng_ctx=temp_mng_ctx,
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
