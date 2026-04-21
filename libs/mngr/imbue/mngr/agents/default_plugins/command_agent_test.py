from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.default_plugins.command_agent import CommandAgent
from imbue.mngr.agents.default_plugins.command_agent import CommandAgentConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString


def _make_command_agent(
    host: Host,
    mngr_ctx: MngrContext,
    tmp_path: Path,
    agent_config: CommandAgentConfig | None = None,
) -> CommandAgent:
    """Create a CommandAgent with a real local host for testing."""
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = CommandAgentConfig()

    return CommandAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-command"),
        agent_type=AgentTypeName("command"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


def test_assemble_command_returns_override_when_provided(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """command_override takes precedence and is returned verbatim."""
    agent = _make_command_agent(local_host, temp_mngr_ctx, tmp_path)
    override = CommandString("echo from-override")
    cmd = agent.assemble_command(local_host, agent_args=("ignored",), command_override=override)
    assert cmd == override


def test_assemble_command_joins_agent_args_with_spaces(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Non-empty agent_args are joined with spaces into a CommandString."""
    agent = _make_command_agent(local_host, temp_mngr_ctx, tmp_path)
    cmd = agent.assemble_command(local_host, agent_args=("sleep", "42"), command_override=None)
    assert cmd == CommandString("sleep 42")


def test_assemble_command_raises_when_no_args_and_no_override(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Empty agent_args with no override produces a helpful ValueError."""
    agent = _make_command_agent(local_host, temp_mngr_ctx, tmp_path)
    with pytest.raises(ValueError, match=r"--type command requires a command after"):
        agent.assemble_command(local_host, agent_args=(), command_override=None)
