import json
import os
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import quote

import httpx
from flask import Flask
from flask import Response
from flask import abort
from flask import request
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.desktop_client.agent_creator import probe_workspace_through_plugin
from imbue.minds.desktop_client.ai_keys import AiKeyMintError
from imbue.minds.desktop_client.ai_keys import mint_workspace_credential_blob
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.api_schema import create_api_schema_blueprint
from imbue.minds.desktop_client.api_v1 import create_api_v1_blueprint
from imbue.minds.desktop_client.assist_chat import AssistSupport
from imbue.minds.desktop_client.assist_chat import check_assist_support
from imbue.minds.desktop_client.assist_chat import spawn_assist_chat
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.create_attempt_rows import CreateAttemptRow
from imbue.minds.desktop_client.create_attempt_rows import derive_create_attempt_rows
from imbue.minds.desktop_client.data_types import RemoteWorkspaceTile
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import delete_destroying
from imbue.minds.desktop_client.destroying import is_host_still_active
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import EnvironmentCondition
from imbue.minds.desktop_client.environment_signals import SleepTracker
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import disconnect_account
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_workspace_verb_for_workspace
from imbue.minds.desktop_client.mind_liveness import compute_mind_liveness_by_agent_id
from imbue.minds.desktop_client.minds_config import DEFAULT_NOTIFICATION_STYLE
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification_feed import NotificationDispatchPreferences
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.provider_display import friendly_provider_label
from imbue.minds.desktop_client.report_collector import submit_report_with_attachments
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import find_handler_for_event
from imbue.minds.desktop_client.responses import make_html_response
from imbue.minds.desktop_client.responses import make_redirect_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.responses import safe_local_redirect_path
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import delete_share_for_host
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.state import set_state
from imbue.minds.desktop_client.static_pages import build_error_page_html
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
from imbue.minds.desktop_client.supertokens_routes import create_supertokens_blueprint
from imbue.minds.desktop_client.supertokens_routes import signout_user_via_plugin
from imbue.minds.desktop_client.supertokens_routes import wake_ui_state_publisher
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.ui_api import create_ui_blueprint
from imbue.minds.desktop_client.ui_api import serve_spa_index
from imbue.minds.desktop_client.ui_api_inbox import build_notification_card
from imbue.minds.desktop_client.ui_api_inbox import primary_agent_ids_by_workspace_name
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_login import handle_static_login_page
from imbue.minds.desktop_client.ui_models import NotificationOutcome
from imbue.minds.desktop_client.ui_models import ProviderPanelStatus
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiEnvironmentMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiProviderEntry
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher
from imbue.minds.desktop_client.webdav import create_webdav_app
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.desktop_client.workspace_record_store import is_cloud_provider_kind
from imbue.minds.desktop_client.workspace_recovery import UnattendedRecoveryDispatcher
from imbue.minds.desktop_client.workspace_recovery import is_network_dependent_workspace
from imbue.minds.desktop_client.workspace_recovery import read_backend_unreachable_verdict
from imbue.minds.desktop_client.workspace_recovery import read_device_cannot_connect_verdict
from imbue.minds.desktop_client.workspace_view_refresh import WorkspaceViewRefresher
from imbue.minds.errors import SyncCryptoError
from imbue.minds.errors import WorkspaceRecordTooNewError
from imbue.minds.errors import WorkspaceSyncError
from imbue.minds.mngr_settings.enablement import list_disabled_provider_names
from imbue.minds.mngr_settings.imbue_cloud_accounts import is_imbue_cloud_provider_enabled_for_account
from imbue.minds.mngr_settings.provider_blocks import imbue_cloud_provider_name_for_account
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.primitives import BROWSER_BRIDGE_PATH
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor


def _json_error(message: str, status_code: int) -> Response:
    """Return a small ``{"error": ...}`` JSON response."""
    return make_response(
        content=json.dumps({"error": message}),
        media_type="application/json",
        status_code=status_code,
    )


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


def _get_mngr_forward_origin() -> str:
    """Build the bare-origin URL of the ``mngr forward`` plugin.

    Used by templates to construct ``/goto/<agent>/`` URLs that target the
    plugin (which owns subdomain forwarding) rather than minds. minds always
    runs the proxy with TLS + HTTP/2, so the scheme is ``https`` and the
    rendered links reach it rather than failing a plaintext request against
    the TLS listener.
    """
    port = get_state().mngr_forward_port or 8421
    return f"https://localhost:{port}"


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


def _handle_forward_bridge() -> Response:
    """Bounce an authenticated browser into a forward-plugin session.

    The chrome page's iframe cannot be pre-set a cookie the way the Electron
    shell pre-sets the preauth cookie, so browser-mode workspace entry routes
    through here first: minds verifies its own session, then 302s to the
    plugin's ``/_bridge`` with the spawn-time secret, which sets the plugin's
    bare-origin session cookie and redirects onward to ``next`` (normally a
    ``/goto/<workspace-id>/`` workspace entry).
    """
    token = get_state().mngr_forward_browser_bridge_token
    if token is None:
        abort(404)
    if not _is_request_authenticated():
        return make_response(status_code=302, headers={"Location": "/"})
    next_path = request.args.get("next", "/")
    # Only a same-origin path may ride through (the plugin re-sanitizes too);
    # protocol-relative forms would make this an open redirector.
    if not next_path.startswith("/") or next_path.startswith("//") or next_path.startswith("/\\"):
        next_path = "/"
    location = (
        f"{_get_mngr_forward_origin()}{BROWSER_BRIDGE_PATH}"
        f"?token={quote(token, safe='')}&next={quote(next_path, safe='')}"
    )
    return make_response(status_code=302, headers={"Location": location})


def _handle_authenticate() -> Response:
    code = _required_one_time_code()

    is_valid = get_state().auth_store.validate_and_consume_code(code=code)

    if not is_valid:
        html = build_error_page_html(
            title="Sign-in failed",
            message="This login code is invalid or has already been used. "
            "Find the login URL printed where the minds app is running and open that full link.",
        )
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


def _resolved_workspace_color(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str:
    """The workspace's stored color hex, or the default for label-less workspaces.

    Workspaces created before the color picker shipped have no ``color``
    label on disk; every render surface shows them as
    ``DEFAULT_WORKSPACE_COLOR`` until the user picks a color (which
    persists the label). This helper is that rule's single home.
    """
    stored = backend_resolver.get_workspace_color(agent_id)
    return stored if stored is not None else DEFAULT_WORKSPACE_COLOR


def _handle_consent_submit() -> Response:
    """Record that the user acknowledged the error-reporting notice (POST /consent).

    The notice sits just after login, so this requires authentication. The screen is informational
    (no opt-out during the alpha), so this only marks the notice as acknowledged -- reporting stays
    on by default -- and it syncs the latchkey daemon's consent file for good measure.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        minds_config.set_error_reporting_consent_given(True)
        _sync_latchkey_forward_sentry_consent(minds_config)
    return make_response(status_code=200, content='{"ok": true}', media_type="application/json")


def _handle_error_reporting_settings() -> Response:
    """Persist the error-reporting opt-out from the Settings page (POST /_chrome/error-reporting).

    A single ``report_unexpected_errors`` boolean gates both automatic error sends and their
    log/traceback attachments. Saved live and mirrored to the latchkey daemon's consent file so the
    change takes effect without an app restart.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict) or "report_unexpected_errors" not in body:
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        minds_config.set_report_unexpected_errors(bool(body["report_unexpected_errors"]))
        _sync_latchkey_forward_sentry_consent(minds_config)
    return make_response(status_code=200, content='{"ok": true}', media_type="application/json")


def _push_new_password_state(
    record_store: WorkspaceRecordStore,
    resolver: BackendResolverInterface,
    user_id: str,
    account_email: str,
    bundle: Mapping[str, object],
) -> None:
    """A non-empty password was just set: push the new bundle + any pending secrets."""
    if record_store.cli is not None:
        record_store.cli.sync_bundle_push(account_email, bundle)
    record_store.push_all_secrets(user_id, account_email, resolver)


def _scrub_cleared_password_server_state(record_store: WorkspaceRecordStore, account_email: str) -> None:
    """The password was cleared: nothing secret may stay server-side."""
    if record_store.cli is None:
        return
    record_store.cli.sync_bundle_delete(account_email)
    record_store.cli.sync_scrub_secrets(account_email)


def _handle_backup_password_change() -> Response:
    """Change the sync master password (POST /_chrome/backup-password).

    Deliberately a desktop-only cookie-auth route (not part of /api/v1): agents
    must never be able to change the master password. The password's only role
    is wrapping each signed-in account's sync DEK: a change rewraps the DEK and
    pushes the new bundle (plus any pending secrets) to the connector; clearing
    the password deletes the server bundle and scrubs the synced secrets.
    Workspace repositories are never touched. The response carries per-account
    results for the Settings page to render inline.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    paths: InstallationPaths | None = get_state().api_v1_paths
    session_store = get_state().session_store
    if paths is None or session_store is None or session_store.record_store is None:
        return make_response(
            status_code=503,
            content='{"error": "Sync is unavailable in this configuration"}',
            media_type="application/json",
        )
    # Wrapped in SecretStr immediately; the plaintext must never reach a log.
    new_password = SecretStr(str(body.get("new_password") or ""))
    confirmation = SecretStr(str(body.get("new_password_confirm") or ""))
    if new_password.get_secret_value() != confirmation.get_secret_value():
        return make_response(
            status_code=400, content='{"error": "The two passwords do not match."}', media_type="application/json"
        )
    accounts = session_store.list_accounts()
    if not accounts:
        return make_response(
            status_code=400,
            content='{"error": "Sign in to an account first -- the master password protects synced account data."}',
            media_type="application/json",
        )
    record_store = session_store.record_store
    resolver = get_state().backend_resolver
    # Accounts that are locked on this device must unlock first: rewrapping
    # here would mint a fresh DEK and overwrite the server bundle that wraps
    # the account's real one, orphaning every already-synced secret.
    locked_user_ids = set(record_store.locked_account_user_ids([str(account.user_id) for account in accounts]))
    results: list[dict[str, object]] = []
    for account in accounts:
        if str(account.user_id) in locked_user_ids:
            results.append(
                {
                    "account": str(account.email),
                    "is_ok": False,
                    "error": "This account's synced secrets are locked on this device; "
                    "unlock them with the current master password first.",
                }
            )
            continue
        try:
            bundle = set_master_password_for_account(paths, str(account.user_id), new_password)
            if bundle is not None:
                _push_new_password_state(record_store, resolver, str(account.user_id), str(account.email), bundle)
            else:
                _scrub_cleared_password_server_state(record_store, str(account.email))
            results.append({"account": str(account.email), "is_ok": True, "error": None})
        except (SyncCryptoError, WorkspaceSyncError, ImbueCloudCliError) as exc:
            logger.warning("Master password change failed for {}: {}", account.email, exc)
            results.append({"account": str(account.email), "is_ok": False, "error": str(exc)})
    return make_response(
        status_code=200,
        content=json.dumps({"ok": all(bool(entry["is_ok"]) for entry in results), "results": results}),
        media_type="application/json",
    )


def _handle_sync_unlock() -> Response:
    """Unlock synced secrets on this device (POST /_chrome/sync-unlock).

    Tries the typed master password against every locked signed-in account's
    key bundle (fetched from the connector when no local mirror exists);
    whichever accounts it unwraps get their DEK installed. Reports which
    accounts remain locked -- they may need an older password.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    session_store = get_state().session_store
    if session_store is None or session_store.record_store is None:
        return make_response(
            status_code=503, content='{"error": "Sync is unavailable"}', media_type="application/json"
        )
    password = SecretStr(str(body.get("password") or ""))
    record_store = session_store.record_store
    accounts = session_store.list_accounts()
    locked_user_ids = record_store.locked_account_user_ids([str(account.user_id) for account in accounts])
    unlocked: list[str] = []
    still_locked: list[str] = []
    is_ssh_material_written = False
    for account in accounts:
        if str(account.user_id) not in locked_user_ids:
            continue
        if record_store.unlock_account(str(account.user_id), str(account.email), password):
            unlocked.append(str(account.email))
            # Materialize this account's synced secrets synchronously (local
            # crypto + file writes) so the page reload right after unlock
            # already renders its cloud workspaces as "connecting" instead of
            # waiting a beat for the async pass.
            is_ssh_material_written = (
                record_store.materialize_account_synced_secrets(str(account.user_id), str(account.email))
                or is_ssh_material_written
            )
        else:
            still_locked.append(str(account.email))
    scheduler = get_state().sync_scheduler
    if unlocked and scheduler is not None:
        scheduler.kick()
    if is_ssh_material_written:
        bounce_latchkey_forward_supervisor(get_state().latchkey_forward_supervisor)
    if not unlocked and still_locked:
        return make_response(
            status_code=200,
            content=json.dumps(
                {
                    "ok": False,
                    "unlocked": unlocked,
                    "still_locked": still_locked,
                    "error": "That password did not unlock any account.",
                }
            ),
            media_type="application/json",
        )
    return make_response(
        status_code=200,
        content=json.dumps({"ok": True, "unlocked": unlocked, "still_locked": still_locked}),
        media_type="application/json",
    )


