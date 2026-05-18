from concurrent.futures import Future
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.interrupt import InterruptResult
from imbue.mngr.api.interrupt import interrupt_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.thread_cleanup import mngr_executor


def test_interrupt_agents_returns_empty_when_no_agents_match(
    temp_mngr_ctx: MngrContext,
) -> None:
    result = interrupt_agents(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "nonexistent-agent"',),
        all_agents=False,
    )

    assert result.successful_agents == []
    assert result.skipped_agents == []
    assert result.failed_agents == []


@pytest.mark.tmux
def test_interrupt_agents_calls_stop_then_start_on_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """interrupt_agents must drive host.stop_agents followed by host.start_agents.

    This is the core contract of the interrupt semantics: kill the agent
    process so any in-flight work and background tasks are gone, then restart
    the agent so it resumes from its saved state.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("interrupt-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847290"),
        ),
    )
    host.start_agents([agent.id])

    call_order: list[str] = []

    real_stop = Host.stop_agents
    real_start = Host.start_agents

    def tracked_stop(self: Host, agent_ids, *args, **kwargs) -> None:
        call_order.append(f"stop:{','.join(agent_ids)}")
        real_stop(self, agent_ids, *args, **kwargs)

    def tracked_start(self: Host, agent_ids, *args, **kwargs) -> None:
        call_order.append(f"start:{','.join(agent_ids)}")
        real_start(self, agent_ids, *args, **kwargs)

    try:
        with patch.object(Host, "stop_agents", tracked_stop):
            with patch.object(Host, "start_agents", tracked_start):
                result = interrupt_agents(
                    mngr_ctx=temp_mngr_ctx,
                    include_filters=('name == "interrupt-test"',),
                )
    finally:
        host.destroy_agent(agent)

    assert result.failed_agents == []
    assert result.successful_agents == ["interrupt-test"]
    assert call_order == [f"stop:{agent.id}", f"start:{agent.id}"]


@pytest.mark.tmux
def test_interrupt_agents_records_failure_when_start_fails(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """When host.start_agents raises, the agent must be recorded in failed_agents.

    Verifies the error-handling path of interrupt_agents: a BaseMngrError from
    the host's start/stop call must be captured into result.failed_agents
    rather than propagating, under the default CONTINUE error behavior.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("start-fail-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847292"),
        ),
    )
    host.start_agents([agent.id])

    error_agents: list[tuple[str, str]] = []

    def exploding_start(self: Host, agent_ids, *args, **kwargs) -> None:
        raise AgentStartError("start-fail-test", "simulated start failure")

    try:
        with patch.object(Host, "start_agents", exploding_start):
            result = interrupt_agents(
                mngr_ctx=temp_mngr_ctx,
                include_filters=('name == "start-fail-test"',),
                error_behavior=ErrorBehavior.CONTINUE,
                on_error=lambda name, err: error_agents.append((name, err)),
            )
    finally:
        host.destroy_agent(agent)

    assert result.successful_agents == []
    assert len(result.failed_agents) == 1
    assert result.failed_agents[0][0] == "start-fail-test"
    assert "simulated start failure" in result.failed_agents[0][1]
    assert len(error_agents) == 1
    assert error_agents[0][0] == "start-fail-test"


@pytest.mark.tmux
def test_interrupt_agents_calls_success_callback(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """The on_success callback must fire with the agent name on successful interrupt."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("callback-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847293"),
        ),
    )
    host.start_agents([agent.id])

    success_agents: list[str] = []
    error_agents: list[tuple[str, str]] = []

    try:
        result = interrupt_agents(
            mngr_ctx=temp_mngr_ctx,
            include_filters=('name == "callback-test"',),
            on_success=lambda name: success_agents.append(name),
            on_error=lambda name, err: error_agents.append((name, err)),
        )
    finally:
        host.destroy_agent(agent)

    assert result.successful_agents == ["callback-test"]
    assert result.failed_agents == []
    assert success_agents == ["callback-test"]
    assert error_agents == []


@pytest.mark.tmux
def test_interrupt_agents_skips_agent_with_in_flight_interrupt(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """A second concurrent interrupt for the same agent is a no-op.

    Simulates a double-click on a stop button: the first interrupt is
    in progress (stop_agents is blocking), and a second call for the
    same agent arrives. The second call must skip the agent and report
    it in skipped_agents.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("dedup-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847294"),
        ),
    )
    host.start_agents([agent.id])

    stop_entered = Event()
    proceed = Event()
    real_stop = Host.stop_agents
    real_start = Host.start_agents

    def blocking_stop(self: Host, agent_ids, *args, **kwargs) -> None:
        stop_entered.set()
        proceed.wait(timeout=10)
        real_stop(self, agent_ids, *args, **kwargs)

    include = ('name == "dedup-test"',)

    try:
        with patch.object(Host, "stop_agents", blocking_stop):
            with patch.object(Host, "start_agents", real_start):
                with mngr_executor(
                    parent_cg=temp_mngr_ctx.concurrency_group,
                    name="test_dedup",
                    max_workers=2,
                ) as executor:
                    first_future: Future[InterruptResult] = executor.submit(
                        interrupt_agents,
                        mngr_ctx=temp_mngr_ctx,
                        include_filters=include,
                    )

                    stop_entered.wait(timeout=10)

                    second_result = interrupt_agents(
                        mngr_ctx=temp_mngr_ctx,
                        include_filters=include,
                    )

                    proceed.set()
                    first_result = first_future.result(timeout=30)

        assert second_result.skipped_agents == ["dedup-test"]
        assert second_result.successful_agents == []
        assert second_result.failed_agents == []

        assert first_result.successful_agents == ["dedup-test"]
        assert first_result.skipped_agents == []
        assert first_result.failed_agents == []
    finally:
        proceed.set()
        host.destroy_agent(agent)
