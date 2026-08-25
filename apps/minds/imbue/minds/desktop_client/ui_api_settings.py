"""/ui/api routes owned by tranche T2 (Settings / Accounts / AI keys).

JSON twins of the data that used to be server-rendered into the Settings,
Accounts, and AI-keys pages, plus the settings writes the SPA performs
directly (the error-reporting opt-out and the notification preferences).
Mutating flows the legacy POST routes already implement (permission revokes,
connector add/disconnect, plan switch, trim, set-default, logout, key mint,
master-password change) are reused by the SPA as-is and stay in ``app.py``.

The error-reporting and notification-prefs writes are minds-owned records,
so each carries the optimistic-concurrency contract: ``GET /ui/api/settings``
returns a per-record ``version`` derived from that record's stored values,
and the write requires that version in ``If-Match`` (412 on mismatch, 428
when absent) so a stale window can never silently clobber a newer change.
"""

import hashlib
import json
import os
from typing import Callable
from typing import TypeVar

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.account_plan_view import build_account_plan_view
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.backup_trim import BackupTrimStatus
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_overview import ServicePermissionOverview
from imbue.minds.desktop_client.latchkey.permission_overview import WorkspaceDelegationGrant
from imbue.minds.desktop_client.latchkey.permission_overview import WorkspaceFileSharingGrant
from imbue.minds.desktop_client.latchkey.permission_overview import build_file_sharing_overview
from imbue.minds.desktop_client.latchkey.permission_overview import build_permission_overview
from imbue.minds.desktop_client.latchkey.permission_overview import build_workspace_overview
from imbue.minds.desktop_client.minds_config import DEFAULT_NOTIFICATION_STYLE
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.state import get_state
from imbue.minds.mngr_settings.imbue_cloud_accounts import is_imbue_cloud_provider_enabled_for_account
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent

_WriteT = TypeVar("_WriteT", bound=FrozenModel)


class UiNotificationPrefs(FrozenModel):
    """The notification-prefs record on the settings overview."""

    is_enabled: bool = Field(description="Master notifications toggle (gates every OS nudge the app sends)")
    style: NotificationStyle = Field(description="Delivery style for feed-backed notifications")
    is_os_hint_dismissed: bool = Field(description="Whether the one-time OS-notification hint was dismissed")
    os_permission_confirmed: bool = Field(
        description="Whether the desktop app has ever confirmed native OS notification permission is granted"
    )
    version: str = Field(description="If-Match version for the notification-prefs write")


class UiNotificationPrefsWrite(FrozenModel):
    """Body of the notification-prefs write."""

    is_enabled: bool = Field(description="New master-toggle value")
    style: NotificationStyle = Field(description="New delivery style")
    is_os_hint_dismissed: bool = Field(description="New hint-dismissed value")


class UiSettingsOverview(FrozenModel):
    """Everything the SPA settings page renders, in one response."""

    services_overview: tuple[ServicePermissionOverview, ...] = Field(description="Connector grants per service")
    file_sharing_grants: tuple[WorkspaceFileSharingGrant, ...] = Field(description="File-sharing grants per workspace")
    workspace_delegation_grants: tuple[WorkspaceDelegationGrant, ...] = Field(
        description="Cross-workspace delegation grants"
    )
    permissions_unavailable: bool = Field(description="True when the latchkey gateway could not be reached")
    is_master_password_set: bool = Field(description="Whether any signed-in account has a master password")
    report_unexpected_errors: bool = Field(description="The per-machine error-reporting opt-out state")
    version: str = Field(description="If-Match version for the error-reporting write")
    notification_prefs: UiNotificationPrefs = Field(
        description="Notification preferences, carrying their own If-Match version"
    )


class UiErrorReportingWrite(FrozenModel):
    """Body of the error-reporting opt-out write."""

    report_unexpected_errors: bool = Field(description="New value for the per-machine flag")


