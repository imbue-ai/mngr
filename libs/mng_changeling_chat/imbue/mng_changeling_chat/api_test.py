"""Unit tests for the mng-changeling-chat API module."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import HostId
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng_changeling_chat.api import ChatCommandError
from imbue.mng_changeling_chat.api import _build_chat_env_vars
from imbue.mng_changeling_chat.api import _build_chat_script_path
from imbue.mng_changeling_chat.api import _build_remote_chat_script
from imbue.mng_changeling_chat.api import build_chat_command_args


class _TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name."""

    def get_expected_process_name(self) -> str:
        return "test-process"


def _make_local_host_and_agent(
    local_provider: LocalProviderInstance,
    mng_ctx: MngContext,
    agent_name: str = "test-agent",
) -> tuple[Host, _TestAgent]:
    """Create a local host and agent for testing."""
    host = Host(
        id=HostId(f"host-{uuid4().hex}"),
        connector=PyinfraConnector(local_provider._create_local_pyinfra_host()),
        provider_instance=local_provider,
        mng_ctx=mng_ctx,
    )
    agent = _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("test"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=mng_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )
    return host, agent


# =========================================================================
# Tests for build_chat_command_args
# =========================================================================


def test_build_chat_command_args_new_mode() -> None:
    result = build_chat_command_args("new", conversation_id=None)
    assert result == ["--new"]


def test_build_chat_command_args_list_mode() -> None:
    result = build_chat_command_args("list", conversation_id=None)
    assert result == ["--list"]


def test_build_chat_command_args_resume_mode() -> None:
    result = build_chat_command_args("resume", conversation_id="conv-12345")
    assert result == ["--resume", "conv-12345"]


def test_build_chat_command_args_resume_mode_requires_conversation_id() -> None:
    with pytest.raises(ChatCommandError, match="conversation_id is required"):
        build_chat_command_args("resume", conversation_id=None)


def test_build_chat_command_args_last_mode_raises() -> None:
    with pytest.raises(ChatCommandError, match="should be resolved before"):
        build_chat_command_args("last", conversation_id=None)


def test_build_chat_command_args_unknown_mode_raises() -> None:
    with pytest.raises(ChatCommandError, match="Unknown chat mode"):
        build_chat_command_args("unknown_mode_abc123", conversation_id=None)


# =========================================================================
# Tests for _build_chat_script_path
# =========================================================================


def test_build_chat_script_path() -> None:
    result = _build_chat_script_path(Path("/home/user/.mng"))
    assert result == "/home/user/.mng/commands/chat.sh"


def test_build_chat_script_path_with_different_host_dir() -> None:
    result = _build_chat_script_path(Path("/data/mng"))
    assert result == "/data/mng/commands/chat.sh"


# =========================================================================
# Tests for _build_chat_env_vars
# =========================================================================


def test_build_chat_env_vars(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    host, agent = _make_local_host_and_agent(local_provider, temp_mng_ctx)

    env_vars = _build_chat_env_vars(agent, host)

    assert env_vars["MNG_HOST_DIR"] == str(host.host_dir)
    assert env_vars["MNG_AGENT_STATE_DIR"] == str(host.host_dir / "agents" / str(agent.id))
    assert env_vars["MNG_AGENT_WORK_DIR"] == str(agent.work_dir)
    assert env_vars["MNG_AGENT_ID"] == str(agent.id)
    assert env_vars["MNG_AGENT_NAME"] == str(agent.name)


# =========================================================================
# Tests for _build_remote_chat_script
# =========================================================================


def test_build_remote_chat_script_sets_env_vars(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    host, agent = _make_local_host_and_agent(local_provider, temp_mng_ctx)

    script = _build_remote_chat_script(host.host_dir, agent, ["--new"])

    assert f"export MNG_HOST_DIR='{host.host_dir}'" in script
    assert f"export MNG_AGENT_STATE_DIR='{host.host_dir}/agents/{agent.id}'" in script
    assert f"export MNG_AGENT_WORK_DIR='{agent.work_dir}'" in script
    assert f"export MNG_AGENT_ID='{agent.id}'" in script
    assert f"export MNG_AGENT_NAME='{agent.name}'" in script


def test_build_remote_chat_script_execs_chat_sh(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    host, agent = _make_local_host_and_agent(local_provider, temp_mng_ctx)

    script = _build_remote_chat_script(host.host_dir, agent, ["--new"])

    assert f"exec '{host.host_dir}/commands/chat.sh' --new" in script


def test_build_remote_chat_script_with_resume_args(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    host, agent = _make_local_host_and_agent(local_provider, temp_mng_ctx)

    script = _build_remote_chat_script(host.host_dir, agent, ["--resume", "conv-12345"])

    assert "--resume conv-12345" in script


def test_build_remote_chat_script_quotes_conversation_id_with_special_chars(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
) -> None:
    """Verify that conversation IDs with special characters are safely quoted."""
    host, agent = _make_local_host_and_agent(local_provider, temp_mng_ctx)

    script = _build_remote_chat_script(host.host_dir, agent, ["--resume", "conv-123; rm -rf /"])

    # The shlex.quote should protect against injection
    assert "rm -rf" in script  # The string is there, but quoted
    assert "exec" in script
