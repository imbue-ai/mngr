"""/ui/api routes for the workspaces shared with the window's account (specs/shared-with-me/spec.md, 4.7).

Every route is scoped to the window's account (``?account=<user id>``), which
must be signed in here. The rows come from the desktop's cache of the
connector's listing joined with the account's own entries (verdicts, pasted
links); the actions go to the connector under that account through the
``mngr imbue_cloud`` CLI, then refetch the listing so the row the SPA gets
back is the connector's view.

Opening is the shell's job: ``open`` records the acceptance and answers the
target's link beside the redeem URL of the reverse device handoff. The SPA
navigates the workspace frame to that URL, which signs the window's account
partition in on the accounts origin and sends the frame on to the link; the
code it carries is single-use and expires in a minute. The shell then
reports what the frame learned to ``verdict``.
"""

import json
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import Final
from urllib.parse import urlencode

import httpx2
from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.shared_with_me_store import SharedAccessVerdict
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeStore
from imbue.minds.desktop_client.shared_with_me_store import SharedWorkspaceRow
from imbue.minds.desktop_client.shared_with_me_store import normalize_manual_link
from imbue.minds.desktop_client.sharing_handler import probe_share_origin
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_account_scope import ACCOUNT_QUERY_PARAMETER
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.errors import AccountDocumentTooNewError
from imbue.mngr.utils.thread_cleanup import start_mngr_thread
from imbue.mngr_imbue_cloud.errors import RATE_LIMITED_CODE
from imbue.mngr_imbue_cloud.errors import SHARE_NOT_FOUND_CODE

# The connector's response kinds, as the CLI spells them.
RESPONSE_ACCEPTED: Final[str] = "accepted"
RESPONSE_DECLINED: Final[str] = "declined"
RESPONSE_REPORTED: Final[str] = "reported"
STATUS_INVITED: Final[str] = "invited"
# The redeem page of the reverse device handoff, on the accounts origin (connector 4.8).
REDEEM_SESSION_PATH: Final[str] = "/accounts/session"
# The gateway's health route, served with CORS on every shared origin: how a pasted link is checked.
_SHARE_HEALTH_PATH: Final[str] = "/_health"
# The gateway answers a probe without a gateway session with a bare 204, and with 200 and detail otherwise.
_SHARE_HEALTH_ALIVE_STATUS_CODES: Final[frozenset[int]] = frozenset({200, 204})
# The endpoint-name prefix of every route here, which the trust gate
# (shared_with_me_gate.py) exempts: these routes act on shared rows by design.
SHARED_WITH_ME_ENDPOINT_PREFIX: Final[str] = "shared_with_me_"
# How long an open's acceptance waits for the mint to hold the warm mngr process before going ahead regardless.
_ACCEPT_AFTER_MINT_CLAIM_TIMEOUT_SECONDS: Final[float] = 5.0
# How long an open waits for its acceptance thread: its two CLI calls (the response, then the listing refetch), each
# with the CLI's own 60-second timeout, with room to report them.
_OPEN_GROUP_EXIT_TIMEOUT_SECONDS: Final[float] = 150.0


class OpenSharedWorkspaceRequest(FrozenModel):
    """Body of ``POST /ui/api/shared-with-me/<share_key>/open``."""

    app: str | None = Field(default=None, description="The app to open; None opens the shell")


class SharedWorkspaceVerdictRequest(FrozenModel):
    """Body of ``POST /ui/api/shared-with-me/<share_key>/verdict``: what the frame's load learned."""

    verdict: SharedAccessVerdict = Field(description="admitted / refused / misconfigured / offline / sign_in_failed")


class AddSharedLinkRequest(FrozenModel):
    """Body of ``POST /ui/api/shared-with-me/links``: a link the user was given out of band."""

    link: str = Field(min_length=1, description="The pasted link; only its https origin is kept")
    label: str = Field(default="", description="What the tile should call it; empty shows the host")


class SharedWorkspaceOpenPlan(FrozenModel):
    """What the shell needs to open a shared target (spec 4.7 and 4.8)."""

    share_key: str = Field(description="The row's key, which the workspace route carries")
    host_id: str = Field(description="The share's host id; empty for a pasted link")
    link: str = Field(description="The target's share URL to load in the frame")
    redeem_url: str | None = Field(
        description="Signs the partition in on the accounts origin and sends it on to the link; None when unavailable"
    )
    status: str = Field(description="The row's status after the open was recorded")


