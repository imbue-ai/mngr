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
from supertokens_python import InputAppInfo
from supertokens_python import SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.recipe import emailpassword
from supertokens_python.recipe import emailverification
from supertokens_python.recipe import session as session_recipe
from supertokens_python.recipe import thirdparty
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MindsConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
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
from imbue.minds.desktop_client.supertokens_auth import SuperTokensSessionStore
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.output import emit_event
from imbue.mngr.primitives import AgentId

_ONE_TIME_CODE_LENGTH: Final[int] = 32


_REMOTE_HOST_DIR: Final[str] = "/mngr"


class AgentDiscoveryHandler(FrozenModel):
    """Handles agent discovery events by setting up reverse tunnels and writing URL files."""

    tunnel_manager: SSHTunnelManager = Field(description="SSH tunnel manager for reverse tunnels")
    server_port: int = Field(description="Local server port to forward")
    mngr_host_dir: Path = Field(description="Base mngr host directory for local agents")
    data_dir: Path = Field(description="Minds data directory for looking up stored tunnel tokens")

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
    paths: WorkspacePaths,
    minds_config: MindsConfig,
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
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    backend_resolver = MngrCliBackendResolver()
    stream_manager = MngrStreamManager(resolver=backend_resolver)
    tunnel_manager = SSHTunnelManager()

    cloudflare_client = _build_cloudflare_client(minds_config.cloudflare_forwarding_url)
    agent_creator = AgentCreator(paths=paths)
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)
    is_electron = os.getenv("MINDS_ELECTRON") == "1"
    notification_dispatcher = NotificationDispatcher(is_electron=is_electron)

    # Initialize SuperTokens if configured
    supertokens_session_store = _init_supertokens(
        data_directory=paths.data_dir,
        connection_uri=str(minds_config.supertokens_connection_uri),
        host=host,
        port=port,
    )

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
    discovery_handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=port,
        mngr_host_dir=paths.mngr_host_dir,
        data_dir=paths.data_dir,
    )
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
        minds_config=minds_config,
        stream_manager=stream_manager,
        supertokens_session_store=supertokens_session_store,
        server_port=port,
        output_format=output_format,
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


def _init_supertokens(
    data_directory: Path,
    connection_uri: str,
    host: str,
    port: int,
) -> SuperTokensSessionStore | None:
    """Initialize the SuperTokens SDK and return a session store.

    The connection URI is supplied by MindsConfig (defaulted to the dev
    deployment, overridable via env var). The API key and OAuth client
    credentials remain env-var-only since they are secrets.
    """
    api_key = os.environ.get("SUPERTOKENS_API_KEY")
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    github_client_id = os.environ.get("GITHUB_CLIENT_ID")
    github_client_secret = os.environ.get("GITHUB_CLIENT_SECRET")

    # Build OAuth provider list
    providers: list[ProviderInput] = []
    if google_client_id and google_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="google",
                    clients=[
                        ProviderClientConfig(
                            client_id=google_client_id,
                            client_secret=google_client_secret,
                        )
                    ],
                ),
            )
        )
    if github_client_id and github_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="github",
                    clients=[
                        ProviderClientConfig(
                            client_id=github_client_id,
                            client_secret=github_client_secret,
                        )
                    ],
                ),
            )
        )

    api_domain = f"http://{host}:{port}"

    supertokens_init(
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key,
        ),
        app_info=InputAppInfo(
            app_name="Minds",
            api_domain=api_domain,
            website_domain=api_domain,
            api_base_path="/auth",
            website_base_path="/auth",
        ),
        framework="fastapi",
        recipe_list=[
            session_recipe.init(),
            emailpassword.init(),
            thirdparty.init(
                sign_in_and_up_feature=thirdparty.SignInAndUpFeature(providers=providers),
            )
            if providers
            else thirdparty.init(),
            emailverification.init(mode="REQUIRED"),
        ],
        mode="asgi",
    )

    session_store = SuperTokensSessionStore(
        data_directory=data_directory / "supertokens",
    )
    logger.info("SuperTokens initialized (core: {})", connection_uri)
    return session_store


def _build_cloudflare_client(forwarding_url: AnyUrl) -> CloudflareForwardingClient:
    """Build a CloudflareForwardingClient from minds config + env-var-only auth fields.

    The forwarding URL always comes from MindsConfig (with built-in default).
    Basic auth credentials (username, secret, owner_email) stay env-var-only
    because they are secrets; if unset, Basic Auth is unavailable and callers
    must use the SuperTokens-enriched client path instead.
    """
    username = os.environ.get("CLOUDFLARE_FORWARDING_USERNAME")
    secret = os.environ.get("CLOUDFLARE_FORWARDING_SECRET")
    owner_email = os.environ.get("OWNER_EMAIL")

    return CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl(str(forwarding_url)),
        username=CloudflareUsername(username) if username else None,
        secret=CloudflareSecret(secret) if secret else None,
        owner_email=OwnerEmail(owner_email) if owner_email else None,
    )


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Sleep for a short delay and then open the given URL in the default web browser."""
    time.sleep(delay)
    webbrowser.open(url)
