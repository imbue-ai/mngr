"""/ui/api routes owned by tranche T2 (Settings / Accounts / AI keys).

JSON twins of the data that used to be server-rendered into the Settings,
Accounts, and AI-keys pages, plus the settings writes the SPA performs
directly (the error-reporting opt-out and the notification preferences).
Mutating flows the legacy POST routes already implement (permission revokes,
connector add/disconnect, plan switch, trim, set-default, logout, key mint,
master-password change) are reused by the SPA as-is and stay in ``app.py``.

The error-reporting and notification-prefs writes are records Imbue Studio owns,
so each carries the optimistic-concurrency contract: ``GET /ui/api/settings``
returns a per-record ``version`` derived from that record's stored values,
and the write requires that version in ``If-Match`` (412 on mismatch, 428
when absent) so a stale window can never silently clobber a newer change.
"""

import hashlib
import json
from typing import Callable
from typing import TypeVar

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.account_plan_view import build_account_plan_view
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.backup_trim import BackupTrimStatus
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.minds_config import DEFAULT_NOTIFICATION_STYLE
from imbue.minds.desktop_client.minds_config import DEFAULT_UPDATE_WINDOW
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME

_WriteT = TypeVar("_WriteT", bound=FrozenModel)


class UiNotificationPrefs(FrozenModel):
    """The notification-prefs record on the settings overview."""

    is_enabled: bool = Field(description="Master notifications toggle (gates every OS nudge the app sends)")
    style: NotificationStyle = Field(description="Delivery style for every feed entry")
    has_chosen: bool = Field(description="Whether notification preferences have been explicitly saved")
    version: str = Field(description="If-Match version for the notification-prefs write")


class UiNotificationPrefsWrite(FrozenModel):
    """Body of the notification-prefs write."""

    is_enabled: bool = Field(description="New master-toggle value")
    style: NotificationStyle = Field(description="New delivery style")


class UiTestNotificationResult(FrozenModel):
    """Answer to the test-notification push."""

    is_electron: bool = Field(
        description="Whether the app is running inside the desktop shell, the only place a banner can reach the OS"
    )


class UiSignInBrowserOption(FrozenModel):
    """One browser provider sign-ins can open in."""

    browser_id: str = Field(description="The id the setting stores")
    label: str = Field(description="The browser's name")


class UiSettingsOverview(FrozenModel):
    """Everything the SPA settings page renders, in one response.

    Deliberately carries no permissions. Credentials and grants belong to one
    machine each, so they are shown and managed on that machine's Permissions
    tab; an app-level view of them would have to speak for every machine at
    once, which stopped being a true thing to say when each machine started
    keeping its own.
    """

    is_master_password_set: bool = Field(description="Whether any signed-in account has a master password")
    report_unexpected_errors: bool = Field(description="The per-machine error-reporting opt-out state")
    version: str = Field(description="If-Match version for the error-reporting write")
    notification_prefs: UiNotificationPrefs = Field(
        description="Notification preferences, carrying their own If-Match version"
    )
    update_window_start_hour: int = Field(description="Local hour scheduled machine updates may start running at")
    update_window_end_hour: int = Field(description="Local hour scheduled machine updates stop running at")
    sign_in_browsers: tuple[UiSignInBrowserOption, ...] = Field(
        default=(), description="The installed browsers provider sign-ins can open in"
    )
    sign_in_browser_id: str | None = Field(
        default=None, description="The browser provider sign-ins open in; None for the default browser"
    )


class UiBrowserImportStatus(FrozenModel):
    """Where the one-time Chrome sign-in import offer stands."""

    is_offered: bool = Field(description="Whether the permission dialog has already made the one-time offer")
    is_available: bool = Field(description="Whether this desktop has a latchkey to import into at all")


class UiBrowserImportResult(FrozenModel):
    """Outcome of one ``latchkey auth import-chrome`` run."""

    is_success: bool = Field(description="Whether the import completed")
    detail: str = Field(description="Why the import did not happen; empty on success")


class UiErrorReportingWrite(FrozenModel):
    """Body of the error-reporting opt-out write."""

    report_unexpected_errors: bool = Field(description="New value for the per-machine flag")


class UiSignInBrowserWrite(FrozenModel):
    """Body of the sign-in browser write."""

    browser_id: str | None = Field(description="An installed browser's id, or None for the default browser")


