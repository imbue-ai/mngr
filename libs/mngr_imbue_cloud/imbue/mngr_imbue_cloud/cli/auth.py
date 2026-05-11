"""`mngr imbue_cloud auth ...` subcommands."""

import getpass
import http.server
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import click

from imbue.mngr_imbue_cloud.auth_helper import force_refresh
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.cli._common import make_session_store
from imbue.mngr_imbue_cloud.cli._common import parse_account
from imbue.mngr_imbue_cloud.cli._common import resolve_account_or_active
from imbue.mngr_imbue_cloud.client import AuthRawResponse
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.session_store import make_session_from_tokens

_OAUTH_LISTEN_TIMEOUT_SECONDS = 300.0


@click.group(name="auth")
def auth() -> None:
    """Sign in/out of Imbue Cloud and manage SuperTokens sessions."""


def _persist_auth_response(
    response: AuthRawResponse,
    expected_account: ImbueCloudAccount | None,
    store: ImbueCloudSessionStore,
) -> dict[str, Any]:
    """Convert a successful AuthRawResponse into a saved session and emit-json payload.

    When ``expected_account`` is None (the OAuth-first-time-signin case), the
    email returned by the auth backend is accepted as-is. When it is set
    (signin / signup with explicit ``--account``), we validate that the
    backend returned the same account and fail otherwise.
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
        "needs_email_verification": response.needs_email_verification,
    }


@auth.command(name="signin")
@click.option("--account", required=True, help="Account email")
@click.option("--password", default=None, help="Password (prompts if omitted)")
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def signin(account: str, password: str | None, connector_url: str | None) -> None:
    """Sign in with email + password and persist the session."""
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
    """Sign up with email + password (returns the new session)."""
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
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def signout(account: str | None, connector_url: str | None) -> None:
    """Revoke the SuperTokens session and remove local tokens for this account."""
    store = make_session_store()
    parsed_account = resolve_account_or_active(store, account)
    session = store.load_by_account(parsed_account)
    if session is None:
        emit_json({"removed": False, "reason": "no session"})
        return
    client = make_connector_client(connector_url)
    try:
        client.auth_revoke_session(session.access_token)
    except ImbueCloudAuthError:
        # Already revoked or expired -- still drop the local token.
        pass
    store.delete_by_account(parsed_account)
    emit_json({"removed": True, "user_id": str(session.user_id), "email": str(session.email)})


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
# OAuth (browser-based) flow
# ----------------------------------------------------------------------


class _OAuthCaptureBox:
    """Thread-safe box that holds the OAuth callback query params.

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


def _make_callback_handler_class(box: _OAuthCaptureBox) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a handler class closed over a specific capture box.

    Closing over the box lets the handler push state without us touching the
    HTTPServer instance's attributes (which would trip the no-getattr ratchet).
    """

    class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence the default access log; we don't need it.
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            box.set(params)
            body = (
                b"<html><head><title>Imbue Cloud sign-in</title></head>"
                b"<body><h1>You are signed in</h1>"
                b"<p>You can close this tab and return to your terminal.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _OAuthCallbackHandler


def _free_localhost_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@auth.command(name="oauth")
@click.argument("provider_id", type=click.Choice(["google", "github"], case_sensitive=False))
@click.option(
    "--account",
    default=None,
    help=(
        "Optional account email. When set, the OAuth response must come back with the same "
        "email or the call fails (useful when re-authing a known account). When omitted, "
        "whatever email the OAuth provider returns becomes this session's account email -- "
        "this is the right shape for first-time signin via Google or GitHub."
    ),
)
@click.option(
    "--callback-port",
    default=None,
    type=int,
    help="Bind the local OAuth callback listener to a specific port (default: auto-pick free port).",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Print the authorize URL instead of launching the browser; useful when running headless.",
)
@click.option("--connector-url", default=None, help="Override connector URL")
@handle_imbue_cloud_errors
def oauth(
    provider_id: str,
    account: str | None,
    callback_port: int | None,
    no_browser: bool,
    connector_url: str | None,
) -> None:
    """OAuth-based sign-in. Spins up a localhost callback listener.

    The callback URL is registered with the connector when it returns the
    authorize URL. Once the OAuth provider redirects back, the listener
    captures the query params, exchanges them at /auth/oauth/callback, and
    persists the resulting session.
    """
    parsed_account = parse_account(account) if account else None
    port = callback_port if callback_port is not None else _free_localhost_port()
    callback_url = f"http://127.0.0.1:{port}/oauth/callback"

    client = make_connector_client(connector_url)
    store = make_session_store()

    authorize_response = client.auth_oauth_authorize(provider_id.lower(), callback_url)
    authorize_url = authorize_response.get("url") or authorize_response.get("authorize_url")
    if not isinstance(authorize_url, str) or not authorize_url:
        fail_with_json("Connector did not return an authorize URL", error_class="OAuthFailed")

    capture_box = _OAuthCaptureBox()
    handler_class = _make_callback_handler_class(capture_box)
    server = http.server.HTTPServer(("127.0.0.1", port), handler_class)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="imbue-cloud-oauth-cb")
    server_thread.start()

    if no_browser:
        click.echo(f"Open this URL in your browser to sign in:\n  {authorize_url}", err=True)
    else:
        click.echo(f"Opening browser to: {authorize_url}", err=True)
        try:
            webbrowser.open(authorize_url)
        except webbrowser.Error:
            click.echo(
                "Failed to launch browser; visit the URL above manually.",
                err=True,
            )

    deadline = time.monotonic() + _OAUTH_LISTEN_TIMEOUT_SECONDS
    captured: dict[str, str] | None = None
    try:
        while time.monotonic() < deadline:
            captured = capture_box.get()
            if captured:
                break
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()

    if not captured:
        fail_with_json("Timed out waiting for OAuth callback", error_class="OAuthTimeout")

    callback_response = client.auth_oauth_callback(
        provider_id=provider_id.lower(),
        callback_url=callback_url,
        query_params=captured,
    )
    payload = _persist_auth_response(callback_response, parsed_account, store)
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
    """Re-send the email verification message for the given account."""
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
    client.auth_send_verification_email(str(session.user_id), str(session.email))
    emit_json({"sent": True, "email": str(session.email)})
