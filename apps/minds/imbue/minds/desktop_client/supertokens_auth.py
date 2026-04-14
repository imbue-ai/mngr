"""SuperTokens session management for the minds desktop client.

Manages a single global user session stored as a JSON file on disk.
The stored access token (JWT) is used for authenticating with the
cloudflare_forwarding service. Refresh tokens allow automatic renewal.
"""

import base64
import binascii
import json
import threading
import time
from pathlib import Path

from loguru import logger
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import refresh_session_without_request_response
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


_TOKEN_EXPIRY_BUFFER_SECONDS = 60


def _jwt_seconds_until_expiry(token: str) -> float | None:
    """Return seconds until the JWT expires, or None if the expiry cannot be determined."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None:
            return None
        return float(exp) - time.time()
    except (IndexError, ValueError, json.JSONDecodeError, binascii.Error):
        return None


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
        """Return a valid access token, refreshing automatically if expired.

        Returns None if not signed in or if the token cannot be refreshed.
        """
        session = self.load_session()
        if session is None:
            return None

        token = str(session.access_token)
        seconds_left = _jwt_seconds_until_expiry(token)
        if seconds_left is not None and seconds_left < _TOKEN_EXPIRY_BUFFER_SECONDS:
            refreshed = self._try_refresh(session)
            if refreshed is not None:
                return refreshed
            if seconds_left < 0:
                logger.warning("Access token expired and refresh failed")
                return None

        return token

    def _try_refresh(self, session: "StoredSession") -> str | None:
        """Attempt to refresh the access token using the stored refresh token.

        Returns the new access token on success, or None on failure.
        """
        if session.refresh_token is None:
            return None
        try:
            new_session = refresh_session_without_request_response(
                refresh_token=str(session.refresh_token),
            )
            tokens = new_session.get_all_session_tokens_dangerously()
            new_access_token = tokens["accessToken"]
            new_refresh_token = tokens.get("refreshToken") or str(session.refresh_token)
            self.store_session(
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                user_id=str(session.user_id),
                email=session.email,
                display_name=session.display_name,
            )
            logger.info("Refreshed expired SuperTokens access token")
            return new_access_token
        except (ValueError, TypeError, KeyError, OSError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.warning("Failed to refresh SuperTokens session: {}", exc)
            return None

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