class UiUpdateWindowWrite(FrozenModel):
    """Body of the scheduled-update window write."""

    start_hour: int = Field(ge=0, le=23, description="Local hour the window opens")
    end_hour: int = Field(ge=0, le=23, description="Local hour the window closes")


class UiNotificationChannels(FrozenModel):
    """An account's notification preferences on Imbue Cloud, as the accounts page shows them."""

    email_enabled: bool = Field(description="Whether notification email (invitations included) may be sent")
    in_app_enabled: bool = Field(description="Whether in-app notifications may be delivered")


class UiAccountNotificationPreferencesResponse(FrozenModel):
    """Answer to the account notification-preferences read or write."""

    preferences: UiNotificationChannels | None = Field(
        description="The preferences; None when Imbue Cloud could not be reached"
    )


class UiAccountNotificationPreferencesWrite(FrozenModel):
    """Body of the account notification-preferences write: the one switch the app exposes."""

    email_enabled: bool = Field(description="Whether notification email may be sent to the account's address")


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

    workspace_id: str = Field(description="The workspace coordinate the mint page was opened with")
    workspace_display_name: str = Field(description="Display name of the workspace")
    account_email: str = Field(description="The billed account's email")
    error_message: str = Field(description="Non-empty when minting is impossible; explains why")


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


