from pathlib import Path

import uvicorn

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import MngStreamManager
from imbue.minds.forwarding_server.ssh_tunnel import SSHTunnelManager


def start_forwarding_server(
    data_directory: Path,
    host: str,
    port: int,
) -> None:
    """Start the forwarding server using uvicorn.

    Starts background streaming subprocesses via MngStreamManager:
    - `mng list --stream` to continuously discover agents and hosts
    - `mng events <agent-id> servers/events.jsonl --follow` per agent to discover servers

    For remote agents (those with SSH info), the server tunnels traffic
    through SSH using paramiko.

    The server can also create new agents from git URLs when no agents
    exist, via the /create form or /api/create-agent API.
    """
    paths = MindPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngCliBackendResolver()
    stream_manager = MngStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()
    agent_creator = AgentCreator(
        paths=paths,
        forwarding_server_port=port,
    )

    stream_manager.start()

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
        agent_creator=agent_creator,
    )

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        stream_manager.stop()
