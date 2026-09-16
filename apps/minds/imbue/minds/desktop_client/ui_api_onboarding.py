"""/ui/api routes for the first-run flow: the consent acknowledgement and onboarding completion.

Two tiny state transitions, both session-authed: acknowledging the
error-reporting notice (the JSON twin of the legacy ``POST /consent``), and
marking onboarding complete. The SPA routes to ``/consent`` while
``needs_error_reporting_consent`` (from ``/ui/api/app-status``) is true, and to
``/start`` while ``is_onboarding_complete`` is false and no workspace exists.
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


def _handle_onboarding_complete() -> Response:
    """Record that the installation is past the start flow (POST /ui/api/onboarding/complete).

    The SPA posts this when the start flow's "I already have one (log in)" answer
    signs in, since that path creates nothing (the create front door records the
    same fact for every create attempt on its own). Persisted, so the next launch
    lands on the home page rather than on the start flow again.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        minds_config.set_is_onboarding_complete(True)
    return _ok_response()


def register_onboarding_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/onboarding/consent", view_func=_handle_consent_acknowledge, methods=["POST"])
    blueprint.add_url_rule("/api/onboarding/complete", view_func=_handle_onboarding_complete, methods=["POST"])
