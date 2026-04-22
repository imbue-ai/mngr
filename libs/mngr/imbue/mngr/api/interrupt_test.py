from pathlib import Path

import pytest

from imbue.mngr.api.create import CreateAgentOptions
from imbue.mngr.api.interrupt import InterruptResult
from imbue.mngr.api.interrupt import agent_type_supports_interrupt
from imbue.mngr.api.interrupt import interrupt_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_agent_type_supports_interrupt_returns_false_for_none() -> None:
    assert agent_type_supports_interrupt(None) is False


def test_agent_type_supports_interrupt_returns_false_for_generic(temp_mngr_ctx: MngrContext) -> None:
    """'generic' falls through to BaseAgent (the default), which is not interruptible.

    ``temp_mngr_ctx`` is requested for its side-effect: it transitively
    initializes the mngr plugin registry so that ``get_agent_class`` returns
    ``BaseAgent`` as the default fallback instead of raising ``MngrError``.
    Without it, the test would exercise the exception branch rather than the
    default-class branch.
    """
    del temp_mngr_ctx
    assert agent_type_supports_interrupt("generic") is False


def test_agent_type_supports_interrupt_returns_false_for_unknown_type(temp_mngr_ctx: MngrContext) -> None:
    """Unknown types fall through to the default class (BaseAgent), not interruptible.

    See ``test_agent_type_supports_interrupt_returns_false_for_generic`` for
    why ``temp_mngr_ctx`` is requested.
    """
    del temp_mngr_ctx
    assert agent_type_supports_interrupt("never-heard-of-it") is False


def test_interrupt_result_initializes_with_empty_lists() -> None:
    result = InterruptResult()
    assert result.successful_agents == []
    assert result.failed_agents == []


def test_interrupt_result_can_add_successful_agent() -> None:
    result = InterruptResult()
    result.successful_agents.append("agent-a")
    assert result.successful_agents == ["agent-a"]


def test_interrupt_result_can_add_failed_agent() -> None:
    result = InterruptResult()
    result.failed_agents.append(("agent-a", "boom"))
    assert result.failed_agents == [("agent-a", "boom")]


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
def test_interrupt_agents_records_non_interruptible_agent_as_failed(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """Generic agents (BaseAgent) do not implement InterruptibleAgentMixin.

    They must be reported in failed_agents with a clear reason rather than
    silently skipped or raising.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("non-interruptible-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847290"),
        ),
    )
    host.start_agents([agent.id])

    error_agents: list[tuple[str, str]] = []

    result = interrupt_agents(
        mngr_ctx=temp_mngr_ctx,
        include_filters=('name == "non-interruptible-test"',),
        on_error=lambda name, err: error_agents.append((name, err)),
    )

    host.destroy_agent(agent)

    assert result.successful_agents == []
    assert len(result.failed_agents) == 1
    assert result.failed_agents[0][0] == "non-interruptible-test"
    assert "does not support interrupt" in result.failed_agents[0][1]
    assert ("non-interruptible-test", result.failed_agents[0][1]) in error_agents