def _handle_sync_initial_status() -> Response:
    """Report first-fetch progress for just-signed-in accounts (GET /_chrome/sync-initial-status).

    Backs the post-signin banner: each entry is an account that signed in on
    this device with no locally synced records yet -- PENDING while the first
    record fetch is in flight, FAILED when the last pass errored (the loop
    retries), or DONE with the fetched workspace count.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    scheduler = get_state().sync_scheduler
    statuses = scheduler.list_initial_sync_statuses() if scheduler is not None else []
    return make_response(
        status_code=200,
        content=json.dumps({"accounts": [status.model_dump(mode="json") for status in statuses]}),
        media_type="application/json",
    )


def _handle_remove_workspace_record() -> Response:
    """Remove a synced workspace record outright (POST /_chrome/workspaces/remove-record).

    The manual escape hatch for stale/confusing rows on the landing list.
    Requires connectivity (the record lives on the connector).
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    coordinate = str((body.get("workspace_id") or body.get("host_id") or "") if isinstance(body, dict) else "")
    if not coordinate:
        return make_response(
            status_code=400, content='{"error": "workspace_id is required"}', media_type="application/json"
        )
    session_store = get_state().session_store
    if session_store is None or session_store.record_store is None:
        return make_response(
            status_code=503, content='{"error": "Sync is unavailable"}', media_type="application/json"
        )
    record_store = session_store.record_store
    for account in session_store.list_accounts():
        # The coordinate is the workspace id; a legacy host id (an older
        # window's persisted state) resolves through the record's host column.
        matching = next(
            (
                record
                for record in record_store.list_records(str(account.user_id))
                if coordinate in (record.agent_id, record.host_id)
            ),
            None,
        )
        if matching is None:
            continue
        try:
            record_store.remove_record_or_raise(str(account.user_id), str(account.email), matching.agent_id)
        except WorkspaceSyncError as exc:
            return make_response(
                status_code=502, content=json.dumps({"error": str(exc)}), media_type="application/json"
            )
        return make_response(status_code=200, content='{"ok": true}', media_type="application/json")
    return make_response(status_code=404, content='{"error": "No such record"}', media_type="application/json")


def _sync_latchkey_forward_sentry_consent(minds_config: MindsConfig) -> None:
    """Rewrite the detached ``mngr latchkey forward`` daemon's live consent file after a consent change.

    The daemon reads this file live (per event) to gate what it sends, so rewriting it here is what
    makes a grant/revoke take effect on the running daemon without respawning it.
    """
    write_latchkey_forward_sentry_consent(
        latchkey_forward_sentry_consent_path(minds_config.data_dir),
        is_error_reporting_enabled=minds_config.get_report_unexpected_errors(),
    )


def _handle_help_report() -> Response:
    """Collect and submit a user-submitted bug report from the help form (POST /help/report).

    Unauthenticated for the same reason as the page: the user may be reporting a sign-in problem. An
    agent-initiated report (the ``/api/v1`` route) lands here too: that route pre-fills this same form
    rather than submitting, so the human-reviewed send always flows through this collector.

    The workspace's own logs and chat transcript are opt-out, so this route attaches them from inside
    the container, along with the shell's captured console. It never waits on that: the archive's
    upload is reserved and collected on a background strand while the user gets their report id now.
    Collection never fails the report either -- when there is no archive, a one-line note (and the
    status document the event points at) says why.
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

    event_id = submit_report_with_attachments(body=body, state=get_state())
    return make_response(
        status_code=200,
        content=json.dumps({"ok": True, "event_id": event_id}),
        media_type="application/json",
    )


def _handle_help_assist() -> Response:
    """Spawn an in-workspace ``/assist`` chat to help with a problem (POST /help/assist).

    Only valid when the help flow was opened from a loaded workspace: the body carries that
    workspace's agent id and the user's description. Before spawning, we probe the workspace for the
    ``/assist`` skill and return 409 if it lacks it (an older default workspace template) or 502 if the workspace is
    unreachable -- so we never spawn a chat that could only hang. Otherwise the desktop app runs
    ``mngr create`` inside that workspace's container (via ``mngr exec``) to spawn a new chat seeded
    with ``/assist <description>``; the system interface auto-opens its tab. The call blocks until
    ``mngr create`` finishes so the get-help modal can hold its "starting..." state until the chat
    exists, then returns 200 on success or 502 if the spawn failed.
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
            content='{"error": "Agent help is only available inside a machine"}',
            media_type="application/json",
        )
    try:
        workspace_agent_id = AgentId(workspace_agent_id_raw)
    except ValueError:
        return make_response(
            status_code=400, content='{"error": "Invalid workspace_agent_id"}', media_type="application/json"
        )

    state = get_state()
    mngr_caller = state.mngr_caller or get_default_mngr_caller()

    # Refuse before spawning if this workspace can't actually host an /assist chat.
    # Workspaces created from a DEFAULT_WORKSPACE_TEMPLATE predating the /assist skill would otherwise accept
    # the ``mngr create`` but hang on the ``/assist`` message (an unknown slash command
    # never submits a prompt, so the send blocks to its full timeout) and leave a
    # half-created chat behind. The probe is a quick filesystem check inside the
    # container; on an unsupported/unreachable workspace we return a clear error the
    # modal turns into a "report a bug instead" screen rather than a dead spinner.
    support = check_assist_support(mngr_caller, workspace_agent_id)
    if support is AssistSupport.UNSUPPORTED:
        return make_response(
            status_code=409,
            content=json.dumps(
                {"error": "This machine doesn't have the agent-assist skill, so an agent can't help here yet."}
            ),
            media_type="application/json",
        )
    if support is AssistSupport.UNREACHABLE:
        return make_response(
            status_code=502,
            content=json.dumps(
                {"error": "Couldn't reach this machine to start an agent. It may be starting up or unavailable."}
            ),
            media_type="application/json",
        )

    # Wait for the create to finish before responding so the get-help modal keeps its
    # "starting..." state until the chat exists, rather than dismissing into a blank gap
    # while the agent boots. The cheroot WSGI pool (50 threads) absorbs the blocking call.
    started = spawn_assist_chat(
        mngr_caller=mngr_caller,
        workspace_agent_id=workspace_agent_id,
        description=description,
    )
    if not started:
        return make_response(
            status_code=502,
            content=json.dumps({"error": "Could not start an agent in this machine. Please try again."}),
            media_type="application/json",
        )
    return make_response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")


def _handle_welcome_skip() -> Response:
    """Record the "Continue without an account" choice and land on home.

    Setting ``is_account_setup_skipped`` stops the home route's bounce back
    to the welcome splash (see ``_handle_landing_page``), so from here on the
    titlebar home button lands on the workspace list / create form. The flag
    is per-run; a fresh cold start of a functionally-empty app shows the
    splash again (matching the startup routing).
    """
    if not _is_request_authenticated():
        return make_response(status_code=302, headers={"Location": "/login"})
    get_state().is_account_setup_skipped = True
    return make_response(status_code=303, headers={"Location": "/"})


def _account_launcher_context(session_store: MultiAccountSessionStore | None) -> tuple[str, int]:
    """Resolve the home screen's bottom-left account launcher label.

    Returns ``(email, extra_count)``: the default (or first) signed-in
    account's email plus how many further accounts are signed in, or
    ``("", 0)`` when signed out (the launcher then reads "Log in").
    """
    accounts = session_store.list_accounts() if session_store else []
    if not accounts:
        return "", 0
    minds_config: MindsConfig | None = get_state().minds_config
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    shown = accounts[0]
    for account in accounts:
        if default_account_id is not None and str(account.user_id) == default_account_id:
            shown = account
            break
    return str(shown.email), len(accounts) - 1


def _build_account_launcher_payload(session_store: MultiAccountSessionStore | None) -> dict[str, object]:
    """The account-identity fields the `/ui/ws` ``accounts`` frame carries.

    The home screen's bottom-left launcher renders from these fields (via
    ``_derive_ui_accounts_message``), but the page stays put across a sign-out /
    sign-in / default-account switch made in a modal. Carrying the identity on
    the channel is what lets the launcher re-label itself (and flip its
    signed-in state, which decides whether clicking it opens Manage Accounts
    or the sign-in modal) without a reload. ``has_accounts`` is derived from the
    account list rather than the email so the welcome splash's self-advance
    keeps its exact "any account at all" meaning.
    """
    accounts = session_store.list_accounts() if session_store else []
    launcher_email, launcher_extra_count = _account_launcher_context(session_store)
    return {
        "has_accounts": bool(accounts),
        "account_email": launcher_email,
        "extra_account_count": launcher_extra_count,
    }


