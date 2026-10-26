"""/ui/api routes for the first-run flow: the consent answer, onboarding completion, and the sign-in landing.

Two tiny state transitions and one question, all session-authed: answering the
error-reporting consent question (on the consent screen, or on the start flow's
run question), marking onboarding complete, and answering where the start
flow's existing-login sign-in should land (whether the signed-in account has
any workspaces yet). The SPA routes to ``/consent`` while
``needs_error_reporting_consent`` (from ``/ui/api/app-status``) is true, and to
``/start`` while ``is_onboarding_complete`` is false and no workspace exists.
"""

import json

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import StrictBool
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.sync_scheduler import InitialSyncState
from imbue.minds.desktop_client.ui_account_scope import is_valid_account_id
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.sentry.core import write_latchkey_forward_sentry_consent


class UiConsentAnswer(FrozenModel):
    """Body of the consent answer; an empty body only marks the question answered."""

    report_unexpected_errors: StrictBool | None = Field(
        default=None, description="The consent checkbox: whether error data may be reported"
    )


def _ok_response() -> Response:
    return Response(json.dumps({"ok": True}), mimetype="application/json")


def _unauthenticated_response() -> Response:
    return Response(json.dumps({"error": "Not authenticated"}), status=401, mimetype="application/json")


def _handle_consent_answer() -> Response:
    """Record the user's answer to the error-reporting consent question (POST /ui/api/onboarding/consent).

    The consent screen and the start flow's run question both post here. The body's ``report_unexpected_errors``
    is their checkbox, which starts checked: it becomes the reporting setting Settings -> Error reporting shows.
    An empty body, or one without it, only marks the question answered and leaves the setting as it was. Either
    way the latchkey daemon's consent file is synced.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    body = request.get_json(silent=True, force=True) if request.get_data() else {}
    if not isinstance(body, dict):
        return make_json_error_response("The consent answer must be a JSON object", 400)
    try:
        answer = UiConsentAnswer.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed consent answer body: {}", e)
        return make_json_error_response(
            "The consent answer takes one optional field, report_unexpected_errors, a boolean", 400
        )
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        if answer.report_unexpected_errors is not None:
            minds_config.set_report_unexpected_errors(answer.report_unexpected_errors)
        minds_config.set_error_reporting_consent_given(True)
        write_latchkey_forward_sentry_consent(
            latchkey_forward_sentry_consent_path(minds_config.data_dir),
            is_error_reporting_enabled=minds_config.get_report_unexpected_errors(),
        )
    return _ok_response()


def _handle_onboarding_complete() -> Response:
    """Record that the installation is past the start flow (POST /ui/api/onboarding/complete).

    The SPA posts this when the start flow's "I have an account (log in)" answer
    signs in to an account that already has workspaces, since that path creates
    nothing (the create front door records the same fact for every create
    attempt on its own, which is how a sign-in landing on a workspace-less
    account completes onboarding: by the create its continued conversation
    submits). Persisted, so the next launch lands on the home page rather than
    on the start flow again.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    minds_config: MindsConfig | None = get_state().minds_config
    if minds_config is not None:
        minds_config.set_is_onboarding_complete(True)
    return _ok_response()


def _handle_signin_landing() -> Response:
    """Answer where the start flow's existing-login sign-in should land (GET /ui/api/onboarding/signin-landing).

    The flow asks after its "I have an account (log in)" sign-in completes, with
    the signed-in account id as ``?account=``: ``done`` with the account's
    active synced-workspace count once the first record pull has finished (or
    the records were already local), ``pending`` while that pull is still in
    flight, and ``failed`` when the last pass could not fetch the records (the
    sync loop keeps retrying on its own). An account with workspaces leaves the
    flow for its home page; one with none continues the conversation.
    """
    if not is_ui_request_authenticated():
        return _unauthenticated_response()
    account_id = request.args.get("account", "")
    if not is_valid_account_id(account_id):
        return make_json_error_response("The account query parameter must name an account id", 400)
    scheduler = get_state().sync_scheduler
    if scheduler is None:
        # No sync machinery (minimal test apps): there are no synced records to wait for.
        return Response(json.dumps({"state": "done", "workspace_count": 0}), mimetype="application/json")
    status = next((entry for entry in scheduler.list_initial_sync_statuses() if entry.user_id == account_id), None)
    if status is not None and status.state == InitialSyncState.PENDING:
        body = {"state": "pending", "workspace_count": 0}
    elif status is not None and status.state == InitialSyncState.FAILED:
        body = {"state": "failed", "workspace_count": 0}
    else:
        # DONE, or no tracking entry because the account's records were already
        # local when it signed in: the replica is current enough to answer from.
        records = scheduler.record_store.list_records(account_id)
        active_count = sum(1 for record in records if record.state == RECORD_STATE_ACTIVE)
        body = {"state": "done", "workspace_count": active_count}
    return Response(json.dumps(body), mimetype="application/json")


def register_onboarding_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/onboarding/consent", view_func=_handle_consent_answer, methods=["POST"])
    blueprint.add_url_rule("/api/onboarding/complete", view_func=_handle_onboarding_complete, methods=["POST"])
    blueprint.add_url_rule("/api/onboarding/signin-landing", view_func=_handle_signin_landing, methods=["GET"])
