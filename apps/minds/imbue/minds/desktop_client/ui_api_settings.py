"""/ui/api routes owned by tranche T2 (Settings / Accounts / AI keys).

JSON twins of the data that used to be server-rendered into the Settings,
Accounts, and AI-keys pages, plus the settings writes the SPA performs
directly. Mutating flows the legacy POST routes already implement (permission
revokes, connector add/disconnect, plan switch, trim, logout, key mint,
master-password change) are reused by the SPA as-is and stay in ``app.py``.

The account-keyed settings (the update window, the auto-update toggle and its
one-time offer's answer, the master notifications toggle) are edited through
one route pair, ``GET``/``PUT /ui/api/settings/<account_id>`` (spec section
7.3). Each account id reads and writes its own replica of the synced
``settings`` document (phase 3A of ``specs/user-profile-state/spec.md``);
the PUT marks the replica dirty and kicks the sync scheduler, whose next
pass pushes it. The overview takes the window's account as a query
parameter, so the page and the per-account record can never disagree.

The error-reporting and notification-style writes are consent-adjacent
records, so each carries the optimistic-concurrency contract:
``GET /ui/api/settings`` returns a per-record ``version`` derived from that
record's stored values, and the write requires that version in ``If-Match``
(412 on mismatch, 428 when absent) so a stale window can never silently
clobber a newer change. The account-keyed PUT is partial and unguarded, like
the plain-preference writes it replaces: each field is independent, so the
last write to a field is simply the newest choice.
"""

import hashlib
import json
import sys
from typing import Callable
from typing import TypeVar

from flask import Blueprint
from flask import Flask
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.account_plan_view import build_account_plan_view
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.backup_trim import BackupTrimStatus
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.resolve import resolve_auto_update_prompt_decision
from imbue.minds.desktop_client.settings.resolve import resolve_auto_update_workspaces
from imbue.minds.desktop_client.settings.resolve import resolve_notification_style
from imbue.minds.desktop_client.settings.resolve import resolve_notifications_enabled
from imbue.minds.desktop_client.settings.resolve import resolve_report_unexpected_errors
from imbue.minds.desktop_client.settings.resolve import resolve_sign_in_browser
from imbue.minds.desktop_client.settings.resolve import resolve_update_window
from imbue.minds.desktop_client.settings.storage import load_settings_context
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_account_scope import is_valid_account_id
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.errors import SettingsRecordTooNewError
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from imbue.mngr_latchkey.core import LatchkeyErrorCode

_WriteT = TypeVar("_WriteT", bound=FrozenModel)


class UiNotificationPrefs(FrozenModel):
    """The notification-prefs record on the settings overview.

    ``is_enabled`` is the account-keyed master toggle, written through the
    account-settings PUT; it rides here so one read serves the panel. The
    ``version`` guards only the install-keyed style write.
    """

    is_enabled: bool = Field(description="Master notifications toggle (gates every OS nudge the app sends)")
    style: NotificationStyle = Field(description="Delivery style for every feed entry")
    has_chosen: bool = Field(description="Whether notification preferences have been explicitly saved")
    version: str = Field(description="If-Match version for the notification-style write")


class UiNotificationStyleWrite(FrozenModel):
    """Body of the notification-style write; the master toggle goes through the account-settings PUT."""

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
    is_auto_update_enabled: bool = Field(
        default=False, description="Whether Imbue Studio arms every behind workspace's update itself"
    )
    auto_update_prompt_decision: AutoUpdatePromptDecision | None = Field(
        default=None, description="How the one-time auto-update offer was answered; None while it never was"
    )
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
    error_code: LatchkeyErrorCode | None = Field(
        default=None,
        description="Latchkey's code for the failure when it gave one this package knows; None otherwise",
    )
    is_full_disk_access_needed: bool = Field(
        default=False,
        description=(
            "Whether the fix is granting the app Full Disk Access: the Chrome profile could not be read "
            "and this desktop is a Mac, where that is what denies every application a look at Chrome's data"
        ),
    )


