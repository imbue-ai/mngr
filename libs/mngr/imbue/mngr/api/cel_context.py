from typing import Any

from imbue.mngr.interfaces.agent import AgentInterface


def agent_to_cel_context(agent: AgentInterface, host_name: str, provider_name: str) -> dict[str, Any]:
    """Convert an agent to a CEL-friendly dict for filtering."""
    return {
        "id": str(agent.id),
        "name": str(agent.name),
        "type": str(agent.agent_type),
        "state": agent.get_lifecycle_state().value,
        "host": {
            "id": str(agent.host_id),
            "name": host_name,
            "provider": provider_name,
        },
    }
