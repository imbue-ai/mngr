"""`mngr imbue_cloud auth ...` subcommands."""

import base64
import getpass
import hashlib
import html
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.mngr.cli.output_helpers import write_stderr_line
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import parse_account
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.cli._common import resolve_accounts_url
from imbue.mngr_imbue_cloud.connector.auth_helper import force_refresh
from imbue.mngr_imbue_cloud.connector.auth_helper import get_active_token
from imbue.mngr_imbue_cloud.connector.client import CONNECTOR_TOO_OLD_REMEDY
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.connector.session_store import make_session_from_tokens
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.wire_types import AuthRawResponse

# The browser leg can legitimately take minutes, and the account is created
# partway through it. Kept below the desktop's subprocess kill deadline and
# flow-status TTL (imbue_cloud_cli._WEB_LOGIN_TIMEOUT_SECONDS,
# supertokens_routes._WEB_LOGIN_FLOW_TTL_SECONDS).
_LOGIN_LISTEN_TIMEOUT_SECONDS = 600.0
_LOGIN_CALLBACK_PATH = "/callback"


@click.group(name="auth")
def auth() -> None:
    """Sign in/out of Imbue Cloud and manage SuperTokens sessions."""


def _persist_auth_response(
    response: AuthRawResponse,
    expected_account: ImbueCloudAccount | None,
    store: ImbueCloudSessionStore,
) -> dict[str, Any]:
    """Convert a successful AuthRawResponse into a saved session and emit-json payload.

    When ``expected_account`` is None (the first-time browser-login case), the
    email returned by the auth backend is accepted as-is. When it is set
    (signin / signup with explicit ``--account``), we validate that the
    backend returned the same account and fail otherwise.

    Every OK response counts as signed in immediately -- email verification is
    non-blocking (it is required only for specific actions, enforced
    server-side), so there is no "pending session" state anymore.
    """
    if response.status != "OK":
        fail_with_json(
            response.message or response.status,
            error_class="AuthFailed",
            status=response.status,
            needs_email_verification=response.needs_email_verification,
        )
    user = response.user or {}
    tokens = response.tokens or {}
    user_id_raw = user.get("user_id")
    email_raw = user.get("email")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(user_id_raw, str) or not isinstance(email_raw, str) or not isinstance(access_token, str):
        fail_with_json("Auth response missing required fields", error_class="AuthFailed")

    account_from_response = ImbueCloudAccount(email_raw)
    if expected_account is not None and account_from_response != expected_account:
        fail_with_json(
            f"Auth backend returned account {account_from_response} but client requested {expected_account}",
            error_class="AuthMismatch",
        )

    if response.needs_email_verification:
        # Only an old connector still reports this (new ones pin it False);
        # under the non-blocking model the account is signed in regardless.
        logger.warning("Connector reported needs_email_verification; treating the account as signed in anyway")

    display_name_raw = user.get("display_name")
    display_name = display_name_raw if isinstance(display_name_raw, str) else None
    session = make_session_from_tokens(
        user_id=SuperTokensUserId(user_id_raw),
        email=account_from_response,
        display_name=display_name,
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
    )
    store.save(session)
    # Make the most-recently-touched account the active one. This is what
    # users expect when they swap between accounts: ``auth signin --account
    # bob`` then ``mngr create`` should default to bob without an extra
    # ``auth use`` step. Power users who prefer pinning still have
    # ``auth use --account <other>`` to override.
    store.set_active_account(account_from_response)
    return {
        "user_id": str(session.user_id),
        "email": str(session.email),
        "display_name": session.display_name,
    }