def is_full_disk_access_needed(error_code: LatchkeyErrorCode | None, platform: str) -> bool:
    """Whether a failed import is the macOS Full Disk Access case.

    Latchkey reports an unreadable Chrome profile with the same code on every
    platform (a plain file-permission problem gets it on Linux), but only on
    macOS is the remedy the Full Disk Access list. ``platform`` is
    ``sys.platform`` of this desktop, the one latchkey ran on.
    """
    return error_code is LatchkeyErrorCode.CHROME_PROFILE_NOT_READABLE and platform == "darwin"


class UiErrorReportingWrite(FrozenModel):
    """Body of the error-reporting opt-out write."""

    report_unexpected_errors: bool = Field(description="New value for the per-machine flag")


class UiSignInBrowserWrite(FrozenModel):
    """Body of the sign-in browser write."""

    browser_id: str | None = Field(description="An installed browser's id, or None for the default browser")


class UiAccountSettings(FrozenModel):
    """The account-keyed settings record, as the GET answers and the PUT echoes it."""

    settings_format: int = Field(description="The record's shape version")
    update_window_start_hour: int = Field(description="Local hour scheduled machine updates may start running at")
    update_window_end_hour: int = Field(description="Local hour scheduled machine updates stop running at")
    auto_update_workspaces: bool = Field(
        description="Whether Imbue Studio arms every behind workspace's update itself"
    )
    auto_update_prompt_decision: AutoUpdatePromptDecision | None = Field(
        description="How the one-time auto-update offer was answered; None while it never was"
    )
    notifications_enabled: bool = Field(description="Master notifications toggle")


class UiAccountSettingsWrite(FrozenModel):
    """Body of the account-settings PUT: any subset of the record's fields, each applied as given.

    The window hours come as a pair or not at all -- half a window is not a
    preference anyone holds. ``auto_update_prompt_decision`` can only be set
    (never cleared), so an absent field and a null field read the same.
    """

    update_window_start_hour: int | None = Field(default=None, ge=0, le=23, description="Local hour the window opens")
    update_window_end_hour: int | None = Field(default=None, ge=0, le=23, description="Local hour the window closes")
    auto_update_workspaces: bool | None = Field(default=None, description="New auto-update toggle value")
    auto_update_prompt_decision: AutoUpdatePromptDecision | None = Field(
        default=None, description="How the one-time offer was answered"
    )
    notifications_enabled: bool | None = Field(default=None, description="New master notifications toggle value")


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
    is_sharing_workspaces: bool = Field(
        default=False, description="Gates the review-shared-workspaces link (the account holds an active share)"
    )


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


