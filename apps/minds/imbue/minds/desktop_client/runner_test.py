from pathlib import Path

from imbue.minds.desktop_client.runner import AgentDiscoveryHandler
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.mngr.primitives import AgentId


def test_agent_discovery_handler_writes_local_url_file(tmp_path: Path) -> None:
    """Verify local agents get a minds_api_url file written."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=8420,
        mngr_host_dir=tmp_path,
    )

    agent_id = AgentId()

    # Call the handler with no SSH info (local agent)
    handler(agent_id, None)

    url_file = tmp_path / "agents" / str(agent_id) / "minds_api_url"
    assert url_file.exists(), "minds_api_url file was not written"
    assert url_file.read_text() == "http://127.0.0.1:8420"

    tunnel_manager.cleanup()


def test_agent_discovery_handler_callable() -> None:
    """Verify AgentDiscoveryHandler is callable with the expected signature."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(tunnel_manager=tunnel_manager, server_port=9000)
    assert callable(handler)
    assert handler.server_port == 9000
    tunnel_manager.cleanup()
