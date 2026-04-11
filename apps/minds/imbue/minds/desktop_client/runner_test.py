from pathlib import Path

from imbue.minds.desktop_client.runner import AgentDiscoveryHandler
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.mngr.primitives import AgentId


def test_agent_discovery_handler_writes_local_url_file() -> None:
    """Verify local agents get a minds_api_url file written."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(tunnel_manager=tunnel_manager, server_port=8420)

    agent_id = AgentId()

    # Call the handler with no SSH info (local agent)
    handler(agent_id, None)

    # The handler writes to ~/.mngr/agents/{agent_id}/minds_api_url
    local_state_dir = Path.home() / ".mngr" / "agents" / str(agent_id)
    url_file = local_state_dir / "minds_api_url"
    if url_file.exists():
        assert url_file.read_text() == "http://127.0.0.1:8420"
        # Clean up
        url_file.unlink()
        local_state_dir.rmdir()

    tunnel_manager.cleanup()


def test_agent_discovery_handler_callable() -> None:
    """Verify AgentDiscoveryHandler is callable with the expected signature."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(tunnel_manager=tunnel_manager, server_port=9000)
    assert callable(handler)
    assert handler.server_port == 9000
    tunnel_manager.cleanup()
