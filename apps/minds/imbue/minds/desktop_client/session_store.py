"""Workspace<->account association store for the minds desktop client.

The mngr_imbue_cloud plugin owns the SuperTokens session state on disk
(tokens, the email -> user_id index, and the active-account marker).
Association is record existence: a workspace belongs to the account whose
workspace-record replica (see ``workspace_record_store``) holds an ACTIVE
record for it. This store joins that view with account *identity* (email,
display_name), which it fetches on demand from the plugin via
``ImbueCloudCli.auth_list()`` and caches in memory so the chrome SSE /
workspace list rendering paths don't fan out into subprocesses on every
poll. Sign-in / sign-out flows must call :meth:`invalidate_identity_cache`
so the cache stays in sync with the plugin's view of who is signed in.
"""

import threading
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import WorkspaceSyncError

_USER_ID_PREFIX_LENGTH = 16
_LEGACY_ASSOCIATIONS_FILENAME = "workspace_associations.json"
_LEGACY_SESSIONS_FILENAME = "sessions.json"


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
    ``display_name``) and the workspace-record replica (``workspace_ids``).
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
    """Joins plugin-owned auth identity with the workspace-record association view.

    Identity is sourced from ``ImbueCloudCli.auth_list()`` and cached in
    memory; sign-in / sign-out callers must invoke
    :meth:`invalidate_identity_cache` so the cache stays consistent with the
    plugin's view. Associations come from (and are written through) the
    :class:`WorkspaceRecordStore`; when none is configured every workspace
    reads as private and association writes raise.
    """

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    cli: ImbueCloudCli = Field(frozen=True, description="Plugin CLI used to source account identity")
    record_store: WorkspaceRecordStore | None = Field(
        default=None, description="Association source of truth; None disables associations entirely"
    )
    _cache_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _identity_cache: dict[str, ImbueCloudAuthAccount] | None = PrivateAttr(default=None)
    _is_last_identity_read_failed: bool = PrivateAttr(default=False)

    # -- Identity cache (sourced from the plugin) ---------------------------

    @property
    def is_last_identity_read_failed(self) -> bool:
        """Whether the most recent ``auth list`` read failed (empty fallback).

        Lets callers distinguish "the user has no accounts" from "the account
        listing was unavailable" -- e.g. the landing route must not bounce a
        just-signed-in user back to the welcome splash because a transient
        subprocess failure made ``list_accounts()`` return empty.
        """
        with self._cache_lock:
            return self._is_last_identity_read_failed

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
                # Don't poison the cache with the empty fallback: a transient
                # subprocess failure would otherwise stick ``no accounts``
                # until the next sign-in / sign-out invalidates the cache.
                self._is_last_identity_read_failed = True
                return {}
            self._is_last_identity_read_failed = False
            self._identity_cache = {account.user_id: account for account in accounts}
            return dict(self._identity_cache)

    def _associations_view(self) -> dict[str, list[str]]:
        if self.record_store is None:
            return {}
        return self.record_store.associations_view()

    def _require_account(self, user_id: str) -> ImbueCloudAuthAccount:
        """Resolve a signed-in account by user_id, refreshing the cache once on a miss."""
        account = self._identity_by_user_id().get(user_id)
        if account is None:
            account = self._identity_by_user_id(refresh=True).get(user_id)
        if account is None:
            raise WorkspaceSyncError(f"No signed-in account matches user id {user_id[:8]}")
        return account

    # -- Public read API ----------------------------------------------------

    def list_accounts(self) -> list[AccountSession]:
        """Return every signed-in account, joined with any workspaces it owns."""
        identity = self._identity_by_user_id()
        associations = self._associations_view()
        return [_build_session(account, associations.get(user_id, [])) for user_id, account in identity.items()]

    def get_session(self, user_id: str) -> AccountSession | None:
        """Return ``user_id``'s session record, or None if not signed in."""
        identity = self._identity_by_user_id()
        account = identity.get(user_id)
        if account is None:
            return None
        return _build_session(account, self._associations_view().get(user_id, []))

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
        """Find the account that owns ``agent_id`` (or None if private).

        When the replica's owner isn't in the cached ``auth list`` snapshot,
        the cache is refreshed once before giving up -- this recovers from an
        identity cache populated before a signin rotated to a new user_id.
        """
        associations = self._associations_view()
        for user_id, workspace_ids in associations.items():
            if agent_id in workspace_ids:
                identity = self._identity_by_user_id()
                account = identity.get(user_id)
                if account is None:
                    identity = self._identity_by_user_id(refresh=True)
                    account = identity.get(user_id)
                if account is None:
                    return None
                return _build_session(account, workspace_ids)
        return None

    def is_any_signed_in(self) -> bool:
        """Whether at least one account is currently signed in (per the plugin)."""
        return bool(self._identity_by_user_id())

    def has_signed_in_before(self) -> bool:
        """Whether the user has ever signed in (replica/legacy state exists or the plugin reports anything).

        The legacy files are matched by prefix so their retired
        (``.pre-sync``-renamed) copies still count after the one-shot
        record-store conversion.
        """
        has_local_state = bool(self._associations_view()) or any(
            any(self.data_dir.glob(f"{name}*")) for name in (_LEGACY_ASSOCIATIONS_FILENAME, _LEGACY_SESSIONS_FILENAME)
        )
        if has_local_state:
            return True
        return self.is_any_signed_in()

    # -- Public write API (workspace associations) -------------------------

    def associate_workspace(self, user_id: str, agent_id: str, resolver: BackendResolverInterface) -> None:
        """Bind ``agent_id`` to ``user_id`` by creating its workspace record (settings semantics).

        Pushes synchronously: raises ``WorkspaceSyncError`` when the connector
        is unreachable, when the workspace isn't locally discovered, or when it
        is owned by another account (disassociate first, then associate).
        """
        if self.record_store is None:
            raise WorkspaceSyncError("Workspace sync is not configured; cannot associate workspaces")
        account = self._require_account(user_id)
        self.record_store.associate_workspace_or_raise(user_id, account.email, agent_id, resolver)
        logger.info("Associated workspace {} with user {}", agent_id, user_id[:8])

    def associate_created_workspace(
        self,
        user_id: str,
        agent_id: str,
        host_id: str,
        display_name: str,
        color: str | None,
        is_cloud_row: bool,
    ) -> None:
        """Create-path association: seed a minimal record now, queued for push.

        Runs right after ``mngr create`` returns the canonical ids -- before
        discovery has seen the workspace -- so the record starts with just the
        form metadata. The reconcile's metadata refresh enriches it (provider,
        secrets) once discovery catches up. Never blocks or fails creation:
        a push failure just leaves the row dirty for the reconcile.
        """
        if self.record_store is None:
            logger.warning("Workspace sync is not configured; created workspace {} stays private", agent_id)
            return
        account = self._require_account(user_id)
        seed = ReplicaRecord(
            host_id=host_id,
            agent_id=agent_id,
            display_name=display_name or agent_id,
            color=color,
            provider_kind="",
            hosting_device_id=None if is_cloud_row else self.record_store.device_id,
            device_label=self.record_store.device_label,
        )
        self.record_store.upsert_local_record(user_id, account.email, seed)
        logger.info("Associated created workspace {} with user {}", agent_id, user_id[:8])

    def disassociate_workspace(self, user_id: str, agent_id: str) -> None:
        """Remove ``agent_id``'s record (the workspace becomes private; requires connectivity)."""
        if self.record_store is None:
            return
        account = self._require_account(user_id)
        self.record_store.disassociate_workspace_or_raise(user_id, account.email, agent_id)
        logger.info("Disassociated workspace {} from user {}", agent_id, user_id[:8])


def _build_session(account: ImbueCloudAuthAccount, workspace_ids: list[str]) -> AccountSession:
    return AccountSession(
        user_id=SuperTokensUserId(account.user_id),
        email=account.email,
        display_name=account.display_name,
        workspace_ids=list(workspace_ids),
    )
