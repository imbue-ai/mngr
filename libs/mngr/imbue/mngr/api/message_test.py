import re
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import AbstractContextManager
from contextlib import nullcontext
from datetime import datetime
from datetime import timezone
from pathlib import Path
from threading import Lock
from typing import Final

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.message import MessageResult
from imbue.mngr.api.message import _deliver_text
from imbue.mngr.api.message import _process_host_for_messaging
from imbue.mngr.api.message import _send_message_to_agent
from imbue.mngr.api.message import send_key_chord_to_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import CorruptedAgentDataError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.mock_provider_test import make_offline_host
from imbue.mngr.utils.polling import wait_for


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


def test_send_key_chord_to_agents_returns_empty_result_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """send_key_chord_to_agents returns an empty result when no agents are provided."""
    result = send_key_chord_to_agents(
        mngr_ctx=temp_mngr_ctx,
        key="M-q",
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

    # Should have failed because the agent is not running (no tmux session)
    assert len(result.failed_agents) == 1
    assert result.failed_agents[0][0] == "stopped-test"
    assert "not running" in result.failed_agents[0][1]
    assert "STOPPED" in result.failed_agents[0][1]


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

    # Clean up
    host.destroy_agent(agent)

    # Agent should have been started and message sent successfully
    assert "start-test" in result.successful_agents
    assert "start-test" in success_agents
    assert len(error_agents) == 0


@pytest.mark.tmux
# real agent setup/teardown plus a stop-and-restart can exceed the 10s default.
@pytest.mark.timeout(30)
def test_send_message_to_agents_revives_done_agent_when_start_desired(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Messaging a DONE agent must revive it, not type the message into the husk shell.

    A DONE agent is one whose main process died (here: a ctrl-c, standing in for a
    crash or an OOM shed) while tmux kept the session open on a bare shell. This is
    distinct from STOPPED (no session at all). Because ``start_agents``
    short-circuits on an existing session, reviving a DONE agent requires tearing
    the husk down first -- otherwise the message is delivered into the leftover
    shell and silently lost. This guards the OOM revival path, which relies on a
    later message restarting a shed agent.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("revive-done-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847271"),
        ),
    )
    host.start_agents([agent.id])

    # Confirm the agent is live before we kill its process.
    wait_for(
        lambda: agent.get_lifecycle_state() in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
        error_message="Expected agent to be running before killing its process",
    )

    # Kill the agent's process but leave the tmux session up, exactly as a ctrl-c
    # (or an OOM shed of the main process) would: the pane drops back to its shell,
    # so the agent reports DONE rather than STOPPED.
    session_name = temp_mngr_ctx.config.agent_session_name(agent.name)
    window_name = temp_mngr_ctx.config.tmux.primary_window_name
    window_target = TmuxWindowTarget(session_name=session_name, window=window_name)
    host.execute_idempotent_command(
        f"tmux send-keys -t {window_target.as_shell_arg()} C-c",
        timeout_seconds=5.0,
    )
    wait_for(
        lambda: agent.get_lifecycle_state() == AgentLifecycleState.DONE,
        error_message="Expected agent lifecycle state to be DONE after killing its process",
    )

    matches = find_all_agents(
        addresses=(),
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    error_agents: list[tuple[str, str]] = []
    result = send_message_to_agents(
        mngr_ctx=temp_mngr_ctx,
        message_content="Welcome back",
        agents_to_message=matches,
        is_start_desired=True,
        on_error=lambda name, err: error_agents.append((name, err)),
    )

    # The decisive check: the DONE husk was torn down and the agent relaunched, so
    # a fresh process is running again. With the bug, the agent would stay DONE
    # (the message having been typed into the dead shell).
    wait_for(
        lambda: agent.get_lifecycle_state() in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
        error_message="Expected the DONE agent to be revived to a running state after messaging",
    )

    # Clean up
    host.destroy_agent(agent)

    assert "revive-done-test" in result.successful_agents
    assert error_agents == []


class _ReviveFailingAgent(BaseAgent[AgentTypeConfig]):
    """Test agent that reports DONE and whose revive fails with AgentStartError."""

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return AgentLifecycleState.DONE

    def wait_for_ready_signal(
        self,
        is_readiness_awaited: bool,
        start_action: Callable[[], None],
        timeout: float | None = None,
    ) -> None:
        raise AgentStartError(str(self.name), "agent did not become ready")


def test_send_message_records_failure_when_revive_fails(
    temp_work_dir: Path,
    local_provider: LocalProviderInstance,
) -> None:
    """A failed revive must land in failed_agents, not vanish into a host-level log.

    If reviving a DONE agent raises (e.g. the ready-wait times out), the failure has
    to be recorded against the agent so `mngr message --start` reports it and exits
    non-zero, instead of exiting 0 with the agent missing from both result lists.
    """
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=_ReviveFailingAgent,
    )

    result = MessageResult()
    errors: list[tuple[str, str]] = []
    _send_message_to_agent(
        agent=agent,
        host=agent.host,
        message_content="hello",
        deliver=_deliver_text,
        result=result,
        result_lock=Lock(),
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=True,
        on_success=None,
        on_error=lambda name, error: errors.append((name, error)),
    )

    assert result.successful_agents == []
    assert result.failed_agents == [
        (str(agent.name), f"Failed to start agent {agent.name}: agent did not become ready")
    ]
    assert errors == result.failed_agents


