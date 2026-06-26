import json
import os
import queue
import shlex
import threading
import time
from collections.abc import Collection
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never
from urllib.parse import urlparse

import httpx
from flask import Flask
from flask import Response
from flask import abort
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import SecretStr
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
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
from imbue.minds.desktop_client.api_v1 import create_api_v1_blueprint
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
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
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import delete_destroying
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.destroying import read_destroying
from imbue.minds.desktop_client.destroying import read_host_id
from imbue.minds.desktop_client.destroying import read_log_chunk
from imbue.minds.desktop_client.destroying import start_destroy
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.mind_liveness import MindLiveness
from imbue.minds.desktop_client.mind_liveness import compute_mind_liveness_by_agent_id
from imbue.minds.desktop_client.mind_liveness import get_shutdown_capable_workspace_agent_ids
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.recovery_probe import HostHealthResponse
from imbue.minds.desktop_client.recovery_probe import build_host_health_response
from imbue.minds.desktop_client.recovery_probe import build_probe_argv
from imbue.minds.desktop_client.region_preference import AWS_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import default_region_for_provider
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.region_preference import resolve_default_region
from imbue.minds.desktop_client.report_collector import submit_bug_report_from_body
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.responses import make_file_response
from imbue.minds.desktop_client.responses import make_html_response
from imbue.minds.desktop_client.responses import make_redirect_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.responses import make_streaming_response
from imbue.minds.desktop_client.responses import safe_local_redirect_path
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_sharing_via_cloudflare
from imbue.minds.desktop_client.sharing_handler import is_probeable_share_url
from imbue.minds.desktop_client.sharing_handler import is_share_ready_from_edge_response
from imbue.minds.desktop_client.sharing_handler import parse_emails_form_value
from imbue.minds.desktop_client.sharing_handler import resolve_account_email_for_workspace
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.state import set_state
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
from imbue.minds.desktop_client.supertokens_routes import create_supertokens_blueprint
from imbue.minds.desktop_client.supertokens_routes import signout_user_via_plugin
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.templates import render_accounts_page
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_consent_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_creating_page
from imbue.minds.desktop_client.templates import render_destroying_page
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_help_page
from imbue.minds.desktop_client.templates import render_inbox_list_fragment
from imbue.minds.desktop_client.templates import render_inbox_page
from imbue.minds.desktop_client.templates import render_inbox_unavailable_fragment
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_recovery_page
from imbue.minds.desktop_client.templates import render_settings_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_welcome_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates import resolve_create_host_name
from imbue.minds.desktop_client.templates import status_text_for
from imbue.minds.desktop_client.tunnel_token_injection import clear_tunnel_token_from_agent
from imbue.minds.desktop_client.tunnel_token_injection import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.webdav import create_webdav_app
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color
from imbue.minds.envs.docker_cleanup import DockerCleanupError
from imbue.minds.envs.docker_cleanup import stop_active_env_state_container
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import InvalidJsonBodyError
from imbue.minds.errors import MindsConfigError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServiceName
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.api.discovery_events import DISCOVERY_STREAM_POLL_INTERVAL_SECONDS
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0


def _json_error(message: str, status_code: int) -> Response:
    """Return a small ``{"error": ...}`` JSON response."""
    return make_response(
        content=json.dumps({"error": message}),
        media_type="application/json",
        status_code=status_code,
    )


