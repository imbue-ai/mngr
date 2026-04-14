"""SuperTokens session management for the minds desktop client.

Manages a single global user session stored as a JSON file on disk.
The stored access token (JWT) is used for authenticating with the
cloudflare_forwarding service. Refresh tokens allow automatic renewal.
"""

import json
import threading
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr

_SESSION_FILENAME = "supertokens_session.json"
_HAS_SIGNED_IN_FILENAME = "has_signed_in"
_USER_ID_PREFIX_LENGTH = 16


class SuperTokensAccessToken(NonEmptyStr):
    """A SuperTokens JWT access token."""

    ...


class SuperTokensRefreshToken(NonEmptyStr):
    """A SuperTokens refresh token."""

    ...


class SuperTokensUserId(NonEmptyStr):
    """A SuperTokens user ID (UUID v4)."""

    ...


class UserIdPrefix(NonEmptyStr):
    """First 16 hex chars of a SuperTokens user ID, used for tunnel naming."""

    ...


class StoredSession(FrozenModel):
    """Session data persisted to disk."""

    access_token: SuperTokensAccessToken = Field(description="JWT access token")
    refresh_token: SuperTokensRefreshToken | None = Field(
        default=None, description="Refresh token for obtaining new access tokens (absent for short-lived sessions)"
    )
    user_id: SuperTokensUserId = Field(description="SuperTokens user ID")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="Display name from OAuth provider")


class UserInfo(FrozenModel):
    """Public user information returned by the auth status endpoint."""

    user_id: SuperTokensUserId = Field(description="SuperTokens user ID")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="Display name from OAuth provider")
    user_id_prefix: UserIdPrefix = Field(description="First 16 hex chars of user ID for tunnel naming")


def derive_user_id_prefix(user_id: str) -> UserIdPrefix:
    """Derive a 16-char hex prefix from a SuperTokens user ID (UUID v4).

    Strips hyphens from the UUID and takes the first 16 hex characters.
    """
    hex_chars = user_id.replace("-", "")
    return UserIdPrefix(hex_chars[:_USER_ID_PREFIX_LENGTH])


class SuperTokensSessionStore(MutableModel):
    """Manages a single global SuperTokens session stored on disk.

    Thread-safe: all read/write operations are protected by a lock.
    """

    data_directory: Path = Field(frozen=True, description="Directory for session data files")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def store_session(
        self,
        access_token: str,
        refresh_token: str | None,
        user_id: str,
        email: str,
        display_name: str | None = None,
    ) -> None:
        """Store session tokens and user info to disk."""
        session = StoredSession(
            access_token=SuperTokensAccessToken(access_token),
            refresh_token=SuperTokensRefreshToken(refresh_token) if refresh_token else None,
            user_id=SuperTokensUserId(user_id),
            email=email,
            display_name=display_name,
        )
        with self._lock:
            self.data_directory.mkdir(parents=True, exist_ok=True)
            session_path = self.data_directory / _SESSION_FILENAME
            session_path.write_text(json.dumps(session.model_dump(mode="json"), indent=2))
            session_path.chmod(0o600)
            # Mark that the user has signed in at least once
            flag_path = self.data_directory / _HAS_SIGNED_IN_FILENAME
            if not flag_path.exists():
                flag_path.touch()
            logger.info("Stored SuperTokens session for user {}", email)

    def load_session(self) -> StoredSession | None:
        """Load session from disk. Returns None if no session exists or file is corrupt."""
        session_path = self.data_directory / _SESSION_FILENAME
        with self._lock:
            if not session_path.exists():
                return None
            try:
                raw = json.loads(session_path.read_text())
                return StoredSession.model_validate(raw)
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load SuperTokens session: {}", e)
                return None

    def clear_session(self) -> None:
        """Remove stored session tokens (sign out)."""
        session_path = self.data_directory / _SESSION_FILENAME
        with self._lock:
            if session_path.exists():
                session_path.unlink()
                logger.info("Cleared SuperTokens session")

    def get_access_token(self) -> str | None:
        """Return the stored access token, or None if not signed in."""
        session = self.load_session()
        if session is None:
            return None
        return str(session.access_token)

    def get_user_info(self) -> UserInfo | None:
        """Return user info from the stored session, or None if not signed in."""
        session = self.load_session()
        if session is None:
            return None
        return UserInfo(
            user_id=session.user_id,
            email=session.email,
            display_name=session.display_name,
            user_id_prefix=derive_user_id_prefix(str(session.user_id)),
        )

    def is_signed_in(self) -> bool:
        """Check if a session exists on disk."""
        session_path = self.data_directory / _SESSION_FILENAME
        return session_path.exists()

    def has_signed_in_before(self) -> bool:
        """Check if the user has ever signed in (flag file exists)."""
        flag_path = self.data_directory / _HAS_SIGNED_IN_FILENAME
        return flag_path.exists()

    def update_access_token(self, new_access_token: str) -> None:
        """Update the stored access token after a refresh."""
        session = self.load_session()
        if session is None:
            return
        self.store_session(
            access_token=new_access_token,
            refresh_token=str(session.refresh_token) if session.refresh_token is not None else None,
            user_id=str(session.user_id),
            email=session.email,
            display_name=session.display_name,
        )
