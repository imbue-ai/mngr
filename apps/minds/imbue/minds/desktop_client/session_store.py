"""Multi-account session-mirror for the minds desktop client.

Records the *identity* of every account the user has signed into, and the
mapping from each account to the workspace agent ids it owns. The ``mngr_imbue_cloud``
plugin holds the SuperTokens tokens themselves -- minds never copies them
into its own file. Anything that needs an authenticated request to the
connector goes through ``mngr imbue_cloud …`` (which transparently refreshes
near-expiry tokens before each call), so the only state minds keeps is what
the UI needs to render: the account's email, display name, user_id, and the
list of workspaces associated with it.
"""

import json
import threading
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr

_SESSIONS_FILENAME = "sessions.json"
_USER_ID_PREFIX_LENGTH = 16


class SuperTokensUserId(NonEmptyStr):
    """A SuperTokens user ID (UUID v4)."""

    ...


class UserIdPrefix(NonEmptyStr):
    """First 16 hex chars of a SuperTokens user ID, used for tunnel naming."""

    ...


class AccountSession(FrozenModel):
    """Identity of one account the user has signed into.

    No token material is stored here -- the ``mngr_imbue_cloud`` plugin owns
    the SuperTokens session on disk and refreshes tokens transparently each
    time minds invokes ``mngr imbue_cloud …``. This record exists only so the
    desktop UI can render account chips and so workspace<->account
    associations survive across desktop client restarts.
    """

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


class MultiAccountSessionStore(MutableModel):
    """Persists the user's signed-in account identities (no token material).

    Thread-safe: all read/write operations are protected by a single lock.
    Sessions are stored in a single JSON file mapping user IDs to
    ``AccountSession`` data.
    """

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

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
        user_id: str,
        email: str,
        display_name: str | None = None,
    ) -> None:
        """Record a signed-in account (or update an existing one's display fields).

        Preserves ``workspace_ids`` if the user is already known.
        """
        with self._lock:
            sessions = self._read_all()
            existing = sessions.get(user_id)
            workspace_ids = existing.workspace_ids if existing is not None else []
            sessions[user_id] = AccountSession(
                user_id=SuperTokensUserId(user_id),
                email=email,
                display_name=display_name,
                workspace_ids=workspace_ids,
            )
            self._write_all(sessions)
            logger.info("Stored session for user {} ({})", email, user_id[:8])

    def remove_session(self, user_id: str) -> None:
        """Remove an account record (log out). Does NOT disassociate workspaces."""
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

    def get_account_email(self, user_id: str) -> str | None:
        """Return the email for a user_id, or None if the account isn't known."""
        session = self.get_session(user_id)
        if session is None:
            return None
        return str(session.email)

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