def _enqueue_health_change(
    health_queue: "queue.Queue[tuple[str, AgentHealth]]",
    change_event: threading.Event,
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


def _should_emit_system_interface_status(
    backend_resolver: BackendResolverInterface,
    tracker: SystemInterfaceHealthTracker | None,
    agent_id: AgentId,
    status: AgentHealth,
) -> bool:
    """Whether to push a ``system_interface_status`` event for an agent in ``status``.

    A STUCK status is what drives the chrome to redirect the content view to the
    recovery page. Gate that redirect on a discovery snapshot taken *after* the
    outage began: a snapshot that predates the outage still carries the pre-outage
    host state (a just-stopped container still reads RUNNING), which would
    misclassify the recovery tier and ask the user to confirm a restart instead of
    auto-dispatching one. So suppress STUCK -- keeping the user on the
    auto-refreshing "Loading workspace" loader -- until a full snapshot whose
    producer timestamp is at or after the agent's outage onset
    (``get_failure_run_started_wall_at``) has landed; by then discovery has
    re-observed the host and the classification is trustworthy. The next emission
    (the per-wake flip check or the periodic re-assert in the chrome-events loop)
    then pushes STUCK and the redirect fires.

    When no onset is recorded (only the force-``mark_stuck`` path, used in tests,
    lacks one) fall back to the absolute-age freshness gate so that path is not
    stranded. Non-STUCK statuses (RESTARTING, RESTART_FAILED, HEALTHY) are emitted
    unconditionally -- they do not trigger the redirect, and the user is already on
    the recovery page when they apply. Only the passive-discovery resolver tracks
    snapshot freshness; for any other resolver the redirect is not gated.
    """
    if status != AgentHealth.STUCK:
        return True
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        return True
    _, last_full_snapshot_at = backend_resolver.get_freshness_timestamps()
    onset = tracker.get_failure_run_started_wall_at(agent_id) if tracker is not None else None
    # FIXME: when discovery is *persistently* stale -- the producer/consumer
    # pipeline has stalled, not merely a provider being down -- no post-onset
    # snapshot ever arrives, so this gate never lets the STUCK redirect through and
    # the user is stranded on the "Loading workspace" loader with no recourse. A
    # discovery-health watchdog should detect a stalled pipeline (snapshot age),
    # auto-restart it, and surface a distinct app-level state; once that exists
    # this gate gains an escape and this FIXME should be removed.
    if onset is None:
        return _is_discovery_fresh(last_full_snapshot_at)
    return last_full_snapshot_at is not None and last_full_snapshot_at >= onset


def _discovery_health_payload(health: DiscoveryHealth) -> dict[str, str]:
    """Build a ``discovery_health`` SSE payload for the app-global pipeline state."""
    return {"type": "discovery_health", "state": health.value}


# -- Request-body + dependency helpers --


def _read_json_body() -> Any:
    """Parse the request body as JSON, raising ``ValueError`` on missing/invalid input.

    Mirrors the FastAPI ``await request.json()`` contract closely enough that
    the existing ``except (json.JSONDecodeError, ValueError)`` handlers around
    the call sites keep working: Flask's ``get_json(silent=True)`` returns
    ``None`` on a malformed body, which we turn into a ``ValueError``.

    ``force=True`` parses the body regardless of the request's ``Content-Type``
    so a client that POSTs JSON without an ``application/json`` header is still
    accepted -- matching the FastAPI ``request.json()`` behavior (which ignored
    the content type) and avoiding a wire-behavior regression for API callers.
    """
    data = request.get_json(silent=True, force=True)
    if data is None:
        raise InvalidJsonBodyError("Invalid or empty JSON body")
    return data


def _get_mngr_forward_origin() -> str:
    """Build the bare-origin URL of the ``mngr forward`` plugin.

    Used by templates to construct ``/goto/<agent>/`` URLs that target the
    plugin (which owns subdomain forwarding) rather than minds.
    """
    port = get_state().mngr_forward_port or 8421
    return f"http://localhost:{port}"


def _get_is_mac() -> bool:
    """Return True if the request's User-Agent indicates macOS.

    Used by templates that gate macOS-specific styling (traffic-light
    padding, hidden window controls).
    """
    user_agent = request.headers.get("user-agent", "")
    return "Macintosh" in user_agent or "Mac OS" in user_agent


def _int_query_param(name: str, default: int) -> int:
    """Read a single integer query param with a fallback when missing or invalid.

    Used by routes that take optional numeric layout hints from the caller
    (e.g. the sidebar's trigger-anchor params packed into the URL by
    chrome.js). Lives at module level rather than as a closure inside the
    handler because the codebase forbids inline functions (see
    `check_inline_functions` in test_ratchets.py).
    """
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# -- Auth helpers --


def _required_one_time_code() -> OneTimeCode:
    """Parse the required ``one_time_code`` query param, aborting 422 when absent.

    Under FastAPI ``one_time_code`` was a required query parameter, so a request
    missing it was rejected with 422 before the handler ran. Mirror that here:
    abort 422 (the catch-all error handler passes HTTPExceptions through with
    their own status) instead of constructing ``OneTimeCode("")``, which would
    raise and surface as a 500.
    """
    raw = request.args.get("one_time_code")
    if not raw:
        abort(422)
    return OneTimeCode(raw)


def _is_request_authenticated() -> bool:
    """Check whether the current request carries a valid global session cookie."""
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(
        cookie_value=cookie_value,
        signing_key=signing_key,
    )


# -- Route handlers (module-level; deps read from get_state()) --


def _handle_login() -> Response:
    code = _required_one_time_code()

    # If user already has a valid session, redirect to landing page
    if _is_request_authenticated():
        return make_response(status_code=307, headers={"Location": "/"})

    # Render JS redirect to /authenticate (prevents prefetch consumption)
    html = render_login_redirect_page(one_time_code=code)
    return make_html_response(content=html)


def _handle_authenticate() -> Response:
    code = _required_one_time_code()

    is_valid = get_state().auth_store.validate_and_consume_code(code=code)

    if not is_valid:
        html = render_auth_error_page(message="This login code is invalid or has already been used.")
        return make_html_response(content=html, status_code=403)

    # Set a host-only session cookie on the bare origin. We do NOT try to
    # share the cookie across `<agent-id>.localhost` subdomains via
    # ``Domain=localhost`` -- both curl and Chromium treat ``localhost`` as
    # a public suffix and refuse to send such cookies to subdomains. Each
    # subdomain gets its own cookie set on first visit, minted via the
    # ``/goto/{agent_id}/`` auth-bridge redirect below.
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = create_session_cookie(signing_key=signing_key)

    response = make_response(status_code=307, headers={"Location": "/"})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


def _is_workspace_provider_errored(info: AgentDisplayInfo | None, errored_provider_names: Collection[str]) -> bool:
    """True when the agent's provider's most recent discovery poll errored.

    Such a workspace is "stale": it was retained from prior state, so its
    host is unreachable (or at least unverified) until the provider
    recovers. Callers build ``errored_provider_names`` once from
    ``backend_resolver.get_provider_errors()`` -- as a set when checking
    many agents -- and reuse it across calls.
    """
    return info is not None and info.provider_name is not None and info.provider_name in errored_provider_names


def _resolved_workspace_color(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str:
    """The workspace's stored color hex, or the default for label-less workspaces.

    Workspaces created before the color picker shipped have no ``color``
    label on disk; every render surface shows them as
    ``DEFAULT_WORKSPACE_COLOR`` until the user picks a color (which
    persists the label). This helper is that rule's single home.
    """
    stored = backend_resolver.get_workspace_color(agent_id)
    return stored if stored is not None else DEFAULT_WORKSPACE_COLOR


def _color_for_new_workspace(raw_color: object) -> str:
    """Lenient parse of a create request's submitted color, with default fallback.

    The create form posts the picker's hidden ``color`` input and the
    JSON API accepts an optional ``color`` field. A missing or malformed
    value (e.g. the browser ate the input) must not reject the whole
    create request -- the new workspace just gets the default color.
    A *missing* color (an absent field, or an explicit JSON ``null``) is
    normal flow (the JSON API treats it as optional) and stays silent; a
    non-empty value that fails to parse indicates a buggy client, so it
    is logged before falling back.
    """
    stripped = str(raw_color).strip() if raw_color is not None else ""
    normalized = normalize_workspace_color(stripped)
    if normalized is not None:
        return normalized
    if stripped:
        logger.warning("Ignoring malformed create-request color {!r}; using the default workspace color.", stripped)
    return DEFAULT_WORKSPACE_COLOR


def _suggested_create_color(backend_resolver: BackendResolverInterface) -> str:
    """Pick the color to preselect in the create form.

    Gathers the colors currently in use across active workspaces (a
    label-less workspace counts as using ``DEFAULT_WORKSPACE_COLOR``,
    since that's what it renders as) and asks
    ``pick_unused_create_color`` for the first unused palette entry --
    falling back to confusion when there are no workspaces yet or every
    palette entry is taken.
    """
    used = {_resolved_workspace_color(backend_resolver, aid) for aid in backend_resolver.list_active_workspace_ids()}
    return pick_unused_create_color(used)


def _maybe_consent_screen() -> Response | None:
    """Return the error-reporting consent screen if it still needs answering, else None.

    The screen is shown once per machine -- on first launch, and once more after upgrading from a
    build that predates it -- after the user has authenticated and before the landing content, until
    they answer it via POST /consent. Callers are responsible for gating this on authentication. When
    config is unavailable (e.g. minimal test apps) there is nothing to gate on, so this is a no-op.
    """
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is None or minds_config.get_error_reporting_consent_given():
        return None
    return make_html_response(
        content=render_consent_page(
            report_unexpected_errors=minds_config.get_report_unexpected_errors(),
            include_logs=minds_config.get_include_error_logs(),
        )
    )


def _handle_consent_page() -> Response:
    """Render the error-reporting consent screen (GET /consent).

    The consent screen sits just after login, so an unauthenticated request is bounced to the login
    page. If consent was already answered, redirect home so the screen never reappears.
    """
    if not _is_request_authenticated():
        return make_response(status_code=302, headers={"Location": "/login"})
    consent_response = _maybe_consent_screen()
    if consent_response is not None:
        return consent_response
    return make_response(status_code=302, headers={"Location": "/"})


def _handle_consent_submit() -> Response:
    """Record the consent-screen choices and mark consent as answered (POST /consent).

    The consent screen sits just after login, so this requires authentication. "Include logs" is only
    persisted as on when reporting is also on, matching the screen's coupling.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        report = bool(body.get("report_unexpected_errors", False))
        include_logs = bool(body.get("include_logs", False))
        minds_config.set_report_unexpected_errors(report)
        minds_config.set_include_error_logs(include_logs and report)
        minds_config.set_error_reporting_consent_given(True)
    return make_response(status_code=200, content='{"ok": true}', media_type="application/json")


def _handle_error_reporting_settings() -> Response:
    """Persist the error-reporting toggles from the Settings page (POST /_chrome/error-reporting).

    Accepts any subset of ``{report_unexpected_errors, include_logs}``; each present boolean is saved.
    The settings UI clears "include logs" when reporting is turned off, so the stored pair stays
    consistent without extra coercion here.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config:
        if "report_unexpected_errors" in body:
            minds_config.set_report_unexpected_errors(bool(body["report_unexpected_errors"]))
        if "include_logs" in body:
            minds_config.set_include_error_logs(bool(body["include_logs"]))
    return make_response(status_code=200, content='{"ok": true}', media_type="application/json")


def _handle_help_page() -> Response:
    """Render the get-help modal page (GET /help).

    Intentionally unauthenticated: reporting a bug must work even when sign-in itself is broken. The
    ``workspace`` query param (set by the titlebar button) scopes the optional workspace section.
    """
    minds_config: MindsConfig | None = get_state().minds_config
    include_logs_setting = minds_config.get_include_error_logs() if minds_config else False
    workspace_agent_id = request.args.get("workspace", "")
    return make_html_response(
        content=render_help_page(
            include_logs_setting=include_logs_setting,
            workspace_agent_id=workspace_agent_id,
        )
    )


def _handle_help_report() -> Response:
    """Collect and submit a user-submitted bug report from the help form (POST /help/report).

    Unauthenticated for the same reason as the page: the user may be reporting a sign-in problem. The
    shared collector also backs the ``/api/v1`` bug-report route, so both paths produce identical reports.
    """
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(
            status_code=400,
            content='{"error": "Request body must be a JSON object"}',
            media_type="application/json",
        )
    if not str(body.get("description", "")).strip():
        return make_response(
            status_code=400, content='{"error": "A description is required"}', media_type="application/json"
        )

    state = get_state()
    event_id = submit_bug_report_from_body(
        body=body,
        session_store=state.session_store,
        backend_resolver=state.backend_resolver,
        minds_config=state.minds_config,
        paths=state.api_v1_paths,
    )
    return make_response(
        status_code=200,
        content=json.dumps({"ok": True, "event_id": event_id}),
        media_type="application/json",
    )


def _existing_workspace_host_names(backend_resolver: BackendResolverInterface) -> set[str]:
    """Gather the host names of every known workspace across all providers.

    Reads the resolver's discovery snapshot (the aggregated view over all
    providers) rather than shelling out per workspace, per the resolver-cache
    read convention. Uses ``list_known_workspace_ids`` -- the *full* set,
    including workspaces on destroyed-but-still-lingering hosts -- so an
    auto-generated ``mind-N`` name does not collide with one that discovery has
    not yet fully dropped. Feeds both the duplicate-name guard and the
    ``mind-N`` auto-naming in ``resolve_create_host_name``.
    """
    names: set[str] = set()
    for aid in backend_resolver.list_known_workspace_ids():
        name = backend_resolver.get_workspace_name(aid)
        if name is not None:
            names.add(name)
    return names


def _handle_welcome_page() -> Response:
    """Render the welcome/splash page for first-time users."""
    if not _is_request_authenticated():
        html = render_login_page()
        return make_html_response(content=html)
    html = render_welcome_page()
    return make_html_response(content=html)


def _handle_landing_page() -> Response:
    if not _is_request_authenticated():
        html = render_login_page()
        return make_html_response(content=html)

    # The error-reporting consent screen sits just after login: once the user is authenticated but
    # has not yet answered it, show it here before the landing content (the Electron content view and
    # browser both load "/" first, and _handle_post_login_redirect routes here while it is unanswered).
    consent_response = _maybe_consent_screen()
    if consent_response is not None:
        return consent_response

    backend_resolver = get_state().backend_resolver
    all_agent_ids = backend_resolver.list_active_workspace_ids()
    paths: WorkspacePaths | None = get_state().api_v1_paths
    landing_session_store: MultiAccountSessionStore | None = get_state().session_store
    destroying_status_by_agent_id = _resolve_destroying_for_landing(paths, backend_resolver, landing_session_store)

    if all_agent_ids:
        telegram_orchestrator: TelegramSetupOrchestrator | None = get_state().telegram_orchestrator
        telegram_status: dict[str, bool] | None = None
        if telegram_orchestrator is not None:
            telegram_status = {str(aid): telegram_orchestrator.agent_has_telegram(aid) for aid in all_agent_ids}
        agent_names: dict[str, str] = {}
        agent_accents: dict[str, str] = {}
        agent_providers: dict[str, str] = {}
        for aid in all_agent_ids:
            info = backend_resolver.get_agent_display_info(aid)
            ws_name = backend_resolver.get_workspace_name(aid)
            if ws_name:
                agent_names[str(aid)] = ws_name
            else:
                agent_names[str(aid)] = info.agent_name if info else str(aid)
            agent_accents[str(aid)] = _resolved_workspace_color(backend_resolver, aid)
            # Collapse the per-region / per-account provider instance name to a
            # single friendly compute-provider label (e.g. aws-us-west-2 -> AWS).
            agent_providers[str(aid)] = friendly_provider_label(info.provider_name if info else None)
        shutdown_capable_agent_ids = get_shutdown_capable_workspace_agent_ids(backend_resolver)
        mind_liveness_by_agent_id = {
            aid: state.value for aid, state in compute_mind_liveness_by_agent_id(backend_resolver).items()
        }
        html = render_landing_page(
            accessible_agent_ids=all_agent_ids,
            mngr_forward_origin=_get_mngr_forward_origin(),
            telegram_status_by_agent_id=telegram_status,
            agent_names=agent_names,
            destroying_status_by_agent_id=destroying_status_by_agent_id,
            agent_accents=agent_accents,
            shutdown_capable_agent_ids=shutdown_capable_agent_ids,
            mind_liveness_by_agent_id=mind_liveness_by_agent_id,
            agent_providers=agent_providers,
        )
        return make_html_response(content=html)

    # No agents discovered yet. If discovery is still in progress, show a
    # "Discovering agents..." page with auto-refresh. Once discovery has
    # completed with no agents found, show the create form so the user can
    # create their first agent instead of polling forever.
    if not backend_resolver.has_completed_initial_discovery():
        html = render_landing_page(
            accessible_agent_ids=(),
            mngr_forward_origin=_get_mngr_forward_origin(),
            is_discovering=True,
        )
        return make_html_response(content=html)

    git_url = request.args.get("git_url", "")
    branch = request.args.get("branch", "")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    minds_config: MindsConfig | None = get_state().minds_config
    agent_creator: AgentCreator | None = get_state().agent_creator
    geo_cache: GeoLocationCache | None = get_state().geo_location_cache
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    is_backup_password_saved = has_saved_backup_password(agent_creator.paths) if agent_creator is not None else False
    region_options, region_selected = _build_region_form_context(minds_config, geo_cache)
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
        has_saved_backup_password=is_backup_password_saved,
        region_options_by_launch_mode=region_options,
        region_selected_by_launch_mode=region_selected,
        # A deep-link that pre-fills a repo/branch wants those advanced fields
        # visible; otherwise start on the simple preset cards.
        start_advanced=bool(git_url or branch),
        color=_suggested_create_color(backend_resolver),
    )
    return make_html_response(content=html)


def _handle_post_login_redirect() -> Response:
    """Decide where a just-authenticated user lands (GET /post-login).

    All sign-in paths (email/password, OAuth, post-email-verification) funnel
    here. A ``?return_to=`` query param (a safe same-origin path, e.g.
    ``/create`` when the user came from the create page to enable the remote
    preset) wins when present. Otherwise a user who already has workspaces
    goes to the account-management page (the prior behavior); a user with none
    goes to ``/`` -- which renders the create form -- so first-time users land
    on the new-workspace screen instead of the account page.
    """
    if not _is_request_authenticated():
        return make_response(status_code=302, headers={"Location": "/login"})
    # The error-reporting consent screen sits just after login. While it is unanswered, send the user
    # to "/" (the landing handler shows the consent screen there) rather than straight to /accounts or
    # a return_to deep-link, so the one-time consent gate is answered first.
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None and not minds_config.get_error_reporting_consent_given():
        return make_response(status_code=302, headers={"Location": "/"})
    return_to = safe_local_redirect_path(request.args.get("return_to"))
    if return_to is not None:
        return make_response(status_code=302, headers={"Location": return_to})
    backend_resolver = get_state().backend_resolver
    has_any_workspace = bool(backend_resolver.list_active_workspace_ids())
    destination = "/accounts" if has_any_workspace else "/"
    return make_response(status_code=302, headers={"Location": destination})


def _region_provider_key_for_launch_mode(launch_mode: LaunchMode) -> str | None:
    """Map a compute launch mode to its region-config provider key, or None if region-less.

    Only ``IMBUE_CLOUD``, ``VULTR``, and ``AWS`` place a host in a chosen
    region; ``DOCKER`` / ``LIMA`` run locally and have no region.
    """
    if launch_mode is LaunchMode.IMBUE_CLOUD:
        return IMBUE_CLOUD_PROVIDER_KEY
    if launch_mode is LaunchMode.VULTR:
        return VULTR_PROVIDER_KEY
    if launch_mode is LaunchMode.AWS:
        return AWS_PROVIDER_KEY
    return None


def _default_region_for_provider_with_config(
    provider_key: str,
    minds_config: MindsConfig | None,
    geo_cache: GeoLocationCache | None,
) -> str:
    """Resolve the default region to pre-select for a provider (config -> geo -> hardcoded)."""
    configured = minds_config.get_region(provider_key) if minds_config is not None else None
    if geo_cache is not None:
        return resolve_default_region(provider_key, configured, geo_cache)
    # No geo cache (e.g. tests): the stored value if it's a known region, else the hardcoded default.
    if configured and configured in known_regions_for_provider(provider_key):
        return configured
    return default_region_for_provider(provider_key)


def _build_region_form_context(
    minds_config: MindsConfig | None,
    geo_cache: GeoLocationCache | None,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Build the per-launch-mode region options + pre-selected default for the create form.

    Keyed by ``LaunchMode`` *value* (``IMBUE_CLOUD`` / ``VULTR`` / ``AWS``) so
    the form JS can look options up directly by the compute-provider dropdown's
    value.
    """
    options_by_launch_mode: dict[str, list[str]] = {}
    selected_by_launch_mode: dict[str, str] = {}
    for launch_mode, provider_key in (
        (LaunchMode.IMBUE_CLOUD, IMBUE_CLOUD_PROVIDER_KEY),
        (LaunchMode.VULTR, VULTR_PROVIDER_KEY),
        (LaunchMode.AWS, AWS_PROVIDER_KEY),
    ):
        options_by_launch_mode[launch_mode.value] = list(known_regions_for_provider(provider_key))
        selected_by_launch_mode[launch_mode.value] = _default_region_for_provider_with_config(
            provider_key, minds_config, geo_cache
        )
    return options_by_launch_mode, selected_by_launch_mode


def _resolve_effective_region(
    launch_mode: LaunchMode,
    submitted_region: str,
    minds_config: MindsConfig | None,
    geo_cache: GeoLocationCache | None,
) -> str:
    """Resolve the region to actually create in for a submitted create request.

    Honors the user's submitted value when it's a known region for the provider;
    otherwise falls back to the same default precedence the form uses. Returns
    "" for region-less providers (DOCKER / LIMA).
    """
    provider_key = _region_provider_key_for_launch_mode(launch_mode)
    if provider_key is None:
        return ""
    if submitted_region and submitted_region in known_regions_for_provider(provider_key):
        return submitted_region
    return _default_region_for_provider_with_config(provider_key, minds_config, geo_cache)


def _persist_region_for_launch_mode(
    minds_config: MindsConfig | None,
    launch_mode: LaunchMode,
    region: str,
) -> None:
    """Persist the chosen region as the provider's new last-used default. Best-effort."""
    provider_key = _region_provider_key_for_launch_mode(launch_mode)
    if minds_config is None or provider_key is None or not region:
        return
    # Best-effort: this runs inside the ``on_created`` callback, which the agent
    # creator invokes inside a try/except that marks the create FAILED on any
    # raised exception. A region-persist failure must never flip an
    # already-successful create. ``set_region`` -> ``_write_raw`` can raise a bare
    # ``OSError`` (disk full / permission) in addition to ``MindsConfigError``, so
    # swallow both at debug level.
    try:
        minds_config.set_region(provider_key, region)
    except (MindsConfigError, OSError) as exc:
        logger.debug("Failed to persist region {} for provider {}: {}", region, provider_key, exc)


def _handle_backup_status_api() -> Response:
    """Return per-project backup status (GET /api/backup-status).

    Queries restic (from the minds machine) for every known workspace using
    its canonical restic.env, in parallel with a per-workspace timeout. The
    landing page fetches this once on load to fill each tile.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    backend_resolver = get_state().backend_resolver
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(content="{}", media_type="application/json")
    root_concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    agent_ids = backend_resolver.list_active_workspace_ids()
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
    return make_response(content=json.dumps(payload), media_type="application/json")


def _handle_backup_export_api(
    agent_id: str,
) -> Response:
    """Build + stream a zip of the workspace's latest snapshot (GET /api/backup-export/{agent_id}).

    Produces the zip on the minds machine via ``restic dump --archive zip`` to a
    /tmp file keyed by host id (so re-exports overwrite), then returns it.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    backend_resolver = get_state().backend_resolver
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(
            status_code=404, content='{"error": "No backups configured"}', media_type="application/json"
        )
    try:
        typed_agent_id = AgentId(agent_id)
    except ValueError:
        return make_response(status_code=400, content='{"error": "Invalid agent id"}', media_type="application/json")
    display_info = backend_resolver.get_agent_display_info(typed_agent_id)
    # The zip file is keyed by host id (per the export contract); fall back to the
    # agent id only if discovery has no display info for this agent.
    host_id = display_info.host_id if display_info is not None else agent_id
    download_label = display_info.agent_name if display_info is not None else agent_id
    root_concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    try:
        zip_path = export_latest_snapshot_zip(
            paths=paths, agent_id=typed_agent_id, host_id=host_id, parent_cg=root_concurrency_group
        )
    except BackupProvisioningError as e:
        logger.warning("Backup export failed for {}: {}", agent_id, e)
        return make_response(status_code=500, content=json.dumps({"error": str(e)}), media_type="application/json")
    return make_file_response(
        path=str(zip_path), media_type="application/zip", filename=f"{download_label}-backup.zip"
    )


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
    account_id: str,
) -> _OnCreatedCallbackFactory | None:
    """Build a callback that injects the tunnel token after agent creation.

    Returns None if no account is selected (nothing to inject).
    """
    if not account_id:
        return None

    session_store: MultiAccountSessionStore | None = get_state().session_store
    imbue_cloud_cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    root_concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    notification_dispatcher: NotificationDispatcher | None = get_state().notification_dispatcher
    backend_resolver: BackendResolverInterface = get_state().backend_resolver

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


# Where the create page sends a user who chose the remote (Imbue Cloud)
# preset without any signed-in account: into the sign-up/sign-in flow with a
# link back to the picker. ``return_to=%2Fcreate`` is the URL-encoded
# ``/create`` -- the client builds the same target via
# ``encodeURIComponent('/create')`` in Create.jinja, and ``_handle_auth_page``
# only honors a safe same-origin path.
_REMOTE_SIGNIN_REDIRECT_URL: Final[str] = "/auth/signup?return_to=%2Fcreate"


def _handle_create_form_submit() -> Response:
    """Handle form submission to create a new agent."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    form = request.form
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
    color = _color_for_new_workspace(form.get("color", ""))
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
    submitted_region = str(form.get("region", "")).strip()

    session_store_inst: MultiAccountSessionStore | None = get_state().session_store
    minds_config: MindsConfig | None = get_state().minds_config
    geo_cache: GeoLocationCache | None = get_state().geo_location_cache

    def _re_render_with_error(message: str, status: int = 400) -> Response:
        accounts_list = session_store_inst.list_accounts() if session_store_inst else []
        region_options, region_selected = _build_region_form_context(minds_config, geo_cache)
        # Re-render with the user's submitted account_id pre-selected
        # (including "" -> "No account") rather than the config default,
        # so a validation error doesn't silently revert their choice.
        html_body = render_create_form(
            git_url=git_url,
            branch=branch,
            host_name=host_name,
            launch_mode=launch_mode,
            ai_provider=ai_provider,
            accounts=accounts_list,
            region_options_by_launch_mode=region_options,
            region_selected_by_launch_mode=region_selected,
            default_account_id=account_id,
            anthropic_api_key=anthropic_api_key,
            backup_provider=backup_provider,
            backup_encryption_method=backup_encryption_method,
            backup_api_key_env=backup_api_key_env,
            has_saved_backup_password=has_saved_backup_password(agent_creator.paths),
            error_message=message,
            # Errors are about advanced-view fields (repository, providers,
            # backup), so open that view directly rather than the cards.
            start_advanced=True,
            color=color,
        )
        return make_html_response(content=html_body, status_code=status)

    if not git_url:
        return _re_render_with_error("Repository URL is required.")

    # The workspace name is chosen automatically unless the user typed one: a
    # submitted value (from the advanced view's optional "Name" field, empty in
    # the common simple flow), else the operator ``MINDS_WORKSPACE_NAME``
    # override, else the next free ``mind-N`` name (computed from the host names
    # already in use across every provider). Resolve it eagerly so an invalid
    # name surfaces inline rather than as a deferred "FAILED" status on the
    # creating page.
    try:
        resolved_host_name = resolve_create_host_name(
            host_name, _existing_workspace_host_names(get_state().backend_resolver)
        )
    except InvalidName as exc:
        return _re_render_with_error(str(exc))

    is_imbue_cloud_compute = launch_mode is LaunchMode.IMBUE_CLOUD
    is_imbue_cloud_ai = ai_provider is AIProvider.IMBUE_CLOUD
    if not account_id and (is_imbue_cloud_compute or is_imbue_cloud_ai):
        # The remote (Imbue Cloud) presets require an account. With no account
        # at all, the compute path is unusable, so route into the sign-in/up
        # flow (the client does this on card-select; this is the no-JS
        # backstop) with a link back to the picker. When accounts exist but
        # none is selected, re-render asking the user to pick one.
        has_any_account = bool(session_store_inst.list_accounts()) if session_store_inst is not None else False
        if not has_any_account:
            return make_response(status_code=303, headers={"Location": _REMOTE_SIGNIN_REDIRECT_URL})
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

    # Resolve the explicit region the user chose (or the resolved default) and,
    # on a successful create, persist it as the provider's new last-used default.
    region = _resolve_effective_region(launch_mode, submitted_region, minds_config, geo_cache)

    # Build a post-creation callback that injects the tunnel token, then also
    # persists the chosen region (fires only after a successful create).
    base_on_created = _build_on_created_callback(account_id)

    def on_created(agent_id: AgentId) -> None:
        if base_on_created is not None:
            base_on_created(agent_id)
        _persist_region_for_launch_mode(minds_config, launch_mode, region)

    # ``start_creation`` returns a CreationId (minds-internal handle for
    # tracking the in-flight create) -- the canonical AgentId only exists
    # after ``mngr create`` returns. Workspace<->account association is now
    # done from the on_created callback (which fires post-canonical-id) so
    # the association is keyed under the right id.
    creation_id = agent_creator.start_creation(
        git_url,
        host_name=resolved_host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        branch_or_tag=branch_or_tag,
        region=region,
        anthropic_api_key=anthropic_api_key,
        on_created=on_created,
        backup_request=backup_request,
        color=color,
    )

    creating_url = "/creating/{}".format(creation_id)
    return make_response(status_code=303, headers={"Location": creating_url})


def _handle_create_page() -> Response:
    """Show the create form page (GET /create)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    backend_resolver = get_state().backend_resolver
    git_url = request.args.get("git_url", "")
    branch = request.args.get("branch", "")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    minds_config: MindsConfig | None = get_state().minds_config
    agent_creator: AgentCreator | None = get_state().agent_creator
    geo_cache: GeoLocationCache | None = get_state().geo_location_cache
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    is_backup_password_saved = has_saved_backup_password(agent_creator.paths) if agent_creator is not None else False
    region_options, region_selected = _build_region_form_context(minds_config, geo_cache)
    html = render_create_form(
        git_url=git_url,
        branch=branch,
        accounts=accounts,
        default_account_id=default_account_id or "",
        has_saved_backup_password=is_backup_password_saved,
        region_options_by_launch_mode=region_options,
        region_selected_by_launch_mode=region_selected,
        # A deep-link that pre-fills a repo/branch wants those advanced fields
        # visible; otherwise start on the simple preset cards.
        start_advanced=bool(git_url or branch),
        color=_suggested_create_color(backend_resolver),
    )
    return make_html_response(content=html)


def _handle_create_agent_api() -> Response:
    """API endpoint for creating an agent (POST /api/create-agent).

    Accepts JSON body with git_url. Returns JSON with agent_id and status.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    try:
        body = _read_json_body()
    except (json.JSONDecodeError, ValueError):
        return make_response(
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
        return make_response(
            status_code=400,
            content='{"error": "Invalid launch_mode"}',
            media_type="application/json",
        )
    try:
        ai_provider = AIProvider(str(body.get("ai_provider", AIProvider.SUBSCRIPTION.value)))
    except ValueError:
        return make_response(
            status_code=400,
            content='{"error": "Invalid ai_provider"}',
            media_type="application/json",
        )
    try:
        backup_provider = BackupProvider(str(body.get("backup_provider", BackupProvider.CONFIGURE_LATER.value)))
    except ValueError:
        return make_response(
            status_code=400,
            content='{"error": "Invalid backup_provider"}',
            media_type="application/json",
        )
    try:
        backup_encryption_method = BackupEncryptionMethod(
            str(body.get("backup_encryption_method", BackupEncryptionMethod.NO_PASSWORD.value))
        )
    except ValueError:
        return make_response(
            status_code=400,
            content='{"error": "Invalid backup_encryption_method"}',
            media_type="application/json",
        )
    backup_master_password = str(body.get("backup_master_password", ""))
    is_save_backup_password = bool(body.get("backup_save_password", False))
    backup_api_key_env = str(body.get("backup_api_key_env", ""))
    anthropic_api_key = str(body.get("anthropic_api_key", "")).strip()
    account_id = str(body.get("account_id", "")).strip()
    submitted_region = str(body.get("region", "")).strip()
    color = _color_for_new_workspace(body.get("color", ""))
    if not git_url:
        return make_response(
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
            return make_response(
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
        return make_response(
            status_code=400,
            content='{"error": "account_id is required when launch_mode or ai_provider is IMBUE_CLOUD"}',
            media_type="application/json",
        )
    if ai_provider is AIProvider.API_KEY and not anthropic_api_key:
        return make_response(
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
        session_store_inst: MultiAccountSessionStore | None = get_state().session_store
        if session_store_inst is not None:
            account_email = session_store_inst.get_account_email(account_id) or ""

    # The form path (``_handle_create_form_submit``) auto-generates a unique
    # ``mind-N`` name via ``resolve_create_host_name``, so it cannot collide.
    # This JSON API still lets a caller pass an explicit ``host_name``; reject a
    # duplicate of an existing workspace inline with a 409. (A footgun this 409
    # does not cover: an API caller passing an empty ``host_name`` gets a name
    # auto-derived from the repo via ``extract_repo_name``, which a second POST
    # with the same ``git_url`` would duplicate, failing as a deferred ``FAILED``
    # status mid-creation rather than a 409 here.)
    if host_name:
        backend_resolver = get_state().backend_resolver
        existing_names = _existing_workspace_host_names(backend_resolver)
        if host_name in existing_names:
            return make_response(
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
        return make_response(
            status_code=400,
            content=json.dumps({"error": backup_error}),
            media_type="application/json",
        )

    # Resolve the explicit region (or its default) and persist it on success.
    minds_config: MindsConfig | None = get_state().minds_config
    geo_cache: GeoLocationCache | None = get_state().geo_location_cache
    region = _resolve_effective_region(launch_mode, submitted_region, minds_config, geo_cache)

    def _persist_region_on_created(agent_id: AgentId) -> None:
        _persist_region_for_launch_mode(minds_config, launch_mode, region)

    creation_id = agent_creator.start_creation(
        git_url,
        host_name=host_name,
        branch=branch,
        launch_mode=launch_mode,
        ai_provider=ai_provider,
        account_email=account_email,
        region=region,
        anthropic_api_key=anthropic_api_key,
        on_created=_persist_region_on_created,
        backup_request=backup_request,
        color=color,
    )

    # API contract: the JSON field stays named ``agent_id`` for backwards
    # compatibility with existing API clients, but the value is now a
    # CreationId (minds-internal in-flight handle, distinct prefix from a
    # canonical AgentId). The status-polling endpoints accept either.
    return make_response(
        content=json.dumps({"agent_id": str(creation_id), "status": str(AgentCreationStatus.INITIALIZING)}),
        media_type="application/json",
    )


def _handle_creation_status_api(
    agent_id: str,
) -> Response:
    """API endpoint for checking agent creation status."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    # The URL parameter is named ``agent_id`` for legacy API compatibility
    # but it actually carries a ``CreationId`` (minds-internal in-flight
    # handle). The canonical mngr ``AgentId`` is reported back through
    # ``info.agent_id`` once ``mngr create`` returns.
    creation_id = CreationId(agent_id)
    info = agent_creator.get_creation_info(creation_id)
    if info is None:
        return make_response(
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
    return make_response(content=json.dumps(result), media_type="application/json")


def _handle_creating_page(
    agent_id: str,
) -> Response:
    """Show the creating/loading page (GET /creating/{agent_id}).

    The page shows the setting-up progress screen while the workspace is
    created in the background, then redirects into the workspace once
    creation finishes. The status-polling / SSE endpoints are keyed by the
    same ``creation_id`` carried in the route.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    # ``agent_id`` route param is actually a CreationId (see comment in
    # ``_handle_creation_status_api``).
    creation_id = CreationId(agent_id)
    info = agent_creator.get_creation_info(creation_id)
    if info is None:
        return make_response(status_code=404, content="Unknown agent creation")

    html = render_creating_page(creation_id=creation_id, info=info)
    return make_html_response(content=html)


def _stream_creation_logs(
    log_queue: queue.Queue[str],
    agent_creator: AgentCreator,
    creation_id: CreationId,
    shutdown_event: threading.Event,
) -> Iterator[str]:
    """Generator that yields SSE events from a creation log queue.

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
            line = log_queue.get(block=True, timeout=1.0)
        except queue.Empty:
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


def _handle_creation_logs_sse(
    agent_id: str,
) -> Response:
    """SSE endpoint that streams creation logs for an agent."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    # ``agent_id`` route param carries a CreationId (see comment in
    # ``_handle_creation_status_api``).
    creation_id = CreationId(agent_id)
    log_queue = agent_creator.get_log_queue(creation_id)
    if log_queue is None:
        return make_response(status_code=404, content="Unknown agent creation")

    return make_streaming_response(
        _stream_creation_logs(log_queue, agent_creator, creation_id, get_state().shutdown_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# -- Agent destruction route handlers --


def _resolve_destroying_for_landing(
    paths: WorkspacePaths | None,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> dict[str, str]:
    """Walk ``<paths.data_dir>/destroying/``, finalize DONE records, return marker map.

    Returns ``{agent_id_str: "running" | "failed"}`` for any in-flight or
    failed destroy. A destroy is DONE only once the whole *host* is gone (not
    just the workspace agent -- see :func:`_host_still_active`); on DONE we
    disassociate the workspace from its account and delete the record, so the
    row vanishes on the next refresh. A FAILED destroy stays associated and
    visible so the user can retry rather than being left with an invisible,
    still-running host.

    Returns an empty dict (and does no work) when ``paths`` is None --
    that path is exercised by tests that build a minimal app without
    a real data dir.
    """
    if paths is None:
        return {}
    records = list_destroying(paths, lambda aid: _host_still_active(backend_resolver, paths, aid))
    marker: dict[str, str] = {}
    for agent_id, record in records.items():
        if record.status == DestroyingStatus.DONE:
            _finalize_destroyed_workspace(agent_id, paths, session_store)
            continue
        marker[str(agent_id)] = "running" if record.status == DestroyingStatus.RUNNING else "failed"
    return marker


def _finalize_destroyed_workspace(
    agent_id: AgentId,
    paths: WorkspacePaths,
    session_store: MultiAccountSessionStore | None,
) -> None:
    """Disassociate a fully-destroyed workspace from its account, then delete its record.

    Runs only once the host is confirmed gone (DONE). Disassociating here --
    rather than synchronously when the user clicks destroy -- means a failed or
    partial teardown keeps the workspace visible instead of hiding a host that
    is still running.
    """
    if session_store is not None:
        account = session_store.get_account_for_workspace(str(agent_id))
        if account is not None:
            session_store.disassociate_workspace(str(account.user_id), str(agent_id))
    delete_destroying(agent_id, paths)


def _host_still_active(
    backend_resolver: BackendResolverInterface,
    paths: WorkspacePaths | None,
    agent_id: AgentId,
) -> bool:
    """Whether the workspace's host is still up (not just the workspace agent).

    True if the workspace agent is still in ``list_active_workspace_ids()`` OR
    its recorded host has not yet reached ``DESTROYED``. A detached destroy is
    only DONE once this is False -- otherwise a destroy that tore down only the
    workspace agent while ``system-services`` kept the host alive would falsely
    read as DONE. See ``destroying.read_destroying``.
    """
    if agent_id in backend_resolver.list_active_workspace_ids():
        return True
    if paths is None:
        return False
    host_id = read_host_id(agent_id, paths)
    if host_id is None:
        return False
    state = backend_resolver.get_host_state(host_id)
    return state is not None and state is not HostState.DESTROYED


def _is_host_still_active(agent_id: AgentId) -> bool:
    """Request-scoped wrapper around :func:`_host_still_active`."""
    return _host_still_active(
        get_state().backend_resolver,
        get_state().api_v1_paths,
        agent_id,
    )


def _handle_destroy_agent_api(
    agent_id: str,
) -> Response:
    """POST /api/destroy-agent/<agent_id>: spawn a detached destroy.

    Resolves the workspace's host id from the in-memory backend resolver (host
    id is immutable and already known for any workspace the user can see), then
    spawns a detached destroy that tears down the *whole host*. Refuses with 409
    if the host id can't be resolved -- rather than half-destroying or hiding a
    host that is still running.

    The workspace is *not* disassociated here: that happens only once the host
    is confirmed gone (see :func:`_finalize_destroyed_workspace`), so a failed
    teardown stays visible and retryable instead of becoming an invisible,
    still-billing orphan.

    Idempotent: if a destroy is already running for this agent, returns
    200 with the existing record's status. Otherwise spawns the
    detached subprocess and returns 202.

    Returns ``redirect_url: "/"`` on success so the settings-page JS can
    navigate to the landing page (where the destroying marker is visible).
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(
            status_code=501, content='{"error": "Destroy not configured"}', media_type="application/json"
        )

    parsed_id = AgentId(agent_id)
    backend_resolver: BackendResolverInterface = get_state().backend_resolver

    # Idempotent: short-circuit if a destroy is already running.
    existing = read_destroying(parsed_id, paths, is_host_still_active=_is_host_still_active(parsed_id))
    if existing is not None and existing.status == DestroyingStatus.RUNNING:
        return make_response(
            status_code=200,
            content=json.dumps({"agent_id": agent_id, "status": "running", "redirect_url": "/"}),
            media_type="application/json",
        )

    # Resolve the immutable host id from discovery; a minds workspace teardown
    # is a host teardown, and there is no safe single-agent fallback. If we
    # can't determine the host, refuse rather than risk a partial destroy.
    host_id = _resolve_host_id(backend_resolver, parsed_id)
    if host_id is None:
        logger.warning("Refusing to destroy {}: could not resolve its host id from discovery", agent_id)
        return make_response(
            status_code=409,
            content=json.dumps(
                {"error": "Could not determine the workspace's host yet. Please wait a moment and try again."}
            ),
            media_type="application/json",
        )
    start_destroy(parsed_id, paths, host_id)

    return make_response(
        status_code=202,
        content=json.dumps({"agent_id": agent_id, "status": "running", "redirect_url": "/"}),
        media_type="application/json",
    )


def _handle_destroying_status_api(
    agent_id: str,
) -> Response:
    """GET /api/destroying/<agent_id>/status: live status of a destroy."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    parsed_id = AgentId(agent_id)
    record = read_destroying(parsed_id, paths, is_host_still_active=_is_host_still_active(parsed_id))
    if record is None:
        return make_response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    return make_response(
        content=json.dumps(
            {
                "agent_id": agent_id,
                "pid": record.pid,
                "pid_alive": record.pid_alive,
                "is_host_still_active": record.is_host_still_active,
                "status": str(record.status).lower(),
            }
        ),
        media_type="application/json",
    )


def _handle_destroying_log_api(
    agent_id: str,
) -> Response:
    """GET /api/destroying/<agent_id>/log?after=<bytes>: tail the destroy log."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    parsed_id = AgentId(agent_id)
    after_str = request.args.get("after", "0")
    try:
        after = max(int(after_str), 0)
    except ValueError:
        after = 0
    try:
        content_bytes, next_offset = read_log_chunk(parsed_id, paths, after)
    except FileNotFoundError:
        return make_response(status_code=404, content='{"error": "No record"}', media_type="application/json")
    return make_response(
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
) -> Response:
    """POST /api/destroying/<agent_id>/dismiss: remove the destroy record."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(status_code=200, content="{}", media_type="application/json")
    parsed_id = AgentId(agent_id)
    delete_destroying(parsed_id, paths)
    return make_response(status_code=200, content="{}", media_type="application/json")


def _handle_destroying_page(
    agent_id: str,
) -> Response:
    """GET /destroying/<agent_id>: the destroy detail / log-tail page."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    backend_resolver = get_state().backend_resolver
    paths: WorkspacePaths | None = get_state().api_v1_paths
    if paths is None:
        return make_response(status_code=404, content="No record")
    parsed_id = AgentId(agent_id)
    record = read_destroying(
        parsed_id, paths, is_host_still_active=_host_still_active(backend_resolver, paths, parsed_id)
    )
    if record is None:
        return make_response(status_code=404, content="No record")
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
    return make_html_response(content=html)


# -- Workspace color route handler --


def _handle_set_workspace_color_api(
    agent_id: str,
) -> Response:
    """POST /api/workspaces/<agent_id>/color: write the per-workspace color label.

    Body: ``{"hex": "<rrggbb>"}``. Lenient: accepts ``#fff`` / ``fff`` /
    ``#ffffff`` / ``ffffff`` in any case; normalized to ``#rrggbb`` lowercase
    server-side.

    Error responses (all JSON with an ``error`` discriminant):
      - 400 ``invalid_hex`` -- the body's hex didn't parse.
      - 404 ``not_primary`` -- the agent is not a primary workspace
        (no ``workspace`` / ``is_primary`` label pair, or unknown).
      - 409 ``stale_provider`` -- the agent's provider's last discovery
        poll errored, so the host is unreachable and writing the label
        would not be observable until provider recovery.
      - 502 ``host_unreachable`` -- ``mngr label`` itself failed (timeout,
        non-zero exit, exec failure).

    On success, writes ``color=<hex>`` via ``mngr label`` (CLI merge
    semantics, so other labels are preserved), optimistically updates
    the resolver's snapshot so the next SSE workspaces tick reflects the
    new color without waiting for the discovery refresh, and returns
    ``{"agent_id": ..., "color": "#rrggbb"}``.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    try:
        body = _read_json_body()
    except (json.JSONDecodeError, ValueError) as exc:
        # ValueError also covers UnicodeDecodeError on a non-UTF-8 body
        # (matches the other request.json() call sites in this file).
        # External (HTTP) input: log at warning level rather than silently
        # swallowing so a buggy / hostile client's bad bodies are visible.
        logger.warning("Color write for {} got malformed JSON body: {}", agent_id, exc)
        return make_response(
            status_code=400,
            content=json.dumps({"error": "invalid_hex"}),
            media_type="application/json",
        )
    raw_hex = body.get("hex", "") if isinstance(body, dict) else ""
    normalized = normalize_workspace_color(str(raw_hex))
    if normalized is None:
        return make_response(
            status_code=400,
            content=json.dumps({"error": "invalid_hex"}),
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)
    backend_resolver: BackendResolverInterface = get_state().backend_resolver

    # The minds primary-workspace filter is the "workspace" + "is_primary"
    # label pair (see backend_resolver.list_known_workspace_ids). Color writes
    # only apply to primary agents; the sibling system-services agent shares
    # the host but does not own workspace identity.
    if parsed_id not in backend_resolver.list_known_workspace_ids():
        return make_response(
            status_code=404,
            content=json.dumps({"error": "not_primary"}),
            media_type="application/json",
        )

    info = backend_resolver.get_agent_display_info(parsed_id)
    errored_provider_names = {str(name) for name in backend_resolver.get_provider_errors()}
    if _is_workspace_provider_errored(info, errored_provider_names):
        return make_response(
            status_code=409,
            content=json.dumps({"error": "stale_provider"}),
            media_type="application/json",
        )

    mngr_binary: str = get_state().mngr_binary
    mngr_host_dir: Path = get_state().mngr_host_dir
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    if concurrency_group is None:
        # The concurrency group is wired in production (see create_desktop_client
        # entrypoint); only test paths that explicitly skip it can hit this.
        logger.warning("No concurrency group available; cannot write color label for {}", parsed_id)
        return make_response(
            status_code=502,
            content=json.dumps({"error": "host_unreachable", "detail": "concurrency group unavailable"}),
            media_type="application/json",
        )

    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    # `mngr label` (CLI) merges with existing labels; BaseAgent.set_labels at
    # the API level would full-replace and clobber concurrent writes to other
    # keys, so we shell out to the CLI to get the merge for free.
    argv = [mngr_binary, "label", str(parsed_id), "-l", f"color={normalized}"]
    try:
        # Run ``mngr label`` to completion on the root concurrency group so the
        # subprocess plumbing (launch, timeout, capture) lives in one place and
        # the handler returns the real outcome of the label write.
        _run_mngr(concurrency_group, argv, env)
    except MngrCommandError as exc:
        logger.warning("mngr label failed for {}: {}", parsed_id, exc)
        return make_response(
            status_code=502,
            content=json.dumps({"error": "host_unreachable", "detail": str(exc)}),
            media_type="application/json",
        )

    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.set_workspace_color_locally(parsed_id, normalized)

    return make_response(
        status_code=200,
        content=json.dumps({"agent_id": agent_id, "color": normalized}),
        media_type="application/json",
    )


# -- Telegram setup route handlers --


def _handle_telegram_setup(
    agent_id: str,
) -> Response:
    """Start Telegram bot setup for an agent (POST /api/agents/{agent_id}/telegram/setup)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = get_state().telegram_orchestrator
    if telegram_orchestrator is None:
        return make_response(
            status_code=501,
            content='{"error": "Telegram setup not configured"}',
            media_type="application/json",
        )

    parsed_id = AgentId(agent_id)

    # Use agent_id as the agent name for bot naming (best we have without additional lookups)
    agent_name = str(parsed_id)[:8]
    try:
        body = _read_json_body()
        agent_name = str(body.get("agent_name", agent_name)).strip() or agent_name
    except (json.JSONDecodeError, ValueError):
        pass

    telegram_orchestrator.start_setup(agent_id=parsed_id, agent_name=agent_name)
    return make_response(
        content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.CHECKING_CREDENTIALS)}),
        media_type="application/json",
    )