class UiAccountEntry(FrozenModel):
    """One signed-in account row on the Accounts page."""

    user_id: str = Field(description="The account's user id")
    email: str = Field(description="The account's email")
    workspace_count: int = Field(description="Number of machines owned by this account")
    is_default: bool = Field(description="Whether this account is the default for new machines")
    is_enabled: bool = Field(description="False when the provider block was disabled (shown as signed out)")


class UiAccountsDetail(FrozenModel):
    """The Accounts page's account list."""

    accounts: tuple[UiAccountEntry, ...] = Field(description="Signed-in accounts, session-store order")


class UiPlanUsageRow(FrozenModel):
    """One usage row in an account's plan section."""

    label: str = Field(description="Quota label")
    used: str = Field(description="Formatted current usage")
    limit: str = Field(description="Formatted limit")
    note: str = Field(description="Explanatory note, possibly empty")


class UiAccountPlanView(FrozenModel):
    """One account's plan + usage, from the connector."""

    plan_name: str = Field(description="Raw plan name")
    plan_display_name: str = Field(description="Display form of the plan name")
    available_plans: tuple[str, ...] = Field(description="Plans the selector offers")
    usage_rows: tuple[UiPlanUsageRow, ...] = Field(description="Usage table rows")
    is_over_storage_quota: bool = Field(description="Gates the free-up-backup-space action")
    is_at_bucket_quota: bool = Field(description="Gates the review-destroyed-backups link")


class UiTrimStatus(FrozenModel):
    """Backup-trim progress for one account."""

    is_running: bool = Field(description="Whether the trim is still going")
    detail: str = Field(description="Human-readable progress / outcome line")


class UiAccountPlanResponse(FrozenModel):
    """Plan section payload; plan_view is None when the connector is unreachable."""

    plan_view: UiAccountPlanView | None = Field(description="Plan + usage, or None when unavailable")
    trim_status: UiTrimStatus | None = Field(description="Trim progress when a trim ran or is running")
    privacy_policy_url: str = Field(
        description="The tier's privacy-policy page (for the plan selector's Learn-more link); '' when unknown"
    )


class UiAiKeysContext(FrozenModel):
    """Context for the workspace AI-key mint page."""

    workspace_host_id: str = Field(description="The workspace host id the key is minted for")
    workspace_display_name: str = Field(description="Display name of the workspace")
    account_email: str = Field(description="The billed account's email")
    error_message: str = Field(description="Non-empty when minting is impossible; explains why")


