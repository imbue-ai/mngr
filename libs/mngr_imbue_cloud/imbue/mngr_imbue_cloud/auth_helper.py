"""Token-refresh glue between session_store and the connector client.

Every authenticated CLI command and every provider operation that talks to
the connector should fetch the access token through ``get_active_token`` so
that an expired (but refreshable) token is transparently rotated before the
real call is made.
"""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import SecretStr

from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.session_store import _decode_jwt_exp


def _refresh_lock_path(sessions_dir: Path, user_id: SuperTokensUserId) -> Path:
    """Return the cross-process lock file path for a user's refresh sequence.

    Lives next to the session JSON so a stale minds install with a different
    sessions_dir won't accidentally share the same lock.
    """
    return sessions_dir / f"{user_id}.refresh.lock"


@contextmanager
def _refresh_lock(sessions_dir: Path, user_id: SuperTokensUserId) -> Iterator[None]:
    """Hold an exclusive cross-process flock around a refresh-rotate-save sequence.

    Why this matters: SuperTokens treats *any* re-use of the same refresh
    token as token theft and revokes the entire session family. Without
    serialization, two processes (e.g. the minds Python server's first
    discovery + the ``mngr observe`` subprocess started shortly after,
    both holding the same refresh_token from the persisted session JSON)
    can race their ``auth_refresh_session`` calls; the second to arrive
    sends the already-rotated old refresh_token and trips the heuristic
    -- after which every subsequent refresh for that user fails until
    they sign in again. ``fcntl.flock`` is advisory but every refresh
    path goes through this helper, so the advisory contract is binding
    in practice.

    The lock fd is opened ``O_RDWR | O_CREAT`` (via ``"a+"`` mode -- which
    creates the file but doesn't truncate, so concurrent openers all get
    the same inode) and explicitly unlocked + closed on context exit so
    a crashed holder doesn't keep the lock indefinitely.
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _refresh_lock_path(sessions_dir, user_id)
    # ``a+`` opens for read+write without truncating an existing file, and
    # creates one if it doesn't exist. We never write to the lock file --
    # its contents don't matter; the inode is the synchronization primitive.
    with open(lock_path, "a+") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def _refresh_locked(
    store: ImbueCloudSessionStore,
    client: ImbueCloudConnectorClient,
    session: AuthSession,
) -> AuthSession:
    """Perform the actual refresh + save. Caller MUST hold the per-user refresh lock.

    Split out so ``force_refresh`` and ``get_active_token`` can both
    re-load the session under the lock and only then decide whether
    they still need to rotate (avoiding redundant work after another
    process won the race and already rotated).
    """
    if session.refresh_token is None:
        raise ImbueCloudAuthError(
            f"No refresh token stored for {session.email!s}. "
            f"Run `mngr imbue_cloud auth signin --account {session.email}` again."
        )
    refreshed = client.auth_refresh_session(session.refresh_token)
    if refreshed.get("status") not in (None, "OK"):
        raise ImbueCloudAuthError(
            f"Refresh rejected by connector: {refreshed.get('message') or refreshed.get('status')}"
        )
    # The connector wraps the tokens in a ``SessionTokens`` body
    # (``{status, tokens: {access_token, refresh_token}}``). Older test
    # fixtures may flatten them, so we accept either shape.
    nested_tokens = refreshed.get("tokens")
    tokens: dict[str, object] = nested_tokens if isinstance(nested_tokens, dict) else refreshed
    new_access = tokens.get("access_token")
    new_refresh_raw = tokens.get("refresh_token")
    new_refresh = new_refresh_raw if isinstance(new_refresh_raw, str) else session.refresh_token.get_secret_value()
    if not isinstance(new_access, str) or not new_access:
        raise ImbueCloudAuthError("Refresh response missing access_token")
    refreshed_session = AuthSession(
        user_id=session.user_id,
        email=session.email,
        display_name=session.display_name,
        access_token=SecretStr(new_access),
        refresh_token=SecretStr(new_refresh),
        access_token_expires_at=_decode_jwt_exp(new_access),
    )
    store.save(refreshed_session)
    return refreshed_session


def force_refresh(
    store: ImbueCloudSessionStore,
    client: ImbueCloudConnectorClient,
    account: ImbueCloudAccount,
) -> AuthSession:
    """Unconditionally rotate this account's access + refresh tokens.

    Holds an exclusive cross-process file lock for the duration of the
    refresh-rotate-save cycle so concurrent callers don't race each
    other into a SuperTokens "token theft" revoke.
    """
    session = store.load_by_account(account)
    if session is None:
        raise ImbueCloudAuthError(
            f"No imbue_cloud session for account {account!s}. "
            f"Run `mngr imbue_cloud auth signin --account {account}` first."
        )
    with _refresh_lock(store.sessions_dir, session.user_id):
        # Re-load after acquiring the lock: another process may have just
        # rotated. ``force_refresh`` semantically means "rotate now", but
        # racing with ourselves is what tripped the theft detector in the
        # first place; rotating off the just-rotated tokens is fine and
        # avoids the race.
        latest = store.load_by_account(account)
        if latest is None:
            raise ImbueCloudAuthError(
                f"No imbue_cloud session for account {account!s} after lock acquisition. "
                f"Was the session removed concurrently?"
            )
        return _refresh_locked(store, client, latest)


def get_active_token(
    store: ImbueCloudSessionStore,
    client: ImbueCloudConnectorClient,
    account: ImbueCloudAccount,
) -> SecretStr:
    """Return a fresh access token for ``account``, refreshing if needed.

    Raises ``ImbueCloudAuthError`` if no session exists or refresh fails.
    """
    session = store.load_by_account(account)
    if session is None:
        raise ImbueCloudAuthError(
            f"No imbue_cloud session for account {account!s}. "
            f"Run `mngr imbue_cloud auth signin --account {account}` first."
        )
    if not store.is_access_token_near_expiry(session):
        return session.access_token
    # Take the refresh lock and re-check expiry: another process may have
    # already refreshed while we were waiting, in which case we should use
    # *their* fresh token rather than refreshing again (which would race
    # them into a theft-detection revoke).
    with _refresh_lock(store.sessions_dir, session.user_id):
        latest = store.load_by_account(account)
        if latest is None:
            raise ImbueCloudAuthError(
                f"No imbue_cloud session for account {account!s} after lock acquisition. "
                f"Was the session removed concurrently?"
            )
        if not store.is_access_token_near_expiry(latest):
            return latest.access_token
        refreshed_session = _refresh_locked(store, client, latest)
        return refreshed_session.access_token
