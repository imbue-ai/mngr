from pathlib import Path

import uvicorn

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.changelings.forwarding_server.app import create_forwarding_server
from imbue.changelings.forwarding_server.auth import FileAuthStore
from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import SubprocessMngCli
from imbue.changelings.forwarding_server.ssh_tunnel import SSHTunnelManager


def start_forwarding_server(
    data_directory: Path,
    host: str,
    port: int,
) -> None:
    """Start the forwarding server using uvicorn.

    The server discovers backend URLs by calling `mng logs <agent-id> servers.jsonl`
    and discovers agents via `mng list`. For remote agents (those with SSH info in the
    mng list output), the server tunnels traffic through SSH using paramiko.

    This ensures newly deployed changelings are immediately available without
    restarting the forwarding server, whether they are local or remote.
    """
    paths = ChangelingPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngCliBackendResolver(mng_cli=SubprocessMngCli())
    tunnel_manager = SSHTunnelManager()

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
    )

    uvicorn.run(app, host=host, port=port)