@pure
def redeem_session_url(accounts_origin: str, code: str, next_url: str) -> str:
    return f"{accounts_origin.rstrip('/')}{REDEEM_SESSION_PATH}?{urlencode({'code': code, 'next': next_url})}"


def _json(payload: object, status_code: int) -> Response:
    return make_response(content=json.dumps(payload), status_code=status_code, media_type="application/json")


def _row_payload(row: SharedWorkspaceRow) -> dict[str, object]:
    return row.model_dump(mode="json")


class _AccountScope(FrozenModel):
    """What every route resolves first: the signed-in account the window is for, and the store."""

    account: AccountSession = Field(description="The window's account, signed in here")
    store: SharedWithMeStore = Field(description="The listing cache and the account documents")

    @property
    def user_id(self) -> str:
        return str(self.account.user_id)

    @property
    def account_email(self) -> str:
        return str(self.account.email)


class _RowScope(_AccountScope):
    """An account scope on one row of the account's listing."""

    row: SharedWorkspaceRow = Field(description="The row the route's share key names")


class _ConnectorRowScope(_RowScope):
    """A row scope for a route that records something at the connector under the account."""

    cli: ImbueCloudCli = Field(description="The connector transport")


def _account_scope() -> _AccountScope | Response:
    """The window's signed-in account and the store, or the refusal to answer with."""
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    account_id = request.args.get(ACCOUNT_QUERY_PARAMETER, "")
    session_store = get_state().session_store
    account = session_store.get_session(account_id) if session_store is not None and account_id else None
    if account is None:
        return make_json_error_response(
            "The window's account is not signed in here", 409, code="account_not_signed_in"
        )
    store = get_state().shared_with_me_store
    if store is None:
        return _unconfigured_response()
    return _AccountScope(account=account, store=store)


def _unconfigured_response() -> Response:
    return make_json_error_response("Imbue Cloud is not configured on this app", 503)


def _row_scope(share_key: str) -> _RowScope | Response:
    """The account scope plus the row ``share_key`` names, or the refusal (404 ``not_shared`` when it names none)."""
    scope = _account_scope()
    if isinstance(scope, Response):
        return scope
    row = scope.store.find_row(scope.user_id, share_key)
    if row is None:
        return make_json_error_response("Nothing with that key is shared with this account", 404, code="not_shared")
    return _RowScope(account=scope.account, store=scope.store, row=row)


def _connector_row_scope(share_key: str) -> _ConnectorRowScope | Response:
    """A row scope with the connector transport, or the refusal (503 when this app has no transport)."""
    scope = _row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    cli = get_state().imbue_cloud_cli
    if cli is None:
        return _unconfigured_response()
    return _ConnectorRowScope(account=scope.account, store=scope.store, row=scope.row, cli=cli)


def _too_new_response(exc: AccountDocumentTooNewError) -> Response:
    """The answer when the entry was written by a newer app and may only be changed there."""
    return make_json_error_response(str(exc), 409, code="too_new")


def _refusal_response(scope: _AccountScope, exc: ImbueCloudCliError, what: str) -> Response:
    """The answer to a connector refusal; a share that no longer names the account also refreshes the stale listing."""
    if isinstance(exc, ImbueCloudShareRefusedCliError):
        if exc.code == SHARE_NOT_FOUND_CODE:
            scope.store.refresh(scope.user_id, scope.account_email, datetime.now(timezone.utc))
            return make_json_error_response(str(exc), 404, code=exc.code)
        status_code = 429 if exc.code == RATE_LIMITED_CODE else 409
        return make_json_error_response(str(exc), status_code, code=exc.code)
    logger.warning("Could not {}: {}", what, exc)
    return make_json_error_response(f"Could not {what}: {exc}", 502)


