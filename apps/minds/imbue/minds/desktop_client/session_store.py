"""Multi-account session store for the minds desktop client.

Replaces the single-session SuperTokensSessionStore with support for
multiple simultaneous accounts. Sessions are stored in a single
``sessions.json`` file at ``~/.minds/sessions.json``, keyed by user ID.
Each account entry also tracks which workspace agent IDs are associated
with it.
"""

import base64
import binascii
import json
import threading
import time
from pathlib import Path

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient

_SESSIONS_FILENAME = "sessions.json"
_USER_ID_PREFIX_LENGTH = 16
_TOKEN_EXPIRY_BUFFER_SECONDS = 60


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


class AccountSession(FrozenModel):
    """Session data for a single account, persisted inside sessions.json."""

    access_token: SuperTokensAccessToken = Field(description="JWT access token")
    refresh_token: SuperTokensRefreshToken | None = Field(
        default=None, description="Refresh token for obtaining new access tokens"
    )
    user_id: SuperTokensUserId = Field(description="SuperTokens user ID")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="Display name from OAuth provider")
    workspace_ids: list[str] = Field(default_factory=list, description="Agent IDs associated with this account")


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


class MultiAccountSessionStore(MutableModel):
    """Manages multiple SuperTokens sessions stored on disk.

    Thread-safe: all read/write operations are protected by a lock.
    Sessions are stored in a single JSON file mapping user IDs to
    AccountSession data.
    """

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    auth_backend_client: AuthBackendClient | None = Field(
        default=None,
        description="Backend used to refresh sessions. When omitted, expired tokens are not refreshed.",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Per-user lock protecting the refresh-token rotation. Without this, two
    # concurrent workspace agents hitting an expiring token would both call
    # ``/auth/session/refresh`` -- SuperTokens rotates the refresh token on
    # each successful refresh, so the second call would invalidate the first
    # and leave one caller with a dead refresh token.
    _refresh_locks: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _refresh_locks_guard: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _sessions_path(self) -> Path:
        return self.data_dir / _SESSIONS_FILENAME

    def _read_all(self) -> dict[str, AccountSession]:
        """Read all sessions from disk. Returns empty dict on missing/corrupt file."""
        path = self._sessions_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                return {}
            result: dict[str, AccountSession] = {}
            for user_id, data in raw.items():
                try:
                    result[user_id] = AccountSession.model_validate(data)
                except (ValueError, TypeError) as e:
                    logger.warning("Skipping corrupt session for user {}: {}", user_id, e)
            return result
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load sessions: {}", e)
            return {}

    def _write_all(self, sessions: dict[str, AccountSession]) -> None:
        """Write all sessions to disk atomically."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._sessions_path
        serialized = {uid: sess.model_dump(mode="json") for uid, sess in sessions.items()}
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(serialized, indent=2))
        tmp_path.chmod(0o600)
        tmp_path.rename(path)

    def add_or_update_session(
        self,
        access_token: str,
        refresh_token: str | None,
        user_id: str,
        email: str,
        display_name: str | None = None,
    ) -> None:
        """Add a new account session or update tokens for an existing one.

        Preserves workspace_ids if the user already exists.
        """
        with self._lock:
            sessions = self._read_all()
            existing = sessions.get(user_id)
            workspace_ids = existing.workspace_ids if existing is not None else []
            sessions[user_id] = AccountSession(
                access_token=SuperTokensAccessToken(access_token),
                refresh_token=SuperTokensRefreshToken(refresh_token) if refresh_token else None,
                user_id=SuperTokensUserId(user_id),
                email=email,
                display_name=display_name,
                workspace_ids=workspace_ids,
            )
            self._write_all(sessions)
            logger.info("Stored session for user {} ({})", email, user_id[:8])

    def remove_session(self, user_id: str) -> None:
        """Remove an account session (log out). Does NOT disassociate workspaces."""
        with self._lock:
            sessions = self._read_all()
            if user_id in sessions:
                email = sessions[user_id].email
                del sessions[user_id]
                self._write_all(sessions)
                logger.info("Removed session for user {} ({})", email, user_id[:8])

    def load_all_sessions(self) -> dict[str, AccountSession]:
        """Load all sessions from disk."""
        with self._lock:
            return self._read_all()

    def get_session(self, user_id: str) -> AccountSession | None:
        """Load a specific account session by user ID."""
        with self._lock:
            sessions = self._read_all()
            return sessions.get(user_id)

    def get_access_token(self, user_id: str) -> str | None:
        """Return a valid access token for the given user, refreshing if expired.

        Returns None if the user is not signed in or the token cannot be refreshed.
        """
        session = self.get_session(user_id)
        if session is None:
            return None

        token = str(session.access_token)
        seconds_left = _jwt_seconds_until_expiry(token)
        if seconds_left is not None and seconds_left < _TOKEN_EXPIRY_BUFFER_SECONDS:
            refreshed = self._try_refresh(session)
            if refreshed is not None:
                return refreshed
            if seconds_left < 0:
                logger.warning("Access token expired for user {} and refresh failed", user_id[:8])
                return None

        return token

    def _refresh_lock_for(self, user_id: str) -> threading.Lock:
        """Return the per-user refresh lock, creating it on first use."""
        with self._refresh_locks_guard:
            lock = self._refresh_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[user_id] = lock
            return lock

    def _try_refresh(self, session: AccountSession) -> str | None:
        """Attempt to refresh the access token. Returns new token on success, None on failure.

        Serializes concurrent refreshes for the same user behind a per-user
        lock. The second caller re-reads the stored session once it acquires
        the lock -- if the first caller already rotated the tokens, the newly
        stored access token is returned without hitting the backend again.
        """
        if session.refresh_token is None or self.auth_backend_client is None:
            return None
        user_id_str = str(session.user_id)
        with self._refresh_lock_for(user_id_str):
            latest = self.get_session(user_id_str)
            if latest is None:
                return None
            latest_token_str = str(latest.access_token)
            seconds_left = _jwt_seconds_until_expiry(latest_token_str)
            if seconds_left is not None and seconds_left >= _TOKEN_EXPIRY_BUFFER_SECONDS:
                # Another caller already refreshed while we waited.
                return latest_token_str
            refresh_token = latest.refresh_token
            if refresh_token is None:
                return None
            try:
                tokens = self.auth_backend_client.refresh_session(refresh_token=str(refresh_token))
            except httpx.HTTPError as exc:
                logger.warning("Failed to refresh session for user {}: {}", user_id_str[:8], exc)
                return None
            if tokens is None:
                logger.warning("Backend rejected session refresh for user {}", user_id_str[:8])
                return None
            new_refresh_token = tokens.refresh_token or str(refresh_token)
            self.add_or_update_session(
                access_token=tokens.access_token,
                refresh_token=new_refresh_token,
                user_id=user_id_str,
                email=latest.email,
                display_name=latest.display_name,
            )
            logger.info("Refreshed access token for user {}", user_id_str[:8])
            return tokens.access_token

    def associate_workspace(self, user_id: str, agent_id: str) -> None:
        """Associate a workspace with an account."""
        with self._lock:
            sessions = self._read_all()
            session = sessions.get(user_id)
            if session is None:
                logger.warning("Cannot associate workspace {}: user {} not found", agent_id, user_id[:8])
                return
            if agent_id not in session.workspace_ids:
                updated_ids = [*session.workspace_ids, agent_id]
                sessions[user_id] = session.model_copy_update(
                    to_update(session.field_ref().workspace_ids, updated_ids),
                )
                self._write_all(sessions)
                logger.info("Associated workspace {} with user {}", agent_id, user_id[:8])

    def disassociate_workspace(self, user_id: str, agent_id: str) -> None:
        """Disassociate a workspace from an account."""
        with self._lock:
            sessions = self._read_all()
            session = sessions.get(user_id)
            if session is None:
                return
            if agent_id in session.workspace_ids:
                updated_ids = [wid for wid in session.workspace_ids if wid != agent_id]
                sessions[user_id] = session.model_copy_update(
                    to_update(session.field_ref().workspace_ids, updated_ids),
                )
                self._write_all(sessions)
                logger.info("Disassociated workspace {} from user {}", agent_id, user_id[:8])

    def get_account_for_workspace(self, agent_id: str) -> AccountSession | None:
        """Find the account associated with a workspace, or None if private."""
        with self._lock:
            sessions = self._read_all()
            for session in sessions.values():
                if agent_id in session.workspace_ids:
                    return session
            return None

    def list_accounts(self) -> list[AccountSession]:
        """Return all logged-in accounts."""
        with self._lock:
            sessions = self._read_all()
            return list(sessions.values())

    def get_user_info(self, user_id: str) -> UserInfo | None:
        """Return user info for a specific account, or None if not logged in."""
        session = self.get_session(user_id)
        if session is None:
            return None
        return UserInfo(
            user_id=session.user_id,
            email=session.email,
            display_name=session.display_name,
            user_id_prefix=derive_user_id_prefix(str(session.user_id)),
        )

    def is_any_signed_in(self) -> bool:
        """Check if any account is currently signed in."""
        with self._lock:
            return bool(self._read_all())

    def has_signed_in_before(self) -> bool:
        """Check if sessions.json exists (user has ever signed in)."""
        return self._sessions_path.exists()
