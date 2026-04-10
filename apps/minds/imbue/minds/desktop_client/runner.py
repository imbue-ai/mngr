import os
import secrets
import time
import webbrowser
from pathlib import Path
from threading import Thread
from typing import Final

import uvicorn
from loguru import logger

from imbue.minds.config.data_types import MindPaths
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
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.output import emit_event

_ONE_TIME_CODE_LENGTH: Final[int] = 32


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
    paths = MindPaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngrCliBackendResolver()
    stream_manager = MngrStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()

    cloudflare_client = _build_cloudflare_client()
    agent_creator = AgentCreator(paths=paths, cloudflare_client=cloudflare_client)
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)

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

    stream_manager.start()

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
        agent_creator=agent_creator,
        cloudflare_client=cloudflare_client,
        telegram_orchestrator=telegram_orchestrator,
    )

    if not is_no_browser:
        thread = Thread(target=_sleep_then_open, args=(login_url,))
        thread.daemon = True
        thread.start()

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        stream_manager.stop()


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