def _row_after_response(scope: _RowScope, response: str | None) -> SharedWorkspaceRow:
    """The row as the connector lists it after a recorded response; the cached row with the response applied when the refetch failed."""
    share_key = scope.row.share_key
    if not scope.store.refresh(scope.user_id, scope.account_email, datetime.now(timezone.utc)):
        if response == RESPONSE_REPORTED:
            scope.store.drop_share(scope.user_id, share_key)
        else:
            scope.store.apply_status(scope.user_id, share_key, response if response is not None else STATUS_INVITED)
    return scope.store.find_row(scope.user_id, share_key) or scope.row


def _respond_and_refresh(scope: _ConnectorRowScope, response: str) -> SharedWorkspaceRow | Response:
    """Record a response at the connector and refetch the listing, answering the row as the connector now lists it."""
    try:
        scope.cli.respond_to_share(account=scope.account_email, host_id=scope.row.host_id, response=response)
    except ImbueCloudCliError as exc:
        return _refusal_response(scope, exc, f"record the response to {scope.row.share_key}")
    return _row_after_response(scope, response)


def _listing_payload(scope: _AccountScope) -> dict[str, object]:
    cache = scope.store.read_cache(scope.user_id)
    return {
        "account_id": scope.user_id,
        "fetched_at": cache.fetched_at.isoformat() if cache is not None else None,
        "shares": [_row_payload(row) for row in scope.store.list_rows(scope.user_id)],
    }


def _handle_list() -> Response:
    scope = _account_scope()
    if isinstance(scope, Response):
        return scope
    return _json(_listing_payload(scope), 200)


def _handle_refresh() -> Response:
    scope = _account_scope()
    if isinstance(scope, Response):
        return scope
    if get_state().imbue_cloud_cli is None:
        return _unconfigured_response()
    if not scope.store.refresh(scope.user_id, scope.account_email, datetime.now(timezone.utc)):
        return make_json_error_response("Could not fetch what is shared with you; the last listing is kept", 502)
    return _json(_listing_payload(scope), 200)


def _handle_open(share_key: str) -> Response:
    """Answer the shell's plan for a target; the connector is needed only to accept an invitation and to mint the redeem code."""
    scope = _row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    try:
        body = OpenSharedWorkspaceRequest.model_validate(request.get_json(silent=True, force=True) or {})
    except ValidationError:
        return make_json_error_response("app must be a string", 400)
    row = scope.row
    link = row.link_for_app(body.app)
    if link is None:
        return make_json_error_response("The workspace has not registered that address yet", 409, code="no_link")
    cli = get_state().imbue_cloud_cli
    # Opening accepts an invitation; a share already accepted or declined keeps its status.
    is_accepting = not row.is_manual and row.status == STATUS_INVITED
    if is_accepting and cli is None:
        return _unconfigured_response()
    acceptance = (
        _ConnectorRowScope(account=scope.account, store=scope.store, row=row, cli=cli)
        if is_accepting and cli is not None
        else None
    )
    redeem_url, accepted = _mint_while_accepting(cli, scope.account, link, acceptance)
    if isinstance(accepted, Response):
        return accepted
    opened_row = row if accepted is None else accepted
    plan = SharedWorkspaceOpenPlan(
        share_key=opened_row.share_key,
        host_id=opened_row.host_id,
        link=link,
        redeem_url=redeem_url,
        status=opened_row.status,
    )
    return _json(plan.model_dump(mode="json"), 200)


def _mint_while_accepting(
    cli: ImbueCloudCli | None,
    account: AccountSession,
    link: str,
    acceptance: _ConnectorRowScope | None,
) -> tuple[str | None, SharedWorkspaceRow | Response | None]:
    """Mint the redeem URL for ``link`` and, given ``acceptance``, record the acceptance of its row alongside.

    The two connector calls run side by side, the mint first: the frame cannot
    load before the code exists, while the acceptance is bookkeeping, and the
    app keeps one warm ``mngr`` process at a time, so the acceptance starts
    only once the mint holds it and takes the cold one. Answers the redeem URL
    (None when no code was minted) and the acceptance's outcome: the row as
    the connector now lists it, the connector's refusal, or None when nothing
    was to accept.
    """
    if acceptance is None:
        return _redeem_url_or_none(cli, account, link), None
    is_mint_process_claimed = threading.Event()
    accepted: list[SharedWorkspaceRow | Response] = []
    parent_group = get_state().root_concurrency_group
    group_name = f"shared-with-me-open-{acceptance.row.share_key}"
    group = (
        parent_group.make_concurrency_group(name=group_name, exit_timeout_seconds=_OPEN_GROUP_EXIT_TIMEOUT_SECONDS)
        if parent_group is not None
        else ConcurrencyGroup(name=group_name, exit_timeout_seconds=_OPEN_GROUP_EXIT_TIMEOUT_SECONDS)
    )
    with group:
        start_mngr_thread(
            group,
            target=_accept_once_the_mint_holds_its_process,
            args=(is_mint_process_claimed, acceptance, accepted),
            name=f"{group_name}-accept",
        )
        try:
            redeem_url = _redeem_url_or_none(cli, account, link, is_mint_process_claimed.set)
        finally:
            is_mint_process_claimed.set()
    return redeem_url, accepted[0] if accepted else None


