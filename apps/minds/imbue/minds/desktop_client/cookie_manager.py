from typing import Final

from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer

from imbue.minds.primitives import CookieSigningKey

_COOKIE_SALT: Final[str] = "minds-auth"
_SUBDOMAIN_AUTH_SALT: Final[str] = "minds-subdomain-auth"

SESSION_COOKIE_NAME: Final[str] = "minds_session"

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
) -> bool:
    """Verify a session cookie is valid and not expired."""
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
