"""Session-cookie + per-subdomain auth-token helpers.

Adapted from ``minds.desktop_client.cookie_manager``. Cookie name is pinned
to ``mngr_forward_session`` so it does not collide with minds' own
``minds_session`` cookie when both bind on different ports of the same host.

The ``verify_session_cookie`` helper accepts an optional opaque
``preauth_cookie_value``: if the inbound cookie matches that value exactly,
it is accepted without verifying a signature. This lets the Electron shell
pre-set a cookie on the bare origin before the first navigation so the
initial OTP flow can be skipped when ``minds run`` spawns the plugin.
"""

from typing import Final

from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer

from imbue.mngr_forward.primitives import CookieSigningKey

_COOKIE_SALT: Final[str] = "mngr-forward-auth"
_SUBDOMAIN_AUTH_SALT: Final[str] = "mngr-forward-subdomain-auth"

_SESSION_PAYLOAD: Final[str] = "authenticated"

_COOKIE_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60
_SUBDOMAIN_TOKEN_MAX_AGE_SECONDS: Final[int] = 30


def create_session_cookie(signing_key: CookieSigningKey) -> str:
    """Create a signed session cookie value for global authentication."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(_SESSION_PAYLOAD, salt=_COOKIE_SALT)


def verify_session_cookie(
    cookie_value: str,
    signing_key: CookieSigningKey,
    preauth_cookie_value: str | None = None,
) -> bool:
    """Verify a session cookie is valid and not expired.

    When ``preauth_cookie_value`` is provided and equals ``cookie_value``,
    the cookie is accepted without checking a signature. This is the
    Electron pre-set path. Otherwise the cookie must be a signed token
    issued by ``create_session_cookie`` and within the max-age window.
    """
    if preauth_cookie_value is not None and cookie_value == preauth_cookie_value:
        return True
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


def create_subdomain_auth_token(signing_key: CookieSigningKey, agent_id: str) -> str:
    """Mint a short-lived signed token that authorizes setting a session cookie
    on the ``<agent-id>.localhost`` subdomain.

    Used by the ``/goto/{agent_id}/`` bridge: the bare-origin handler (which
    can read the real session cookie) mints this token, 302-redirects the
    browser to the subdomain with the token in the query string, and the
    subdomain handler verifies it before setting its own subdomain-scoped
    session cookie. Short expiry (seconds) keeps the token effectively
    one-shot even though it isn't actually consumed on validation.
    """
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(agent_id, salt=_SUBDOMAIN_AUTH_SALT)


def verify_subdomain_auth_token(
    token: str,
    signing_key: CookieSigningKey,
    agent_id: str,
) -> bool:
    """Verify that ``token`` was minted by ``create_subdomain_auth_token`` for
    ``agent_id`` and is within the short expiry window."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    try:
        payload = serializer.loads(
            token,
            salt=_SUBDOMAIN_AUTH_SALT,
            max_age=_SUBDOMAIN_TOKEN_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return False
    return payload == agent_id
