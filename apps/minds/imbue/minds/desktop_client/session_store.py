"""Workspace<->account association store for the minds desktop client.

The mngr_imbue_cloud plugin owns the SuperTokens session state on disk
(tokens, the email -> user_id index, and the active-account marker).
Minds keeps only one piece of state the plugin can't know about: the
association between a workspace agent_id and the user_id of the account
that owns it.

When callers need account *identity* (email, display_name) the store
fetches it on demand from the plugin via
``ImbueCloudCli.auth_list()``. Results are cached in memory so the
chrome SSE / workspace list rendering paths don't fan out into
subprocesses on every poll. Sign-in / sign-out flows must call
:meth:`invalidate_identity_cache` so the cache stays in sync with the
plugin's view of who is signed in.
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
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError

_WORKSPACE_ASSOCIATIONS_FILENAME = "workspace_associations.json"
_LEGACY_SESSIONS_FILENAME = "sessions.json"
_USER_ID_PREFIX_LENGTH = 16


class SuperTokensUserId(NonEmptyStr):
    """A SuperTokens user ID (UUID v4)."""

    ...


class UserIdPrefix(NonEmptyStr):
    """First 16 hex chars of a SuperTokens user ID, used for tunnel naming."""

    ...


class AccountSession(FrozenModel):
    """Identity of one signed-in account joined with its workspace_ids.

    Built on demand by :class:`MultiAccountSessionStore` from
    ``ImbueCloudCli.auth_list()`` (identity: ``user_id`` / ``email`` /
    ``display_name``) and the local on-disk associations file
    (``workspace_ids``).
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
    """Joins plugin-owned auth identity with minds-local workspace associations.

    Disk layout: ``<data_dir>/workspace_associations.json`` mapping
    ``user_id -> [agent_id, ...]``. No identity fields are stored.

    Identity is sourced from ``ImbueCloudCli.auth_list()`` and cached in
    memory; sign-in / sign-out callers must invoke
    :meth:`invalidate_identity_cache` so the cache stays consistent
    with the plugin's view.
    """

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    cli: ImbueCloudCli = Field(frozen=True, description="Plugin CLI used to source account identity")
    _disk_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _cache_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _identity_cache: dict[str, ImbueCloudAuthAccount] | None = PrivateAttr(default=None)

    @property
    def _associations_path(self) -> Path:
        return self.data_dir / _WORKSPACE_ASSOCIATIONS_FILENAME

    @property
    def _legacy_sessions_path(self) -> Path:
        return self.data_dir / _LEGACY_SESSIONS_FILENAME

    # -- Disk: workspace associations ---------------------------------------

    def _read_associations_unlocked(self) -> dict[str, list[str]]:
        """Read ``user_id -> [agent_id, ...]`` from disk.

        Falls back to the legacy ``sessions.json`` (which used to store
        full identity records) when ``workspace_associations.json``
        doesn't yet exist, extracting just the ``workspace_ids`` field
        from each entry. Returns an empty dict on missing / corrupt
        files so a brand-new install starts clean.
        """
        path = self._associations_path
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Failed to load workspace associations: {}", e)
                return {}
            if not isinstance(raw, dict):
                return {}
            result: dict[str, list[str]] = {}
            for user_id, value in raw.items():
                if not isinstance(value, list):
                    continue
                result[user_id] = [str(v) for v in value if isinstance(v, str)]
            return result

        legacy = self._legacy_sessions_path
        if not legacy.exists():
            return {}
        try:
            raw = json.loads(legacy.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load legacy sessions file: {}", e)
            return {}
        if not isinstance(raw, dict):
            return {}
        result_legacy: dict[str, list[str]] = {}
        for user_id, data in raw.items():
            if not isinstance(data, dict):
                continue
            workspace_ids = data.get("workspace_ids", [])
            if isinstance(workspace_ids, list):
                result_legacy[user_id] = [str(v) for v in workspace_ids if isinstance(v, str)]
        return result_legacy

    def _write_associations_unlocked(self, associations: dict[str, list[str]]) -> None:
        """Persist ``user_id -> [agent_id, ...]`` atomically."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._associations_path
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(associations, indent=2))
        tmp_path.chmod(0o600)
        tmp_path.rename(path)

    # -- Identity cache (sourced from the plugin) ---------------------------

    def invalidate_identity_cache(self) -> None:
        """Drop the cached ``auth list`` result.

        Callers must invoke this whenever a sign-in / sign-out / oauth
        flow successfully runs, so the cache reflects the plugin's view
        on the next read.
        """
        with self._cache_lock:
            self._identity_cache = None

    def _identity_by_user_id(self, refresh: bool = False) -> dict[str, ImbueCloudAuthAccount]:
        with self._cache_lock:
            if not refresh and self._identity_cache is not None:
                # Return a shallow copy so that an ``invalidate_identity_cache``
                # call from another thread can't swap the underlying dict
                # while a caller iterates over it.
                return dict(self._identity_cache)
            try:
                accounts = self.cli.auth_list()
            except ImbueCloudCliError as exc:
                logger.warning("Failed to list imbue_cloud accounts: {}", exc)
                # Don't poison the cache with the empty fallback: a
                # transient subprocess failure would otherwise stick
                # ``no accounts`` until the next sign-in / sign-out
                # invalidates the cache. Return an empty mapping for
                # this call only and let the next read retry.
                return {}
            self._identity_cache = {account.user_id: account for account in accounts}
            return dict(self._identity_cache)

    # -- Public read API ----------------------------------------------------

    def list_accounts(self) -> list[AccountSession]:
        """Return every signed-in account, joined with any workspaces it owns."""
        identity = self._identity_by_user_id()
        with self._disk_lock:
            associations = self._read_associations_unlocked()
        return [_build_session(account, associations.get(user_id, [])) for user_id, account in identity.items()]

    def get_session(self, user_id: str) -> AccountSession | None:
        """Return ``user_id``'s session record, or None if not signed in."""
        identity = self._identity_by_user_id()
        account = identity.get(user_id)
        if account is None:
            return None
        with self._disk_lock:
            associations = self._read_associations_unlocked()
        return _build_session(account, associations.get(user_id, []))

    def get_account_email(self, user_id: str) -> str | None:
        """Return the email for ``user_id``, or None if not signed in."""
        identity = self._identity_by_user_id()
        account = identity.get(user_id)
        return None if account is None else account.email

    def get_user_info(self, user_id: str) -> UserInfo | None:
        """Return the UI-side ``UserInfo`` for ``user_id``, or None."""
        identity = self._identity_by_user_id()
        account = identity.get(user_id)
        if account is None:
            return None
        return UserInfo(
            user_id=SuperTokensUserId(account.user_id),
            email=account.email,
            display_name=account.display_name,
            user_id_prefix=derive_user_id_prefix(account.user_id),
        )

    def get_account_for_workspace(self, agent_id: str) -> AccountSession | None:
        """Find the account that owns ``agent_id`` (or None if private)."""
        with self._disk_lock:
            associations = self._read_associations_unlocked()
        for user_id, workspace_ids in associations.items():
            if agent_id in workspace_ids:
                identity = self._identity_by_user_id()
                account = identity.get(user_id)
                if account is None:
                    return None
                return _build_session(account, workspace_ids)
        return None

    def is_any_signed_in(self) -> bool:
        """Whether at least one account is currently signed in (per the plugin)."""
        return bool(self._identity_by_user_id())

    def has_signed_in_before(self) -> bool:
        """Whether the user has ever signed in (associations file or plugin reports anything)."""
        if self._associations_path.exists() or self._legacy_sessions_path.exists():
            return True
        return self.is_any_signed_in()

    # -- Public write API (workspace associations) -------------------------

    def associate_workspace(self, user_id: str, agent_id: str) -> None:
        """Bind ``agent_id`` to ``user_id`` on disk."""
        with self._disk_lock:
            associations = self._read_associations_unlocked()
            existing = associations.get(user_id, [])
            if agent_id in existing:
                return
            associations[user_id] = [*existing, agent_id]
            self._write_associations_unlocked(associations)
            logger.info("Associated workspace {} with user {}", agent_id, user_id[:8])

    def disassociate_workspace(self, user_id: str, agent_id: str) -> None:
        """Remove ``agent_id`` from ``user_id``'s workspace list."""
        with self._disk_lock:
            associations = self._read_associations_unlocked()
            existing = associations.get(user_id, [])
            if agent_id not in existing:
                return
            associations[user_id] = [wid for wid in existing if wid != agent_id]
            self._write_associations_unlocked(associations)
            logger.info("Disassociated workspace {} from user {}", agent_id, user_id[:8])


def _build_session(account: ImbueCloudAuthAccount, workspace_ids: list[str]) -> AccountSession:
    return AccountSession(
        user_id=SuperTokensUserId(account.user_id),
        email=account.email,
        display_name=account.display_name,
        workspace_ids=list(workspace_ids),
    )
