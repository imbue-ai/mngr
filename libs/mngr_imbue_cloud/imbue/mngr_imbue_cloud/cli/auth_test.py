"""Tests for ``mngr imbue_cloud auth`` helpers.

Covers the browser-login localhost callback listener's handler. The handler
must:
- Capture query params from a real ``GET /callback?...`` hit.
- NOT overwrite a previously-captured callback when secondary browser GETs
  (favicon, prefetches, service-worker pings) arrive at the same listener
  with no query params. Before the fix, those secondary GETs erased the
  captured params and the CLI then hung until the 300s login timeout.

The ``running_callback_server`` fixture lives in ``cli/conftest.py``.

Also covers ``_persist_auth_response`` (every OK response counts as signed in
immediately -- verification is non-blocking), the PKCE / login-URL helpers
the browser flow is built from, and the page the browser lands on, which must
report the sign-in's real outcome.
"""

import base64
import hashlib
import json
import signal
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from click.testing import Result
from pydantic import AnyUrl

from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.cli._common import resolve_accounts_url
from imbue.mngr_imbue_cloud.cli.auth import _CallbackCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _LoginPageOutcome
from imbue.mngr_imbue_cloud.cli.auth import _bind_callback_listener
from imbue.mngr_imbue_cloud.cli.auth import _ensure_connector_supports_browser_login
from imbue.mngr_imbue_cloud.cli.auth import _hold_listener_lease
from imbue.mngr_imbue_cloud.cli.auth import _login_result_page
from imbue.mngr_imbue_cloud.cli.auth import _make_callback_handler_class
from imbue.mngr_imbue_cloud.cli.auth import _persist_auth_response
from imbue.mngr_imbue_cloud.cli.auth import _revoke_server_sessions
from imbue.mngr_imbue_cloud.cli.auth import _write_login_url_file
from imbue.mngr_imbue_cloud.cli.auth import auth
from imbue.mngr_imbue_cloud.cli.auth import build_login_url
from imbue.mngr_imbue_cloud.cli.auth import compute_pkce_challenge
from imbue.mngr_imbue_cloud.cli.auth import make_pkce_verifier
from imbue.mngr_imbue_cloud.config import ACCOUNTS_URL_ENV_VAR
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.connector.session_store import make_session_from_tokens
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.wire_types import AuthRawResponse


def _get(port: int, path: str) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5.0) as resp:
        return resp.status


def _get_page_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60.0) as resp:
        return resp.read().decode("utf-8")


def test_callback_handler_captures_login_query_params(
    running_callback_server: tuple[_CallbackCaptureBox, int],
) -> None:
    box, port = running_callback_server
    status = _get(port, "/callback?code=abc123&state=xyz")
    assert status == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_followup_favicon_get(
    running_callback_server: tuple[_CallbackCaptureBox, int],
) -> None:
    """Browsers fire a secondary GET /favicon.ico after the callback page renders.

    Before the fix this overwrote the captured params with ``{}``, causing the
    CLI's polling loop to never observe a truthy box and hang until timeout.
    """
    box, port = running_callback_server
    assert _get(port, "/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/favicon.ico") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_paramless_root_get(
    running_callback_server: tuple[_CallbackCaptureBox, int],
) -> None:
    """A bare GET / (e.g. from a manual probe or prefetch) must not clobber the box."""
    box, port = running_callback_server
    assert _get(port, "/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_query_params_on_wrong_path(
    running_callback_server: tuple[_CallbackCaptureBox, int],
) -> None:
    """Even if some other path carries query params, only /callback should be captured."""
    box, port = running_callback_server
    assert _get(port, "/some-other-path?code=should_be_ignored") == 200
    assert box.get() is None


def _get_body(port: int, path: str) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5.0) as resp:
        return resp.read().decode("utf-8")


