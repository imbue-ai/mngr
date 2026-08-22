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
immediately -- verification is non-blocking) and the PKCE / login-URL helpers
the browser flow is built from.
"""

import base64
import hashlib
import urllib.parse
import urllib.request
from pathlib import Path

import httpx
import pytest
from pydantic import AnyUrl

from imbue.mngr_imbue_cloud.cli._common import resolve_accounts_url
from imbue.mngr_imbue_cloud.cli.auth import _CallbackCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _bind_callback_listener
from imbue.mngr_imbue_cloud.cli.auth import _ensure_connector_supports_browser_login
from imbue.mngr_imbue_cloud.cli.auth import _login_success_page
from imbue.mngr_imbue_cloud.cli.auth import _make_callback_handler_class
from imbue.mngr_imbue_cloud.cli.auth import _persist_auth_response
from imbue.mngr_imbue_cloud.cli.auth import _revoke_server_sessions
from imbue.mngr_imbue_cloud.cli.auth import _write_login_url_file
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


def test_success_page_without_redirect_says_return_to_terminal() -> None:
    page = _login_success_page(None).decode("utf-8")
    assert "return to your terminal" in page
    assert "<script>" not in page


def test_success_page_with_redirect_links_to_url_without_auto_navigation() -> None:
    # Deliberately a plain link, not an automatic navigation: the click is the
    # user gesture that triggers the browser's open-external-app prompt. The
    # app-driven variant carries the minds wordmark and copy.
    page = _login_success_page("minds://").decode("utf-8")
    assert '<a href="minds://">Open app</a>' in page
    assert "<svg" in page and 'fill="currentColor"' in page
    assert "Feel free to close this tab." in page
    assert "<script>" not in page


def test_success_page_escapes_redirect_url_markup() -> None:
    """A crafted URL must not be able to inject markup into the page: the
    href is attribute-escaped."""
    page = _login_success_page('minds://x?a=<b>&q="hi"').decode("utf-8")
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
        user={"user_id": "user-abc", "email": "alice@imbue.com", "display_name": "Alice"},
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
    session = store.load_by_account(account)
    assert session is not None
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
        _bind_callback_listener(occupied_port, _make_callback_handler_class(_CallbackCaptureBox(), None))

    stderr = capsys.readouterr().err
    assert '"error"' in stderr
    assert str(occupied_port) in stderr


def test_bind_callback_listener_reports_an_out_of_range_port_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --callback-port outside 0-65535 raises OverflowError from socket.bind,
    which must become the JSON error body, not a raw traceback."""
    with pytest.raises(SystemExit):
        _bind_callback_listener(70000, _make_callback_handler_class(_CallbackCaptureBox(), None))

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
