import secrets
from enum import auto
from typing import assert_never

from flask import Flask
from flask import Response
from flask import g
from flask import request

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.state import get_state
from imbue.minds.primitives import CookieSigningKey


class SessionCookieVerdict(UpperCaseStrEnum):
    """What the session cookie a request presented is worth."""

    SIGNED = auto()
    PREAUTH = auto()
    INVALID = auto()


def _classify_presented_session_cookie() -> SessionCookieVerdict:
    presented_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if presented_cookie is None:
        return SessionCookieVerdict.INVALID
    state = get_state()
    preauth_cookie = state.session_preauth_cookie
    # Compared as bytes: compare_digest raises TypeError on a non-ASCII str, and
    # the presented value is whatever the caller sent.
    if preauth_cookie is not None and secrets.compare_digest(
        presented_cookie.encode("utf-8"), preauth_cookie.get_secret_value().encode("utf-8")
    ):
        return SessionCookieVerdict.PREAUTH
    if verify_session_cookie(cookie_value=presented_cookie, signing_key=state.auth_store.get_signing_key()):
        return SessionCookieVerdict.SIGNED
    return SessionCookieVerdict.INVALID


def get_request_session_cookie_verdict() -> SessionCookieVerdict:
    """Classify the current request's session cookie, once per request however many checks ask."""
    if "minds_session_cookie_verdict" not in g:
        g.minds_session_cookie_verdict = _classify_presented_session_cookie()
    return g.minds_session_cookie_verdict


def is_request_session_cookie_accepted() -> bool:
    return get_request_session_cookie_verdict() != SessionCookieVerdict.INVALID


def set_fresh_session_cookie(response: Response, signing_key: CookieSigningKey) -> None:
    """Set a newly signed session cookie on the response.

    Host-only: both curl and Chromium treat ``localhost`` as a public suffix and
    refuse to send a ``Domain=localhost`` cookie to subdomains, so each workspace
    subdomain gets its own cookie through the forward server's ``/goto/<agent-id>/``
    auth bridge instead. No expiry either: the cookie lasts for the browser session,
    and its age is bounded by the signature when it is verified.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_cookie(signing_key=signing_key),
        path="/",
        httponly=True,
        samesite="lax",
    )


def _is_session_cookie_set_on(response: Response) -> bool:
    return any(header.startswith(f"{SESSION_COOKIE_NAME}=") for header in response.headers.getlist("Set-Cookie"))


def _renew_presented_session_cookie(response: Response) -> Response:
    """Answer a request that presented a valid signed session cookie with a newly signed one.

    A signed cookie stops being accepted a fixed time after it was signed, so a
    session outlives that only by being re-signed while it is in use. The preauth
    value does not age, and a signed cookie in its place would.
    """
    match get_request_session_cookie_verdict():
        case SessionCookieVerdict.SIGNED:
            if not _is_session_cookie_set_on(response):
                set_fresh_session_cookie(response, get_state().auth_store.get_signing_key())
        case SessionCookieVerdict.PREAUTH | SessionCookieVerdict.INVALID:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return response


def register_session_renewal(app: Flask) -> None:
    """Install the renewal on the app: every response to a request with a valid signed cookie carries a fresh one."""
    app.after_request(_renew_presented_session_cookie)
