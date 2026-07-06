import json
import os
import queue
import threading
import time
from collections.abc import Collection
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import urlparse

import httpx
from flask import Flask
from flask import Response
from flask import abort
from flask import request
from loguru import logger
from pydantic import Field
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.bootstrap import is_imbue_cloud_provider_enabled_for_account
from imbue.minds.bootstrap import list_disabled_provider_names
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.api_schema import create_api_schema_blueprint
from imbue.minds.desktop_client.api_v1 import create_api_v1_blueprint
from imbue.minds.desktop_client.assist_chat import spawn_assist_chat
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_password_store import has_saved_backup_password
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import delete_destroying
from imbue.minds.desktop_client.destroying import is_host_still_active
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.destroying import read_destroying
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.help_modal_requests import OpenHelpRequest
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.mind_liveness import compute_mind_liveness_by_agent_id
from imbue.minds.desktop_client.mind_liveness import get_shutdown_capable_workspace_agent_ids
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.region_preference import AWS_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.report_collector import submit_bug_report_from_body
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.responses import make_html_response
from imbue.minds.desktop_client.responses import make_redirect_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.responses import make_streaming_response
from imbue.minds.desktop_client.responses import safe_local_redirect_path
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import is_share_ready_from_edge_response
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.state import set_state
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
from imbue.minds.desktop_client.webdav import create_webdav_app
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import pick_unused_create_color
from imbue.minds.desktop_client.workspace_create import default_region_for_provider_with_config
from imbue.minds.errors import InvalidJsonBodyError
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr.primitives import AgentId
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
    description = request.args.get("description", "")
    # An in-workspace agent's escalation opens this modal via the open_help flow with
    # ``agent_report=1``. In that case the modal frames the pre-filled report as the
    # agent's submission (titled with the workspace it came from) and drops the
    # have-an-agent-help / report-a-bug choice -- we are already reporting. The
    # workspace name is best-effort (empty for an unknown/label-less workspace).
    is_agent_report = request.args.get("agent_report") == "1"
    workspace_name = ""
    if is_agent_report and workspace_agent_id:
        try:
            workspace_name = get_state().backend_resolver.get_workspace_name(AgentId(workspace_agent_id)) or ""
        except ValueError:
            workspace_name = ""
    return make_html_response(
        content=render_help_page(
            include_logs_setting=include_logs_setting,
            workspace_agent_id=workspace_agent_id,
            description=description,
            is_agent_report=is_agent_report,
            workspace_name=workspace_name,
        )
    )


def _handle_help_report() -> Response:
    """Collect and submit a user-submitted bug report from the help form (POST /help/report).

    Unauthenticated for the same reason as the page: the user may be reporting a sign-in problem. An
    agent-initiated report (the ``/api/v1`` route) lands here too: that route pre-fills this same form
    rather than submitting, so the human-reviewed send always flows through this collector.
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


def _handle_help_assist() -> Response:
    """Spawn an in-workspace ``/assist`` chat to help with a problem (POST /help/assist).

    Only valid when the help flow was opened from a loaded workspace: the body carries that
    workspace's agent id and the user's description. The desktop app runs ``mngr create`` inside that
    workspace's container (via ``mngr exec``) to spawn a new chat seeded with ``/assist <description>``;
    the system interface auto-opens its tab. The call blocks until ``mngr create`` finishes so the
    get-help modal can hold its "starting..." state until the chat exists, then returns 200 on success
    or 502 if the spawn failed.
    """
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(
            status_code=400, content='{"error": "Request body must be a JSON object"}', media_type="application/json"
        )
    description = str(body.get("description", "")).strip()
    if not description:
        return make_response(
            status_code=400, content='{"error": "A description is required"}', media_type="application/json"
        )
    workspace_agent_id_raw = str(body.get("workspace_agent_id", "")).strip()
    if not workspace_agent_id_raw:
        return make_response(
            status_code=400,
            content='{"error": "Agent help is only available inside a workspace"}',
            media_type="application/json",
        )
    try:
        workspace_agent_id = AgentId(workspace_agent_id_raw)
    except ValueError:
        return make_response(
            status_code=400, content='{"error": "Invalid workspace_agent_id"}', media_type="application/json"
        )

    # Wait for the create to finish before responding so the get-help modal keeps its
    # "starting..." state until the chat exists, rather than dismissing into a blank gap
    # while the agent boots. The cheroot WSGI pool (50 threads) absorbs the blocking call.
    started = spawn_assist_chat(
        mngr_caller=get_default_mngr_caller(),
        workspace_agent_id=workspace_agent_id,
        description=description,
    )
    if not started:
        return make_response(
            status_code=502,
            content=json.dumps({"error": "Could not start an agent in this workspace. Please try again."}),
            media_type="application/json",
        )
    return make_response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")


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
        selected_by_launch_mode[launch_mode.value] = default_region_for_provider_with_config(
            provider_key, minds_config, geo_cache
        )
    return options_by_launch_mode, selected_by_launch_mode


# -- Agent creation route handlers --


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


def _handle_creating_page(
    agent_id: str,
) -> Response:
    """Show the creating/loading page (GET /creating/{agent_id}).

    The page shows the setting-up progress screen while the workspace is
    created in the background, then redirects into the workspace once
    creation finishes. The page's JS polls the versioned operations resource
    (``/api/v1/workspaces/operations/create/<creation_id>`` + ``/logs``) for status
    and live logs, keyed by the same ``creation_id`` carried in the route.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")

    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return make_response(status_code=501, content="Agent creation not configured")

    # The ``agent_id`` route param is actually a ``CreationId`` (the
    # minds-internal in-flight handle returned by ``start_creation``); the
    # canonical mngr ``AgentId`` only exists once ``mngr create`` returns.
    creation_id = CreationId(agent_id)
    info = agent_creator.get_creation_info(creation_id)
    if info is None:
        # The creation registry is in-memory, so a ``/creating/<id>`` window that
        # outlives its creation -- reopened after an app restart, or after a
        # failed creation was cleaned up -- finds no info here. This is a
        # full-page navigation, so fall back to the landing page rather than
        # stranding the window on a bare 404. (The status/onboarding/logs
        # endpoints below keep returning 404 -- they are XHR/SSE callers, not
        # navigations, and their JS handles the not-found case itself.)
        return make_redirect_response(url="/", status_code=303)

    html = render_creating_page(creation_id=creation_id, info=info)
    return make_html_response(content=html)


