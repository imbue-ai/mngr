import re
from typing import Final

from itsdangerous import BadSignature
from itsdangerous import URLSafeTimedSerializer

from imbue.changelings.primitives import CookieSigningKey
from imbue.imbue_common.pure import pure
from imbue.mng.primitives import AgentId

_COOKIE_SALT: Final[str] = "changeling-auth"

_COOKIE_PREFIX: Final[str] = "changeling_"

_COOKIE_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60

# Only allow alphanumeric characters, hyphens, and underscores in cookie names
_SAFE_COOKIE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_-]")


@pure
def get_cookie_name_for_agent(agent_id: AgentId) -> str:
    """Return the cookie name used to store auth for a specific agent."""
    sanitized = _SAFE_COOKIE_NAME_PATTERN.sub("_", str(agent_id))
    return f"{_COOKIE_PREFIX}{sanitized}"


def create_signed_cookie_value(
    agent_id: AgentId,
    signing_key: CookieSigningKey,
) -> str:
    """Create a signed cookie value containing the agent ID."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    return serializer.dumps(str(agent_id), salt=_COOKIE_SALT)


def verify_signed_cookie_value(
    cookie_value: str,
    signing_key: CookieSigningKey,
) -> AgentId | None:
    """Verify and decode a signed cookie, returning the agent ID or None if invalid."""
    serializer = URLSafeTimedSerializer(secret_key=signing_key.get_secret_value())
    try:
        agent_id_str = serializer.loads(
            cookie_value,
            salt=_COOKIE_SALT,
            max_age=_COOKIE_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return None
    if not isinstance(agent_id_str, str) or not agent_id_str:
        return None
    return AgentId(agent_id_str)
