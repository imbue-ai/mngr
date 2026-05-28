import asyncio
import concurrent.futures
import html
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from urllib.parse import urlparse

import httpx
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.bootstrap import is_imbue_cloud_provider_enabled_for_account
from imbue.minds.bootstrap import list_disabled_provider_names
from imbue.minds.bootstrap import set_provider_is_enabled
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.agent_creator import resolve_template_version
from imbue.minds.desktop_client.api_v1 import create_api_v1_router
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.deps import BackendResolverDep
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import delete_destroying
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.destroying import lookup_host_id
from imbue.minds.desktop_client.destroying import read_destroying
from imbue.minds.desktop_client.destroying import read_log_chunk
from imbue.minds.desktop_client.destroying import start_destroy
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import ProbeRecord
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.recovery_probe import parse_probe_output
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_sharing_via_cloudflare
from imbue.minds.desktop_client.sharing_handler import parse_emails_form_value
from imbue.minds.desktop_client.sharing_handler import resolve_account_email_for_workspace
from imbue.minds.desktop_client.supertokens_routes import create_supertokens_router
from imbue.minds.desktop_client.supertokens_routes import signout_user_via_plugin
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.templates import render_accounts_page
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_creating_page
from imbue.minds.desktop_client.templates import render_destroying_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_welcome_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates import status_text_for
from imbue.minds.desktop_client.templates import workspace_accent
from imbue.minds.desktop_client.tunnel_token_injection import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.webdav import create_webdav_app
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServiceName
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0


def _json_error(message: str, status_code: int) -> Response:
    """Return a small ``{"error": ...}`` JSON response."""
    return Response(
        content=json.dumps({"error": message}),
        media_type="application/json",
        status_code=status_code,
    )


def _enqueue_health_change(
    health_queue: "asyncio.Queue[tuple[str, AgentHealth]]",
    change_event: asyncio.Event,
    agent_id: AgentId,
    status: AgentHealth,
) -> None:
    """Push a health-change event into ``health_queue`` and wake the SSE loop."""
    health_queue.put_nowait((str(agent_id), status))
    change_event.set()


def _system_interface_status_payload(
    tracker: "SystemInterfaceHealthTracker | None",
    agent_id: str,
    status: AgentHealth,
) -> dict[str, str]:
    """Build a ``system_interface_status`` SSE payload, including the failure reason for RESTART_FAILED."""
    payload: dict[str, str] = {"type": "system_interface_status", "agent_id": agent_id, "status": status.value}
    if status == AgentHealth.RESTART_FAILED and tracker is not None:
        error = tracker.get_last_restart_error(AgentId(agent_id))
        if error is not None:
            payload["error"] = error
    return payload


# -- Dependency injection helpers --


def _get_auth_store(request: Request) -> AuthStoreInterface:
    return request.app.state.auth_store


AuthStoreDep = Annotated[AuthStoreInterface, Depends(_get_auth_store)]


def _get_mngr_forward_origin(request: Request) -> str:
    """Build the bare-origin URL of the ``mngr forward`` plugin.

    Used by templates to construct ``/goto/<agent>/`` URLs that target the
    plugin (which owns subdomain forwarding) rather than minds.
    """
    port = request.app.state.mngr_forward_port or 8421
    return f"http://localhost:{port}"


# -- Auth helpers --


def _is_authenticated(
    cookies: Mapping[str, str],
    auth_store: AuthStoreInterface,
) -> bool:
    """Check whether the user has a valid global session cookie."""
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = auth_store.get_signing_key()
    cookie_value = cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(
        cookie_value=cookie_value,
        signing_key=signing_key,
    )


# -- Lifespan --


@asynccontextmanager
async def _managed_lifespan(
    inner_app: FastAPI,
    is_externally_managed_client: bool,
) -> AsyncGenerator[None, None]:
    """Manage the httpx client lifecycle and capture the running event loop.

    SSH tunnels (forward + reverse) live in ``cli/run.py``'s
    ``SSHTunnelManager``, which is solely used by the surviving Latchkey
    discovery callback and is cleaned up by ``cli/run.py``.
    """
    if not is_externally_managed_client:
        inner_app.state.http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
    # Captured here so background callbacks (e.g. the mngr event refresh
    # dispatch) can schedule async work on the server's running loop via
    # asyncio.run_coroutine_threadsafe.
    inner_app.state.event_loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        # Signal SSE handlers to exit before anything else. Setting the
        # event alone isn't enough -- the chrome SSE blocks on a
        # ``change_event.wait()`` with a 30s timeout, so it'd take up to
        # 30s to notice the shutdown. Poke the backend resolver's
        # change callback (which fires the same change_event) to wake
        # every chrome SSE handler immediately; they then see the
        # shutdown event set and return cleanly from their generators.
        inner_app.state.shutdown_event.set()
        backend_resolver = inner_app.state.backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
        # Clear the captured loop reference first so background callbacks that
        # race with shutdown see None and drop their events instead of trying
        # to schedule on a loop that is about to close.
        inner_app.state.event_loop = None
        if not is_externally_managed_client:
            await inner_app.state.http_client.aclose()
        # Stop every long-lived strand that's blocked on external I/O
        # BEFORE draining the concurrency group. The CG's __exit__ just
        # joins threads with a timeout; threads blocked on subprocess
        # pipes or socket reads with no read timeout can't unblock on
        # their own. If we drain the CG while they're still wedged, it
        # times out waiting for them and surfaces "N strands did not
        # finish in time" warnings on every clean shutdown.
        #
        # Order matters within this block only to the extent that each
        # stop() returns quickly; their effects (terminate subprocess,
        # close httpx connection) all unblock threads independently.
        # Redundant cleanup calls in ``cli/run.py``'s finally block
        # remain as fallbacks for startup-error paths that never reach
        # this lifespan teardown.
        envelope_stream_consumer = inner_app.state.envelope_stream_consumer
        if envelope_stream_consumer is not None:
            # SIGTERMs the mngr forward subprocess; closes its pipes so
            # the three mngr-forward-{stdout,stderr,lifecycle} reader
            # threads exit their for-line loops.
            envelope_stream_consumer.terminate()
        permission_requests_consumer = inner_app.state.permission_requests_consumer
        if permission_requests_consumer is not None:
            # Sets the consumer's stop event AND closes the in-flight
            # follow-stream httpx client so the latchkey-permission-
            # requests-consumer thread unblocks from its iter_lines read
            # (which uses read=None timeout and otherwise blocks forever
            # waiting for the gateway to push the next request).
            permission_requests_consumer.stop()
        # Exit the root ConcurrencyGroup. ``__exit__`` waits up to
        # ``shutdown_timeout_seconds`` for any still-in-flight strands (e.g.
        # a detached tunnel-setup task) to finish.
        root_concurrency_group: ConcurrencyGroup | None = inner_app.state.root_concurrency_group
        if root_concurrency_group is not None:
            logger.info("Exiting root concurrency group...")
            try:
                root_concurrency_group.__exit__(None, None, None)
            except ConcurrencyExceptionGroup as exc:
                # Strands reported failures or timed out during shutdown;
                # log but don't propagate so other cleanup below can run.
                logger.warning("Root concurrency group exit reported errors: {}", exc)


# -- Route handlers (module-level, using Depends for dependency injection) --


def _handle_login(
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    code = OneTimeCode(one_time_code)

    # If user already has a valid session, redirect to landing page
    if _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=307, headers={"Location": "/"})

    # Render JS redirect to /authenticate (prevents prefetch consumption)
    html = render_login_redirect_page(one_time_code=code)
    return HTMLResponse(content=html)


