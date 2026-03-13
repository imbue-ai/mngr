import secrets
from pathlib import Path
from typing import Final

import uvicorn
from loguru import logger

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import MngStreamManager
from imbue.minds.forwarding_server.ssh_tunnel import SSHTunnelManager
from imbue.minds.primitives import OneTimeCode

_ONE_TIME_CODE_LENGTH: Final[int] = 32


def start_forwarding_server(
    data_directory: Path,
    host: str,
    port: int,
) -> None:
    """Start the forwarding server using uvicorn.

    Generates a one-time login URL and prints it to the console so the
    user can authenticate. Starts background streaming subprocesses via
    MngStreamManager for continuous agent and server discovery.
    """
    paths = MindPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngCliBackendResolver()
    stream_manager = MngStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()
    agent_creator = AgentCreator(paths=paths)

    # Generate a one-time login URL for the user
    code = OneTimeCode(secrets.token_urlsafe(_ONE_TIME_CODE_LENGTH))
    auth_store.add_one_time_code(code=code)
    login_url = "http://{}:{}/login?one_time_code={}".format(host, port, code)

    logger.info("")
    logger.info("Login URL (one-time use):")
    logger.info("  {}", login_url)
    logger.info("")

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
