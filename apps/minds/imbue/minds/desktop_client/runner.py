import os
import secrets
import time
import webbrowser
from pathlib import Path
from threading import Thread
from typing import Final

import paramiko
import uvicorn
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import MngrStreamManager
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.desktop_client.cloudflare_client import CloudflareSecret
from imbue.minds.desktop_client.cloudflare_client import CloudflareUsername
from imbue.minds.desktop_client.cloudflare_client import OwnerEmail
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.output import emit_event
from imbue.mngr.primitives import AgentId

_ONE_TIME_CODE_LENGTH: Final[int] = 32

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"



class AgentDiscoveryHandler(FrozenModel):
    """Handles agent discovery events by setting up reverse tunnels and writing URL files."""

    tunnel_manager: SSHTunnelManager = Field(description="SSH tunnel manager for reverse tunnels")
    server_port: int = Field(description="Local server port to forward")
    mngr_host_dir: Path = Field(
        default_factory=lambda: _DEFAULT_MNGR_HOST_DIR,
        description="Base mngr host directory for local agents (defaults to ~/.mngr)",
    )

    def __call__(self, agent_id: AgentId, ssh_info: RemoteSSHInfo | None) -> None:
        if ssh_info is not None:
            self._handle_remote_agent(agent_id, ssh_info)
        else:
            self._handle_local_agent(agent_id)

    def _handle_remote_agent(self, agent_id: AgentId, ssh_info: RemoteSSHInfo) -> None:
        try:
            agent_state_dir = self.tunnel_manager.read_remote_agent_state_dir(ssh_info, agent_id)
            remote_port = self.tunnel_manager.setup_reverse_tunnel(
                ssh_info=ssh_info,
                local_port=self.server_port,
                agent_state_dir=agent_state_dir,
            )
            api_url = f"http://127.0.0.1:{remote_port}"
            self.tunnel_manager.write_api_url_to_remote(
                ssh_info=ssh_info,
                agent_state_dir=agent_state_dir,
                url=api_url,
            )
            logger.debug("Wrote API URL {} for remote agent {}", api_url, agent_id)
        except (SSHTunnelError, OSError, paramiko.SSHException) as e:
            logger.warning("Failed to set up reverse tunnel for agent {}: {}", agent_id, e)

    def _handle_local_agent(self, agent_id: AgentId) -> None:
        local_state_dir = self.mngr_host_dir / "agents" / str(agent_id)
        api_url = f"http://127.0.0.1:{self.server_port}"
        try:
            self.tunnel_manager.write_api_url_to_local(local_state_dir, api_url)
            logger.debug("Wrote API URL {} for local agent {}", api_url, agent_id)
        except OSError as e:
            logger.warning("Failed to write API URL for local agent {}: {}", agent_id, e)


def start_desktop_client(
    data_directory: Path,
    host: str,
    port: int,
    output_format: OutputFormat,
    is_no_browser: bool = False,
) -> None:
    """Start the desktop client using uvicorn.

    Generates a one-time login URL for authentication. The URL is always
    logged to stderr. It is also emitted to stdout in the active output
    format (human-readable text or JSONL event). Unless --no-browser is
    set, the URL is opened in the system browser.
    """
    paths = WorkspacePaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngrCliBackendResolver()
    stream_manager = MngrStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()

    cloudflare_client = _build_cloudflare_client()
    agent_creator = AgentCreator(paths=paths, cloudflare_client=cloudflare_client)
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)
    is_electron = os.getenv("MINDS_ELECTRON") == "1"
    notification_dispatcher = NotificationDispatcher(is_electron=is_electron)

    # Generate a one-time login URL for the user
    code = OneTimeCode(secrets.token_urlsafe(_ONE_TIME_CODE_LENGTH))
    auth_store.add_one_time_code(code=code)
    login_url = "http://{}:{}/login?one_time_code={}".format(host, port, code)

    # Log to stderr (always)
    logger.info("Login URL (one-time use): {}", login_url)

    # Emit to stdout in the active output format so machine consumers
    # (like the Electron shell) can parse it
    emit_event(
        "login_url",
        {"login_url": login_url, "message": login_url},
        output_format,
    )

    # Register callback to set up reverse tunnels and write API URL files
    # when agents are discovered
    discovery_handler = AgentDiscoveryHandler(tunnel_manager=tunnel_manager, server_port=port)
    stream_manager.add_on_agent_discovered_callback(discovery_handler)
    stream_manager.start()

    # Start health checking for reverse tunnels
    tunnel_manager.start_reverse_tunnel_health_check()

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
        agent_creator=agent_creator,
        cloudflare_client=cloudflare_client,
        telegram_orchestrator=telegram_orchestrator,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
        stream_manager=stream_manager,
    )

    if not is_no_browser:
        thread = Thread(target=_sleep_then_open, args=(login_url,))
        thread.daemon = True
        thread.start()

    # Subprocess cleanup (stream_manager.stop(), tunnel_manager.cleanup())
    # happens in the ASGI lifespan shutdown hook inside create_desktop_client,
    # NOT in a finally block here. Uvicorn re-raises the captured SIGTERM
    # after shutdown (via signal.raise_signal), so a finally block around
    # uvicorn.run() would never execute on signal-triggered shutdown.
    #
    # timeout_graceful_shutdown=1 ensures uvicorn cancels in-flight tasks
    # quickly, giving the lifespan shutdown hook time to run within
    # electron's 5-second SIGKILL window.
    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=1)


def _build_cloudflare_client() -> CloudflareForwardingClient | None:
    """Build a CloudflareForwardingClient from environment variables, or None if not configured."""
    forwarding_url = os.environ.get("CLOUDFLARE_FORWARDING_URL")
    username = os.environ.get("CLOUDFLARE_FORWARDING_USERNAME")
    secret = os.environ.get("CLOUDFLARE_FORWARDING_SECRET")
    owner_email = os.environ.get("OWNER_EMAIL")

    if not all([forwarding_url, username, secret, owner_email]):
        logger.info("Cloudflare forwarding not configured (missing env vars), tunnel features disabled")
        return None

    assert forwarding_url is not None
    assert username is not None
    assert secret is not None
    assert owner_email is not None

    return CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl(forwarding_url),
        username=CloudflareUsername(username),
        secret=CloudflareSecret(secret),
        owner_email=OwnerEmail(owner_email),
    )


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Sleep for a short delay and then open the given URL in the default web browser."""
    time.sleep(delay)
    webbrowser.open(url)
