from pathlib import Path

import pytest

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.api.create import CreateAgentOptions
from imbue.mng.api.message import MessageResult
from imbue.mng.api.message import _agent_to_cel_context
from imbue.mng.api.message import send_message_to_agents
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import SendMessageError
from imbue.mng.hosts.host import Host
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance


def test_message_result_initializes_with_empty_lists() -> None:
    """Test that MessageResult initializes with empty lists."""
    result = MessageResult()
    assert result.successful_agents == []
    assert result.failed_agents == []


def test_message_result_can_add_successful_agent() -> None:
    """Test that we can add successful agents to the result."""
    result = MessageResult()
    result.successful_agents.append("test-agent")
    assert result.successful_agents == ["test-agent"]


def test_message_result_can_add_failed_agent() -> None:
    """Test that we can add failed agents to the result."""
    result = MessageResult()
    result.failed_agents.append(("test-agent", "error message"))
    assert result.failed_agents == [("test-agent", "error message")]


@pytest.mark.tmux
def test_agent_to_cel_context_returns_expected_fields(
    temp_work_dir: Path,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that _agent_to_cel_context returns the expected fields."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("cel-test-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 1"),
        ),
    )

    context = _agent_to_cel_context(agent, "local")

    assert context["id"] == str(agent.id)
    assert context["name"] == "cel-test-agent"
    assert context["type"] == "generic"
    assert context["state"] == AgentLifecycleState.STOPPED.value
    assert context["host"]["provider"] == "local"
    assert context["host"]["id"] == str(agent.host_id)


def test_send_message_to_agents_returns_empty_result_when_no_agents_match(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that send_message returns empty result when no agents match filters."""
    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello",
        include_filters=('name == "nonexistent-agent"',),
        all_agents=False,
    )

    assert result.successful_agents == []
    assert result.failed_agents == []


@pytest.mark.tmux
def test_send_message_to_agents_calls_success_callback(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message calls the success callback when message is sent."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("message-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847264"),
        ),
    )

    # Start the agent
    host.start_agents([agent.id])

    success_agents: list[str] = []
    error_agents: list[tuple[str, str]] = []

    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello from test",
        all_agents=True,
        on_success=lambda name: success_agents.append(name),
        on_error=lambda name, err: error_agents.append((name, err)),
    )

    # Clean up
    host.destroy_agent(agent)

    assert "message-test" in result.successful_agents
    assert "message-test" in success_agents


@pytest.mark.tmux
def test_send_message_to_agents_fails_for_stopped_agent(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that sending message to stopped agent fails."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("stopped-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847265"),
        ),
    )

    # Don't start the agent - it should be stopped

    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello",
        all_agents=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    # Clean up
    host.destroy_agent(agent)

    # Should have failed because agent has no tmux session
    assert len(result.failed_agents) == 1
    assert result.failed_agents[0][0] == "stopped-test"
    assert "no tmux session" in result.failed_agents[0][1]


@pytest.mark.tmux
def test_send_message_to_agents_starts_stopped_agent_when_start_desired(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message auto-starts a stopped agent when is_start_desired=True."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("start-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847268"),
        ),
    )

    # Don't start the agent - it should be stopped
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    success_agents: list[str] = []
    error_agents: list[tuple[str, str]] = []

    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello with auto-start",
        all_agents=True,
        is_start_desired=True,
        on_success=lambda name: success_agents.append(name),
        on_error=lambda name, err: error_agents.append((name, err)),
    )

    # Clean up
    host.destroy_agent(agent)

    # Agent should have been started and message sent successfully
    assert "start-test" in result.successful_agents
    assert "start-test" in success_agents
    assert len(error_agents) == 0


@pytest.mark.tmux
def test_send_message_to_agents_with_include_filter(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message respects include filters."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    # Create two agents
    agent1 = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("filter-test-1"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847266"),
        ),
    )
    agent2 = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("filter-test-2"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847267"),
        ),
    )

    # Start both agents
    host.start_agents([agent1.id, agent2.id])

    # Send message only to agent1 using filter
    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello filtered",
        include_filters=('name == "filter-test-1"',),
        all_agents=False,
    )

    # Clean up
    host.destroy_agent(agent1)
    host.destroy_agent(agent2)

    # Only agent1 should have received the message
    assert "filter-test-1" in result.successful_agents
    assert "filter-test-2" not in result.successful_agents


@pytest.mark.tmux
def test_send_message_one_agent_failure_does_not_prevent_other_agents(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    local_provider: LocalProviderInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One agent's SendMessageError must not kill the broadcast to other agents.

    SendMessageError inherits from BaseMngError (not MngError). Before the switch
    to concurrent sends, the serial loop only caught MngError, so a SendMessageError
    would propagate up and abort the entire broadcast.
    """
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent1 = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("will-explode"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847280"),
        ),
    )
    agent2 = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("will-succeed"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847281"),
        ),
    )

    host.start_agents([agent1.id, agent2.id])

    original_send = BaseAgent.send_message

    def exploding_send(self: BaseAgent, message: str) -> None:
        if str(self.name) == "will-explode":
            raise SendMessageError("will-explode", "simulated send failure")
        original_send(self, message)

    monkeypatch.setattr(BaseAgent, "send_message", exploding_send)

    result = send_message_to_agents(
        mng_ctx=temp_mng_ctx,
        message_content="Hello",
        all_agents=True,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    # Clean up
    host.destroy_agent(agent1)
    host.destroy_agent(agent2)

    # The exploding agent should be recorded as failed
    failed_names = [name for name, _err in result.failed_agents]
    assert "will-explode" in failed_names

    # The other agent must still have succeeded
    assert "will-succeed" in result.successful_agents
