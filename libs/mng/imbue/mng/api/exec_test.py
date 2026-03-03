"""Unit tests for the exec API module."""

import json
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.api.exec import ExecResult
from imbue.mng.api.exec import MultiExecResult
from imbue.mng.api.exec import exec_command_on_agent
from imbue.mng.api.exec import exec_command_on_agents
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentNotFoundError
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.testing import cleanup_tmux_session
from imbue.mng.utils.testing import get_short_random_string

_AGENT_COMMAND = "sleep 98761"


class RunningTestAgent(FrozenModel):
    """A test agent with a running tmux session."""

    agent: BaseAgent = Field(description="The test agent instance")
    session_name: str = Field(description="Name of the tmux session running this agent")


def _create_running_test_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    work_dir: Path,
    mng_test_prefix: str,
) -> RunningTestAgent:
    """Create a real test agent with a running tmux session on the local provider."""
    host = local_provider.get_host(HostName("localhost"))

    agent_id = AgentId.generate()
    agent_name = AgentName(f"exec-test-{get_short_random_string()}")

    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": "generic",
        "command": _AGENT_COMMAND,
        "work_dir": str(work_dir),
        "create_time": datetime.now(timezone.utc).isoformat(),
        "permissions": [],
        "start_on_boot": False,
    }
    (agent_dir / "data.json").write_text(json.dumps(data, indent=2))

    agent = BaseAgent(
        id=agent_id,
        host_id=host.id,
        name=agent_name,
        agent_type=AgentTypeName("generic"),
        agent_config=AgentTypeConfig(command=CommandString(_AGENT_COMMAND)),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host=host,
        mng_ctx=temp_mng_ctx,
    )

    session_name = f"{mng_test_prefix}{agent_name}"
    host.execute_command(
        f"tmux new-session -d -s '{session_name}' '{_AGENT_COMMAND}'",
        timeout_seconds=5.0,
    )

    return RunningTestAgent(agent=agent, session_name=session_name)


@pytest.fixture
def running_test_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    mng_test_prefix: str,
) -> Generator[RunningTestAgent, None, None]:
    """Create a running test agent and clean up its tmux session on teardown."""
    running = _create_running_test_agent(local_provider, temp_mng_ctx, temp_work_dir, mng_test_prefix)
    yield running
    cleanup_tmux_session(running.session_name)


def test_exec_result_fields() -> None:
    """Test ExecResult has the expected fields."""
    result = ExecResult(
        agent_name="test-agent",
        stdout="hello\n",
        stderr="",
        success=True,
    )
    assert result.agent_name == "test-agent"
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.success is True


def test_exec_result_failure() -> None:
    """Test ExecResult with a failed command."""
    result = ExecResult(
        agent_name="test-agent",
        stdout="",
        stderr="command not found\n",
        success=False,
    )
    assert result.success is False
    assert result.stderr == "command not found\n"


@pytest.mark.tmux
def test_exec_command_on_agent_runs_command(
    temp_mng_ctx: MngContext,
    running_test_agent: RunningTestAgent,
) -> None:
    """Test exec_command_on_agent runs a command on a real local agent."""
    result = exec_command_on_agent(
        mng_ctx=temp_mng_ctx,
        agent_str=str(running_test_agent.agent.name),
        command="echo hello",
    )

    assert result.agent_name == str(running_test_agent.agent.name)
    assert "hello" in result.stdout
    assert result.success is True


@pytest.mark.tmux
def test_exec_command_on_agent_uses_custom_cwd(
    temp_mng_ctx: MngContext,
    running_test_agent: RunningTestAgent,
    tmp_path: Path,
) -> None:
    """Test that --cwd overrides the agent's work_dir."""
    custom_dir = tmp_path / "custom_cwd"
    custom_dir.mkdir()
    (custom_dir / "marker.txt").write_text("found")

    result = exec_command_on_agent(
        mng_ctx=temp_mng_ctx,
        agent_str=str(running_test_agent.agent.name),
        command="cat marker.txt",
        cwd=str(custom_dir),
    )

    assert result.stdout == "found"
    assert result.success is True


@pytest.mark.tmux
def test_exec_command_on_agent_returns_failure(
    temp_mng_ctx: MngContext,
    running_test_agent: RunningTestAgent,
) -> None:
    """Test that a failing command returns success=False."""
    result = exec_command_on_agent(
        mng_ctx=temp_mng_ctx,
        agent_str=str(running_test_agent.agent.name),
        command="false",
    )

    assert result.success is False


def test_multi_exec_result_fields() -> None:
    """Test MultiExecResult has the expected fields."""
    result = MultiExecResult()
    assert result.successful_results == []
    assert result.failed_agents == []


def test_multi_exec_result_accumulates_results() -> None:
    """Test MultiExecResult accumulates results correctly."""
    result = MultiExecResult()
    result.successful_results.append(ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True))
    result.failed_agents.append(("agent-2", "host offline"))

    assert len(result.successful_results) == 1
    assert len(result.failed_agents) == 1
    assert result.successful_results[0].agent_name == "agent-1"
    assert result.failed_agents[0] == ("agent-2", "host offline")


@pytest.mark.tmux
def test_exec_command_on_agents_single_agent(
    temp_mng_ctx: MngContext,
    running_test_agent: RunningTestAgent,
) -> None:
    """Test exec_command_on_agents runs a command on a single agent."""
    result = exec_command_on_agents(
        mng_ctx=temp_mng_ctx,
        agent_identifiers=[str(running_test_agent.agent.name)],
        command="echo multi-exec-test",
        is_all=False,
    )

    assert len(result.successful_results) == 1
    assert result.successful_results[0].agent_name == str(running_test_agent.agent.name)
    assert "multi-exec-test" in result.successful_results[0].stdout
    assert result.successful_results[0].success is True
    assert len(result.failed_agents) == 0


def test_exec_command_on_agents_nonexistent_agent(
    temp_mng_ctx: MngContext,
) -> None:
    """Test exec_command_on_agents with a nonexistent agent raises AgentNotFoundError."""
    with pytest.raises(AgentNotFoundError):
        exec_command_on_agents(
            mng_ctx=temp_mng_ctx,
            agent_identifiers=["nonexistent-agent-82716"],
            command="echo test",
            is_all=False,
        )


@pytest.mark.tmux
def test_exec_command_on_agents_invokes_callbacks(
    temp_mng_ctx: MngContext,
    running_test_agent: RunningTestAgent,
) -> None:
    """Test exec_command_on_agents invokes on_success callback."""
    callback_results: list[ExecResult] = []

    result = exec_command_on_agents(
        mng_ctx=temp_mng_ctx,
        agent_identifiers=[str(running_test_agent.agent.name)],
        command="echo callback-test",
        is_all=False,
        on_success=lambda r: callback_results.append(r),
    )

    assert len(callback_results) == 1
    assert "callback-test" in callback_results[0].stdout
    assert len(result.successful_results) == 1