def _accept_once_the_mint_holds_its_process(
    is_mint_process_claimed: threading.Event,
    acceptance: _ConnectorRowScope,
    accepted_out: list[SharedWorkspaceRow | Response],
) -> None:
    """The acceptance thread of ``_mint_while_accepting``: waits for the mint to hold its process, then accepts."""
    is_mint_process_claimed.wait(_ACCEPT_AFTER_MINT_CLAIM_TIMEOUT_SECONDS)
    accepted_out.append(_respond_and_refresh(acceptance, RESPONSE_ACCEPTED))


def _redeem_url_or_none(
    cli: ImbueCloudCli | None,
    account: AccountSession,
    link: str,
    # Runs once the mint holds its mngr process (or never, when no code is minted).
    on_process_claimed: Callable[[], None] | None = None,
) -> str | None:
    """The reverse handoff's redeem URL for the link; None when the code could not be minted (the shell may still try)."""
    client_env_config = get_state().client_env_config
    if cli is None or client_env_config is None:
        logger.debug("Opening {} without a partition sign-in: this app has no connector transport or env config", link)
        return None
    try:
        minted = cli.mint_browser_session_code(account=str(account.email), on_process_claimed=on_process_claimed)
    except ImbueCloudCliError as exc:
        logger.warning("Could not mint a browser session code for {}: {}", account.email, exc)
        return None
    return redeem_session_url(client_env_config.accounts_origin_url(), minted.code, link)


def _handle_decline(share_key: str) -> Response:
    return _respond_route(share_key, RESPONSE_DECLINED)


def _handle_report(share_key: str) -> Response:
    return _respond_route(share_key, RESPONSE_REPORTED)


def _respond_route(share_key: str, response: str) -> Response:
    scope = _connector_row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    if scope.row.is_manual:
        return make_json_error_response(
            "A pasted link has no owner to respond to; remove it instead", 409, code="manual"
        )
    updated = _respond_and_refresh(scope, response)
    if isinstance(updated, Response):
        return updated
    if response == RESPONSE_REPORTED:
        # A reported share is never listed again, so there is no row to answer.
        return _json({"share_key": share_key, "response": response, "row": None}, 200)
    return _json({"share_key": share_key, "response": response, "row": _row_payload(updated)}, 200)


def _handle_undo_decline(share_key: str) -> Response:
    scope = _connector_row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    if scope.row.is_manual:
        return make_json_error_response("A pasted link has no response to clear", 409, code="manual")
    try:
        scope.cli.clear_share_response(account=scope.account_email, host_id=scope.row.host_id)
    except ImbueCloudCliError as exc:
        return _refusal_response(scope, exc, f"clear the response to {share_key}")
    updated = _row_after_response(scope, None)
    return _json({"share_key": share_key, "response": None, "row": _row_payload(updated)}, 200)


def _handle_retry(share_key: str) -> Response:
    scope = _row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    try:
        scope.store.clear_verdict(scope.user_id, share_key)
    except AccountDocumentTooNewError as exc:
        return _too_new_response(exc)
    updated = scope.store.find_row(scope.user_id, share_key) or scope.row
    return _json({"share_key": share_key, "row": _row_payload(updated)}, 200)


