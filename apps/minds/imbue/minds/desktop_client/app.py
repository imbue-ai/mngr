import asyncio
import html
import json
import os
import queue
import shlex
import threading
import time
from collections.abc import AsyncGenerator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from urllib.parse import urlparse

import httpx
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
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
from imbue.minds.desktop_client.backup_export import export_latest_snapshot_zip
from imbue.minds.desktop_client.backup_password_store import has_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password_if_absent
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.backup_status import compute_backup_status_for_workspaces
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
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.onboarding import OnboardingAnswers
from imbue.minds.desktop_client.onboarding import OnboardingApplier
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_sharing_via_cloudflare
from imbue.minds.desktop_client.sharing_handler import is_probeable_share_url
from imbue.minds.desktop_client.sharing_handler import is_share_ready_from_edge_response
from imbue.minds.desktop_client.sharing_handler import parse_emails_form_value
from imbue.minds.desktop_client.sharing_handler import resolve_account_email_for_workspace
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
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
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_request_unavailable_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_welcome_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates import status_text_for
from imbue.minds.desktop_client.templates import workspace_accent
from imbue.minds.desktop_client.tunnel_token_injection import clear_tunnel_token_from_agent
from imbue.minds.desktop_client.tunnel_token_injection import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.webdav import create_webdav_app
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import MngrCommandError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServiceName
from imbue.minds.primitives import UserDataPreference
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

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
    """Manage the httpx client lifecycle and root concurrency group teardown.

    SSH tunnels (forward + reverse) live in ``cli/run.py``'s
    ``SSHTunnelManager``, which is solely used by the surviving Latchkey
    discovery callback and is cleaned up by ``cli/run.py``.
    """
    if not is_externally_managed_client:
        inner_app.state.http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=_PROXY_TIMEOUT_SECONDS,
        )
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
    agent_creator: AgentCreator | None = request.app.state.agent_creator
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    is_backup_password_saved = has_saved_backup_password(agent_creator.paths) if agent_creator is not None else False
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
        has_saved_backup_password=is_backup_password_saved,
    )
    return HTMLResponse(content=html)


