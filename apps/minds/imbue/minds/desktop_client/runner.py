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
from pydantic import AnyUrl
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import MngrStreamManager
from imbue.minds.desktop_client.cloudflare_client import CloudflareClient
from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.latchkey.core import LATCHKEY_BINARY
from imbue.minds.desktop_client.latchkey.core import Latchkey
from imbue.minds.desktop_client.latchkey.core import LatchkeyDestructionHandler
from imbue.minds.desktop_client.latchkey.core import LatchkeyDiscoveryHandler
from imbue.minds.desktop_client.latchkey.core import LatchkeyReconcileCallback
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import LatchkeyServicesCatalogError
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyClient
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingRequestHandler
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.output import emit_event
from imbue.mngr.primitives import AgentId

_ONE_TIME_CODE_LENGTH: Final[int] = 32

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"


_REMOTE_HOST_DIR: Final[str] = "/mngr"


class AgentDiscoveryHandler(FrozenModel):
    """Handles agent discovery events by setting up reverse tunnels and writing URL files."""

    tunnel_manager: SSHTunnelManager = Field(description="SSH tunnel manager for reverse tunnels")
    server_port: int = Field(description="Local server port to forward")
    mngr_host_dir: Path = Field(
        default_factory=lambda: _DEFAULT_MNGR_HOST_DIR,
        description="Base mngr host directory for local agents (defaults to ~/.mngr)",
    )
    data_dir: Path = Field(
        default_factory=lambda: _DEFAULT_MNGR_HOST_DIR.parent / ".minds",
        description="Minds data directory for looking up stored tunnel tokens",
    )

    def __call__(self, agent_id: AgentId, ssh_info: RemoteSSHInfo | None, provider_name: str) -> None:
        if ssh_info is not None:
            self._handle_remote_agent(agent_id, ssh_info)
        else:
            self._handle_local_agent(agent_id)

    @staticmethod
    def _remote_host_dir() -> str:
        """Return the mngr host directory for a remote provider."""
        return _REMOTE_HOST_DIR

    def _handle_remote_agent(self, agent_id: AgentId, ssh_info: RemoteSSHInfo) -> None:
        host_dir = self._remote_host_dir()
        agent_state_dir = f"{host_dir}/agents/{agent_id}"
        try:
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

        # Inject stored tunnel token if one exists for this agent
        self._inject_stored_tunnel_token(agent_id)

    def _inject_stored_tunnel_token(self, agent_id: AgentId) -> None:
        """If a tunnel token is stored for this agent, inject it via mngr exec."""
        token = load_tunnel_token(self.data_dir, agent_id)
        if token is None:
            return
        inject_tunnel_token_into_agent(agent_id, token)

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
    is_electron = os.getenv("MINDS_ELECTRON") == "1"
    notification_dispatcher = NotificationDispatcher(is_electron=is_electron)
    backend_resolver = MngrCliBackendResolver()
    stream_manager = MngrStreamManager(resolver=backend_resolver, notification_dispatcher=notification_dispatcher)
    tunnel_manager = SSHTunnelManager()
    latchkey = _build_latchkey(data_directory=data_directory)
    latchkey.initialize(data_dir=data_directory)

    # Top-level ConcurrencyGroup that brackets the FastAPI lifespan. Every
    # subprocess/thread spawned by the desktop client (agent setup subprocesses,
    # background tunnel work, etc.) is tracked as a descendant so shutdown can
    # wait on or cancel in-flight strands via the default ``__exit__`` path.
    root_concurrency_group = ConcurrencyGroup(name="desktop-client")
    root_concurrency_group.__enter__()

    minds_config = MindsConfig(data_dir=data_directory)
    latchkey_permission_handler = LatchkeyPermissionGrantHandler(
        data_dir=data_directory,
        latchkey=latchkey,
        services_catalog=_try_load_latchkey_services_catalog(),
        mngr_message_sender=MngrMessageSender(),
    )
    cloudflare_client = _build_cloudflare_client(minds_config.remote_service_connector_url)
    auth_backend_client = AuthBackendClient(base_url=minds_config.remote_service_connector_url)
    host_pool_client = _build_host_pool_client(minds_config.remote_service_connector_url)
    litellm_key_client = _build_litellm_key_client(minds_config.remote_service_connector_url)
    agent_creator = AgentCreator(
        paths=paths,
        server_port=port,
        latchkey=latchkey,
        host_pool_client=host_pool_client,
        litellm_key_client=litellm_key_client,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
    )
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)

    # Initialize multi-account session store
    session_store = MultiAccountSessionStore(
        data_dir=data_directory,
        auth_backend_client=auth_backend_client,
    )
    sharing_request_handler = SharingRequestHandler(session_store=session_store)

    # Initialize request inbox from stored response events
    response_events = load_response_events(data_directory)
    request_inbox = RequestInbox()
    for resp in response_events:
        request_inbox = request_inbox.add_response(resp)

    # Generate a one-time login URL for the user. The URL hostname is
    # always ``localhost`` (not the internal bind address) so that the
    # session cookie issued by /authenticate -- which sets
    # ``Domain=localhost`` when the request host is on localhost -- is
    # valid on every ``<agent-id>.localhost`` subdomain the desktop
    # client forwards to. uvicorn binding 127.0.0.1 still accepts the
    # localhost-addressed requests because localhost resolves there.
    code = OneTimeCode(secrets.token_urlsafe(_ONE_TIME_CODE_LENGTH))
    auth_store.add_one_time_code(code=code)
    login_url = "http://localhost:{}/login?one_time_code={}".format(port, code)

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
    discovery_handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=port,
        data_dir=data_directory,
    )
    stream_manager.add_on_agent_discovered_callback(discovery_handler)

    # Register callbacks that spawn/terminate a dedicated ``latchkey gateway``
    # subprocess for each discovered agent. For agents running in a container,
    # VM, or VPS the handler also sets up a reverse SSH tunnel so the agent
    # can reach the host-side gateway on a constant ``127.0.0.1`` URL.
    latchkey_discovery_handler = LatchkeyDiscoveryHandler(
        latchkey=latchkey,
        tunnel_manager=tunnel_manager,
    )
    latchkey_destruction_handler = LatchkeyDestructionHandler(latchkey=latchkey)
    stream_manager.add_on_agent_discovered_callback(latchkey_discovery_handler)
    stream_manager.add_on_agent_destroyed_callback(latchkey_destruction_handler)

    # Once the initial mngr-observe snapshot arrives, reconcile any adopted
    # gateways whose agent is no longer known so orphans from the previous
    # desktop-client session are cleaned up.
    reconcile_callback = LatchkeyReconcileCallback(
        latchkey=latchkey,
        resolver=backend_resolver,
    )
    backend_resolver.add_on_change_callback(reconcile_callback)

    stream_manager.start()

    # Start health checking for reverse tunnels
    tunnel_manager.start_reverse_tunnel_health_check()

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        tunnel_manager=tunnel_manager,
        latchkey=latchkey,
        agent_creator=agent_creator,
        cloudflare_client=cloudflare_client,
        telegram_orchestrator=telegram_orchestrator,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
        stream_manager=stream_manager,
        session_store=session_store,
        auth_backend_client=auth_backend_client,
        minds_config=minds_config,
        request_inbox=request_inbox,
        request_event_handlers=(latchkey_permission_handler, sharing_request_handler),
        server_port=port,
        output_format=output_format,
        root_concurrency_group=root_concurrency_group,
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