def _compute_cloud_tile_state(
    backend_resolver: BackendResolverInterface,
    record_store: WorkspaceRecordStore,
    account_email: str,
    record: ReplicaRecord,
) -> tuple[str, str | None]:
    """Derive the access state for one cloud row that is not in local discovery.

    Everything is computed from current facts (key-file presence and mtime,
    the provider's latest snapshot, the in-memory materialization error) --
    no stored flags:

    - ``""`` (plain remote): chips are suppressed while the account's provider
      block is disabled, and nothing is shown before any key is materialized
      (locked account / no synced key).
    - ``"error"``: the last materialization attempt failed (detail in tooltip).
    - ``"connecting"``: a key exists but no healthy provider snapshot has
      arrived since it appeared -- discovery has not had its chance yet.
    - ``"unreachable"``: a healthy snapshot newer than the key lacks the host
      (the lease expired/was released, or the key does not grant access).
    """
    if not is_imbue_cloud_provider_enabled_for_account(account_email, root=MindsRoot.from_environment()):
        return "", None
    error_detail = record_store.ssh_material_errors().get(record.agent_id)
    if error_detail is not None:
        return "error", error_detail
    key_path = record_store.imbue_cloud_host_ssh_key_path(account_email, record.host_id)
    if key_path is None or not key_path.is_file():
        return "", None
    provider_name = ProviderInstanceName(imbue_cloud_provider_name_for_account(account_email))
    last_snapshot_at = backend_resolver.get_last_snapshot_at_for_provider(provider_name)
    is_provider_errored = provider_name in backend_resolver.get_provider_errors()
    try:
        key_appeared_at = datetime.fromtimestamp(key_path.stat().st_mtime, timezone.utc)
    except OSError:
        return "", None
    if last_snapshot_at is None or last_snapshot_at <= key_appeared_at or is_provider_errored:
        return "connecting", None
    return "unreachable", None


