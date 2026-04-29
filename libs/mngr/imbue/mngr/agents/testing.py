"""Shared, non-fixture test utilities for agent tests."""

import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import TypeVar
from typing import overload

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import get_short_random_string

AgentT = TypeVar("AgentT", bound=BaseAgent[AgentTypeConfig])


@overload
def create_test_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_config: AgentTypeConfig | None = ...,
    agent_type: AgentTypeName | None = ...,
    *,
    extra_data: Mapping[str, Any] | None = ...,
) -> BaseAgent[AgentTypeConfig]: ...


@overload
def create_test_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_config: AgentTypeConfig | None = ...,
    agent_type: AgentTypeName | None = ...,
    *,
    extra_data: Mapping[str, Any] | None = ...,
    agent_class: type[AgentT],
) -> AgentT: ...


def create_test_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_config: AgentTypeConfig | None = None,
    agent_type: AgentTypeName | None = None,
    *,
    extra_data: Mapping[str, Any] | None = None,
    agent_class: type[BaseAgent[AgentTypeConfig]] = BaseAgent,
) -> BaseAgent[AgentTypeConfig]:
    """Create a test agent backed by a real local host filesystem.

    Accepts optional ``agent_config`` and ``agent_type`` overrides for tests
    that need non-default configuration (e.g., assemble_command tests).

    ``extra_data`` is merged into the agent's ``data.json`` after the default
    fields, so callers can inject things like ``ready_timeout_seconds``.

    ``agent_class`` lets callers substitute a ``BaseAgent`` subclass (e.g. a
    test stub that records calls).
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))

    agent_id = AgentId.generate()
    agent_name = AgentName(f"test-agent-{get_short_random_string()}")
    resolved_type = agent_type or AgentTypeName("test")
    resolved_config = agent_config or AgentTypeConfig(command=CommandString("sleep 1000"))
    create_time = datetime.now(timezone.utc)

    agent_dir = local_provider.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": str(resolved_type),
        "work_dir": str(temp_work_dir),
        "create_time": create_time.isoformat(),
        "start_on_boot": False,
    }
    if resolved_config.command is not None:
        data["command"] = str(resolved_config.command)
    if extra_data is not None:
        data.update(extra_data)
    data_path = agent_dir / "data.json"
    data_path.write_text(json.dumps(data, indent=2))

    return agent_class(
        id=agent_id,
        name=agent_name,
        agent_type=resolved_type,
        work_dir=temp_work_dir,
        create_time=create_time,
        host_id=host.id,
        host=host,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=resolved_config,
    )
