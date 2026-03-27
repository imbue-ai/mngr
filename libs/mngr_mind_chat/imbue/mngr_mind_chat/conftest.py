"""Test fixtures for mngr-mind-chat.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.) and defines chat-specific fixtures below.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_mind_chat.testing import TestAgent

register_plugin_test_fixtures(globals())


@pytest.fixture
def local_host_and_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> tuple[Host, TestAgent]:
    """Create a local host and test agent for chat plugin tests."""
    host = Host(
        id=HostId(f"host-{uuid4().hex}"),
        connector=PyinfraConnector(local_provider._create_local_pyinfra_host()),
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
    )
    work_dir = tmp_path / "agent_work"
    work_dir.mkdir()
    agent = TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=temp_mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )
    return host, agent