def _collect_remote_workspace_tiles(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> list[RemoteWorkspaceTile]:
    """Workspaces known only from synced records (not in local discovery), for the landing list."""
    if session_store is None or session_store.record_store is None:
        return []
    # "Not in local discovery" is only meaningful once discovery has produced
    # its first complete snapshot; before that every record (including this
    # device's own workspaces) would misclassify as remote.
    if not backend_resolver.has_completed_initial_discovery():
        return []
    local_ids = {str(aid) for aid in backend_resolver.list_known_workspace_ids()}
    tiles: list[RemoteWorkspaceTile] = []
    seen_agent_ids: set[str] = set()
    for account in session_store.list_accounts():
        for record in session_store.record_store.list_records(str(account.user_id)):
            is_remote_active = (
                record.state == RECORD_STATE_ACTIVE
                and record.agent_id not in local_ids
                and record.agent_id not in seen_agent_ids
            )
            if not is_remote_active:
                continue
            seen_agent_ids.add(record.agent_id)
            location = record.device_label or record.provider_kind or "another device"
            state, state_detail = ("", None)
            if is_cloud_provider_kind(record.provider_kind):
                state, state_detail = _compute_cloud_tile_state(
                    backend_resolver, session_store.record_store, str(account.email), record
                )
            tiles.append(
                RemoteWorkspaceTile(
                    agent_id=record.agent_id,
                    name=record.display_name or record.agent_id,
                    accent=record.color or DEFAULT_WORKSPACE_COLOR,
                    location=location,
                    host_id=record.host_id,
                    state=state,
                    state_detail=state_detail,
                )
            )
    return tiles


def _build_remote_tile_states(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
) -> dict[str, str]:
    """``agent_id -> derived state`` for every remote tile (the SSE drift payload).

    A rendered remote tile whose id vanishes from this map (it flipped into
    local discovery) or whose state changed makes the landing page reload.
    """
    return {tile.agent_id: tile.state for tile in _collect_remote_workspace_tiles(backend_resolver, session_store)}


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


# -- Agent create-attempt route handlers --


# -- Agent destruction route handlers --


def _finalize_and_mark_destroying(
    paths: InstallationPaths | None,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    imbue_cloud_cli: ImbueCloudCli | None,
) -> dict[str, str]:
    """Walk ``<paths.data_dir>/destroying/``, finalize DONE records, return marker map.

    Called from the publisher's workspaces derive, which therefore owns
    DONE-record cleanup in the SPA world (the legacy landing-page render used
    to own it). Returns ``{agent_id_str: "running" | "failed"}`` for any
    in-flight or failed destroy. A destroy is DONE only once the whole *host*
    is gone (not just the workspace agent -- see
    :func:`destroying.is_host_still_active`); on DONE we disassociate the
    workspace from its account and delete the record, so the row vanishes on
    the next refresh. A FAILED destroy stays associated and visible so the
    user can retry rather than being left with an invisible, still-running
    host.

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
            _finalize_destroyed_workspace(agent_id, paths, session_store, imbue_cloud_cli)
            continue
        marker[str(agent_id)] = "running" if record.status == DestroyingStatus.RUNNING else "failed"
    return marker


def _finalize_destroyed_workspace(
    agent_id: AgentId,
    paths: InstallationPaths,
    session_store: MultiAccountSessionStore | None,
    imbue_cloud_cli: ImbueCloudCli | None,
) -> None:
    """Tombstone a fully-destroyed workspace's record, then delete the destroying marker.

    Runs only once the host is confirmed gone (DONE). The workspace record is
    kept (state=DESTROYED, secrets intact) so the workspace's backups stay
    reachable from any of the account's devices; it just disappears from the
    active UI. Tombstoning here -- rather than synchronously when the user
    clicks destroy -- means a failed or partial teardown keeps the workspace
    visible instead of hiding a host that is still running.
    """
    if session_store is not None and session_store.record_store is not None:
        found = session_store.record_store.find_active_record(str(agent_id))
        if found is not None:
            owner_user_id, record = found
            owner_email = session_store.get_account_email(owner_user_id)
            if owner_email is not None:
                # Destroying the host does not touch the account's machine
                # share -- nothing downstream of `mngr destroy` knows it
                # exists. Left behind it keeps a relay hostname reserved and
                # counts against a quota measured in machines ever created,
                # so it must go before the record that names it is tombstoned.
                delete_share_for_host(imbue_cloud_cli, owner_email, record.host_id)
                try:
                    session_store.record_store.tombstone_record(owner_user_id, owner_email, str(agent_id))
                except WorkspaceRecordTooNewError as exc:
                    # The destroy gate refuses this upfront; reaching here means
                    # the host went away some other way. The record must not be
                    # blindly tombstoned by an app that cannot read it -- a
                    # newer install (or the server) retires it instead.
                    logger.warning("Not tombstoning the record for destroyed agent {}: {}", agent_id, exc)
            else:
                logger.warning(
                    "Skipping workspace-record tombstone for destroyed agent {}: owning account {} is not "
                    "signed in on this device; the owner's next signed-in reconcile will retire the record",
                    agent_id,
                    owner_user_id,
                )
    delete_destroying(agent_id, paths)


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
    disabled_names = list_disabled_provider_names(root=MindsRoot.from_environment())
    last_event_at, last_full_snapshot_at = backend_resolver.get_freshness_timestamps()

    # Active (non-destroyed) workspace count per provider, for the panel's
    # bring-your-own-key-account Delete buttons: an account in use renders its
    # button disabled ("in use by N"). Render-time UX only -- the DELETE route's
    # own active-workspace check (409) remains the authority.
    workspace_count_by_provider: dict[str, int] = {}
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is not None and info.provider_name is not None:
            provider_name_str = str(info.provider_name)
            workspace_count_by_provider[provider_name_str] = workspace_count_by_provider.get(provider_name_str, 0) + 1

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
    # Bring-your-own-key account annotations, applied across every source bucket
    # (a byok provider can be healthy, errored, or disabled and still deletable).
    for name, entry in entry_by_name.items():
        if name.startswith("byok-"):
            entry["is_cloud_account"] = True
            entry["workspace_count"] = workspace_count_by_provider.get(name, 0)
    # Stable alphabetical order by name across all categories.
    entries = sorted(entry_by_name.values(), key=lambda entry: entry["name"])
    return {
        "providers": entries,
        "last_event_at": last_event_at.isoformat() if last_event_at is not None else None,
        "last_full_snapshot_at": last_full_snapshot_at.isoformat() if last_full_snapshot_at is not None else None,
    }


def _visible_create_attempt_rows(backend_resolver: BackendResolverInterface) -> list[CreateAttemptRow]:
    """Derive the create attempt rows currently visible in the workspace list.

    Merges the live in-memory create attempts with the persisted pending-create-attempt
    records (interrupted / failed rows from previous sessions), dropping any
    create attempt whose workspace a discovery snapshot has already confirmed --
    the real workspace row takes over in place.
    """
    agent_creator: AgentCreator | None = get_state().agent_creator
    if agent_creator is None:
        return []
    store = agent_creator.pending_create_attempt_store
    records = store.list_records() if store is not None else []
    known_agent_id_strs = frozenset(str(aid) for aid in backend_resolver.list_known_workspace_ids())
    return derive_create_attempt_rows(agent_creator.list_create_attempt_infos(), records, known_agent_id_strs)


def _create_attempt_row_entry(row: CreateAttemptRow) -> dict[str, str]:
    """One create attempt row as a chrome-SSE ``workspaces`` entry.

    ``id`` is the create attempt id (the row's stable identity until the workspace
    row takes over); ``create_attempt_state`` is what the row builders badge on and
    what routes a click to ``/creating/<id>`` instead of ``/goto/``.
    """
    entry: dict[str, str] = {
        "id": row.create_attempt_id,
        "name": row.display_name,
        "accent": row.color or DEFAULT_WORKSPACE_COLOR,
        "create_attempt_state": row.kind.value.lower(),
    }
    if row.account_email:
        entry["account"] = row.account_email
    return entry


def _build_workspace_list(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None = None,
    # Supplies each workspace's outage onset, which is what the backend-unreachable
    # verdict is freshness-gated against. Optional so this builder stays callable
    # without a tracker; without one the verdict falls back to absolute snapshot age.
    tracker: SystemInterfaceHealthTracker | None = None,
    # In-flight / interrupted / failed create attempt rows to merge into the list
    # (see ``_visible_create_attempt_rows``). A parameter -- not read from the app
    # state here -- so this builder stays callable outside a request context.
    create_attempt_rows: Sequence[CreateAttemptRow] | None = None,
) -> list[dict[str, str]]:
    """Build a JSON-serializable list of workspaces from the backend resolver.

    Each entry carries an ``accent`` (#rrggbb CSS color) for the chrome and
    sidebar to render. The accent is the workspace's stored ``color`` label
    (set at create time by the create-form picker, or via the settings POST
    endpoint); workspaces that lack the label (i.e. they were created before
    the picker shipped and the user hasn't repicked yet) get the default
    workspace color. The contrasting titlebar foreground is no longer sent --
    the chrome derives it from the accent in pure CSS (``.titlebar-surface``).

    Entries whose backend the recovery verdict reads as unreachable carry
    ``is_backend_unreachable="true"``, which is how the recovery band knows it
    may name the backend instead of reporting a generic loss of contact. It is
    the card's own verdict (``read_backend_unreachable_verdict``), not a raw
    "this provider's last poll errored" flag, so the band and the card cannot
    disagree -- in particular a provider error latched from an earlier episode
    does not get to explain a fresh outage. An entry whose provider discovery has
    named also carries that provider's friendly name as ``provider_label``, which
    is what the band renders and what the machines list badges on its own.

    Entries the forward could not reach from *this device* -- a tunnel it could
    not build, its own connection pool exhausted -- carry
    ``is_device_cannot_connect="true"``, from the card's other verdict and for
    the same reason: the band would otherwise report a machine that is probably
    running fine as lost.

    Shutdown-capable minds (those on a provider whose host minds can stop/start,
    see :func:`provider_backend_supports_shutdown`) additionally carry
    ``supports_shutdown="true"`` and a ``liveness`` of RUNNING / STOPPED /
    STOPPING / STARTING / UNKNOWN. Container liveness rides here rather than
    on a separate SSE channel: a liveness change makes the entry differ, so
    the existing ``workspaces`` diff pushes it. Non-capable minds carry
    neither field.
    """
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
            # Content URLs are keyed by the workspace id itself; host_id (the
            # current machine) rides along for legacy-coordinate resolution
            # and machine-scoped affordances.
            "host_id": str(info.host_id) if info is not None else "",
            "name": ws_name,
            "accent": accent,
        }
        # The recovery card's verdict, run per row so the band reports the same
        # thing the card behind "Open recovery" will. Read here rather than
        # derived from the error map directly because the verdict is
        # freshness-gated, and the gate needs this workspace's outage onset.
        if read_backend_unreachable_verdict(aid, backend_resolver=backend_resolver, tracker=tracker) is not None:
            entry["is_backend_unreachable"] = "true"
        # The other verdict that explains the row's health instead of restating
        # it, carried for the same reason: the band must not report a generic
        # loss of contact for a machine that is very likely fine.
        if read_device_cannot_connect_verdict(aid, tracker=tracker) is not None:
            entry["is_device_cannot_connect"] = "true"
        if info is not None and info.provider_name is not None:
            entry["provider_label"] = friendly_provider_label(info.provider_name)
        # Whether this device's own connectivity can say anything about this
        # machine at all. The band needs it to know when *not* to speak: a
        # docker container answers over loopback with the wifi off, so blaming
        # the network for its outage is both wrong and a route to a card with no
        # restart button. Emitted on every real row, so an absent key means a
        # row that has no backend (a create attempt, or a machine on another
        # device) and keeps the conservative default.
        entry["is_network_dependent"] = "true" if is_network_dependent_workspace(backend_resolver, aid) else "false"
        liveness = liveness_by_agent_id.get(str(aid))
        if liveness is not None:
            entry["supports_shutdown"] = "true"
            entry["liveness"] = liveness.value
        if session_store is not None:
            account = session_store.get_account_for_workspace(str(aid))
            if account is not None:
                entry["account"] = account.email
        workspaces.append(entry)
    # In-flight / interrupted / failed create attempts ride in the same list,
    # badged via ``create_attempt_state``, so they sit inline with the finished
    # workspaces (not in a separate section) and hand off to the real row
    # in place once discovery confirms the workspace.
    for create_attempt_row in create_attempt_rows or ():
        workspaces.append(_create_attempt_row_entry(create_attempt_row))
    # Append workspaces known only from synced records (hosted on another
    # device). They render greyed and non-navigable; ``location`` names where
    # they live.
    for tile in _collect_remote_workspace_tiles(backend_resolver, session_store):
        remote_entry: dict[str, str] = {
            "id": tile.agent_id,
            "name": tile.name,
            "accent": tile.accent,
            "is_remote": "true",
            "location": tile.location,
        }
        owner = session_store.get_account_for_workspace(tile.agent_id) if session_store is not None else None
        if owner is not None:
            remote_entry["account"] = owner.email
        workspaces.append(remote_entry)
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

    The chrome's live request UI (badge, panel refresh) must react to any
    change in the *set* of pending requests, not merely its size. A bare
    count is a lossy summary: if one request is resolved while another
    arrives, the count is unchanged even though the inbox contents are not.
    Keying updates off the count therefore silently drops those transitions.

    To make change detection sound, we surface the actual pending request
    ids (in a deterministic order) alongside the count. Consumers diff
    ``request_ids`` to decide whether to refresh the panel; the count remains
    for the badge.

    Requests whose host can't be resolved are excluded (see
    :func:`_displayable_pending_requests`) so the badge count and the
    rendered cards stay in agreement.
    """
    pending = _displayable_pending_requests(inbox, backend_resolver)
    request_ids = [str(req.event_id) for req in pending]
    return {"count": len(request_ids), "request_ids": request_ids}


# -- System-interface health probing --
#
# The probe loop's own timeout is all that lives here. The recovery page's route
# is registered with the rest of the SPA routes further down, and its data calls
# are served elsewhere: the restart tiers by the versioned surface (POST
# /api/v1/workspaces/<id>/restart with a ``scope``), and the polled verdicts the
# card renders by ``ui_api_lifecycle``'s recovery-info read. Both run off
# ``workspace_recovery.py`` and the health tracker, with no command of their own.

# How long a single workspace probe through the plugin is allowed to hang.
# Used by the background system-interface-health probe loop -- we want a short,
# snappy timeout so a wedged workspace doesn't gate the recovery UI.
_WORKSPACE_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0


# -- Account management routes --


def _handle_account_trim_backups(user_id: str) -> Response:
    """Start the over-quota backup trim flow for one account (idempotent while running)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    paths: InstallationPaths | None = get_state().api_v1_paths
    account = next(
        (a for a in (session_store.list_accounts() if session_store else []) if str(a.user_id) == user_id),
        None,
    )
    if account is None or cli is None or paths is None:
        # The SPA reloads /accounts and reads errors from the response; the
        # legacy re-render of the accounts page died with the JinjaX pages.
        return make_response(status_code=409, content="Account not found or imbue_cloud CLI unavailable.")
    get_state().backup_trim_manager.start_trim(
        user_id=user_id,
        account_email=str(account.email),
        cli=cli,
        paths=paths,
        notification_dispatcher=get_state().notification_dispatcher,
    )
    return make_response(status_code=303, headers={"Location": "/accounts"})


def _handle_account_set_plan(user_id: str) -> Response:
    """Switch an account's plan; on failure return the server's reason for the SPA to surface."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    plan = str(request.form.get("plan", "")).strip()
    if not plan:
        return make_response(status_code=422, content="No plan selected.")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    account = next(
        (a for a in (session_store.list_accounts() if session_store else []) if str(a.user_id) == user_id),
        None,
    )
    if account is None or cli is None:
        return make_response(status_code=409, content="Account not found or imbue_cloud CLI unavailable.")
    try:
        cli.set_account_plan(str(account.email), plan)
    except ImbueCloudEmailNotVerifiedCliError as exc:
        # The verification-gated refusal is the contextual trigger for the
        # verification email: auto-send it (the connector's per-user cooldown
        # bounds repeats) and return the structured 403 so the SPA renders the
        # "we just sent a link to ..." prompt with a resend button.
        email = exc.email or str(account.email)
        is_sent = _send_verification_email_best_effort(cli, email)
        return make_response(
            status_code=403,
            content=json.dumps({"code": "email_not_verified", "email": email, "sent": is_sent}),
            media_type="application/json",
        )
    except ImbueCloudCliError as exc:
        # The connector's reason (e.g. "requires partner access") is the
        # user-facing explanation -- surface it plainly.
        return make_response(status_code=502, content=f"Could not switch {account.email} to '{plan}': {exc}")
    return make_response(status_code=303, headers={"Location": "/accounts"})


def _send_verification_email_best_effort(cli: ImbueCloudCli, email: str) -> bool:
    """Send the verification email for ``email``; False when suppressed or failed.

    Best-effort: the caller's response already tells the user a link is on the
    way (or to use resend), so a failed send must not turn the whole
    plan-switch response into an error.
    """
    try:
        return cli.auth_resend_verification(email)
    except ImbueCloudCliError as exc:
        logger.warning("Could not send the verification email for {}: {}", email, exc)
        return False


def _handle_account_resend_verification(user_id: str) -> Response:
    """(Re-)send the verification email for one signed-in account (the SPA's resend button)."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    account = next(
        (a for a in (session_store.list_accounts() if session_store else []) if str(a.user_id) == user_id),
        None,
    )
    if account is None or cli is None:
        return make_response(status_code=409, content="Account not found or imbue_cloud CLI unavailable.")
    is_sent = _send_verification_email_best_effort(cli, str(account.email))
    return make_response(
        status_code=200,
        content=json.dumps({"sent": is_sent, "email": str(account.email)}),
        media_type="application/json",
    )


def _find_predefined_permission_handler() -> LatchkeyPermissionGrantHandler | None:
    """Return the registered predefined-permission handler, or ``None`` if absent.

    The handler owns the latchkey gateway client, the services catalog, and the
    :class:`Latchkey` wrapper the permissions settings section needs. It is
    registered in ``request_event_handlers`` at startup; minimal setups (some
    tests) may omit it, in which case the permissions section renders empty.
    """
    for handler in get_state().request_event_handlers:
        if isinstance(handler, LatchkeyPermissionGrantHandler):
            return handler
    return None


def _workspace_record_store() -> WorkspaceRecordStore | None:
    """The workspace-record store, when the sync machinery is configured."""
    sync_scheduler = get_state().sync_scheduler
    return None if sync_scheduler is None else sync_scheduler.record_store


def _handle_mint_ai_key() -> Response:
    """Mint a LiteLLM key for a workspace (POST /settings/ai-keys/mint).

    JSON body: ``{"workspace": "<workspace_id>"}`` (a machine's host id is
    also accepted while in-workspace deep links transition). Returns
    ``{"credentials": ...}`` (the env-var-style blob the workspace modal
    expects) on success, or ``{"error": ...}`` with a matching status code.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error": "Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True) or {}
    workspace_coordinate = str(body.get("workspace", "")).strip()
    if not workspace_coordinate:
        return make_response(
            status_code=400,
            content=json.dumps({"error": "Missing 'workspace' (the workspace id)"}),
            media_type="application/json",
        )
    resolved = resolve_workspace_account(workspace_coordinate, _workspace_record_store(), get_state().session_store)
    if resolved is None:
        return make_response(
            status_code=400,
            content=json.dumps(
                {
                    "error": "This machine has no associated Imbue account. Associate one on the "
                    "machine's settings page first."
                }
            ),
            media_type="application/json",
        )
    imbue_cloud_cli = get_state().imbue_cloud_cli
    if imbue_cloud_cli is None:
        return make_response(
            status_code=501,
            content=json.dumps({"error": "Imbue Cloud is not configured on this install"}),
            media_type="application/json",
        )
    try:
        credential_blob = mint_workspace_credential_blob(
            workspace_id=resolved.workspace_id,
            account_email=resolved.account_email,
            imbue_cloud_cli=imbue_cloud_cli,
        )
    except AiKeyMintError as exc:
        logger.warning("LiteLLM key mint failed for workspace {}: {}", resolved.workspace_id, exc)
        return make_response(status_code=502, content=json.dumps({"error": str(exc)}), media_type="application/json")
    return make_response(
        status_code=200, content=json.dumps({"credentials": credential_blob}), media_type="application/json"
    )


# The revoke routes below (predefined services, file sharing, workspace
# delegation; per-workspace and across-all-workspaces) share the same plumbing.
# ``_revoke_prelude`` does auth + body parsing + locating the
# predefined-permission handler (which owns the shared gateway client +
# latchkey); ``_apply_revoke`` runs the route-specific revoke and maps its two
# failure modes to status codes. Each route is then a short, linear body that
# extracts its fields between the two.


def _revoke_prelude() -> Response | tuple[Mapping[str, Any], LatchkeyPermissionGrantHandler]:
    """Auth + JSON-body + handler lookup shared by the revoke routes.

    Returns an error :class:`Response` (403 unauthenticated, 400 invalid body,
    503 when the predefined-permission handler is unavailable), or ``(body,
    handler)`` on success.
    """
    if not _is_request_authenticated():
        return make_response(status_code=403, content='{"error":"Not authenticated"}', media_type="application/json")
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_response(status_code=400, content='{"error": "Invalid JSON body"}', media_type="application/json")
    handler = _find_predefined_permission_handler()
    if handler is None:
        return _json_error("Permission management is unavailable", status_code=503)
    return body, handler


def _apply_revoke(revoke: Callable[..., object], **kwargs: Any) -> Response:
    """Run a revoke call and map its outcome to an HTTP response (its return value is ignored).

    :class:`PermissionOverviewError` (bad request / unresolvable target) -> 400;
    :class:`LatchkeyGatewayClientError` (gateway unreachable) -> 502; success ->
    ``200 {"status": "ok"}``.
    """
    try:
        revoke(**kwargs)
    except PermissionOverviewError as e:
        return _json_error(str(e), status_code=400)
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not revoke through the latchkey gateway: {}", e)
        return _json_error(f"Could not revoke through the latchkey gateway: {e}", status_code=502)
    return make_response(content='{"status": "ok"}', media_type="application/json")


def _handle_revoke_service_for_workspace() -> Response:
    """Revoke one connector account's grants for one workspace (POST /settings/permissions/revoke).

    Body: ``{"workspace_agent_id": "...", "service_name": "...", "account": "..."}``
    (the unnamed default account is the empty string). Removes the account-scoped
    rule of every scope the service owns from that workspace's host permissions
    file, leaving the service's other accounts and the stored credentials
    untouched.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    workspace_agent_id = str(body.get("workspace_agent_id", ""))
    service_name = str(body.get("service_name", ""))
    if not workspace_agent_id or not service_name or "account" not in body:
        return _json_error("workspace_agent_id, service_name and account are required.", status_code=400)
    return _apply_revoke(
        revoke_service_account_for_workspace,
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        services_catalog=handler.services_catalog,
        latchkey=handler.latchkey,
        workspace_agent_id=workspace_agent_id,
        service_name=service_name,
        account=str(body.get("account", "")),
    )


def _handle_revoke_service_for_all_workspaces() -> Response:
    """Revoke one connector account's grants everywhere (POST /settings/permissions/revoke-all).

    Body: ``{"service_name": "...", "account": "..."}``.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    service_name = str(body.get("service_name", ""))
    if not service_name or "account" not in body:
        return _json_error("service_name and account are required.", status_code=400)
    return _apply_revoke(
        revoke_service_account_for_all_workspaces,
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        services_catalog=handler.services_catalog,
        latchkey=handler.latchkey,
        service_name=service_name,
        account=str(body.get("account", "")),
    )


def _handle_revoke_file_sharing_for_workspace() -> Response:
    """Revoke all file-sharing grants for one workspace (POST /settings/permissions/file-sharing/revoke).

    Body: ``{"workspace_agent_id": "..."}``. Removes every ``minds-file-server-*``
    permission from that workspace's host file, leaving unrelated permissions
    intact.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    workspace_agent_id = str(body.get("workspace_agent_id", ""))
    if not workspace_agent_id:
        return _json_error("workspace_agent_id is required.", status_code=400)
    return _apply_revoke(
        revoke_file_sharing_for_workspace,
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        latchkey=handler.latchkey,
        workspace_agent_id=workspace_agent_id,
    )


def _handle_revoke_file_sharing_for_all_workspaces() -> Response:
    """Revoke file-sharing grants across every active workspace (POST /settings/permissions/file-sharing/revoke-all).

    Takes no body parameters.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    _, handler = prelude
    return _apply_revoke(
        revoke_file_sharing_for_all_workspaces,
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        latchkey=handler.latchkey,
    )


def _handle_revoke_workspace_delegation_verb() -> Response:
    """Revoke one cross-workspace-management verb for one granting workspace.

    Route: POST /settings/permissions/workspace/revoke. Body:
    ``{"workspace_agent_id": "...", "verb": "minds-workspaces-<verb>"}``. Removes
    that verb across every target it was granted on for the given workspace.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    workspace_agent_id = str(body.get("workspace_agent_id", ""))
    verb = str(body.get("verb", ""))
    if not workspace_agent_id or not verb:
        return _json_error("workspace_agent_id and verb are required.", status_code=400)
    return _apply_revoke(
        revoke_workspace_verb_for_workspace,
        backend_resolver=get_state().backend_resolver,
        gateway_client=handler.gateway_client,
        latchkey=handler.latchkey,
        workspace_agent_id=workspace_agent_id,
        verb_permission=verb,
    )


def _handle_add_connector_account() -> Response:
    """Sign in to a new account for a connector service (POST /settings/connectors/add-account).

    Body: ``{"service_name": "..."}``. Runs the ephemeral-browser sign-in
    (:meth:`Latchkey.add_account`) synchronously -- exactly like clicking Approve
    on a permission request whose service has no credentials yet, but starting
    from a fresh browser session so the user can add a *new* account. Blocks
    until the browser flow finishes; the settings page reloads on success.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    service_name = str(body.get("service_name", ""))
    if not service_name:
        return _json_error("service_name is required.", status_code=400)
    is_success, detail = handler.latchkey.add_account(service_name)
    if not is_success:
        return _json_error(detail or "Sign-in did not complete.", status_code=502)
    return make_response(content='{"status": "ok"}', media_type="application/json")


def _handle_disconnect_connector_account() -> Response:
    """Disconnect one account from a connector service (POST /settings/connectors/disconnect-account).

    Body: ``{"service_name": "...", "account": "..."}`` (the default account is the
    empty string). Clears that account's stored credentials and then strips the
    account's now-inert grants from every workspace, so reconnecting the same
    account later starts from no permissions rather than silently resurrecting
    the old ones. The cleanup runs in the background and the route returns
    immediately.
    """
    prelude = _revoke_prelude()
    if isinstance(prelude, Response):
        return prelude
    body, handler = prelude
    service_name = str(body.get("service_name", ""))
    account = str(body.get("account", ""))
    if not service_name:
        return _json_error("service_name is required.", status_code=400)
    try:
        disconnect_account(handler.latchkey, service_name, account)
    except PermissionOverviewError as e:
        return _json_error(str(e), status_code=502)
    _revoke_service_account_for_all_workspaces_in_background(handler, service_name, account)
    return make_response(content='{"status": "ok"}', media_type="application/json")


def _revoke_service_account_for_all_workspaces_in_background(
    handler: LatchkeyPermissionGrantHandler,
    service_name: str,
    account: str,
) -> None:
    """Fire off ``revoke_service_account_for_all_workspaces`` on a daemon thread.

    A disconnected account's grants have no credentials to back them, so we
    strip them from every workspace's host file. This touches one gateway call
    per active host, so it runs off the request thread to keep the Disconnect
    click responsive; failures are logged rather than surfaced (the grants are
    inert without credentials, and the user can retry via the account's "Revoke
    all").
    """
    backend_resolver = get_state().backend_resolver
    threading.Thread(
        target=_run_revoke_service_account_for_all_workspaces,
        args=(backend_resolver, handler, service_name, account),
        name=f"revoke-all-{service_name}",
        daemon=True,
    ).start()


def _run_revoke_service_account_for_all_workspaces(
    backend_resolver: BackendResolverInterface,
    handler: LatchkeyPermissionGrantHandler,
    service_name: str,
    account: str,
) -> None:
    """Body of the thread spawned by :func:`_revoke_service_account_for_all_workspaces_in_background`."""
    try:
        revoke_service_account_for_all_workspaces(
            backend_resolver=backend_resolver,
            gateway_client=handler.gateway_client,
            services_catalog=handler.services_catalog,
            latchkey=handler.latchkey,
            service_name=service_name,
            account=account,
        )
    except (PermissionOverviewError, LatchkeyGatewayClientError) as e:
        logger.warning(
            "Background revoke-all for {} account {!r} after disconnect failed: {}", service_name, account, e
        )


def _handle_set_default_account() -> Response:
    """Set the default account for new workspaces."""
    if not _is_request_authenticated():
        return make_response(status_code=403, content="Not authenticated")
    form = request.form
    user_id = str(form.get("user_id", ""))
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config and user_id:
        minds_config.set_default_account_id(user_id)
        # The home screen's account launcher shows the default account, so the
        # open windows behind this modal need to re-read it.
        wake_ui_state_publisher()
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


# -- Inbox routes --


def _handle_sharing_redirect(
    agent_id: str,
    service_name: str = "",
) -> Response:
    """Redirect a legacy sharing-editor URL to the options panel's Share tab.

    The standalone editor is gone -- the Share machine pane in the workspace
    options panel is the one sharing surface -- but its URLs were handed out
    (workspace settings links, permission-request approvals), so they land on
    the replacement instead of a 404. A service segment picks that target.
    """
    # The old /modal spelling is a rendering variant, not a share target.
    target = f"&target={quote(service_name)}" if service_name and service_name != "modal" else ""
    return make_redirect_response(f"/workspace/{quote(agent_id)}/options?tab=share{target}", status_code=302)


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
    inbox badge UX).

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


# -- /ui channel publisher wiring --


def _ui_workspace_entry_from_legacy_dict(entry: Mapping[str, str]) -> UiWorkspaceEntry:
    """Convert one ``_build_workspace_list`` row into the typed channel entry.

    The legacy SSE encoded flags as ``"true"`` strings; the channel models use
    real booleans. Field semantics are identical.
    """
    return UiWorkspaceEntry(
        id=entry["id"],
        name=entry["name"],
        accent=entry["accent"],
        host_id=entry.get("host_id", ""),
        is_backend_unreachable=entry.get("is_backend_unreachable") == "true",
        is_device_cannot_connect=entry.get("is_device_cannot_connect") == "true",
        provider_label=entry.get("provider_label", ""),
        is_network_dependent=entry.get("is_network_dependent", "true") == "true",
        supports_shutdown=entry.get("supports_shutdown") == "true",
        liveness=entry.get("liveness", ""),
        account=entry.get("account", ""),
        create_attempt_state=entry.get("create_attempt_state", ""),
        is_remote=entry.get("is_remote") == "true",
        location=entry.get("location", ""),
    )


def _ui_provider_entry_from_legacy_dict(entry: Mapping[str, Any]) -> UiProviderEntry:
    return UiProviderEntry(
        name=entry["name"],
        backend=entry.get("backend"),
        # The legacy panel payload uses lowercase buckets; the wire enum is
        # the repo-standard uppercase form.
        status=ProviderPanelStatus(str(entry["status"]).upper()),
        is_enabled=bool(entry.get("is_enabled", True)),
        error_type=entry.get("error_type"),
        error_message=entry.get("error_message"),
        is_cloud_account=bool(entry.get("is_cloud_account", False)),
        workspace_count=int(entry.get("workspace_count", 0)),
    )


def _derive_ui_workspaces_message(
    app: Flask,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    paths: InstallationPaths | None,
) -> UiWorkspacesMessage:
    with app.app_context():
        rows = _build_workspace_list(
            backend_resolver,
            session_store,
            tracker=get_state().system_interface_health_tracker,
            create_attempt_rows=_visible_create_attempt_rows(backend_resolver),
        )
        restorable_ids = [str(aid) for aid in backend_resolver.list_restorable_workspace_ids()] + [
            str(hid) for hid in backend_resolver.list_restorable_workspace_host_ids()
        ]
        # The derive owns DONE-destroy cleanup: finalizing here (rather than in
        # any request handler) is what deletes the destroying marker, the
        # machine's share, and the account association once the host is gone.
        destroying_marker = _finalize_and_mark_destroying(
            paths, backend_resolver, session_store, get_state().imbue_cloud_cli
        )
        return UiWorkspacesMessage(
            workspaces=tuple(_ui_workspace_entry_from_legacy_dict(row) for row in rows),
            destroying_agent_ids=tuple(destroying_marker),
            restorable_workspace_ids=tuple(restorable_ids),
            remote_workspace_states=_build_remote_tile_states(backend_resolver, session_store),
        )


def _derive_ui_accounts_message(app: Flask, session_store: MultiAccountSessionStore | None) -> UiAccountsMessage:
    with app.app_context():
        payload = _build_account_launcher_payload(session_store)
        extra_account_count = payload["extra_account_count"]
        if not isinstance(extra_account_count, int):
            raise SwitchError(f"Account launcher payload carried a non-int extra_account_count: {payload!r}")
        return UiAccountsMessage(
            has_accounts=bool(payload["has_accounts"]),
            account_email=str(payload["account_email"]),
            extra_account_count=extra_account_count,
        )


def _derive_ui_providers_message(app: Flask, backend_resolver: BackendResolverInterface) -> UiProvidersMessage:
    with app.app_context():
        payload = _build_providers_state_payload(backend_resolver)
        return UiProvidersMessage(
            providers=tuple(_ui_provider_entry_from_legacy_dict(entry) for entry in payload["providers"]),
            last_event_at=payload["last_event_at"],
            last_full_snapshot_at=payload["last_full_snapshot_at"],
        )


def _derive_ui_requests_message(
    app: Flask,
    backend_resolver: BackendResolverInterface,
) -> UiRequestsMessage:
    with app.app_context():
        payload = _build_requests_payload(get_state().request_inbox, backend_resolver)
        return UiRequestsMessage(count=payload["count"], request_ids=tuple(payload["request_ids"]))


# How a recorded response's status becomes a feed outcome. A status outside
# this mapping (malformed on-disk event) maps to no outcome at all, which
# leaves the entry to close via the vanished-request rule.
_RESOLVED_OUTCOME_BY_RESPONSE_STATUS: Final[dict[str, NotificationOutcome]] = {
    str(RequestStatus.GRANTED): NotificationOutcome.APPROVED,
    str(RequestStatus.DENIED): NotificationOutcome.DENIED,
}


def _derive_ui_notifications_message(
    app: Flask,
    backend_resolver: BackendResolverInterface,
) -> UiNotificationsMessage:
    """Reconcile the notification feed against the current pending-request view.

    Uses the same displayable-pending set as the requests frame (so the feed
    and the badge always agree on what exists) and the same card derivation
    as the inbox list (so a feed entry renders exactly like its inbox row).
    """
    with app.app_context():
        state = get_state()
        feed = state.notification_feed
        if feed is None:
            return UiNotificationsMessage(entries=(), unresolved_count=0)
        inbox = state.request_inbox
        pending = _displayable_pending_requests(inbox, backend_resolver)
        primary_agent_id_by_ws_name = primary_agent_ids_by_workspace_name(backend_resolver)
        pending_cards = tuple(
            build_notification_card(req, state.request_event_handlers, backend_resolver, primary_agent_id_by_ws_name)
            for req in pending
        )
        responses_by_request_id: dict[str, NotificationOutcome] = {}
        for response in inbox.responses if inbox is not None else ():
            outcome = _RESOLVED_OUTCOME_BY_RESPONSE_STATUS.get(response.status)
            if outcome is not None:
                responses_by_request_id[response.request_event_id] = outcome
        return feed.reconcile(
            pending_cards=pending_cards,
            responses_by_request_id=responses_by_request_id,
        )


def _derive_ui_discovery_health_message(
    discovery_health_watchdog: DiscoveryHealthWatchdog | None,
) -> UiDiscoveryHealthMessage:
    health = discovery_health_watchdog.get_health() if discovery_health_watchdog else DiscoveryHealth.HEALTHY
    return UiDiscoveryHealthMessage(state=health)


def _derive_ui_environment_message(
    connectivity_detector: ConnectivityDetector | None,
) -> UiEnvironmentMessage:
    """The device's own condition, read without touching the network.

    Whatever the last probe found. NONE where no detector is wired, and UNKNOWN
    before the first probe lands or after a wake blanks the reading -- an
    unmeasured device must be reported as neither broken nor fine, since a
    surface told it is fine goes on to blame whatever is next in line.
    """
    condition = (
        connectivity_detector.get_reading().environment_condition
        if connectivity_detector is not None
        else EnvironmentCondition.NONE
    )
    return UiEnvironmentMessage(state=condition)


def _derive_ui_health_states(
    system_interface_health_tracker: SystemInterfaceHealthTracker | None,
) -> tuple[UiHealthMessage, ...]:
    if system_interface_health_tracker is None:
        return ()
    return tuple(
        _ui_health_message(system_interface_health_tracker, str(agent_id), status)
        for agent_id, status in system_interface_health_tracker.snapshot_all().items()
    )


class _LegacyUiStateDeriver(MutableModel):
    """Bridges the legacy payload builders to the typed channel derive callables.

    Bound methods of this holder are handed to :class:`UiStatePublisher`; each
    enters an app context (the helpers read ``get_state()``) so the
    publisher's background strand can call them without a request. When the
    legacy SSE helpers are deleted, their bodies move here wholesale.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    flask_app: Flask = Field(frozen=True, description="App whose context the derive helpers need")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Discovery resolver")
    session_store: MultiAccountSessionStore | None = Field(frozen=True, description="Account sessions")
    paths: InstallationPaths | None = Field(frozen=True, description="Workspace data paths")
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = Field(
        frozen=True, description="Per-workspace health tracker"
    )
    discovery_health_watchdog: DiscoveryHealthWatchdog | None = Field(
        frozen=True, description="Discovery pipeline watchdog"
    )
    connectivity_detector: ConnectivityDetector | None = Field(
        frozen=True, description="This device's own connectivity condition"
    )

    def derive_workspaces(self) -> UiWorkspacesMessage:
        return _derive_ui_workspaces_message(self.flask_app, self.backend_resolver, self.session_store, self.paths)

    def derive_accounts(self) -> UiAccountsMessage:
        return _derive_ui_accounts_message(self.flask_app, self.session_store)

    def derive_providers(self) -> UiProvidersMessage:
        return _derive_ui_providers_message(self.flask_app, self.backend_resolver)

    def derive_requests(self) -> UiRequestsMessage:
        return _derive_ui_requests_message(self.flask_app, self.backend_resolver)

    def derive_notifications(self) -> UiNotificationsMessage:
        return _derive_ui_notifications_message(self.flask_app, self.backend_resolver)

    def derive_discovery_health(self) -> UiDiscoveryHealthMessage:
        return _derive_ui_discovery_health_message(self.discovery_health_watchdog)

    def derive_environment(self) -> UiEnvironmentMessage:
        return _derive_ui_environment_message(self.connectivity_detector)

    def derive_health_states(self) -> tuple[UiHealthMessage, ...]:
        return _derive_ui_health_states(self.system_interface_health_tracker)


def _create_ui_state_publisher(
    app: Flask,
    broadcaster: UiChannelBroadcaster,
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    paths: InstallationPaths | None,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None,
    discovery_health_watchdog: DiscoveryHealthWatchdog | None,
    connectivity_detector: ConnectivityDetector | None,
) -> UiStatePublisher:
    """Build the channel publisher from the same derivation helpers the legacy SSE uses."""
    deriver = _LegacyUiStateDeriver(
        flask_app=app,
        backend_resolver=backend_resolver,
        session_store=session_store,
        paths=paths,
        system_interface_health_tracker=system_interface_health_tracker,
        discovery_health_watchdog=discovery_health_watchdog,
        connectivity_detector=connectivity_detector,
    )
    return UiStatePublisher(
        broadcaster=broadcaster,
        derive_workspaces=deriver.derive_workspaces,
        derive_accounts=deriver.derive_accounts,
        derive_providers=deriver.derive_providers,
        derive_requests=deriver.derive_requests,
        derive_notifications=deriver.derive_notifications,
        derive_discovery_health=deriver.derive_discovery_health,
        derive_environment=deriver.derive_environment,
        derive_health_states=deriver.derive_health_states,
    )


def _ui_health_message(tracker: SystemInterfaceHealthTracker, agent_id: str, status: AgentHealth) -> UiHealthMessage:
    """The channel twin of ``_system_interface_status_payload``."""
    error: str | None = None
    is_restart_a_no_op = False
    is_restart_start_only: bool | None = None
    if status == AgentHealth.RESTART_FAILED:
        error = tracker.get_last_restart_error(AgentId(agent_id))
        # Only read on the terminal state, which is the only one whose copy
        # turns on it -- everywhere else the episode is still in progress and
        # the start may yet report.
        is_restart_a_no_op = tracker.is_restart_a_no_op(AgentId(agent_id))
    if status == AgentHealth.RESTARTING:
        # The shape of a restart is news only while one is running, which is the
        # only state whose copy turns on it. The tracker gates it the same way,
        # so a frame that raced the episode's end reads None and the surfaces
        # fall back to the weaker claim rather than to a stale one.
        is_restart_start_only = tracker.get_restart_is_start_only(AgentId(agent_id))
    return UiHealthMessage(
        agent_id=agent_id,
        status=status,
        error=error,
        is_restart_a_no_op=is_restart_a_no_op,
        is_restart_start_only=is_restart_start_only,
    )


# -- App factory --


def create_desktop_client(
    auth_store: AuthStoreInterface,
    backend_resolver: BackendResolverInterface,
    http_client: httpx.Client | None,
    agent_creator: AgentCreator | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    notification_dispatcher: NotificationDispatcher | None = None,
    paths: InstallationPaths | None = None,
    minds_config: MindsConfig | None = None,
    client_env_config: ClientEnvConfig | None = None,
    envelope_stream_consumer: EnvelopeStreamConsumer | None = None,
    session_store: MultiAccountSessionStore | None = None,
    request_inbox: RequestInbox | None = None,
    request_event_handlers: tuple[RequestEventHandler, ...] = (),
    server_port: int = 0,
    mngr_forward_port: int = 0,
    mngr_forward_preauth_cookie: str | None = None,
    mngr_forward_browser_bridge_token: str | None = None,
    output_format: OutputFormat | None = None,
    root_concurrency_group: ConcurrencyGroup | None = None,
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = None,
    mngr_binary: str = "mngr",
    mngr_host_dir: Path | None = None,
    minds_api_key: str | None = None,
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None = None,
    discovery_health_watchdog: DiscoveryHealthWatchdog | None = None,
    mngr_caller: MngrCaller | None = None,
    sync_scheduler: WorkspaceSyncScheduler | None = None,
    connectivity_detector: ConnectivityDetector | None = None,
) -> Flask:
    """Create the bare-origin minds Flask application.

    The agent-subdomain forwarding lives in the ``mngr_forward`` plugin
    (``libs/mngr_forward``) now; this app only serves minds-specific routes
    on the bare origin (login, landing, accounts, workspace settings,
    sharing, agent create / destroy). Workspace links go to the proxy's
    ``localhost:<mngr_forward_port>/goto/<agent>/`` route (``https`` when the
    proxy serves HTTP/2, else ``http``) instead of being routed in-process.

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
    # Static assets served by Flask's built-in handler at the ``/_static`` URL:
    # the embed contract module, the vendored Sentry browser bundle + its init,
    # the service icons, and the built SPA bundle under static/ui/.
    _static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, static_folder=str(_static_dir), static_url_path="/_static")

    @app.errorhandler(Exception)
    def _unhandled_exception_handler(exc: Exception) -> Response | HTTPException:
        # Let werkzeug's HTTP exceptions (404, 405, abort(401), ...) keep their
        # own status instead of collapsing them into a 500 -- matching the prior
        # FastAPI/Starlette behavior where the catch-all only handled real 500s.
        if isinstance(exc, HTTPException):
            # A routing-level 404/405 reached by a real page NAVIGATION would
            # paint werkzeug's bare error page -- no chrome, no links --
            # stranding the user (e.g. a link that points at a POST-only
            # route, which session restore then faithfully re-opens). Serve a
            # friendly page with a way home instead. Navigation is detected
            # from the request, not the path: even an /api/ URL strands the
            # user when opened as a document, while fetch()/XHR callers (who
            # read resp.ok, not the body) keep the raw status. Chromium always
            # sends Sec-Fetch-Dest; the Accept sniff is the fallback for
            # clients that don't (fetches default to Accept: */*).
            sec_fetch_dest = request.headers.get("Sec-Fetch-Dest", "")
            is_document_navigation = (
                sec_fetch_dest == "document" if sec_fetch_dest else "text/html" in request.headers.get("Accept", "")
            )
            if exc.code in (404, 405) and is_document_navigation:
                if exc.code == 404:
                    title, message = "Page not found", "This page doesn't exist (it may have moved)."
                else:
                    title, message = "That didn't work", "This link can't be opened as a page."
                return make_html_response(
                    content=build_error_page_html(title=title, message=message), status_code=exc.code
                )
            return exc
        logger.opt(exception=exc).error("Unhandled exception on {} {}", request.method, request.path)
        return make_response(status_code=500, content=f"Internal Server Error: {exc}")

    # The /ui channel: the broadcaster fans serialized frames out to every
    # connected SPA window, and the edge-driven publisher derives + diffs the
    # chrome state onto it.
    ui_channel_broadcaster = UiChannelBroadcaster()
    ui_publisher = _create_ui_state_publisher(
        app=app,
        broadcaster=ui_channel_broadcaster,
        backend_resolver=backend_resolver,
        session_store=session_store,
        paths=paths,
        system_interface_health_tracker=system_interface_health_tracker,
        discovery_health_watchdog=discovery_health_watchdog,
        connectivity_detector=connectivity_detector,
    )

    # The durable notification feed behind the channel's notifications frame,
    # reconciled by the notifications derive on every publish tick. Its OS
    # dispatch consults the stored preferences live, so a Settings change
    # applies without a restart; without a MindsConfig the defaults apply.
    notification_feed = NotificationFeed(
        notification_dispatcher=notification_dispatcher,
        get_dispatch_preferences=_NotificationDispatchPreferencesReader(minds_config=minds_config),
        get_connected_focused_workspace_agent_ids=_ConnectedFocusedWorkspaceAgentIdsReader(
            broadcaster=ui_channel_broadcaster
        ),
    )

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
        mngr_forward_browser_bridge_token=mngr_forward_browser_bridge_token,
        auth_output_format=output_format or OutputFormat.JSONL,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=system_interface_health_tracker,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir if mngr_host_dir is not None else Path.home() / ".mngr",
        minds_api_key=minds_api_key,
        latchkey_forward_supervisor=latchkey_forward_supervisor,
        discovery_health_watchdog=discovery_health_watchdog,
        connectivity_detector=connectivity_detector,
        mngr_caller=mngr_caller,
        sync_scheduler=sync_scheduler,
        ui_channel_broadcaster=ui_channel_broadcaster,
        ui_publisher=ui_publisher,
        notification_feed=notification_feed,
    )
    set_state(app, state)

    # Wire the channel publisher into every producer of derived state:
    # resolver changes, health edges, and discovery-health changes. One-shot
    # workspace_stopped / open_help / workspace_refresh events are published
    # directly by their producers via publish_one_shot.
    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.add_on_change_callback(ui_publisher.notify_change)
    if system_interface_health_tracker is not None:
        _health_tracker_for_ui = system_interface_health_tracker

        def _publish_ui_health_edge(agent_id: AgentId, status: AgentHealth) -> None:
            ui_publisher.publish_health(_ui_health_message(_health_tracker_for_ui, str(agent_id), status))
            # A health edge also records (or clears) the outage onset, which is
            # what the workspace list's freshness-gated is_backend_unreachable is
            # measured against -- so the row can change with no resolver event
            # behind it. Without this wake the list would keep its pre-onset
            # answer until some unrelated producer fired, which during an outage
            # of the workspace's own provider may be a full poll interval away.
            # The publisher diffs, so an edge that changes no row broadcasts
            # nothing.
            ui_publisher.notify_change()

        _health_tracker_for_ui.add_on_change_callback(_publish_ui_health_edge)

        if root_concurrency_group is not None:
            # Both edges feed the refresher: the health one raises a refresh, and
            # the connectivity one is what releases a refresh raised at a moment
            # the reload it asks for could not have survived.
            workspace_view_refresher = WorkspaceViewRefresher(
                publisher=ui_publisher,
                backend_resolver=backend_resolver,
                connectivity_detector=connectivity_detector,
                concurrency_group=root_concurrency_group,
            )
            _health_tracker_for_ui.add_on_recovery_callback(workspace_view_refresher)
            if connectivity_detector is not None:
                connectivity_detector.add_on_recovery_callback(workspace_view_refresher.on_connectivity_recovered)
            # The tracker fires its on-change callbacks before its stuck-edge ones,
            # so the band is already showing STUCK by the time this dispatches.
            unattended_recovery_dispatcher = UnattendedRecoveryDispatcher(
                tracker=_health_tracker_for_ui,
                backend_resolver=backend_resolver,
                registry=state.workspace_operation_registry,
                concurrency_group=root_concurrency_group,
                mngr_binary=state.mngr_binary,
                mngr_host_dir=state.mngr_host_dir,
                mngr_forward_port=state.mngr_forward_port or 0,
                mngr_forward_preauth_cookie=state.mngr_forward_preauth_cookie,
                connectivity_detector=connectivity_detector,
            )
            _health_tracker_for_ui.add_on_stuck_edge_callback(unattended_recovery_dispatcher)
            # The other half of the gate: a start withheld while this device
            # could not reach anything is owed, and the detector is what
            # eventually reports that it can.
            if connectivity_detector is not None:
                connectivity_detector.add_on_recovery_callback(
                    unattended_recovery_dispatcher.on_connectivity_recovered
                )
    if discovery_health_watchdog is not None:
        discovery_health_watchdog.add_on_change_callback(ui_publisher.notify_change)
    if connectivity_detector is not None:
        # The device's condition is chrome state like any other: the publisher
        # re-derives and diffs it, so a probe that changes nothing broadcasts
        # nothing.
        connectivity_detector.add_on_change_callback(ui_publisher.notify_change)

    # Mount the SPA surface (/ui, /ui/ws, /ui/api/*).
    app.register_blueprint(create_ui_blueprint())

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

    # SPA page routes: every hub path serves the same index document; the
    # Mithril router owns what renders. Path parameters are accepted (and
    # ignored) by serve_spa_index so parameterized pages route here too.
    for spa_route in (
        "/",
        "/create",
        "/create/template",
        "/creating/<agent_id>",
        "/settings",
        "/settings/ai-keys",
        "/accounts",
        "/workspaces/destroyed",
        # The workspace-display route: renders the shell around the sandboxed
        # workspace iframe (the SPA twin of the deleted /_chrome wrapper).
        # Electron's wrapperUrlForWorkspace + session restore load it, and it
        # accepts either coordinate (agent- or host-scoped).
        "/workspace/<agent_id>",
        "/workspace/<agent_id>/settings",
        "/workspace/<agent_id>/options",
        "/workspace/<agent_id>/backups",
        "/destroying/<agent_id>",
        "/agents/<agent_id>/recovery",
        "/help",
        "/welcome",
        "/consent",
        "/_dev/styleguide",
    ):
        app.add_url_rule(spa_route, endpoint=f"spa_index:{spa_route}", view_func=serve_spa_index)

    # Core action routes (POST/GET actions the SPA drives; page GETs are above)
    app.add_url_rule("/consent", view_func=_handle_consent_submit, methods=["POST"])
    app.add_url_rule("/_chrome/error-reporting", view_func=_handle_error_reporting_settings, methods=["POST"])
    app.add_url_rule("/_chrome/backup-password", view_func=_handle_backup_password_change, methods=["POST"])
    app.add_url_rule("/_chrome/sync-unlock", view_func=_handle_sync_unlock, methods=["POST"])
    app.add_url_rule("/_chrome/sync-initial-status", view_func=_handle_sync_initial_status, methods=["GET"])
    app.add_url_rule("/_chrome/workspaces/remove-record", view_func=_handle_remove_workspace_record, methods=["POST"])
    app.add_url_rule("/help/report", view_func=_handle_help_report, methods=["POST"])
    app.add_url_rule("/help/assist", view_func=_handle_help_assist, methods=["POST"])
    app.add_url_rule("/welcome/skip", view_func=_handle_welcome_skip)
    app.add_url_rule("/login", view_func=handle_static_login_page)
    app.add_url_rule("/authenticate", view_func=_handle_authenticate)
    app.add_url_rule("/forward-bridge", view_func=_handle_forward_bridge)
    app.add_url_rule("/post-login", view_func=_handle_post_login_redirect)

    # Account management action routes
    app.add_url_rule("/settings/ai-keys/mint", view_func=_handle_mint_ai_key, methods=["POST"])
    app.add_url_rule("/settings/permissions/revoke", view_func=_handle_revoke_service_for_workspace, methods=["POST"])
    app.add_url_rule(
        "/settings/permissions/revoke-all", view_func=_handle_revoke_service_for_all_workspaces, methods=["POST"]
    )
    app.add_url_rule(
        "/settings/permissions/file-sharing/revoke",
        view_func=_handle_revoke_file_sharing_for_workspace,
        methods=["POST"],
    )
    app.add_url_rule(
        "/settings/permissions/file-sharing/revoke-all",
        view_func=_handle_revoke_file_sharing_for_all_workspaces,
        methods=["POST"],
    )
    app.add_url_rule(
        "/settings/permissions/workspace/revoke",
        view_func=_handle_revoke_workspace_delegation_verb,
        methods=["POST"],
    )
    app.add_url_rule(
        "/settings/connectors/add-account",
        view_func=_handle_add_connector_account,
        methods=["POST"],
    )
    app.add_url_rule(
        "/settings/connectors/disconnect-account",
        view_func=_handle_disconnect_connector_account,
        methods=["POST"],
    )
    app.add_url_rule("/accounts/set-default", view_func=_handle_set_default_account, methods=["POST"])
    app.add_url_rule("/accounts/<user_id>/plan", view_func=_handle_account_set_plan, methods=["POST"])
    app.add_url_rule(
        "/accounts/<user_id>/resend-verification",
        view_func=_handle_account_resend_verification,
        methods=["POST"],
    )
    app.add_url_rule("/accounts/<user_id>/trim-backups", view_func=_handle_account_trim_backups, methods=["POST"])
    app.add_url_rule("/accounts/<user_id>/logout", view_func=_handle_account_logout, methods=["POST"])

    # Request inbox action routes
    app.add_url_rule("/requests/<request_id>/grant", view_func=_handle_request_grant, methods=["POST"])
    app.add_url_rule("/requests/<request_id>/deny", view_func=_handle_request_deny, methods=["POST"])

    # Legacy sharing-editor URLs redirect to the options panel's Share tab
    # (the /modal spelling included -- an overlay that lands there follows the
    # redirect into the browser-mode page, which still renders the pane).
    app.add_url_rule("/sharing/<agent_id>", view_func=_handle_sharing_redirect)
    app.add_url_rule(
        "/sharing/<agent_id>/<service_name>",
        view_func=_handle_sharing_redirect,
        endpoint="sharing_redirect_service",
    )
    app.add_url_rule(
        "/sharing/<agent_id>/<service_name>/modal",
        view_func=_handle_sharing_redirect,
        endpoint="sharing_redirect_modal",
    )

    return app