def compute_notification_style_version(style: str, has_chosen: bool) -> str:
    """The If-Match version of the notification-style record: a hash of its stored values.

    A per-record version (rather than folding these values into the
    error-reporting version) keeps each record's writes from 412-ing pages
    that only touched the other record.
    """
    canonical = json.dumps({"style": style, "has_chosen": has_chosen}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _is_notification_choice_recorded() -> bool:
    """Whether notification preferences have ever been chosen on this install.

    A saved delivery style in ``config.toml``, or a master toggle in any
    account's settings record (the device's included), counts. Install-wide
    on purpose: the one-time first-choice card asks once per install, as it
    did when one install-wide copy backed every account.
    """
    minds_config = get_state().minds_config
    if minds_config is None:
        # No settings storage: there is nowhere to save a choice.
        return True
    if minds_config.get_notification_style_with_choice()[1]:
        return True
    settings_store = get_state().account_settings_store
    return settings_store is not None and settings_store.is_notifications_enabled_chosen_anywhere()


def _current_notification_prefs(context: SettingsResolutionContext) -> UiNotificationPrefs:
    """The notification-prefs record (defaults when no MindsConfig is wired)."""
    has_chosen = _is_notification_choice_recorded()
    style = resolve_notification_style(context)
    return UiNotificationPrefs(
        is_enabled=resolve_notifications_enabled(context),
        style=style,
        has_chosen=has_chosen,
        version=compute_notification_style_version(style, has_chosen),
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
    """GET /ui/api/settings?account=<id>: the app-level settings page's full data payload.

    Every declared setting's value comes through the resolution path, with the
    account-keyed ones resolved for the window's account (the ``account``
    query parameter; the device's when absent), so this page and the
    per-account record can never disagree.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    account_id = request.args.get("account", LOCAL_PROFILE_USER_ID)
    if not is_valid_account_id(account_id):
        return _error_response("The account parameter must name an account id", 400)
    context = load_settings_context(get_state().minds_config, get_state().account_settings_store, account_id)
    report_unexpected_errors = resolve_report_unexpected_errors(context)
    update_window = resolve_update_window(context)
    browsers = get_state().sign_in_browsers.list_browsers()
    stored_browser_id = resolve_sign_in_browser(context)
    overview = UiSettingsOverview(
        is_master_password_set=_is_any_account_master_password_set(),
        report_unexpected_errors=report_unexpected_errors,
        version=compute_error_reporting_version(report_unexpected_errors),
        notification_prefs=_current_notification_prefs(context),
        update_window_start_hour=update_window[0],
        update_window_end_hour=update_window[1],
        is_auto_update_enabled=resolve_auto_update_workspaces(context),
        auto_update_prompt_decision=resolve_auto_update_prompt_decision(context),
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
    write = _read_json_write(write_model_type)
    if isinstance(write, Response):
        return write
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
    # Mirror the change into the ``mngr latchkey forward`` child's
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


def _apply_notification_style_write(
    minds_config: MindsConfig, write: UiNotificationStyleWrite, expected_version: str
) -> str | None:
    # Compare-and-swap under one MindsConfig lock hold, same rationale as
    # _apply_error_reporting_write above: the version check and the write must not be two
    # separate locked calls, or a concurrent writer starting from the same version could
    # slip in between them and silently clobber this write.
    settings_store = get_state().account_settings_store
    return minds_config.set_notification_style_if_version_matches(
        expected_version=expected_version,
        compute_version=compute_notification_style_version,
        style=write.style,
        # The served version folds in a master-toggle choice recorded in the
        # settings records, so the check must see the same has_chosen.
        is_chosen_elsewhere=(settings_store is not None and settings_store.is_notifications_enabled_chosen_anywhere()),
    )


def _handle_notification_style_write() -> Response:
    """POST /ui/api/settings/notifications: If-Match-guarded notification-style write.

    The master toggle is account-keyed and goes through the account-settings
    PUT; this writes the install-keyed delivery style alone.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config = get_state().minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    return _handle_if_match_write(
        minds_config=minds_config,
        write_model_type=UiNotificationStyleWrite,
        apply_versioned_write=_apply_notification_style_write,
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
    reason, so the dialog can show it and go on to the sign-in; when latchkey
    coded the failure, ``error_code`` carries it, and
    ``is_full_disk_access_needed`` says whether this desktop's platform makes
    the Full Disk Access list the fix, so the client can say what to do rather
    than quote the reason. Also counts as the offer having been made, since
    running it is one way of answering it.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    handler = _find_permission_grant_handler()
    if handler is None:
        return _error_response("Browser cookies are not configured on this desktop", 503)
    minds_config = get_state().minds_config
    if minds_config is not None:
        minds_config.set_is_browser_import_offered(True)
    outcome = handler.latchkey.import_chrome_browser_state()
    return _json_response(
        UiBrowserImportResult(
            is_success=outcome.is_success,
            detail=outcome.detail,
            error_code=outcome.error_code,
            is_full_disk_access_needed=is_full_disk_access_needed(outcome.error_code, sys.platform),
        )
    )


def _empty_update_window_response() -> Response:
    return _error_response("The update window needs a start and end that differ", 400)


def _resolved_account_settings(account_id: str) -> UiAccountSettings:
    """The account's record as the routes serve it, through the resolution path."""
    context = load_settings_context(get_state().minds_config, get_state().account_settings_store, account_id)
    update_window = resolve_update_window(context)
    return UiAccountSettings(
        settings_format=context.account.settings_format,
        update_window_start_hour=update_window[0],
        update_window_end_hour=update_window[1],
        auto_update_workspaces=resolve_auto_update_workspaces(context),
        auto_update_prompt_decision=resolve_auto_update_prompt_decision(context),
        notifications_enabled=resolve_notifications_enabled(context),
    )


def _handle_account_settings(account_id: str) -> Response:
    """GET/PUT /ui/api/settings/<account_id>: the account's settings record (spec section 7.3).

    Each account id reads and writes its own replica of the synced ``settings``
    document; the PUT marks it dirty and kicks the sync scheduler, whose next
    pass pushes it. The PUT is partial (any subset of the record's fields) and
    unguarded, like the plain-preference writes it replaced: each field is
    independent, so the last write to a field is simply the newest choice.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    if not is_valid_account_id(account_id):
        return _error_response("The path must name an account id", 400)
    if request.method != "PUT":
        return _json_response(_resolved_account_settings(account_id))
    settings_store = get_state().account_settings_store
    if settings_store is None:
        return _error_response("Settings storage is not configured", 503)
    write = _read_json_write(UiAccountSettingsWrite)
    if isinstance(write, Response):
        return write
    if (write.update_window_start_hour is None) != (write.update_window_end_hour is None):
        return _error_response("The update window needs both its start and end hours", 400)
    if write.update_window_start_hour is not None and write.update_window_start_hour == write.update_window_end_hour:
        return _empty_update_window_response()
    try:
        # Only the explicitly-sent fields land: a key never chosen stays
        # absent from the record, which is what "never chosen" means to it.
        settings_store.apply_settings_values(account_id, write.model_dump(mode="json", exclude_none=True))
    except SettingsRecordTooNewError:
        return _error_response(
            f"These settings were saved by a newer version of Imbue Studio "
            f"(format {settings_store.get_stored_settings_format(account_id)}) and can only be changed there",
            409,
        )
    sync_scheduler = get_state().sync_scheduler
    if sync_scheduler is not None:
        sync_scheduler.kick()
    # Every armed workspace's file carries the window, and the updates frame
    # carries the auto-update fields; the sweep rewrites both.
    if write.update_window_start_hour is not None or write.auto_update_workspaces is not None:
        apply_account_settings_change_side_effects()
    return _json_response(_resolved_account_settings(account_id))


def apply_account_settings_change_side_effects(app: Flask | None = None) -> None:
    """Re-resolve every surface an account-settings change moves: the armed windows and the updates frame.

    Called by the PUT (in request context) and, with the app passed
    explicitly, by the sync scheduler when a pull lands another device's
    change.
    """
    state = get_state(app)
    if state.workspace_update_service is not None:
        state.workspace_update_service.detector.request_pass()
    if state.ui_publisher is not None:
        state.ui_publisher.notify_change()


def _read_json_write(model: type[_WriteT]) -> _WriteT | Response:
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error_response("Invalid JSON body", 400)
    try:
        return model.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed {} body: {}", model.__name__, e)
        return _error_response("Invalid JSON body", 400)


def _handle_sign_in_browser_write() -> Response:
    """POST /ui/api/settings/sign-in-browser: choose the browser provider sign-ins open in."""
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    state = get_state()
    minds_config = state.minds_config
    if minds_config is None:
        return _error_response("Settings storage is not configured", 503)
    write = _read_json_write(UiSignInBrowserWrite)
    if isinstance(write, Response):
        return write
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
                    "This page needs to be opened from a workspace: use the Sign in with Imbue "
                    "option in the workspace's Claude sign-in dialog."
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
                    "This workspace has no associated Imbue account. Associate an account on the "
                    "workspace's settings page, then come back here."
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
    # Werkzeug ranks static segments over the <account_id> converter, so the
    # named sub-routes below stay reachable; no real account id collides with
    # them (SuperTokens ids are UUIDs, and the device's is "local").
    blueprint.add_url_rule("/api/settings/<account_id>", view_func=_handle_account_settings, methods=["GET", "PUT"])
    blueprint.add_url_rule("/api/settings/error-reporting", view_func=_handle_error_reporting_write, methods=["POST"])
    blueprint.add_url_rule("/api/settings/notifications", view_func=_handle_notification_style_write, methods=["POST"])
    blueprint.add_url_rule("/api/settings/notifications/test", view_func=_handle_test_notification, methods=["POST"])
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
