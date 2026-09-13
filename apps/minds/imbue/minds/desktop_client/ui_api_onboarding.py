"""/ui/api routes owned by tranche T6 (onboarding/welcome/consent).

Two tiny state transitions the first-run flow needs, as JSON twins of the
legacy ``POST /consent`` and ``GET /welcome/skip`` handlers: acknowledging the
error-reporting notice, and choosing to continue without signing in. Both are
session-authed; the SPA routes to ``/consent`` while
``needs_error_reporting_consent`` (from ``/ui/api/app-status``) is true, and to
``/welcome`` on a functionally empty first run.
"""

import json

from flask import Blueprint
from flask import Response

from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent


def _ok_response() -> Response:
    return Response(json.dumps({"ok": True}), mimetype="application/json")


def _unauthenticated_response() -> Response:
    return Response(json.dumps({"error": "Not authenticated"}), status=401, mimetype="application/json")


def _handle_consent_acknowledge() -> Response:
    """Record that the user acknowledged the error-reporting notice (POST /ui/api/onboarding/consent).

    The notice is informational (no opt-out here -- Settings owns that), so
    this only flips the consent-given flag and syncs the latchkey daemon's
    consent file, matching the legacy ``POST /consent``.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        minds_config.set_error_reporting_consent_given(True)
        write_latchkey_forward_sentry_consent(
            latchkey_forward_sentry_consent_path(minds_config.data_dir),
            is_error_reporting_enabled=minds_config.get_report_unexpected_errors(),
        )
    return _ok_response()


def _handle_skip_account_setup() -> Response:
    """Record the continue-without-an-account choice (POST /ui/api/onboarding/skip-account-setup).

    Sets the per-run ``is_account_setup_skipped`` flag so the SPA's first-run
    routing stops sending the user back to the welcome splash; the choice is
    intentionally not persisted (a fresh cold start of an empty app re-offers
    it), matching the legacy ``/welcome/skip``.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    get_state().is_account_setup_skipped = True
    return _ok_response()


def register_onboarding_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/onboarding/consent", view_func=_handle_consent_acknowledge, methods=["POST"])
    blueprint.add_url_rule(
        "/api/onboarding/skip-account-setup", view_func=_handle_skip_account_setup, methods=["POST"]
    )
