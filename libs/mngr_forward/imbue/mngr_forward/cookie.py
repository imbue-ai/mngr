"""Session-cookie + per-subdomain auth-token helpers.

Adapted from ``minds.desktop_client.cookie_manager``. Cookie name is pinned
to ``mngr_forward_session`` so it does not collide with a host application's
own session cookie when both bind on different ports of the same host.

``classify_session_cookie`` accepts an optional opaque
``preauth_cookie_value``: if the inbound cookie matches that value exactly,
it is accepted without verifying a signature. This lets the Electron shell
pre-set a cookie on the bare origin before the first navigation so the
initial OTP flow can be skipped when the host application spawns the plugin.
"""

from enum import auto
from typing import Final

from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr_forward.primitives import CookieSigningKey

_COOKIE_SALT: Final[str] = "mngr-forward-auth"
_SUBDOMAIN_AUTH_SALT: Final[str] = "mngr-forward-subdomain-auth"

_SESSION_PAYLOAD: Final[str] = "authenticated"

_COOKIE_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60
_SUBDOMAIN_TOKEN_MAX_AGE_SECONDS: Final[int] = 30


class SessionCookieVerdict(UpperCaseStrEnum):
    """What a presented session cookie is worth."""

    SIGNED = auto()
    PREAUTH = auto()
    INVALID = auto()


def create_session_cookie(signing_key: CookieSigningKey) -> str:
    """Create a signed session cookie value for global authentication."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(_SESSION_PAYLOAD, salt=_COOKIE_SALT)


def verify_session_cookie(
    cookie_value: str,
    signing_key: CookieSigningKey,
) -> bool:
    """Verify a session cookie is a signed token issued by ``create_session_cookie`` and within the max-age window."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    try:
        payload = serializer.loads(
            cookie_value,
            salt=_COOKIE_SALT,
            max_age=_COOKIE_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return False
    return payload == _SESSION_PAYLOAD


def classify_session_cookie(
    cookie_value: str,
    signing_key: CookieSigningKey,
    preauth_cookie_value: str | None,
) -> SessionCookieVerdict:
    """Decide what a presented session cookie is worth.

    A cookie equal to ``preauth_cookie_value`` is accepted without checking a
    signature: the Electron pre-set path. Anything else must be a signed token.
    """
    if preauth_cookie_value is not None and cookie_value == preauth_cookie_value:
        return SessionCookieVerdict.PREAUTH
    if verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key):
        return SessionCookieVerdict.SIGNED
    return SessionCookieVerdict.INVALID


def create_subdomain_auth_token(signing_key: CookieSigningKey, origin_coordinate: str) -> str:
    """Mint a short-lived signed token that authorizes setting a session cookie
    on the ``agent-<hex>.localhost`` origin domain (or a legacy host-keyed one).

    Used by the ``/goto/{coordinate}/`` bridge: the bare-origin handler (which
    can read the real session cookie) mints this token, 302-redirects the
    browser to the workspace origin with the token in the query string, and
    the workspace handler verifies it before setting the domain-scoped
    session cookie. Short expiry (seconds) keeps the token effectively
    one-shot even though it isn't actually consumed on validation.
    """
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(origin_coordinate, salt=_SUBDOMAIN_AUTH_SALT)


def verify_subdomain_auth_token(
    token: str,
    signing_key: CookieSigningKey,
    origin_coordinate: str,
) -> bool:
    """Verify that ``token`` was minted by ``create_subdomain_auth_token`` for
    ``origin_coordinate`` and is within the short expiry window."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    try:
        payload = serializer.loads(
            token,
            salt=_SUBDOMAIN_AUTH_SALT,
            max_age=_SUBDOMAIN_TOKEN_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return False
    return payload == origin_coordinate