def test_callback_handler_serves_the_verification_reminder_for_an_unverified_email(
    running_callback_server: tuple[_CallbackCaptureBox, int],
) -> None:
    """A callback flagged verified=0 renders the reminder; the plain page otherwise."""
    box, port = running_callback_server
    box.publish_outcome(_LoginPageOutcome.SIGNED_IN)
    reminder = _get_body(port, "/callback?code=abc123&state=xyz&email=alice%40example.com&verified=0")
    assert "Click the email verification link" in reminder
    assert "You must verify your address: alice@example.com" in reminder
    assert "Check your spam folder" in reminder

    plain = _get_body(port, "/callback?code=abc123&state=xyz&email=alice%40example.com&verified=1")
    assert "Click the email verification link" not in plain
    assert "You are signed in" in plain


def test_success_page_without_redirect_says_return_to_terminal() -> None:
    page = _login_result_page(None, _LoginPageOutcome.SIGNED_IN, None).decode("utf-8")
    assert "You are signed in" in page
    assert "return to your terminal" in page
    assert "<script>" not in page


def test_success_page_with_redirect_links_to_url_without_auto_navigation() -> None:
    # Deliberately a plain link, not an automatic navigation: the click is the
    # user gesture that triggers the browser's open-external-app prompt. The
    # app-driven variant carries the minds wordmark and copy.
    page = _login_result_page("minds://", _LoginPageOutcome.SIGNED_IN, None).decode("utf-8")
    assert '<a href="minds://">Open app</a>' in page
    assert "<svg" in page and 'fill="currentColor"' in page
    assert "You&#x27;re in! Feel free to close this tab." in page
    assert "<script>" not in page


@pytest.mark.parametrize(
    ("success_redirect_url", "outcome", "expected_text", "unexpected_text"),
    [
        ("minds://", _LoginPageOutcome.FAILED, "Sign-in didn&#x27;t finish. Go back to the app", "You&#x27;re in"),
        ("minds://", _LoginPageOutcome.PENDING, "Almost done. Go back to the app", "You&#x27;re in"),
        (None, _LoginPageOutcome.FAILED, "Sign-in did not finish", "You are signed in"),
        (None, _LoginPageOutcome.PENDING, "Almost done", "You are signed in"),
    ],
)
def test_result_page_never_claims_success_for_an_unfinished_sign_in(
    success_redirect_url: str | None,
    outcome: _LoginPageOutcome,
    expected_text: str,
    unexpected_text: str,
) -> None:
    # An unverified address must not turn an unfinished sign-in into the verification reminder either.
    page = _login_result_page(success_redirect_url, outcome, "alice@example.com").decode("utf-8")
    assert expected_text in page
    assert unexpected_text not in page
    assert "Click the email verification link" not in page


def test_success_page_with_unverified_email_leads_with_the_verification_reminder() -> None:
    page = _login_result_page("minds://", _LoginPageOutcome.SIGNED_IN, "alice@example.com").decode("utf-8")
    assert '<h1 class="verify">Click the email verification link</h1>' in page
    assert "You must verify your address: alice@example.com" in page
    assert "Check your spam folder" in page
    assert "Feel free to close this tab." not in page
    # The app link stays: the reminder replaces the welcome, not the way back.
    assert '<a href="minds://">Open app</a>' in page


def test_success_page_escapes_the_unverified_email() -> None:
    page = _login_result_page(None, _LoginPageOutcome.SIGNED_IN, "<b>bold</b>@example.com").decode("utf-8")
    assert "<b>" not in page
    assert "&lt;b&gt;bold&lt;/b&gt;@example.com" in page


def test_success_page_escapes_redirect_url_markup() -> None:
    """A crafted URL must not be able to inject markup into the page: the
    href is attribute-escaped."""
    page = _login_result_page('minds://x?a=<b>&q="hi"', _LoginPageOutcome.SIGNED_IN, None).decode("utf-8")
    assert "<b>" not in page
    assert 'href="minds://x?a=&lt;b&gt;&amp;q=&quot;hi&quot;"' in page


def test_pkce_challenge_is_base64url_sha256_of_the_verifier() -> None:
    verifier = make_pkce_verifier()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert compute_pkce_challenge(verifier) == expected
    # Fresh verifiers must be unique and URL-safe.
    assert verifier != make_pkce_verifier()
    assert urllib.parse.quote(verifier, safe="-_") == verifier


