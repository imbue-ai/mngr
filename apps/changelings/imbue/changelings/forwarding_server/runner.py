from pathlib import Path

import uvicorn

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.changelings.forwarding_server.app import create_forwarding_server
from imbue.changelings.forwarding_server.auth import FileAuthStore
from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import MngStreamManager
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelManager


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
    """
    paths = ChangelingPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngCliBackendResolver()
    stream_manager = MngStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()

    stream_manager.start()

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
    )

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        stream_manager.stop()