def _is_settings_request_authenticated() -> bool:
    """The same session-cookie check the /ui index uses.

    Duplicated (six lines) rather than imported from ``ui_api``: that module
    imports this one to register routes, so importing back would be circular.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key)


def _json_response(payload: FrozenModel, status_code: int = 200) -> Response:
    return Response(payload.model_dump_json(), status=status_code, mimetype="application/json")


def _error_response(message: str, status_code: int) -> Response:
    return Response(json.dumps({"error": message}), status=status_code, mimetype="application/json")


def _unauthenticated_response() -> Response:
    return _error_response("Not authenticated", 401)


def compute_error_reporting_version(report_unexpected_errors: bool) -> str:
    """The If-Match version of the error-reporting record: a hash of its stored value.

    A write started from state A only succeeds while the stored state still
    equals A -- exactly the staleness contract optimistic concurrency needs
    for a record this small.
    """
    canonical = json.dumps({"report_unexpected_errors": report_unexpected_errors}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def compute_notification_prefs_version(is_enabled: bool, style: str, is_os_hint_dismissed: bool) -> str:
    """The If-Match version of the notification-prefs record: a hash of its stored values.

    A per-record version (rather than folding these values into the
    error-reporting version) keeps each record's writes from 412-ing pages
    that only touched the other record.
    """
    canonical = json.dumps(
        {"is_enabled": is_enabled, "is_os_hint_dismissed": is_os_hint_dismissed, "style": style},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _current_notification_prefs() -> UiNotificationPrefs:
    """The stored notification-prefs record (defaults when no MindsConfig is wired)."""
    minds_config = get_state().minds_config
    if minds_config is None:
        is_enabled = True
        style: NotificationStyle = DEFAULT_NOTIFICATION_STYLE
        is_os_hint_dismissed = False
        os_permission_confirmed = False
    else:
        is_enabled, style, is_os_hint_dismissed, os_permission_confirmed = minds_config.get_notification_prefs()
    return UiNotificationPrefs(
        is_enabled=is_enabled,
        style=style,
        is_os_hint_dismissed=is_os_hint_dismissed,
        os_permission_confirmed=os_permission_confirmed,
        version=compute_notification_prefs_version(is_enabled, style, is_os_hint_dismissed),
    )


def _find_permission_grant_handler() -> LatchkeyPermissionGrantHandler | None:
    for handler in get_state().request_event_handlers:
        if isinstance(handler, LatchkeyPermissionGrantHandler):
            return handler
    return None


def _is_any_account_master_password_set() -> bool:
    paths = get_state().api_v1_paths
    session_store = get_state().session_store
    if paths is None or session_store is None:
        return False
    return any(
        is_master_password_set_for_account(paths, str(account.user_id)) for account in session_store.list_accounts()
    )


def _handle_settings_overview() -> Response:
    """GET /ui/api/settings: the app-level settings page's full data payload."""
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    services_overview: tuple[ServicePermissionOverview, ...] = ()
    file_sharing_grants: tuple[WorkspaceFileSharingGrant, ...] = ()
    workspace_delegation_grants: tuple[WorkspaceDelegationGrant, ...] = ()
    permissions_unavailable = False
    handler = _find_permission_grant_handler()
    if handler is not None:
        try:
            services_overview = tuple(
                build_permission_overview(
                    backend_resolver=get_state().backend_resolver,
                    gateway_client=handler.gateway_client,
                    services_catalog=handler.services_catalog,
                    latchkey=handler.latchkey,
                )
            )
            file_sharing_grants = tuple(
                build_file_sharing_overview(
                    backend_resolver=get_state().backend_resolver,
                    gateway_client=handler.gateway_client,
                    latchkey=handler.latchkey,
                )
            )
            workspace_delegation_grants = tuple(
                build_workspace_overview(
                    backend_resolver=get_state().backend_resolver,
                    gateway_client=handler.gateway_client,
                    latchkey=handler.latchkey,
                )
            )
        except LatchkeyGatewayClientError as e:
            logger.warning("Could not build the permission overview for the settings payload: {}", e)
            permissions_unavailable = True
    minds_config = get_state().minds_config
    report_unexpected_errors = minds_config.get_report_unexpected_errors() if minds_config else True
    overview = UiSettingsOverview(
        services_overview=services_overview,
        file_sharing_grants=file_sharing_grants,
        workspace_delegation_grants=workspace_delegation_grants,
        permissions_unavailable=permissions_unavailable,
        is_master_password_set=_is_any_account_master_password_set(),
        report_unexpected_errors=report_unexpected_errors,
        version=compute_error_reporting_version(report_unexpected_errors),
        notification_prefs=_current_notification_prefs(),
    )
    return _json_response(overview)


def _handle_if_match_write(
    minds_config: MindsConfig,
    write_model_type: type[_WriteT],
    apply_versioned_write: Callable[[MindsConfig, _WriteT, str], str | None],
) -> Response:
    """Shared If-Match-guarded settings write: parse the body, compare-and-swap, respond.

    ``apply_versioned_write`` performs the version check AND the persistence (plus any
    side effects) atomically -- under one MindsConfig lock hold, not two separate calls --
    and returns the record's new version, or None on a version mismatch. Shared across
    every minds-owned settings record so each one doesn't reimplement the same
    parse/validate/If-Match/compare-and-swap skeleton.
    """
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error_response("Invalid JSON body", 400)
    try:
        write = write_model_type.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed settings write body: {}", e)
        return _error_response("Invalid JSON body", 400)
    provided_version = request.headers.get("If-Match")
    if provided_version is None:
        return _error_response("If-Match header is required for this write", 428)
    new_version = apply_versioned_write(minds_config, write, provided_version)
    if new_version is None:
        return _error_response("The setting changed since this page loaded", 412)
    return Response(json.dumps({"version": new_version}), mimetype="application/json")


