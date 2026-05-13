from pathlib import Path
from unittest.mock import patch

import pytest

from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.interrupt import interrupt_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_interrupt_agents_returns_empty_when_no_agents_match(
    temp_mngr_ctx: MngrContext,
) -> None:
    result = interrupt_agents(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "nonexistent-agent"',),
        all_agents=False,
    )

    assert result.successful_agents == []
    assert result.failed_agents == []


@pytest.mark.tmux
def test_interrupt_agents_calls_stop_then_start_on_host(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """interrupt_agents must drive host.stop_agents followed by host.start_agents.

    This is the core contract of the new interrupt semantics: kill the agent
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