# -- Agent destruction route handlers --


def _resolve_destroying_for_landing(
    paths: WorkspacePaths | None,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> dict[str, str]:
    """Walk ``<paths.data_dir>/destroying/``, finalize DONE records, return marker map.

    Returns ``{agent_id_str: "running" | "failed"}`` for any in-flight or
    failed destroy. A destroy is DONE only once the whole *host* is gone (not
    just the workspace agent -- see :func:`destroying.is_host_still_active`); on DONE we
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
    records = list_destroying(paths, lambda aid: is_host_still_active(backend_resolver, paths, aid))
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


def _is_host_still_active(agent_id: AgentId) -> bool:
    """Request-scoped wrapper around :func:`destroying.is_host_still_active`."""
    return is_host_still_active(
        get_state().backend_resolver,
        get_state().api_v1_paths,
        agent_id,
    )


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
        parsed_id, paths, is_host_still_active=is_host_still_active(backend_resolver, paths, parsed_id)
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
    help_broker = get_state().help_modal_request_broker

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

        # Agent-initiated "open the pre-filled report modal" requests arrive on a
        # Flask request thread (the /api/v1 report route) via the broker. We
        # accumulate them per-connection and drain them in the loop, the same
        # way health transitions are handled.
        open_help_queue: queue.Queue[OpenHelpRequest] = queue.Queue()

        def _on_change() -> None:
            change_event.set()

        def _on_health_change(agent_id: AgentId, status: AgentHealth) -> None:
            _enqueue_health_change(health_queue, change_event, agent_id, status)

        # Subscribe this connection's queue + wake event directly (no callback)
        # so the broker fans open-help requests onto it the same way health
        # transitions reach ``health_queue``.
        help_broker.subscribe(open_help_queue, change_event)

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
            # The agent ids the shell may restore windows to: live workspaces plus
            # any from the persisted last-good topology not yet re-discovered this
            # session. Lets restore decline to drop a window whose workspace is
            # merely absent from a slow/partial cold-start snapshot.
            last_restorable_ids = [str(aid) for aid in backend_resolver.list_restorable_workspace_ids()]
            yield "data: {}\n\n".format(
                json.dumps(
                    {
                        "type": "workspaces",
                        "workspaces": last_workspace_data,
                        "destroying_agent_ids": last_destroying_ids,
                        "has_accounts": has_accounts,
                        "restorable_workspace_ids": last_restorable_ids,
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
            # connection, so a steadily-STUCK workspace is redirected exactly once
            # (the 15s re-assert still re-delivers for a chrome that lost the
            # one-shot). An agent is dropped from the set when it leaves STUCK so a
            # later re-STUCK re-promotes.
            redirected_agent_ids: set[str] = set()
            if tracker is not None:
                for aid, status in tracker.snapshot_all().items():
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

                while not open_help_queue.empty():
                    help_request = open_help_queue.get_nowait()
                    yield "data: {}\n\n".format(
                        json.dumps(
                            {
                                "type": "open_help",
                                "description": help_request.description,
                                "workspace_agent_id": help_request.workspace_agent_id,
                            }
                        )
                    )

                while not health_queue.empty():
                    aid_str, status = health_queue.get_nowait()
                    # Leaving STUCK clears the redirect latch so a later re-STUCK
                    # is redirected again.
                    if status != AgentHealth.STUCK:
                        redirected_agent_ids.discard(aid_str)
                    if status == AgentHealth.STUCK:
                        redirected_agent_ids.add(aid_str)
                    yield "data: {}\n\n".format(json.dumps(_system_interface_status_payload(tracker, aid_str, status)))

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
                now = time.monotonic()
                if (
                    tracker is not None
                    and now - last_status_reassert >= _SYSTEM_INTERFACE_STATUS_REASSERT_INTERVAL_SECONDS
                ):
                    last_status_reassert = now
                    for aid, status in tracker.snapshot_all().items():
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
            help_broker.unsubscribe(open_help_queue, change_event)
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
    records = list_destroying(paths, lambda aid: is_host_still_active(backend_resolver, paths, aid))
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


# -- System-interface recovery page --
#
# The recovery page's data calls (host-health probe + the two restart tiers) are
# served by the versioned surface now (GET /api/v1/workspaces/<id>/health, POST
# /api/v1/workspaces/<id>/restart with a ``scope``), whose engine lives in
# ``workspace_recovery.py``. Only the page route and its helpers remain here.

# How long a single workspace probe through the plugin is allowed to hang.
# Used by the background system-interface-health probe loop -- we want a short,
# snappy timeout so a wedged workspace doesn't gate the recovery UI.
_WORKSPACE_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0


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
    """Render workspace settings page with account, sharing, and delete options."""
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
        is_leased_imbue_cloud=is_leased_imbue_cloud,
        current_color=current_color,
        is_stale=is_stale,
    )
    return make_html_response(content=html)


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
    sharing, agent create / destroy). Workspace links go to
    ``http://localhost:<mngr_forward_port>/goto/<agent>/`` instead of being
    routed in-process.

    ``envelope_stream_consumer`` feeds discovery events into
    ``backend_resolver`` and is also the bounce target for ``SIGHUP``-style
    re-discovery after a SuperTokens signin writes a new provider entry.

    When ``agent_creator`` is provided, the server can create new agents from
    git URLs: the create page submits to ``POST /api/v1/workspaces`` and
    ``/creating/<id>`` polls the v1 operations resource for status and logs.

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
        # Mount the self-describing OpenAPI document at /api/schema (describes the
        # gateway-reachable /api/v* surface; default-allowed for agents).
        app.register_blueprint(create_api_schema_blueprint())
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
    app.add_url_rule("/help/assist", view_func=_handle_help_assist, methods=["POST"])
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

    # Workspace settings page (the account-association and color writes it drives
    # now go through PATCH /api/v1/workspaces/<id>).
    app.add_url_rule("/workspace/<agent_id>/settings", view_func=_handle_workspace_settings)

    # Request inbox routes
    app.add_url_rule("/inbox", view_func=_handle_inbox_page)
    app.add_url_rule("/inbox/list", view_func=_handle_inbox_list_fragment)
    app.add_url_rule("/inbox/detail/<request_id>", view_func=_handle_inbox_detail_fragment)
    app.add_url_rule("/_chrome/requests-auto-open", view_func=_handle_requests_auto_open, methods=["POST"])
    app.add_url_rule("/requests/<request_id>/grant", view_func=_handle_request_grant, methods=["POST"])
    app.add_url_rule("/requests/<request_id>/deny", view_func=_handle_request_deny, methods=["POST"])

    # Sharing editor routes (used by both request approval and direct editing)
    app.add_url_rule("/sharing/<agent_id>/<service_name>", view_func=_handle_sharing_page)

    # Agent creation routes. The create form now submits to POST
    # /api/v1/workspaces and /creating/<id> polls the v1 operations resource, so
    # only the GET create-form page and the /creating/<id> progress page remain
    # here; status/logs and the form POST moved to the versioned surface.
    app.add_url_rule("/create", view_func=_handle_create_page)
    app.add_url_rule("/creating/<agent_id>", view_func=_handle_creating_page)

    # Agent destruction routes. Destroy, status/log streaming, and dismiss
    # (DELETE /api/v1/workspaces/operations/destroy/<id>) are all served by the
    # versioned /api/v1/workspaces surface now; only the detail page remains here.
    app.add_url_rule("/destroying/<agent_id>", view_func=_handle_destroying_page)

    # Workspace-recovery page. The host-health probe and the restart actions it
    # drives are served by the versioned surface now (GET
    # /api/v1/workspaces/<id>/health, POST /api/v1/workspaces/<id>/restart with a
    # ``scope``); only the page route remains here.
    app.add_url_rule("/agents/<agent_id>/recovery", view_func=_handle_recovery_page)

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

    Each tick reads the resolver's ``last_event_at`` and hands it to
    ``watchdog.evaluate``, which detects a producer stall (or a dead supervisor)
    and runs producer remediation -- bounce once, then restart on a capped
    exponential backoff, retrying forever. The thread no-ops when there is no
    concurrency group (test factories that skip background threads).
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
        last_event_at, _ = backend_resolver.get_freshness_timestamps()
        watchdog.evaluate(last_event_at)
        threading.Event().wait(timeout=_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS)
