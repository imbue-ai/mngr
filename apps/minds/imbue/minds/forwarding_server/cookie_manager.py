from typing import Final

from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer

from imbue.minds.primitives import CookieSigningKey

_COOKIE_SALT: Final[str] = "mind-auth"

SESSION_COOKIE_NAME: Final[str] = "mind_session"

_SESSION_PAYLOAD: Final[str] = "authenticated"

_COOKIE_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60


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