_PROBE_PARSE_ERROR: Final[str] = "Expecting value: line 1 column 1 (char 0)"


def _make_probe_failure(agent: BaseAgent[AgentTypeConfig]) -> CorruptedAgentDataError:
    """Build the error ``probe_lifecycle`` raises when the agent's data.json will not parse."""
    return CorruptedAgentDataError(agent.id, agent._get_data_path(), ValueError(_PROBE_PARSE_ERROR))


class _ProbeFailingAgent(BaseAgent[AgentTypeConfig]):
    """Test agent whose lifecycle probe raises instead of reporting a state.

    ``probe_lifecycle`` resolves the expected process name by reading the agent's data.json,
    so ``CorruptedAgentDataError`` is a real way for it to raise past its
    HostConnectionError guard.
    """

    def get_lifecycle_state(self) -> AgentLifecycleState:
        raise _make_probe_failure(self)


@pytest.mark.parametrize("error_behavior", [ErrorBehavior.CONTINUE, ErrorBehavior.ABORT], ids=["continue", "abort"])
def test_send_message_records_failure_when_the_lifecycle_probe_raises(
    temp_work_dir: Path,
    local_provider: LocalProviderInstance,
    error_behavior: ErrorBehavior,
) -> None:
    """A failed lifecycle probe must be recorded, like every other per-agent failure.

    The probe decides whether the agent needs starting, so it runs before any path that
    records an outcome. `BaseAgent.probe_lifecycle` absorbs only HostConnectionError, so
    any other MngrError would escape `_send_message_to_agent` entirely and leave its agent
    in neither result list -- reporting delivery for a message that was never sent.
    """
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=_ProbeFailingAgent,
    )
    is_abort = error_behavior == ErrorBehavior.ABORT
    expected_error = str(_make_probe_failure(agent))

    result = MessageResult()
    errors: list[tuple[str, str]] = []
    expectation: AbstractContextManager = (
        pytest.raises(MngrError, match=re.escape(expected_error)) if is_abort else nullcontext()
    )
    with expectation:
        _send_message_to_agent(
            agent=agent,
            host=agent.host,
            message_content="hello",
            deliver=_deliver_text,
            result=result,
            result_lock=Lock(),
            error_behavior=error_behavior,
            is_start_desired=True,
            on_success=None,
            on_error=lambda name, error: errors.append((name, error)),
        )

    assert result.successful_agents == []
    assert result.failed_agents == [(str(agent.name), expected_error)]
    assert errors == result.failed_agents


def _make_matches_on_host(
    provider: BaseProviderInstance,
    host_id: HostId,
    host_name: str,
    agent_names: Sequence[str],
) -> list[AgentMatch]:
    """Build one AgentMatch per name, all resolving to the same host."""
    return [
        AgentMatch(
            agent_id=AgentId.generate(),
            agent_name=AgentName(name),
            host_id=host_id,
            host_name=HostName(host_name),
            provider_name=provider.name,
        )
        for name in agent_names
    ]


class _HostStartFailingProvider(MockProviderInstance):
    """Test provider whose offline hosts cannot be started."""

    def start_host(self, host: HostInterface | HostId, snapshot_id: SnapshotId | None = None) -> Host:
        raise HostConnectionError("could not reach the host")


