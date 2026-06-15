from pathlib import Path

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import get_short_random_string


def test_send_message_to_agents_returns_empty_result_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that send_message returns empty result when no agents are provided."""
    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello",
        agents_to_message=[],
    )

    assert result.successful_agents == []
    assert result.failed_agents == []


@pytest.mark.tmux
def test_send_message_to_agents_calls_success_callback(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message calls the success callback when message is sent."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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

    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello from test",
        agents_to_message=matches,
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
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that sending message to stopped agent fails."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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

    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello",
        agents_to_message=matches,
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
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message auto-starts a stopped agent when is_start_desired=True."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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

    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello with auto-start",
        agents_to_message=matches,
        is_start_desired=True,
        on_success=lambda name: success_agents.append(name),
        on_error=lambda name, err: error_agents.append((name, err)),
    )

    # The agent must have actually transitioned out of STOPPED (i.e. its tmux
    # session was started), not merely been reported as a successful send.
    started_state = agent.get_lifecycle_state()

    # Clean up
    host.destroy_agent(agent)

    # Agent should have been started and message sent successfully
    assert started_state != AgentLifecycleState.STOPPED
    assert "start-test" in result.successful_agents
    assert "start-test" in success_agents
    assert len(error_agents) == 0


@pytest.mark.tmux
# real agent setup/teardown occasionally exceeds the 10s default.
@pytest.mark.timeout(30)
def test_send_message_to_agents_only_messages_requested_agents(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that send_message only delivers to the agents in agents_to_message.

    Locally runs in ~5s. On offload it occasionally exceeds the default 10s
    pytest-timeout during tmux kill-session cleanup under CI load (the hang
    is inside loguru's sink during log_span, not in the actual kill).
    Bumped to 30s rather than marked flaky so failures stay loud.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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

    # Resolve only agent1 and send to that one
    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )
    matches_for_agent1 = [m for m in matches if str(m.agent_name) == "filter-test-1"]
    assert len(matches_for_agent1) == 1

    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello filtered",
        agents_to_message=matches_for_agent1,
    )

    # Clean up
    host.destroy_agent(agent1)
    host.destroy_agent(agent2)

    # Only agent1 should have received the message
    assert "filter-test-1" in result.successful_agents
    assert "filter-test-2" not in result.successful_agents


class _ExplodingSendAgent(BaseAgent):
    """Real agent whose ``send_message`` always raises a ``SendMessageError``.

    Registered as a dedicated agent type so ``send_message_to_agents`` resolves
    and loads it through the normal production path (``host.get_agents()`` ->
    ``resolve_agent_type``), rather than patching ``BaseAgent.send_message``
    globally. This keeps the failure-isolation test wired to real behavior: if
    the send path is refactored, this fake still fails for real.
    """

    def send_message(self, message: str) -> None:
        raise SendMessageError(str(self.name), "simulated send failure")


@pytest.mark.tmux
def test_send_message_one_agent_failure_does_not_prevent_other_agents(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """One agent's SendMessageError must not kill the broadcast to other agents.

    SendMessageError is an AgentError, which inherits from MngrError. The per-agent
    send is guarded by ``except MngrError`` so that, in CONTINUE mode, one
    agent's failure is recorded without aborting the broadcast to the others.

    The failing agent is a real ``_ExplodingSendAgent`` resolved via a
    dedicated registered agent type (the agent registry is reset per test by
    the autouse plugin-manager fixture), so no production internals are patched.
    """
    # Register a one-off agent type whose send always raises. resolve_agent_type
    # requires both a class and a config to be registered for the type name.
    exploding_type = f"exploding-{get_short_random_string()}"
    register_agent_class(exploding_type, _ExplodingSendAgent)
    register_agent_config(exploding_type, AgentTypeConfig)

    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent1 = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("will-explode"),
            agent_type=AgentTypeName(exploding_type),
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

    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Hello",
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
    )

    # Clean up
    host.destroy_agent(agent1)
    host.destroy_agent(agent2)

    # The exploding agent should be recorded as failed with its send error.
    failed_by_name = {name: err for name, err in result.failed_agents}
    assert "will-explode" in failed_by_name
    assert "simulated send failure" in failed_by_name["will-explode"]

    # The other agent must still have succeeded
    assert "will-succeed" in result.successful_agents
