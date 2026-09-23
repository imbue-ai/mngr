"""/ui/api routes for looking other users up by email (the Share tab's resolve-on-add step).

``POST /ui/api/users/resolve`` turns a typed address into the identity record
of the verified account that owns it, so a grant can be stored under the
stable user id instead of the email. A miss is a 404 the tab treats as
"store an invite"; any other failure is a 502 with the same consequence.
"""

import json

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.identity_records import now_utc
from imbue.minds.desktop_client.identity_records import record_from_cli_identity
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated


class ResolveUserRequest(FrozenModel):
    """Body of ``POST /ui/api/users/resolve``."""

    email: str = Field(min_length=1, description="The address to resolve to an account")


def _json_response(payload: object, status_code: int) -> Response:
    return make_response(content=json.dumps(payload), status_code=status_code, media_type="application/json")


def _lookup_account(accounts: list[AccountSession]) -> AccountSession | None:
    """The account whose session performs the lookup: the active one, else any signed-in one."""
    for account in accounts:
        if account.is_active:
            return account
    return accounts[0] if accounts else None


def _handle_resolve_user() -> Response:
    if not is_ui_request_authenticated():
        return _json_response({"error": "Not authenticated"}, 401)
    try:
        body = ResolveUserRequest.model_validate(request.get_json(silent=True, force=True) or {})
    except ValidationError:
        return _json_response({"error": "email is required"}, 400)
    state = get_state()
    cli = state.imbue_cloud_cli
    session_store = state.session_store
    account = _lookup_account(session_store.list_accounts()) if session_store is not None else None
    if cli is None or account is None:
        return _json_response({"error": "Sign in to an Imbue account to look people up"}, 409)
    try:
        info = cli.resolve_user(account=str(account.email), email=body.email.strip())
    except ImbueCloudCliError as exc:
        logger.debug("Could not resolve {}: {}", body.email, exc)
        return _json_response({"error": f"Could not look up {body.email}: {exc}"}, 502)
    if info is None:
        return _json_response({"error": f"No Imbue account has the verified email {body.email}"}, 404)
    record = record_from_cli_identity(info)
    if state.identity_cache is not None:
        state.identity_cache.put(record, now_utc())
    return _json_response(record.model_dump(mode="json"), 200)


def register_user_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/users/resolve", view_func=_handle_resolve_user, methods=["POST"])
