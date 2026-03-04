"""Test fixtures for mng-changeling-chat.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, etc.) and defines chat-specific fixtures below.
"""

import json
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
from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


class _TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name."""

    def get_expected_process_name(self) -> str:
        return "test-process"


@pytest.fixture
def local_host_and_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> tuple[Host, _TestAgent]:
    """Create a local host and test agent for chat plugin tests."""
    host = Host(
        id=HostId(f"host-{uuid4().hex}"),
        connector=PyinfraConnector(local_provider._create_local_pyinfra_host()),
        provider_instance=local_provider,
        mng_ctx=temp_mng_ctx,
    )
    # Use a real temp directory so host.execute_command(cwd=work_dir) works
    work_dir = tmp_path / "agent_work"
    work_dir.mkdir()
    agent = _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mng_ctx=temp_mng_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )
    return host, agent


def create_conversation_events(
    host: Host,
    agent: _TestAgent,
    conversations: list[dict[str, str]],
) -> None:
    """Create conversation event files on the host for testing."""
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    conv_dir = agent_state_dir / "events" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for conv in conversations:
        lines.append(json.dumps(conv))
    (conv_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


def create_message_events(
    host: Host,
    agent: _TestAgent,
    messages: list[dict[str, str]],
) -> None:
    """Create message event files on the host for testing."""
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    msg_dir = agent_state_dir / "events" / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for msg in messages:
        lines.append(json.dumps(msg))
    (msg_dir / "events.jsonl").write_text("\n".join(lines) + "\n")
