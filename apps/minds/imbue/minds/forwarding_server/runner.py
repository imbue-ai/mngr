import secrets
import time
import webbrowser
from pathlib import Path
from threading import Thread
from typing import Final

import uvicorn
from loguru import logger

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import MngrStreamManager
from imbue.minds.forwarding_server.ssh_tunnel import SSHTunnelManager
from imbue.minds.primitives import OneTimeCode

_ONE_TIME_CODE_LENGTH: Final[int] = 32


def start_forwarding_server(
    data_directory: Path,
    host: str,
    port: int,
    # When True, skip opening the system browser and suppress duplicate
    # human-readable login URL output. The structured login_url event is
    # still emitted for the Electron shell to parse from JSONL stderr.
    is_headless: bool = False,
) -> None:
    """Start the forwarding server using uvicorn.

    Generates a one-time login URL for authentication. In normal mode the
    URL is printed to the console and opened in the system browser. In
    headless mode the URL is emitted as a structured log event (for the
    Electron shell) and the browser is not opened.

    Starts background streaming subprocesses via MngrStreamManager for
    continuous agent and server discovery.
    """
    paths = MindPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngrCliBackendResolver()
    stream_manager = MngrStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()
    agent_creator = AgentCreator(paths=paths)

    # Generate a one-time login URL for the user
    code = OneTimeCode(secrets.token_urlsafe(_ONE_TIME_CODE_LENGTH))
    auth_store.add_one_time_code(code=code)
    login_url = "http://{}:{}/login?one_time_code={}".format(host, port, code)

    if is_headless:
        # Emit login URL as a structured field -- in JSONL mode Electron's
        # backend.js detects and parses this event via the `extra.login_url` key.
        logger.info("Login URL ready", login_url=login_url)
    else:
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

    # In headless mode the Electron BrowserWindow navigates directly to the
    # login URL, so we skip opening the system browser.
    if not is_headless:
        thread = Thread(target=_sleep_then_open, args=(login_url,))
        thread.daemon = True
        thread.start()

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        stream_manager.stop()


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Sleep for a short delay and then open the given URL in the default web browser."""
    time.sleep(delay)
    webbrowser.open(url)