def _apply_error_reporting_write(
    minds_config: MindsConfig, write: UiErrorReportingWrite, expected_version: str
) -> str | None:
    # Compare-and-swap under one MindsConfig lock hold: checking the version and applying
    # the write as two separate locked calls would let a concurrent writer starting from
    # the same version slip in between them and silently clobber this write with no
    # conflict reported to either side.
    new_version = minds_config.set_report_unexpected_errors_if_version_matches(
        expected_version=expected_version,
        compute_version=compute_error_reporting_version,
        enabled=write.report_unexpected_errors,
    )
    if new_version is None:
        return None
    # Mirror the change into the detached ``mngr latchkey forward`` daemon's
    # live consent file (read per event) so the opt-out takes effect without
    # an app restart, exactly as the legacy /_chrome/error-reporting write did.
    write_latchkey_forward_sentry_consent(
        latchkey_forward_sentry_consent_path(minds_config.data_dir),
        is_error_reporting_enabled=write.report_unexpected_errors,
    )
    return new_version


def _handle_error_reporting_write() -> Response:
    """POST /ui/api/settings/error-reporting: If-Match-guarded opt-out write."""
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    return _handle_if_match_write(
        minds_config=minds_config,
        write_model_type=UiErrorReportingWrite,
        apply_versioned_write=_apply_error_reporting_write,
    )


def _apply_notification_prefs_write(
    minds_config: MindsConfig, write: UiNotificationPrefsWrite, expected_version: str
) -> str | None:
    # Compare-and-swap under one MindsConfig lock hold, same rationale as
    # _apply_error_reporting_write above: the version check and the write must not be two
    # separate locked calls, or a concurrent writer starting from the same version could
    # slip in between them and silently clobber this write.
    return minds_config.set_notification_prefs_if_version_matches(
        expected_version=expected_version,
        compute_version=compute_notification_prefs_version,
        is_enabled=write.is_enabled,
        style=write.style,
        is_os_hint_dismissed=write.is_os_hint_dismissed,
    )


def _handle_notification_prefs_write() -> Response:
    """POST /ui/api/settings/notifications: If-Match-guarded notification-prefs write."""
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    return _handle_if_match_write(
        minds_config=minds_config,
        write_model_type=UiNotificationPrefsWrite,
        apply_versioned_write=_apply_notification_prefs_write,
    )


class UiNotificationOsPermissionWrite(FrozenModel):
    """Body of the OS-notification-permission-confirmed write."""

    os_permission_confirmed: bool = Field(
        description="Whether native OS notification permission was just confirmed granted"
    )


def _handle_notification_os_permission_write() -> Response:
    """POST /ui/api/settings/notification-os-permission: records the desktop app's own
    best-effort observation of whether native OS notification permission is granted.

    Electron exposes no permission-status API on macOS, so this is set from a
    probe notification's 'show' event actually firing (or not) -- the one
    signal available. Deliberately unguarded by If-Match: this is
    system-observed state the app derives for itself, not a user-typed
    preference, so a lost update under a race just means probing once more
    than strictly necessary, never a torn record.
    """
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error_response("Invalid JSON body", 400)
    try:
        write = UiNotificationOsPermissionWrite.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed notification-os-permission write body: {}", e)
        return _error_response("Invalid JSON body", 400)
    minds_config.set_notification_os_permission_confirmed(write.os_permission_confirmed)
    return Response(status=204)


def _handle_accounts_detail() -> Response:
    """GET /ui/api/accounts: the Accounts page's account list."""
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    session_store = get_state().session_store
    minds_config = get_state().minds_config
    accounts = session_store.list_accounts() if session_store else []
    default_account_id = minds_config.get_default_account_id() if minds_config else None
    entries = tuple(
        UiAccountEntry(
            user_id=str(account.user_id),
            email=str(account.email),
            workspace_count=len(account.workspace_ids),
            is_default=str(account.user_id) == default_account_id,
            is_enabled=is_imbue_cloud_provider_enabled_for_account(
                str(account.email), root=MindsRoot.from_environment()
            ),
        )
        for account in accounts
    )
    return _json_response(UiAccountsDetail(accounts=entries))