def test_build_login_url_carries_the_authorize_handoff_as_next() -> None:
    url = build_login_url(
        "https://connector.example.com/",
        "http://127.0.0.1:8123/callback",
        "challenge-abc",
        "state-xyz",
    )
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "connector.example.com"
    assert parsed.path == "/login"
    next_value = urllib.parse.parse_qs(parsed.query)["next"][0]
    next_parsed = urllib.parse.urlsplit(next_value)
    assert next_parsed.path == "/accounts/authorize"
    next_query = urllib.parse.parse_qs(next_parsed.query)
    assert next_query["redirect_uri"] == ["http://127.0.0.1:8123/callback"]
    assert next_query["code_challenge"] == ["challenge-abc"]
    assert next_query["state"] == ["state-xyz"]


def test_resolve_accounts_url_prefers_flag_then_env_then_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser accounts origin resolves flag > env > None (None = no dedicated
    accounts origin, so ``login`` falls back to opening the page on the connector host)."""
    monkeypatch.delenv(ACCOUNTS_URL_ENV_VAR, raising=False)
    assert resolve_accounts_url(None) is None

    monkeypatch.setenv(ACCOUNTS_URL_ENV_VAR, "https://accounts-env.example.com/")
    assert resolve_accounts_url(None) == "https://accounts-env.example.com"
    assert resolve_accounts_url("https://accounts-flag.example.com/") == "https://accounts-flag.example.com"


def _make_auth_response(needs_email_verification: bool) -> AuthRawResponse:
    return AuthRawResponse(
        status="OK",
        user={
            "user_id": "user-abc",
            "email": "alice@imbue.com",
            "display_name": "Alice",
            "profile_picture_url": "https://accounts.example/users/user-abc/profile-picture/abc",
        },
        # The payload segment is base64url for {"foo":"bar"} -- a decodable JWT
        # body without an exp claim, so expiry decoding yields None.
        tokens={"access_token": "header.eyJmb28iOiJiYXIifQ.sig", "refresh_token": "refresh-tok"},
        needs_email_verification=needs_email_verification,
    )


def test_persist_auth_response_marks_account_active(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    account = ImbueCloudAccount("alice@imbue.com")

    payload = _persist_auth_response(_make_auth_response(needs_email_verification=False), account, store)

    assert payload["email"] == "alice@imbue.com"
    assert payload["profile_picture_url"] == "https://accounts.example/users/user-abc/profile-picture/abc"
    session = store.load_by_account(account)
    assert session is not None
    # The profile picture rides the persisted session so `auth list` can serve it
    # without a connector round trip.
    assert session.profile_picture_url == "https://accounts.example/users/user-abc/profile-picture/abc"
    assert store.get_active_account() == account


def test_persist_auth_response_signs_in_even_when_old_connector_reports_unverified(tmp_path: Path) -> None:
    """Verification is non-blocking: an old connector's needs_email_verification=True changes nothing."""
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    account = ImbueCloudAccount("alice@imbue.com")

    _persist_auth_response(_make_auth_response(needs_email_verification=True), account, store)

    session = store.load_by_account(account)
    assert session is not None
    assert store.get_active_account() == account