def _handle_backup_status_api(
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Return per-project backup status (GET /api/backup-status).

    Queries restic (from the minds machine) for every known workspace using
    its canonical restic.env, in parallel with a per-workspace timeout. The
    landing page fetches this once on load to fill each tile.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(content="{}", media_type="application/json")
    root_concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    agent_ids = backend_resolver.list_known_workspace_ids()
    status_by_agent_id = compute_backup_status_for_workspaces(paths, agent_ids, parent_cg=root_concurrency_group)
    # Workspace creation time lets the landing page show "Created N ago" instead
    # of a scary "No backups" for a freshly-created, not-yet-backed-up workspace.
    create_time_by_agent_id: dict[str, datetime] = {}
    for agent_id in agent_ids:
        display_info = backend_resolver.get_agent_display_info(agent_id)
        if display_info is not None and display_info.create_time is not None:
            create_time_by_agent_id[str(agent_id)] = display_info.create_time
    payload = {
        agent_id: {
            "state": str(status.state),
            "last_success_at": status.last_success_at.isoformat() if status.last_success_at is not None else None,
            "created_at": (
                create_time_by_agent_id[agent_id].isoformat() if agent_id in create_time_by_agent_id else None
            ),
        }
        for agent_id, status in status_by_agent_id.items()
    }
    return Response(content=json.dumps(payload), media_type="application/json")


def _handle_backup_export_api(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Build + stream a zip of the workspace's latest snapshot (GET /api/backup-export/{agent_id}).

    Produces the zip on the minds machine via ``restic dump --archive zip`` to a
    /tmp file keyed by host id (so re-exports overwrite), then returns it.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = request.app.state.api_v1_paths
    if paths is None:
        return Response(status_code=404, content='{"error": "No backups configured"}', media_type="application/json")
    try:
        typed_agent_id = AgentId(agent_id)
    except ValueError:
        return Response(status_code=400, content='{"error": "Invalid agent id"}', media_type="application/json")
    display_info = backend_resolver.get_agent_display_info(typed_agent_id)
    # The zip file is keyed by host id (per the export contract); fall back to the
    # agent id only if discovery has no display info for this agent.
    host_id = display_info.host_id if display_info is not None else agent_id
    download_label = display_info.agent_name if display_info is not None else agent_id
    root_concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    try:
        zip_path = export_latest_snapshot_zip(
            paths=paths, agent_id=typed_agent_id, host_id=host_id, parent_cg=root_concurrency_group
        )
    except BackupProvisioningError as e:
        logger.warning("Backup export failed for {}: {}", agent_id, e)
        return Response(status_code=500, content=json.dumps({"error": str(e)}), media_type="application/json")
    return FileResponse(path=str(zip_path), media_type="application/zip", filename=f"{download_label}-backup.zip")


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


def _build_backup_request_or_error(
    *,
    backup_provider: BackupProvider,
    encryption_method: BackupEncryptionMethod,
    typed_master_password: str,
    is_save_password: bool,
    api_key_env: str,
    account_email: str,
    paths: WorkspacePaths,
) -> tuple[BackupSetupRequest | None, str | None]:
    """Resolve form backup inputs into a ``BackupSetupRequest`` or an error message.

    Reads / first-time-saves the shared master password as a side effect.
    Returns ``(request, None)`` on success or ``(None, message)`` for a
    validation error the caller should re-render on the form.
    """
    if backup_provider is BackupProvider.CONFIGURE_LATER:
        return BackupSetupRequest(backup_provider=BackupProvider.CONFIGURE_LATER), None
    if backup_provider is BackupProvider.IMBUE_CLOUD and not account_email:
        return None, (
            "imbue_cloud backups require a selected account. Choose an account or pick a different backup provider."
        )
    # The user never sets the repository password: minds initializes the repo
    # and assigns each workspace its own random RESTIC_PASSWORD, so reject it
    # if a user puts one in the api_key env block.
    if backup_provider is BackupProvider.API_KEY and env_text_defines_restic_password(api_key_env):
        return None, (
            "Don't set RESTIC_PASSWORD in the backup env -- minds assigns each workspace its own random "
            "repository password. Provide RESTIC_REPOSITORY and any backend credentials only."
        )
    # The master password (or empty, for no_password) is used only to
    # initialize the repo from the minds machine; it never enters the workspace.
    master_password: SecretStr | None = None
    if encryption_method is BackupEncryptionMethod.MASTER_PASSWORD:
        saved_password = read_saved_backup_password(paths)
        if saved_password is not None:
            master_password = SecretStr(saved_password)
        elif typed_master_password:
            master_password = SecretStr(typed_master_password)
            if is_save_password:
                save_backup_password_if_absent(paths, typed_master_password)
        else:
            return None, "Enter a backup master password, or set the encryption method to 'no password'."
    return (
        BackupSetupRequest(
            backup_provider=backup_provider,
            master_password=master_password,
            api_key_env_text=api_key_env if backup_provider is BackupProvider.API_KEY else "",
            account_email=account_email,
        ),
        None,
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
    try:
        backup_provider = BackupProvider(str(form.get("backup_provider", BackupProvider.CONFIGURE_LATER.value)))
    except ValueError:
        backup_provider = BackupProvider.CONFIGURE_LATER
    try:
        backup_encryption_method = BackupEncryptionMethod(
            str(form.get("backup_encryption_method", BackupEncryptionMethod.NO_PASSWORD.value))
        )
    except ValueError:
        backup_encryption_method = BackupEncryptionMethod.NO_PASSWORD
    backup_master_password = str(form.get("backup_master_password", ""))
    is_save_backup_password = str(form.get("backup_save_password", "")).strip() != ""
    backup_api_key_env = str(form.get("backup_api_key_env", ""))

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
            anthropic_api_key=anthropic_api_key,
            backup_provider=backup_provider,
            backup_encryption_method=backup_encryption_method,
            backup_api_key_env=backup_api_key_env,
            has_saved_backup_password=has_saved_backup_password(agent_creator.paths),
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

    # Resolve the account email when needed (imbue_cloud compute, AI, or
    # backup). The mngr_imbue_cloud plugin owns the SuperTokens session and
    # is responsible for fetching a fresh access token at the time of each
    # subprocess invocation, so minds only needs to know which account to
    # ask for.
    is_imbue_cloud_backup = backup_provider is BackupProvider.IMBUE_CLOUD
    account_email = ""
    if (
        account_id
        and session_store_inst is not None
        and (is_imbue_cloud_compute or is_imbue_cloud_ai or is_imbue_cloud_backup)
    ):
        account_email = session_store_inst.get_account_email(account_id) or ""

    # Resolve the backup configuration (reads / first-time-saves the shared
    # master password as a side effect) before kicking off creation.
    backup_request, backup_error = _build_backup_request_or_error(
        backup_provider=backup_provider,
        encryption_method=backup_encryption_method,
        typed_master_password=backup_master_password,
        is_save_password=is_save_backup_password,
        api_key_env=backup_api_key_env,
        account_email=account_email,
        paths=agent_creator.paths,
    )
    if backup_error is not None:
        return _re_render_with_error(backup_error)

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
        on_created=on_created,
        backup_request=backup_request,
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
    agent_creator: AgentCreator | None = request.app.state.agent_creator
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    is_backup_password_saved = has_saved_backup_password(agent_creator.paths) if agent_creator is not None else False
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
        has_saved_backup_password=is_backup_password_saved,
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
    try:
        backup_provider = BackupProvider(str(body.get("backup_provider", BackupProvider.CONFIGURE_LATER.value)))
    except ValueError:
        return Response(
            status_code=400,
            content='{"error": "Invalid backup_provider"}',
            media_type="application/json",
        )
    try:
        backup_encryption_method = BackupEncryptionMethod(
            str(body.get("backup_encryption_method", BackupEncryptionMethod.NO_PASSWORD.value))
        )
    except ValueError:
        return Response(
            status_code=400,
            content='{"error": "Invalid backup_encryption_method"}',
            media_type="application/json",
        )
    backup_master_password = str(body.get("backup_master_password", ""))
    is_save_backup_password = bool(body.get("backup_save_password", False))
    backup_api_key_env = str(body.get("backup_api_key_env", ""))
    anthropic_api_key = str(body.get("anthropic_api_key", "")).strip()
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

    # Resolve the account email when an imbue_cloud field is selected (compute,
    # AI, or backup) so the background creation can mint a LiteLLM key / lease a
    # pool host / create a backup bucket. The session store is the source of
    # truth for email <-> user_id mapping.
    is_imbue_cloud_backup = backup_provider is BackupProvider.IMBUE_CLOUD
    account_email = ""
    if account_id and (is_imbue_cloud_compute or is_imbue_cloud_ai or is_imbue_cloud_backup):
        session_store_inst: MultiAccountSessionStore | None = request.app.state.session_store
        if session_store_inst is not None:
            account_email = session_store_inst.get_account_email(account_id) or ""

    # FIXME: two duplicate-name footguns this 409 doesn't cover:
    # (1) API + empty ``host_name``: a second POST with the same
    #     ``git_url`` auto-derives the same name via
    #     ``extract_repo_name`` and fails as a deferred ``FAILED``
    #     status mid-creation. Fix: derive + uniquify here, or
    #     reject the duplicate inline.
    # (2) Form + default ``"assistant"``: the form pre-fills with
    #     ``_FALLBACK_HOST_NAME``, but ``_handle_create_form_submit``
    #     never runs this check, so a second Create with the
    #     untouched default also fails as ``FAILED``. Fix: uniquify
    #     the default at render time, or mirror this 409 on the form
    #     path.
    if host_name:
        backend_resolver = request.app.state.backend_resolver
        existing_names: set[str] = set()
        for existing_id in backend_resolver.list_known_workspace_ids():
            existing_name = backend_resolver.get_workspace_name(existing_id)
            if existing_name is not None:
                existing_names.add(existing_name)
        if host_name in existing_names:
            return Response(
                status_code=409,
                content=json.dumps(
                    {
                        "error": (
                            "An agent named '{}' already exists. "
                            "Pick a different name, or destroy the existing one first."
                        ).format(host_name)
                    }
                ),
                media_type="application/json",
            )

    backup_request, backup_error = _build_backup_request_or_error(
        backup_provider=backup_provider,
        encryption_method=backup_encryption_method,
        typed_master_password=backup_master_password,
        is_save_password=is_save_backup_password,
        api_key_env=backup_api_key_env,
        account_email=account_email,
        paths=agent_creator.paths,
    )
    if backup_error is not None:
        return Response(
            status_code=400,
            content=json.dumps({"error": backup_error}),
            media_type="application/json",
        )

    creation_id = agent_creator.start_creation(
        git_url,
        host_name=host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        anthropic_api_key=anthropic_api_key,
        backup_request=backup_request,
    )

    # Apply any onboarding answers supplied inline by the API caller. Absent
    # / empty fields map to the no-op path, so existing callers that omit
    # them are unaffected. The form-driven UI submits answers separately via
    # POST /api/create-agent/{id}/onboarding once the user finishes the
    # questions.
    onboarding_applier: OnboardingApplier | None = request.app.state.onboarding_applier
    if onboarding_applier is not None:
        onboarding_applier.start_apply(creation_id, _parse_onboarding_answers(body))

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


def _parse_onboarding_answers(data: Mapping[str, object]) -> OnboardingAnswers:
    """Parse the three optional onboarding fields from a JSON body / form mapping.

    An unrecognized or empty ``user_data_preference`` resolves to ``None``
    (the question was skipped), matching the no-op semantics of every
    onboarding answer.
    """
    raw_preference = str(data.get("user_data_preference", "")).strip()
    data_preference: UserDataPreference | None = None
    if raw_preference:
        try:
            data_preference = UserDataPreference(raw_preference)
        except ValueError:
            data_preference = None
    return OnboardingAnswers(
        data_preference=data_preference,
        initial_problem=str(data.get("initial_problem", "")),
        permissions_preference=str(data.get("permissions_preference", "")),
    )


async def _handle_onboarding_submit(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Apply onboarding answers for an in-flight creation (POST /api/create-agent/{agent_id}/onboarding).

    Used by the creating-page question flow: the answers are submitted once
    the user finishes the questions, then applied on a background thread.
    Returns immediately; the route param carries a ``CreationId``.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    onboarding_applier: OnboardingApplier | None = request.app.state.onboarding_applier
    if onboarding_applier is None:
        return Response(
            status_code=501, content='{"error": "Onboarding not configured"}', media_type="application/json"
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")

    creation_id = CreationId(agent_id)
    if onboarding_applier.agent_creator.get_creation_info(creation_id) is None:
        return Response(status_code=404, content='{"error": "Unknown agent creation"}', media_type="application/json")

    answers = _parse_onboarding_answers(body)
    onboarding_applier.start_apply(creation_id, answers)
    return Response(content=json.dumps({"status": "ok"}), media_type="application/json")


def _handle_creating_page(
    agent_id: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Show the creating/onboarding page (GET /creating/{agent_id}).

    The page renders the onboarding questions first (the workspace is
    already being created in the background) and falls through to the
    loading screen if creation hasn't finished by the time the user is
    done. It no longer redirects when creation is already DONE -- the
    questions still need to be shown so their answers can take effect; the
    page itself redirects into the workspace once the user finishes.
    """
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
    Writes via :func:`set_provider_is_enabled`, then bounces the detached
    ``mngr latchkey forward`` supervisor's ``mngr observe`` child -- the single
    discovery observer -- to pick up the new setting. The next
    ``FullDiscoverySnapshotEvent`` it writes to the shared discovery log is tailed
    by minds' ``mngr forward --observe-via-file``; the chrome's optimistic
    "waiting for refresh" state clears at that point.
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
    # Only bounce when the settings file actually changed -- a no-op toggle
    # (e.g. user clicking Disable twice) should not trigger a SIGHUP and a full
    # mngr observe restart, since the next discovery snapshot would be identical.
    if changed:
        # Bounce the single discovery observer (latchkey forward's `mngr observe`)
        # so its next snapshot reflects the new provider set; minds' `mngr forward`
        # tails the resulting shared discovery log.
        bounce_latchkey_forward_supervisor(request.app.state.latchkey_forward_supervisor)
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


def _handle_dev_styleguide() -> Response:
    """Render the design-system styleguide page."""
    return HTMLResponse(content=render_dev_styleguide_page())


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
            paths: WorkspacePaths | None = request.app.state.api_v1_paths
            last_workspace_data = _build_workspace_list(backend_resolver, session_store)
            last_destroying_ids = _destroying_agent_ids(paths, backend_resolver.list_known_workspace_ids())
            has_accounts = bool(session_store and session_store.list_accounts())
            yield "data: {}\n\n".format(
                json.dumps(
                    {
                        "type": "workspaces",
                        "workspaces": last_workspace_data,
                        "destroying_agent_ids": last_destroying_ids,
                        "has_accounts": has_accounts,
                    }
                )
            )
            # Send the initial providers panel state so the chrome can render
            # the providers section before the first resolver change fires.
            last_providers_data = _build_providers_state_payload(backend_resolver)
            yield "data: {}\n\n".format(json.dumps({"type": "providers_state", **last_providers_data}))
            inbox: RequestInbox | None = request.app.state.request_inbox
            last_requests_payload = _build_requests_payload(inbox)
            # ``auto_open`` is bundled with the requests payload (rather than
            # its own SSE event) so the Electron shell sees both atomically
            # when deciding whether to auto-open the panel.
            minds_config: MindsConfig | None = request.app.state.minds_config
            auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
            yield "data: {}\n\n".format(
                json.dumps({"type": "requests", **last_requests_payload, "auto_open": auto_open})
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
                current_destroying_ids = _destroying_agent_ids(paths, backend_resolver.list_known_workspace_ids())
                if current_data != last_workspace_data or current_destroying_ids != last_destroying_ids:
                    last_workspace_data = current_data
                    last_destroying_ids = current_destroying_ids
                    yield "data: {}\n\n".format(
                        json.dumps(
                            {
                                "type": "workspaces",
                                "workspaces": current_data,
                                "destroying_agent_ids": current_destroying_ids,
                            }
                        )
                    )

                current_providers_data = _build_providers_state_payload(backend_resolver)
                if current_providers_data != last_providers_data:
                    last_providers_data = current_providers_data
                    yield "data: {}\n\n".format(json.dumps({"type": "providers_state", **current_providers_data}))

                inbox = request.app.state.request_inbox
                current_requests_payload = _build_requests_payload(inbox)
                # Diff the full payload (count + ordered pending ids), not just
                # the count, so a change to the pending *set* at constant size
                # still pushes an update and the panel refreshes.
                if current_requests_payload != last_requests_payload:
                    last_requests_payload = current_requests_payload
                    auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
                    yield "data: {}\n\n".format(
                        json.dumps({"type": "requests", **current_requests_payload, "auto_open": auto_open})
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


def _destroying_agent_ids(paths: WorkspacePaths | None, known_workspace_ids: tuple[AgentId, ...]) -> list[str]:
    """Return the agent ids currently in any in-flight / failed destroy state.

    Pure read of the on-disk ``destroying/`` dir; never deletes records (the
    landing-page render path owns DONE-record cleanup). The chrome SSE emits
    this alongside the workspaces list so Electron can distinguish "the
    workspace disappeared because we destroyed it" from "discovery transiently
    lost it" -- the latter must not navigate the user's window away from a
    workspace that is still around.
    """
    if paths is None:
        return []
    in_resolver = frozenset(known_workspace_ids)
    records = list_destroying(paths, in_resolver)
    return [str(agent_id) for agent_id, record in records.items() if record.status != DestroyingStatus.DONE]


def _build_workspace_list(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
) -> list[dict[str, str]]:
    """Build a JSON-serializable list of workspaces from the backend resolver.

    Each entry carries a deterministic "accent" CSS color derived from the
    agent id so the chrome and sidebar can render a per-workspace accent
    without running a digest in JS. Entries whose provider's latest discovery
    poll errored carry ``is_stale="true"`` so the UI can flag them as
    retained-but-unverified (they remain fully interactive).
    """
    errored_provider_names = {str(name) for name in backend_resolver.get_provider_errors()}
    agent_ids = backend_resolver.list_known_workspace_ids()
    workspaces: list[dict[str, str]] = []
    for aid in agent_ids:
        info = backend_resolver.get_agent_display_info(aid)
        ws_name = backend_resolver.get_workspace_name(aid)
        if not ws_name:
            ws_name = info.agent_name if info else str(aid)
        entry: dict[str, str] = {"id": str(aid), "name": ws_name, "accent": workspace_accent(str(aid))}
        # Mark the workspace stale when its provider's most recent discovery
        # poll errored: it was retained from prior state, so its liveness is
        # unverified rather than confirmed healthy.
        if info is not None and info.provider_name is not None and info.provider_name in errored_provider_names:
            entry["is_stale"] = "true"
        if session_store is not None:
            account = session_store.get_account_for_workspace(str(aid))
            if account is not None:
                entry["account"] = account.email
        workspaces.append(entry)
    return workspaces


def _build_requests_payload(inbox: RequestInbox | None) -> dict[str, Any]:
    """Build the content-based requests payload pushed over the chrome SSE.

    The chrome's live request UI (badge, panel refresh, auto-open) must react
    to any change in the *set* of pending requests, not merely its size. A
    bare count is a lossy summary: if one request is resolved while another
    arrives, the count is unchanged even though the inbox contents are not.
    Keying updates off the count therefore silently drops those transitions.

    To make change detection sound, we surface the actual pending request
    ids (in a deterministic order) alongside the count. Consumers diff
    ``request_ids`` to decide whether to refresh the panel and which ids are
    newly arrived (for auto-open); the count remains for the badge.
    """
    pending = inbox.get_pending_requests() if inbox else []
    request_ids = [str(req.event_id) for req in pending]
    return {"count": len(request_ids), "request_ids": request_ids}


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
# How long we wait for the system interface to answer again after a restart,
# split by tier. A surgical (in-place) restart leaves the container running, so
# the interface should answer again quickly. A host restart cold-boots the
# container (restore-from-snapshot + the bootstrap service manager spawning the
# system interface), which legitimately takes longer. Initial agent-creation
# readiness waiting keeps its own, much longer, timeout.
_SURGICAL_STARTUP_WAIT_SECONDS: Final[float] = 15.0
_HOST_RESTART_STARTUP_WAIT_SECONDS: Final[float] = 30.0
# Poll cadence while waiting for the system interface to come back post-restart.
_RESTART_PROBE_INTERVAL_SECONDS: Final[float] = 1.0


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
    provider_name: str | None,
) -> list[str]:
    """Build the argv for the layer-2 probe: list agents to read each host's lifecycle state.

    The recovery page keys its restart tier off the workspace host's state:
    a RUNNING host can be recovered with the surgical system-interface
    restart, while a stopped host needs a full host restart. ``mngr list``
    is a pure read -- it never starts a stopped container.

    Scopes the listing to just this workspace's chat agent + system-services
    agent via a CEL ``id == ...`` include, for a smaller payload. When the
    workspace's provider is known it also passes ``--provider`` so discovery
    only queries that provider: ``--provider`` is a discovery fan-out control
    (unlike the post-discovery CEL ``--include``), so an unrelated provider
    being unreachable does not make this listing exit nonzero and blank out
    this workspace's own host state. ``--on-error continue`` keeps a per-host
    failure within the scoped provider from hard-failing the listing.
    """
    if services_agent_id is None:
        include = f'id == "{agent_id}"'
    else:
        include = f'id == "{agent_id}" || id == "{services_agent_id}"'
    argv = [
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
    if provider_name is not None:
        argv += ["--provider", provider_name]
    return argv


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


def _ssh_command_for_agent(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str | None:
    """Build the copy-pasteable SSH command for an agent's host, or None when it has no SSH info.

    Every minds workspace (Docker, Lima, remote) is reached over SSH, so this is
    populated in practice; it is None only during the brief window before
    discovery surfaces the host's ``HOST_SSH_INFO`` event. The format matches the
    command mngr itself emits for the host (``ssh -i <key> -p <port> <user>@<host>``).
    """
    ssh_info = backend_resolver.get_ssh_info(agent_id)
    if ssh_info is None:
        return None
    return f"ssh -i {ssh_info.key_path} -p {ssh_info.port} {ssh_info.user}@{ssh_info.host}"


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
            # HEALTHY with no return_to to redirect to: render with
            # render_status still HEALTHY -- the page then offers a manual
            # restart button.
            pass
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    html_body = render_recovery_page(
        agent_id=aid,
        return_to=return_to,
        initial_status=render_status,
        initial_error=initial_error,
        ssh_command=_ssh_command_for_agent(backend_resolver, aid),
    )
    return HTMLResponse(content=html_body)


def _run_mngr(concurrency_group: ConcurrencyGroup, argv: list[str], env: dict[str, str]) -> str:
    """Run an ``mngr`` subprocess to completion and return its stdout on a clean exit.

    Raises ``MngrCommandError`` for every non-clean outcome, like the rest of
    minds' mngr calls (``run_mngr_create``, the destroy cleanup) -- one domain
    error the caller catches once. The non-clean outcomes are:

    * a timeout (with ``is_checked_after=False`` a timeout comes back as a
      finished process flagged ``is_timed_out`` rather than raising);
    * a nonzero exit;
    * a failure to launch at all -- ``OSError`` for fork/exec failures, and
      ``ConcurrencyGroupError`` for the group's own setup / shutdown / strand
      failures (``ProcessSetupError``, ``StrandTimedOutError``,
      ``EnvironmentStoppedError``, ``InvalidConcurrencyGroupStateError``).
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=_RESTART_COMMAND_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=env,
        )
    except (OSError, ConcurrencyGroupError) as exc:
        # The command never ran (a fork/exec failure, or a concurrency-group
        # setup/strand/shutdown failure). create/destroy let these propagate to
        # one outer handler because a launch failure is fatal to the operation;
        # our callers instead handle failure locally and must keep going (the
        # host-health probe composes a partial response and cannot 500), so we
        # wrap it as the single MngrCommandError they already catch rather than
        # leaving them to also catch this infra-exception tuple.
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        raise MngrCommandError(f"timed out after {int(_RESTART_COMMAND_TIMEOUT_SECONDS)}s")
    if finished.returncode != 0:
        raise MngrCommandError(f"exited {finished.returncode}: {finished.stderr.strip()}")
    return finished.stdout


def _await_system_interface_ready(
    agent_id: AgentId, mngr_forward_port: int, preauth_cookie: str, wait_seconds: float
) -> bool:
    """Poll the system interface through the plugin until it answers 200, or ``wait_seconds`` elapses."""
    deadline = time.monotonic() + wait_seconds
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
    skip_stop: bool = False,
) -> None:
    """Background worker: stop + start the system-services agent, then await recovery.

    Drives the health tracker to HEALTHY on recovery or RESTART_FAILED (with a
    reason) when a step errors or the system interface does not return within
    the tier's startup-wait budget (the host tier cold-boots a container, so it
    waits longer than the in-place surgical tier). A crash of this worker is
    turned into RESTART_FAILED by ``_RestartWorkerFailureHandler``, wired as the
    thread's ``on_failure`` callback.

    ``skip_stop`` is set only for the auto-dispatched host tier, which is chosen
    exclusively when the host-health probe found the container fully stopped --
    there is nothing to stop, so the (idempotent but not free) ``mngr stop
    --stop-host`` subprocess is skipped to shave a full mngr invocation off the
    cold boot's critical path.
    """
    tier_label = "host restart" if is_host_restart else "system-interface restart"
    startup_wait_seconds = _HOST_RESTART_STARTUP_WAIT_SECONDS if is_host_restart else _SURGICAL_STARTUP_WAIT_SECONDS
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        tracker.mark_restart_failed(
            workspace_agent_id, "Could not locate the system-services agent for this workspace."
        )
        return

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)

    if skip_stop:
        logger.info("Skipping stop step for {} ({}): container already fully stopped", workspace_agent_id, tier_label)
    else:
        try:
            _run_mngr(concurrency_group, _build_mngr_stop_argv(mngr_binary, services_agent_id, is_host_restart), env)
        except MngrCommandError as exc:
            logger.warning("Stop step of {} for {} failed: {}", tier_label, workspace_agent_id, exc)
            tracker.mark_restart_failed(workspace_agent_id, f"Stop step of {tier_label} failed: {exc}")
            return

    try:
        _run_mngr(concurrency_group, _build_mngr_start_argv(mngr_binary, services_agent_id), env)
    except MngrCommandError as exc:
        logger.warning("Start step of {} for {} failed: {}", tier_label, workspace_agent_id, exc)
        tracker.mark_restart_failed(workspace_agent_id, f"Start step of {tier_label} failed: {exc}")
        return

    # Without a plugin route there is no way to probe for recovery, so treat a
    # clean dispatch as success (mirrors the background probe loop being a no-op).
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie:
        tracker.record_probe_success(workspace_agent_id)
        return

    if _await_system_interface_ready(
        workspace_agent_id, mngr_forward_port, mngr_forward_preauth_cookie, startup_wait_seconds
    ):
        tracker.record_probe_success(workspace_agent_id)
    else:
        tracker.mark_restart_failed(
            workspace_agent_id,
            f"The system interface did not respond within {int(startup_wait_seconds)}s of the {tier_label}.",
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

    # The auto-dispatched host tier (chosen only when the host-health probe
    # found the container fully stopped) passes ``host_already_stopped=1`` so
    # the worker can skip the redundant stop step. Honored only for host
    # restarts: a manually-requested restart may target a still-running
    # container, which must be stopped first.
    skip_stop = is_host_restart and request.query_params.get("host_already_stopped") == "1"

    # is_checked=False + on_failure: a crash of the one-shot worker is handled
    # by transitioning the tracker to RESTART_FAILED (so the recovery page does
    # not hang), rather than being surfaced later when the root group is checked.
    #
    # The spawn itself can also raise (``ConcurrencyGroupError`` when the group
    # is shutting down). Since we've already claimed RESTARTING, catch that here
    # and roll the tracker into RESTART_FAILED too -- otherwise it would be stuck
    # RESTARTING forever with no worker to advance it.
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
                "skip_stop": skip_stop,
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
    """Layer-2 probe: run each recovery-diagnostics probe, classify the dispatch tier.

    Returns a flat ``HostHealthResponse`` -- a list of named probes plus a
    derived ``dispatch_tier``. The recovery page renders each probe as a
    row and keys its restart-tier branching off ``dispatch_tier``.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    concurrency_group: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    if concurrency_group is None:
        return _json_error("Host health probe is unavailable in this configuration", status_code=503)
    response = _run_host_health_probe(aid, request, concurrency_group)
    logger.info("Layer-2 host-state probe for {}: dispatch_tier={}", aid, response.dispatch_tier.value)
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
    host state / services-agent state, the batched in-container
    ``mngr exec`` probe, and the plugin's resolver-snapshot mirror.
    """
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(request.app.state.mngr_host_dir)
    mngr_binary: str = request.app.state.mngr_binary
    backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
    services_agent_id = backend_resolver.get_system_services_agent_id(agent_id)
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    list_argv = _build_mngr_host_state_argv(mngr_binary, agent_id, services_agent_id, provider_name)
    list_command = shlex.join(list_argv)
    list_error: str | None = None
    list_stdout = ""
    try:
        list_stdout = _run_mngr(concurrency_group, list_argv, env)
    except MngrCommandError as exc:
        # The listing is scoped to this workspace's own provider (see
        # _build_mngr_host_state_argv), so a non-clean exit reflects a problem
        # with *this* provider/host rather than an unrelated sibling, and there
        # is no trustworthy listing to keep. Record the reason and continue with
        # an empty listing; it is logged here and threaded into the response so
        # the recovery page can surface it on the host-state rows in place of a
        # bare "no row".
        list_error = str(exc)
        logger.warning("`mngr list` for host-health probe of {} did not exit cleanly: {}", agent_id, list_error)
    list_json: str | None = list_stdout or None
    # The in-container probe stays quiet at warning level: its argv embeds a
    # long base64 inner script that adds nothing to diagnostics, and the
    # dispatch_tier INFO line already records the outcome. Trust the stdout only
    # on a clean exit -- any non-clean outcome (a failed ``mngr exec`` such as
    # ``--no-start`` against a stopped host, a timeout, or a launch / group
    # failure) raises and leaves ``in_container_stdout`` None, which parses to a
    # "no" on the can-we-run-commands probe, and is recorded only at debug.
    in_container_stdout: str | None = None
    if services_agent_id is not None:
        try:
            in_container_stdout = _run_mngr(concurrency_group, build_probe_argv(mngr_binary, services_agent_id), env)
        except MngrCommandError as exc:
            logger.debug("in-container probe for host-health of {} did not exit cleanly: {}", agent_id, exc)
    consumer: EnvelopeStreamConsumer | None = request.app.state.envelope_stream_consumer
    plugin_resolver_services: dict[str, str] = (
        consumer.get_resolver_snapshot_for_agent(agent_id) if consumer is not None else {}
    )
    if services_agent_id is not None:
        exec_command = shlex.join(build_probe_argv(mngr_binary, services_agent_id))
    else:
        exec_command = "(mngr exec <system-services-agent>) -- no services agent id known"
    return build_host_health_response(
        list_json=list_json,
        agent_id=agent_id,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services,
        mngr_list_command=list_command,
        mngr_list_error=list_error,
        mngr_exec_command=exec_command,
        mngr_binary=mngr_binary,
    )


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
            # plugin owns tunnel state -- minds keeps no local cache. After
            # deleting the tunnel server-side, also clear the token file inside
            # the agent so its cloudflare-tunnel service stops cloudflared
            # rather than spinning against a now-deleted tunnel.
            if cli is not None:
                try:
                    tunnel = cli.find_tunnel_for_agent(account=str(account.email), agent_id=agent_id)
                    if tunnel is not None:
                        cli.delete_tunnel(account=str(account.email), tunnel_name=tunnel.tunnel_name)
                        clear_tunnel_token_from_agent(AgentId(agent_id))
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
        return HTMLResponse(
            content=render_request_unavailable_page(message="It may have expired, or it was opened from an old link."),
            status_code=404,
        )
    # A granted/denied request lingers in the append-only log, so re-rendering
    # the grant/deny form would let the user act on it again. Show a friendly
    # "no longer available" page instead.
    if inbox.is_request_resolved(request_id):
        return HTMLResponse(
            content=render_request_unavailable_page(message="It has already been processed."),
            status_code=200,
        )

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


_SHARE_READINESS_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0


async def _probe_share_url_readiness(http_client: httpx.AsyncClient, url: str) -> bool:
    """Fetch ``url`` once and report whether the Cloudflare Access app is live.

    Uses the app's shared (``follow_redirects=False``) client so the Access
    login redirect is observed rather than followed. Any transport error or
    timeout is treated as "not ready yet".
    """
    try:
        response = await http_client.get(url, timeout=_SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Probed share URL {} but it is not ready yet: {}", url, exc)
        return False
    return is_share_ready_from_edge_response(response.status_code, response.headers.get("location"))


async def _handle_sharing_readiness_api(
    agent_id: str,
    service_name: str,
    request: Request,
    auth_store: AuthStoreDep,
) -> Response:
    """Probe a shared service's hostname to see if Cloudflare Access is live yet.

    Cloudflare can take a few seconds after sharing is enabled to publish the
    Access application at the edge. Until then the hostname does not return the
    Access login redirect, so showing the URL immediately makes forwarding look
    broken. The editor JS polls this endpoint and only reveals the link once the
    edge returns the Access redirect (or a short client-side timeout elapses).
    Probing from minds keeps the connector request short and lets the browser
    drive the wait. Contract: ``{"ready": bool}``.
    """
    if not _is_authenticated(cookies=request.cookies, auth_store=auth_store):
        return Response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    probe_url = request.query_params.get("url", "")
    http_client: httpx.AsyncClient | None = request.app.state.http_client
    if http_client is None or not is_probeable_share_url(probe_url):
        return Response(content=json.dumps({"ready": False}), media_type="application/json")
    is_ready = await _probe_share_url_readiness(http_client, probe_url)
    return Response(content=json.dumps({"ready": is_ready}), media_type="application/json")


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
    # Reject a second grant/deny on an already-resolved request so a stale
    # (e.g. cached) form cannot re-apply side effects.
    if inbox.is_request_resolved(request_id):
        return _json_error("This request has already been approved or denied.", status_code=409)

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


def _handle_request_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Process an incoming request event and add it to the app's inbox.

    After mutating the inbox, fires the resolver's change notification so
    the chrome SSE wakes up and pushes the new ``requests`` payload immediately
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
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None = None,
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
    # Handle to the detached ``mngr latchkey forward`` supervisor so the
    # provider-change request handlers (provider toggle, signin/signout) can
    # ``bounce()`` it alongside the ``mngr forward`` observe bounce, keeping
    # latchkey's provider set in lockstep with minds' own. None in tests.
    app.state.latchkey_forward_supervisor = latchkey_forward_supervisor
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
    # Applies onboarding answers (Q1 local scan, Q2 chat message, Q3 memory
    # file) on a background thread. Available whenever agent creation is: it
    # reuses the agent creator's own root concurrency group to track the
    # detached apply thread, and reads the host name / canonical agent id off
    # the creator. Without an agent_creator the endpoint returns 501.
    onboarding_applier: OnboardingApplier | None = None
    if agent_creator is not None:
        onboarding_applier = OnboardingApplier(
            agent_creator=agent_creator,
            paths=agent_creator.paths,
            message_sender=MngrMessageSender(mngr_binary=mngr_binary),
            root_concurrency_group=agent_creator.root_concurrency_group,
            mngr_binary=mngr_binary,
        )
    app.state.onboarding_applier = onboarding_applier
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

    app.get("/_dev/styleguide")(_handle_dev_styleguide)

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
    app.get("/api/sharing-readiness/{agent_id}/{service_name}")(_handle_sharing_readiness_api)

    # Agent creation routes
    app.get("/create")(_handle_create_page)
    app.post("/create")(_handle_create_form_submit)
    app.get("/api/backup-status")(_handle_backup_status_api)
    app.get("/api/backup-export/{agent_id}")(_handle_backup_export_api)
    app.post("/api/create-agent")(_handle_create_agent_api)
    app.post("/api/create-agent/{agent_id}/onboarding")(_handle_onboarding_submit)
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