def _trim_status_payload(trim_status: BackupTrimStatus | None) -> UiTrimStatus | None:
    if trim_status is None:
        return None
    return UiTrimStatus(is_running=trim_status.is_running, detail=trim_status.detail)


def _privacy_policy_url() -> str:
    """The tier's privacy-policy page, served by the connector's accounts surface.

    Prefers the dedicated accounts origin (production: accounts.imbue.com)
    and falls back to the connector host, mirroring how the login page is
    resolved. Empty when the app runs without a client env config.
    """
    client_env_config = get_state().client_env_config
    if client_env_config is None:
        return ""
    return client_env_config.accounts_origin_url() + "/privacy-policy"


def _handle_account_plan(user_id: str) -> Response:
    """GET /ui/api/accounts/<user_id>/plan: one account's plan + usage (slow: connector round trip).

    A connector failure degrades to ``plan_view: null`` rather than an error
    status, so the card renders a plan-unavailable state instead of failing.
    """
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    session_store = get_state().session_store
    cli = get_state().imbue_cloud_cli
    account = next(
        (a for a in (session_store.list_accounts() if session_store else []) if str(a.user_id) == user_id),
        None,
    )
    plan_view: UiAccountPlanView | None = None
    if account is not None and cli is not None:
        try:
            info = cli.get_account_info(str(account.email))
        except ImbueCloudCliError as exc:
            logger.debug("Could not fetch account info for {}: {}", account.email, exc)
        else:
            plan_view = UiAccountPlanView.model_validate(build_account_plan_view(info))
    trim_status = get_state().backup_trim_manager.get_status(user_id)
    return _json_response(
        UiAccountPlanResponse(
            plan_view=plan_view,
            trim_status=_trim_status_payload(trim_status),
            privacy_policy_url=_privacy_policy_url(),
        )
    )


def _handle_ai_keys_context() -> Response:
    """GET /ui/api/ai-keys?workspace=<host_id>: context for the mint page."""
    if not _is_settings_request_authenticated():
        return _unauthenticated_response()
    workspace_host_id = request.args.get("workspace", "").strip()
    if not workspace_host_id:
        return _json_response(
            UiAiKeysContext(
                workspace_host_id="",
                workspace_display_name="",
                account_email="",
                error_message=(
                    "This page needs to be opened from a machine: use the Sign in with Imbue "
                    "option in the machine's Claude sign-in dialog."
                ),
            )
        )
    sync_scheduler = get_state().sync_scheduler
    record_store = None if sync_scheduler is None else sync_scheduler.record_store
    resolved = resolve_workspace_account(workspace_host_id, record_store, get_state().session_store)
    if resolved is None:
        return _json_response(
            UiAiKeysContext(
                workspace_host_id=workspace_host_id,
                workspace_display_name="",
                account_email="",
                error_message=(
                    "This machine has no associated Imbue account. Associate an account on the "
                    "machine's settings page, then come back here."
                ),
            )
        )
    return _json_response(
        UiAiKeysContext(
            workspace_host_id=workspace_host_id,
            workspace_display_name=resolved.workspace_display_name,
            account_email=resolved.account_email,
            error_message="",
        )
    )


def register_settings_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/settings", view_func=_handle_settings_overview)
    blueprint.add_url_rule("/api/settings/error-reporting", view_func=_handle_error_reporting_write, methods=["POST"])
    blueprint.add_url_rule("/api/settings/notifications", view_func=_handle_notification_prefs_write, methods=["POST"])
    blueprint.add_url_rule(
        "/api/settings/notification-os-permission",
        view_func=_handle_notification_os_permission_write,
        methods=["POST"],
    )
    blueprint.add_url_rule("/api/accounts", view_func=_handle_accounts_detail)
    blueprint.add_url_rule("/api/accounts/<user_id>/plan", view_func=_handle_account_plan)
    blueprint.add_url_rule("/api/ai-keys", view_func=_handle_ai_keys_context)