def test_bind_callback_listener_reports_an_occupied_port_as_json(
    running_callback_server: tuple[_CallbackCaptureBox, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An occupied --callback-port is an OSError, which must become the JSON error body embedders parse."""
    _box, occupied_port = running_callback_server

    with pytest.raises(SystemExit):
        _bind_callback_listener(occupied_port, _make_callback_handler_class(_CallbackCaptureBox(), None, 0.0))

    stderr = capsys.readouterr().err
    assert '"error"' in stderr
    assert str(occupied_port) in stderr


def test_bind_callback_listener_reports_an_out_of_range_port_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --callback-port outside 0-65535 raises OverflowError from socket.bind,
    which must become the JSON error body, not a raw traceback."""
    with pytest.raises(SystemExit):
        _bind_callback_listener(70000, _make_callback_handler_class(_CallbackCaptureBox(), None, 0.0))

    stderr = capsys.readouterr().err
    assert '"error"' in stderr
    assert "70000" in stderr


def test_write_login_url_file_reports_an_unwritable_path_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad --url-file (missing parent dir) must become the JSON error body, not a traceback."""
    missing_dir_target = tmp_path / "no-such-dir" / "url.txt"

    with pytest.raises(SystemExit):
        _write_login_url_file(str(missing_dir_target), "https://example.com/login")

    stderr = capsys.readouterr().err
    assert '"error"' in stderr
    assert "no-such-dir" in stderr


def test_login_fails_fast_against_a_connector_without_the_accounts_surface(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale connector must be reported before any browser or listener starts.

    Without the probe, the login opens a 404 page and hangs until the listen
    timeout -- the worst possible way to learn the env needs a redeploy.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = ImbueCloudConnectorClient(
        base_url=AnyUrl("https://example.com"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SystemExit):
        _ensure_connector_supports_browser_login(client)
    stderr = capsys.readouterr().err
    assert "too old" in stderr
    assert "minds-admin env deploy" in stderr


def _store_with_stale_session(tmp_path: Path) -> tuple[ImbueCloudSessionStore, ImbueCloudAccount]:
    """A session store holding one session whose access token needs a refresh.

    The access token is not a JWT, so its expiry is unknown and
    ``is_access_token_near_expiry`` treats it as needing a refresh -- the
    same state a real session reaches once its ~1h access token lapses.
    """
    store = ImbueCloudSessionStore(sessions_dir=tmp_path / "sessions")
    account = ImbueCloudAccount("alice@example.com")
    session = make_session_from_tokens(
        user_id=SuperTokensUserId("user-1"),
        email=account,
        display_name=None,
        access_token="stale-at",
        refresh_token="rt-1",
        profile_picture_url=None,
    )
    store.save(session)
    return store, account


def test_signout_revoke_refreshes_an_expired_token_first(tmp_path: Path) -> None:
    """The revoke must carry a freshly-rotated token, not the expired one.

    The revoke endpoints answer 401 to an expired bearer token and the client
    treats 401 as "already revoked", so revoking with the stale token would
    silently skip the server-side revocation while the CLI reports success.
    """
    store, account = _store_with_stale_session(tmp_path)
    session = store.load_by_account(account)
    assert session is not None
    revoke_bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/session/refresh":
            return httpx.Response(
                200, json={"status": "OK", "tokens": {"access_token": "fresh-at", "refresh_token": "rt-2"}}
            )
        if request.url.path == "/auth/session/revoke":
            revoke_bearers.append(request.headers["authorization"])
            return httpx.Response(200, json={"status": "OK", "revoked_count": 3})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))

    _revoke_server_sessions(store, client, account, session, all_devices=True)

    assert revoke_bearers == ["Bearer fresh-at"]


def test_signout_revoke_falls_back_to_the_stored_token_when_refresh_fails(tmp_path: Path) -> None:
    """A dead refresh token means the session is already gone server-side.

    The revoke is still attempted with the stored token (its 401 is then a
    truthful "already revoked") and the failure must not escape -- signout
    proceeds to drop the local files either way.
    """
    store, account = _store_with_stale_session(tmp_path)
    session = store.load_by_account(account)
    assert session is not None
    revoke_bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/session/refresh":
            return httpx.Response(401, json={"detail": "refresh token revoked"})
        if request.url.path == "/auth/session/revoke-current":
            revoke_bearers.append(request.headers["authorization"])
            return httpx.Response(401, json={"detail": "expired"})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))

    assert _revoke_server_sessions(store, client, account, session, all_devices=False) is True

    assert revoke_bearers == ["Bearer stale-at"]


def _client_whose_revoke_endpoints_fail(
    tmp_path: Path,
) -> tuple[
    ImbueCloudSessionStore,
    ImbueCloudAccount,
    ImbueCloudConnectorClient,
]:
    """A store with one session and a client whose revoke endpoints answer 500.

    A non-401 error means the server-side revocation did NOT happen (401 is
    the only "already revoked" answer, and the client treats it as success).
    """
    store, account = _store_with_stale_session(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/session/refresh":
            return httpx.Response(401, json={"detail": "refresh token revoked"})
        if request.url.path in ("/auth/session/revoke", "/auth/session/revoke-current"):
            return httpx.Response(500, json={"detail": "internal error"})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    return store, account, client


def test_signout_revoke_reports_failure_when_the_server_errors(tmp_path: Path) -> None:
    """A failed single-device revoke must not masquerade as 'already revoked'.

    Sign-out still proceeds (dropping local tokens must work offline), so the
    helper reports the failure for the caller to surface instead of raising.
    """
    store, account, client = _client_whose_revoke_endpoints_fail(tmp_path)
    session = store.load_by_account(account)
    assert session is not None

    assert _revoke_server_sessions(store, client, account, session, all_devices=False) is False


def test_signout_revoke_all_devices_propagates_a_failed_revocation(tmp_path: Path) -> None:
    """--all-devices exists to kill every other session; a revoke that never
    landed must fail the command instead of reporting success."""
    store, account, client = _client_whose_revoke_endpoints_fail(tmp_path)
    session = store.load_by_account(account)
    assert session is not None

    with pytest.raises(ImbueCloudAuthError):
        _revoke_server_sessions(store, client, account, session, all_devices=True)


def test_capture_box_keeps_the_first_published_outcome() -> None:
    """The login command publishes a success inside its listener block and a
    failure on the way out; the success must not be overwritten."""
    box = _CallbackCaptureBox()
    box.publish_outcome(_LoginPageOutcome.SIGNED_IN)
    box.publish_outcome(_LoginPageOutcome.FAILED)
    assert box.wait_for_outcome(0.0) == _LoginPageOutcome.SIGNED_IN


def test_capture_box_reports_pending_when_no_outcome_arrives_in_time() -> None:
    assert _CallbackCaptureBox().wait_for_outcome(0.0) == _LoginPageOutcome.PENDING


def _run_login_and_complete_the_callback(connector_url: str, tmp_path: Path, host_dir: Path) -> tuple[Result, str]:
    """Run ``auth login`` against ``connector_url`` and play the browser's final redirect.

    Returns the command's result and the text of the page the browser was shown.
    """
    # The command saves the session under the active mngr profile.
    get_or_create_profile_dir(host_dir)
    url_file = tmp_path / "login.url"
    results: list[Result] = []
    login_thread = threading.Thread(
        target=lambda: results.append(
            CliRunner().invoke(
                auth,
                [
                    "login",
                    "--no-browser",
                    "--url-file",
                    str(url_file),
                    "--success-redirect-url",
                    "minds://",
                    "--connector-url",
                    connector_url,
                ],
            )
        ),
        daemon=True,
        name="auth-login-under-test",
    )
    login_thread.start()

    login_url, _, _ = poll_for_value(lambda: url_file.read_text().strip() if url_file.exists() else None, timeout=30.0)
    assert login_url is not None, "auth login never wrote its sign-in URL"
    next_path = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)["next"][0]
    authorize_query = urllib.parse.parse_qs(urllib.parse.urlparse(next_path).query)
    callback_url = authorize_query["redirect_uri"][0]
    state = authorize_query["state"][0]

    page = _get_page_text(f"{callback_url}?{urllib.parse.urlencode({'code': 'code-8821', 'state': state})}")
    login_thread.join(timeout=60.0)
    assert results, "auth login did not finish"
    return results[0], page


def test_login_page_says_signed_in_only_after_the_code_exchange_succeeds(
    device_login_connector_stub: Callable[..., tuple[str, list[str]]],
    tmp_path: Path,
    temp_host_dir: Path,
) -> None:
    result, page = _run_login_and_complete_the_callback(device_login_connector_stub(200)[0], tmp_path, temp_host_dir)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["email"] == "device-login-4417@example.com"
    assert "You&#x27;re in!" in page


def test_login_page_reports_failure_when_the_connector_refuses_the_code(
    device_login_connector_stub: Callable[..., tuple[str, list[str]]],
    tmp_path: Path,
    temp_host_dir: Path,
) -> None:
    result, page = _run_login_and_complete_the_callback(device_login_connector_stub(400)[0], tmp_path, temp_host_dir)

    assert result.exit_code == 1
    # The JSON error body follows the "Open this URL" line on stderr.
    error_body = json.loads(result.stderr[result.stderr.index("{") :])
    assert error_body["error_class"] == "ImbueCloudDeviceCodeRefusedError"
    assert "Sign-in didn&#x27;t finish" in page
    assert "You&#x27;re in" not in page


def test_login_gives_up_after_the_requested_listen_timeout(
    device_login_connector_stub: Callable[..., tuple[str, list[str]]],
    tmp_path: Path,
    temp_host_dir: Path,
) -> None:
    get_or_create_profile_dir(temp_host_dir)
    result = CliRunner().invoke(
        auth,
        [
            "login",
            "--no-browser",
            "--listen-timeout",
            "1",
            "--url-file",
            str(tmp_path / "login.url"),
            "--connector-url",
            device_login_connector_stub(200)[0],
        ],
    )

    assert result.exit_code == 1
    error_body = json.loads(result.stderr[result.stderr.index("{") :])
    assert error_body["error_class"] == "LoginTimeout"


def test_login_holds_the_listener_lease_while_waiting_and_releases_it_after(
    device_login_connector_stub: Callable[..., tuple[str, list[str]]],
    tmp_path: Path,
    temp_host_dir: Path,
) -> None:
    """The connector checks this lease before redirecting, so it must be live before the browser can arrive."""
    connector_url, recorded_requests = device_login_connector_stub(200)

    result, page = _run_login_and_complete_the_callback(connector_url, tmp_path, temp_host_dir)

    assert result.exit_code == 0, result.output
    assert "You&#x27;re in!" in page
    lease_requests = [request for request in recorded_requests if "/auth/device/attempts/" in request]
    challenge = lease_requests[0].rsplit("/", 1)[1]
    assert lease_requests[0] == f"PUT /auth/device/attempts/{challenge}"
    assert lease_requests[-1] == f"DELETE /auth/device/attempts/{challenge}"
    assert recorded_requests.index(lease_requests[0]) < recorded_requests.index("POST /auth/device/token")


def test_login_still_signs_in_against_a_connector_without_listener_leases(
    device_login_connector_stub: Callable[..., tuple[str, list[str]]],
    tmp_path: Path,
    temp_host_dir: Path,
) -> None:
    connector_url, _recorded_requests = device_login_connector_stub(200, device_attempts_status_code=404)

    result, page = _run_login_and_complete_the_callback(connector_url, tmp_path, temp_host_dir)

    assert result.exit_code == 0, result.output
    assert "You&#x27;re in!" in page


def test_sigterm_while_holding_the_listener_lease_still_releases_it() -> None:
    """The desktop stops ``auth login`` with SIGTERM on quit; the connector must still learn the listener is gone."""
    lease_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lease_requests.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"status": "OK"})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx.MockTransport(handler))
    challenge = compute_pkce_challenge(make_pkce_verifier())
    # The handler is only installed on the main thread; elsewhere the raised SIGTERM would kill the test process.
    assert threading.current_thread() is threading.main_thread()
    handler_before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as exc_info:
        with _hold_listener_lease(client, challenge):
            signal.raise_signal(signal.SIGTERM)

    assert exc_info.value.code == 128 + signal.SIGTERM
    assert lease_requests == [
        f"PUT /auth/device/attempts/{challenge}",
        f"DELETE /auth/device/attempts/{challenge}",
    ]
    assert signal.getsignal(signal.SIGTERM) == handler_before
