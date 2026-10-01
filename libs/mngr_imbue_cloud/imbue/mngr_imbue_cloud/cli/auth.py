"""`mngr imbue_cloud auth ...` subcommands."""

import base64
import getpass
import hashlib
import html
import http.server
import secrets
import signal
import sys
import threading
import urllib.parse
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
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
# partway through it. Embedders that can wait longer pass --listen-timeout.
_DEFAULT_LOGIN_LISTEN_TIMEOUT_SECONDS: Final[float] = 600.0
_LOGIN_CALLBACK_PATH = "/callback"

# The connector's listener lease lasts about 30 seconds; renewing this often
# survives a missed renewal or two.
_LISTENER_LEASE_RENEW_INTERVAL_SECONDS: Final[float] = 10.0

# How long the callback page holds its response for the code exchange to
# finish, so the browser reports the real outcome. Past it the page tells the
# user to return to the app, which shows the result.
_LOGIN_OUTCOME_WAIT_SECONDS: Final[float] = 30.0


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
    profile_picture_url_raw = user.get("profile_picture_url")
    profile_picture_url = profile_picture_url_raw if isinstance(profile_picture_url_raw, str) else None
    session = make_session_from_tokens(
        user_id=SuperTokensUserId(user_id_raw),
        email=account_from_response,
        display_name=display_name,
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        profile_picture_url=profile_picture_url,
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
        "profile_picture_url": session.profile_picture_url,
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

    Each entry contains ``user_id``, ``email``, ``display_name``,
    ``profile_picture_url``, and ``is_active`` (whether this account is the one ``auth use`` /
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
                "profile_picture_url": session.profile_picture_url,
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
            "profile_picture_url": session.profile_picture_url,
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


# Browser-based login (the hosted accounts surface + loopback handoff)


class _LoginPageOutcome(UpperCaseStrEnum):
    """What the loopback callback page tells the browser about the sign-in."""

    SIGNED_IN = auto()
    FAILED = auto()
    PENDING = auto()


class _CallbackCaptureBox:
    """Thread-safe handoff between the loopback callback handler and the login command.

    The handler records the callback's query params, then waits for the
    command to publish how the sign-in ended, so the page it serves reports
    the real outcome instead of claiming success before the code exchange.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._params: dict[str, str] | None = None
        self._outcome: _LoginPageOutcome | None = None
        self._callback_received = threading.Event()
        self._outcome_published = threading.Event()

    def set(self, params: dict[str, str]) -> None:
        with self._lock:
            self._params = dict(params)
        self._callback_received.set()

    def get(self) -> dict[str, str] | None:
        with self._lock:
            return None if self._params is None else dict(self._params)

    def wait_for_callback(self, timeout_seconds: float) -> dict[str, str] | None:
        self._callback_received.wait(timeout_seconds)
        return self.get()

    def publish_outcome(self, outcome: _LoginPageOutcome) -> None:
        """Record how the sign-in ended; only the first published outcome counts."""
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
        self._outcome_published.set()

    def wait_for_outcome(self, timeout_seconds: float) -> _LoginPageOutcome:
        self._outcome_published.wait(timeout_seconds)
        with self._lock:
            return self._outcome or _LoginPageOutcome.PENDING


# Inline styles for the login result page: it is served from a localhost
# listener with no other assets, so everything must be self-contained.
_LOGIN_RESULT_PAGE_STYLE = (
    "html,body{height:100%;margin:0}"
    "body{display:flex;align-items:center;justify-content:center;text-align:center;"
    'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'
    "background:#faf8f2;color:#000}"
    "main{padding:2rem;max-width:26rem}"
    "h1{font-size:1.6rem;font-weight:600;margin:0 0 0.6rem}"
    "h1.verify{font-size:2.6rem;line-height:1.1;margin:1.5rem 0 1rem}"
    "p{margin:0;font-size:1rem;line-height:1.25}"
    ".message{margin:1.75rem 0 1.25rem}"
    ".verify-detail{margin:0 0 0.5rem}"
    ".verify-detail+.verify-detail{margin-bottom:1.5rem}"
    "a{color:inherit}"
    "@media (prefers-color-scheme:dark){body{background:#1a170a;color:#fff}}"
)

# The Imbue Studio wordmark, inlined because the page ships no assets: the
# drawing apps/minds ships as studio-wordmark.svg, with its paths filling with
# currentColor so the mark follows the page's text color in both themes.
_STUDIO_WORDMARK_SVG = (
    '<svg width="150" height="42" viewBox="0 0 642.882 180" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M37.5396 178.499C53.7741 180.961 73.4979 177.24 87.7037 169.172C99.9058 162.243 103.706 153.536 103.706 143.4C103.706 122.322 78.5132 106.047 79.9047 98.0687C82.3914 83.8115 102.437 93.2481 107.774 81.9499C113.523 69.7781 102.88 61.9144 96.9274 56.6419C89.5685 50.124 74.1458 44.2761 62.1907 44.2761C40.4982 42.1672 22.212 53.014 15.4296 72.3087C9.26482 89.8464 17.9655 100.381 23.8353 109.969C27.0032 115.144 29.3465 119.604 28.0836 123.829C24.4806 135.88 6.08972 127.745 1.38535 143.4C-2.60825 156.689 -0.0474759 172.799 37.5396 178.499Z" fill="currentColor"/>'
    '<path d="M170.226 87.3729C164.384 107.258 172.486 116.447 174.896 125.335C180.028 144.261 175.968 156.066 172.486 162.694C166.233 174.595 152.308 178.466 140.177 177.54C128.858 176.675 109.969 168.569 109.969 139.495C109.969 122.473 116.296 121.719 121.719 101.232C124.191 91.8921 118.556 83.4561 111.626 77.8824C107.466 74.5365 100.93 57.5456 114.488 48.9589C126.272 41.496 133.819 43.5935 143.19 31.1831C148.082 24.7054 146.902 -0.0241729 164.083 2.01978e-05C174.76 0.0150555 178.598 6.62828 179.717 10.545C181.869 18.0771 179.335 26.8495 182.579 35.4011C185.893 44.1384 201.532 42.4143 197.492 63.4207C195.117 75.7734 172.768 78.7197 170.226 87.3729Z" fill="currentColor"/>'
    '<path d="M300.696 54.0364C306.597 62.5791 308.433 74.4957 310.217 86.1677C313.833 109.819 319.248 135.766 306.766 153.422C302.332 159.694 297.302 166.171 290.574 170.362C280.405 176.695 265.995 178.887 246.479 178.887C226.963 178.887 214.637 175.512 205.328 169.307C196.629 163.51 192.824 159.28 189.103 152.797C176.468 130.814 191.979 68.0402 198.881 55.6881C202.809 48.6576 209.184 46.1022 217.056 46.1022C229.488 46.1022 236.287 52.3683 237.651 65.3465C238.886 77.1082 239.942 97.1672 241.763 111.92C242.579 118.498 244.641 122.164 249.781 122.164C254.323 122.164 257.217 119.017 258.391 112.093C260.603 99.0419 263.844 79.4405 266.867 64.1131C269.426 51.1403 274.298 46.0967 282.802 46.0967C291.305 46.0967 296.681 48.2223 300.696 54.0364Z" fill="currentColor"/>'
    '<path fill-rule="evenodd" clip-rule="evenodd" d="M391.613 46.9321C400.757 40.794 402.61 21.4743 409.388 11.2306C416.167 0.98691 423.7 -0.711249 432.136 1.58948C444.456 4.94956 447.338 32.933 448.104 44.2214C448.706 53.1093 449.761 78.568 449.008 89.5649C448.254 100.562 445.953 129.471 433.605 151.202C429.839 157.83 423.964 163.404 416.114 168.792C403.717 177.302 390.486 181.151 367.991 179.7C346.452 178.312 332.372 170.014 324.584 158.436C312.191 140.016 310.709 113.673 316.894 92.0264C319.907 81.4814 329.415 68.0754 339.854 60.6415C356.639 48.6895 380.616 54.3136 391.613 46.9321ZM382.267 122.771C397.47 120.528 399.029 93.5563 378.657 97.6848C363.795 100.693 363.728 125.516 382.267 122.771Z" fill="currentColor"/>'
    '<path d="M511.034 71.0351C512.728 74.7344 513.998 82.3856 514.208 86.469C515.48 111.309 513.839 133.47 504.564 154.409C497.184 172.335 487.853 178.466 475.723 177.54C464.403 176.675 446.781 164.422 444.742 135.648C443.239 114.445 445.2 91.7582 457.928 72.8989C461.442 67.6983 464.548 64.1386 468.7 60.3891C472.443 57.0077 475.617 54.0003 475.494 51.6179C475.371 49.1068 474.734 47.4665 469.817 43.025C466.286 39.8388 461.632 33.5395 462.704 25.7558C464.627 11.8124 476.723 -0.0223195 493.904 3.16079e-05C504.581 0.016909 511.672 8.31419 512.906 17.3139C513.968 25.1254 511.409 33.8574 504.564 39.4931C499.866 43.3598 497.402 46.2668 497.184 49.2185C496.91 52.8787 499.938 56.6227 504.134 60.9748C506.258 63.1843 509.341 67.3357 511.034 71.0351Z" fill="currentColor"/>'
    '<path fill-rule="evenodd" clip-rule="evenodd" d="M592.843 44.1386C607.109 44.6409 618.984 51.7939 626.057 58.9583C631.549 64.5212 636.894 75.0125 640.517 88.5199C644.257 102.469 644.855 128.491 632.507 150.222C628.741 156.85 622.866 162.424 615.017 167.812C602.619 176.322 589.388 180.171 566.894 178.72C545.354 177.331 531.275 169.034 523.486 157.456C511.093 139.036 510.803 112.774 516.842 91.0864C522.03 72.4594 535.847 61.0205 543.734 55.738C559.628 45.0922 571.452 43.3853 592.843 44.1386ZM581.169 121.791C596.371 119.544 597.931 92.5788 577.559 96.7049C562.698 99.7151 562.629 124.532 581.169 121.791Z" fill="currentColor"/>'
    "</svg>"
)


_TERMINAL_PAGE_BODY_BY_OUTCOME: Final[dict[_LoginPageOutcome, str]] = {
    _LoginPageOutcome.SIGNED_IN: "<h1>You are signed in</h1><p>You can close this tab and return to your terminal.</p>",
    _LoginPageOutcome.FAILED: "<h1>Sign-in did not finish</h1><p>Return to your terminal for details.</p>",
    _LoginPageOutcome.PENDING: "<h1>Almost done</h1><p>Return to your terminal to finish signing in.</p>",
}

_APP_PAGE_MESSAGE_BY_OUTCOME: Final[dict[_LoginPageOutcome, str]] = {
    _LoginPageOutcome.SIGNED_IN: "You're in! Feel free to close this tab.",
    _LoginPageOutcome.FAILED: "Sign-in didn't finish. Go back to the app and click Try again.",
    _LoginPageOutcome.PENDING: "Almost done. Go back to the app to finish signing in.",
}


def _verification_reminder_html(unverified_email: str) -> str:
    """The page body for an account whose email is still unverified.

    A password sign-up counts as signed in right away, but the actions that
    matter (creating a remote workspace, opening a shared one) require the
    link in the verification email, so this is the one moment to say so
    loudly. The heading is deliberately oversized.
    """
    email_html = html.escape(unverified_email)
    return (
        '<h1 class="verify">Click the email verification link</h1>'
        f'<p class="verify-detail">You must verify your address: {email_html}</p>'
        '<p class="verify-detail">Check your spam folder</p>'
    )


def _login_result_page(
    success_redirect_url: str | None,
    outcome: _LoginPageOutcome,
    unverified_email: str | None,
) -> bytes:
    """Build the HTML the callback listener serves to the browser for a sign-in outcome.

    With a redirect URL, the page offers a link to it -- the minds desktop
    app passes its imbue-studio:// deeplink so a click hands focus back to the app;
    since that flow is minds-driven (nothing else passes the option today),
    the page carries the Imbue Studio wordmark. Deliberately a link rather than an
    automatic navigation: the click is a user gesture, so browsers show
    their open-external-app prompt at a moment the user chose instead of
    unprompted on page load.

    ``unverified_email`` is the signed-in address when the connector reported
    it as not yet verified; a completed sign-in then leads with the
    verification reminder instead of the plain welcome. A failed or pending
    sign-in shows only its own message.
    """
    if success_redirect_url is None:
        if outcome != _LoginPageOutcome.SIGNED_IN or unverified_email is None:
            body_html = _TERMINAL_PAGE_BODY_BY_OUTCOME[outcome]
        else:
            body_html = _verification_reminder_html(unverified_email) + "<p>Then return to your terminal.</p>"
    else:
        href = html.escape(success_redirect_url, quote=True)
        if outcome != _LoginPageOutcome.SIGNED_IN or unverified_email is None:
            welcome_html = f'<p class="message">{html.escape(_APP_PAGE_MESSAGE_BY_OUTCOME[outcome])}</p>'
        else:
            welcome_html = _verification_reminder_html(unverified_email)
        body_html = _STUDIO_WORDMARK_SVG + welcome_html + f'<p><a href="{href}">Open app</a></p>'
    page = (
        "<!DOCTYPE html><html><head><title>Imbue Cloud sign-in</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{_LOGIN_RESULT_PAGE_STYLE}</style></head>"
        f"<body><main>{body_html}</main></body></html>"
    )
    return page.encode("utf-8")


def _unverified_email_from_callback(params: dict[str, str]) -> str | None:
    """The signed-in address the connector flagged as unverified, or None.

    The connector appends ``email`` and ``verified`` to the loopback redirect
    beside ``code`` and ``state``; a connector that predates them sends
    neither, which reads as nothing to remind about.
    """
    email = params.get("email", "")
    if params.get("verified") == "0" and email:
        return email
    return None


def _make_callback_handler_class(
    box: _CallbackCaptureBox,
    success_redirect_url: str | None,
    outcome_wait_seconds: float,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a handler class closed over a specific capture box.

    Closing over the box lets the handler push state without us touching the
    HTTPServer instance's attributes (which would trip the no-getattr ratchet).
    """

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
                outcome = box.wait_for_outcome(outcome_wait_seconds)
            else:
                outcome = box.wait_for_outcome(0.0)
            body = _login_result_page(success_redirect_url, outcome, _unverified_email_from_callback(params))
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


@contextmanager
def _serve_login_callback(server: http.server.HTTPServer, capture_box: _CallbackCaptureBox) -> Iterator[None]:
    """Serve the loopback callback listener for the duration of the block.

    However the block ends, a callback page still waiting for the outcome is
    released with a failure (a success is published inside the block) before
    the listener shuts down.
    """
    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="imbue-cloud-login-cb")
    server_thread.start()
    try:
        yield
    finally:
        capture_box.publish_outcome(_LoginPageOutcome.FAILED)
        server.shutdown()
        server.server_close()


def _renew_listener_lease_best_effort(client: ImbueCloudConnectorClient, code_challenge: str) -> None:
    try:
        client.renew_device_login_attempt(code_challenge)
    except ImbueCloudAuthError as exc:
        logger.debug("Could not renew the sign-in listener lease: {}", exc)


def _release_listener_lease_best_effort(client: ImbueCloudConnectorClient, code_challenge: str) -> None:
    try:
        client.release_device_login_attempt(code_challenge)
    except ImbueCloudAuthError as exc:
        logger.debug("Could not release the sign-in listener lease: {}", exc)


def _renew_listener_lease_until_stopped(
    client: ImbueCloudConnectorClient, code_challenge: str, stop_event: threading.Event
) -> None:
    while not stop_event.wait(_LISTENER_LEASE_RENEW_INTERVAL_SECONDS):
        _renew_listener_lease_best_effort(client, code_challenge)


@contextmanager
def _exit_cleanly_on_sigterm() -> Iterator[None]:
    """Turn SIGTERM into a normal exit for the block, so its cleanup runs.

    Only the main thread can install a signal handler; elsewhere (an embedder
    running the command on a worker thread) the default handling stays.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous_handler = signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


@contextmanager
def _hold_listener_lease(client: ImbueCloudConnectorClient, code_challenge: str) -> Iterator[None]:
    """Keep the connector's lease on this sign-in's listener alive for the block, then release it.

    Before handing the browser to the loopback port, the connector checks
    this lease: once it has lapsed or been released, the browser gets a
    "reopen the app" page instead of a connection error. The first renewal
    happens before the block starts, so the lease exists before the browser
    opens. Lease calls are best effort; a failure only means the connector
    redirects as it always has.
    """
    _renew_listener_lease_best_effort(client, code_challenge)
    stop_event = threading.Event()
    renew_thread = threading.Thread(
        target=_renew_listener_lease_until_stopped,
        args=(client, code_challenge, stop_event),
        daemon=True,
        name="imbue-cloud-login-lease",
    )
    renew_thread.start()
    try:
        with _exit_cleanly_on_sigterm():
            yield
    finally:
        stop_event.set()
        _release_listener_lease_best_effort(client, code_challenge)


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
        "URL the success page links to once the callback lands (e.g. an imbue-studio:// "
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
        "embedder (the Imbue Studio desktop client) offer a copy-the-link fallback without "
        "parsing stderr."
    ),
)
@click.option(
    "--listen-timeout",
    default=_DEFAULT_LOGIN_LISTEN_TIMEOUT_SECONDS,
    type=click.FloatRange(min=1.0),
    show_default=True,
    help=(
        "Seconds to keep waiting for the browser to finish signing in. A browser that finishes "
        "after this lands on a closed local port, so embedders that stay open (the Imbue Studio desktop "
        "app) pass a long window."
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
    listen_timeout: float,
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
    code_challenge = compute_pkce_challenge(code_verifier)
    state = secrets.token_urlsafe(16)

    capture_box = _CallbackCaptureBox()
    handler_class = _make_callback_handler_class(capture_box, success_redirect_url, _LOGIN_OUTCOME_WAIT_SECONDS)
    server = _bind_callback_listener(callback_port, handler_class)
    port = server.server_address[1]
    callback_url = f"http://127.0.0.1:{port}{_LOGIN_CALLBACK_PATH}"
    # Tiers without a dedicated accounts origin serve the page on the connector host.
    login_base_url = resolve_accounts_url(accounts_url) or str(client.base_url)
    login_url = build_login_url(login_base_url, callback_url, code_challenge, state)

    # The browser tab is still waiting on the callback page while the code is
    # exchanged, so the listener stays up until the outcome is known. The
    # listener sits inside the lease so the page gets its outcome before the
    # lease-release round trip.
    with _hold_listener_lease(client, code_challenge), _serve_login_callback(server, capture_box):
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

        captured = capture_box.wait_for_callback(listen_timeout)
        if not captured:
            fail_with_json("Timed out waiting for the browser sign-in", error_class="LoginTimeout")
        if captured.get("state") != state:
            fail_with_json("Login callback state mismatch; refusing the response", error_class="LoginStateMismatch")
        code = captured.get("code", "")
        if not code:
            fail_with_json("Login callback carried no code", error_class="LoginCallbackMissingCode")

        token_response = client.auth_device_token(code=code, code_verifier=code_verifier, redirect_uri=callback_url)
        payload = _persist_auth_response(token_response, parsed_account, store)
        capture_box.publish_outcome(_LoginPageOutcome.SIGNED_IN)
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