def _handle_telegram_status(
    agent_id: str,
) -> Response:
    """Get Telegram setup status for an agent (GET /api/agents/{agent_id}/telegram/status)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")

    telegram_orchestrator: TelegramSetupOrchestrator | None = get_state().telegram_orchestrator
    if telegram_orchestrator is None:
        return make_response(
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
            return make_response(
                content=json.dumps({"agent_id": str(parsed_id), "status": str(TelegramSetupStatus.DONE)}),
                media_type="application/json",
            )
        return make_response(
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
    return make_response(content=json.dumps(result), media_type="application/json")


# -- Providers panel toggle route --


def _handle_provider_toggle(
    provider_name: str,
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
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    try:
        body = _read_json_body()
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Provider toggle request body was not valid JSON: {}", e)
        return make_response(status_code=400, content='{"error": "Body must be JSON"}', media_type="application/json")
    # request.json() can return any JSON value (array, string, number, null, ...),
    # not just objects. Reject non-dict bodies before calling .get() so we return
    # a structured 400 rather than a 500 from an AttributeError.
    if not isinstance(body, dict):
        return make_response(
            status_code=400,
            content='{"error": "Body must be a JSON object"}',
            media_type="application/json",
        )
    is_enabled = body.get("is_enabled")
    if not isinstance(is_enabled, bool):
        return make_response(
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
        bounce_latchkey_forward_supervisor(get_state().latchkey_forward_supervisor)
    return make_response(
        content=json.dumps({"provider_name": provider_name, "is_enabled": is_enabled, "changed": changed}),
        media_type="application/json",
    )


# -- Chrome (persistent shell) route handlers --


def _handle_chrome_page() -> Response:
    """Serve the persistent chrome page (title bar + sidebar + content iframe).

    This route is unauthenticated -- the chrome renders for all users. The sidebar
    shows an empty state for unauthenticated users; the SSE stream populates it
    after authentication.
    """
    is_mac = _get_is_mac()

    authenticated = _is_request_authenticated()
    backend_resolver = get_state().backend_resolver
    initial_workspaces = _build_workspace_list(backend_resolver) if authenticated else []

    html = render_chrome_page(
        is_mac=is_mac,
        is_authenticated=authenticated,
        mngr_forward_origin=_get_mngr_forward_origin(),
        initial_workspaces=initial_workspaces,
    )
    return make_html_response(content=html)


def _handle_chrome_sidebar() -> Response:
    """Serve the standalone sidebar page loaded into the shared modal WebContentsView.

    Position params (``trigger_x`` / ``trigger_y`` / ``trigger_w`` / ``trigger_h``
    / ``offset_x`` / ``offset_y``) come from the caller (chrome.js packs the
    sidebar-toggle button's getBoundingClientRect + a caller-chosen offset into
    the URL). Missing or unparseable params fall back to render_sidebar_page's
    defaults (a 38px-tall element at the top-left of the window, nudged 2px
    left and 2px below it).
    """
    html = render_sidebar_page(
        mngr_forward_origin=_get_mngr_forward_origin(),
        trigger_x=_int_query_param("trigger_x", 0),
        trigger_y=_int_query_param("trigger_y", 0),
        trigger_w=_int_query_param("trigger_w", 0),
        trigger_h=_int_query_param("trigger_h", 38),
        offset_x=_int_query_param("offset_x", -2),
        offset_y=_int_query_param("offset_y", 2),
    )
    return make_html_response(content=html)


def _handle_dev_styleguide() -> Response:
    """Render the design-system styleguide page."""
    return make_html_response(content=render_dev_styleguide_page())


# How often the chrome-events stream re-asserts the current non-HEALTHY
# system-interface statuses, on top of the one-shot connect-time snapshot and
# the per-transition pushes. This is a self-healing backstop: a chrome renderer
# that lost its in-memory health state (e.g. a reloaded webview whose one-shot
# ``system_interface_status`` was never replayed) re-learns a still-stuck
# workspace within this interval and can finally redirect to the recovery page,
# even though the tracker emitted no fresh transition. Re-asserting ``stuck`` is
# idempotent client-side (the recovery-redirect lock prevents re-navigation), so
# the only cost is one tiny event per non-healthy agent per interval.
_SYSTEM_INTERFACE_STATUS_REASSERT_INTERVAL_SECONDS: Final[float] = 15.0


def _handle_chrome_events() -> Response:
    """SSE endpoint that streams workspace list and auth status changes to the chrome.

    The chrome subscribes to this on load. If unauthenticated, sends an auth_required
    event. Once authenticated, sends the current workspace list and pushes updates
    whenever the backend resolver's data changes (driven by MngrStreamManager's
    discovery and events streams).
    """
    authenticated = _is_request_authenticated()
    backend_resolver = get_state().backend_resolver

    def _event_generator() -> Iterator[str]:
        if not authenticated:
            yield "data: {}\n\n".format(json.dumps({"type": "auth_required"}))
            return

        # Wake up when the resolver's data changes. The resolver fires callbacks
        # from background threads; a ``threading.Event`` is set directly from
        # those threads (set() is thread-safe), no event loop needed.
        change_event = threading.Event()

        # Health transitions from the system-interface tracker arrive on
        # background threads (envelope reader, probe loop, restart endpoint).
        # We accumulate them into a per-connection queue and drain them
        # in the main generator loop so each subscriber sees every event.
        health_queue: queue.Queue[tuple[str, AgentHealth]] = queue.Queue()

        def _on_change() -> None:
            change_event.set()

        def _on_health_change(agent_id: AgentId, status: AgentHealth) -> None:
            _enqueue_health_change(health_queue, change_event, agent_id, status)

        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.add_on_change_callback(_on_change)

        tracker: SystemInterfaceHealthTracker | None = get_state().system_interface_health_tracker
        if tracker is not None:
            tracker.add_on_change_callback(_on_health_change)

        # The watchdog's no-arg on-change reuses the same wake as the resolver;
        # the loop re-reads its tier each tick (like providers_state) and emits
        # only the terminal BLOCKED transition, once.
        discovery_watchdog: DiscoveryHealthWatchdog | None = get_state().discovery_health_watchdog
        if discovery_watchdog is not None:
            discovery_watchdog.add_on_change_callback(_on_change)
        discovery_blocked_emitted = False

        # Local-mind liveness is derived from discovery host state, which the
        # resolver already wakes us on (the on-change above). A Start/Stop action
        # sets an optimistic override on the same resolver and fires that same
        # on-change, so an in-app action wakes this loop immediately too; each
        # tick recomputes and diffs the per-mind states.
        try:
            # Send initial workspace list and request count
            session_store: MultiAccountSessionStore | None = get_state().session_store
            paths: WorkspacePaths | None = get_state().api_v1_paths
            last_workspace_data = _build_workspace_list(backend_resolver, session_store)
            last_destroying_ids = _destroying_agent_ids(paths, backend_resolver)
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
            inbox: RequestInbox | None = get_state().request_inbox
            last_requests_payload = _build_requests_payload(inbox, backend_resolver)
            # ``auto_open`` is bundled with the requests payload (rather than
            # its own SSE event) so the Electron shell sees both atomically
            # when deciding whether to auto-open the panel.
            minds_config: MindsConfig | None = get_state().minds_config
            auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
            yield "data: {}\n\n".format(
                json.dumps({"type": "requests", **last_requests_payload, "auto_open": auto_open})
            )

            # Agents for which a STUCK redirect has already been emitted on this
            # connection, so the per-wake flip check below emits each stuck episode
            # exactly once (the 15s re-assert still re-delivers for a chrome that
            # lost the one-shot). An agent is dropped from the set when it leaves
            # STUCK so a later re-STUCK re-promotes.
            redirected_agent_ids: set[str] = set()
            if tracker is not None:
                for aid, status in tracker.snapshot_all().items():
                    if not _should_emit_system_interface_status(backend_resolver, tracker, aid, status):
                        continue
                    if status == AgentHealth.STUCK:
                        redirected_agent_ids.add(str(aid))
                    yield "data: {}\n\n".format(
                        json.dumps(_system_interface_status_payload(tracker, str(aid), status))
                    )
            # Replay the app-global discovery-pipeline state if it is already
            # BLOCKED, so a freshly (re)loaded chrome re-learns it and takes over
            # the whole app. BLOCKED is terminal, so a single connect-time emit
            # plus the on-change push below is sufficient.
            if discovery_watchdog is not None and discovery_watchdog.get_health() is DiscoveryHealth.BLOCKED:
                yield "data: {}\n\n".format(json.dumps(_discovery_health_payload(DiscoveryHealth.BLOCKED)))
                discovery_blocked_emitted = True
            # Anchor the periodic re-assert clock to the connect-time snapshot
            # just sent, so the first backstop re-assert is a full interval out.
            last_status_reassert = time.monotonic()

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
            # bottom-of-loop clear, leaving the queued item idle until the
            # next idle-wake timeout -- a UX regression for health-state
            # transitions like RESTARTING -> HEALTHY.
            shutdown_event: threading.Event = get_state().shutdown_event
            while not shutdown_event.is_set():
                # Wait for a change signal or timeout. The timeout bounds the
                # worst-case re-assert cadence on an otherwise-idle connection
                # (the periodic status backstop below only runs on a loop wake).
                # Cap it at the re-assert interval so a steadily-stuck workspace
                # really is re-asserted on that cadence. A client that has
                # disconnected is detected when the next ``yield`` write fails
                # (WSGI has no proactive disconnect signal); the generator's
                # ``finally`` then removes the callbacks.
                change_event.wait(timeout=_SYSTEM_INTERFACE_STATUS_REASSERT_INTERVAL_SECONDS)
                # Clear BEFORE draining so any producer firing between drain
                # and the next ``wait()`` re-sets the event and is observed
                # promptly. See the comment above for the full invariant.
                change_event.clear()

                # Server-side shutdown signalled (the signal handler sets
                # shutdown_event and pokes the resolver's change callback,
                # which fires change_event). Exit the generator cleanly so
                # the server's drain doesn't have to wait us out.
                if shutdown_event.is_set():
                    break

                while not health_queue.empty():
                    aid_str, status = health_queue.get_nowait()
                    # Leaving STUCK clears the redirect latch so a later re-STUCK
                    # is promoted again by the flip check below.
                    if status != AgentHealth.STUCK:
                        redirected_agent_ids.discard(aid_str)
                    if not _should_emit_system_interface_status(backend_resolver, tracker, AgentId(aid_str), status):
                        continue
                    if status == AgentHealth.STUCK:
                        redirected_agent_ids.add(aid_str)
                    yield "data: {}\n\n".format(json.dumps(_system_interface_status_payload(tracker, aid_str, status)))

                # Promote any STUCK agent whose suppression has just lifted: the
                # STUCK edge fired earlier (and was suppressed because no post-onset
                # snapshot had landed yet), and this wake is the snapshot arriving.
                # Emit immediately rather than waiting for the 15s re-assert below,
                # bounding redirect latency to one discovery poll. The latch keeps
                # this to one emit per stuck episode.
                if tracker is not None:
                    for aid, status in tracker.snapshot_all().items():
                        if status != AgentHealth.STUCK or str(aid) in redirected_agent_ids:
                            continue
                        if not _should_emit_system_interface_status(backend_resolver, tracker, aid, status):
                            continue
                        redirected_agent_ids.add(str(aid))
                        yield "data: {}\n\n".format(
                            json.dumps(_system_interface_status_payload(tracker, str(aid), status))
                        )

                if (
                    discovery_watchdog is not None
                    and not discovery_blocked_emitted
                    and discovery_watchdog.get_health() is DiscoveryHealth.BLOCKED
                ):
                    discovery_blocked_emitted = True
                    yield "data: {}\n\n".format(json.dumps(_discovery_health_payload(DiscoveryHealth.BLOCKED)))

                # Periodic backstop: re-assert the current non-HEALTHY statuses
                # even when no transition fired this tick. The tracker only
                # *transitions* on edges (HEALTHY <-> STUCK/RESTARTING/...), so a
                # workspace that is steadily STUCK emits nothing after its one
                # initial edge. A renderer that lost that one-shot event (e.g. a
                # reloaded chrome webview) would otherwise never re-learn it and
                # never redirect to the recovery page. Re-asserting is idempotent
                # client-side (the recovery-redirect lock prevents re-navigation).
                # Prompt promotion of a suppressed STUCK once a post-onset snapshot
                # lands is handled by the flip check above; this is the slower
                # lost-event backstop.
                now = time.monotonic()
                if (
                    tracker is not None
                    and now - last_status_reassert >= _SYSTEM_INTERFACE_STATUS_REASSERT_INTERVAL_SECONDS
                ):
                    last_status_reassert = now
                    for aid, status in tracker.snapshot_all().items():
                        if not _should_emit_system_interface_status(backend_resolver, tracker, aid, status):
                            continue
                        if status == AgentHealth.STUCK:
                            redirected_agent_ids.add(str(aid))
                        yield "data: {}\n\n".format(
                            json.dumps(_system_interface_status_payload(tracker, str(aid), status))
                        )

                # Each workspace entry carries its mind liveness (derived from
                # discovery host state + any optimistic override), so a liveness
                # change makes ``current_data`` differ and pushes a ``workspaces``
                # update below -- no separate liveness channel needed.
                current_data = _build_workspace_list(backend_resolver, session_store)
                current_destroying_ids = _destroying_agent_ids(paths, backend_resolver)
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

                inbox = get_state().request_inbox
                current_requests_payload = _build_requests_payload(inbox, backend_resolver)
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
            if discovery_watchdog is not None:
                discovery_watchdog.remove_on_change_callback(_on_change)

    return make_streaming_response(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
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


def _destroying_agent_ids(paths: WorkspacePaths | None, backend_resolver: BackendResolverInterface) -> list[str]:
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
    records = list_destroying(paths, lambda aid: _host_still_active(backend_resolver, paths, aid))
    return [str(agent_id) for agent_id, record in records.items() if record.status != DestroyingStatus.DONE]


def _build_workspace_list(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
) -> list[dict[str, str]]:
    """Build a JSON-serializable list of workspaces from the backend resolver.

    Each entry carries an ``accent`` (#rrggbb CSS color) for the chrome and
    sidebar to render. The accent is the workspace's stored ``color`` label
    (set at create time by the create-form picker, or via the settings POST
    endpoint); workspaces that lack the label (i.e. they were created before
    the picker shipped and the user hasn't repicked yet) get the default
    workspace color. The contrasting titlebar foreground is no longer sent --
    the chrome derives it from the accent in pure CSS (``.titlebar-surface``).

    Entries whose provider's latest discovery poll errored carry
    ``is_stale="true"`` so the UI can flag them as
    retained-but-unverified (they remain fully interactive).

    Shutdown-capable minds (those on a provider whose host minds can stop/start,
    see :func:`provider_backend_supports_shutdown`) additionally carry
    ``supports_shutdown="true"`` and a ``liveness`` of RUNNING / STOPPED /
    UNKNOWN. Container liveness rides here rather than on a separate SSE channel:
    a liveness change makes the entry differ, so the existing ``workspaces``
    diff pushes it. Non-capable minds carry neither field.
    """
    errored_provider_names = {str(name) for name in backend_resolver.get_provider_errors()}
    liveness_by_agent_id = compute_mind_liveness_by_agent_id(backend_resolver)
    agent_ids = backend_resolver.list_active_workspace_ids()
    workspaces: list[dict[str, str]] = []
    for aid in agent_ids:
        info = backend_resolver.get_agent_display_info(aid)
        ws_name = backend_resolver.get_workspace_name(aid)
        if not ws_name:
            ws_name = info.agent_name if info else str(aid)
        accent = _resolved_workspace_color(backend_resolver, aid)
        entry: dict[str, str] = {
            "id": str(aid),
            "name": ws_name,
            "accent": accent,
        }
        # Mark the workspace stale when its provider's most recent discovery
        # poll errored: it was retained from prior state, so its liveness is
        # unverified rather than confirmed healthy.
        if _is_workspace_provider_errored(info, errored_provider_names):
            entry["is_stale"] = "true"
        liveness = liveness_by_agent_id.get(str(aid))
        if liveness is not None:
            entry["supports_shutdown"] = "true"
            entry["liveness"] = liveness.value
        if session_store is not None:
            account = session_store.get_account_for_workspace(str(aid))
            if account is not None:
                entry["account"] = account.email
        workspaces.append(entry)
    return workspaces


def _displayable_pending_requests(
    inbox: RequestInbox | None,
    backend_resolver: BackendResolverInterface,
) -> list[RequestEvent]:
    """Pending requests whose originating agent's host is currently resolvable.

    A permission request filed by an agent on a since-stopped workspace
    lingers in the inbox after that workspace disappears from discovery
    (the request file survives on the gateway). With no live agent to
    resolve, the inbox can only fall back to raw agent ids, which render
    as meaningless 16-char hex in the UI. Rather than show those, we hide
    a request whenever ``get_agent_display_info`` can't resolve its agent
    -- the same signal every other display path uses to map an agent to a
    host/workspace. The request itself is untouched on the gateway, so it
    reappears if the workspace comes back (or once a freshly-arrived
    request's host is discovered).
    """
    pending = inbox.get_pending_requests() if inbox else []
    displayable: list[RequestEvent] = []
    for req in pending:
        try:
            agent_id = AgentId(req.agent_id)
        except InvalidRandomIdError:
            # A request with a malformed agent_id (not a valid 'agent-...' id) can't
            # resolve to a real agent, so it isn't displayable. Skip it rather than let
            # the AgentId() validation raise and take down the whole request panel.
            continue
        if backend_resolver.get_agent_display_info(agent_id) is not None:
            displayable.append(req)
    return displayable


def _build_requests_payload(
    inbox: RequestInbox | None,
    backend_resolver: BackendResolverInterface,
) -> dict[str, Any]:
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

    Requests whose host can't be resolved are excluded (see
    :func:`_displayable_pending_requests`) so the badge count and the
    rendered cards stay in agreement.
    """
    pending = _displayable_pending_requests(inbox, backend_resolver)
    request_ids = [str(req.event_id) for req in pending]
    return {"count": len(request_ids), "request_ids": request_ids}


# -- System-interface recovery / restart --

# Minds creates two mngr agents per workspace, both with ``work_dir=/code``
# in the same container:
#   - a ``claude``-type agent with the user-chosen name -- runs the user's
#     Claude conversation.
#   - a ``main``-type agent always named ``system-services`` -- runs the
#     bootstrap, which execs supervisord, which supervises the system interface.
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
# Default hard timeout for an ``mngr`` subprocess run via ``_run_mngr``. Generous
# because it is sized for the slowest legitimate case -- a host stop/start, which
# bounces a container and can take tens of seconds -- so it is a "definitely
# wedged" ceiling, not an estimate. Most callers (stop/start, bulk host stop,
# ``mngr label``) take this default; the recovery host-health exec probe
# overrides it with a much shorter cap.
_MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
# Hard timeout for the recovery host-health probe's in-container ``mngr exec``.
# Far shorter than the default ceiling: this is a *diagnostic* that gates the
# recovery UI. The exec touches the provider (``get_host`` -> the connector's
# ~30s httpx) before reaching the container, so it must carry its own 30s-class
# cap rather than inheriting the 120s default -- otherwise a wedged host could
# gate the recovery UI for the full timeout. The probe is only fired when the
# provider is reachable and the host is RUNNING, so it never stacks a doomed
# round-trip during an outage.
_HOST_HEALTH_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
# How recent the last full discovery snapshot must be to treat discovery as
# trustworthy. A healthy discovery poll emits a snapshot every
# ``DISCOVERY_STREAM_POLL_INTERVAL_SECONDS``; three missed snapshots means the
# pipeline has stalled, so the host/provider state it last reported can no longer
# be trusted to drive the recovery redirect. The 3x multiple stays comfortably
# above the normal inter-snapshot interval to avoid a false "stale" during a
# single slow-but-healthy poll.
_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS: Final[float] = 3 * DISCOVERY_STREAM_POLL_INTERVAL_SECONDS
# How long we wait for the system interface to answer again after a restart,
# split by tier. A surgical (in-place) restart leaves the container running, so
# the interface should answer again quickly. A host restart cold-boots the
# container (restore-from-snapshot + the bootstrap execing supervisord, which
# spawns the system interface), which legitimately takes longer. Initial agent-creation
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
) -> Response:
    """Render the workspace-recovery page (shown by the 503 redirect or by direct nav)."""
    if not _is_request_authenticated():
        return make_html_response(content=render_login_page(), status_code=403)
    aid = AgentId(agent_id)
    tracker: SystemInterfaceHealthTracker | None = get_state().system_interface_health_tracker
    initial_status = tracker.get_health(aid).value if tracker is not None else AgentHealth.HEALTHY.value
    initial_error = (tracker.get_last_restart_error(aid) or "") if tracker is not None else ""
    return_to = _sanitize_recovery_return_to(request.args.get("return_to", ""))
    is_explicit_restart = request.args.get("intent", "") == "restart"
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
            return make_redirect_response(url=return_to, status_code=302)
        else:
            # HEALTHY with no return_to to redirect to: render with
            # render_status still HEALTHY -- the page then offers a manual
            # restart button.
            pass
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    html_body = render_recovery_page(
        agent_id=aid,
        return_to=return_to,
        initial_status=render_status,
        initial_error=initial_error,
        ssh_command=_ssh_command_for_agent(backend_resolver, aid),
    )
    return make_html_response(content=html_body)


def _run_mngr(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run an ``mngr`` subprocess to completion and return its stdout on a clean exit.

    Raises ``MngrCommandError`` for every non-clean outcome, like the rest of
    minds' mngr calls (``run_mngr_create``, the destroy cleanup) -- one domain
    error the caller catches once. Delegates the launch/timeout handling to
    ``_run_mngr_capturing`` and layers the raise-on-nonzero policy on top, so the
    subprocess plumbing lives in one place. A timeout therefore surfaces as
    ``MngrCommandTimeoutError`` (a ``MngrCommandError`` subclass, so existing
    ``except MngrCommandError`` callers are unaffected), a nonzero exit as a bare
    ``MngrCommandError`` (discarding stdout, unlike ``_run_mngr_capturing``), and
    a launch failure as a bare ``MngrCommandError``.

    ``timeout_seconds`` defaults to the generous ceiling sized for a host
    stop/start; the recovery host-health exec probe overrides it with a much
    shorter cap since it must not gate the recovery UI for tens of seconds.
    """
    stdout, returncode, stderr = _run_mngr_capturing(concurrency_group, argv, env, timeout_seconds=timeout_seconds)
    if returncode != 0:
        raise MngrCommandError(f"exited {returncode}: {stderr.strip()}")
    return stdout


def _run_mngr_capturing(
    concurrency_group: ConcurrencyGroup,
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float = _MNGR_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, int, str]:
    """Run an ``mngr`` subprocess, returning ``(stdout, returncode, stderr)`` without raising on a nonzero exit.

    A nonzero exit is reported through the returned ``returncode`` rather than
    raised, so stdout is preserved for the caller to inspect. A failure to launch
    the process raises ``MngrCommandError``; a timeout raises the more specific
    ``MngrCommandTimeoutError``.
    """
    try:
        finished = concurrency_group.run_process_to_completion(
            argv,
            timeout=timeout_seconds,
            is_checked_after=False,
            env=env,
        )
    except (OSError, ConcurrencyGroupError) as exc:
        # The command never ran (a fork/exec failure, or a concurrency-group
        # setup/strand/shutdown failure -- ``ProcessSetupError``,
        # ``StrandTimedOutError``, ``EnvironmentStoppedError``,
        # ``InvalidConcurrencyGroupStateError``). Our callers handle failure
        # locally and must keep going (the host-health probe composes a partial
        # response and cannot 500), so we wrap it as the single MngrCommandError
        # they already catch rather than leaving them to also catch this
        # infra-exception tuple.
        raise MngrCommandError(str(exc)) from exc
    if finished.is_timed_out:
        raise MngrCommandTimeoutError(f"timed out after {int(timeout_seconds)}s")
    # A finished, non-timed-out process always carries a returncode; the Optional
    # is for the not-yet-finished case, which this branch has ruled out. Coerce a
    # surprise None to a nonzero so the caller treats it as a failed listing.
    returncode = finished.returncode if finished.returncode is not None else 1
    return finished.stdout, returncode, finished.stderr


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
    agent_id: str,
    is_host_restart: bool,
) -> Response:
    """Shared body for the two restart endpoints: validate, mark RESTARTING, spawn the worker."""
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    tracker: SystemInterfaceHealthTracker | None = get_state().system_interface_health_tracker
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    if tracker is None or concurrency_group is None:
        return _json_error("Workspace restart is unavailable in this configuration", status_code=503)
    # A restart is already in flight for this agent -- don't stack a second
    # worker thread racing the first one's stop/start commands. mark_restarting
    # decides the RESTARTING transition under its own lock and reports whether
    # this caller won it, so this check-and-claim is atomic against concurrent
    # restart requests (recovery page, sidebar, landing page).
    if not tracker.mark_restarting(aid):
        return make_response(status_code=202, content="{}", media_type="application/json")

    # The auto-dispatched host tier (chosen only when the host-health probe
    # found the container fully stopped) passes ``host_already_stopped=1`` so
    # the worker can skip the redundant stop step. Honored only for host
    # restarts: a manually-requested restart may target a still-running
    # container, which must be stopped first.
    skip_stop = is_host_restart and request.args.get("host_already_stopped") == "1"

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
                "mngr_binary": get_state().mngr_binary,
                "mngr_host_dir": get_state().mngr_host_dir,
                "concurrency_group": concurrency_group,
                "mngr_forward_port": get_state().mngr_forward_port or 0,
                "mngr_forward_preauth_cookie": get_state().mngr_forward_preauth_cookie,
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
    return make_response(status_code=202, content="{}", media_type="application/json")


def _handle_restart_system_interface_api(
    agent_id: str,
) -> Response:
    """Dispatch a surgical restart of the system-services agent (``mngr stop`` + ``mngr start``)."""
    return _dispatch_restart(agent_id=agent_id, is_host_restart=False)


def _handle_restart_host_api(
    agent_id: str,
) -> Response:
    """Dispatch a full host restart (``mngr stop --stop-host`` + ``mngr start`` of system-services)."""
    return _dispatch_restart(agent_id=agent_id, is_host_restart=True)


# -- Mind host Start / Stop --
#
# A "shutdown-capable mind" is a workspace on a provider whose host minds can
# stop and start (see ``provider_backend_supports_shutdown`` -- the local docker
# / lima backends today). Stopping one frees the user's machine while preserving
# data and leaving it fully restartable. Stop = ``mngr stop --stop-host`` on the
# host (same teardown the host-restart tier uses); Start = ``mngr start`` (boots
# the stopped container). Both set an optimistic host-state override on the
# resolver so the landing page and quit prompt flip at once; the next discovery
# snapshot then confirms (or corrects) it.
#
# The single-mind endpoints run the ``mngr`` command synchronously (on the
# request's worker thread) and return the real outcome -- no fire-and-forget
# dispatch. The quit-time bulk stop issues ONE
# ``mngr stop <ids...> --stop-host``, which stops every named host concurrently
# via mngr's own executor, rather than one subprocess per mind.


class _MindHostAction(UpperCaseStrEnum):
    """Which lifecycle action a Start/Stop runs on a mind's host."""

    STOP = auto()
    START = auto()


def _resolve_host_id(backend_resolver: BackendResolverInterface, workspace_agent_id: AgentId) -> HostId | None:
    """Return the host id of ``workspace_agent_id`` (the key the liveness override uses), or None."""
    info = backend_resolver.get_agent_display_info(workspace_agent_id)
    return HostId(info.host_id) if info is not None else None


def _build_mngr_stop_hosts_argv(mngr_binary: str, agent_ids: Sequence[AgentId]) -> list[str]:
    """Build the argv for one ``mngr stop <ids...> --stop-host`` over several hosts.

    ``mngr stop`` is variadic and stops the named hosts concurrently (mngr's own
    executor), so a single command replaces one subprocess per mind.
    """
    return [mngr_binary, "stop", *(str(aid) for aid in agent_ids), "--quiet", "--stop-host"]


def _perform_mind_host_action(
    workspace_agent_id: AgentId,
    action: _MindHostAction,
    backend_resolver: BackendResolverInterface,
    mngr_binary: str,
    mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> bool:
    """Stop or start one mind's host, running ``mngr`` to completion; return True on success.

    On success sets the optimistic host-state override (so the landing page and
    quit prompt flip immediately, reconciling on the next discovery snapshot); on
    failure clears any override so the UI reverts to the authoritative discovery
    state. Runs synchronously in the caller's thread, so the endpoint returns the
    real outcome.
    """
    services_agent_id = backend_resolver.get_system_services_agent_id(workspace_agent_id)
    if services_agent_id is None:
        logger.warning(
            "Could not locate the system-services agent for host {} on {}", action.value, workspace_agent_id
        )
        return False
    host_id = _resolve_host_id(backend_resolver, workspace_agent_id)
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(mngr_host_dir)
    match action:
        case _MindHostAction.STOP:
            argv = _build_mngr_stop_argv(mngr_binary, services_agent_id, is_host_restart=True)
        case _MindHostAction.START:
            argv = _build_mngr_start_argv(mngr_binary, services_agent_id)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        _run_mngr(concurrency_group, argv, env)
    except MngrCommandError as exc:
        logger.warning("Host {} for {} failed: {}", action.value, workspace_agent_id, exc)
        if host_id is not None:
            backend_resolver.clear_host_state_override(host_id)
        return False
    if host_id is not None:
        match action:
            case _MindHostAction.STOP:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
            case _MindHostAction.START:
                backend_resolver.set_host_state_override(host_id, HostState.RUNNING)
            case _ as unreachable:
                assert_never(unreachable)
    return True


def _dispatch_mind_host_action(
    agent_id: str,
    action: _MindHostAction,
) -> Response:
    """Shared body for the stop-host / start-host endpoints: validate, run synchronously, return the outcome."""
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    if concurrency_group is None:
        return _json_error("Mind host control is unavailable in this configuration", status_code=503)
    succeeded = _perform_mind_host_action(
        workspace_agent_id=aid,
        action=action,
        backend_resolver=backend_resolver,
        mngr_binary=get_state().mngr_binary,
        mngr_host_dir=get_state().mngr_host_dir,
        concurrency_group=concurrency_group,
    )
    if not succeeded:
        return _json_error(f"Could not {action.value.lower()} the mind host", status_code=500)
    return make_response(content="{}", media_type="application/json")


def _handle_stop_host_api(
    agent_id: str,
) -> Response:
    """Stop a mind's host (``mngr stop --stop-host``)."""
    return _dispatch_mind_host_action(agent_id=agent_id, action=_MindHostAction.STOP)


def _handle_start_host_api(
    agent_id: str,
) -> Response:
    """Start a mind's stopped host (``mngr start``)."""
    return _dispatch_mind_host_action(agent_id=agent_id, action=_MindHostAction.START)


def _running_mind_entries(backend_resolver: BackendResolverInterface) -> list[dict[str, str]]:
    """Return ``[{id, name}, ...]`` for every shutdown-capable mind currently RUNNING.

    Reads liveness from the discovery snapshot (plus any optimistic override) in
    memory -- no subprocess -- so callers (the quit prompt, the bulk-stop result)
    are instant.
    """
    running: list[dict[str, str]] = []
    for aid_str, state in compute_mind_liveness_by_agent_id(backend_resolver).items():
        if state != MindLiveness.RUNNING:
            continue
        aid = AgentId(aid_str)
        name = backend_resolver.get_workspace_name(aid)
        if not name:
            info = backend_resolver.get_agent_display_info(aid)
            name = info.agent_name if info is not None else aid_str
        running.append({"id": aid_str, "name": name})
    return running


def _handle_running_minds_api() -> Response:
    """Return the shutdown-capable minds whose containers are currently running, for the quit prompt.

    Derives state from the discovery snapshot's host state (plus any optimistic
    override from a just-issued Start/Stop) in memory rather than shelling out to
    ``mngr list`` -- so the quit dialog appears instantly instead of blocking on a
    subprocess. The prompt's purpose is "free local resources you forgot about",
    not exact accounting: a container stopped externally since the last discovery
    snapshot may still be listed, but re-stopping it is idempotent. Each entry
    carries the agent id and human-readable workspace name.
    """
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    return make_response(
        content=json.dumps({"running": _running_mind_entries(backend_resolver)}), media_type="application/json"
    )


def _handle_stop_mind_hosts_api() -> Response:
    """Stop the hosts of the given shutdown-capable minds in one ``mngr stop --stop-host``.

    The target agent ids come from repeated ``agent_id`` query params (the ids the
    quit prompt listed). Each is resolved to the system-services agent sharing its
    host -- the host-stop target -- and all are passed to a single, synchronous
    ``mngr stop ... --stop-host``; mngr stops every named host concurrently via
    its own executor, so this is one subprocess rather than one per mind.

    After the attempt it recomputes liveness and returns the requested minds still
    running, so the quit flow can offer Retry without polling. On full success
    every targeted host is stopped, the STOPPED override is set per host, and
    ``still_running`` is empty; on partial failure ``mngr stop`` raises (it still
    joins every host first), so ``still_running`` reflects the current discovery
    snapshot -- which may briefly over-report a host that did stop until discovery
    catches up. A Retry re-stop is idempotent (mngr reports "already stopped").
    """
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    if concurrency_group is None:
        return _json_error("Mind host control is unavailable in this configuration", status_code=503)
    requested_ids = request.args.getlist("agent_id")
    # Resolve each workspace agent to the system-services agent that shares its
    # host (the host-stop target) and remember its host for the optimistic override.
    services_agent_ids: list[AgentId] = []
    host_ids: list[HostId] = []
    for agent_id in requested_ids:
        aid = AgentId(agent_id)
        services_agent_id = backend_resolver.get_system_services_agent_id(aid)
        if services_agent_id is None:
            logger.warning("Could not locate the system-services agent for host stop on {}", aid)
            continue
        services_agent_ids.append(services_agent_id)
        host_id = _resolve_host_id(backend_resolver, aid)
        if host_id is not None:
            host_ids.append(host_id)
    if services_agent_ids:
        env = dict(os.environ)
        env["MNGR_HOST_DIR"] = str(get_state().mngr_host_dir)
        argv = _build_mngr_stop_hosts_argv(get_state().mngr_binary, services_agent_ids)
        try:
            _run_mngr(concurrency_group, argv, env)
        except MngrCommandError as exc:
            logger.warning("Bulk host stop failed for {}: {}", requested_ids, exc)
        else:
            for host_id in host_ids:
                backend_resolver.set_host_state_override(host_id, HostState.STOPPED)
    requested_set = set(requested_ids)
    still_running = [entry for entry in _running_mind_entries(backend_resolver) if entry["id"] in requested_set]
    return make_response(content=json.dumps({"still_running": still_running}), media_type="application/json")


def _handle_stop_state_container_api() -> Response:
    """Stop this env's mngr Docker state container, to fully free local resources at quit.

    The docker provider keeps a singleton state container (``<MNGR_PREFIX>docker-
    state-<user_id>``) holding host records; ``mngr stop --stop-host`` leaves it
    running. The Electron quit flow calls this after all minds are stopped so
    nothing minds-related is left running. It stops (not removes) the container --
    the volume / records persist and it restarts on next use. This is inherently
    docker-specific (the state container is a docker-provider construct); a no-op
    for envs without one.
    """
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    if concurrency_group is None:
        return make_response(content=json.dumps({"stopped": False}), media_type="application/json")
    try:
        was_attempted = stop_active_env_state_container(
            mngr_host_dir=get_state().mngr_host_dir,
            parent_concurrency_group=concurrency_group,
        )
    except DockerCleanupError as exc:
        logger.warning("Failed to stop the Docker state container at shutdown: {}", exc)
        return _json_error(f"Could not stop the Docker state container: {exc}", status_code=500)
    return make_response(content=json.dumps({"stopped": was_attempted}), media_type="application/json")


def _handle_host_health_probe_api(
    agent_id: str,
) -> Response:
    """Layer-2 probe: run each recovery-diagnostics probe, classify the dispatch tier.

    Returns a flat ``HostHealthResponse`` -- a list of named probes plus a
    derived ``dispatch_tier``. The recovery page renders each probe as a
    row and keys its restart-tier branching off ``dispatch_tier``.
    """
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    aid = AgentId(agent_id)
    concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    if concurrency_group is None:
        return _json_error("Host health probe is unavailable in this configuration", status_code=503)
    response = _run_host_health_probe(aid, concurrency_group)
    logger.info("Layer-2 host-state probe for {}: dispatch_tier={}", aid, response.dispatch_tier.value)
    return make_response(
        content=response.model_dump_json(),
        media_type="application/json",
    )


def _provider_error_message_for_workspace(
    provider_errors: Mapping[ProviderInstanceName, DiscoveryError], provider_name: str | None
) -> str | None:
    """Map this workspace's provider error message (if any) from the discovery snapshot.

    ``get_provider_errors()`` keys per-provider discovery errors by provider
    name, so attribution to *this* workspace's provider is exact -- a docker
    mind's recovery is never blamed on a simultaneous imbue_cloud outage. Returns
    None in the brief pre-discovery window where the provider is unknown
    (``provider_name is None``) rather than guess, and None when this workspace's
    provider has no surfaced error. Otherwise returns the provider's own error
    message, which the recovery page surfaces verbatim.
    """
    if provider_name is None:
        return None
    for name, error in provider_errors.items():
        if str(name) == provider_name:
            return error.message
    return None


def _is_discovery_fresh(last_full_snapshot_at: datetime | None) -> bool:
    """Whether the most recent full discovery snapshot is recent enough to trust.

    A snapshot older than ``_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS`` (or no
    snapshot at all) means discovery has stalled -- the resolver's host state may
    pre-date an outage -- so reachability cannot be positively established.
    """
    if last_full_snapshot_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - last_full_snapshot_at).total_seconds()
    return age_seconds <= _DISCOVERY_FRESHNESS_THRESHOLD_SECONDS


def _run_host_health_probe(
    agent_id: AgentId,
    concurrency_group: ConcurrencyGroup,
) -> HostHealthResponse:
    """Compose the host-health response from the passive resolver + an in-container probe.

    Provider reachability and host lifecycle are read from the
    ``backend_resolver`` -- the single passive-discovery sampler shared with the
    rest of minds -- not re-sampled with a synchronous ``mngr list``. The reason
    the inner interface isn't answering comes from the batched in-container
    ``mngr exec`` probe, which is fired only when the provider is reachable and
    the host is RUNNING so an outage never pays a doomed provider round-trip. The
    plugin's resolver-snapshot mirror supplies the last probe.

    Callers reach this only once discovery is fresh (the recovery redirect is
    gated on freshness in the chrome-events stream), so the host/provider state
    read here is trustworthy without a per-call freshness gate.
    """
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(get_state().mngr_host_dir)
    mngr_binary: str = get_state().mngr_binary
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    services_agent_id = backend_resolver.get_system_services_agent_id(agent_id)
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    # Friendly provider name for the "Can't connect to ..." page title; reuse the
    # workspace-listing label map (docker -> "Docker", imbue_cloud_* -> "Imbue
    # Cloud", ...), falling back to a generic label for an unknown/None provider.
    provider_label = friendly_provider_label(provider_name) or "the workspace backend"

    # Read host/provider state from the passive discovery resolver.
    host_state_enum = (
        backend_resolver.get_host_state(HostId(display_info.host_id)) if display_info is not None else None
    )
    host_state = host_state_enum.value if host_state_enum is not None else ""
    provider_error_message = _provider_error_message_for_workspace(
        backend_resolver.get_provider_errors(), provider_name
    )

    # In-container exec probe, only when the provider is reachable and the host is
    # RUNNING. The exec SSHes to the container via ``get_host`` (the connector's
    # ~30s httpx), so it carries an explicit 30s-class cap (never the 120s
    # default) and is skipped entirely unless the provider has no surfaced error
    # and the host is RUNNING -- so an outage never stacks a doomed round-trip
    # here, and a stopped host is classified offline without an exec attempt. A
    # non-clean outcome leaves ``in_container_stdout`` None (parses to "no" on the
    # can-we-run-commands probe) and is recorded only at debug.
    in_container_stdout: str | None = None
    if services_agent_id is not None and provider_error_message is None and host_state_enum == HostState.RUNNING:
        try:
            in_container_stdout = _run_mngr(
                concurrency_group,
                build_probe_argv(mngr_binary, services_agent_id),
                env,
                timeout_seconds=_HOST_HEALTH_PROBE_TIMEOUT_SECONDS,
            )
        except MngrCommandError as exc:
            logger.debug("in-container probe for host-health of {} did not exit cleanly: {}", agent_id, exc)
    consumer: EnvelopeStreamConsumer | None = get_state().envelope_stream_consumer
    plugin_resolver_services: dict[str, str] = (
        consumer.get_resolver_snapshot_for_agent(agent_id) if consumer is not None else {}
    )
    if services_agent_id is not None:
        exec_command = shlex.join(build_probe_argv(mngr_binary, services_agent_id))
    else:
        exec_command = "(mngr exec <system-services-agent>) -- no services agent id known"
    return build_host_health_response(
        host_state=host_state,
        services_agent_id=services_agent_id,
        in_container_stdout=in_container_stdout,
        plugin_resolver_services=plugin_resolver_services,
        mngr_exec_command=exec_command,
        mngr_binary=mngr_binary,
        provider_error_message=provider_error_message,
        provider_label=provider_label,
    )


# -- Account management routes --


def _handle_accounts_page() -> Response:
    """Render the manage accounts page."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    minds_config: MindsConfig | None = get_state().minds_config
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
    return make_html_response(content=html)


def _handle_settings_page() -> Response:
    """Render the app-level settings page (GET /settings).

    Hosts the per-machine error-reporting toggles, seeded from ``MindsConfig``. Requires the same
    local session as the rest of the app; it is not account-scoped.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    minds_config: MindsConfig | None = get_state().minds_config
    report_unexpected_errors = minds_config.get_report_unexpected_errors() if minds_config else False
    include_error_logs = minds_config.get_include_error_logs() if minds_config else False
    html = render_settings_page(
        report_unexpected_errors=report_unexpected_errors,
        include_error_logs=include_error_logs,
    )
    return make_html_response(content=html)


def _handle_set_default_account() -> Response:
    """Set the default account for new workspaces."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    form = request.form
    user_id = str(form.get("user_id", ""))
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config and user_id:
        minds_config.set_default_account_id(user_id)
    return make_response(status_code=303, headers={"Location": "/accounts"})


def _handle_account_logout(
    user_id: str,
) -> Response:
    """Log out a specific account.

    Routes through the same plugin-side signout as ``_handle_signout_api``
    so the SuperTokens session is actually revoked, the
    ``[providers.imbue_cloud_<slug>]`` block is torn down, and the
    identity cache reflects the new state. Without this, just dropping
    the cache would let the next ``auth list`` call resurrect the
    account because the plugin still holds the session on disk.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    if get_state().session_store is not None:
        signout_user_via_plugin(user_id)
    return make_response(status_code=303, headers={"Location": "/accounts"})


# -- Workspace settings routes --


_IMBUE_CLOUD_PROVIDER_PREFIX: Final[str] = "imbue_cloud_"


def _is_leased_imbue_cloud_workspace(backend_resolver: BackendResolverInterface, agent_id: str) -> bool:
    """Return True if the workspace runs on a host leased from imbue_cloud.

    Leased hosts surface under a per-account provider instance named
    ``imbue_cloud_<account-slug>`` (the bare singleton ``imbue_cloud`` provider
    is hidden and never hosts a user workspace). The trailing-underscore prefix
    matches the per-account instances while excluding that singleton.
    """
    info = backend_resolver.get_agent_display_info(AgentId(agent_id))
    if info is None or info.provider_name is None:
        return False
    return info.provider_name.startswith(_IMBUE_CLOUD_PROVIDER_PREFIX)


def _handle_workspace_settings(
    agent_id: str,
) -> Response:
    """Render workspace settings page with account, sharing, telegram, and delete options."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    backend_resolver = get_state().backend_resolver
    session_store: MultiAccountSessionStore | None = get_state().session_store
    current_account = session_store.get_account_for_workspace(agent_id) if session_store else None
    accounts = session_store.list_accounts() if session_store else []
    is_leased_imbue_cloud = _is_leased_imbue_cloud_workspace(backend_resolver, agent_id)

    parsed_agent_id = AgentId(agent_id)
    ws_name = backend_resolver.get_workspace_name(parsed_agent_id)
    info = backend_resolver.get_agent_display_info(parsed_agent_id)
    if not ws_name:
        ws_name = info.agent_name if info else agent_id

    servers = [str(s) for s in backend_resolver.list_services_for_agent(parsed_agent_id)]

    telegram_orchestrator: TelegramSetupOrchestrator | None = get_state().telegram_orchestrator
    telegram_state: str | None = None
    if telegram_orchestrator is not None:
        telegram_state = "active" if telegram_orchestrator.agent_has_telegram(parsed_agent_id) else "pending"

    # Pre-fill the color picker with the workspace's stored color (or the
    # default when the workspace has no color label yet). Disable
    # the picker controls when the provider that owns this workspace is
    # in error state -- writes against an unreachable host would not be
    # observable until the provider recovers.
    current_color = _resolved_workspace_color(backend_resolver, parsed_agent_id)
    errored_provider_names = {str(name) for name in backend_resolver.get_provider_errors()}
    is_stale = _is_workspace_provider_errored(info, errored_provider_names)

    html = render_workspace_settings(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
        is_leased_imbue_cloud=is_leased_imbue_cloud,
        current_color=current_color,
        is_stale=is_stale,
    )
    return make_html_response(content=html)


def _handle_workspace_associate(
    agent_id: str,
) -> Response:
    """Associate a workspace with an account."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    # Leased imbue_cloud hosts are permanently bound to their leasing account;
    # re-associating one to a different account would cause confusing account
    # mixing, so reject it here as a defense-in-depth backstop to the UI guard.
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    if _is_leased_imbue_cloud_workspace(backend_resolver, agent_id):
        return make_response(
            status_code=403,
            content="Cannot change the account association of a host leased from imbue_cloud",
        )
    form = request.form
    user_id = str(form.get("user_id", ""))
    redirect_url = str(form.get("redirect", ""))
    session_store: MultiAccountSessionStore | None = get_state().session_store
    if session_store and user_id:
        session_store.associate_workspace(user_id, agent_id)
        # Wake the chrome SSE so the workspace tile picks up its new
        # 'account' field immediately rather than at the next 30s SSE
        # heartbeat. Without this, the user clicks Associate, the page
        # reloads via 303, but the chrome panel still shows the old
        # unassociated state for ~half a minute.
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
    location = redirect_url if redirect_url else f"/workspace/{agent_id}/settings"
    return make_response(status_code=303, headers={"Location": location})


def _handle_workspace_disassociate(
    agent_id: str,
) -> Response:
    """Disassociate a workspace from its account and tear down its tunnel."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    # Leased imbue_cloud hosts must stay bound to their leasing account; block
    # disassociation here as a defense-in-depth backstop to the disabled UI control.
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    if _is_leased_imbue_cloud_workspace(backend_resolver, agent_id):
        return make_response(
            status_code=403,
            content="Cannot disassociate a host leased from imbue_cloud",
        )
    session_store: MultiAccountSessionStore | None = get_state().session_store
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
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
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.notify_change()
    return make_response(status_code=303, headers={"Location": f"/workspace/{agent_id}/settings"})


# -- Inbox routes --


def _build_inbox_cards() -> list[Mapping[str, str]]:
    """Build the inbox card dicts for the current pending requests.

    Each card carries the fields the InboxList JinjaX component reads:
    ``id``, ``kind_label``, ``ws_name``, ``display_name``, ``accent``.
    Order matches ``RequestInbox.get_pending_requests`` --
    most-recent-first.
    """
    inbox: RequestInbox | None = get_state().request_inbox
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    pending = _displayable_pending_requests(inbox, backend_resolver)
    handlers: tuple[RequestEventHandler, ...] = get_state().request_event_handlers
    # Map ws_name -> "homepage agent id" so the card accent matches the
    # color the homepage tile and the titlebar use for that workspace
    # name. Each minds workspace owns two sibling mngr agents -- a
    # user-facing claude agent + a ``system-services`` agent. Latchkey
    # permission requests are filed by ``system-services``, so
    # ``req.agent_id`` is the sibling-not-shown-on-homepage. Computing
    # accent off the homepage agent's id keeps the inbox color in sync
    # with the rest of the UI. Falls back to the default workspace color
    # if no discovered agent claims that workspace (e.g. a freshly-arrived
    # request whose host hasn't been re-discovered yet).
    primary_agent_id_by_ws_name: dict[str, str] = {}
    for aid in backend_resolver.list_known_workspace_ids():
        wn = backend_resolver.get_workspace_name(aid)
        if wn and wn not in primary_agent_id_by_ws_name:
            primary_agent_id_by_ws_name[wn] = str(aid)
    cards: list[Mapping[str, str]] = []
    for req in pending:
        handler = find_handler_for_event(handlers, req)
        if handler is not None:
            kind_label = handler.kind_label()
            display_name = handler.display_name_for_event(req)
        else:
            # Fall through: unknown request type. Should never happen in
            # practice -- a request without a registered handler can't be
            # rendered or resolved -- but we still surface it in the
            # inbox so the user sees something is wrong.
            kind_label = "request"
            display_name = ""
        parsed_id = AgentId(req.agent_id)
        ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
        if not ws_name:
            info = backend_resolver.get_agent_display_info(parsed_id)
            ws_name = info.agent_name if info else req.agent_id[:16]
        # Inbox card accent mirrors the homepage tile's accent for the
        # workspace the request belongs to. ``primary_agent_id_by_ws_name``
        # comes from the resolver's current snapshot, so the primary id
        # is always a freshly-stringified AgentId -- reparsing through
        # AgentId is safe.
        primary_agent_id_str = primary_agent_id_by_ws_name.get(ws_name)
        accent = (
            _resolved_workspace_color(backend_resolver, AgentId(primary_agent_id_str))
            if primary_agent_id_str is not None
            else DEFAULT_WORKSPACE_COLOR
        )
        cards.append(
            {
                "id": str(req.event_id),
                "kind_label": kind_label,
                "ws_name": ws_name,
                "display_name": display_name,
                "accent": accent,
            }
        )
    return cards


def _resolve_inbox_selection(
    selected_id: str,
    backend_resolver: BackendResolverInterface,
) -> tuple[str, str]:
    """Resolve ``?selected=<id>`` to ``(selected_id, detail_html)``.

    Returns the id that should be highlighted in the left list and the
    HTML to embed in the right pane. Falls back to the first pending
    request when ``selected_id`` is empty; returns an "unavailable"
    fragment when the id is unknown or already resolved. ``selected_id``
    is the empty string if the inbox is empty or no item could be
    resolved.
    """
    inbox: RequestInbox | None = get_state().request_inbox
    if inbox is None:
        return "", ""
    pending = _displayable_pending_requests(inbox, backend_resolver)
    if not pending:
        return "", ""

    handlers: tuple[RequestEventHandler, ...] = get_state().request_event_handlers
    # Only requests in the displayable set are selectable: a request whose
    # host can't be resolved is hidden from the list, so honoring a stale
    # ``selected_id`` that points at one would render the same
    # agent-id-only detail we're hiding the card to avoid.
    displayable_by_id = {str(req.event_id): req for req in pending}
    target = None
    if selected_id:
        candidate = displayable_by_id.get(selected_id)
        if candidate is not None and not inbox.is_request_resolved(selected_id):
            target = candidate
    if target is None and selected_id:
        # Caller asked for a specific id but it can't be resolved: keep
        # the master list on its server-rendered default ordering and
        # surface the "no longer available" message in the right pane.
        return "", render_inbox_unavailable_fragment(
            message="It may have expired, or it was opened from an old link.",
        )
    if target is None:
        target = pending[0]

    handler = find_handler_for_event(handlers, target)
    if handler is None:
        return str(target.event_id), (f"<p>No handler registered for request type {target.request_type!r}</p>")
    detail_html = handler.render_request_detail_fragment(
        req_event=target,
        backend_resolver=backend_resolver,
        mngr_forward_origin=_get_mngr_forward_origin(),
    )
    return str(target.event_id), detail_html


def _handle_inbox_page() -> Response:
    """Render the full inbox modal page (``GET /inbox``)."""
    if not _is_request_authenticated():
        return make_html_response(content="<p>Not authenticated</p>")
    backend_resolver = get_state().backend_resolver
    cards = _build_inbox_cards()
    selected_query = request.args.get("selected", "")
    selected_id, detail_html = _resolve_inbox_selection(selected_query, backend_resolver)
    minds_config: MindsConfig | None = get_state().minds_config
    auto_open = minds_config.get_auto_open_requests_panel() if minds_config else True
    return make_html_response(
        content=render_inbox_page(
            cards=cards,
            selected_id=selected_id,
            detail_html=detail_html,
            is_empty=len(cards) == 0,
            auto_open=auto_open,
        )
    )


def _handle_inbox_list_fragment() -> Response:
    """Return the left-list fragment (``GET /inbox/list``)."""
    if not _is_request_authenticated():
        return make_html_response(content="<p>Not authenticated</p>")
    cards = _build_inbox_cards()
    return make_html_response(content=render_inbox_list_fragment(cards=cards, selected_id=""))


def _handle_inbox_detail_fragment(
    request_id: str,
) -> Response:
    """Return the right-pane detail fragment (``GET /inbox/detail/{id}``).

    Resolved or unknown ids get the "no longer available" fragment with
    HTTP 200 so the shell JS can innerHTML-swap it directly.
    """
    if not _is_request_authenticated():
        return make_html_response(content="<p>Not authenticated</p>")
    backend_resolver = get_state().backend_resolver
    inbox: RequestInbox | None = get_state().request_inbox
    if inbox is None:
        # The InboxUnavailable heading reads "This permission request is no
        # longer available", which makes no sense when the issue is that there
        # is no inbox at all. Drop the supporting message so only the heading
        # shows; the template treats an empty message as the no-extra-copy case.
        return make_html_response(content=render_inbox_unavailable_fragment())
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return make_html_response(
            content=render_inbox_unavailable_fragment(
                message="It may have expired, or it was opened from an old link.",
            ),
        )
    if inbox.is_request_resolved(request_id):
        return make_html_response(
            content=render_inbox_unavailable_fragment(message="It has already been processed."),
        )
    handlers: tuple[RequestEventHandler, ...] = get_state().request_event_handlers
    handler = find_handler_for_event(handlers, req_event)
    if handler is None:
        return make_html_response(
            content=f"<p>No handler registered for request type {req_event.request_type!r}</p>",
            status_code=500,
        )
    return make_html_response(
        content=handler.render_request_detail_fragment(
            req_event=req_event,
            backend_resolver=backend_resolver,
            mngr_forward_origin=_get_mngr_forward_origin(),
        )
    )


def _handle_requests_auto_open() -> Response:
    """Toggle the auto-open setting for the inbox modal.

    The route URL and on-disk setting key keep ``requests-panel`` /
    ``auto_open_requests_panel`` for backward compatibility (see
    :class:`MindsConfig`); "panel" here now refers to the inbox modal.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config:
        try:
            body = _read_json_body()
            enabled = body.get("enabled", True)
            minds_config.set_auto_open_requests_panel(bool(enabled))
        except (json.JSONDecodeError, ValueError):
            pass
    return make_response(status_code=200, content='{"ok": true}', media_type="application/json")


def _resolve_ws_name_and_account(
    agent_id: str,
    backend_resolver: BackendResolverInterface,
) -> tuple[str, str, bool, list[AccountSession]]:
    """Resolve workspace name, account email, has_account flag, and accounts list."""
    parsed_id = AgentId(agent_id)
    ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
    if not ws_name:
        info = backend_resolver.get_agent_display_info(parsed_id)
        ws_name = info.agent_name if info else agent_id
    session_store: MultiAccountSessionStore | None = get_state().session_store
    account = session_store.get_account_for_workspace(agent_id) if session_store else None
    account_email = account.email if account else ""
    has_account = account is not None
    accounts = session_store.list_accounts() if session_store else []
    return ws_name, account_email, has_account, accounts


def _handle_sharing_page(
    agent_id: str,
    service_name: str,
) -> Response:
    """Render the sharing editor page for direct editing (from workspace settings)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    backend_resolver = get_state().backend_resolver
    ws_name, account_email, has_account, accounts = _resolve_ws_name_and_account(
        agent_id,
        backend_resolver,
    )

    html = render_sharing_editor(
        agent_id=agent_id,
        service_name=service_name,
        title=f"Sharing: {service_name}",
        mngr_forward_origin=_get_mngr_forward_origin(),
        has_account=has_account,
        accounts=accounts,
        redirect_url=f"/sharing/{agent_id}/{service_name}",
        ws_name=ws_name,
        account_email=account_email,
    )
    return make_html_response(content=html)


def _handle_sharing_enable(
    agent_id: str,
    service_name: str,
) -> Response:
    """Enable or update sharing for a service via the workspace-settings editor.

    Sharing is configured exclusively from this editor; agents no longer
    write sharing-request events back into the inbox.

    On a soft failure (no signed-in account, plugin error, etc.) the
    handler returns 502 with a JSON ``{"error": "..."}`` body. The
    sharing editor JS surfaces that inline instead of silently
    redirecting to a now-empty status page.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    backend_resolver = get_state().backend_resolver
    form = request.form
    emails = parse_emails_form_value(str(form.get("emails", "[]")))
    try:
        enable_sharing_via_cloudflare(
            agent_id=AgentId(agent_id),
            service_name=ServiceName(service_name),
            emails=emails,
            backend_resolver=backend_resolver,
        )
    except SharingError as exc:
        return make_response(
            status_code=502,
            content=json.dumps({"error": str(exc)}),
            media_type="application/json",
        )
    return make_response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


def _handle_sharing_disable(
    agent_id: str,
    service_name: str,
) -> Response:
    """Disable sharing for a service via the imbue_cloud plugin.

    Removes the service from its tunnel (DNS + Access app teardown
    happen connector-side). The tunnel itself stays around so re-
    enabling later doesn't re-issue a fresh token.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    session_store: MultiAccountSessionStore | None = get_state().session_store
    if cli is None:
        return make_response(
            status_code=502,
            content=json.dumps({"error": "imbue_cloud CLI is not configured."}),
            media_type="application/json",
        )
    parsed_id = AgentId(agent_id)
    try:
        account_email = resolve_account_email_for_workspace(session_store, parsed_id)
    except SharingError as exc:
        return make_response(
            status_code=502,
            content=json.dumps({"error": str(exc)}),
            media_type="application/json",
        )

    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(parsed_id))
    except ImbueCloudCliError as exc:
        return make_response(
            status_code=502,
            content=json.dumps({"error": f"Failed to look up the tunnel: {exc}"}),
            media_type="application/json",
        )
    if tunnel is None:
        # No tunnel = nothing to disable. Treat as success so the JS
        # redirect lands on the (already-disabled) status page.
        return make_response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})

    try:
        cli.remove_service(account=account_email, tunnel_name=tunnel.tunnel_name, service_name=service_name)
    except ImbueCloudCliError as exc:
        return make_response(
            status_code=502,
            content=json.dumps({"error": f"Failed to disable sharing: {exc}"}),
            media_type="application/json",
        )
    return make_response(status_code=303, headers={"Location": f"/sharing/{agent_id}/{service_name}"})


def _handle_sharing_status_api(
    agent_id: str,
    service_name: str,
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
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")

    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    session_store: MultiAccountSessionStore | None = get_state().session_store
    if cli is None:
        return make_response(
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
        return make_response(
            content=json.dumps({"enabled": False, "url": None, "policy": {"emails": []}}),
            media_type="application/json",
        )

    default_policy = {"emails": [account_email]}
    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(parsed_id))
    except ImbueCloudCliError as exc:
        logger.warning("Failed to list tunnels for {}: {}", parsed_id, exc)
        return make_response(
            content=json.dumps({"enabled": False, "url": None, "policy": default_policy}),
            media_type="application/json",
        )
    if tunnel is None or service_name not in tunnel.services:
        return make_response(
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

    return make_response(
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


def _probe_share_url_readiness(http_client: httpx.Client, url: str) -> bool:
    """Fetch ``url`` once and report whether the Cloudflare Access app is live.

    Uses the app's shared (``follow_redirects=False``) client so the Access
    login redirect is observed rather than followed. Any transport error or
    timeout is treated as "not ready yet".
    """
    try:
        response = http_client.get(url, timeout=_SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Probed share URL {} but it is not ready yet: {}", url, exc)
        return False
    return is_share_ready_from_edge_response(response.status_code, response.headers.get("location"))


def _handle_sharing_readiness_api(
    agent_id: str,
    service_name: str,
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
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    probe_url = request.args.get("url", "")
    http_client: httpx.Client | None = get_state().http_client
    if http_client is None or not is_probeable_share_url(probe_url):
        return make_response(content=json.dumps({"ready": False}), media_type="application/json")
    is_ready = _probe_share_url_readiness(http_client, probe_url)
    return make_response(content=json.dumps({"ready": is_ready}), media_type="application/json")


def _handle_request_grant(
    request_id: str,
) -> Response:
    """Dispatch a grant to the handler that claims the event's request type.

    The route layer is intentionally agnostic: it authenticates, looks
    up the request event, finds the registered
    :class:`RequestEventHandler` whose ``handles_request_type`` matches,
    and forwards the rest. Per-handler differences (form parsing,
    response shape, side effects) live in the handler.
    """
    return _dispatch_request_action(
        request_id=request_id,
        action="grant",
    )


def _handle_request_deny(
    request_id: str,
) -> Response:
    """Dispatch a deny to the handler that claims the event's request type."""
    return _dispatch_request_action(
        request_id=request_id,
        action="deny",
    )


def _dispatch_request_action(
    request_id: str,
    action: str,
) -> Response:
    """Shared body of grant/deny dispatchers.

    Authenticates, looks up the request event, picks the right handler,
    and forwards. ``action`` must be ``"grant"`` or ``"deny"``.
    """
    if not _is_request_authenticated():
        return _json_error("Not authenticated", status_code=403)
    inbox: RequestInbox | None = get_state().request_inbox
    if inbox is None:
        return _json_error("Request inbox not available", status_code=500)
    req_event = inbox.get_request_by_id(request_id)
    if req_event is None:
        return _json_error("Request not found", status_code=404)
    # Reject a second grant/deny on an already-resolved request so a stale
    # (e.g. cached) form cannot re-apply side effects.
    if inbox.is_request_resolved(request_id):
        return _json_error("This request has already been approved or denied.", status_code=409)

    handlers: tuple[RequestEventHandler, ...] = get_state().request_event_handlers
    handler = find_handler_for_event(handlers, req_event)
    if handler is None:
        return _json_error(
            f"No handler registered for request type '{req_event.request_type}'",
            status_code=400,
        )
    if action == "grant":
        return handler.apply_grant_request(request, req_event)
    if action == "deny":
        return handler.apply_deny_request(request, req_event)
    return _json_error(f"Unsupported action '{action}'", status_code=500)


_request_event_apps: dict[int, Flask] = {}


def _handle_request_event_callback(agent_id_str: str, raw_line: str) -> None:
    """Process an incoming request event and add it to the app's inbox.

    After mutating the inbox, fires the resolver's change notification so
    the chrome SSE wakes up and pushes the new ``requests`` payload immediately
    (otherwise it would lag up to 30s for the next poll tick, breaking the
    inbox modal auto-open and badge UX).

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
        app_state = get_state(app)
        current_inbox: RequestInbox | None = app_state.request_inbox
        if current_inbox is not None:
            app_state.request_inbox = current_inbox.add_request(event)
            logger.info("Request event from agent {}: {}", agent_id_str, event.request_type)
            backend_resolver: BackendResolverInterface = app_state.backend_resolver
            if isinstance(backend_resolver, MngrCliBackendResolver):
                backend_resolver.notify_change()


# -- App factory --


def create_desktop_client(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.Client | None,
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
    discovery_health_watchdog: DiscoveryHealthWatchdog | None = None,
) -> Flask:
    """Create the bare-origin minds Flask application.

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
    # Static assets: the compiled Tailwind v4 stylesheet (app.min.css) + per-page
    # JS, served by Flask's built-in static handler at the ``/_static`` URL.
    # app.min.css is built from static/app.css by `just minds-css`
    # (pnpm run build:css) and is gitignored; if it's missing the route still
    # works and the server logs a hint at startup.
    _static_dir = Path(__file__).resolve().parent / "static"
    if not (_static_dir / "app.min.css").exists():
        logger.warning("Missing static/app.min.css. Run `just minds-css` from the repo root to build it.")
    app = Flask(__name__, static_folder=str(_static_dir), static_url_path="/_static")

    @app.errorhandler(Exception)
    def _unhandled_exception_handler(exc: Exception) -> Response | HTTPException:
        # Let werkzeug's HTTP exceptions (404, 405, abort(401), ...) keep their
        # own status instead of collapsing them into a 500 -- matching the prior
        # FastAPI/Starlette behavior where the catch-all only handled real 500s.
        if isinstance(exc, HTTPException):
            return exc
        logger.opt(exception=exc).error("Unhandled exception on {} {}", request.method, request.path)
        return make_response(status_code=500, content=f"Internal Server Error: {exc}")

    state = DesktopClientState(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=http_client,
        agent_creator=agent_creator,
        imbue_cloud_cli=imbue_cloud_cli,
        telegram_orchestrator=telegram_orchestrator,
        notification_dispatcher=notification_dispatcher,
        api_v1_paths=paths,
        minds_config=minds_config,
        client_env_config=client_env_config,
        envelope_stream_consumer=envelope_stream_consumer,
        session_store=session_store,
        request_inbox=request_inbox,
        request_event_handlers=request_event_handlers,
        auth_server_port=server_port,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=mngr_forward_preauth_cookie,
        auth_output_format=output_format or OutputFormat.JSONL,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=system_interface_health_tracker,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir if mngr_host_dir is not None else Path.home() / ".mngr",
        minds_api_key=minds_api_key,
        latchkey_forward_supervisor=latchkey_forward_supervisor,
        discovery_health_watchdog=discovery_health_watchdog,
    )
    set_state(app, state)

    # Register callback to process incoming request events from agents
    if isinstance(backend_resolver, MngrCliBackendResolver):
        _request_event_apps[id(backend_resolver)] = app
        backend_resolver.add_on_request_callback(_handle_request_event_callback)

    # Mount the auth routes (proxy to the mngr_imbue_cloud plugin's auth subcommands)
    if session_store is not None and imbue_cloud_cli is not None:
        app.register_blueprint(create_supertokens_blueprint())

    # Mount the REST API v1 blueprint
    if paths is not None:
        app.register_blueprint(create_api_v1_blueprint())
        # Mount the WebDAV file server (a WSGI app) under /api/v1/files via
        # Werkzeug's dispatcher. Each share root maps URL-path == on-disk-path
        # (``~`` and ``/tmp``); the mount is gated by the same central-key
        # Bearer check that protects the rest of /api/v1, resolving
        # ``minds_api_key`` from the app's state on every request so the gate
        # stays in sync if a future code path ever rotates the key.
        webdav_app = create_webdav_app(_MindsApiKeyProvider(app=app))
        # The standard Flask sub-app mount pattern; ``wsgi_app`` is typed as the
        # bound method, so assigning a WSGI middleware over it trips the checker.
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/api/v1/files": webdav_app})  # ty: ignore[invalid-assignment]

    # Chrome (persistent shell) routes
    app.add_url_rule("/_chrome", view_func=_handle_chrome_page)
    app.add_url_rule("/_chrome/sidebar", view_func=_handle_chrome_sidebar)
    app.add_url_rule("/_chrome/events", view_func=_handle_chrome_events)

    app.add_url_rule("/_dev/styleguide", view_func=_handle_dev_styleguide)

    # Core routes
    app.add_url_rule("/consent", view_func=_handle_consent_page)
    app.add_url_rule("/consent", view_func=_handle_consent_submit, methods=["POST"])
    app.add_url_rule("/_chrome/error-reporting", view_func=_handle_error_reporting_settings, methods=["POST"])
    app.add_url_rule("/help", view_func=_handle_help_page)
    app.add_url_rule("/help/report", view_func=_handle_help_report, methods=["POST"])
    app.add_url_rule("/welcome", view_func=_handle_welcome_page)
    app.add_url_rule("/login", view_func=_handle_login)
    app.add_url_rule("/authenticate", view_func=_handle_authenticate)
    app.add_url_rule("/", view_func=_handle_landing_page)
    app.add_url_rule("/post-login", view_func=_handle_post_login_redirect)

    # Account management routes
    app.add_url_rule("/accounts", view_func=_handle_accounts_page)
    app.add_url_rule("/settings", view_func=_handle_settings_page)
    app.add_url_rule("/accounts/set-default", view_func=_handle_set_default_account, methods=["POST"])
    app.add_url_rule("/accounts/<user_id>/logout", view_func=_handle_account_logout, methods=["POST"])

    # Workspace settings routes
    app.add_url_rule("/workspace/<agent_id>/settings", view_func=_handle_workspace_settings)
    app.add_url_rule("/workspace/<agent_id>/associate", view_func=_handle_workspace_associate, methods=["POST"])
    app.add_url_rule("/workspace/<agent_id>/disassociate", view_func=_handle_workspace_disassociate, methods=["POST"])

    # Request inbox routes
    app.add_url_rule("/inbox", view_func=_handle_inbox_page)
    app.add_url_rule("/inbox/list", view_func=_handle_inbox_list_fragment)
    app.add_url_rule("/inbox/detail/<request_id>", view_func=_handle_inbox_detail_fragment)
    app.add_url_rule("/_chrome/requests-auto-open", view_func=_handle_requests_auto_open, methods=["POST"])
    app.add_url_rule("/requests/<request_id>/grant", view_func=_handle_request_grant, methods=["POST"])
    app.add_url_rule("/requests/<request_id>/deny", view_func=_handle_request_deny, methods=["POST"])

    # Sharing editor routes (used by both request approval and direct editing)
    app.add_url_rule("/sharing/<agent_id>/<service_name>", view_func=_handle_sharing_page)
    app.add_url_rule("/sharing/<agent_id>/<service_name>/enable", view_func=_handle_sharing_enable, methods=["POST"])
    app.add_url_rule("/sharing/<agent_id>/<service_name>/disable", view_func=_handle_sharing_disable, methods=["POST"])
    app.add_url_rule("/api/sharing-status/<agent_id>/<service_name>", view_func=_handle_sharing_status_api)
    app.add_url_rule("/api/sharing-readiness/<agent_id>/<service_name>", view_func=_handle_sharing_readiness_api)

    # Agent creation routes
    app.add_url_rule("/create", view_func=_handle_create_page)
    app.add_url_rule("/create", view_func=_handle_create_form_submit, methods=["POST"])
    app.add_url_rule("/api/backup-status", view_func=_handle_backup_status_api)
    app.add_url_rule("/api/backup-export/<agent_id>", view_func=_handle_backup_export_api)
    app.add_url_rule("/api/create-agent", view_func=_handle_create_agent_api, methods=["POST"])
    app.add_url_rule("/api/create-agent/<agent_id>/status", view_func=_handle_creation_status_api)
    app.add_url_rule("/api/create-agent/<agent_id>/logs", view_func=_handle_creation_logs_sse)
    app.add_url_rule("/creating/<agent_id>", view_func=_handle_creating_page)

    # Agent destruction routes
    app.add_url_rule("/api/destroy-agent/<agent_id>", view_func=_handle_destroy_agent_api, methods=["POST"])
    app.add_url_rule("/api/destroying/<agent_id>/status", view_func=_handle_destroying_status_api)
    app.add_url_rule("/api/destroying/<agent_id>/log", view_func=_handle_destroying_log_api)
    app.add_url_rule("/api/destroying/<agent_id>/dismiss", view_func=_handle_destroying_dismiss_api, methods=["POST"])
    app.add_url_rule("/destroying/<agent_id>", view_func=_handle_destroying_page)

    # Workspace color route
    app.add_url_rule("/api/workspaces/<agent_id>/color", view_func=_handle_set_workspace_color_api, methods=["POST"])

    # Telegram setup routes
    app.add_url_rule("/api/agents/<agent_id>/telegram/setup", view_func=_handle_telegram_setup, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/telegram/status", view_func=_handle_telegram_status)

    # Providers panel toggle (Disable / Enable buttons in the landing page panel)
    app.add_url_rule("/api/providers/<provider_name>/toggle", view_func=_handle_provider_toggle, methods=["POST"])

    # System-interface recovery routes
    app.add_url_rule("/agents/<agent_id>/recovery", view_func=_handle_recovery_page)
    app.add_url_rule("/api/agents/<agent_id>/host-health", view_func=_handle_host_health_probe_api)
    app.add_url_rule(
        "/api/agents/<agent_id>/restart-system-interface",
        view_func=_handle_restart_system_interface_api,
        methods=["POST"],
    )
    app.add_url_rule("/api/agents/<agent_id>/restart-host", view_func=_handle_restart_host_api, methods=["POST"])

    # Mind host Start / Stop + the quit-prompt running-minds lookup and bulk stop
    app.add_url_rule("/api/agents/<agent_id>/stop-host", view_func=_handle_stop_host_api, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/start-host", view_func=_handle_start_host_api, methods=["POST"])
    app.add_url_rule("/api/minds/running", view_func=_handle_running_minds_api)
    app.add_url_rule("/api/minds/stop-hosts", view_func=_handle_stop_mind_hosts_api, methods=["POST"])
    app.add_url_rule("/api/minds/stop-state-container", view_func=_handle_stop_state_container_api, methods=["POST"])

    return app


class _MindsApiKeyProvider(FrozenModel):
    """Resolves the live central minds API key from an app's state for the WebDAV gate.

    A small callable (rather than a closure/partial) so the WebDAV bearer gate can
    look the key up fresh on each request without minds capturing a stale value.
    """

    app: Flask = Field(frozen=True, description="The Flask app whose state holds the current minds API key.")

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}

    def __call__(self) -> str | None:
        return get_state(self.app).minds_api_key


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


# How often the discovery-health watchdog re-reads the resolver's snapshot
# freshness. Comfortably below the watchdog's inter-remediation wait so a due
# producer remediation fires within a tick or two of becoming due.
_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS: Final[float] = 5.0


def start_discovery_health_watchdog_loop(
    watchdog: DiscoveryHealthWatchdog,
    backend_resolver: BackendResolverInterface,
    root_concurrency_group: ConcurrencyGroup | None,
) -> None:
    """Start the background thread that drives the discovery-health watchdog.

    Each tick reads the resolver's ``last_full_snapshot_at`` and hands it to
    ``watchdog.evaluate``, which detects a producer stall, runs the
    bounce -> restart remediations, and escalates to BLOCKED. The thread no-ops when
    there is no concurrency group (test factories that skip background threads).
    """
    if root_concurrency_group is None:
        return
    root_concurrency_group.start_new_thread(
        target=_run_discovery_health_watchdog_loop,
        args=(watchdog, backend_resolver, root_concurrency_group),
        name="discovery-health-watchdog",
        daemon=True,
    )


def _run_discovery_health_watchdog_loop(
    watchdog: DiscoveryHealthWatchdog,
    backend_resolver: BackendResolverInterface,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Loop body for the discovery-health watchdog thread."""
    if not isinstance(backend_resolver, MngrCliBackendResolver):
        # Static resolvers used by tests report no freshness, so there is
        # nothing to watch. Resolver type is fixed for the process lifetime, so
        # exit immediately rather than spinning doing nothing.
        logger.debug(
            "Discovery-health watchdog thread exiting: backend_resolver is {}, not MngrCliBackendResolver",
            type(backend_resolver).__name__,
        )
        return
    while not root_concurrency_group.is_shutting_down():
        _, last_full_snapshot_at = backend_resolver.get_freshness_timestamps()
        watchdog.evaluate(last_full_snapshot_at)
        threading.Event().wait(timeout=_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS)