@pytest.mark.allow_warnings(match=r"^Error accessing host")
@pytest.mark.parametrize("error_behavior", [ErrorBehavior.CONTINUE, ErrorBehavior.ABORT], ids=["continue", "abort"])
@pytest.mark.parametrize("is_start_desired", [True, False], ids=["start-fails", "offline-without-start"])
def test_unreachable_host_fails_its_agents_or_aborts(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    is_start_desired: bool,
    error_behavior: ErrorBehavior,
) -> None:
    """An unreachable host must fail every agent on it, not vanish into a log.

    `mngr message` exits non-zero only when an agent lands in failed_agents or
    blocked_agents, so a host-level error recorded nowhere reports success for a message
    that was never delivered. Both error behaviours record; ABORT additionally re-raises,
    which is what fails the command rather than only reporting the failure in its result.
    """
    host_id = HostId.generate()
    agent_names = ("sleepy", "dozy")
    provider = _HostStartFailingProvider(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    provider.mock_hosts.append(
        make_offline_host(
            CertifiedHostData(
                host_id=str(host_id),
                host_name="stopped-host",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            provider,
            temp_mngr_ctx,
        )
    )
    expected_error = (
        "could not reach the host" if is_start_desired else f"Host '{host_id}' is offline. Cannot send messages."
    )
    is_abort = error_behavior == ErrorBehavior.ABORT

    result = MessageResult()
    errors: list[tuple[str, str]] = []
    expectation: AbstractContextManager = (
        pytest.raises(HostConnectionError, match=re.escape(expected_error)) if is_abort else nullcontext()
    )
    with expectation:
        _process_host_for_messaging(
            matches=_make_matches_on_host(provider, host_id, "stopped-host", agent_names),
            provider=provider,
            message_content="hello",
            deliver=_deliver_text,
            error_behavior=error_behavior,
            is_start_desired=is_start_desired,
            result=result,
            result_lock=Lock(),
            parent_cg=temp_mngr_ctx.concurrency_group,
            on_success=None,
            on_error=lambda name, error: errors.append((name, error)),
        )

    assert result.successful_agents == []
    assert result.failed_agents == [(name, expected_error) for name in agent_names]
    assert errors == result.failed_agents


class _AgentListFailingHost(Host):
    """Host subclass whose get_agents always raises HostConnectionError."""

    def get_agents(self) -> list[AgentInterface]:
        raise HostConnectionError("could not list agents")


class _AgentListFailingProvider(LocalProviderInstance):
    """Provider that returns an online _AgentListFailingHost from get_host()."""

    def get_host(self, host: HostId | HostName) -> _AgentListFailingHost:
        return _AgentListFailingHost(
            id=self.host_id,
            host_name=HostName("test"),
            connector=PyinfraConnector(self._create_local_pyinfra_host()),
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )


@pytest.mark.allow_warnings(match=r"^Error accessing host")
def test_host_that_cannot_list_its_agents_fails_them(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A reachable host whose agent listing raises must fail its agents too.

    ``get_agents`` is the one host-phase step reached past ``_resolve_online_host``, so
    it is the statement a refactor could move into the send phase -- where the handler
    only logs and `mngr message` goes back to exiting 0 on an undelivered message.
    """
    agent_names = ("sleepy", "dozy")
    provider = _AgentListFailingProvider(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )

    result = MessageResult()
    errors: list[tuple[str, str]] = []
    _process_host_for_messaging(
        matches=_make_matches_on_host(provider, provider.host_id, "test", agent_names),
        provider=provider,
        message_content="hello",
        deliver=_deliver_text,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=False,
        result=result,
        result_lock=Lock(),
        parent_cg=temp_mngr_ctx.concurrency_group,
        on_success=None,
        on_error=lambda name, error: errors.append((name, error)),
    )

    assert result.successful_agents == []
    assert result.failed_agents == [(name, "could not list agents") for name in agent_names]
    assert errors == result.failed_agents


@pytest.mark.parametrize("error_behavior", [ErrorBehavior.CONTINUE, ErrorBehavior.ABORT], ids=["continue", "abort"])
def test_agent_missing_from_its_host_is_recorded_before_the_abort(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    error_behavior: ErrorBehavior,
) -> None:
    """An agent missing from its host must be recorded, including under ABORT.

    ABORT raises on the first missing agent, so only that one is recorded, whereas
    CONTINUE goes on to the rest; either way the agent that missed the message reaches
    failed_agents, which is what `mngr message` exits non-zero on.
    """
    agent_names = ("sleepy", "dozy")
    matches = _make_matches_on_host(local_provider, local_provider.host_id, LOCAL_HOST_NAME, agent_names)
    is_abort = error_behavior == ErrorBehavior.ABORT
    expected_failures = [
        (str(match.agent_name), f"Agent {match.agent_id} not found on host {local_provider.host_id}")
        for match in (matches[:1] if is_abort else matches)
    ]

    result = MessageResult()
    errors: list[tuple[str, str]] = []
    expectation: AbstractContextManager = (
        pytest.raises(AgentNotFoundOnHostError, match=re.escape(expected_failures[0][1]))
        if is_abort
        else nullcontext()
    )
    with expectation:
        _process_host_for_messaging(
            matches=matches,
            provider=local_provider,
            message_content="hello",
            deliver=_deliver_text,
            error_behavior=error_behavior,
            is_start_desired=False,
            result=result,
            result_lock=Lock(),
            parent_cg=temp_mngr_ctx.concurrency_group,
            on_success=None,
            on_error=lambda name, error: errors.append((name, error)),
        )

    assert result.successful_agents == []
    assert result.failed_agents == expected_failures
    assert errors == result.failed_agents


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


@pytest.mark.tmux
def test_send_message_one_agent_failure_does_not_prevent_other_agents(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One agent's SendMessageError must not kill the broadcast to other agents.

    SendMessageError is an AgentError, which inherits from MngrError. The per-agent
    send is guarded by ``except MngrError`` so that, in CONTINUE mode, one
    agent's failure is recorded without aborting the broadcast to the others.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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

    original_send = SendKeysAgent.send_message

    def exploding_send(self: SendKeysAgent, message: str) -> None:
        if str(self.name) == "will-explode":
            raise SendMessageError("will-explode", "simulated send failure")
        original_send(self, message)

    monkeypatch.setattr(SendKeysAgent, "send_message", exploding_send)

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

    # The exploding agent should be recorded as failed
    failed_names = [name for name, _err in result.failed_agents]
    assert "will-explode" in failed_names

    # The other agent must still have succeeded
    assert "will-succeed" in result.successful_agents