@auth.command(name="signin")
@click.option("--account", required=True, help="Account email")
@click.option("--password", default=None, help="Password (prompts if omitted)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def signin(account: str, password: str | None, connector_url: str | None) -> None:
    """Sign in with email + password and persist the session.

    The headless path (tests, SSH sessions, scripts). Interactive users
    normally use ``auth login``, which drives the hosted browser page.
    """
    parsed_account = parse_account(account)
    if password is None:
        password = getpass.getpass(prompt=f"Password for {parsed_account}: ")
    if not password:
        fail_with_json("Password cannot be empty", error_class="UsageError")
    client = make_connector_client(connector_url)
    store = make_session_store()
    response = client.auth_signin(str(parsed_account), password)
    payload = _persist_auth_response(response, parsed_account, store)
    emit_json(payload)


_MAX_PASSWORD_CONFIRM_ATTEMPTS = 3


def _prompt_password_with_confirmation(parsed_account: ImbueCloudAccount) -> str:
    """Read a password from the TTY twice, verify they match.

    Allows up to ``_MAX_PASSWORD_CONFIRM_ATTEMPTS`` retries on mismatch
    so a typo doesn't ship to the connector. ``--password`` on the CLI
    bypasses this entirely (CI / scripted use cases).
    """
    for attempt in range(_MAX_PASSWORD_CONFIRM_ATTEMPTS):
        first = getpass.getpass(prompt=f"Password for new account {parsed_account}: ")
        if not first:
            fail_with_json("Password cannot be empty", error_class="UsageError")
        confirm = getpass.getpass(prompt="Confirm password: ")
        if first == confirm:
            return first
        remaining = _MAX_PASSWORD_CONFIRM_ATTEMPTS - attempt - 1
        if remaining == 0:
            fail_with_json(
                "Passwords did not match after several attempts",
                error_class="UsageError",
            )
        click.echo(
            f"Passwords did not match. {remaining} attempt(s) remaining.",
            err=True,
        )
    # Unreachable -- the loop either returns or fails out -- but keeps the
    # type checker happy about the return type.
    raise AssertionError("unreachable")


@auth.command(name="signup")
@click.option("--account", required=True, help="Account email")
@click.option(
    "--password",
    default=None,
    help="Password. When omitted, the command prompts twice on the TTY and verifies the two entries match.",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def signup(account: str, password: str | None, connector_url: str | None) -> None:
    """Sign up with email + password (returns the new session).

    The headless path for tests on dev/CI tiers only: production and staging
    refuse account creation through this API (status ``SIGNUP_DISABLED``) --
    create the account with ``auth login`` instead, which drives the hosted
    browser page.
    """
    parsed_account = parse_account(account)
    if password is None:
        password = _prompt_password_with_confirmation(parsed_account)
    elif not password:
        fail_with_json("Password cannot be empty", error_class="UsageError")
    client = make_connector_client(connector_url)
    store = make_session_store()
    response = client.auth_signup(str(parsed_account), password)
    payload = _persist_auth_response(response, parsed_account, store)
    emit_json(payload)


@auth.command(name="signout")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option(
    "--all-devices",
    is_flag=True,
    default=False,
    help="Revoke EVERY session for this account (other devices and the browser), not just this machine's.",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def signout(account: str | None, all_devices: bool, connector_url: str | None) -> None:
    """Revoke this machine's SuperTokens session and remove the local tokens.

    Only the local device's session is revoked by default -- the account's
    browser session and other devices stay signed in (use ``--all-devices``
    to revoke everything).

    The local tokens are removed even when the connector cannot be reached;
    the emitted ``server_session_revoked`` field reports whether the
    server-side revocation actually happened. ``--all-devices`` instead fails
    outright when the revocation does not land (killing every other session
    is its whole point), keeping the local session so a retry can still
    revoke.
    """
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    session = store.load_by_account(parsed_account)
    if session is None:
        emit_json({"removed": False, "reason": "no session"})
        return
    client = make_connector_client(connector_url)
    server_revoked = _revoke_server_sessions(store, client, parsed_account, session, all_devices=all_devices)
    store.delete_by_account(parsed_account)
    if not server_revoked:
        write_stderr_line(
            "Warning: the connector could not be reached to revoke the server-side session; "
            "only the local tokens were removed."
        )
    emit_json(
        {
            "removed": True,
            "user_id": str(session.user_id),
            "email": str(session.email),
            "server_session_revoked": server_revoked,
        }
    )


def _revoke_server_sessions(
    store: ImbueCloudSessionStore,
    client: ImbueCloudConnectorClient,
    account: ImbueCloudAccount,
    session: AuthSession,
    *,
    all_devices: bool,
) -> bool:
    """Revoke the account's server-side session(s) with a fresh access token.

    The stored access token may have expired since the last authenticated
    call; the revoke endpoints answer 401 to an expired token and the client
    treats 401 as "already revoked", which would silently skip the revocation
    (worst for ``--all-devices``, whose whole point is killing every other
    session). Refreshing first makes the revoke real; when the refresh itself
    fails the session is already dead server-side, so falling back to the
    stored token keeps the 401-as-already-revoked treatment truthful.

    Returns whether the server-side revocation happened. The client already
    treats a 401 revoke answer as success (the session was dead), so an
    ``ImbueCloudAuthError`` here means the revocation did NOT land: the
    connector was unreachable or answered a server error. For
    ``--all-devices`` that failure propagates -- reporting "all sessions
    revoked" when none were is dangerous (e.g. after a device compromise) --
    while the default single-device sign-out returns ``False`` so the caller
    can still drop the local tokens (signing out must work offline) and
    report the failure.
    """
    try:
        access_token = get_active_token(store, client, account)
    except ImbueCloudAuthError:
        access_token = session.access_token
    try:
        if all_devices:
            client.auth_revoke_session(access_token)
        else:
            client.auth_revoke_current_session(access_token)
    except ImbueCloudAuthError:
        if all_devices:
            raise
        return False
    return True


@auth.command(name="list")
@handle_imbue_cloud_errors
def list_accounts() -> None:
    """Emit one JSON object per signed-in account.

    Each entry contains ``user_id``, ``email``, ``display_name``, and
    ``is_active`` (whether this account is the one ``auth use`` /
    ``auth signin`` last marked active). Used by minds to source account
    identity (account chips, the workspace<->account dropdown, the
    bootstrap reconciliation) without keeping its own on-disk copy.

    Accounts whose session file is missing or unreadable are skipped
    silently -- callers should treat the output as the authoritative
    list of "currently signed in".
    """
    store = make_session_store()
    active = store.get_active_account()
    accounts: list[dict[str, Any]] = []
    for email in store.list_accounts():
        session = store.load_by_account(email)
        if session is None:
            continue
        accounts.append(
            {
                "user_id": str(session.user_id),
                "email": str(session.email),
                "display_name": session.display_name,
                "is_active": active == email,
            }
        )
    emit_json(accounts)


@auth.command(name="status")
@click.option(
    "--account",
    default=None,
    help="Account email (defaults to the active account; pass to query a different signed-in account).",
)
@handle_imbue_cloud_errors
def status(account: str | None) -> None:
    """Print whether a session is on disk for an account.

    With no ``--account``, returns status for the active account (set via
    ``auth use``, or by the most recent signin). When no account can be
    resolved, lists known signed-in accounts so the user can pick one.
    """
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    session = store.load_by_account(parsed_account)
    active = store.get_active_account()
    if session is None:
        emit_json({"signed_in": False, "email": str(parsed_account), "is_active": active == parsed_account})
        return
    near_expiry = store.is_access_token_near_expiry(session)
    emit_json(
        {
            "signed_in": True,
            "user_id": str(session.user_id),
            "email": str(session.email),
            "display_name": session.display_name,
            "access_token_expires_at": session.access_token_expires_at,
            "near_expiry": near_expiry,
            "has_refresh_token": session.refresh_token is not None,
            "is_active": active == session.email,
        }
    )


@auth.command(name="use")
@click.option(
    "--account",
    required=True,
    help=(
        "Account email to mark as active. Must already be signed in (run `mngr "
        "imbue_cloud auth signin --account <email>` first)."
    ),
)
@handle_imbue_cloud_errors
def use(account: str) -> None:
    """Pin ``account`` as the active imbue_cloud account.

    The default ``[providers.imbue_cloud]`` provider instance and any
    ``mngr imbue_cloud ...`` sub-command that omits ``--account`` resolve
    to this account. Persists across mngr invocations until explicitly
    changed (or the account signs out).
    """
    parsed_account = parse_account(account)
    store = make_session_store()
    store.set_active_account(parsed_account)
    emit_json({"active_account": str(parsed_account)})


@auth.command(name="refresh")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def refresh(account: str | None, connector_url: str | None) -> None:
    """Force a token refresh now.

    Unconditionally calls the connector's refresh endpoint and rotates the
    persisted access + refresh tokens. Useful for verifying refresh works
    before tokens are near expiry. Authed CLI subcommands rotate
    transparently when the cached token is near expiry, so manual
    invocations of this command are normally unnecessary.
    """
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    client = make_connector_client(connector_url)
    previous = store.load_by_account(parsed_account)
    refreshed_session = force_refresh(store, client, parsed_account)
    emit_json(
        {
            "user_id": str(refreshed_session.user_id),
            "email": str(refreshed_session.email),
            "access_token_expires_at": refreshed_session.access_token_expires_at,
            "previous_access_token_expires_at": (previous.access_token_expires_at if previous is not None else None),
            "refreshed": True,
        }
    )


# ----------------------------------------------------------------------
# Browser-based login (the hosted accounts surface + loopback handoff)
# ----------------------------------------------------------------------


class _CallbackCaptureBox:
    """Thread-safe box that holds the loopback callback query params.

    The HTTP handler writes here once it receives a callback; the main thread
    polls the box to know when to stop the listener.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._params: dict[str, str] | None = None

    def set(self, params: dict[str, str]) -> None:
        with self._lock:
            self._params = dict(params)

    def get(self) -> dict[str, str] | None:
        with self._lock:
            return None if self._params is None else dict(self._params)


# Inline styles for the login success page: it is served from a localhost
# listener with no other assets, so everything must be self-contained.
_LOGIN_SUCCESS_PAGE_STYLE = (
    "html,body{height:100%;margin:0}"
    "body{display:flex;align-items:center;justify-content:center;text-align:center;"
    'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'
    "background:#faf8f2;color:#000}"
    "main{padding:2rem;max-width:26rem}"
    "h1{font-size:1.6rem;font-weight:600;margin:0 0 0.6rem}"
    "p{margin:0;font-size:1rem;line-height:1.25}"
    ".message{margin:1.75rem 0 1.25rem}"
    "a{color:inherit}"
    "@media (prefers-color-scheme:dark){body{background:#1a170a;color:#fff}}"
)

# The minds wordmark, inlined because the page ships no assets. Paths fill
# with currentColor so the mark follows the page's text color in both themes.
_MINDS_WORDMARK_SVG = (
    '<svg width="159" height="43" viewBox="0 0 159 43" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M0 42V13.08H4.68V16.98C5.7 13.86 8.04 12.12 10.86 12.12C13.5 12.12 15.78 13.74 16.68 17.4C17.94 14.22 20.16 12.12 23.7 12.12C28.02 12.12 30.36 15.6 30.36 22.14V42H25.68V22.74C25.68 18.66 24.84 16.2 22.02 16.2C18.66 16.2 17.52 19.86 17.52 23.7V42H12.84V23.1C12.84 18.84 11.88 16.2 9 16.2C5.76 16.2 4.68 19.92 4.68 23.94V42H0Z" fill="currentColor"/>'
    '<path d="M34.8366 42V37.74H48.6366V17.34H37.2966V13.08H53.7366V37.74H65.6166V42H34.8366ZM47.3766 7.98V1.08H53.9166V7.98H47.3766Z" fill="currentColor"/>'
    '<path d="M70.3331 42V13.08H75.4931V16.98C76.9931 14.46 80.4731 12.12 84.7931 12.12C91.7531 12.12 95.7731 16.62 95.7731 24.06V42H90.6131V24.72C90.6131 19.26 88.8131 16.2 83.8931 16.2C78.4931 16.2 75.4931 20.22 75.4931 24.84V42H70.3331Z" fill="currentColor"/>'
    '<path d="M114.59 42.9C107.03 42.9 101.21 37.38 101.21 27.54C101.21 18.78 106.49 12.12 114.65 12.12C119.51 12.12 122.69 14.76 123.95 16.98V0H129.11V42H123.95V37.98C122.39 40.68 118.91 42.9 114.59 42.9ZM115.43 38.88C120.65 38.88 124.31 34.44 124.31 27.48C124.31 20.58 120.71 16.2 115.43 16.2C110.27 16.2 106.61 20.76 106.61 27.54C106.61 34.32 110.21 38.88 115.43 38.88Z" fill="currentColor"/>'
    '<path d="M146.846 42.9C139.046 42.9 134.546 38.64 134.426 32.46H139.466C139.646 36.36 142.286 38.88 146.906 38.88C150.866 38.88 153.566 37.08 153.566 34.14C153.566 31.86 152.006 30.36 148.526 29.7L144.146 28.86C138.746 27.84 135.326 25.08 135.326 20.64C135.326 15.72 140.006 12.12 146.546 12.12C153.506 12.12 157.706 15.54 158.066 21.42H152.966C152.546 17.94 150.086 16.2 146.186 16.2C142.706 16.2 140.486 17.88 140.486 20.34C140.486 22.68 142.166 23.82 145.406 24.42L149.906 25.26C155.126 26.22 158.666 28.8 158.666 33.6C158.666 38.76 154.226 42.9 146.846 42.9Z" fill="currentColor"/>'
    "</svg>"
)


def _login_success_page(success_redirect_url: str | None) -> bytes:
    """Build the HTML the callback listener serves to the browser.

    With a redirect URL, the page offers a link to it -- the minds desktop
    app passes its minds:// deeplink so a click hands focus back to the app;
    since that flow is minds-driven (nothing else passes the option today),
    the page carries the minds wordmark. Deliberately a link rather than an
    automatic navigation: the click is a user gesture, so browsers show
    their open-external-app prompt at a moment the user chose instead of
    unprompted on page load.
    """
    if success_redirect_url is None:
        body_html = "<h1>You are signed in</h1><p>You can close this tab and return to your terminal.</p>"
    else:
        href = html.escape(success_redirect_url, quote=True)
        body_html = (
            _MINDS_WORDMARK_SVG
            + '<p class="message">You\'re in! Feel free to close this tab.</p>'
            + f'<p><a href="{href}">Open app</a></p>'
        )
    page = (
        "<!DOCTYPE html><html><head><title>Imbue Cloud sign-in</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{_LOGIN_SUCCESS_PAGE_STYLE}</style></head>"
        f"<body><main>{body_html}</main></body></html>"
    )
    return page.encode("utf-8")


def _make_callback_handler_class(
    box: _CallbackCaptureBox, success_redirect_url: str | None
) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a handler class closed over a specific capture box.

    Closing over the box lets the handler push state without us touching the
    HTTPServer instance's attributes (which would trip the no-getattr ratchet).
    """
    body = _login_success_page(success_redirect_url)

    class _LoginCallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence the default access log; we don't need it.
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            # Only the real /callback hit with query params is the callback. Browsers
            # routinely fire secondary GETs (favicon.ico, prefetches, service-worker pings)
            # at the same listener; those must not overwrite the captured params.
            if parsed.path == _LOGIN_CALLBACK_PATH and params:
                box.set(params)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _LoginCallbackHandler


def make_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def compute_pkce_challenge(code_verifier: str) -> str:
    """The S256 PKCE challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _ensure_connector_supports_browser_login(client: ImbueCloudConnectorClient) -> None:
    """Fail fast with an actionable error when the connector predates the hosted accounts pages.

    Without this probe, a login against a stale connector opens a 404 page in
    the browser and the CLI hangs until the listen timeout -- the failure has
    to be reported before anything opens.
    """
    if client.supports_browser_login():
        return
    fail_with_json(
        f"The connector at {client.base_url} is too old for browser sign-in "
        f"(it does not serve the hosted accounts pages). {CONNECTOR_TOO_OLD_REMEDY}",
        error_class="AuthFailed",
        status="CONNECTOR_TOO_OLD",
    )


def _bind_callback_listener(
    callback_port: int | None, handler_class: type[http.server.BaseHTTPRequestHandler]
) -> http.server.HTTPServer:
    """Bind the localhost login-callback listener, failing with the structured JSON body.

    Binds directly (port 0 = kernel-assigned) rather than probing for a free
    port with a separate socket and rebinding, which leaves a TOCTOU window
    (the pattern cli/conftest.py warns about). A bind failure (an occupied
    ``--callback-port``, a privileged port) is an OSError, and a
    ``--callback-port`` outside 0-65535 (click's ``type=int`` accepts any
    integer) is an OverflowError from ``socket.bind`` -- neither is an
    ImbueCloudError, so both would otherwise escape
    ``handle_imbue_cloud_errors`` as a raw traceback instead of the JSON
    error body embedders parse.
    """
    try:
        return http.server.HTTPServer(("127.0.0.1", callback_port or 0), handler_class)
    except (OSError, OverflowError) as exc:
        fail_with_json(
            f"Could not bind the login callback listener on 127.0.0.1:{callback_port or 0}: {exc}",
            error_class="LoginFailed",
        )


def _write_login_url_file(url_file: str, login_url: str) -> None:
    """Write the sign-in URL for the embedder, failing with the structured JSON body.

    ``click.Path(dir_okay=False)`` does not validate writability or parent
    existence, so a bad ``--url-file`` surfaces here as an OSError -- which
    must become the JSON error body, not a raw traceback.
    """
    try:
        Path(url_file).write_text(login_url + "\n")
    except OSError as exc:
        fail_with_json(f"Could not write the sign-in URL to {url_file}: {exc}", error_class="LoginFailed")


def build_login_url(login_base_url: str, callback_url: str, code_challenge: str, state: str) -> str:
    """The hosted login page URL that authorizes a device handoff back to ``callback_url``.

    ``login_base_url`` must be the tier's browser accounts origin when it has
    one (Google's OAuth redirect URI and the session cookie's Domain are bound
    to it); only tiers without a dedicated accounts origin serve the page on
    the connector host itself.
    """
    authorize_query = urllib.parse.urlencode(
        {"redirect_uri": callback_url, "code_challenge": code_challenge, "state": state}
    )
    next_path = f"/accounts/authorize?{authorize_query}"
    return f"{login_base_url.rstrip('/')}/login?" + urllib.parse.urlencode({"next": next_path})


@auth.command(name="login")
@click.option(
    "--account",
    default=None,
    help=(
        "Optional account email. When set, the browser login must come back with the same "
        "email or the call fails (useful when re-authing a known account). When omitted, "
        "whatever account signs in on the hosted page becomes this session's account."
    ),
)
@click.option(
    "--callback-port",
    default=None,
    type=int,
    help="Bind the local callback listener to a specific port (default: auto-pick free port).",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help=(
        "Print the sign-in URL instead of launching the browser. The URL only works in a "
        "browser on THIS machine (it redirects back to a localhost listener); on a headless "
        "machine use `auth signin` instead."
    ),
)
@click.option(
    "--success-redirect-url",
    default=None,
    help=(
        "URL the success page links to once the callback lands (e.g. a minds:// "
        "deeplink so a click returns the user to the desktop app). Default: no link; "
        "the page just says to close the tab."
    ),
)
@click.option(
    "--url-file",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Write the sign-in URL to this file once the callback listener is up. Lets an "
        "embedder (the minds desktop client) offer a copy-the-link fallback without "
        "parsing stderr."
    ),
)
@click.option("--connector-url", default=None, help="Override connector URL")
@click.option(
    "--accounts-url",
    default=None,
    help=(
        "Override the browser accounts-origin URL the login page is opened on "
        "(default: $MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL, else the connector URL). "
        "Tiers with a dedicated accounts domain (e.g. production) only complete Google "
        "sign-in and session cookies on that origin."
    ),
)
@handle_imbue_cloud_errors
def login(
    account: str | None,
    callback_port: int | None,
    no_browser: bool,
    success_redirect_url: str | None,
    url_file: str | None,
    connector_url: str | None,
    accounts_url: str | None,
) -> None:
    """Sign in via the hosted browser page (email/password, sign-up, or Google).

    Spins up a localhost callback listener, opens the hosted login page in
    the system browser (on the tier's accounts origin when one is configured,
    else on the connector host), and exchanges the one-time code the page
    hands back (PKCE-bound) for this machine's own session. The browser
    session established along the way stays in the browser; this device gets
    independent tokens.
    """
    parsed_account = parse_account(account) if account else None

    client = make_connector_client(connector_url)
    store = make_session_store()
    _ensure_connector_supports_browser_login(client)

    code_verifier = make_pkce_verifier()
    state = secrets.token_urlsafe(16)

    capture_box = _CallbackCaptureBox()
    handler_class = _make_callback_handler_class(capture_box, success_redirect_url)
    server = _bind_callback_listener(callback_port, handler_class)
    port = server.server_address[1]
    callback_url = f"http://127.0.0.1:{port}{_LOGIN_CALLBACK_PATH}"
    # Tiers without a dedicated accounts origin serve the page on the connector host.
    login_base_url = resolve_accounts_url(accounts_url) or str(client.base_url)
    login_url = build_login_url(login_base_url, callback_url, compute_pkce_challenge(code_verifier), state)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="imbue-cloud-login-cb")
    server_thread.start()

    deadline = time.monotonic() + _LOGIN_LISTEN_TIMEOUT_SECONDS
    captured: dict[str, str] | None = None
    try:
        if url_file is not None:
            # The listener is live, so the URL is usable the moment this appears.
            _write_login_url_file(url_file, login_url)

        if no_browser:
            click.echo(f"Open this URL in your browser to sign in:\n  {login_url}", err=True)
        else:
            click.echo(f"Opening browser to: {login_url}", err=True)
            try:
                webbrowser.open(login_url)
            except webbrowser.Error:
                click.echo(
                    "Failed to launch browser; visit the URL above manually.",
                    err=True,
                )

        while time.monotonic() < deadline:
            captured = capture_box.get()
            if captured:
                break
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()

    if not captured:
        fail_with_json("Timed out waiting for the browser sign-in", error_class="LoginTimeout")
    if captured.get("state") != state:
        fail_with_json("Login callback state mismatch; refusing the response", error_class="LoginStateMismatch")
    code = captured.get("code", "")
    if not code:
        fail_with_json("Login callback carried no code", error_class="LoginFailed")

    token_response = client.auth_device_token(code=code, code_verifier=code_verifier, redirect_uri=callback_url)
    payload = _persist_auth_response(token_response, parsed_account, store)
    emit_json(payload)


@auth.command(name="forgot-password")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def forgot_password(account: str | None, connector_url: str | None) -> None:
    """Send a password-reset email. The connector returns OK regardless to avoid enumeration."""
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    client = make_connector_client(connector_url)
    client.auth_forgot_password(str(parsed_account))
    emit_json({"sent": True, "email": str(parsed_account)})


@auth.command(name="resend-verification")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def resend_verification(account: str | None, connector_url: str | None) -> None:
    """(Re-)send the email verification message for the given account.

    Verification is non-blocking, but a few actions (visiting shares, the
    ally plan) require a verified email; this sends the link on demand.
    ``sent`` is False when the connector suppressed the send because one
    went out moments ago (its per-user cooldown).
    """
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    session = store.load_by_account(parsed_account)
    if session is None:
        fail_with_json(
            f"No session for {parsed_account}; sign in first.",
            error_class="NotSignedIn",
        )
    # `session` is now narrowed to AuthSession (fail_with_json is NoReturn).
    client = make_connector_client(connector_url)
    access_token = get_active_token(store, client, parsed_account)
    is_sent = client.auth_send_verification_email(access_token, str(session.email))
    emit_json({"sent": is_sent, "email": str(session.email)})


@auth.command(name="is-verified")
@click.option("--account", default=None, help="Account email (defaults to the active account)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def is_verified(account: str | None, connector_url: str | None) -> None:
    """Check whether the account's email is verified (a plain status query).

    Verification is non-blocking: an unverified account is fully signed in,
    and only specific actions (visiting shares, the ally plan) require the
    email to be verified. Safe to poll repeatedly.
    """
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    session = store.load_by_account(parsed_account)
    if session is None:
        fail_with_json(
            f"No session for {parsed_account}; sign in first.",
            error_class="NotSignedIn",
        )
    client = make_connector_client(connector_url)
    access_token = get_active_token(store, client, parsed_account)
    is_email_verified = client.auth_is_email_verified(access_token, str(session.email))
    emit_json(
        {
            "verified": is_email_verified,
            "user_id": str(session.user_id),
            "email": str(session.email),
            "display_name": session.display_name,
        }
    )