def compute_notification_prefs_version(is_enabled: bool, style: str, has_chosen: bool) -> str:
    """The If-Match version of the notification-prefs record: a hash of its stored values.

    A per-record version (rather than folding these values into the
    error-reporting version) keeps each record's writes from 412-ing pages
    that only touched the other record.
    """
    canonical = json.dumps({"is_enabled": is_enabled, "style": style, "has_chosen": has_chosen}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _current_notification_prefs() -> UiNotificationPrefs:
    """The stored notification-prefs record (defaults when no MindsConfig is wired)."""
    minds_config = get_state().minds_config
    if minds_config is None:
        is_enabled = True
        style: NotificationStyle = DEFAULT_NOTIFICATION_STYLE
        # No settings storage: there is nowhere to save a choice.
        has_chosen = True
    else:
        is_enabled, style, has_chosen = minds_config.get_notification_prefs_with_choice()
    return UiNotificationPrefs(
        is_enabled=is_enabled,
        style=style,
        has_chosen=has_chosen,
        version=compute_notification_prefs_version(is_enabled, style, has_chosen),
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
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    report_unexpected_errors = minds_config.get_report_unexpected_errors() if minds_config else True
    update_window = minds_config.get_update_window() if minds_config is not None else DEFAULT_UPDATE_WINDOW
    browsers = get_state().sign_in_browsers.list_browsers()
    stored_browser_id = minds_config.get_sign_in_browser_id() if minds_config is not None else None
    overview = UiSettingsOverview(
        is_master_password_set=_is_any_account_master_password_set(),
        report_unexpected_errors=report_unexpected_errors,
        version=compute_error_reporting_version(report_unexpected_errors),
        notification_prefs=_current_notification_prefs(),
        update_window_start_hour=update_window[0],
        update_window_end_hour=update_window[1],
        sign_in_browsers=tuple(
            UiSignInBrowserOption(browser_id=browser.browser_id, label=browser.label) for browser in browsers
        ),
        # A chosen browser that has since been removed reads as the default, which is what opens.
        sign_in_browser_id=(
            stored_browser_id if any(browser.browser_id == stored_browser_id for browser in browsers) else None
        ),
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
    every settings record Imbue Studio owns so each one doesn't reimplement the same
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
    if not is_ui_request_authenticated():
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
    )


def _handle_notification_prefs_write() -> Response:
    """POST /ui/api/settings/notifications: If-Match-guarded notification-prefs write."""
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    return _handle_if_match_write(
        minds_config=minds_config,
        write_model_type=UiNotificationPrefsWrite,
        apply_versioned_write=_apply_notification_prefs_write,
    )


def _handle_test_notification() -> Response:
    """POST /ui/api/settings/notifications/test: push one banner through the real OS path.

    Deliberately ignores the stored preferences: the button exists to find
    out whether banners reach the OS at all, which is the question the
    reader has when a preference looks right and nothing appears.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    dispatcher = get_state().notification_dispatcher
    if dispatcher is None:
        return _error_response("Notification dispatch is not configured", 503)
    dispatcher.dispatch(
        NotificationRequest(
            title="Imbue Studio",
            subtitle="Test notification",
            body="System notifications are reaching you.",
        )
    )
    return _json_response(UiTestNotificationResult(is_electron=dispatcher.is_electron))


def _has_completed_a_browser_sign_in(handler: LatchkeyPermissionGrantHandler) -> bool:
    """Whether this computer's latchkey holds a browser session from a completed sign-in.

    Latchkey writes the browser state only at the end of a completed,
    non-ephemeral browser sign-in (or of the import itself), and every machine
    store reaches the desktop's copy through a symlink, so the one file says
    whether the user has already logged in through the browser here.
    """
    return (handler.latchkey.latchkey_directory / BROWSER_STATE_FILENAME).is_file()


def _browser_import_status() -> UiBrowserImportStatus:
    """The offer's standing, settling it from the browser state when nothing has recorded it yet.

    A user who has already signed in through the browser has already paid the
    login the offer exists to skip, so the offer is recorded as made the first
    time it is asked about, and the import stays reachable from Settings. Done
    here rather than at startup so an install that has never been asked stays
    untouched, and recorded rather than re-derived so the answer holds still
    even if the state file later goes away.
    """
    minds_config = get_state().minds_config
    handler = _find_permission_grant_handler()
    if minds_config is None:
        is_offered = True
    else:
        is_offered = minds_config.get_is_browser_import_offered()
        if not is_offered and handler is not None and _has_completed_a_browser_sign_in(handler):
            minds_config.set_is_browser_import_offered(True)
            is_offered = True
    return UiBrowserImportStatus(is_offered=is_offered, is_available=handler is not None)


def _handle_browser_import_status() -> Response:
    """GET /ui/api/settings/browser-import: whether the one-time offer is still due, and whether an import can run.

    Without settings storage there is nowhere to remember an offer, so it reads
    as already made rather than being repeated on every Approve.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    return _json_response(_browser_import_status())


def _handle_browser_import_offered() -> Response:
    """POST /ui/api/settings/browser-import/offered: record that the one-time offer has been shown.

    Shown is what counts, whatever the answer was: the offer is made once, and a
    user who waved it away is not asked again on their next Approve. The import
    stays reachable from Settings.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    minds_config.set_is_browser_import_offered(True)
    return _json_response(_browser_import_status())


def _handle_browser_import_run() -> Response:
    """POST /ui/api/settings/browser-import: import the user's Chrome cookies and site logins into latchkey.

    Blocks for the whole run (tens of seconds), like the browser sign-in an
    Approve runs: the caller shows a spinner and reads the outcome from the
    body. A failed import answers 200 with ``is_success`` false and latchkey's
    reason, so the dialog can show it and go on to the sign-in. Also counts as
    the offer having been made, since running it is one way of answering it.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    handler = _find_permission_grant_handler()
    if handler is None:
        return _error_response("Browser cookies are not configured on this desktop", 503)
    minds_config = get_state().minds_config
    if minds_config is not None:
        minds_config.set_is_browser_import_offered(True)
    is_success, detail = handler.latchkey.import_chrome_browser_state()
    return _json_response(UiBrowserImportResult(is_success=is_success, detail=detail))


def _handle_update_window_write() -> Response:
    """POST /ui/api/settings/update-window: set the local hours scheduled updates run in.

    No If-Match guard: a plain preference with no consent semantics.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error_response("Invalid JSON body", 400)
    try:
        write = UiUpdateWindowWrite.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed update-window write body: {}", e)
        return _error_response("Invalid JSON body", 400)
    if write.start_hour == write.end_hour:
        return _error_response("The update window needs a start and end that differ", 400)
    minds_config.set_update_window(write.start_hour, write.end_hour)
    return _json_response(UiUpdateWindowWrite(start_hour=write.start_hour, end_hour=write.end_hour))


def _handle_sign_in_browser_write() -> Response:
    """POST /ui/api/settings/sign-in-browser: choose the browser provider sign-ins open in."""
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    state = get_state()
    minds_config = state.minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error_response("Invalid JSON body", 400)
    try:
        write = UiSignInBrowserWrite.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed sign-in browser write body: {}", e)
        return _error_response("Invalid JSON body", 400)
    if write.browser_id is not None and all(
        browser.browser_id != write.browser_id for browser in state.sign_in_browsers.list_browsers()
    ):
        return _error_response("That browser is not installed", 400)
    minds_config.set_sign_in_browser_id(write.browser_id)
    return _json_response(write)


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
    if not is_ui_request_authenticated():
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


def _signed_in_account_for(user_id: str) -> AccountSession | None:
    session_store = get_state().session_store
    accounts = session_store.list_accounts() if session_store is not None else []
    return next((account for account in accounts if str(account.user_id) == user_id), None)


def _handle_account_notification_preferences(user_id: str) -> Response:
    """GET/POST /ui/api/accounts/<user_id>/notification-preferences: the account's channels on Imbue Cloud.

    Only the email switch is written: in-app stays as it is (the channel is
    modelled but not delivered yet). A connector failure degrades to
    ``preferences: null`` rather than an error status, so the card renders an
    unavailable state instead of failing.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    account = _signed_in_account_for(user_id)
    cli = get_state().imbue_cloud_cli
    if account is None or cli is None:
        return _json_response(UiAccountNotificationPreferencesResponse(preferences=None))
    try:
        if request.method == "POST":
            body = request.get_json(silent=True, force=True)
            if not isinstance(body, dict):
                return _error_response("Invalid JSON body", 400)
            try:
                write = UiAccountNotificationPreferencesWrite.model_validate(body)
            except ValidationError as e:
                logger.debug("Rejected a malformed notification-preferences write body: {}", e)
                return _error_response("Invalid JSON body", 400)
            current = cli.get_notification_preferences(account=str(account.email))
            info = cli.set_notification_preferences(
                account=str(account.email), email_enabled=write.email_enabled, in_app_enabled=current.in_app_enabled
            )
        else:
            info = cli.get_notification_preferences(account=str(account.email))
    except ImbueCloudCliError as exc:
        logger.debug("Could not reach the notification preferences for {}: {}", account.email, exc)
        return _json_response(UiAccountNotificationPreferencesResponse(preferences=None))
    return _json_response(
        UiAccountNotificationPreferencesResponse(
            preferences=UiNotificationChannels(email_enabled=info.email_enabled, in_app_enabled=info.in_app_enabled)
        )
    )


def _handle_ai_keys_context() -> Response:
    """GET /ui/api/ai-keys?workspace=<workspace_id>: context for the mint page.

    A machine's host id is also accepted as the coordinate while in-workspace
    deep links (written before workspace ids) transition.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    workspace_coordinate = request.args.get("workspace", "").strip()
    if not workspace_coordinate:
        return _json_response(
            UiAiKeysContext(
                workspace_id="",
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
    resolved = resolve_workspace_account(workspace_coordinate, record_store, get_state().session_store)
    if resolved is None:
        return _json_response(
            UiAiKeysContext(
                workspace_id=workspace_coordinate,
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
            workspace_id=resolved.workspace_id,
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
    blueprint.add_url_rule("/api/settings/notifications/test", view_func=_handle_test_notification, methods=["POST"])
    blueprint.add_url_rule("/api/settings/update-window", view_func=_handle_update_window_write, methods=["POST"])
    blueprint.add_url_rule("/api/settings/sign-in-browser", view_func=_handle_sign_in_browser_write, methods=["POST"])
    blueprint.add_url_rule("/api/settings/browser-import", view_func=_handle_browser_import_status)
    blueprint.add_url_rule(
        "/api/settings/browser-import",
        view_func=_handle_browser_import_run,
        methods=["POST"],
        endpoint="browser_import_run",
    )
    blueprint.add_url_rule(
        "/api/settings/browser-import/offered", view_func=_handle_browser_import_offered, methods=["POST"]
    )
    blueprint.add_url_rule("/api/accounts/<user_id>/plan", view_func=_handle_account_plan)
    blueprint.add_url_rule(
        "/api/accounts/<user_id>/notification-preferences",
        view_func=_handle_account_notification_preferences,
        methods=["GET", "POST"],
    )
    blueprint.add_url_rule("/api/ai-keys", view_func=_handle_ai_keys_context)