def _handle_authenticate(
    one_time_code: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    code = OneTimeCode(one_time_code)

    is_valid = auth_store.validate_and_consume_code(code=code)

    if not is_valid:
        html = render_auth_error_page(message="This login code is invalid or has already been used.")
        return HTMLResponse(content=html, status_code=403)

    # Set a host-only session cookie on the bare origin. We do NOT try to
    # share the cookie across `<agent-id>.localhost` subdomains via
    # ``Domain=localhost`` -- both curl and Chromium treat ``localhost`` as
    # a public suffix and refuse to send such cookies to subdomains. Each
    # subdomain gets its own cookie set on first visit, minted via the
    # ``/goto/{agent_id}/`` auth-bridge redirect below.
    signing_key = auth_store.get_signing_key()
    cookie_value = create_session_cookie(signing_key=signing_key)

    response = Response(status_code=307, headers={"Location": "/"})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


def _handle_welcome_page(request: Request, auth_store: AuthStoreDep) -> Response:
    """Render the welcome/splash page for first-time users."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        html = render_login_page()
        return HTMLResponse(content=html)
    html = render_welcome_page()
    return HTMLResponse(content=html)


def _handle_landing_page(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        html = render_login_page()
        return HTMLResponse(content=html)

    all_agent_ids = backend_resolver.list_known_workspace_ids()
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    destroying_status_by_agent_id = _resolve_destroying_for_landing(paths, all_agent_ids)

    if all_agent_ids:
        telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
        telegram_status: dict[str, bool] | None = None
        if telegram_orchestrator is not None:
            telegram_status = {str(aid): telegram_orchestrator.agent_has_telegram(aid) for aid in all_agent_ids}
        agent_names: dict[str, str] = {}
        for aid in all_agent_ids:
            ws_name = backend_resolver.get_workspace_name(aid)
            if ws_name:
                agent_names[str(aid)] = ws_name
            else:
                info = backend_resolver.get_agent_display_info(aid)
                agent_names[str(aid)] = info.agent_name if info else str(aid)
        html = render_landing_page(
            accessible_agent_ids=all_agent_ids,
            mngr_forward_origin=_get_mngr_forward_origin(request),
            telegram_status_by_agent_id=telegram_status,
            agent_names=agent_names,
            destroying_status_by_agent_id=destroying_status_by_agent_id,
        )
        return HTMLResponse(content=html)

    # No agents discovered yet. If discovery is still in progress, show a
    # "Discovering agents..." page with auto-refresh. Once discovery has
    # completed with no agents found, show the create form so the user can
    # create their first agent instead of polling forever.
    if not backend_resolver.has_completed_initial_discovery():
        html = render_landing_page(
            accessible_agent_ids=(),
            mngr_forward_origin=_get_mngr_forward_origin(request),
            is_discovering=True,
        )
        return HTMLResponse(content=html)

    git_url = request.query_params.get("git_url", "")
    branch = request.query_params.get("branch", "")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    minds_config: MindsConfig | None = request.app.state.minds_config
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
    )
    return HTMLResponse(content=html)


# -- Agent creation route handlers --


def _run_tunnel_setup(
    agent_id: AgentId,
    imbue_cloud_cli: ImbueCloudCli,
    account_email: str,
    notification_dispatcher: NotificationDispatcher,
    agent_display_name: str,
) -> None:
    """Create a Cloudflare tunnel via the plugin and inject its token into the agent.

    Runs on a detached thread scheduled by ``_OnCreatedCallbackFactory`` on
    the desktop client's root ``ConcurrencyGroup``. Failures are logged via
    loguru and surfaced to the user via ``notification_dispatcher``.

    The plugin owns all tunnel state (token, services, auth policy);
    minds keeps no local cache. ``create_tunnel`` is idempotent on the
    connector side, so re-injecting on every agent (re)creation just
    delivers the existing token rather than rotating.
    """
    try:
        info = imbue_cloud_cli.create_tunnel(account=account_email, agent_id=str(agent_id))
    except ImbueCloudCliError as exc:
        logger.warning("Failed to create tunnel for {}: {}", agent_id, exc)
        _notify_tunnel_failure(
            notification_dispatcher=notification_dispatcher,
            agent_display_name=agent_display_name,
            error_message=str(exc),
        )
        return
    if info.token is None:
        logger.warning("Tunnel created for {} but no token returned", agent_id)
        return
    inject_tunnel_token_into_agent(agent_id, info.token.get_secret_value())
    logger.debug("Injected tunnel token into agent {}", agent_id)


def _notify_tunnel_failure(
    notification_dispatcher: NotificationDispatcher,
    agent_display_name: str,
    error_message: str,
) -> None:
    """Dispatch an OS notification for a tunnel-setup failure (no rate limit).

    ``NotificationDispatcher.dispatch`` spawns its own background thread or
    subprocess per channel and swallows channel-specific errors internally,
    so a top-level ``except`` wrapper here would only mask genuine bugs.
    """
    notification_dispatcher.dispatch(
        NotificationRequest(
            title="Tunnel setup failed",
            message=(
                f"Couldn't set up the Cloudflare tunnel for '{agent_display_name}'. "
                f"Sharing may be unavailable. Error: {error_message}"
            ),
            urgency=NotificationUrgency.NORMAL,
        ),
        agent_display_name=agent_display_name,
    )


class _OnCreatedCallbackFactory(MutableModel):
    """Callable that records the workspace<->account association and schedules Cloudflare tunnel setup.

    ``__call__`` is the single hook that runs once the inner ``mngr create``
    has returned the canonical ``AgentId`` -- before this refactor minds
    pre-generated an id and associated it with the account synchronously
    in the route handler, but for imbue_cloud agents that pre-generated
    id is fictional (the lease forces it back to the pool host's pre-baked
    id), so the association ended up keyed under a phantom row. We now
    do the ``associate_workspace`` call here, where ``agent_id`` is
    guaranteed canonical.

    The tunnel-setup work is scheduled on a detached thread on the root
    ``ConcurrencyGroup`` so the agent-creation thread can flip status to
    ``DONE`` without waiting on a multi-second Cloudflare round-trip.
    """

    session_store: MultiAccountSessionStore = Field(frozen=True, description="Session store for account lookup")
    imbue_cloud_cli: ImbueCloudCli = Field(
        frozen=True,
        description="CLI wrapper for `mngr imbue_cloud tunnels create`.",
    )
    root_concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description="Root group on which the detached tunnel task is scheduled.",
    )
    notification_dispatcher: NotificationDispatcher = Field(
        frozen=True,
        description="Dispatcher for surfacing tunnel-setup failures as OS notifications.",
    )
    backend_resolver: BackendResolverInterface = Field(
        frozen=True,
        description=(
            "Backend resolver pinged via notify_change() after the association write so the "
            "chrome SSE workspace list refreshes its 'account' field without waiting for the "
            "next 30s discovery heartbeat."
        ),
    )
    account_id: str = Field(
        frozen=True,
        default="",
        description=(
            "Account that owns this workspace. Empty when no account is selected (private "
            "workspace), in which case no association is recorded and no tunnel is set up."
        ),
    )

    def __call__(self, agent_id: AgentId) -> None:
        if not self.account_id:
            return
        # Bind the workspace to the account using the canonical agent id --
        # this is what later ``get_account_for_workspace`` lookups (e.g. for
        # the destruction handler) expect to find.
        self.session_store.associate_workspace(self.account_id, str(agent_id))
        # Wake the chrome SSE so the workspace tile picks up its new
        # 'account' field immediately. Without this, the chrome shows
        # the workspace as unassociated until the next discovery cycle
        # (~30s+) writes an unrelated change.
        if isinstance(self.backend_resolver, MngrCliBackendResolver):
            self.backend_resolver.notify_change()
        account = self.session_store.get_account_for_workspace(str(agent_id))
        if account is None:
            # The account vanished between selection and now (logout?). The
            # association above is still in place; we just skip the tunnel.
            return
        # ``_build_on_created_callback`` doesn't have easy access to the
        # user-chosen name at this point (see ``backend_resolver``), so fall
        # back to the short form of the agent id for the notification copy.
        agent_display_name = str(agent_id)[:8]
        self.root_concurrency_group.start_new_thread(
            target=_run_tunnel_setup,
            kwargs={
                "agent_id": agent_id,
                "imbue_cloud_cli": self.imbue_cloud_cli,
                "account_email": str(account.email),
                "notification_dispatcher": self.notification_dispatcher,
                "agent_display_name": agent_display_name,
            },
            name=f"tunnel-setup-{agent_id}",
            # is_checked=False so that a failing tunnel task does not poison
            # the root CG for unrelated strands; failures are surfaced via
            # notifications + loguru from within ``_run_tunnel_setup``.
            is_checked=False,
        )


def _build_on_created_callback(
    request: Request,
    account_id: str,
) -> _OnCreatedCallbackFactory | None:
    """Build a callback that injects the tunnel token after agent creation.

    Returns None if no account is selected (nothing to inject).
    """
    if not account_id:
        return None

    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    imbue_cloud_cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    root_concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    notification_dispatcher: NotificationDispatcher | None = request.app.state.notification_dispatcher
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver

    if (
        session_store is None
        or imbue_cloud_cli is None
        or root_concurrency_group is None
        or notification_dispatcher is None
    ):
        return None

    return _OnCreatedCallbackFactory(
        session_store=session_store,
        imbue_cloud_cli=imbue_cloud_cli,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=backend_resolver,
        account_id=account_id,
    )


async def _handle_create_form_submit(request: Request, auth_store: AuthStoreDep) -> Response:
    """Handle form submission to create a new agent."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    form = await request.form()
    git_url = str(form.get("git_url", "")).strip()
    host_name = str(form.get("host_name", "")).strip()
    branch = str(form.get("branch", "")).strip()
    try:
        launch_mode = LaunchMode(str(form.get("launch_mode", LaunchMode.DOCKER.value)))
    except ValueError:
        launch_mode = LaunchMode.DOCKER
    try:
        ai_provider = AIProvider(str(form.get("ai_provider", AIProvider.SUBSCRIPTION.value)))
    except ValueError:
        ai_provider = AIProvider.SUBSCRIPTION
    account_id = str(form.get("account_id", "")).strip()
    anthropic_api_key = str(form.get("anthropic_api_key", "")).strip()
    gh_token = str(form.get("gh_token", "")).strip()

    session_store_inst: MultiAccountSessionStore | None = request.app.state.session_store

    def _re_render_with_error(message: str, status: int = 400) -> Response:
        accounts_list = session_store_inst.list_accounts() if session_store_inst else []
        # Re-render with the user's submitted account_id pre-selected
        # (including "" -> "No account") rather than the config default,
        # so a validation error doesn't silently revert their choice.
        html_body = render_create_form(
            git_url=git_url,
            host_name=host_name,
            branch=branch,
            launch_mode=launch_mode,
            ai_provider=ai_provider,
            accounts=accounts_list,
            default_account_id=account_id,
            gh_token=gh_token,
            anthropic_api_key=anthropic_api_key,
            error_message=message,
        )
        return HTMLResponse(content=html_body, status_code=status)

    if not git_url:
        return _re_render_with_error("Repository URL is required.")

    # Validate the host name eagerly so the user sees the error inline on
    # the form rather than as a deferred "FAILED" status on the creating
    # page. An empty value falls through; ``start_creation`` substitutes a
    # repo-derived fallback for the API path.
    if host_name:
        try:
            HostName(host_name)
        except InvalidName as exc:
            return _re_render_with_error(str(exc))

    is_imbue_cloud_compute = launch_mode is LaunchMode.IMBUE_CLOUD
    is_imbue_cloud_ai = ai_provider is AIProvider.IMBUE_CLOUD
    if not account_id and (is_imbue_cloud_compute or is_imbue_cloud_ai):
        return _re_render_with_error(
            "imbue_cloud requires an account. Select an account or pick a different "
            "option for both the compute and AI providers."
        )

    if ai_provider is AIProvider.API_KEY and not anthropic_api_key:
        return _re_render_with_error("An Anthropic API key is required when AI provider is set to api_key.")

    # Resolve the account email when needed (imbue_cloud compute or AI). The
    # mngr_imbue_cloud plugin owns the SuperTokens session and is responsible
    # for fetching a fresh access token at the time of each subprocess
    # invocation, so minds only needs to know which account to ask for.
    account_email = ""
    if account_id and session_store_inst is not None and (is_imbue_cloud_compute or is_imbue_cloud_ai):
        account_email = session_store_inst.get_account_email(account_id) or ""

    branch_or_tag = branch
    if is_imbue_cloud_compute and not branch_or_tag:
        branch_or_tag = resolve_template_version(git_url, branch, parent_cg=agent_creator.root_concurrency_group)

    # Build a post-creation callback that injects the tunnel token
    on_created = _build_on_created_callback(request, account_id)

    # ``start_creation`` returns a CreationId (minds-internal handle for
    # tracking the in-flight create) -- the canonical AgentId only exists
    # after ``mngr create`` returns. Workspace<->account association is now
    # done from the on_created callback (which fires post-canonical-id) so
    # the association is keyed under the right id.
    creation_id = agent_creator.start_creation(
        git_url,
        host_name=host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        branch_or_tag=branch_or_tag,
        anthropic_api_key=anthropic_api_key,
        gh_token=gh_token,
        on_created=on_created,
    )

    creating_url = "/creating/{}".format(creation_id)
    return Response(status_code=303, headers={"Location": creating_url})


def _handle_create_page(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Show the create form page (GET /create)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    git_url = request.query_params.get("git_url", "")
    branch = request.query_params.get("branch", "")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    minds_config: MindsConfig | None = request.app.state.minds_config
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
    )
    return HTMLResponse(content=html)


async def _handle_create_agent_api(request: Request, auth_store: AuthStoreDep) -> Response:
    """API endpoint for creating an agent (POST /api/create-agent).

    Accepts JSON body with git_url. Returns JSON with agent_id and status.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response(
            status_code=400,
            content='{"error": "Invalid JSON body"}',
            media_type="application/json",
        )
    git_url = str(body.get("git_url", "")).strip()
    host_name = str(body.get("host_name", "")).strip()
    branch = str(body.get("branch", "")).strip()
    try:
        launch_mode = LaunchMode(str(body.get("launch_mode", LaunchMode.DOCKER.value)))
    except ValueError:
        return Response(
            status_code=400,
            content='{"error": "Invalid launch_mode"}',
            media_type="application/json",
        )
    try:
        ai_provider = AIProvider(str(body.get("ai_provider", AIProvider.SUBSCRIPTION.value)))
    except ValueError:
        return Response(
            status_code=400,
            content='{"error": "Invalid ai_provider"}',
            media_type="application/json",
        )
    anthropic_api_key = str(body.get("anthropic_api_key", "")).strip()
    gh_token = str(body.get("gh_token", "")).strip()
    account_id = str(body.get("account_id", "")).strip()
    if not git_url:
        return Response(
            status_code=400,
            content='{"error": "git_url is required"}',
            media_type="application/json",
        )
    # Validate the host name eagerly so a malformed value returns 400 from
    # the API rather than failing deferred in the background thread.
    if host_name:
        try:
            HostName(host_name)
        except InvalidName as exc:
            return Response(
                status_code=400,
                content=json.dumps({"error": str(exc)}),
                media_type="application/json",
            )
    # Mirror the form path's account requirement so the API rejects
    # imbue_cloud-without-account up front instead of failing later inside
    # the background thread with a vague MngrCommandError.
    is_imbue_cloud_compute = launch_mode is LaunchMode.IMBUE_CLOUD
    is_imbue_cloud_ai = ai_provider is AIProvider.IMBUE_CLOUD
    if not account_id and (is_imbue_cloud_compute or is_imbue_cloud_ai):
        return Response(
            status_code=400,
            content='{"error": "account_id is required when launch_mode or ai_provider is IMBUE_CLOUD"}',
            media_type="application/json",
        )
    if ai_provider is AIProvider.API_KEY and not anthropic_api_key:
        return Response(
            status_code=400,
            content='{"error": "anthropic_api_key is required when ai_provider is API_KEY"}',
            media_type="application/json",
        )

    # Resolve the account email when an imbue_cloud field is selected so the
    # background creation can mint a LiteLLM key / lease a pool host. The
    # session store is the source of truth for email <-> user_id mapping.
    account_email = ""
    if account_id and (is_imbue_cloud_compute or is_imbue_cloud_ai):
        session_store_inst: MultiAccountSessionStore | None = request.app.state.session_store
        if session_store_inst is not None:
            account_email = session_store_inst.get_account_email(account_id) or ""

    creation_id = agent_creator.start_creation(
        git_url,
        host_name=host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        anthropic_api_key=anthropic_api_key,
        gh_token=gh_token,
    )
    # API contract: the JSON field stays named ``agent_id`` for backwards
    # compatibility with existing API clients, but the value is now a
    # CreationId (minds-internal in-flight handle, distinct prefix from a
    # canonical AgentId). The status-polling endpoints accept either.
    return Response(
        content=json.dumps({"agent_id": str(creation_id), "status": str(AgentCreationStatus.INITIALIZING)}),
        media_type="application/json",
    )


def _handle_creation_status_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """API endpoint for checking agent creation status."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    # The URL parameter is named ``agent_id`` for legacy API compatibility
    # but it actually carries a ``CreationId`` (minds-internal in-flight
    # handle). The canonical mngr ``AgentId`` is reported back through
    # ``info.agent_id`` once ``mngr create`` returns.
    creation_id = CreationId(agent_id)
    info = agent_creator.get_creation_info(creation_id)
    if info is None:
        return Response(
            status_code=404,
            content='{"error": "Unknown agent creation"}',
            media_type="application/json",
        )

    result: dict[str, str] = {
        "creation_id": str(info.creation_id),
        "status": str(info.status),
    }
    if info.agent_id is not None:
        result["agent_id"] = str(info.agent_id)
    if info.redirect_url is not None:
        result["redirect_url"] = info.redirect_url
    if info.error is not None:
        result["error"] = info.error
    return Response(content=json.dumps(result), media_type="application/json")


def _handle_creating_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Show the creating progress page (GET /creating/{agent_id})."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    # ``agent_id`` route param is actually a CreationId (see comment in
    # ``_handle_creation_status_api``).
    creation_id = CreationId(agent_id)
    info = agent_creator.get_creation_info(creation_id)
    if info is None:
        return Response(status_code=404, content="Unknown agent creation")

    if info.status == AgentCreationStatus.DONE and info.redirect_url is not None:
        return Response(status_code=307, headers={"Location": info.redirect_url})

    html = render_creating_page(creation_id=creation_id, info=info)
    return HTMLResponse(content=html)


async def _stream_creation_logs(
    log_queue: queue.Queue[str],
    agent_creator: AgentCreator,
    creation_id: CreationId,
    shutdown_event: threading.Event,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE events from a creation log queue.

    Each iteration polls ``agent_creator.get_creation_info(creation_id)``
    and emits a ``{"_type": "status", ...}`` event whenever the status
    has changed since the last emission. This piggybacks on the existing
    ~1s log-queue keepalive cadence; caption-update latency is therefore
    bounded by the queue.get timeout below, which is acceptable since
    each backend phase takes much longer than 1s.

    Exits cleanly when ``shutdown_event`` is set so the server's
    graceful-shutdown deadline doesn't have to cancel us mid-stream.
    """
    last_status: AgentCreationStatus | None = None
    streaming = True
    while streaming:
        if shutdown_event.is_set():
            return
        info = agent_creator.get_creation_info(creation_id)
        if info is not None and info.status != last_status:
            last_status = info.status
            status_event = {
                "_type": "status",
                "status": str(info.status),
                "status_text": status_text_for(
                    str(info.status),
                    error=info.error,
                    launch_mode=info.launch_mode,
                ),
            }
            yield "data: {}\n\n".format(json.dumps(status_event))

        try:
            line = await asyncio.get_running_loop().run_in_executor(None, log_queue.get, True, 1.0)
        except (queue.Empty, TimeoutError, OSError):
            yield ": keepalive\n\n"
            continue

        if line == LOG_SENTINEL:
            streaming = False
            info = agent_creator.get_creation_info(creation_id)
            if info is not None:
                result = {"status": str(info.status)}
                if info.redirect_url is not None:
                    result["redirect_url"] = info.redirect_url
                if info.error is not None:
                    result["error"] = info.error
                result["_type"] = "done"
                yield "data: {}\n\n".format(json.dumps(result))
                # Yield a final keepalive so the done event is flushed to the
                # browser in its own TCP segment, separate from the stream close.
                yield ": end\n\n"
        else:
            yield "data: {}\n\n".format(json.dumps({"log": line}))


async def _handle_creation_logs_sse(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """SSE endpoint that streams creation logs for an agent."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = request.app.state.agent_creator
    if agent_creator is None:
        return Response(status_code=501, content="Agent creation not configured")

    # ``agent_id`` route param carries a CreationId (see comment in
    # ``_handle_creation_status_api``).
    creation_id = CreationId(agent_id)
    log_queue = agent_creator.get_log_queue(creation_id)
    if log_queue is None:
        return Response(status_code=404, content="Unknown agent creation")

    return StreamingResponse(
        _stream_creation_logs(log_queue, agent_creator, creation_id, request.app.state.shutdown_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Agent destruction route handlers --


def _resolve_destroying_for_landing(
    paths: WorkspacePaths | None,
    all_agent_ids: tuple[AgentId, ...],
) -> dict[str, str]:
    """Walk ``<paths.data_dir>/destroying/``, delete DONE records, return marker map.

    Returns ``{agent_id_str: "running" | "failed"}`` for any in-flight or
    failed destroy whose agent_id is currently known to the resolver. DONE
    records (pid dead AND agent missing from the resolver) are deleted on
    the spot so the row vanishes naturally on the next refresh.

    Returns an empty dict (and does no work) when ``paths`` is None --
    that path is exercised by tests that build a minimal app without
    a real data dir.
    """
    if paths is None:
        return {}
    in_resolver = frozenset(all_agent_ids)
    records = list_destroying(paths, in_resolver)
    marker: dict[str, str] = {}
    for agent_id, record in records.items():
        if record.status == DestroyingStatus.DONE:
            delete_destroying(agent_id, paths)
            continue
        marker[str(agent_id)] = "running" if record.status == DestroyingStatus.RUNNING else "failed"
    return marker


def _agent_in_resolver(request: Request, agent_id: AgentId) -> bool:
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    return agent_id in backend_resolver.list_known_workspace_ids()


async def _handle_destroy_agent_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """POST /api/destroy-agent/<agent_id>: spawn a detached destroy.

    Idempotent: if a destroy is already running for this agent, returns
    200 with the existing record's status. Otherwise spawns the
    detached subprocess and returns 202.

    Always returns ``redirect_url: "/"`` so the settings-page JS can
    immediately navigate to the landing page (where the destroying
    marker is already visible).
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=501, content='{"error": "Destroy not configured"}', media_type="application/json")

    parsed_id = AgentId(agent_id)

    # Disassociate the workspace from the session store synchronously.
    # Tokens live in the plugin's session store; minds only owns the
    # workspace<->account mapping, which we want broken before mngr
    # destroy returns regardless of whether the destroy succeeds.
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store:
        account = session_store.get_account_for_workspace(agent_id)
        if account:
            session_store.disassociate_workspace(str(account.user_id), agent_id)

    # Idempotent: short-circuit if a destroy is already running.
    existing = read_destroying(parsed_id, paths, agent_in_resolver=_agent_in_resolver(request, parsed_id))
    if existing is not None and existing.status == DestroyingStatus.RUNNING:
        return Response(
            status_code=200,
            content=json.dumps({"agent_id": agent_id, "status": "running", "redirect_url": "/"}),
            media_type="application/json",
        )

    host_id = lookup_host_id(parsed_id)
    start_destroy(parsed_id, paths, host_id)

    return Response(
        status_code=202,
        content=json.dumps({"agent_id": agent_id, "status": "running", "redirect_url": "/"}),
        media_type="application/json",
    )


def _handle_destroying_status_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """GET /api/destroying/<agent_id>/status: live status of a destroy."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    parsed_id = AgentId(agent_id)
    record = read_destroying(parsed_id, paths, agent_in_resolver=_agent_in_resolver(request, parsed_id))
    if record is None:
        return Response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    return Response(
        content=json.dumps(
            {
                "agent_id": agent_id,
                "pid": record.pid,
                "pid_alive": record.pid_alive,
                "agent_in_resolver": record.agent_in_resolver,
                "status": str(record.status).lower(),
            }
        ),
        media_type="application/json",
    )


def _handle_destroying_log_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """GET /api/destroying/<agent_id>/log?after=<bytes>: tail the destroy log."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    parsed_id = AgentId(agent_id)
    after_str = request.query_params.get("after", "0")
    try:
        after = max(int(after_str), 0)
    except ValueError:
        after = 0
    try:
        content_bytes, next_offset = read_log_chunk(parsed_id, paths, after)
    except FileNotFoundError:
        return Response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    return Response(
        content=json.dumps(
            {
                "bytes_read": len(content_bytes),
                "next_offset": next_offset,
                "content": content_bytes.decode("utf-8", errors="replace"),
            }
        ),
        media_type="application/json",
    )


def _handle_destroying_dismiss_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """POST /api/destroying/<agent_id>/dismiss: remove the destroy record."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=200, content="{}", media_type="application/json")
    parsed_id = AgentId(agent_id)
    delete_destroying(parsed_id, paths)
    return Response(status_code=200, content="{}", media_type="application/json")


def _handle_destroying_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """GET /destroying/<agent_id>: the destroy detail / log-tail page."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=404, content="No record")
    parsed_id = AgentId(agent_id)
    in_resolver = parsed_id in backend_resolver.list_known_workspace_ids()
    record = read_destroying(parsed_id, paths, agent_in_resolver=in_resolver)
    if record is None:
        return Response(status_code=404, content="No record")
    workspace_name = backend_resolver.get_workspace_name(parsed_id)
    if not workspace_name:
        info = backend_resolver.get_agent_display_info(parsed_id)
        workspace_name = info.agent_name if info else agent_id
    html = render_destroying_page(
        agent_id=parsed_id,
        agent_name=workspace_name or agent_id,
        pid=record.pid,
        status=str(record.status).lower(),
    )
    return HTMLResponse(content=html)


# -- Telegram setup route handlers --


async def _handle_telegram_setup(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Start Telegram bot setup for an agent (POST /api/agents/{agent_id}/telegram/setup)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return Response(
            status_code=501,
            content='{"error": "Telegram setup not configured"}',
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)

    # Use agent_id as the agent name for bot naming (best we have without additional lookups)
    agent_name = str(parsed_id)[:8]
    try:
        body = await request.json()
        agent_name = str(body.get("agent_name", agent_name)).strip() or agent_name
    except (json.JSONDecodeError, ValueError):
        pass

    telegram_orchestrator.start_setup(agent_id=parsed_id, agent_name=agent_name)
    return Response(
        content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.CHECKING_CREDENTIALS)}),
        media_type="application/json",
    )


def _handle_telegram_status(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Get Telegram setup status for an agent (GET /api/agents/{agent_id}/telegram/status)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return Response(
            status_code=501,
            content='{"error": "Telegram setup not configured"}',
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)
    info = telegram_orchestrator.get_setup_info(parsed_id)

    if info is None:
        # No active setup -- check if already set up
        is_active = telegram_orchestrator.agent_has_telegram(parsed_id)
        if is_active:
            return Response(
                content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.DONE)}),
                media_type="application/json",
            )
        return Response(
            status_code=404,
            content='{"error": "No Telegram setup in progress for this agent"}',
            media_type="application/json",
        )

    result: dict[str, str | None] = {
        "agent_id": str(info.agent_id),
        "status": str(info.status),
    }
    if info.error is not None:
        result["error"] = info.error
    if info.bot_username is not None:
        result["bot_username"] = info.bot_username
    return Response(content=json.dumps(result), media_type="application/json")


# -- Providers panel toggle route --


async def _handle_provider_toggle(
    provider_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Toggle ``is_enabled`` for a provider in minds' active settings and bounce observe.

    POST ``/api/providers/{provider_name}/toggle`` with body ``{"is_enabled": bool}``.
    Writes via :func:`set_provider_is_enabled`, then sends ``SIGHUP`` to
    ``mngr forward`` so it restarts its ``mngr observe`` child to pick up the
    new setting. The next ``FullDiscoverySnapshotEvent`` will reflect the
    change; the chrome's optimistic "waiting for refresh" state clears at that
    point.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Provider toggle request body was not valid JSON: {}", e)
        return Response(status_code=400, content='{"error": "Body must be JSON"}', media_type="application/json")
    # request.json() can return any JSON value (array, string, number, null, ...),
    # not just objects. Reject non-dict bodies before calling .get() so we return
    # a structured 400 rather than a 500 from an AttributeError.
    if not isinstance(body, dict):
        return Response(
            status_code=400,
            content='{"error": "Body must be a JSON object"}',
            media_type="application/json",
        )
    is_enabled = body.get("is_enabled")
    if not isinstance(is_enabled, bool):
        return Response(
            status_code=400,
            content='{"error": "Body must include is_enabled: bool"}',
            media_type="application/json",
        )
    changed = set_provider_is_enabled(provider_name, is_enabled)
    # Only bounce observe when the settings file actually changed -- a no-op toggle
    # (e.g. user clicking Disable twice) should not trigger a SIGHUP and a full
    # mngr observe restart, since the next discovery snapshot would be identical.
    if changed:
        consumer: EnvelopeStreamConsumer | None = request.app.state.envelope_stream_consumer
        if consumer is not None:
            consumer.bounce_observe()
    return Response(
        content=json.dumps({"provider_name": provider_name, "is_enabled": is_enabled, "changed": changed}),
        media_type="application/json",
    )


# -- Chrome (persistent shell) route handlers --


def _handle_chrome_page(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Serve the persistent chrome page (title bar + sidebar + content iframe).

    This route is unauthenticated -- the chrome renders for all users. The sidebar
    shows an empty state for unauthenticated users; the SSE stream populates it
    after authentication.
    """
    user_agent = request.headers.get("user-agent", "")
    is_mac = "Macintosh" in user_agent or "Mac OS" in user_agent

    authenticated = _is_authenticated(cookies=request.cookies, auth_store=auth_store)
    initial_workspaces = _build_workspace_list(backend_resolver) if authenticated else []

    html = render_chrome_page(
        is_mac=is_mac,
        is_authenticated=authenticated,
        mngr_forward_origin=_get_mngr_forward_origin(request),
        initial_workspaces=initial_workspaces,
    )
    return HTMLResponse(content=html)


def _handle_chrome_sidebar(request: Request) -> Response:
    """Serve the standalone sidebar page for the Electron sidebar WebContentsView."""
    html = render_sidebar_page(mngr_forward_origin=_get_mngr_forward_origin(request))
    return HTMLResponse(content=html)


async def _handle_chrome_events(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """SSE endpoint that streams workspace list and auth status changes to the chrome.

    The chrome subscribes to this on load. If unauthenticated, sends an auth_required
    event. Once authenticated, sends the current workspace list and pushes updates
    whenever the backend resolver's data changes (driven by MngrStreamManager's
    discovery and events streams).
    """
    authenticated = _is_authenticated(cookies=request.cookies, auth_store=auth_store)

    async def _event_generator() -> AsyncGenerator[str, None]:
        if not authenticated:
            yield "data: {}\n\n".format(json.dumps({"type": "auth_required"}))
            return

        # Use an asyncio.Event to wake up when the resolver's data changes.
        # The resolver fires callbacks from background threads, so we use
        # call_soon_threadsafe to signal the event on the event loop.
        change_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Health transitions from the system-interface tracker arrive on
        # background threads (envelope reader, probe loop, restart endpoint).
        # We accumulate them into a per-connection queue and drain them
        # in the main generator loop so each subscriber sees every event.
        health_queue: asyncio.Queue[tuple[str, AgentHealth]] = asyncio.Queue()

        def _on_change() -> None:
            loop.call_soon_threadsafe(change_event.set)

        def _on_health_change(agent_id: AgentId, status: AgentHealth) -> None:
            loop.call_soon_threadsafe(_enqueue_health_change, health_queue, change_event, agent_id, status)

        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.add_on_change_callback(_on_change)

        tracker: SystemInterfaceHealthTracker | None = request.app.state.system_interface_health_tracker
        if tracker is not None:
            tracker.add_on_change_callback(_on_health_change)

        try:
            # Send initial workspace list and request count
            session_store: MultiAccountSessionStore | None = request.app.state.session_store
            last_workspace_data = _build_workspace_list(backend_resolver, session_store)
            has_accounts = bool(session_store and session_store.list_accounts())
            yield "data: {}\n\n".format(
                json.dumps({"type": "workspaces", "workspaces": last_workspace_data, "has_accounts": has_accounts})
            )
            # Send the initial providers panel state so the chrome can render
            # the providers section before the first resolver change fires.
            last_providers_data = _build_providers_state_payload(backend_resolver)
            yield "data: {}\n\n".format(json.dumps({"type": "providers_state", **last_providers_data}))
            inbox: RequestInbox | None = request.app.state.request_inbox
            last_request_count = inbox.get_pending_count() if inbox else 0
            # ``auto_open`` is bundled with ``request_count`` (rather than its
            # own SSE event) so the Electron shell sees both atomically when
            # deciding whether to auto-open the panel on count increases.
            minds_config: MindsConfig | None = request.app.state.minds_config
            auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
            yield "data: {}\n\n".format(
                json.dumps({"type": "request_count", "count": last_request_count, "auto_open": auto_open})
            )

            if tracker is not None:
                for aid, status in tracker.snapshot_all().items():
                    yield "data: {}\n\n".format(
                        json.dumps(_system_interface_status_payload(tracker, str(aid), status))
                    )

            # Wait for changes and push updates until client disconnects.
            #
            # Loop ordering invariant: ``change_event.clear()`` runs
            # immediately after ``wait()`` returns and BEFORE draining the
            # per-connection queue. A producer always pushes to the queue
            # first and then sets the event. With this ordering:
            #
            # - Producer fires between ``wait()`` returning and ``clear()``:
            #   queue gets the item, event is wiped, but this iteration's
            #   drain catches the item.
            # - Producer fires between ``clear()`` and drain: queue gets the
            #   item, event is set again. Drain catches the item. Next
            #   ``wait()`` returns immediately, drain is empty -- a benign
            #   false wake.
            # - Producer fires after drain: event is set. Next ``wait()``
            #   returns immediately and drain catches the item.
            #
            # Clearing at the bottom of the loop instead would lose the
            # wakeup for any producer that fires between the drain and the
            # bottom-of-loop clear, leaving the queued item idle for up to
            # 30s -- a UX regression for health-state transitions like
            # RESTARTING -> HEALTHY.
            shutdown_event: threading.Event = request.app.state.shutdown_event
            connected = not await request.is_disconnected()
            while connected and not shutdown_event.is_set():
                # Wait for a change signal or timeout (timeout for disconnect checks).
                try:
                    await asyncio.wait_for(change_event.wait(), timeout=30.0)
                except TimeoutError:
                    pass
                # Clear BEFORE draining so any producer firing between drain
                # and the next ``wait()`` re-sets the event and is observed
                # promptly. See the comment above for the full invariant.
                change_event.clear()

                # Server-side shutdown signalled (via lifespan teardown
                # calling backend_resolver.notify_change() right after
                # setting shutdown_event). Exit the generator cleanly so
                # uvicorn's graceful-shutdown deadline doesn't have to
                # cancel us mid-stream.
                if shutdown_event.is_set():
                    break

                connected = not await request.is_disconnected()
                if not connected:
                    break

                while not health_queue.empty():
                    aid_str, status = health_queue.get_nowait()
                    yield "data: {}\n\n".format(json.dumps(_system_interface_status_payload(tracker, aid_str, status)))

                current_data = _build_workspace_list(backend_resolver, session_store)
                if current_data != last_workspace_data:
                    last_workspace_data = current_data
                    yield "data: {}\n\n".format(json.dumps({"type": "workspaces", "workspaces": current_data}))

                current_providers_data = _build_providers_state_payload(backend_resolver)
                if current_providers_data != last_providers_data:
                    last_providers_data = current_providers_data
                    yield "data: {}\n\n".format(json.dumps({"type": "providers_state", **current_providers_data}))

                inbox = request.app.state.request_inbox
                current_request_count = inbox.get_pending_count() if inbox else 0
                if current_request_count != last_request_count:
                    last_request_count = current_request_count
                    auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
                    yield "data: {}\n\n".format(
                        json.dumps({"type": "request_count", "count": current_request_count, "auto_open": auto_open})
                    )
        finally:
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.remove_on_change_callback(_on_change)
            if tracker is not None:
                tracker.remove_on_change_callback(_on_health_change)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Provider names that are always hidden from minds' providers panel:
# - ``local``: always present, always healthy; nothing actionable.
# - ``imbue_cloud``: the default singleton instance is non-functional. Minds
#   uses the multi-account variant (``imbue_cloud_<slug>`` per signed-in
#   account), so the default block is dead weight and surfacing it would
#   confuse users into thinking they need to enable / disable it.
# Other consumers (e.g. `mngr list` CLI) keep showing both normally -- the
# hide applies only to minds' panel.
_HIDDEN_PROVIDER_NAMES_IN_PANEL: Final[frozenset[str]] = frozenset({"local", "imbue_cloud"})


def _build_providers_state_payload(backend_resolver: BackendResolverInterface) -> dict[str, Any]:
    """Build the providers panel SSE payload from resolver state + minds' settings file.

    Combines three sources:
    - ``backend_resolver.list_providers()`` -- providers that loaded
      successfully in the most recent discovery snapshot.
    - ``backend_resolver.get_provider_errors()`` -- providers whose discovery
      raised.
    - ``list_disabled_provider_names()`` -- providers minds' settings file
      explicitly disables. These are skipped by discovery and so don't appear
      in the snapshot, but the panel needs them for the Enable button.

    The ``local`` provider is always hidden. Each entry carries name + backend
    + status; errored entries also carry ``error_type`` and ``error_message``.
    """
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return {
            "providers": [],
            "last_event_at": None,
            "last_full_snapshot_at": None,
        }
    providers = backend_resolver.list_providers()
    errored = backend_resolver.get_provider_errors()
    disabled_names = list_disabled_provider_names()
    last_event_at, last_full_snapshot_at = backend_resolver.get_freshness_timestamps()

    # De-duplicate by name with priority disabled > error > ok. A provider can
    # appear in multiple source buckets during the window between a Disable click
    # (writes to minds' settings) and mngr observe's restart (rewrites the snapshot
    # to drop the now-disabled provider). In that window the same name shows up in
    # both `disabled_names` and the resolver's errored or healthy set. The user's
    # explicitly recorded intent (disabled-in-settings) wins; transient error state
    # wins over stale healthy state.
    entry_by_name: dict[str, dict[str, Any]] = {}
    for provider in providers:
        name = str(provider.provider_name)
        if name in _HIDDEN_PROVIDER_NAMES_IN_PANEL:
            continue
        entry_by_name[name] = {
            "name": name,
            "backend": str(provider.config.backend),
            "status": "ok",
            "is_enabled": provider.config.is_enabled if provider.config.is_enabled is not None else True,
        }
    for provider_name, error in errored.items():
        name = str(provider_name)
        if name in _HIDDEN_PROVIDER_NAMES_IN_PANEL:
            continue
        entry_by_name[name] = {
            "name": name,
            "backend": None,
            "status": "error",
            "is_enabled": True,
            "error_type": error.type_name,
            "error_message": error.message,
        }
    for name in disabled_names:
        if name in _HIDDEN_PROVIDER_NAMES_IN_PANEL:
            continue
        entry_by_name[name] = {
            "name": name,
            "backend": None,
            "status": "disabled",
            "is_enabled": False,
        }
    # Stable alphabetical order by name across all categories.
    entries = sorted(entry_by_name.values(), key=lambda entry: entry["name"])
    return {
        "providers": entries,
        "last_event_at": last_event_at.isoformat() if last_event_at is not None else None,
        "last_full_snapshot_at": last_full_snapshot_at.isoformat() if last_full_snapshot_at is not None else None,
    }


def _build_workspace_list(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
) -> list[dict[str, str]]:
    """Build a JSON-serializable list of workspaces from the backend resolver.

    Each entry carries a deterministic "accent" CSS color derived from the
    agent id so the chrome and sidebar can render a per-workspace accent
    without running a digest in JS.
    """
    agent_ids = backend_resolver.list_known_workspace_ids()
    workspaces: list[dict[str, str]] = []
    for aid in agent_ids:
        ws_name = backend_resolver.get_workspace_name(aid)
        if not ws_name:
            info = backend_resolver.get_agent_display_info(aid)
            ws_name = info.agent_name if info else str(aid)
        entry: dict[str, str] = {"id": str(aid), "name": ws_name, "accent": workspace_accent(str(aid))}
        if session_store is not None:
            account = session_store.get_account_for_workspace(str(aid))
            if account is not None:
                entry["account"] = account.email
        workspaces.append(entry)
    return workspaces


# -- System-interface recovery / restart --

# Minds creates two mngr agents per workspace, both with ``work_dir=/code``
# in the same container:
#   - a ``claude``-type agent with the user-chosen name -- runs the user's
#     Claude conversation.
#   - a ``main``-type agent always named ``system-services`` -- runs the
#     bootstrap service manager, which spawns the system interface.
# The restart endpoints are invoked with the user agent's id; the recovery
# flow restarts the *system-services* agent (which shares the user agent's
# host), so it resolves that agent through the backend resolver.
#
# Two recovery tiers:
#   - System-interface restart (surgical): ``mngr stop`` + ``mngr start`` on
#     the system-services agent. The user's claude agent is untouched.
#   - Host restart: ``mngr stop --stop-host`` + ``mngr start`` on the
#     system-services agent. This bounces the whole container, so every
#     agent in the workspace is interrupted; only system-services is
#     started back up (the claude agent is started template-side on the
#     user's next message).

# How long a single workspace probe through the plugin is allowed to hang.
# Used by the background system-interface-health probe loop -- we want a short,
# snappy timeout so a wedged workspace doesn't gate the recovery UI.
_WORKSPACE_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
# Hard timeout for a single ``mngr`` stop/start subprocess during a restart.
# Generous: a host stop/start bounces a container and can legitimately take
# tens of seconds, so this is a "definitely wedged" ceiling, not an estimate.
_RESTART_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
# Shared budget for how long we wait for the system interface to answer again
# after a restart. Used by every recovery wait point; initial agent-creation
# readiness waiting deliberately keeps its own (longer) timeout.
_SYSTEM_INTERFACE_STARTUP_WAIT_SECONDS: Final[float] = 15.0
# Poll cadence while waiting for the system interface to come back post-restart.
_RESTART_PROBE_INTERVAL_SECONDS: Final[float] = 1.0
# Cap on the per-agent host-health probe cache. Entries are popped on a
# non-HEALTHY -> HEALTHY transition by _LogProbeOnRecoveryCallback, but a
# workspace that the user gives up on while RESTART_FAILED never recovers --
# so without a cap, the cache grows monotonically with every distinct
# workspace whose recovery page is ever visited. 256 is generous for any
# realistic user (dozens of workspaces) and bounds pathological cases.
_HOST_HEALTH_CACHE_MAX_ENTRIES: Final[int] = 256


def _build_mngr_stop_argv(mngr_binary: str, agent_id: AgentId, is_host_restart: bool) -> list[str]:
    """Build the argv for ``mngr stop`` on ``agent_id`` -- with ``--stop-host`` for the host tier."""
    argv = [mngr_binary, "stop", str(agent_id), "--quiet"]
    if is_host_restart:
        argv.append("--stop-host")
    return argv


def _build_mngr_start_argv(mngr_binary: str, agent_id: AgentId) -> list[str]:
    """Build the argv for ``mngr start`` on ``agent_id`` (also starts the host if it is stopped)."""
    return [mngr_binary, "start", str(agent_id), "--quiet"]


def _build_mngr_host_state_argv(
    mngr_binary: str,
    agent_id: AgentId,
    services_agent_id: AgentId | None,
) -> list[str]:
    """Build the argv for the layer-2 probe: list agents to read each host's lifecycle state.

    The recovery page keys its restart tier off the workspace host's state:
    a RUNNING host can be recovered with the surgical system-interface
    restart, while a stopped host needs a full host restart. ``mngr list``
    is a pure read -- it never starts a stopped container.

    Scopes the listing to just this workspace's chat agent + system-services
    agent via a CEL ``id == ...`` include. Smaller payload, easier to reason
    about in the diagnostics menu, and the include filter never confuses
    enumeration since the per-host SSH-tolerance fix in libs/mngr means a
    broken sibling host no longer poisons the whole provider's discovery.

    ``--on-error continue`` keeps the listing from hard-failing when one
    provider couldn't be reached (the surviving providers still emit their
    agents). The CLI exits non-zero whenever ``result.errors`` is non-empty,
    so the caller cannot rely on returncode alone -- the ``mngr_list_error``
    field in the response is sourced from stderr / parsed errors / exit
    code together.
    """
    if services_agent_id is None:
        include = f'id == "{agent_id}"'
    else:
        include = f'id == "{agent_id}" || id == "{services_agent_id}"'
    return [
        mngr_binary,
        "list",
        "--format",
        "json",
        "--quiet",
        "--include",
        include,
        "--on-error",
        "continue",
    ]


def _sanitize_recovery_return_to(raw: str) -> str:
    """Return a safe value for the recovery page's ``return_to`` parameter.

    The recovery page navigates the user back to ``return_to`` after a
    successful restart. Without validation, this is an open-redirect
    primitive: a crafted URL like ``?return_to=https://evil.com/`` would
    cause the page to navigate to an attacker-controlled site.

    The only legitimate values are:
      - Relative URLs starting with ``/`` (same-origin).
      - Absolute URLs whose host is ``localhost`` or ends in ``.localhost``
        (the convention used by the mngr_forward subdomain plugin, where
        each agent is served at ``<agent-id>.localhost:<port>``).

    Anything else is dropped (returned as ``""``) and the recovery page
    falls back to ``window.location.reload()``.
    """
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    # Relative URL with no scheme/host -- must start with a single '/' so we
    # don't accidentally allow protocol-relative URLs ("//evil.com/path"),
    # which urlparse parses with netloc="evil.com".
    if not parsed.scheme and not parsed.netloc:
        return raw if raw.startswith("/") and not raw.startswith("//") else ""
    # Absolute URL: allow only http(s) on localhost / *.localhost hosts.
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return raw
    return ""


def _handle_recovery_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Render the workspace-recovery page (shown by the 503 redirect or by direct nav)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return HTMLResponse(content=render_login_page(), status_code=403)
    aid = AgentId(agent_id)
    tracker: SystemInterfaceHealthTracker | None = request.app.state.system_interface_health_tracker
    initial_status = tracker.get_health(aid).value if tracker is not None else AgentHealth.HEALTHY.value
    initial_error = (tracker.get_last_restart_error(aid) or "") if tracker is not None else ""
    return_to = _sanitize_recovery_return_to(request.query_params.get("return_to", ""))
    is_explicit_restart = request.query_params.get("intent", "") == "restart"
    # The recovery page renders from ``render_status`` and then auto-refreshes
    # itself while a restart is in flight; every refresh re-runs this handler,
    # so the live tracker state is re-read each tick. A HEALTHY tracker needs
    # special handling rather than rendering a misleading "not responding" page.
    render_status = initial_status
    if initial_status == AgentHealth.HEALTHY.value:
        if is_explicit_restart:
            # The user explicitly asked to restart a currently-healthy
            # workspace (the home-page restart control). Render as STUCK so
            # the page runs the layer-2 probe and dispatches a restart instead
            # of sitting idle on a "healthy" page.
            render_status = AgentHealth.STUCK.value
        elif return_to:
            # The workspace recovered before this page loaded -- either a race
            # (the chrome navigated here on STUCK but the agent recovered
            # before this GET landed) or the page's own post-restart refresh
            # observing success. Either way, send the user back to where they
            # were going.
            return RedirectResponse(url=return_to, status_code=302)
        else:
            # HEALTHY with no return_to to redirect to: fall through and render
            # with render_status still HEALTHY -- the page then offers a manual
            # restart button. This is the correct no-op; nothing more to do.
            pass
    html_body = render_recovery_page(
        agent_id=aid,
        return_to=return_to,
        initial_status=render_status,
        initial_error=initial_error,
    )
    return HTMLResponse(content=html_body)


def _run_mngr_subprocess(
    concurrency_group: ConcurrencyGroup, argv: list[str], env: dict[str, str]
) -> tuple[FinishedProcess | None, str | None]:
    """Run an ``mngr`` subprocess to completion and classify the outcome.

    Returns ``(finished, failure_reason)``:
      - ``finished`` -- the completed process, or None when the subprocess
        could not be run at all (one of the caught exceptions fired).
      - ``failure_reason`` -- a human-readable description of why the run
        failed, or None on a clean exit (returncode 0, not timed out).
    Shared by ``_run_mngr_command`` (which forwards the reason) and
    ``_capture_mngr_command`` (which needs the stdout on success).
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=_RESTART_COMMAND_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=env,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ConcurrencyGroupError) as exc:
        # OSError covers fork/exec failures, RuntimeError the executor itself,
        # ConcurrencyGroupError the strand-level failures the group raises, and
        # TimeoutExpired any internal wait that surfaces it.
        logger.warning("mngr command {} failed: {}", argv, exc)
        return None, str(exc)
    if finished.is_timed_out:
        # With is_checked_after=False the timeout ceiling does not raise; it
        # comes back as a finished process flagged is_timed_out (with a
        # signal-based returncode), so it must be detected explicitly here --
        # otherwise it would be misreported as a plain non-zero exit below.
        logger.warning("mngr command {} timed out after {}s", argv, _RESTART_COMMAND_TIMEOUT_SECONDS)
        return finished, f"timed out after {int(_RESTART_COMMAND_TIMEOUT_SECONDS)}s"
    if finished.returncode != 0:
        logger.warning("mngr command {} exited {}: {}", argv, finished.returncode, finished.stderr)
        return finished, f"exited {finished.returncode}: {finished.stderr.strip()}"
    return finished, None


def _run_mngr_command(concurrency_group: ConcurrencyGroup, argv: list[str], env: dict[str, str]) -> str | None:
    """Run an ``mngr`` subprocess to completion; return an error message, or None on success."""
    _finished, failure_reason = _run_mngr_subprocess(concurrency_group, argv, env)
    return failure_reason


def _capture_mngr_command(concurrency_group: ConcurrencyGroup, argv: list[str], env: dict[str, str]) -> str | None:
    """Run a read-only ``mngr`` subprocess and return its stdout, or None if it failed.

    ``_run_mngr_command`` reports success/failure for restart steps and discards
    stdout; this is the counterpart for ``mngr`` queries whose stdout the caller
    needs to parse (currently the host-state probe).
    """
    finished, failure_reason = _run_mngr_subprocess(concurrency_group, argv, env)
    if failure_reason is not None or finished is None:
        return None
    return finished.stdout


def _summarize_mngr_list_payload_errors(list_json: str | None) -> str | None:
    """Return a one-line summary of the ``errors`` array in ``mngr list`` JSON, or None.

    With ``--on-error continue`` the subprocess can exit 0 (or non-zero, depending
    on whether ``result.errors`` is empty) but still report per-provider failures
    in the payload's ``errors`` field. Surfacing the first error's message lets
    the recovery page tell the user that their workspace's apparent
    unreachability is collateral damage of a different host's discovery failure
    rather than a problem with their own workspace.
    """
    if list_json is None:
        return None
    try:
        payload = json.loads(list_json)
    except json.JSONDecodeError as e:
        # mngr list emitting non-JSON on stdout is unexpected; surface it so a
        # malformed payload is visible instead of a silent None that the
        # diagnostic would otherwise render the same as "no errors".
        logger.warning("Could not parse `mngr list` stdout as JSON: {}", e)
        return None
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    exception_type = first.get("exception_type")
    provider_name = first.get("provider_name")
    parts: list[str] = []
    if isinstance(provider_name, str) and provider_name:
        parts.append(f"provider={provider_name}")
    if isinstance(exception_type, str) and exception_type:
        parts.append(exception_type)
    if isinstance(message, str) and message:
        parts.append(message)
    if not parts:
        return None
    summary = ": ".join(parts)
    if len(errors) > 1:
        summary = f"{summary} (+{len(errors) - 1} more)"
    return summary


def _await_system_interface_ready(agent_id: AgentId, mngr_forward_port: int, preauth_cookie: str) -> bool:
    """Poll the system interface through the plugin until it answers 200, or the wait budget elapses."""
    deadline = time.monotonic() + _SYSTEM_INTERFACE_STARTUP_WAIT_SECONDS
    with make_workspace_probe_client(
        preauth_cookie=preauth_cookie,
        probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
    ) as probe_client:
        while time.monotonic() < deadline:
            status = probe_workspace_through_plugin(
                mngr_forward_port=mngr_forward_port,
                preauth_cookie=preauth_cookie,
                agent_id=agent_id,
                probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                client=probe_client,
            )
            if status == 200:
                return True
            threading.Event().wait(timeout=_RESTART_PROBE_INTERVAL_SECONDS)
    return False


class _RestartWorkerFailureHandler(MutableModel):
    """Callable ``on_failure`` hook for the restart worker thread.

    The recovery page only leaves its "Restarting..." state on a HEALTHY or
    RESTART_FAILED transition, and the tracker is already RESTARTING when the
    worker starts. If the worker thread crashes unexpectedly, the
    ``ConcurrencyGroup`` invokes this so the tracker still reaches
    RESTART_FAILED instead of the page hanging. The crash itself is logged by
    the ``ObservableThread`` machinery, so this only records the recovery state.
    """

    tracker: SystemInterfaceHealthTracker = Field(frozen=True, description="Health tracker to transition.")
    workspace_agent_id: AgentId = Field(frozen=True, description="Workspace agent whose restart worker crashed.")

    def __call__(self, exc: BaseException) -> None:
        self.tracker.mark_restart_failed(self.workspace_agent_id, f"The restart worker failed unexpectedly: {exc}")


def _run_restart_sequence(
    workspace_agent_id: AgentId,
    is_host_restart: bool,
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str | None,
) -> None:
    """Background worker: stop + start the system-services agent, then await recovery.

    Drives the health tracker to HEALTHY on recovery or RESTART_FAILED (with
    a reason) when a step errors or the system interface does not return
    within the shared startup-wait budget. A crash of this worker is turned
    into RESTART_FAILED by ``_RestartWorkerFailureHandler``, wired as the
    thread's ``on_failure`` callback.
    """
    tier_label = "host restart" if is_host_restart else "system-interface restart"
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        tracker.mark_restart_failed(
            workspace_agent_id, "Could not locate the system-services agent for this workspace."
        )
        return

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)

    stop_error = _run_mngr_command(
        concurrency_group, _build_mngr_stop_argv(mngr_binary, services_agent_id, is_host_restart), env
    )
    if stop_error is not None:
        tracker.mark_restart_failed(workspace_agent_id, f"Stop step of {tier_label} failed: {stop_error}")
        return

    start_error = _run_mngr_command(concurrency_group, _build_mngr_start_argv(mngr_binary, services_agent_id), env)
    if start_error is not None:
        tracker.mark_restart_failed(workspace_agent_id, f"Start step of {tier_label} failed: {start_error}")
        return

    # Without a plugin route there is no way to probe for recovery, so treat a
    # clean dispatch as success (mirrors the background probe loop being a no-op).
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie:
        tracker.record_probe_success(workspace_agent_id)
        return

    if _await_system_interface_ready(workspace_agent_id, mngr_forward_port, mngr_forward_preauth_cookie):
        tracker.record_probe_success(workspace_agent_id)
    else:
        tracker.mark_restart_failed(
            workspace_agent_id,
            f"The system interface did not respond within "
            f"{int(_SYSTEM_INTERFACE_STARTUP_WAIT_SECONDS)}s of the {tier_label}.",
        )


def _dispatch_restart(
    request: Request,
    auth_store: AuthStoreDep,
    agent_id: str,
    is_host_restart: bool,
) -> Response:
    """Shared body for the two restart endpoints: validate, mark RESTARTING, spawn the worker."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    tracker: SystemInterfaceHealthTracker | None = request.app.state.system_interface_health_tracker
    concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    if tracker is None or concurrency_group is None:
        return _json_error("Workspace restart is unavailable in this configuration", status_code=503)
    # A restart is already in flight for this agent -- don't stack a second
    # worker thread racing the first one's stop/start commands. mark_restarting
    # decides the RESTARTING transition under its own lock and reports whether
    # this caller won it, so this check-and-claim is atomic against concurrent
    # restart requests (recovery page, sidebar, landing page).
    if not tracker.mark_restarting(aid):
        return Response(status_code=202, content="{}", media_type="application/json")

    # is_checked=False + on_failure: a crash of the one-shot worker is handled
    # by transitioning the tracker to RESTART_FAILED (so the recovery page does
    # not hang), rather than being surfaced later when the root group is checked.
    #
    # ``start_new_thread`` itself can raise (its concurrency-group decorators
    # fire ``ConcurrencyGroupError`` when the group is shutting down or has
    # already failed). If the spawn raises after we've already claimed
    # RESTARTING, the tracker would otherwise be stuck in that state forever
    # with no worker to advance it. Catch the spawn-time failures explicitly
    # and roll the tracker into RESTART_FAILED so the recovery page surfaces
    # the failure instead of polling indefinitely.
    try:
        concurrency_group.start_new_thread(
            target=_run_restart_sequence,
            kwargs={
                "workspace_agent_id": aid,
                "is_host_restart": is_host_restart,
                "tracker": tracker,
                "backend_resolver": backend_resolver,
                "mngr_binary": request.app.state.mngr_binary,
                "mngr_host_dir": request.app.state.mngr_host_dir,
                "concurrency_group": concurrency_group,
                "mngr_forward_port": request.app.state.mngr_forward_port or 0,
                "mngr_forward_preauth_cookie": request.app.state.mngr_forward_preauth_cookie,
            },
            name=f"system-interface-restart-{aid}",
            daemon=True,
            is_checked=False,
            on_failure=_RestartWorkerFailureHandler(tracker=tracker, workspace_agent_id=aid),
        )
    except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
        logger.warning("Failed to spawn restart worker for {}: {}", aid, exc)
        tracker.mark_restart_failed(aid, f"Could not start the restart worker: {exc}")
        return _json_error(f"Could not start the restart worker: {exc}", status_code=503)
    return Response(status_code=202, content="{}", media_type="application/json")


def _handle_restart_system_interface_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Dispatch a surgical restart of the system-services agent (``mngr stop`` + ``mngr start``)."""
    return _dispatch_restart(request=request, auth_store=auth_store, agent_id=agent_id, is_host_restart=False)


def _handle_restart_host_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Dispatch a full host restart (``mngr stop --stop-host`` + ``mngr start`` of system-services)."""
    return _dispatch_restart(request=request, auth_store=auth_store, agent_id=agent_id, is_host_restart=True)


def _handle_host_health_probe_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Layer-2 probe: classify the workspace host + run the recovery-diagnostics probe.

    Reads the host's lifecycle state from ``mngr list`` AND runs a batched
    in-container probe via ``mngr exec`` (recovery-diagnostics: tmux ls,
    services.toml parse, ss/curl on the inner port). Returns the full
    :class:`HostHealthResponse` -- the recovery page uses ``reachable`` /
    ``host_offline`` for auto-dispatch tiering, ``is_misconfigured`` /
    ``ssh_dead`` to choose between the misconfigured / unresponsive
    variants, and the rest of the payload for the diagnostics menu.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    if concurrency_group is None:
        return _json_error("Host health probe is unavailable in this configuration", status_code=503)
    response = _run_host_health_probe(aid, request, concurrency_group)
    logger.info(
        "Layer-2 host-state probe for {}: reachable={} host_offline={} ssh_dead={} is_misconfigured={}",
        aid,
        response.reachable,
        response.host_offline,
        response.ssh_dead,
        response.is_misconfigured,
    )
    return Response(
        content=response.model_dump_json(),
        media_type="application/json",
    )


def _run_host_health_probe(
    agent_id: AgentId,
    request: Request,
    concurrency_group: ConcurrencyGroup,
) -> HostHealthResponse:
    """Run the batched ``mngr exec`` probe + ``mngr list`` lookup, return the response.

    Composes the response from three independent inputs: ``mngr list`` for
    host state / services-agent state / SSH connection info, the batched
    in-container ``mngr exec`` probe, and the plugin's resolver-snapshot
    mirror. Caches the response on the ``_HostHealthCache`` held at
    ``app.state.host_health_cache`` so the on-recovery callback can log
    the most recent observation on the next non-HEALTHY -> HEALTHY
    transition.
    """
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(request.app.state.mngr_host_dir)
    mngr_binary: str = request.app.state.mngr_binary
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    services_agent_id = backend_resolver.get_system_services_agent_id(agent_id)
    list_argv = _build_mngr_host_state_argv(mngr_binary, agent_id, services_agent_id)
    list_command = shlex.join(list_argv)
    finished, mngr_list_error = _run_mngr_subprocess(concurrency_group, list_argv, env)
    if finished is not None:
        list_json: str | None = finished.stdout
        list_stdout = finished.stdout
        list_stderr = finished.stderr
        list_exit_code = finished.returncode
    else:
        # Subprocess could not be spawned at all -- mngr_list_error already
        # carries the exec failure as a str(exc). Leave the captured streams
        # empty so the diagnostics page shows the error alone.
        list_json = None
        list_stdout = ""
        list_stderr = ""
        list_exit_code = None
    # When the listing exited 0 but reported per-provider errors in its JSON
    # payload, surface those as ``mngr_list_error`` too -- the recovery page
    # needs the failing host name to explain that the user's workspace is
    # collateral damage of a sibling host failure, not a problem with their
    # own workspace.
    if mngr_list_error is None:
        mngr_list_error = _summarize_mngr_list_payload_errors(list_json)
    probe = _run_batched_probe(concurrency_group, mngr_binary, services_agent_id, env)
    consumer: EnvelopeStreamConsumer | None = request.app.state.envelope_stream_consumer
    plugin_resolver_services: dict[str, str] = (
        consumer.get_resolver_snapshot_for_agent(agent_id) if consumer is not None else {}
    )
    response = build_host_health_response(
        list_json=list_json,
        agent_id=agent_id,
        services_agent_id=services_agent_id,
        probe=probe,
        plugin_resolver_services=plugin_resolver_services,
        mngr_list_error=mngr_list_error,
        mngr_list_command=list_command,
        mngr_list_stdout=list_stdout,
        mngr_list_stderr=list_stderr,
        mngr_list_exit_code=list_exit_code,
    )
    cache: _HostHealthCache = request.app.state.host_health_cache
    cache.put(agent_id, response)
    return response


class _HostHealthCache(MutableModel):
    """Reference holder around the LRU-capped host-health response cache.

    Exists to give the host-health endpoint and the on-recovery callback a
    by-reference handle on the same OrderedDict. A Pydantic model with a
    ``dict`` / ``OrderedDict`` field validates and *copies* the input on
    construction, breaking the shared-reference contract; holding the
    OrderedDict inside a ``PrivateAttr`` makes Pydantic leave it alone,
    and passing this holder as a field to another Pydantic model passes
    the holder through by identity (Pydantic does not copy nested models).

    The cap and LRU semantics live here rather than at every call site so
    the endpoint code is just ``cache.put(agent_id, response)`` and the
    callback code is just ``cache.pop(agent_id)``.
    """

    max_entries: int = Field(
        default=_HOST_HEALTH_CACHE_MAX_ENTRIES,
        description="Cap on the number of cached entries; oldest evicted on overflow.",
    )

    _entries: "OrderedDict[str, HostHealthResponse]" = PrivateAttr(default_factory=OrderedDict)
    # Guards the compound put / pop operations. CPython's GIL serializes each
    # individual dict op, but ``put`` is three ops (setitem, move_to_end, an
    # eviction loop) and can interleave with concurrent calls from the FastAPI
    # threadpool (one per host-health probe) and the on-recovery callback
    # thread. Without this lock two concurrent puts could each see ``len > max``
    # and both evict, shrinking the cache below the cap.
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def put(self, agent_id: AgentId, response: HostHealthResponse) -> None:
        """Insert or refresh the cached response for ``agent_id`` (LRU on the back)."""
        aid_str = str(agent_id)
        with self._lock:
            self._entries[aid_str] = response
            self._entries.move_to_end(aid_str)
            while len(self._entries) > self.max_entries:
                # KeyError can only fire if the cache was emptied concurrently
                # (impossible under the lock, but kept as a defense in depth).
                try:
                    self._entries.popitem(last=False)
                except KeyError:
                    break

    def pop(self, agent_id: AgentId) -> HostHealthResponse | None:
        """Remove and return the cached response for ``agent_id``, or None if not cached."""
        with self._lock:
            return self._entries.pop(str(agent_id), None)


class _LogProbeOnRecoveryCallback(MutableModel):
    """Callable that logs the cached probe at INFO on every non-HEALTHY -> HEALTHY recovery.

    Registered with the health tracker so that when a workspace recovers
    (from STUCK, RESTARTING, or RESTART_FAILED back to HEALTHY), the most
    recent host-health probe response (cached by the host-health endpoint)
    lands in a single INFO log line. The line includes either the probe
    payload or a "(no probe observation cached)" marker so the operator
    can correlate the recovery with the most recent observation.

    Holds an :class:`_HostHealthCache` reference (a Pydantic model is passed
    through by identity, unlike a raw OrderedDict field which Pydantic
    would copy on construction), so the endpoint's writes are visible
    here. ``_HostHealthCache`` serializes its own ``put`` and ``pop``
    operations under an internal lock, so concurrent calls from the
    FastAPI threadpool and this callback thread interleave safely.
    """

    cache: _HostHealthCache = Field(
        frozen=True,
        description="Shared cache populated by the host-health endpoint.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __call__(self, agent_id: AgentId) -> None:
        response = self.cache.pop(agent_id)
        if response is None:
            logger.info("Workspace {} recovered (no probe observation cached)", agent_id)
            return
        logger.info("Workspace {} recovered; final probe: {}", agent_id, response.model_dump_json())


def _run_batched_probe(
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    services_agent_id: AgentId | None,
    env: dict[str, str],
) -> ProbeRecord:
    """Run the batched in-container probe via ``mngr exec``.

    Returns a probe record with ``ssh_dead=True`` when the
    system-services agent has not yet been discovered (so we can't even
    address the in-container script), when ``mngr exec`` could not be
    run, or when the sentinel never lands on stdout (SSH transport down
    or container hang). Recovery-page client steers SSH-dead to the
    host-restart tier.
    """
    if services_agent_id is None:
        return ProbeRecord(ssh_dead=True)
    argv = build_probe_argv(mngr_binary, services_agent_id)
    stdout = _capture_mngr_command(concurrency_group, argv, env)
    return parse_probe_output(stdout)


# -- Account management routes --


def _handle_accounts_page(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Render the manage accounts page."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    minds_config: MindsConfig | None = request.app.state.minds_config
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    enabled_by_user_id = {
        str(account.user_id): is_imbue_cloud_provider_enabled_for_account(str(account.email)) for account in accounts
    }
    html = render_accounts_page(
        accounts=accounts,
        default_account_id=default_account_id,
        enabled_by_user_id=enabled_by_user_id,
    )
    return HTMLResponse(content=html)


async def _handle_set_default_account(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Set the default account for new workspaces."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    form = await request.form()
    user_id = str(form.get("user_id", ""))
    minds_config: MindsConfig | None = request.app.state.minds_config
    if minds_config and user_id:
        minds_config.set_default_account_id(user_id)
    return Response(status_code=303, headers={"Location": "/accounts"})


async def _handle_account_logout(
    user_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Log out a specific account.

    Routes through the same plugin-side signout as ``_handle_signout_api``
    so the SuperTokens session is actually revoked, the
    ``[providers.imbue_cloud_<slug>]`` block is torn down, and the
    identity cache reflects the new state. Without this, just dropping
    the cache would let the next ``auth list`` call resurrect the
    account because the plugin still holds the session on disk.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    if request.app.state.session_store is not None:
        signout_user_via_plugin(request, user_id)
    return Response(status_code=303, headers={"Location": "/accounts"})


# -- Workspace settings routes --


def _handle_workspace_settings(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render workspace settings page with account, sharing, telegram, and delete options."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    current_account = session_store.get_account_for_workspace(agent_id) if session_store else None
    accounts = session_store.list_accounts() if session_store else []

    ws_name = backend_resolver.get_workspace_name(AgentId(agent_id))
    if not ws_name:
        info = backend_resolver.get_agent_display_info(AgentId(agent_id))
        ws_name = info.agent_name if info else agent_id

    servers = [str(s) for s in backend_resolver.list_services_for_agent(AgentId(agent_id))]

    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    telegram_state: str | None = None
    if telegram_orchestrator is not None:
        telegram_state = "active" if telegram_orchestrator.agent_has_telegram(AgentId(agent_id)) else "pending"

    html = render_workspace_settings(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
    )
    return HTMLResponse(content=html)


async def _handle_workspace_associate(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Associate a workspace with an account."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    form = await request.form()
    user_id = str(form.get("user_id", ""))
    redirect_url = str(form.get("redirect", ""))
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if session_store and user_id:
        session_store.associate_workspace(user_id, agent_id)
        # Wake the chrome SSE so the workspace tile picks up its new
        # 'account' field immediately rather than at the next 30s SSE
        # heartbeat. Without this, the user clicks Associate, the page
        # reloads via 303, but the chrome panel still shows the old
        # unassociated state for ~half a minute.
        backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
    location = redirect_url if redirect_url else f"/workspace/{agent_id}/settings"
    return Response(status_code=303, headers={"Location": location})


async def _handle_workspace_disassociate(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Disassociate a workspace from its account and tear down its tunnel."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    if session_store:
        account = session_store.get_account_for_workspace(agent_id)
        if account:
            # Tear down the Cloudflare tunnel for this agent (if any). The
            # plugin owns tunnel state -- minds keeps no local cache.
            if cli is not None:
                try:
                    tunnel = cli.find_tunnel_for_agent(account=str(account.email), agent_id=agent_id)
                    if tunnel is not None:
                        cli.delete_tunnel(account=str(account.email), tunnel_name=tunnel.tunnel_name)
                except ImbueCloudCliError as e:
                    logger.warning("Failed to delete tunnel during disassociation: {}", e)
            session_store.disassociate_workspace(str(account.user_id), agent_id)
            # Mirror the associate handler: poke the chrome SSE so the
            # tile flips back to unassociated immediately instead of
            # waiting out the 30s heartbeat.
            backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.notify_change()
    return Response(status_code=303, headers={"Location": f"/workspace/{agent_id}/settings"})


# -- Requests panel routes --


def _handle_requests_panel(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Render the right-side requests inbox panel."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return HTMLResponse(content="<p>Not authenticated</p>")
    inbox: RequestInbox | None = request.app.state.request_inbox
    pending = inbox.get_pending_requests() if inbox else []
    minds_config: MindsConfig | None = request.app.state.minds_config
    auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True

    cards = []
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    handlers: tuple[RequestEventHandler, ...] = request.app.state.request_event_handlers
    for req in pending:
        handler = find_handler_for_event(handlers, req)
        if handler is not None:
            kind_label = handler.kind_label()
            display_label = handler.display_name_for_event(req)
        else:
            # Fall through: unknown request type. Should never happen in
            # practice -- a request without a registered handler can't be
            # rendered or resolved -- but we still surface it in the
            # panel so the user sees something is wrong.
            kind_label = "request"
            display_label = ""
        parsed_id = AgentId(req.agent_id)
        ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
        if not ws_name:
            info = backend_resolver.get_agent_display_info(parsed_id)
            ws_name = info.agent_name if info else req.agent_id[:16]
        event_id = str(req.event_id)
        # Encode as JSON for safe embedding in the JS call, then HTML-escape
        # the result so it is also safe inside the double-quoted onclick
        # attribute. This is defense-in-depth: req.agent_id is validated as
        # an AgentId above, but req.event_id is only required to be a
        # non-empty string by its type, and relying on upstream validation
        # at each interpolation site is fragile.
        event_id_attr = html.escape(json.dumps(event_id), quote=True)
        agent_id_attr = html.escape(json.dumps(req.agent_id), quote=True)
        cards.append(
            f'<div class="req-card" onclick="navigateToRequest({event_id_attr}, {agent_id_attr})">'
            f'<div style="font-size:13px;color:#e2e8f0;font-weight:500;">{kind_label}: {ws_name}</div>'
            f'<div style="font-size:12px;color:#64748b;margin-top:2px;">{display_label}</div></div>'
        )

    html_content = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Requests</title>'
        "<style>body{font-family:-apple-system,sans-serif;background:#0f172a;color:#cbd5e1;"
        "margin:0;padding:0;overflow-y:auto;height:100vh;}"
        "h2{font-size:15px;color:#e2e8f0;padding:12px;margin:0;border-bottom:1px solid #334155;}"
        ".req-card{padding:10px 12px;margin:2px 0;cursor:pointer;border-radius:6px;transition:background 100ms;}"
        ".req-card:hover{background:rgba(255,255,255,0.06);}"
        "</style></head>"
        f"<body>"
        f"<script>"
        f"function navigateToRequest(eventId, agentId) {{"
        f"  if (window.minds && window.minds.navigateToRequest) {{"
        f"    window.minds.navigateToRequest(agentId, eventId);"
        f"  }} else if (window.minds) {{"
        f'    window.minds.navigateContent("/requests/" + eventId);'
        f"  }} else {{"
        f'    window.top.location = "/requests/" + eventId;'
        f"  }}"
        f"}}"
        f"</script>"
        f"<h2>Requests ({len(pending)})</h2>"
        f"<div>{''.join(cards) if cards else '<p style=padding:12px;color:#64748b;>No pending requests.</p>'}</div>"
        f'<div style="position:fixed;bottom:0;left:0;right:0;padding:12px;border-top:1px solid #334155;'
        f'background:#0f172a;">'
        f'<label style="font-size:12px;color:#94a3b8;cursor:pointer;">'
        f'<input type="checkbox" {"checked" if auto_open else ""} '
        f"onchange=\"fetch('/_chrome/requests-auto-open',{{method:'POST',headers:{{'Content-Type':"
        f"'application/json'}},body:JSON.stringify({{enabled:this.checked}})}})\"> "
        f"Auto-open on new request</label></div>"
        "</body></html>"
    )
    return HTMLResponse(content=html_content)


async def _handle_requests_auto_open(
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Toggle the auto-open setting for the requests panel."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    minds_config: MindsConfig | None = request.app.state.minds_config
    if minds_config:
        try:
            body = await request.json()
            enabled = body.get("enabled", True)
            minds_config.set_auto_open_requests_panel(bool(enabled))
        except (json.JSONDecodeError, ValueError):
            pass
    return Response(status_code=200, content='{"ok": true}', media_type="application/json")


def _resolve_ws_name_and_account(
    agent_id: str,
    request: Request,
    backend_resolver: BackendResolverInterface,
) -> tuple[str, str, bool, list[AccountSession]]:
    """Resolve workspace name, account email, has_account flag, and accounts list."""
    parsed_id = AgentId(agent_id)
    ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
    if not ws_name:
        info = backend_resolver.get_agent_display_info(parsed_id)
        ws_name = info.agent_name if info else agent_id
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    account = session_store.get_account_for_workspace(agent_id) if session_store else None
    account_email = account.email if account else ""
    has_account = account is not None
    accounts = session_store.list_accounts() if session_store else []
    return ws_name, account_email, has_account, accounts


def _handle_request_page(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render the request editing page.

    Dispatches by request type to the registered
    :class:`RequestEventHandler`. The route layer is intentionally
    agnostic about what each request kind looks like: it authenticates,
    looks up the event, and forwards to the handler.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is None:
        return HTMLResponse(content="<p>Request inbox not available</p>", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return HTMLResponse(content="<p>Request not found</p>", status_code=404)

    handlers: tuple[RequestEventHandler, ...] = request.app.state.request_event_handlers
    handler = find_handler_for_event(handlers, req_event)
    if handler is None:
        return HTMLResponse(
            content=f"<p>No handler registered for request type {req_event.request_type!r}</p>",
            status_code=500,
        )
    return handler.render_request_page(
        req_event=req_event,
        backend_resolver=backend_resolver,
        mngr_forward_origin=_get_mngr_forward_origin(request),
    )


def _handle_sharing_page(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Render the sharing editor page for direct editing (from workspace settings)."""
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    ws_name, account_email, has_account, accounts = _resolve_ws_name_and_account(
        agent_id,
        request,
        backend_resolver,
    )

    html = render_sharing_editor(
        agent_id=agent_id,
        service_name=service_name,
        title=f"Sharing: {service_name}",
        mngr_forward_origin=_get_mngr_forward_origin(request),
        has_account=has_account,
        accounts=accounts,
        redirect_url=f"/sharing/{agent_id}/{service_name}",
        ws_name=ws_name,
        account_email=account_email,
    )
    return HTMLResponse(content=html)


async def _handle_sharing_enable(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Enable or update sharing for a service via the workspace-settings editor.

    Sharing is configured exclusively from this editor; agents no longer
    write sharing-request events back into the inbox.

    On a soft failure (no signed-in account, plugin error, etc.) the
    handler returns 502 with a JSON ``{"error": "..."}`` body. The
    sharing editor JS surfaces that inline instead of silently
    redirecting to a now-empty status page.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    form = await request.form()
    emails = parse_emails_form_value(str(form.get("emails", "[]")))
    try:
        enable_sharing_via_cloudflare(
            request=request,
            agent_id=AgentId(agent_id),
            service_name=ServiceName(service_name),
            emails=emails,
            backend_resolver=backend_resolver,
        )
    except SharingError as exc:
        return Response(
            status_code=502,
            content=json.dumps({"error": str(exc)}),
            media_type="application/json",
        )
    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


async def _handle_sharing_disable(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Disable sharing for a service via the imbue_cloud plugin.

    Removes the service from its tunnel (DNS + Access app teardown
    happen connector-side). The tunnel itself stays around so re-
    enabling later doesn't re-issue a fresh token.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content="Not authenticated")

    cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if cli is None:
        return Response(
            status_code=502,
            content=json.dumps({"error": "imbue_cloud CLI is not configured."}),
            media_type="application/json",
        )
    parsed_id = AgentId(agent_id)
    try:
        account_email = resolve_account_email_for_workspace(session_store, parsed_id)
    except SharingError as exc:
        return Response(
            status_code=502,
            content=json.dumps({"error": str(exc)}),
            media_type="application/json",
        )

    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(parsed_id))
    except ImbueCloudCliError as exc:
        return Response(
            status_code=502,
            content=json.dumps({"error": f"Failed to look up the tunnel: {exc}"}),
            media_type="application/json",
        )
    if tunnel is None:
        # No tunnel = nothing to disable. Treat as success so the JS
        # redirect lands on the (already-disabled) status page.
        return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})

    try:
        cli.remove_service(account=account_email, tunnel_name=tunnel.tunnel_name, service_name=service_name)
    except ImbueCloudCliError as exc:
        return Response(
            status_code=502,
            content=json.dumps({"error": f"Failed to disable sharing: {exc}"}),
            media_type="application/json",
        )
    return Response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


def _handle_sharing_status_api(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """JSON API to get current sharing status for the editor JS.

    Reads tunnel + service + per-service auth from the imbue_cloud
    plugin (the connector is the source of truth -- minds keeps no
    local copy). The JS contract is::

        {"enabled": bool, "url": str | null, "policy": {"emails": [str, ...], ...}}

    ``policy`` is the AuthPolicy shape the plugin emits. Default policy
    when sharing isn't yet enabled is the workspace's associated account
    email.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")

    cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
    if cli is None:
        return Response(
            content=json.dumps({"enabled": False, "url": None, "policy": {"emails": []}}),
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)
    try:
        account_email = resolve_account_email_for_workspace(session_store, parsed_id)
    except SharingError as exc:
        # No associated account = no plugin call available; surface
        # an empty default rather than 502 since the page itself
        # already shows the "associate an account" affordance for
        # this state.
        logger.debug("Sharing status: {}", exc)
        return Response(
            content=json.dumps({"enabled": False, "url": None, "policy": {"emails": []}}),
            media_type="application/json",
        )

    default_policy = {"emails": [account_email]}
    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(parsed_id))
    except ImbueCloudCliError as exc:
        logger.warning("Failed to list tunnels for {}: {}", parsed_id, exc)
        return Response(
            content=json.dumps({"enabled": False, "url": None, "policy": default_policy}),
            media_type="application/json",
        )
    if tunnel is None or service_name not in tunnel.services:
        return Response(
            content=json.dumps({"enabled": False, "url": None, "policy": default_policy}),
            media_type="application/json",
        )

    try:
        service_entries = cli.list_services(account_email, tunnel.tunnel_name)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to list services for tunnel {}: {}", tunnel.tunnel_name, exc)
        service_entries = []
    hostname = next(
        (entry.get("hostname") for entry in service_entries if entry.get("service_name") == service_name),
        None,
    )

    try:
        policy = cli.get_service_auth(account_email, tunnel.tunnel_name, service_name)
    except ImbueCloudCliError:
        try:
            policy = cli.get_tunnel_auth(account_email, tunnel.tunnel_name)
        except ImbueCloudCliError:
            policy = default_policy
    if not policy.get("emails") and not policy.get("email_domains"):
        # Empty policy means "use tunnel default"; surface the owner's
        # email so the editor doesn't render an empty ACL.
        policy = default_policy

    return Response(
        content=json.dumps(
            {
                "enabled": True,
                "url": f"https://{hostname}" if hostname else None,
                "policy": policy,
            }
        ),
        media_type="application/json",
    )


async def _handle_request_grant(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Dispatch a grant to the handler that claims the event's request type.

    The route layer is intentionally agnostic: it authenticates, looks
    up the request event, finds the registered
    :class:`RequestEventHandler` whose ``handles_request_type`` matches,
    and forwards the rest. Per-handler differences (form parsing,
    response shape, side effects) live in the handler.
    """
    return await _dispatch_request_action(
        request_id=request_id,
        request=request,
        auth_store=auth_store,
        action="grant",
    )


async def _handle_request_deny(
    request_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Dispatch a deny to the handler that claims the event's request type."""
    return await _dispatch_request_action(
        request_id=request_id,
        request=request,
        auth_store=auth_store,
        action="deny",
    )


async def _dispatch_request_action(
    request_id: str,
    request: Request,
    auth_store: AuthStoreInterface,
    action: str,
) -> Response:
    """Shared body of grant/deny dispatchers.

    Authenticates, looks up the request event, picks the right handler,
    and forwards. ``action`` must be ``"grant"`` or ``"deny"``.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return _json_error("Not authenticated", status_code=403)
    inbox: RequestInbox | None = request.app.state.request_inbox
    if inbox is None:
        return _json_error("Request inbox not available", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return _json_error("Request not found", status_code=404)

    handlers: tuple[RequestEventHandler, ...] = request.app.state.request_event_handlers
    handler = find_handler_for_event(handlers, req_event)
    if handler is None:
        return _json_error(
            f"No handler registered for request type '{req_event.request_type}'",
            status_code=400,
        )
    if action == "grant":
        return await handler.apply_grant_request(request, req_event)
    if action == "deny":
        return await handler.apply_deny_request(request, req_event)
    return _json_error(f"Unsupported action '{action}'", status_code=500)


_request_event_apps: dict[int, FastAPI] = {}
_refresh_event_apps: dict[int, FastAPI] = {}


def _handle_request_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Process an incoming request event and add it to the app's inbox.

    After mutating the inbox, fires the resolver's change notification so
    the chrome SSE wakes up and pushes the new ``request_count`` immediately
    (otherwise it would lag up to 30s for the next poll tick, breaking the
    requests panel auto-open and badge UX).

    ``LATCHKEY_PERMISSION`` events from the JSONL stream are ignored
    here: latchkey 2.9.0 ships a gateway extension that owns the
    pending-permission queue, and the desktop client consumes it via
    :class:`PermissionRequestsConsumer` instead. Any latchkey events
    that still arrive over the legacy JSONL channel are stale (the
    agents migrating to the extension write directly to the gateway
    now) and would only double-count.
    """
    event = parse_request_event(raw_line)
    if event is None:
        return
    if event.request_type == str(RequestType.LATCHKEY_PERMISSION):
        logger.debug(
            "Ignoring legacy JSONL latchkey-permission event from agent {}; the gateway extension owns this flow now",
            agent_id_str,
        )
        return
    for app in _request_event_apps.values():
        current_inbox: RequestInbox | None = app.state.request_inbox
        if current_inbox is not None:
            app.state.request_inbox = current_inbox.add_request(event)
            logger.info("Request event from agent {}: {}", agent_id_str, event.request_type)
            backend_resolver: BackendResolverInterface = app.state.backend_resolver
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.notify_change()


def _parse_refresh_service_name(raw_line: str) -> str | None:
    """Extract service_name from a refresh event line, or None if unparseable."""
    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    service_name = data.get("service_name")
    if not isinstance(service_name, str) or not service_name:
        return None
    return service_name


async def _dispatch_refresh_broadcast(app: FastAPI, agent_id: AgentId, service_name: str) -> None:
    """POST to the agent's system interface so it emits a refresh_service WS broadcast.

    Routed through the ``mngr forward`` plugin's per-agent subdomain so we
    reuse the plugin's existing SSH tunnel to the agent rather than
    maintaining one in minds. The request connects to the plugin on loopback
    and carries the agent's ``agent-<hex>.localhost`` vhost in the ``Host``
    header (the plugin routes on that header), so it does not depend on
    ``*.localhost`` name resolution. Auth on the plugin uses the same
    ``preauth_cookie`` value the plugin trusts for the Electron-shell
    pre-set; minds knows that value because it minted it in ``cli/run.py``.
    Errors are logged but swallowed -- a missed refresh is never worth
    crashing on.
    """
    plugin_port: int = app.state.mngr_forward_port or 8421
    preauth_cookie: str | None = app.state.mngr_forward_preauth_cookie
    if preauth_cookie is None:
        logger.debug("Refresh broadcast skipped for {}/{}: no preauth cookie wired", agent_id, service_name)
        return
    url = f"http://127.0.0.1:{plugin_port}/api/refresh-service/{service_name}/broadcast"
    host_header = f"{agent_id}.localhost"
    http_client: httpx.AsyncClient = app.state.http_client
    try:
        response = await http_client.post(
            url,
            headers={"Host": host_header},
            cookies={"mngr_forward_session": preauth_cookie},
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Refresh broadcast POST to {} ({}) failed: {}", url, host_header, e)


def _log_refresh_dispatch_result(
    future: concurrent.futures.Future[None], agent_id_str: str, service_name: str
) -> None:
    """Surface any exception stashed on a scheduled refresh-dispatch future.

    ``run_coroutine_threadsafe`` stores exceptions on the returned
    ``concurrent.futures.Future``; if nothing calls ``.exception()`` they are
    never logged. This callback runs when the coroutine finishes and logs
    anything other than cancellation.
    """
    try:
        exc = future.exception()
    except asyncio.CancelledError:
        logger.debug("Refresh dispatch cancelled for agent {} service {}", agent_id_str, service_name)
        return
    if exc is not None:
        logger.warning("Refresh dispatch failed for agent {} service {}: {}", agent_id_str, service_name, exc)


def _handle_refresh_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Fan a refresh event out to every registered app's system interface.

    Runs on the mngr-events reader thread, so the async POST is scheduled
    on each app's captured event loop via run_coroutine_threadsafe.
    """
    service_name = _parse_refresh_service_name(raw_line)
    if service_name is None:
        logger.debug("Ignoring malformed refresh event from {}: {}", agent_id_str, raw_line[:200])
        return
    agent_id = AgentId(agent_id_str)
    for app in _refresh_event_apps.values():
        # event_loop is set to None in create_desktop_client and populated by
        # _managed_lifespan on startup. In production, stream_manager.start()
        # (which feeds this callback) runs before uvicorn.run(app) starts the
        # lifespan, so there is a brief window during which refresh events
        # can arrive before the loop is captured. Drop such events rather
        # than crashing the reader thread with AttributeError. The same guard
        # also covers loops that have already been closed (e.g. the app was
        # torn down but its entry in _refresh_event_apps has not yet been
        # removed) -- scheduling on a closed loop would raise RuntimeError
        # and leak an unawaited coroutine.
        loop: asyncio.AbstractEventLoop | None = app.state.event_loop
        if loop is None or loop.is_closed():
            logger.debug(
                "Dropping refresh for agent {} service {}: app event loop unavailable",
                agent_id_str,
                service_name,
            )
            continue
        future = asyncio.run_coroutine_threadsafe(_dispatch_refresh_broadcast(app, agent_id, service_name), loop)
        future.add_done_callback(lambda f, aid=agent_id_str, sn=service_name: _log_refresh_dispatch_result(f, aid, sn))
        logger.info("Scheduled refresh broadcast for agent {} service {}", agent_id_str, service_name)


# -- App factory --


def create_desktop_client(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.AsyncClient | None,
    agent_creator: AgentCreator | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    telegram_orchestrator: TelegramSetupOrchestrator | None = None,
    notification_dispatcher: NotificationDispatcher | None = None,
    paths: WorkspacePaths | None = None,
    minds_config: MindsConfig | None = None,
    client_env_config: ClientEnvConfig | None = None,
    envelope_stream_consumer: EnvelopeStreamConsumer | None = None,
    session_store: MultiAccountSessionStore | None = None,
    request_inbox: RequestInbox | None = None,
    request_event_handlers: tuple[RequestEventHandler, ...] = (),
    server_port: int = 0,
    mngr_forward_port: int = 0,
    mngr_forward_preauth_cookie: str | None = None,
    output_format: OutputFormat | None = None,
    root_concurrency_group: ConcurrencyGroup | None = None,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = None,
    mngr_binary: str = "mngr",
    mngr_host_dir: Path | None = None,
    minds_api_key: str | None = None,
) -> FastAPI:
    """Create the bare-origin minds FastAPI application.

    The agent-subdomain forwarding lives in the ``mngr_forward`` plugin
    (``libs/mngr_forward``) now; this app only serves minds-specific routes
    on the bare origin (login, landing, accounts, workspace settings,
    sharing, telegram, agent create / destroy). Workspace links go to
    ``http://localhost:<mngr_forward_port>/goto/<agent>/`` instead of being
    routed in-process.

    ``envelope_stream_consumer`` feeds discovery events into
    ``backend_resolver`` and is also the bounce target for ``SIGHUP``-style
    re-discovery after a SuperTokens signin writes a new provider entry.

    When ``agent_creator`` is provided, the server can create new agents
    from git URLs via the /create form and /api/create-agent API.

    When ``telegram_orchestrator`` is provided, the landing page shows
    Telegram setup buttons and the /api/agents/{agent_id}/telegram/*
    endpoints are available.

    When ``paths`` is provided, the /api/v1/ REST API router is mounted with
    API key authentication. The notification endpoint within the router
    additionally requires ``notification_dispatcher`` to be provided;
    without it that endpoint returns 501.
    """
    is_externally_managed_client = http_client is not None

    @asynccontextmanager
    async def _lifespan(inner_app: FastAPI) -> AsyncGenerator[None, None]:
        async with _managed_lifespan(inner_app=inner_app, is_externally_managed_client=is_externally_managed_client):
            yield

    app = FastAPI(lifespan=_lifespan)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        logger.opt(exception=exc).error("Unhandled exception on {} {}", request.method, request.url.path)
        return Response(status_code=500, content=f"Internal Server Error: {exc}")

    app.state.auth_store = auth_store
    app.state.backend_resolver = backend_resolver
    app.state.envelope_stream_consumer = envelope_stream_consumer
    # Placeholder so the lifespan teardown can read this slot
    # unconditionally; ``cli/run.py`` overwrites it with the running
    # consumer right after starting it.
    app.state.permission_requests_consumer = None
    # Cross-thread flag the SSE handlers poll to exit cleanly on
    # process shutdown. ``threading.Event`` (not ``asyncio.Event``) so
    # tests that exercise the endpoints without invoking the lifespan
    # context manager still see a valid, settable object on app.state
    # -- and because the lifespan teardown setter runs in the asyncio
    # event loop's thread but the SSE handlers read it from the same
    # thread, so awaitability buys us nothing here.
    app.state.shutdown_event = threading.Event()
    app.state.agent_creator = agent_creator
    app.state.imbue_cloud_cli = imbue_cloud_cli
    app.state.telegram_orchestrator = telegram_orchestrator
    app.state.notification_dispatcher = notification_dispatcher
    app.state.session_store = session_store
    app.state.minds_config = minds_config
    app.state.client_env_config = client_env_config
    app.state.request_inbox = request_inbox
    app.state.request_event_handlers = request_event_handlers
    app.state.auth_server_port = server_port
    app.state.mngr_forward_port = mngr_forward_port
    app.state.mngr_forward_preauth_cookie = mngr_forward_preauth_cookie
    app.state.auth_output_format = output_format or OutputFormat.JSONL
    app.state.root_concurrency_group = root_concurrency_group
    app.state.system_interface_health_tracker = system_interface_health_tracker
    app.state.mngr_binary = mngr_binary
    app.state.mngr_host_dir = mngr_host_dir if mngr_host_dir is not None else Path.home() / ".mngr"
    # Per-agent cache of the most recent host-health probe response. The
    # host-health endpoint writes here on every probe; the recovery-log
    # callback below reads on every non-HEALTHY -> HEALTHY transition
    # (STUCK, RESTARTING, or RESTART_FAILED -> HEALTHY) so the final
    # observed state of a recovered workspace gets a single INFO log line.
    # Wrapped in an _HostHealthCache holder so the same instance is shared
    # by the endpoint and the callback (a raw OrderedDict field on a
    # Pydantic model is validate-copied on construction). The holder
    # enforces the LRU cap so a workspace that the user gives up on
    # (stays in RESTART_FAILED and never fires the recovery callback)
    # cannot grow the cache without bound.
    app.state.host_health_cache = _HostHealthCache()
    if system_interface_health_tracker is not None:
        system_interface_health_tracker.add_on_recovery_callback(
            _LogProbeOnRecoveryCallback(cache=app.state.host_health_cache)
        )
    # Populated with the running loop by _managed_lifespan on startup. Defined
    # up-front as None so background callbacks fired before startup (e.g. mngr
    # events produced between consumer.start() and uvicorn.run()) see a
    # valid attribute and can choose to drop the event instead of crashing.
    app.state.event_loop = None
    # Always-set (possibly None) so consumers can read directly via
    # ``app.state.api_v1_paths`` instead of using a defaulting attribute
    # lookup -- the latter is flagged by the project ratchet.
    app.state.api_v1_paths = paths
    # Central minds API key. Required for ``/api/v1/...`` and the WebDAV
    # mount; tests that don't exercise those routes can leave it as
    # ``None`` (the bearer-auth gates fail closed when the key is None).
    app.state.minds_api_key = minds_api_key
    if http_client is not None:
        app.state.http_client = http_client

    # Register callback to process incoming request events from agents
    if isinstance(backend_resolver, MngrCliBackendResolver):
        _request_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_request_callback(_handle_request_event_callback)
        _refresh_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_refresh_callback(_handle_refresh_event_callback)

    # Mount the auth routes (proxy to the mngr_imbue_cloud plugin's auth subcommands)
    if session_store is not None and imbue_cloud_cli is not None:
        supertokens_router = create_supertokens_router(
            session_store=session_store,
            imbue_cloud_cli=imbue_cloud_cli,
            server_port=server_port,
            output_format=output_format or OutputFormat.JSONL,
        )
        app.include_router(supertokens_router)

    # Mount the REST API v1 router
    if paths is not None:
        api_v1_router = create_api_v1_router()
        app.include_router(api_v1_router, prefix="/api/v1")
        # Mount the WebDAV file server under /api/v1/files. Each share
        # root maps URL-path == on-disk-path (``~`` and ``/tmp``); the
        # mount itself is gated by the same central-key Bearer check
        # that protects the rest of /api/v1, via a closure that reads
        # ``app.state.minds_api_key`` on every request so the gate
        # stays in sync if a future code path ever rotates the key.
        app.mount("/api/v1/files", create_webdav_app(lambda: app.state.minds_api_key))

    # Static assets: Tailwind Play CDN JS + hand-written tokens.css +
    # per-page JS. The Tailwind JS is fetched once by `just minds-tailwind`
    # (plain curl, no build step) and is gitignored; if it's missing, the
    # mount still works and the server logs a hint at startup.
    _static_dir = Path(__file__).resolve().parent / "static"
    if not (_static_dir / "tailwind.js").exists():
        logger.warning("Missing static/tailwind.js. Run `just minds-tailwind` from the repo root to fetch it.")
    app.mount("/_static", StaticFiles(directory=str(_static_dir)), name="static")

    # Chrome (persistent shell) routes
    app.get("/_chrome")(_handle_chrome_page)
    app.get("/_chrome/sidebar")(_handle_chrome_sidebar)
    app.get("/_chrome/events")(_handle_chrome_events)

    # Register routes
    app.get("/welcome")(_handle_welcome_page)
    app.get("/login")(_handle_login)
    app.get("/authenticate")(_handle_authenticate)
    app.get("/")(_handle_landing_page)

    # Account management routes
    app.get("/accounts")(_handle_accounts_page)
    app.post("/accounts/set-default")(_handle_set_default_account)
    app.post("/accounts/{user_id}/logout")(_handle_account_logout)

    # Workspace settings routes
    app.get("/workspace/{agent_id}/settings")(_handle_workspace_settings)
    app.post("/workspace/{agent_id}/associate")(_handle_workspace_associate)
    app.post("/workspace/{agent_id}/disassociate")(_handle_workspace_disassociate)

    # Request inbox routes
    app.get("/_chrome/requests-panel")(_handle_requests_panel)
    app.post("/_chrome/requests-auto-open")(_handle_requests_auto_open)
    app.get("/requests/{request_id}")(_handle_request_page)
    app.post("/requests/{request_id}/grant")(_handle_request_grant)
    app.post("/requests/{request_id}/deny")(_handle_request_deny)

    # Sharing editor routes (used by both request approval and direct editing)
    app.get("/sharing/{agent_id}/{service_name}")(_handle_sharing_page)
    app.post("/sharing/{agent_id}/{service_name}/enable")(_handle_sharing_enable)
    app.post("/sharing/{agent_id}/{service_name}/disable")(_handle_sharing_disable)
    app.get("/api/sharing-status/{agent_id}/{service_name}")(_handle_sharing_status_api)

    # Agent creation routes
    app.get("/create")(_handle_create_page)
    app.post("/create")(_handle_create_form_submit)
    app.post("/api/create-agent")(_handle_create_agent_api)
    app.get("/api/create-agent/{agent_id}/status")(_handle_creation_status_api)
    app.get("/api/create-agent/{agent_id}/logs")(_handle_creation_logs_sse)
    app.get("/creating/{agent_id}")(_handle_creating_page)

    # Agent destruction routes
    app.post("/api/destroy-agent/{agent_id}")(_handle_destroy_agent_api)
    app.get("/api/destroying/{agent_id}/status")(_handle_destroying_status_api)
    app.get("/api/destroying/{agent_id}/log")(_handle_destroying_log_api)
    app.post("/api/destroying/{agent_id}/dismiss")(_handle_destroying_dismiss_api)
    app.get("/destroying/{agent_id}")(_handle_destroying_page)

    # Telegram setup routes
    app.post("/api/agents/{agent_id}/telegram/setup")(_handle_telegram_setup)
    app.get("/api/agents/{agent_id}/telegram/status")(_handle_telegram_status)

    # Providers panel toggle (Disable / Enable buttons in the landing page panel)
    app.post("/api/providers/{provider_name}/toggle")(_handle_provider_toggle)

    # System-interface recovery routes
    app.get("/agents/{agent_id}/recovery")(_handle_recovery_page)
    app.get("/api/agents/{agent_id}/host-health")(_handle_host_health_probe_api)
    app.post("/api/agents/{agent_id}/restart-system-interface")(_handle_restart_system_interface_api)
    app.post("/api/agents/{agent_id}/restart-host")(_handle_restart_host_api)

    return app


# How often the background probe loop polls each suspect / non-HEALTHY agent.
# This is also the resolution of the HEALTHY -> STUCK decision: a workspace is
# marked STUCK once its probe-failure run reaches ``stuck_threshold_seconds``,
# so STUCK fires at most one interval after the threshold elapses.
_HEALTH_PROBE_INTERVAL_SECONDS: Final[float] = 2.0


def start_system_interface_health_probe_loop(
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str | None,
    root_concurrency_group: ConcurrencyGroup | None,
) -> None:
    """Start a background thread that probes suspect / non-HEALTHY agents.

    For each agent the tracker reports as a probe target (suspect agents
    enrolled by a failure envelope, plus STUCK / RESTARTING / RESTART_FAILED
    agents), the thread polls the plugin's per-agent subdomain every
    ``_HEALTH_PROBE_INTERVAL_SECONDS``. A 200 response flips the tracker back
    to HEALTHY; any other result is reported as a probe failure, and a run of
    probe failures lasting ``stuck_threshold_seconds`` transitions a suspect
    agent to STUCK. Either way the on-change callback feeding the SSE stream
    fires. The thread silently no-ops when there are no probe targets.

    This loop is the single authority on STUCK: a ``system_interface_backend_failure``
    envelope only enrolls an agent as suspect, and STUCK is reached solely
    through probe failures observed here.

    Probing is skipped entirely when the plugin port or preauth cookie are
    unset (e.g. minds running without the plugin) -- without a working
    plugin route there is no way to ask whether the workspace is reachable.
    """
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie or root_concurrency_group is None:
        return

    root_concurrency_group.start_new_thread(
        target=_run_system_interface_health_probe_loop,
        args=(tracker, backend_resolver, mngr_forward_port, mngr_forward_preauth_cookie, root_concurrency_group),
        name="system-interface-health-probe",
        daemon=True,
    )


def _run_system_interface_health_probe_loop(
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Loop body for the background system-interface health probe thread."""
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        # Static resolvers used by tests don't expose the same subdomain
        # routing, so probing them by ID is meaningless. Resolver type is
        # fixed for the process lifetime, so exit the thread immediately
        # rather than spinning forever doing nothing.
        logger.debug(
            "System-interface health probe thread exiting: backend_resolver is {}, not MngrCliBackendResolver",
            type(backend_resolver).__name__,
        )
        return
    with make_workspace_probe_client(
        preauth_cookie=mngr_forward_preauth_cookie,
        probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
    ) as probe_client:
        while not root_concurrency_group.is_shutting_down():
            for aid in tracker.snapshot_probe_targets():
                probe_status = probe_workspace_through_plugin(
                    mngr_forward_port=mngr_forward_port,
                    preauth_cookie=mngr_forward_preauth_cookie,
                    agent_id=aid,
                    probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                    client=probe_client,
                )
                if probe_status == 200:
                    tracker.record_probe_success(aid)
                else:
                    tracker.record_probe_failure(aid)
            threading.Event().wait(timeout=_HEALTH_PROBE_INTERVAL_SECONDS)