def _handle_remove(share_key: str) -> Response:
    """Remove a row: tombstone a pasted link's entry (no row remains), or decline a listed share (its row stays, declined)."""
    scope = _row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    if scope.row.is_manual:
        try:
            scope.store.remove_manual_link(scope.user_id, share_key)
        except AccountDocumentTooNewError as exc:
            return _too_new_response(exc)
        return _json({"share_key": share_key, "outcome": "removed", "row": None}, 200)
    cli = get_state().imbue_cloud_cli
    if cli is None:
        return _unconfigured_response()
    connector_scope = _ConnectorRowScope(account=scope.account, store=scope.store, row=scope.row, cli=cli)
    updated = _respond_and_refresh(connector_scope, RESPONSE_DECLINED)
    if isinstance(updated, Response):
        return updated
    return _json({"share_key": share_key, "outcome": "declined", "row": _row_payload(updated)}, 200)


def _handle_verdict(share_key: str) -> Response:
    scope = _row_scope(share_key)
    if isinstance(scope, Response):
        return scope
    try:
        body = SharedWorkspaceVerdictRequest.model_validate(request.get_json(silent=True, force=True) or {})
    except ValidationError:
        return make_json_error_response(
            "verdict must be one of: " + ", ".join(verdict.value for verdict in SharedAccessVerdict), 400
        )
    try:
        scope.store.record_verdict(scope.user_id, share_key, body.verdict, datetime.now(timezone.utc))
    except AccountDocumentTooNewError as exc:
        return _too_new_response(exc)
    updated = scope.store.find_row(scope.user_id, share_key) or scope.row
    return _json({"share_key": share_key, "row": _row_payload(updated)}, 200)


def _probe_share_health(http_client: httpx2.Client, link: str) -> bool:
    """Whether the origin answers the share gateway's health route (so the link is a shared workspace's)."""
    return probe_share_origin(http_client, f"{link.rstrip('/')}{_SHARE_HEALTH_PATH}", _SHARE_HEALTH_ALIVE_STATUS_CODES)


def _handle_add_link() -> Response:
    scope = _account_scope()
    if isinstance(scope, Response):
        return scope
    try:
        body = AddSharedLinkRequest.model_validate(request.get_json(silent=True, force=True) or {})
    except ValidationError:
        return make_json_error_response("link is required", 400)
    link = normalize_manual_link(body.link)
    if link is None:
        return make_json_error_response("Paste the https link of a shared workspace", 400, code="not_a_link")
    http_client = get_state().http_client
    if http_client is None:
        return make_json_error_response("This app cannot check pasted links", 503)
    if not _probe_share_health(http_client, link):
        return make_json_error_response(
            "That address is not a shared workspace, or it is not reachable right now", 409, code="not_a_share"
        )
    try:
        row = scope.store.add_manual_link(scope.user_id, link, body.label.strip())
    except AccountDocumentTooNewError as exc:
        return _too_new_response(exc)
    return _json({"row": _row_payload(row)}, 200)


def register_shared_with_me_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint, each endpoint named under the gate's exemption prefix."""
    rules: list[tuple[str, str, Callable[..., Response], list[str]]] = [
        ("/api/shared-with-me", "list", _handle_list, ["GET"]),
        ("/api/shared-with-me/refresh", "refresh", _handle_refresh, ["POST"]),
        ("/api/shared-with-me/links", "add_link", _handle_add_link, ["POST"]),
        ("/api/shared-with-me/<share_key>/open", "open", _handle_open, ["POST"]),
        ("/api/shared-with-me/<share_key>/decline", "decline", _handle_decline, ["POST"]),
        ("/api/shared-with-me/<share_key>/decline", "undo_decline", _handle_undo_decline, ["DELETE"]),
        ("/api/shared-with-me/<share_key>/report", "report", _handle_report, ["POST"]),
        ("/api/shared-with-me/<share_key>/retry", "retry", _handle_retry, ["POST"]),
        ("/api/shared-with-me/<share_key>/remove", "remove", _handle_remove, ["POST"]),
        ("/api/shared-with-me/<share_key>/verdict", "verdict", _handle_verdict, ["POST"]),
    ]
    for rule, name, view_func, methods in rules:
        blueprint.add_url_rule(
            rule, endpoint=f"{SHARED_WITH_ME_ENDPOINT_PREFIX}{name}", view_func=view_func, methods=methods
        )