class _NotificationDispatchPreferencesReader(FrozenModel):
    """Live reader of the stored notification preferences for the feed's OS dispatch.

    Reads MindsConfig on every call so a Settings change applies without an
    app restart; without a MindsConfig (minimal test apps) the defaults apply.
    """

    minds_config: MindsConfig | None = Field(frozen=True, description="Preference store; None means defaults.")

    def __call__(self) -> NotificationDispatchPreferences:
        if self.minds_config is None:
            return NotificationDispatchPreferences(is_enabled=True, style=DEFAULT_NOTIFICATION_STYLE)
        # One atomic read (not two separate locked getters): a concurrent
        # set_notification_prefs() write landing between two separate calls could
        # otherwise produce an (is_enabled, style) pair never actually persisted together.
        is_enabled, style, _is_os_hint_dismissed = self.minds_config.get_notification_prefs()
        return NotificationDispatchPreferences(is_enabled=is_enabled, style=style)


class _ConnectedFocusedWorkspaceAgentIdsReader(FrozenModel):
    """Live reader of the workspace agent ids a *focused* connected UI window is displaying.

    Consulted by the notification feed at dispatch time so a request from the
    workspace the user is actually looking at right now stays silent (the
    in-app review popup covers it) -- distinct from the in-app toast's own
    on-screen check, which does not require OS/browser focus: a window can be
    displaying the right workspace while alt-tabbed away or behind another
    app, in which case the reader is not looking at the in-app popup and
    should still get an OS banner. Windows report their route/workspace/focus
    over the /ui/ws channel's client_state frames.
    """

    broadcaster: UiChannelBroadcaster = Field(frozen=True, description="The /ui/ws fan-out holding per-window state.")

    def __call__(self) -> tuple[str, ...]:
        return tuple(
            state.workspace_agent_id
            for state in self.broadcaster.get_connected_client_states()
            if state.workspace_agent_id and state.has_focus
        )


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
    sleep_tracker: SleepTracker | None = None,
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

    Each pass ticks ``sleep_tracker`` before it reads anything, so the failure
    runs it is about to age are aged against a wake this loop has established
    for itself rather than one the heartbeat thread may not have recorded yet.
    """
    if mngr_forward_port == 0 or not mngr_forward_preauth_cookie or root_concurrency_group is None:
        return

    root_concurrency_group.start_new_thread(
        target=_run_system_interface_health_probe_loop,
        args=(
            tracker,
            backend_resolver,
            mngr_forward_port,
            mngr_forward_preauth_cookie,
            root_concurrency_group,
            sleep_tracker,
        ),
        name="system-interface-health-probe",
        daemon=True,
    )


def _run_system_interface_health_probe_loop(
    tracker: SystemInterfaceHealthTracker,
    backend_resolver: BackendResolverInterface,
    mngr_forward_port: int,
    mngr_forward_preauth_cookie: str,
    root_concurrency_group: ConcurrencyGroup,
    sleep_tracker: SleepTracker | None,
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
            if sleep_tracker is not None:
                # Established here rather than trusted from the heartbeat
                # thread, for the reason the discovery watchdog ticks it too:
                # both loops block on a wall-clock deadline that expires *during*
                # a sleep, so on resume they become runnable at the same instant
                # with no ordering between them. A pass that lands first reports
                # every probe target as failing -- the workspaces really are
                # unreachable for the moment -- and each of those failures is
                # aged against a monotonic clock that barely moved while the
                # machine slept, so a run opened seconds before the lid closed
                # convicts immediately. This loop is the only authority on STUCK
                # and cannot take that back: record_probe_failure returns early
                # for anything but HEALTHY. Ticking here is honest (this loop
                # running is the same evidence the heartbeat records) and cannot
                # invent a gap: an extra tick only ever shortens the one it
                # measures.
                sleep_tracker.record_heartbeat()
            for aid in tracker.snapshot_probe_targets():
                # Workspace origins are keyed by the workspace id (the services
                # agent's id), so the probe target needs no coordinate lookup:
                # an agent the plugin cannot resolve (discovery still warming
                # up, or the agent gone) answers with its 503 loader, which
                # records as a failure below.
                probe_status = probe_workspace_through_plugin(
                    mngr_forward_port=mngr_forward_port,
                    preauth_cookie=mngr_forward_preauth_cookie,
                    workspace_id=str(aid),
                    probe_timeout_seconds=_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                    client=probe_client,
                )
                if probe_status == 200:
                    tracker.record_probe_success(aid)
                else:
                    tracker.record_probe_failure(aid)
            # Sleep on the group's shutdown event (not a throwaway Event) so
            # the loop wakes immediately when shutdown is triggered instead of
            # holding the concurrency-group exit for up to a full interval.
            root_concurrency_group.shutdown_event.wait(timeout=_HEALTH_PROBE_INTERVAL_SECONDS)


# How often the discovery-health watchdog re-reads the resolver's snapshot
# freshness. Comfortably below the watchdog's inter-remediation wait so a due
# producer remediation fires within a tick or two of becoming due.
_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS: Final[float] = 5.0


def start_discovery_health_watchdog_loop(
    watchdog: DiscoveryHealthWatchdog,
    backend_resolver: BackendResolverInterface,
    root_concurrency_group: ConcurrencyGroup | None,
    sleep_tracker: SleepTracker | None = None,
) -> None:
    """Start the background thread that drives the discovery-health watchdog.

    Each tick reads the resolver's ``last_event_at`` -- and the moment this
    process was last observed running again, which it establishes by ticking
    ``sleep_tracker`` itself rather than trusting the heartbeat loop to have got
    there first -- and hands both to ``watchdog.evaluate``, which detects a
    producer stall (or a dead supervisor) and runs producer remediation --
    bounce once, then restart on a capped exponential backoff, retrying forever.
    The thread no-ops when there is no concurrency group (test factories that
    skip background threads).
    """
    if root_concurrency_group is None:
        return
    root_concurrency_group.start_new_thread(
        target=_run_discovery_health_watchdog_loop,
        args=(watchdog, backend_resolver, root_concurrency_group, sleep_tracker),
        name="discovery-health-watchdog",
        daemon=True,
    )


def _run_discovery_health_watchdog_loop(
    watchdog: DiscoveryHealthWatchdog,
    backend_resolver: BackendResolverInterface,
    root_concurrency_group: ConcurrencyGroup,
    sleep_tracker: SleepTracker | None,
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
        last_wake_at: datetime | None = None
        if sleep_tracker is not None:
            # Tick before reading, so the reading is current at the moment it is
            # consumed. Both loops block on a wall-clock deadline that expires
            # *during* a sleep, so on resume both become runnable at the same
            # instant with no ordering between them -- and a tick of this loop
            # that reads a pre-sleep wake ages an hours-old event from the
            # watchdog's start, calls it a stall, and SIGHUPs a producer that
            # was never given a chance to emit. Ticking here is honest (this
            # loop running is the same evidence the heartbeat records) and
            # cannot invent a gap: an extra tick only ever shortens the one it
            # measures.
            sleep_tracker.record_heartbeat()
            last_wake_at = sleep_tracker.get_last_wake_at()
        watchdog.evaluate(last_event_at, last_wake_at)
        # Sleep on the group's shutdown event (not a throwaway Event) so the
        # loop wakes immediately when shutdown is triggered instead of holding
        # the concurrency-group exit for up to a full interval.
        root_concurrency_group.shutdown_event.wait(timeout=_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS)


# How often the sleep tracker samples the wall clock. The gap threshold sits
# thirty times above this, so the tick can be missed repeatedly under load
# without the window between two of them ever reading as a sleep.
_SLEEP_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 1.0


def start_sleep_heartbeat_loop(
    sleep_tracker: SleepTracker,
    root_concurrency_group: ConcurrencyGroup | None,
) -> None:
    """Start the background thread whose ticks are the sleep detector.

    The loop's only job is to prove the process was running: each tick records
    the wall clock, and a gap between two of them is a window in which nothing
    here ran. Started as early as possible in the app's life, since a consumer
    can only subtract sleep that happened after the first tick.

    The thread no-ops when there is no concurrency group (test factories that
    skip background threads); the tracker then simply records no intervals,
    which every consumer reads as "no sleep known".
    """
    if root_concurrency_group is None:
        return
    root_concurrency_group.start_new_thread(
        target=_run_sleep_heartbeat_loop,
        args=(sleep_tracker, root_concurrency_group),
        name="sleep-heartbeat",
        daemon=True,
    )


def _run_sleep_heartbeat_loop(
    sleep_tracker: SleepTracker,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Loop body for the sleep-heartbeat thread."""
    while not root_concurrency_group.is_shutting_down():
        sleep_tracker.record_heartbeat()
        # Sleep on the group's shutdown event (not a throwaway Event) so the
        # loop wakes immediately when shutdown is triggered instead of holding
        # the concurrency-group exit for up to a full interval.
        root_concurrency_group.shutdown_event.wait(timeout=_SLEEP_HEARTBEAT_INTERVAL_SECONDS)