def _try_load_latchkey_services_catalog() -> dict[str, ServicePermissionInfo]:
    """Load the latchkey services catalog, downgrading failures to a logged warning.

    A missing or malformed catalog must not prevent the desktop client
    from starting -- agents that don't try to use latchkey are unaffected.
    With an empty catalog the permission dialog renders a deny-only page
    for any incoming permission request.
    """
    try:
        return load_services_catalog()
    except LatchkeyServicesCatalogError as e:
        logger.warning("Could not load latchkey services catalog; permission dialogs disabled: {}", e)
        return {}


def _build_host_pool_client(connector_url: AnyUrl) -> HostPoolClient:
    """Build a HostPoolClient from the remote service connector URL."""
    return HostPoolClient(
        connector_url=RemoteServiceConnectorUrl(str(connector_url)),
    )


def _build_litellm_key_client(connector_url: AnyUrl) -> LiteLLMKeyClient:
    """Build a LiteLLMKeyClient from the remote service connector URL."""
    return LiteLLMKeyClient(
        connector_url=RemoteServiceConnectorUrl(str(connector_url)),
    )


def _build_cloudflare_client(connector_url: AnyUrl) -> CloudflareClient:
    """Build a shared CloudflareClient holding only the remote service connector URL.

    Per-request auth (SuperTokens token, user-id prefix, email) is attached in
    ``api_v1.get_cf_client_with_auth`` from the caller's signed-in account.
    """
    return CloudflareClient(
        connector_url=RemoteServiceConnectorUrl(str(connector_url)),
    )


def _build_latchkey(data_directory: Path) -> Latchkey:
    """Build a ``Latchkey`` wrapper honoring minds-level env var overrides.

    ``MINDS_LATCHKEY_BINARY`` can override the path to the ``latchkey`` CLI
    (typically supplied by the Electron shell, which installs the npm
    package under its own ``node_modules``). ``MINDS_LATCHKEY_DIRECTORY``
    overrides the shared ``LATCHKEY_DIRECTORY`` that every spawned subprocess
    inherits; when unset we default to ``<minds_data_dir>/latchkey`` so all
    invocations share a single credential store instead of scribbling into
    ``~/.latchkey``.
    """
    binary_override = os.environ.get("MINDS_LATCHKEY_BINARY")
    latchkey_binary = binary_override if binary_override else LATCHKEY_BINARY

    directory_override = os.environ.get("MINDS_LATCHKEY_DIRECTORY")
    if directory_override:
        latchkey_directory: Path | None = Path(directory_override).expanduser()
    else:
        latchkey_directory = data_directory / "latchkey"

    return Latchkey(
        latchkey_binary=latchkey_binary,
        latchkey_directory=latchkey_directory,
    )


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Sleep for a short delay and then open the given URL in the default web browser."""
    time.sleep(delay)
    webbrowser.open(url)
