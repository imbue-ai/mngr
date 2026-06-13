"""Non-fixture test utilities shared across mngr_wait tests."""

import json
from pathlib import Path

from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.primitives import AgentId


def create_agent_data_json(per_host_dir: Path, agent_name: str) -> AgentId:
    """Create an agent ``data.json`` so the agent (and its host) appear in discovery."""
    agent_id = AgentId.generate()
    agent_dir = get_agent_state_dir_path(per_host_dir, agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": "generic",
        "command": "sleep 1",
        "work_dir": "/tmp/test",
        "create_time": "2026-01-01T00:00:00+00:00",
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    return agent_id
